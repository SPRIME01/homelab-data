#!/usr/bin/env python3

import os
import sys
import json
import time
import yaml
import aio_pika # Changed from pika
import aiohttp
import asyncio
import logging
import backoff
import jsonschema
from typing import Dict, Any, Optional
from dataclasses import dataclass, asdict # Added asdict
from datetime import datetime
from aiohttp.client_exceptions import ClientError

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('ha_command_subscriber.log')
    ]
)
logger = logging.getLogger("ha_command_subscriber")

# Command schema for validation
COMMAND_SCHEMA = {
    "type": "object",
    "required": ["domain", "service", "target"],
    "properties": {
        "domain": {"type": "string"},
        "service": {"type": "string"},
        "target": {
            "type": "object",
            "properties": {
                "entity_id": {
                    "oneOf": [
                        {"type": "string"},
                        {"type": "array", "items": {"type": "string"}}
                    ]
                }
            }
        },
        "service_data": {"type": "object"},
        "priority": {"type": "integer", "minimum": 0, "maximum": 10},
        "retry": {
            "type": "object",
            "properties": {
                "max_attempts": {"type": "integer", "minimum": 1},
                "delay": {"type": "number", "minimum": 0}
            }
        }
    }
}

@dataclass
class CommandResult:
    """Represents the result of a command execution."""
    success: bool
    message: str
    timestamp: str
    response: Optional[Dict] = None
    error: Optional[str] = None

class HACommandSubscriber:
    def __init__(self, config_path: str = "config.yaml"):
        """
        Initializes the HACommandSubscriber with configuration and prepares connection attributes.
        
        Loads configuration from the specified YAML file and sets up attributes for RabbitMQ connection, channel, HTTP session, command results storage, and consumer task tracking.
        """
        self.config = self._load_config(config_path)
        self.connection: Optional[aio_pika.RobustConnection] = None # Typed
        self.channel: Optional[aio_pika.Channel] = None # Typed
        self.should_exit = False
        self.session: Optional[aiohttp.ClientSession] = None # Typed
        self.command_results = {}
        self._consuming_tasks = [] # To keep track of consumer tasks

    def _load_config(self, config_path: str) -> Dict:
        """
        Loads and validates configuration from a YAML file.
        
        Ensures that the configuration contains the required sections for RabbitMQ, Home Assistant, and queue definitions, including both 'commands' and 'results' queues. Exits the program if loading or validation fails.
        
        Args:
            config_path: Path to the YAML configuration file.
        
        Returns:
            A dictionary containing the validated configuration data.
        """
        try:
            with open(config_path, 'r') as f:
                config = yaml.safe_load(f)

            # Simplified required_keys for this example, assuming config structure is known
            required_keys = ['rabbitmq', 'home_assistant', 'queues']
            for key in required_keys:
                if key not in config:
                    raise ValueError(f"Missing required configuration section: {key}")
            if 'commands' not in config['queues'] or 'results' not in config['queues']:
                 raise ValueError("Missing 'commands' or 'results' queue configuration.")


            return config
        except Exception as e:
            logger.error(f"Failed to load configuration: {e}")
            sys.exit(1)

    async def connect_rabbitmq(self) -> None:
        """
        Asynchronously establishes a connection to RabbitMQ and declares command and results queues with retry logic.
        
        Attempts to connect to RabbitMQ using configuration parameters, declaring all command queues and the results queue as durable with specified TTL and priority settings. Retries the connection on failure until signaled to exit.
        """
        rabbitmq_config = self.config['rabbitmq']
        connection_url = (
            f"amqp://{rabbitmq_config['username']}:{rabbitmq_config['password']}@"
            f"{rabbitmq_config['host']}:{rabbitmq_config['port']}/"
        )
        
        while not self.should_exit:
            try:
                self.connection = await aio_pika.connect_robust(
                    connection_url,
                    loop=asyncio.get_running_loop(),
                    heartbeat=60
                )
                self.channel = await self.connection.channel()
                await self.channel.set_qos(prefetch_count=self.config.get('prefetch_count', 10))

                logger.info("Successfully connected to RabbitMQ and channel opened.")
                
                # Declare queues specified in the 'queues' section (not 'queues':'commands')
                # Assuming self.config['queues'] itself is a list of queue objects
                # or that 'command_queues' is the correct key.
                # For this refactor, I'll assume 'command_queues' is the list of queues to declare for consuming
                # and 'results_queue_name' is for publishing.
                
                # The original code declared queues from self.config['queues'].
                # Let's assume self.config['queues']['commands'] is a list of queue objects to declare
                # And self.config['queues']['results'] is the config for the results queue.
                
                for queue_config in self.config['queues'].get('commands', []): # Assuming 'commands' is a list of queues to consume from
                    queue_name = queue_config['name']
                    await self.channel.declare_queue(
                        name=queue_name,
                        durable=True,
                        arguments={
                            'x-message-ttl': queue_config.get('ttl', 60000),
                            'x-max-priority': queue_config.get('max_priority', 10)
                        }
                    )
                    logger.info(f"Declared queue: {queue_name}")

                # Declare results queue as well, if it's defined and needs declaration by subscriber
                results_queue_config = self.config['queues'].get('results')
                if results_queue_config and results_queue_config.get('name'):
                    results_queue_name = results_queue_config['name']
                    await self.channel.declare_queue(
                        name=results_queue_name,
                        durable=True,
                        arguments={
                            'x-message-ttl': results_queue_config.get('ttl', 60000),
                            'x-max-priority': results_queue_config.get('max_priority', 10)
                        }
                    )
                    logger.info(f"Declared results queue: {results_queue_name}")

                return
            except (aio_pika.exceptions.AMQPConnectionError, ConnectionRefusedError) as e: # More specific exceptions
                logger.error(f"Failed to connect to RabbitMQ: {e}")
                await asyncio.sleep(5)
            except Exception as e: # Catch other potential errors during setup
                logger.error(f"An unexpected error occurred during RabbitMQ setup: {e}")
                await asyncio.sleep(5)


    async def setup_http_session(self) -> None:
        """
        Initializes an HTTP session for communicating with the Home Assistant API.
        
        Creates a new aiohttp.ClientSession with the appropriate base URL and authorization
        headers if no session exists or the existing session is closed.
        """
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(
                base_url=self.config['home_assistant']['url'],
                headers={
                    "Authorization": f"Bearer {self.config['home_assistant']['token']}",
                    "Content-Type": "application/json"
                }
            )

    def validate_command(self, command: Dict) -> bool:
        """
        Validates a command dictionary against the predefined JSON schema.
        
        Args:
            command: The command data to validate.
        
        Returns:
            True if the command is valid according to the schema, otherwise False.
        """
        try:
            jsonschema.validate(instance=command, schema=COMMAND_SCHEMA)
            return True
        except jsonschema.exceptions.ValidationError as e:
            logger.error(f"Command validation failed: {e}")
            return False

    @backoff.on_exception(
        backoff.expo,
        (ClientError, asyncio.TimeoutError),
        max_tries=3
    )
    async def execute_command(self, command: Dict) -> CommandResult:
        """
        Executes a command against the Home Assistant API and returns the result.
        
        Sends a POST request to the Home Assistant services endpoint using the provided
        command details. Returns a CommandResult indicating success or failure, including
        the API response or error message.
        """
        # Ensure session is created for each command execution if it's not persistent
        if self.session is None or self.session.closed:
             await self.setup_http_session()

        domain, service = "unknown", "unknown" # Define for error case
        try:
            domain = command['domain']
            service = command['service']
            target = command['target']
            service_data = command.get('service_data', {})

            url = f"/api/services/{domain}/{service}"
            data = {**target, **(service_data or {})}
            
            assert self.session is not None # Make type checker happy
            async with self.session.post(url, json=data) as response:
                response.raise_for_status()
                result = await response.json()

                return CommandResult(
                    success=True,
                    message=f"Successfully executed {domain}.{service}",
                    timestamp=datetime.utcnow().isoformat(),
                    response=result
                )

        except Exception as e:
            error_msg = str(e)
            logger.error(f"Command execution failed for {domain}.{service}: {error_msg}")
            return CommandResult(
                success=False,
                message=f"Failed to execute {domain}.{service}",
                timestamp=datetime.utcnow().isoformat(),
                error=error_msg
            )

    async def process_command(self, command_data: Dict) -> None:
        """
        Processes a command by validating, executing, and retrying it as configured.
        
        Attempts to execute the provided command up to the specified number of retries on failure. Publishes the result of the execution, whether successful or after exhausting all attempts.
        """
        if not self.validate_command(command_data):
            logger.error(f"Invalid command format: {command_data}")
            # Consider sending to a dead-letter queue or logging permanently
            return

        retry_config = command_data.get('retry', {'max_attempts': 1, 'delay': 0.5}) # Default to 1 attempt
        attempts = 0

        while attempts < retry_config['max_attempts']:
            result = await self.execute_command(command_data)

            if result.success:
                self.command_results[command_data.get('id', str(time.time()))] = result
                await self.publish_result(result)
                break 
            
            attempts += 1
            if attempts < retry_config['max_attempts']:
                logger.info(f"Command failed, attempt {attempts}/{retry_config['max_attempts']}. Retrying in {retry_config['delay']}s...")
                await asyncio.sleep(retry_config['delay'])
            else:
                logger.error(f"Command failed after {attempts} attempts: {command_data.get('id', 'N/A')}")
                await self.publish_result(result) # Publish final failure

    async def publish_result(self, result: CommandResult) -> None:
        """
        Publishes a command execution result to the configured results queue in RabbitMQ.
        
        If the channel is unavailable or closed, the result is not published.
        """
        if not self.channel or self.channel.is_closed:
            logger.error("Cannot publish result, channel is not available.")
            return
        try:
            # Assuming 'results' key in self.config['queues'] holds the name of the results queue
            results_queue_name = self.config['queues']['results']['name']
            
            message_body = json.dumps(asdict(result)).encode('utf-8')
            message = aio_pika.Message(
                body=message_body,
                delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                content_type='application/json'
            )
            await self.channel.default_exchange.publish(message, routing_key=results_queue_name)
            logger.debug(f"Published result to {results_queue_name}")
        except Exception as e:
            logger.error(f"Failed to publish result: {e}")


    async def on_message(self, message: aio_pika.IncomingMessage) -> None:
        """
        Handles incoming RabbitMQ messages by decoding and processing commands asynchronously.
        
        Attempts to decode the message body as JSON and process it as a command. Acknowledges the message on success, rejects non-JSON messages without requeue, and negatively acknowledges other errors without requeue.
        """
        async with message.process(ignore_processed=True): # Auto ack/nack based on context exit
            try:
                command = json.loads(message.body.decode())
                # Using asyncio.create_task to allow concurrent processing if desired,
                # but for sequential processing within one consumer, can await directly.
                # For simplicity here, let's await directly if the number of workers is 1.
                # If more complex concurrency is needed, create_task is fine.
                await self.process_command(command)
                await message.ack()
            except json.JSONDecodeError:
                logger.error("Failed to decode message body")
                await message.reject(requeue=False) # Reject non-JSON messages
            except Exception as e:
                logger.error(f"Error processing message: {e}")
                # Decide whether to requeue based on error type or configuration
                await message.nack(requeue=False) # Example: Nack without requeue

    async def start_consuming(self) -> None:
        """
        Begins asynchronous consumption of messages from all configured command queues.
        
        For each command queue specified in the configuration, starts a consumer that processes incoming messages using the on_message handler. Stores consumer tags for later cancellation. Logs errors if a queue cannot be consumed or if no consumers are started.
        """
        if not self.channel:
            logger.error("Cannot start consuming, channel is not available.")
            return

        # Assuming self.config['queues']['commands'] is a list of queue configurations
        for queue_config in self.config['queues'].get('commands', []):
            queue_name = queue_config['name']
            try:
                queue = await self.channel.get_queue(queue_name) # Get queue instance
                consumer_tag = await queue.consume(self.on_message)
                self._consuming_tasks.append(consumer_tag) # Store consumer tag to cancel later
                logger.info(f"Started consuming from queue: {queue_name} with tag {consumer_tag}")
            except Exception as e:
                logger.error(f"Failed to start consuming from queue {queue_name}: {e}")
        
        if not self._consuming_tasks:
            logger.warning("No consumers were started.")


    async def run(self) -> None:
        """
        Starts the command subscriber service, managing its lifecycle and graceful shutdown.
        
        Initializes the HTTP session and connects to RabbitMQ, then begins consuming commands from configured queues. Keeps the service running until signaled to exit. On shutdown or error, cancels consumers and closes all connections and resources cleanly.
        """
        await self.setup_http_session() # Setup HTTP session once
        try:
            await self.connect_rabbitmq()
            if self.channel: # Ensure channel is available
                 await self.start_consuming()
                 # Keep the service running
                 while not self.should_exit:
                     await asyncio.sleep(1)
            else:
                logger.error("Failed to establish RabbitMQ channel. Exiting.")
                self.should_exit = True

        except Exception as e:
            logger.error(f"Service error: {e}", exc_info=True)
            self.should_exit = True
        finally:
            logger.info("Shutting down...")
            # Cancel consumer tasks
            if self.channel and not self.channel.is_closed:
                for consumer_tag in self._consuming_tasks:
                    try:
                        logger.info(f"Cancelling consumer: {consumer_tag}")
                        await self.channel.cancel(consumer_tag)
                    except Exception as e:
                        logger.error(f"Error cancelling consumer {consumer_tag}: {e}")
            
            if self.session and not self.session.closed:
                await self.session.close()
                logger.info("HTTP session closed.")
            if self.connection and not self.connection.is_closed:
                await self.connection.close()
                logger.info("RabbitMQ connection closed.")

def main():
    """
    Initializes and runs the Home Assistant command subscriber service.
    
    Reads configuration, starts the asynchronous event loop, and manages graceful
    shutdown on keyboard interrupt, ensuring all tasks are cancelled before exit.
    """
    config_path = os.environ.get('CONFIG_PATH', 'config.yaml')
    subscriber = HACommandSubscriber(config_path)
    
    loop = asyncio.get_event_loop()
    try:
        loop.run_until_complete(subscriber.run())
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt received.")
        subscriber.should_exit = True
        # Give some time for graceful shutdown, then force stop if needed
        # This part might need more robust handling in subscriber.run() itself.
        loop.run_until_complete(asyncio.sleep(2)) # Allow ongoing tasks to finish
    finally:
        # Ensure all tasks are cancelled before closing loop
        tasks = [t for t in asyncio.all_tasks(loop) if t is not asyncio.current_task(loop)]
        for task in tasks:
            task.cancel()
        
        # Gather all tasks to ensure they are finished after cancellation
        # loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
        # loop.close() # This can cause errors if run_until_complete is called again
        logger.info("Service shutdown complete.")


if __name__ == "__main__":
    main()

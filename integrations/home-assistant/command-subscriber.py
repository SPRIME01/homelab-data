#!/usr/bin/env python3

import os
import sys
import json
import time
import yaml
import pika
import aiohttp
import asyncio
import logging
import backoff
import jsonschema
from typing import Dict, Any, Optional
from dataclasses import dataclass
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
        """Initialize the command subscriber."""
        self.config = self._load_config(config_path)
        self.connection = None
        self.channel = None
        self.should_exit = False
        self.session = None
        self.command_results = {}

    def _load_config(self, config_path: str) -> Dict:
        """Load configuration from YAML file."""
        try:
            with open(config_path, 'r') as f:
                config = yaml.safe_load(f)

            required_keys = ['rabbitmq', 'home_assistant', 'queues', 'webhook', 'logging', 'retry_config', 'monitoring', 'security']
            for key in required_keys:
                if key not in config:
                    raise ValueError(f"Missing required configuration section: {key}")

            return config
        except Exception as e:
            logger.error(f"Failed to load configuration: {e}")
            sys.exit(1)

    async def connect_rabbitmq(self) -> None:
        """Establish connection to RabbitMQ with retry logic."""
        while not self.should_exit:
            try:
                # Connection parameters
                params = pika.ConnectionParameters(
                    host=self.config['rabbitmq']['host'],
                    port=self.config['rabbitmq']['port'],
                    credentials=pika.PlainCredentials(
                        username=self.config['rabbitmq']['username'],
                        password=self.config['rabbitmq']['password']
                    ),
                    heartbeat=60,
                    connection_attempts=3
                )

                self.connection = pika.BlockingConnection(params)
                self.channel = self.connection.channel()

                # Declare queues
                for queue in self.config['queues']:
                    self.channel.queue_declare(
                        queue=queue['name'],
                        durable=True,
                        arguments={
                            'x-message-ttl': queue.get('ttl', 60000),
                            'x-max-priority': queue.get('max_priority', 10)
                        }
                    )

                logger.info("Successfully connected to RabbitMQ")
                return
            except Exception as e:
                logger.error(f"Failed to connect to RabbitMQ: {e}")
                await asyncio.sleep(5)

    async def setup_http_session(self) -> None:
        """Set up HTTP session for Home Assistant API."""
        self.session = aiohttp.ClientSession(
            base_url=self.config['home_assistant']['url'],
            headers={
                "Authorization": f"Bearer {self.config['home_assistant']['token']}",
                "Content-Type": "application/json"
            }
        )

    def validate_command(self, command: Dict) -> bool:
        """Validate command against schema."""
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
        """Execute command via Home Assistant API."""
        try:
            domain = command['domain']
            service = command['service']
            target = command['target']
            service_data = command.get('service_data', {})

            url = f"/api/services/{domain}/{service}"
            data = {**target, **(service_data or {})}

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
            logger.error(f"Command execution failed: {error_msg}")
            return CommandResult(
                success=False,
                message=f"Failed to execute {domain}.{service}",
                timestamp=datetime.utcnow().isoformat(),
                error=error_msg
            )

    async def process_command(self, command: Dict) -> None:
        """Process and execute a command with retry logic."""
        if not self.validate_command(command):
            logger.error(f"Invalid command format: {command}")
            return

        retry_config = command.get('retry', {'max_attempts': 3, 'delay': 1.0})
        attempts = 0

        while attempts < retry_config['max_attempts']:
            result = await self.execute_command(command)

            if result.success:
                # Store result and publish to results queue
                self.command_results[command.get('id', str(time.time()))] = result
                await self.publish_result(result)
                break

            attempts += 1
            if attempts < retry_config['max_attempts']:
                await asyncio.sleep(retry_config['delay'])
            else:
                logger.error(f"Command failed after {attempts} attempts")
                await self.publish_result(result)

    async def publish_result(self, result: CommandResult) -> None:
        """Publish command result to results queue."""
        try:
            result_queue = self.config['queues']['results']['name']
            self.channel.basic_publish(
                exchange='',
                routing_key=result_queue,
                body=json.dumps(result.__dict__),
                properties=pika.BasicProperties(
                    delivery_mode=2,  # Make message persistent
                    content_type='application/json'
                )
            )
        except Exception as e:
            logger.error(f"Failed to publish result: {e}")

    async def start_consuming(self) -> None:
        """Start consuming messages from command queues."""
        def callback(ch, method, properties, body):
            """Process received message."""
            try:
                command = json.loads(body)
                asyncio.create_task(self.process_command(command))
                ch.basic_ack(delivery_tag=method.delivery_tag)
            except json.JSONDecodeError:
                logger.error("Failed to decode message")
                ch.basic_reject(delivery_tag=method.delivery_tag, requeue=False)
            except Exception as e:
                logger.error(f"Error processing message: {e}")
                ch.basic_nack(delivery_tag=method.delivery_tag, requeue=True)

        # Set up consumers for each command queue
        for queue in self.config['queues']['commands']:
            self.channel.basic_qos(prefetch_count=queue.get('prefetch_count', 1))
            self.channel.basic_consume(
                queue=queue['name'],
                on_message_callback=callback
            )

        logger.info("Started consuming messages")
        try:
            self.channel.start_consuming()
        except KeyboardInterrupt:
            self.should_exit = True

    async def run(self) -> None:
        """Run the command subscriber service."""
        try:
            # Initialize connections
            await self.connect_rabbitmq()
            await self.setup_http_session()

            # Start consuming messages
            await self.start_consuming()

        except Exception as e:
            logger.error(f"Service error: {e}")
            self.should_exit = True

        finally:
            # Cleanup
            if self.session:
                await self.session.close()
            if self.connection:
                self.connection.close()

def main():
    """Main entry point."""
    # Load config path from environment or use default
    config_path = os.environ.get('CONFIG_PATH', 'config.yaml')

    # Create subscriber instance
    subscriber = HACommandSubscriber(config_path)

    # Run the service
    asyncio.run(subscriber.run())

if __name__ == "__main__":
    main()

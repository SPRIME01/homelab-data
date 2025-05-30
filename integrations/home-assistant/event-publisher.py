#!/usr/bin/env python3

import os
import sys
import json
import time
import signal
import asyncio
import logging
import aiohttp
import aio_pika # Changed from pika
import yaml
from typing import Dict, List, Optional, Any, Tuple # Added Tuple
from dataclasses import dataclass, asdict # Added asdict
from datetime import datetime
from aiohttp import web
from collections import defaultdict

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('ha_event_publisher.log')
    ]
)
logger = logging.getLogger("ha_event_publisher")

@dataclass
class EventBatch:
    """Represents a batch of events for a specific exchange/routing key."""
    events: List[Dict]
    last_update: float
    exchange_name: str # Renamed for clarity
    routing_key: str

class HAEventPublisher:
    def __init__(self, config_path: str = "config.yaml"):
        """
        Initializes the HAEventPublisher with configuration, connection placeholders, and batching state.
        
        Loads configuration from the specified YAML file, prepares placeholders for RabbitMQ connection and channel, initializes event batch storage and concurrency locks, and sets up a shutdown flag.
        """
        self.config = self.load_config(config_path)
        self.connection: Optional[aio_pika.RobustConnection] = None # Typed
        self.channel: Optional[aio_pika.Channel] = None # Typed
        self.should_exit = False
        self.event_batches = defaultdict(lambda: EventBatch([], time.time(), "", "")) # Will store exchange_name now
        self.batch_lock = asyncio.Lock()
        self._connection_retry_lock = asyncio.Lock() # To prevent multiple concurrent reconnections

        # Setup signal handlers
        # loop = asyncio.get_event_loop() # Not needed here, signal handlers added in main context
        # for sig in (signal.SIGINT, signal.SIGTERM):
        #     loop.add_signal_handler(sig, lambda s=sig: asyncio.create_task(self.signal_handler_async(s)))


    def load_config(self, config_path: str) -> Dict:
        """
        Loads and validates configuration from a YAML file.
        
        Reads the specified YAML file, ensures required sections and RabbitMQ settings are present, and returns the configuration as a dictionary. Exits the application if loading or validation fails.
        """
        try:
            with open(config_path, 'r') as f:
                config = yaml.safe_load(f)

            required_keys = ['rabbitmq', 'home_assistant', 'webhook']
            for key in required_keys:
                if key not in config:
                    raise ValueError(f"Missing required configuration section: {key}")
            if 'exchanges' not in config['rabbitmq'] or 'routing' not in config['rabbitmq']:
                 raise ValueError("Missing 'rabbitmq.exchanges' or 'rabbitmq.routing' configuration.")
            return config
        except Exception as e:
            logger.error(f"Failed to load configuration: {e}")
            sys.exit(1)

    async def signal_handler_async(self, signum: int) -> None: # Made async
        """
        Handles shutdown signals by setting the shutdown flag.
        
        Sets the internal `should_exit` flag to initiate a graceful shutdown when a termination signal is received.
        """
        logger.info(f"Received signal {signum}, initiating shutdown...")
        self.should_exit = True

    async def connect_rabbitmq(self) -> bool: # Return bool for success
        """
        Attempts to establish an asynchronous connection to RabbitMQ with configurable retry logic.
        
        Returns:
            True if the connection and channel are successfully established; False if the maximum number of attempts is reached, a connection is already in progress, or shutdown is requested.
        """
        # Prevent multiple concurrent connection attempts
        if self._connection_retry_lock.locked():
            logger.debug("Connection attempt already in progress.")
            return False

        async with self._connection_retry_lock:
            if self.connection and not self.connection.is_closed:
                logger.info("Connection already established.")
                return True

            rabbitmq_config = self.config['rabbitmq']
            connection_url = (
                f"amqp://{rabbitmq_config['username']}:{rabbitmq_config['password']}@"
                f"{rabbitmq_config['host']}:{rabbitmq_config['port']}/"
            )
            
            attempt = 0
            max_attempts = self.config['rabbitmq'].get('connection_attempts', 3) # Use from config or default
            retry_delay = 5 # seconds

            while not self.should_exit and attempt < max_attempts :
                attempt +=1
                try:
                    logger.info(f"Attempting to connect to RabbitMQ (attempt {attempt}/{max_attempts})...")
                    self.connection = await aio_pika.connect_robust(
                        connection_url,
                        loop=asyncio.get_running_loop(),
                        heartbeat=rabbitmq_config.get('heartbeat', 60)
                    )
                    self.channel = await self.connection.channel()
                    # Optional: await self.channel.set_qos(prefetch_count=10) # If needed

                    # Declare exchanges
                    for exchange_config in self.config['rabbitmq']['exchanges']:
                        await self.channel.declare_exchange(
                            name=exchange_config['name'],
                            type=aio_pika.ExchangeType(exchange_config['type']), # Use Enum
                            durable=exchange_config.get('durable', True)
                        )
                        logger.info(f"Declared exchange: {exchange_config['name']}")

                    logger.info("Successfully connected to RabbitMQ and channel opened.")
                    return True # Connection successful
                except (aio_pika.exceptions.AMQPConnectionError, ConnectionRefusedError) as e:
                    logger.error(f"Failed to connect to RabbitMQ (attempt {attempt}/{max_attempts}): {e}")
                    if attempt < max_attempts:
                        await asyncio.sleep(retry_delay)
                    else:
                        logger.error("Max connection attempts reached.")
                        return False # Max attempts reached
                except Exception as e: # Catch other potential errors
                    logger.error(f"An unexpected error occurred during RabbitMQ connection (attempt {attempt}/{max_attempts}): {e}")
                    if attempt < max_attempts:
                         await asyncio.sleep(retry_delay)
                    else:
                        logger.error("Max connection attempts reached after unexpected error.")
                        return False
            return False # Loop exited due to should_exit or max_attempts

    async def publish_event(self, event_data: Dict, exchange_name: str, routing_key: str) -> None:
        """
        Publishes an event to the specified RabbitMQ exchange with the given routing key.
        
        If the RabbitMQ connection or channel is unavailable, attempts to reconnect before publishing.
        If a channel closure occurs during publishing, reconnects and retries once. Skips publishing if a shutdown is in progress.
        """
        if self.should_exit:
            logger.warning("Shutdown in progress, not publishing event.")
            return

        if not self.connection or self.connection.is_closed or not self.channel or self.channel.is_closed:
            logger.warning("RabbitMQ connection lost. Attempting to reconnect before publishing.")
            if not await self.connect_rabbitmq(): # Try to reconnect
                logger.error("Failed to reconnect to RabbitMQ. Cannot publish event.")
                return # Exit if reconnection failed

        # This check is crucial after potential reconnection
        if not self.channel or self.channel.is_closed:
            logger.error("Channel is not available even after attempting reconnection. Cannot publish.")
            return

        try:
            exchange = await self.channel.get_exchange(exchange_name) # Get exchange object
            
            message_body = json.dumps(event_data).encode('utf-8')
            message = aio_pika.Message(
                body=message_body,
                delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                timestamp=datetime.utcnow(), # Use datetime object
                content_type='application/json'
            )
            
            await exchange.publish(message, routing_key=routing_key)
            logger.debug(f"Published event to exchange '{exchange_name}' with key '{routing_key}'")

        except aio_pika.exceptions.ChannelClosed as e:
            logger.error(f"Channel closed while publishing event: {e}. Attempting reconnect.")
            self.channel = None # Mark channel as unusable
            if await self.connect_rabbitmq(): # Try to reconnect
                 await self.publish_event(event_data, exchange_name, routing_key) # Retry publishing
            else:
                logger.error("Failed to republish event after reconnection.")
        except Exception as e:
            logger.error(f"Failed to publish event to exchange '{exchange_name}': {e}")
            # Consider more specific error handling or re-raising if critical

    def get_routing_info(self, event: Dict) -> Tuple[str, str]:
        """
        Determines the RabbitMQ exchange and routing key for a given event.
        
        Selects the exchange and routing key based on the event's type and domain using routing rules from the configuration. Falls back to default exchange and routing key pattern if no specific rule matches.
        
        Args:
            event: The event dictionary containing at least an 'event_type' key.
        
        Returns:
            A tuple containing the exchange name and routing key.
        """
        event_type = event.get('event_type', '')
        domain = event_type.split('.')[0] if '.' in event_type else 'default'

        for route_config in self.config['rabbitmq']['routing']:
            if (route_config['domain'] == domain or route_config['domain'] == '*') and \
               (route_config['event_type'] == event_type or route_config['event_type'] == '*'):
                return route_config['exchange'], route_config['routing_key'].format(
                    domain=domain,
                    event_type=event_type
                )
        
        default_exchange = self.config['rabbitmq'].get('default_exchange', 'default.events')
        default_routing_key_pattern = self.config['rabbitmq'].get('default_routing_key_pattern', 'events.{domain}.{event_type}')
        
        return (
            default_exchange,
            default_routing_key_pattern.format(domain=domain, event_type=event_type)
        )

    async def add_to_batch(self, event: Dict) -> None:
        """
        Adds an event to the batch corresponding to its exchange and routing key.
        
        If the batch does not exist, creates a new one. Updates the batch's last update timestamp.
        """
        exchange_name, routing_key = self.get_routing_info(event)
        batch_key = f"{exchange_name}:{routing_key}"

        async with self.batch_lock:
            # Ensure EventBatch gets exchange_name
            if batch_key not in self.event_batches:
                self.event_batches[batch_key] = EventBatch([], time.time(), exchange_name, routing_key)
            
            batch = self.event_batches[batch_key]
            batch.events.append(event)
            batch.last_update = time.time()

    async def process_batches(self) -> None:
        """
        Periodically processes and publishes event batches based on size or timeout thresholds.
        
        Continuously checks all event batches and publishes them to RabbitMQ when either the batch size or timeout condition is met. Batches are published as a single message if they contain multiple events, or as individual events otherwise. After publishing, empty batches are cleaned up. Runs until a shutdown signal is received.
        """
        batch_processing_interval = self.config['rabbitmq'].get('batch_process_interval', 1) # seconds
        while not self.should_exit:
            try:
                current_time = time.time()
                batch_size_config = self.config['rabbitmq'].get('batch_size', 100)
                batch_timeout_config = self.config['rabbitmq'].get('batch_timeout', 5) # seconds

                processed_keys = []
                async with self.batch_lock:
                    for batch_key, batch in list(self.event_batches.items()): # Iterate over a copy
                        if len(batch.events) >= batch_size_config or \
                           (len(batch.events) > 0 and current_time - batch.last_update >= batch_timeout_config):
                            
                            events_to_publish = list(batch.events) # Copy events to publish
                            batch.events.clear() # Clear original batch events under lock
                            batch.last_update = current_time # Update last_update to prevent immediate re-processing
                            processed_keys.append(batch_key) # Mark for potential deletion if empty later

                            # Actual publishing happens outside the lock to avoid holding it during I/O
                            if len(events_to_publish) > 1:
                                await self.publish_event({
                                    'type': 'batch',
                                    'timestamp': datetime.utcnow().isoformat(),
                                    'events': events_to_publish
                                }, batch.exchange_name, batch.routing_key)
                                logger.info(f"Published batch of {len(events_to_publish)} events for {batch_key}")
                            elif len(events_to_publish) == 1:
                                await self.publish_event(
                                    events_to_publish[0],
                                    batch.exchange_name,
                                    batch.routing_key
                                )
                                logger.info(f"Published single event for {batch_key}")
                
                # Clean up empty batches under lock
                async with self.batch_lock:
                    for key in processed_keys:
                        if key in self.event_batches and not self.event_batches[key].events:
                            del self.event_batches[key]
                            logger.debug(f"Cleaned up empty batch for key {key}")
                                
            except Exception as e:
                logger.error(f"Error processing batches: {e}", exc_info=True)
            
            await asyncio.sleep(batch_processing_interval)


    async def handle_webhook(self, request: web.Request) -> web.Response:
        """
        Handles incoming webhook POST requests, validates and enriches event data, and enqueues events for batching and publishing.
        
        Accepts JSON payloads containing an `event_type` field, adds metadata, and returns HTTP 202 on success. Responds with HTTP 400 for validation errors and HTTP 500 for unexpected errors.
        """
        try:
            event = await request.json()
            if not isinstance(event, dict) or 'event_type' not in event:
                logger.warning(f"Invalid event format received: {event}")
                raise ValueError("Invalid event format")

            event['received_at'] = datetime.utcnow().isoformat()
            event['source'] = 'home_assistant_webhook' # More specific source
            await self.add_to_batch(event)
            return web.Response(status=202, text="Accepted")
        except ValueError as ve: # Catch specific validation error
            logger.error(f"Validation error handling webhook: {ve}")
            return web.Response(status=400, text=str(ve))
        except Exception as e:
            logger.error(f"Error handling webhook: {e}", exc_info=True)
            return web.Response(status=500, text="Internal Server Error")

    async def start_server(self) -> None:
        """
        Starts the event publisher service, including RabbitMQ connection, batch processing, and the webhook HTTP server.
        
        Initializes the RabbitMQ connection, launches the batch processing task, and starts the aiohttp web server to receive incoming events. Runs until a shutdown signal is received.
        """
        if not await self.connect_rabbitmq():
            logger.error("Failed to connect to RabbitMQ during startup. Exiting.")
            self.should_exit = True
            return

        self.batch_processor_task = asyncio.create_task(self.process_batches())

        app = web.Application()
        app.router.add_post(self.config['webhook']['path'], self.handle_webhook)
        
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        self.site = web.TCPSite(
            self.runner,
            self.config['webhook']['host'],
            self.config['webhook']['port']
        )
        await self.site.start()
        logger.info(f"Webhook server started on http://{self.config['webhook']['host']}:{self.config['webhook']['port']}{self.config['webhook']['path']}")

        while not self.should_exit:
            await asyncio.sleep(1)

    async def stop_server(self) -> None:
        """
        Stops the event publisher service and performs cleanup.
        
        Cancels the batch processing task, stops the webhook server, cleans up resources, and closes the RabbitMQ connection if open.
        """
        logger.info("Stopping event publisher service...")
        if hasattr(self, 'batch_processor_task') and self.batch_processor_task:
            self.batch_processor_task.cancel()
            try:
                await self.batch_processor_task
            except asyncio.CancelledError:
                logger.info("Batch processor task cancelled.")
        
        if hasattr(self, 'site') and self.site:
            await self.site.stop()
            logger.info("Webhook site stopped.")
        if hasattr(self, 'runner') and self.runner:
            await self.runner.cleanup()
            logger.info("Webhook runner cleaned up.")
        
        if self.connection and not self.connection.is_closed:
            await self.connection.close()
            logger.info("RabbitMQ connection closed.")
        logger.info("Event publisher service stopped.")


async def main_async(): # Renamed to avoid conflict
    """
    Asynchronous entry point for starting the event publisher service.
    
    Initializes the event publisher with configuration, sets up signal handlers for graceful shutdown, starts the server, and ensures proper cleanup on exit or unhandled exceptions.
    """
    config_path = os.environ.get('CONFIG_PATH', 'config.yaml')
    publisher = HAEventPublisher(config_path)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda s=sig: asyncio.create_task(publisher.signal_handler_async(s)))
    
    try:
        await publisher.start_server()
    except Exception as e:
        logger.error(f"Unhandled error in publisher: {e}", exc_info=True)
    finally:
        if not publisher.should_exit: # Ensure stop is called if not initiated by signal
            publisher.should_exit = True 
        await publisher.stop_server()

if __name__ == "__main__":
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        logger.info("Application interrupted by user.")
    except Exception as e:
        logger.critical(f"Application terminated with unhandled exception: {e}", exc_info=True)
    finally:
        logger.info("Application shutdown finalized.")

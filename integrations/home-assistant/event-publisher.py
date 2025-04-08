#!/usr/bin/env python3

import os
import sys
import json
import time
import signal
import asyncio
import logging
import aiohttp
import pika
import yaml
from typing import Dict, List, Optional, Any
from dataclasses import dataclass
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
    exchange: str
    routing_key: str

class HAEventPublisher:
    def __init__(self, config_path: str = "config.yaml"):
        self.config = self.load_config(config_path)
        self.connection = None
        self.channel = None
        self.should_exit = False
        self.event_batches = defaultdict(lambda: EventBatch([], time.time(), "", ""))
        self.batch_lock = asyncio.Lock()

        # Setup signal handlers
        signal.signal(signal.SIGINT, self.signal_handler)
        signal.signal(signal.SIGTERM, self.signal_handler)

    def load_config(self, config_path: str) -> Dict:
        """Load configuration from YAML file."""
        try:
            with open(config_path, 'r') as f:
                config = yaml.safe_load(f)

            # Validate required configuration
            required_keys = ['rabbitmq', 'home_assistant', 'webhook']
            for key in required_keys:
                if key not in config:
                    raise ValueError(f"Missing required configuration section: {key}")

            return config
        except Exception as e:
            logger.error(f"Failed to load configuration: {e}")
            sys.exit(1)

    def signal_handler(self, signum: int, frame: Any) -> None:
        """Handle shutdown signals."""
        logger.info(f"Received signal {signum}, initiating shutdown...")
        self.should_exit = True

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

                # Declare exchanges
                for exchange in self.config['rabbitmq']['exchanges']:
                    self.channel.exchange_declare(
                        exchange=exchange['name'],
                        exchange_type=exchange['type'],
                        durable=True
                    )

                logger.info("Successfully connected to RabbitMQ")
                return
            except Exception as e:
                logger.error(f"Failed to connect to RabbitMQ: {e}")
                await asyncio.sleep(5)

    async def publish_event(self, event: Dict, exchange: str, routing_key: str) -> None:
        """Publish event to RabbitMQ."""
        try:
            if not self.connection or self.connection.is_closed:
                await self.connect_rabbitmq()

            self.channel.basic_publish(
                exchange=exchange,
                routing_key=routing_key,
                body=json.dumps(event),
                properties=pika.BasicProperties(
                    delivery_mode=2,  # Make message persistent
                    timestamp=int(time.time()),
                    content_type='application/json'
                )
            )
        except Exception as e:
            logger.error(f"Failed to publish event: {e}")
            # Close connection to force reconnect
            if self.connection:
                try:
                    self.connection.close()
                except:
                    pass
            self.connection = None
            self.channel = None

    def get_routing_info(self, event: Dict) -> Tuple[str, str]:
        """Determine exchange and routing key based on event type."""
        event_type = event.get('event_type', '')
        domain = event_type.split('.')[0] if '.' in event_type else 'default'

        # Match event type to routing configuration
        for route in self.config['rabbitmq']['routing']:
            if (route['domain'] == domain or route['domain'] == '*') and \
               (route['event_type'] == event_type or route['event_type'] == '*'):
                return route['exchange'], route['routing_key'].format(
                    domain=domain,
                    event_type=event_type
                )

        # Default routing if no match found
        return (
            self.config['rabbitmq']['default_exchange'],
            f"home.events.{domain}.{event_type}"
        )

    async def add_to_batch(self, event: Dict) -> None:
        """Add event to appropriate batch."""
        exchange, routing_key = self.get_routing_info(event)
        batch_key = f"{exchange}:{routing_key}"

        async with self.batch_lock:
            if batch_key not in self.event_batches:
                self.event_batches[batch_key] = EventBatch([], time.time(), exchange, routing_key)

            batch = self.event_batches[batch_key]
            batch.events.append(event)
            batch.last_update = time.time()

    async def process_batches(self) -> None:
        """Process event batches periodically."""
        while not self.should_exit:
            try:
                current_time = time.time()
                batch_size = self.config['rabbitmq'].get('batch_size', 100)
                batch_timeout = self.config['rabbitmq'].get('batch_timeout', 5)

                async with self.batch_lock:
                    for batch_key, batch in list(self.event_batches.items()):
                        # Process batch if it's full or timed out
                        if len(batch.events) >= batch_size or \
                           (len(batch.events) > 0 and current_time - batch.last_update >= batch_timeout):

                            if len(batch.events) > 1:
                                # Publish as batch
                                await self.publish_event({
                                    'type': 'batch',
                                    'timestamp': datetime.utcnow().isoformat(),
                                    'events': batch.events
                                }, batch.exchange, batch.routing_key)
                            elif len(batch.events) == 1:
                                # Publish single event
                                await self.publish_event(
                                    batch.events[0],
                                    batch.exchange,
                                    batch.routing_key
                                )

                            # Clear processed events
                            del self.event_batches[batch_key]

            except Exception as e:
                logger.error(f"Error processing batches: {e}")

            await asyncio.sleep(1)

    async def handle_webhook(self, request: web.Request) -> web.Response:
        """Handle incoming webhooks from Home Assistant."""
        try:
            event = await request.json()

            # Validate event format
            if not isinstance(event, dict) or 'event_type' not in event:
                raise ValueError("Invalid event format")

            # Add metadata
            event['received_at'] = datetime.utcnow().isoformat()
            event['source'] = 'home_assistant'

            # Add to batch for processing
            await self.add_to_batch(event)

            return web.Response(status=202)
        except Exception as e:
            logger.error(f"Error handling webhook: {e}")
            return web.Response(status=400, text=str(e))

    async def start(self) -> None:
        """Start the event publisher service."""
        # Initialize RabbitMQ connection
        await self.connect_rabbitmq()

        # Start batch processing
        batch_processor = asyncio.create_task(self.process_batches())

        # Setup webhook server
        app = web.Application()
        app.router.add_post(self.config['webhook']['path'], self.handle_webhook)

        # Start webhook server
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(
            runner,
            self.config['webhook']['host'],
            self.config['webhook']['port']
        )
        await site.start()

        logger.info(f"Webhook server started on {self.config['webhook']['host']}:{self.config['webhook']['port']}")

        try:
            while not self.should_exit:
                await asyncio.sleep(1)
        finally:
            # Cleanup
            batch_processor.cancel()
            await runner.cleanup()
            if self.connection:
                self.connection.close()

def main():
    """Main entry point."""
    # Load config path from environment or use default
    config_path = os.environ.get('CONFIG_PATH', 'config.yaml')

    # Create publisher instance
    publisher = HAEventPublisher(config_path)

    # Run the service
    asyncio.run(publisher.start())

if __name__ == "__main__":
    main()

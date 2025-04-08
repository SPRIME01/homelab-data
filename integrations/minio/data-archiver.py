#!/usr/bin/env python3

"""
Data Archiver for RabbitMQ to MinIO

Run the service `python data-archiver.py --config config.yaml`"""

import os
import sys
import json
import time
import yaml
import logging
import asyncio
from typing import Dict, List, Any, Optional
from datetime import datetime, timedelta
import aiohttp
import aio_pika
import zstandard
from minio import Minio
from minio.commonconfig import ENABLED, Filter
from minio.lifecycleconfig import LifecycleConfig, Rule, Expiration, Transition
from prometheus_client import start_http_server, Counter, Gauge, Summary

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('data_archiver.log')
    ]
)
logger = logging.getLogger("data_archiver")

# Prometheus metrics
MESSAGES_ARCHIVED = Counter('archiver_messages_archived_total',
                          'Number of messages archived', ['queue', 'bucket'])
BATCH_SIZE = Gauge('archiver_batch_size', 'Current batch size', ['queue'])
ARCHIVE_TIME = Summary('archiver_archive_duration_seconds',
                      'Time spent archiving batch')
STORAGE_USAGE = Gauge('archiver_storage_bytes',
                     'Storage usage in bytes', ['bucket'])

class MessageBatch:
    """Represents a batch of messages to be archived."""

    def __init__(self, max_size: int = 1000, max_age: float = 300):
        self.messages: List[Dict] = []
        self.first_timestamp: Optional[float] = None
        self.max_size = max_size
        self.max_age = max_age

    def add(self, message: Dict) -> bool:
        """Add a message to the batch. Return True if batch is full."""
        if not self.first_timestamp:
            self.first_timestamp = time.time()

        self.messages.append(message)
        return self.is_full()

    def is_full(self) -> bool:
        """Check if batch should be archived."""
        if len(self.messages) >= self.max_size:
            return True

        if self.first_timestamp and \
           time.time() - self.first_timestamp >= self.max_age:
            return True

        return False

    def clear(self) -> None:
        """Clear the batch."""
        self.messages.clear()
        self.first_timestamp = None

class DataArchiver:
    """Archives messages from RabbitMQ to MinIO."""

    def __init__(self, config_path: str):
        """Initialize the archiver with configuration."""
        self.config = self._load_config(config_path)
        self.connection = None
        self.channel = None
        self.minio_client = None
        self.should_exit = False
        self.batches: Dict[str, MessageBatch] = {}

    def _load_config(self, config_path: str) -> Dict:
        """Load configuration from YAML file."""
        try:
            with open(config_path, 'r') as f:
                config = yaml.safe_load(f)

            # Validate required configuration
            required_sections = ['rabbitmq', 'minio', 'archival']
            missing = [s for s in required_sections if s not in config]
            if missing:
                raise ValueError(f"Missing required config sections: {missing}")

            return config

        except Exception as e:
            logger.error(f"Failed to load configuration: {e}")
            sys.exit(1)

    async def setup(self):
        """Set up connections and initialize storage."""
        # Connect to RabbitMQ
        try:
            self.connection = await aio_pika.connect_robust(
                host=self.config['rabbitmq']['host'],
                port=self.config['rabbitmq']['port'],
                login=self.config['rabbitmq']['username'],
                password=self.config['rabbitmq']['password'],
                virtualhost=self.config['rabbitmq'].get('vhost', '/')
            )
            self.channel = await self.connection.channel()
            logger.info("Connected to RabbitMQ")

        except Exception as e:
            logger.error(f"Failed to connect to RabbitMQ: {e}")
            raise

        # Initialize MinIO client
        try:
            self.minio_client = Minio(
                self.config['minio']['endpoint'],
                access_key=self.config['minio']['access_key'],
                secret_key=self.config['minio']['secret_key'],
                secure=self.config['minio'].get('secure', True)
            )

            # Ensure buckets exist with proper configuration
            await self._setup_buckets()
            logger.info("Connected to MinIO")

        except Exception as e:
            logger.error(f"Failed to connect to MinIO: {e}")
            raise

    async def _setup_buckets(self):
        """Set up MinIO buckets with lifecycle policies."""
        for archive_config in self.config['archival']['archives']:
            bucket = archive_config['bucket']

            # Create bucket if it doesn't exist
            if not self.minio_client.bucket_exists(bucket):
                self.minio_client.make_bucket(bucket)
                logger.info(f"Created bucket: {bucket}")

            # Configure lifecycle rules
            lifecycle_rules = []

            if 'retention' in archive_config:
                # Add expiration rule
                lifecycle_rules.append(
                    Rule(
                        ENABLED,
                        rule_filter=Filter(prefix=archive_config.get('prefix', '')),
                        rule_id="expiration",
                        expiration=Expiration(
                            days=archive_config['retention'].get('days', 365)
                        )
                    )
                )

            if 'tiering' in archive_config:
                # Add transition rules for tiering
                for tier in archive_config['tiering']:
                    lifecycle_rules.append(
                        Rule(
                            ENABLED,
                            rule_filter=Filter(prefix=archive_config.get('prefix', '')),
                            rule_id=f"transition_{tier['name']}",
                            transition=Transition(
                                days=tier['days'],
                                storage_class=tier['storage_class']
                            )
                        )
                    )

            if lifecycle_rules:
                config = LifecycleConfig(lifecycle_rules)
                self.minio_client.set_bucket_lifecycle(bucket, config)
                logger.info(f"Set lifecycle rules for bucket: {bucket}")

    def _get_object_path(self, queue_name: str, timestamp: datetime) -> str:
        """Generate object path based on partitioning strategy."""
        # Use hierarchical partitioning: year/month/day/hour
        return f"{queue_name}/{timestamp.year}/{timestamp.month:02d}/{timestamp.day:02d}/{timestamp.hour:02d}/" + \
               f"messages_{timestamp.timestamp():.0f}.json.zst"

    async def _archive_batch(self, queue_name: str, batch: MessageBatch):
        """Archive a batch of messages to MinIO."""
        if not batch.messages:
            return

        try:
            archive_config = next(
                (a for a in self.config['archival']['archives']
                 if queue_name in a['queues']),
                None
            )

            if not archive_config:
                logger.warning(f"No archive configuration for queue: {queue_name}")
                return

            # Prepare metadata
            timestamp = datetime.now()
            metadata = {
                "queue": queue_name,
                "message_count": len(batch.messages),
                "first_message_time": batch.first_timestamp,
                "archive_time": timestamp.isoformat(),
                "compression": "zstd"
            }

            # Compress messages with zstandard
            compressor = zstandard.ZstdCompressor(level=self.config['archival'].get('compression_level', 3))
            json_data = json.dumps(batch.messages).encode('utf-8')
            compressed_data = compressor.compress(json_data)

            # Generate object path
            object_name = self._get_object_path(queue_name, timestamp)

            # Upload to MinIO
            self.minio_client.put_object(
                bucket_name=archive_config['bucket'],
                object_name=object_name,
                data=compressed_data,
                length=len(compressed_data),
                metadata=metadata
            )

            # Update metrics
            MESSAGES_ARCHIVED.labels(queue=queue_name,
                                  bucket=archive_config['bucket']).inc(len(batch.messages))
            STORAGE_USAGE.labels(bucket=archive_config['bucket']).inc(len(compressed_data))

            logger.info(f"Archived {len(batch.messages)} messages from {queue_name} to {object_name}")
            batch.clear()

        except Exception as e:
            logger.error(f"Failed to archive batch from {queue_name}: {e}")
            # Don't clear batch on error to retry later

    async def _process_message(self, message: aio_pika.IncomingMessage):
        """Process a single message."""
        async with message.process():
            queue_name = message.routing_key

            try:
                # Parse message body
                body = json.loads(message.body.decode())

                # Get or create batch for this queue
                if queue_name not in self.batches:
                    archive_config = next(
                        (a for a in self.config['archival']['archives']
                         if queue_name in a['queues']),
                        None
                    )
                    if not archive_config:
                        logger.warning(f"No archive configuration for queue: {queue_name}")
                        return

                    self.batches[queue_name] = MessageBatch(
                        max_size=archive_config.get('batch_size', 1000),
                        max_age=archive_config.get('batch_age', 300)
                    )

                batch = self.batches[queue_name]

                # Add message to batch
                if batch.add(body):
                    # Batch is full, archive it
                    await self._archive_batch(queue_name, batch)

                # Update batch size metric
                BATCH_SIZE.labels(queue=queue_name).set(len(batch.messages))

            except json.JSONDecodeError:
                logger.error(f"Failed to parse message from {queue_name}")
            except Exception as e:
                logger.error(f"Error processing message from {queue_name}: {e}")

    async def run(self):
        """Run the archiver."""
        try:
            # Set up connections
            await self.setup()

            # Start metrics server if enabled
            metrics_port = self.config.get('metrics', {}).get('port', 8000)
            if metrics_port:
                start_http_server(metrics_port)
                logger.info(f"Started metrics server on port {metrics_port}")

            # Create queue bindings
            for archive_config in self.config['archival']['archives']:
                for queue_name in archive_config['queues']:
                    queue = await self.channel.declare_queue(
                        queue_name,
                        durable=True
                    )

                    # Start consuming
                    await queue.consume(self._process_message)
                    logger.info(f"Started consuming from queue: {queue_name}")

            # Run periodic batch check
            check_interval = self.config['archival'].get('check_interval', 60)
            while not self.should_exit:
                # Archive any full or aged batches
                for queue_name, batch in self.batches.items():
                    if batch.is_full():
                        await self._archive_batch(queue_name, batch)

                await asyncio.sleep(check_interval)

        except Exception as e:
            logger.error(f"Fatal error: {e}")
            self.should_exit = True

        finally:
            # Clean up
            if self.connection:
                await self.connection.close()

def main():
    """Main entry point."""
    import argparse

    parser = argparse.ArgumentParser(description='RabbitMQ Data Archiver')
    parser.add_argument('--config', '-c', type=str, default='config.yaml',
                      help='Path to configuration file')
    args = parser.parse_args()

    archiver = DataArchiver(args.config)

    try:
        asyncio.run(archiver.run())
    except KeyboardInterrupt:
        logger.info("Shutting down...")
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()

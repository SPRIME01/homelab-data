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
import io # Added for BytesIO
# import aiohttp # aiohttp is not used directly here, can be removed if not needed elsewhere
import aio_pika
import zstandard
from minio import Minio
from minio.commonconfig import ENABLED, Filter # type: ignore
from minio.lifecycleconfig import LifecycleConfig, Rule, Expiration, Transition # type: ignore
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
        """
        Initializes the DataArchiver with the provided configuration file.
        
        Loads configuration settings and prepares internal state for connections, batching, and event loop management.
        """
        self.config = self._load_config(config_path)
        self.connection: Optional[aio_pika.RobustConnection] = None
        self.channel: Optional[aio_pika.Channel] = None
        self.minio_client: Optional[Minio] = None
        self.should_exit = False
        self.batches: Dict[str, MessageBatch] = {}
        self.loop: Optional[asyncio.AbstractEventLoop] = None


    def _load_config(self, config_path: str) -> Dict:
        """
        Loads and validates configuration from a YAML file.
        
        Reads the specified YAML file, parses its contents, and ensures that the required sections ('rabbitmq', 'minio', and 'archival') are present. Exits the application if loading or validation fails.
        
        Args:
            config_path: Path to the YAML configuration file.
        
        Returns:
            A dictionary containing the loaded configuration.
        """
        try:
            with open(config_path, 'r') as f:
                config = yaml.safe_load(f)

            required_sections = ['rabbitmq', 'minio', 'archival']
            missing = [s for s in required_sections if s not in config]
            if missing:
                raise ValueError(f"Missing required config sections: {missing}")

            return config

        except Exception as e:
            logger.error(f"Failed to load configuration: {e}")
            sys.exit(1)

    async def setup(self):
        """
        Asynchronously establishes connections to RabbitMQ and MinIO, and ensures required storage buckets are configured.
        
        Raises:
            Exception: If connection to RabbitMQ or MinIO fails, or if bucket setup encounters an error.
        """
        self.loop = asyncio.get_running_loop()
        # Connect to RabbitMQ
        try:
            rabbitmq_config = self.config['rabbitmq']
            self.connection = await aio_pika.connect_robust(
                host=rabbitmq_config['host'],
                port=rabbitmq_config['port'],
                login=rabbitmq_config['username'],
                password=rabbitmq_config['password'],
                virtualhost=rabbitmq_config.get('vhost', '/'),
                loop=self.loop
            )
            self.channel = await self.connection.channel()
            logger.info("Connected to RabbitMQ")

        except Exception as e:
            logger.error(f"Failed to connect to RabbitMQ: {e}")
            raise

        # Initialize MinIO client
        try:
            minio_config = self.config['minio']
            self.minio_client = Minio(
                minio_config['endpoint'],
                access_key=minio_config['access_key'],
                secret_key=minio_config['secret_key'],
                secure=minio_config.get('secure', True)
            )

            await self._setup_buckets()
            logger.info("Connected to MinIO and buckets configured")

        except Exception as e:
            logger.error(f"Failed to connect to MinIO: {e}")
            raise

    async def _setup_buckets(self):
        """
        Ensures MinIO buckets exist and applies lifecycle policies for retention and tiering.
        
        For each archive configuration, creates the bucket if it does not exist and sets up
        lifecycle rules for data expiration and storage class transitions based on the provided
        retention and tiering settings.
        """
        if not self.minio_client:
            raise ConnectionError("MinIO client not initialized")

        for archive_config in self.config['archival']['archives']:
            bucket = archive_config['bucket']
            
            bucket_exists = await self.loop.run_in_executor(None, self.minio_client.bucket_exists, bucket)
            if not bucket_exists:
                await self.loop.run_in_executor(None, self.minio_client.make_bucket, bucket)
                logger.info(f"Created bucket: {bucket}")

            lifecycle_rules = []
            if 'retention' in archive_config:
                lifecycle_rules.append(
                    Rule(
                        status=ENABLED, # Corrected: status=ENABLED instead of ENABLED directly
                        rule_filter=Filter(prefix=archive_config.get('prefix', '')),
                        rule_id="expiration",
                        expiration=Expiration(days=archive_config['retention'].get('days', 365))
                    )
                )

            if 'tiering' in archive_config:
                for tier in archive_config['tiering']:
                    lifecycle_rules.append(
                        Rule(
                            status=ENABLED, # Corrected
                            rule_filter=Filter(prefix=archive_config.get('prefix', '')),
                            rule_id=f"transition_{tier['name']}",
                            transition=Transition(days=tier['days'], storage_class=tier['storage_class'])
                        )
                    )
            
            if lifecycle_rules:
                config = LifecycleConfig(rules=lifecycle_rules) # Corrected: rules=lifecycle_rules
                await self.loop.run_in_executor(None, self.minio_client.set_bucket_lifecycle, bucket, config)
                logger.info(f"Set lifecycle rules for bucket: {bucket}")


    def _get_object_path(self, queue_name: str) -> str: # Removed timestamp argument
        """
        Generates an object storage path for a queue using the current UTC time for partitioning.
        
        The path format is: `{queue_name}/YYYY/MM/DD/HH/messages_<timestamp>.json.zst`, where `<timestamp>` is the current UTC epoch time in seconds.
        """
        # Use UTC for partitioning to avoid timezone issues
        timestamp_utc = datetime.utcnow() # Use current UTC time
        return f"{queue_name}/{timestamp_utc.year}/{timestamp_utc.month:02d}/{timestamp_utc.day:02d}/{timestamp_utc.hour:02d}/" + \
               f"messages_{int(timestamp_utc.timestamp())}.json.zst" # Use int for timestamp

    async def _archive_batch(self, queue_name: str, batch: MessageBatch):
        """
        Archives a batch of messages from a specified queue to MinIO object storage.
        
        The batch is serialized as JSON, compressed with Zstandard, and uploaded to the configured MinIO bucket. Uploads are retried on failure according to configuration. Prometheus metrics for archived messages and storage usage are updated upon success. The batch is cleared after successful archival.
        """
        if not batch.messages or not self.minio_client or not self.loop:
            return

        try:
            archive_config = next(
                (a for a in self.config['archival']['archives']
                 if queue_name in a['queues']), None
            )
            if not archive_config:
                logger.warning(f"No archive configuration for queue: {queue_name}")
                return

            timestamp = datetime.utcnow() # Changed to utcnow for consistency
            metadata = {
                "queue": queue_name,
                "message_count": str(len(batch.messages)), # Metadata values are often strings
                "first_message_time": str(batch.first_timestamp) if batch.first_timestamp else "",
                "archive_time": timestamp.isoformat(),
                "compression": "zstd"
            }

            compressor = zstandard.ZstdCompressor(level=self.config['archival'].get('compression_level', 3))
            json_data = json.dumps(batch.messages).encode('utf-8')
            compressed_data = compressor.compress(json_data)
            compressed_data_stream = io.BytesIO(compressed_data) # Use BytesIO for stream
            compressed_data_len = len(compressed_data)

            object_name = self._get_object_path(queue_name) # Call without timestamp

            retries = 0
            max_retries = self.config.get('error_handling', {}).get('max_retries', 3)
            retry_delay = self.config.get('error_handling', {}).get('retry_delay', 5)

            while retries < max_retries:
                try:
                    await self.loop.run_in_executor(
                        None,
                        self.minio_client.put_object,
                        archive_config['bucket'],
                        object_name,
                        compressed_data_stream,
                        compressed_data_len,
                        metadata=metadata,
                        content_type='application/octet-stream' # More appropriate for compressed data
                    )
                    compressed_data_stream.seek(0) # Reset stream position if retrying, though new stream is made if error
                    break 
                except Exception as e:
                    retries += 1
                    logger.error(f"Failed to upload to MinIO (attempt {retries}/{max_retries}): {e}")
                    compressed_data_stream.seek(0) # Reset stream for retry
                    if retries < max_retries:
                        await asyncio.sleep(retry_delay)
                    else:
                        raise
            
            MESSAGES_ARCHIVED.labels(queue=queue_name, bucket=archive_config['bucket']).inc(len(batch.messages))
            STORAGE_USAGE.labels(bucket=archive_config['bucket']).inc(compressed_data_len)
            logger.info(f"Archived {len(batch.messages)} messages from {queue_name} to {object_name}")
            batch.clear()

        except Exception as e:
            logger.error(f"Failed to archive batch from {queue_name}: {e}", exc_info=True)

    async def _process_message(self, message: aio_pika.IncomingMessage):
        """
        Processes a single RabbitMQ message, batching it for archival or handling errors.
        
        If the message's queue is configured for archival, adds the message to the appropriate batch and archives the batch if full. Acknowledges messages on success, rejects malformed JSON messages without requeue, and nacks other errors without requeue.
        """
        async with message.process(ignore_processed=True): # Auto ack/nack based on context
            queue_name = message.routing_key or message.exchange # Fallback if routing_key is None

            try:
                body = json.loads(message.body.decode())
                if queue_name not in self.batches:
                    archive_config = next(
                        (a for a in self.config['archival']['archives'] if queue_name in a['queues']), None
                    )
                    if not archive_config:
                        logger.warning(f"No archive configuration for queue/routing_key: {queue_name}. Message will be dropped after ack.")
                        await message.ack() # Acknowledge to remove from queue
                        return

                    self.batches[queue_name] = MessageBatch(
                        max_size=archive_config.get('batch_size', 1000),
                        max_age=archive_config.get('batch_age', 300)
                    )
                
                batch = self.batches[queue_name]
                if batch.add(body):
                    await self._archive_batch(queue_name, batch)
                
                BATCH_SIZE.labels(queue=queue_name).set(len(batch.messages))
                await message.ack()

            except json.JSONDecodeError:
                logger.error(f"Failed to parse message from {queue_name}")
                await message.reject(requeue=False) # Reject non-JSON messages
            except Exception as e:
                logger.error(f"Error processing message from {queue_name}: {e}", exc_info=True)
                await message.nack(requeue=False) # Nack without requeue on other errors

    async def run(self):
        """
        Starts the main asynchronous loop for the data archiver service.
        
        Initializes connections, sets up Prometheus metrics, begins consuming messages from configured RabbitMQ queues, and periodically checks and archives message batches. Handles graceful shutdown and resource cleanup on exit or fatal errors.
        """
        self.loop = asyncio.get_running_loop()
        try:
            await self.setup()
            metrics_port = self.config.get('metrics', {}).get('port', 8000)
            if metrics_port:
                start_http_server(metrics_port)
                logger.info(f"Started metrics server on port {metrics_port}")

            if not self.channel:
                 logger.error("RabbitMQ channel not available. Exiting.")
                 return

            for archive_config in self.config['archival']['archives']:
                for queue_name in archive_config['queues']:
                    queue = await self.channel.declare_queue(queue_name, durable=True)
                    await queue.consume(self._process_message)
                    logger.info(f"Started consuming from queue: {queue_name}")
            
            check_interval = self.config['archival'].get('check_interval', 60)
            while not self.should_exit:
                for queue_name, batch in list(self.batches.items()): # Iterate copy for safe modification
                    if batch.is_full():
                        await self._archive_batch(queue_name, batch)
                await asyncio.sleep(check_interval)

        except Exception as e:
            logger.error(f"Fatal error in run loop: {e}", exc_info=True)
            self.should_exit = True
        finally:
            logger.info("Shutting down archiver...")
            if self.connection and not self.connection.is_closed:
                await self.connection.close()
                logger.info("RabbitMQ connection closed.")
            logger.info("Archiver shutdown complete.")

def main():
    """
    Parses command-line arguments, initializes the DataArchiver, and runs the archiving service.
    
    Handles graceful shutdown on keyboard interruption and logs critical errors before exiting.
    """
    import argparse
    parser = argparse.ArgumentParser(description='RabbitMQ Data Archiver')
    parser.add_argument('--config', '-c', type=str, default='config.yaml', help='Path to configuration file')
    args = parser.parse_args()

    archiver = DataArchiver(args.config)
    
    try:
        asyncio.run(archiver.run())
    except KeyboardInterrupt:
        logger.info("Archiver interrupted by user.")
        archiver.should_exit = True 
        # Allow run() to enter finally block or explicitly call a cleanup method if run() already exited
        if archiver.loop and not archiver.loop.is_closed():
             archiver.loop.run_until_complete(archiver.loop.create_task(asyncio.sleep(1))) # Give a moment for cleanup
    except Exception as e:
        logger.critical(f"Archiver terminated with unhandled exception: {e}", exc_info=True)
        sys.exit(1)
    finally:
        logger.info("Archiver application shutdown finalized.")

if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Run the service:
python influxdb-consumer.py --config config.yaml
"""

import os
import sys
import json
import yaml
import time
import logging
import asyncio
from typing import Dict, List, Any, Optional, Tuple
from datetime import datetime, timedelta
from dataclasses import dataclass, field
import aiormq
from influxdb_client import InfluxDBClient, Point, WriteOptions
from influxdb_client.client.write_api import SYNCHRONOUS, ASYNCHRONOUS
from prometheus_client import start_http_server, Counter, Gauge, Summary

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('influxdb_consumer.log')
    ]
)
logger = logging.getLogger("influxdb_consumer")

# Prometheus metrics
MESSAGES_PROCESSED = Counter('influxdb_consumer_messages_processed_total',
                           'Number of messages processed', ['queue', 'status'])
BATCH_SIZE = Gauge('influxdb_consumer_batch_size',
                  'Current batch size', ['measurement'])
WRITE_TIME = Summary('influxdb_consumer_write_duration_seconds',
                    'Time spent writing to InfluxDB')
QUEUE_LAG = Gauge('influxdb_consumer_queue_lag_seconds',
                  'Message processing lag', ['queue'])

@dataclass
class BatchConfig:
    """Configuration for batch writing."""
    size: int = 1000
    interval: float = 10.0  # seconds
    jitter: float = 0.1
    points: List[Point] = field(default_factory=list)
    last_write: float = field(default_factory=time.time)

class InfluxDBConsumer:
    """Consumes messages from RabbitMQ and writes to InfluxDB."""

    def __init__(self, config_path: str):
        """Initialize the consumer with configuration."""
        self.config = self._load_config(config_path)
        self.connection = None
        self.channel = None
        self.influxdb_client = None
        self.write_api = None
        self.should_exit = False
        self.batches: Dict[str, BatchConfig] = {}

    def _load_config(self, config_path: str) -> Dict:
        """Load configuration from YAML file."""
        try:
            with open(config_path, 'r') as f:
                config = yaml.safe_load(f)

            # Validate required configuration
            required_sections = ['rabbitmq', 'influxdb', 'mappings']
            missing = [s for s in required_sections if s not in config]
            if missing:
                raise ValueError(f"Missing required configuration sections: {missing}")

            return config
        except Exception as e:
            logger.error(f"Failed to load configuration: {e}")
            sys.exit(1)

    async def setup(self):
        """Set up connections and initialize InfluxDB."""
        # Connect to InfluxDB
        try:
            self.influxdb_client = InfluxDBClient(
                url=self.config['influxdb']['url'],
                token=self.config['influxdb']['token'],
                org=self.config['influxdb']['org']
            )

            # Configure write API with batching
            self.write_api = self.influxdb_client.write_api(
                write_options=WriteOptions(
                    batch_size=self.config['influxdb'].get('batch_size', 1000),
                    flush_interval=self.config['influxdb'].get('flush_interval', 10_000),
                    jitter_interval=self.config['influxdb'].get('jitter_interval', 1_000),
                    retry_interval=self.config['influxdb'].get('retry_interval', 5_000),
                    max_retries=self.config['influxdb'].get('max_retries', 3),
                    max_retry_delay=self.config['influxdb'].get('max_retry_delay', 30_000),
                    exponential_base=self.config['influxdb'].get('exponential_base', 2)
                )
            )

            # Set up retention policies
            await self._setup_retention_policies()

            # Set up continuous queries
            await self._setup_continuous_queries()

        except Exception as e:
            logger.error(f"Failed to connect to InfluxDB: {e}")
            raise

        # Connect to RabbitMQ
        try:
            rabbitmq_config = self.config['rabbitmq']
            self.connection = await aiormq.connect(
                f"amqp://{rabbitmq_config['username']}:{rabbitmq_config['password']}@"
                f"{rabbitmq_config['host']}:{rabbitmq_config['port']}/{rabbitmq_config.get('vhost', '/')}"
            )
            self.channel = await self.connection.channel()

            # Set QoS
            await self.channel.basic_qos(
                prefetch_count=rabbitmq_config.get('prefetch_count', 100)
            )

            logger.info("Successfully connected to RabbitMQ and InfluxDB")
        except Exception as e:
            logger.error(f"Failed to connect to RabbitMQ: {e}")
            raise

    async def _setup_retention_policies(self):
        """Set up retention policies for different data types."""
        retention_policies = self.config['influxdb'].get('retention_policies', [])

        for rp in retention_policies:
            try:
                if 'name' not in rp or 'database' not in rp or 'duration' not in rp:
                    raise ValueError(f"Invalid retention policy configuration: {rp}")

                query = f"""CREATE RETENTION POLICY "{rp['name']}"
                           ON "{rp['database']}"
                           DURATION {rp['duration']}
                           REPLICATION {rp.get('replication', 1)}
                           {'DEFAULT' if rp.get('default', False) else ''}"""

                await self.influxdb_client.query_api().query_raw(query)
                logger.info(f"Created retention policy: {rp['name']}")
            except Exception as e:
                logger.warning(f"Failed to create retention policy {rp['name']}: {e}")

    async def _setup_continuous_queries(self):
        """Set up continuous queries for data aggregation."""
        continuous_queries = self.config['influxdb'].get('continuous_queries', [])

        for cq in continuous_queries:
            try:
                query = f"""CREATE CONTINUOUS QUERY "{cq['name']}"
                           ON "{cq['database']}"
                           {f'RESAMPLE EVERY {cq["resample_every"]}' if 'resample_every' in cq else ''}
                           BEGIN
                               {cq['query']}
                           END"""

                await self.influxdb_client.query_api().query_raw(query)
                logger.info(f"Created continuous query: {cq['name']}")
            except Exception as e:
                logger.warning(f"Failed to create continuous query {cq['name']}: {e}")

    def _transform_to_point(self, message: Dict, mapping: Dict) -> Optional[Point]:
        """Transform a message to InfluxDB point based on mapping configuration."""
        try:
            # Create point with measurement
            point = Point(mapping['measurement'])

            # Add tags
            for tag_name, tag_path in mapping.get('tags', {}).items():
                tag_value = self._get_nested_value(message, tag_path)
                if tag_value is not None:
                    point = point.tag(tag_name, str(tag_value))

            # Add fields
            for field_name, field_config in mapping.get('fields', {}).items():
                if isinstance(field_config, str):
                    # Simple field mapping
                    value = self._get_nested_value(message, field_config)
                    if value is not None:
                        point = point.field(field_name, value)
                else:
                    # Complex field mapping with type conversion
                    value = self._get_nested_value(message, field_config['path'])
                    if value is not None:
                        converted_value = self._convert_value(value, field_config.get('type', 'float'))
                        if converted_value is not None:
                            point = point.field(field_name, converted_value)

            # Add timestamp if specified
            timestamp_path = mapping.get('timestamp')
            if timestamp_path:
                timestamp = self._get_nested_value(message, timestamp_path)
                if timestamp:
                    try:
                        if isinstance(timestamp, (int, float)):
                            point = point.time(int(timestamp))
                        else:
                            dt = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
                            point = point.time(int(dt.timestamp() * 1e9))
                    except Exception as e:
                        logger.warning(f"Failed to parse timestamp {timestamp}: {e}")

            return point

        except Exception as e:
            logger.error(f"Failed to transform message to point: {e}")
            return None

    def _get_nested_value(self, obj: Dict, path: str) -> Any:
        """Get nested value from dictionary using dot notation."""
        try:
            parts = path.split('.')
            value = obj
            for part in parts:
                value = value[part]
            return value
        except (KeyError, TypeError):
            return None

    def _convert_value(self, value: Any, type_name: str) -> Any:
        """Convert value to specified type."""
        try:
            if type_name == 'float':
                return float(value)
            elif type_name == 'int':
                return int(value)
            elif type_name == 'bool':
                return bool(value)
            elif type_name == 'str':
                return str(value)
            else:
                return value
        except (ValueError, TypeError):
            return None

    async def process_message(self, message: aiormq.Message):
        """Process a single message."""
        start_time = time.time()
        queue_name = message.routing_key

        try:
            # Parse message
            body = json.loads(message.body)

            # Find matching mapping
            mapping = None
            for m in self.config['mappings']:
                if (m.get('queue') == queue_name or
                    m.get('routing_key', '').replace('*', '.*').replace('#', '.*') == queue_name):
                    mapping = m
                    break

            if not mapping:
                logger.warning(f"No mapping found for routing key: {queue_name}")
                await message.channel.basic_ack(message.delivery.delivery_tag)
                return

            # Transform message to point
            point = self._transform_to_point(body, mapping)
            if not point:
                logger.warning(f"Failed to transform message: {body}")
                await message.channel.basic_ack(message.delivery.delivery_tag)
                return

            # Add to batch
            measurement = mapping['measurement']
            if measurement not in self.batches:
                self.batches[measurement] = BatchConfig(
                    size=mapping.get('batch_size', self.config['influxdb'].get('batch_size', 1000)),
                    interval=mapping.get('batch_interval', self.config['influxdb'].get('batch_interval', 10.0))
                )

            batch = self.batches[measurement]
            batch.points.append(point)
            BATCH_SIZE.labels(measurement=measurement).set(len(batch.points))

            # Check if batch should be written
            if (len(batch.points) >= batch.size or
                time.time() - batch.last_write > batch.interval):
                await self._write_batch(measurement)

            # Update metrics
            MESSAGES_PROCESSED.labels(queue=queue_name, status="success").inc()
            QUEUE_LAG.labels(queue=queue_name).set(time.time() - start_time)

            # Acknowledge message
            await message.channel.basic_ack(message.delivery.delivery_tag)

        except Exception as e:
            logger.error(f"Error processing message: {e}")
            MESSAGES_PROCESSED.labels(queue=queue_name, status="error").inc()
            # Negative acknowledge message for requeue
            await message.channel.basic_nack(message.delivery.delivery_tag, requeue=True)

    async def _write_batch(self, measurement: str):
        """Write a batch of points to InfluxDB."""
        batch = self.batches[measurement]
        if not batch.points:
            return

        try:
            with WRITE_TIME.time():
                self.write_api.write(
                    bucket=self.config['influxdb']['bucket'],
                    record=batch.points
                )

            logger.info(f"Wrote {len(batch.points)} points to measurement {measurement}")
            batch.points = []
            batch.last_write = time.time()
            BATCH_SIZE.labels(measurement=measurement).set(0)

        except Exception as e:
            logger.error(f"Failed to write batch to InfluxDB: {e}")
            # Points will remain in batch for retry

    async def run(self):
        """Run the consumer."""
        try:
            # Set up connections
            await self.setup()

            # Start Prometheus metrics server
            metrics_port = self.config.get('metrics', {}).get('port', 8000)
            start_http_server(metrics_port)
            logger.info(f"Started metrics server on port {metrics_port}")

            # Set up message handler
            for queue in self.config['rabbitmq']['queues']:
                await self.channel.queue_declare(queue=queue['name'], durable=True)
                await self.channel.basic_consume(
                    queue=queue['name'],
                    consumer_callback=self.process_message
                )
                logger.info(f"Started consuming from queue: {queue['name']}")

            # Start batch writer
            while not self.should_exit:
                # Write any full or timed-out batches
                for measurement in list(self.batches.keys()):
                    batch = self.batches[measurement]
                    if (len(batch.points) > 0 and
                        time.time() - batch.last_write > batch.interval):
                        await self._write_batch(measurement)

                await asyncio.sleep(1.0)

        except Exception as e:
            logger.error(f"Fatal error: {e}")
            self.should_exit = True

        finally:
            # Clean up
            if self.write_api:
                self.write_api.close()
            if self.influxdb_client:
                self.influxdb_client.close()
            if self.connection:
                await self.connection.close()

def main():
    """Main entry point."""
    import argparse

    parser = argparse.ArgumentParser(description='InfluxDB Consumer')
    parser.add_argument('--config', '-c', type=str, default='config.yaml',
                      help='Path to configuration file')
    args = parser.parse_args()

    consumer = InfluxDBConsumer(args.config)

    try:
        asyncio.run(consumer.run())
    except KeyboardInterrupt:
        logger.info("Shutting down...")
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()

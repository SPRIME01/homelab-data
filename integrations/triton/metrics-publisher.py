#!/usr/bin/env python3

import os
import sys
import json
import time
import yaml
import logging
import asyncio
import aiohttp
import pika
from typing import Dict, List, Any, Optional
from dataclasses import dataclass
from datetime import datetime
from prometheus_client.parser import text_string_to_metric_families

# Configure logging
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('triton_metrics_publisher.log')
    ]
)
logger = logging.getLogger("triton_metrics")

@dataclass
class MetricConfig:
    """Configuration for a metric to collect."""
    name: str
    type: str
    include_labels: List[str]
    aggregation: Optional[str] = None
    description: Optional[str] = None

class TritonMetricsPublisher:
    def __init__(self, config_path: str = "config.yaml"):
        """Initialize the metrics publisher."""
        self.config = self._load_config(config_path)
        self.connection = None
        self.channel = None
        self.session = None
        self.should_exit = False

        # Set up metrics configuration
        self.metrics_config = self._setup_metrics_config()

        # Last published values for delta calculations
        self.last_values = {}

    def _load_config(self, config_path: str) -> Dict:
        """Load configuration from YAML file."""
        try:
            with open(config_path, 'r') as f:
                config = yaml.safe_load(f)

            required_keys = ['rabbitmq', 'triton', 'metrics']
            for key in required_keys:
                if key not in config:
                    raise ValueError(f"Missing required configuration section: {key}")

            return config
        except Exception as e:
            logger.error(f"Failed to load configuration: {e}")
            sys.exit(1)

    def _setup_metrics_config(self) -> Dict[str, MetricConfig]:
        """Set up metrics configuration from config file."""
        metrics_config = {}

        for metric in self.config['metrics'].get('collect', []):
            metrics_config[metric['name']] = MetricConfig(
                name=metric['name'],
                type=metric.get('type', 'gauge'),
                include_labels=metric.get('include_labels', []),
                aggregation=metric.get('aggregation'),
                description=metric.get('description')
            )

        return metrics_config

    async def connect_rabbitmq(self) -> None:
        """Connect to RabbitMQ with retry logic."""
        while not self.should_exit:
            try:
                # Setup connection parameters
                parameters = pika.ConnectionParameters(
                    host=self.config['rabbitmq']['host'],
                    port=self.config['rabbitmq']['port'],
                    credentials=pika.PlainCredentials(
                        username=self.config['rabbitmq']['username'],
                        password=self.config['rabbitmq']['password']
                    ),
                    heartbeat=60,
                    virtual_host=self.config['rabbitmq'].get('vhost', '/')
                )

                self.connection = pika.BlockingConnection(parameters)
                self.channel = self.connection.channel()

                # Declare exchange
                exchange_config = self.config['rabbitmq'].get('exchange', {})
                self.channel.exchange_declare(
                    exchange=exchange_config.get('name', 'metrics'),
                    exchange_type=exchange_config.get('type', 'topic'),
                    durable=True
                )

                logger.info("Successfully connected to RabbitMQ")
                return
            except Exception as e:
                logger.error(f"Failed to connect to RabbitMQ: {e}")
                await asyncio.sleep(5)

    async def setup_http_session(self) -> None:
        """Set up HTTP session for Triton metrics endpoint."""
        if self.session is None:
            self.session = aiohttp.ClientSession()

    async def collect_metrics(self) -> List[Dict]:
        """Collect metrics from Triton server."""
        try:
            metrics_url = f"http://{self.config['triton']['host']}:{self.config['triton']['metrics_port']}/metrics"

            async with self.session.get(metrics_url) as response:
                response.raise_for_status()
                metrics_text = await response.text()

            # Parse Prometheus format metrics
            metrics = []
            for family in text_string_to_metric_families(metrics_text):
                if family.name in self.metrics_config:
                    config = self.metrics_config[family.name]

                    for sample in family.samples:
                        # Filter labels based on configuration
                        labels = {
                            k: v for k, v in sample.labels.items()
                            if k in config.include_labels
                        }

                        metric_data = {
                            "name": family.name,
                            "type": config.type,
                            "value": sample.value,
                            "labels": labels,
                            "timestamp": datetime.utcnow().isoformat()
                        }

                        if config.description:
                            metric_data["description"] = config.description

                        # Handle counter metrics
                        if config.type == "counter":
                            metric_key = f"{family.name}:{json.dumps(labels, sort_keys=True)}"
                            last_value = self.last_values.get(metric_key, 0)
                            delta = sample.value - last_value
                            self.last_values[metric_key] = sample.value
                            metric_data["delta"] = delta

                        # Apply aggregation if configured
                        if config.aggregation:
                            metric_data["aggregation"] = config.aggregation

                        metrics.append(metric_data)

            return metrics

        except Exception as e:
            logger.error(f"Failed to collect metrics: {e}")
            return []

    async def publish_metrics(self, metrics: List[Dict]) -> None:
        """Publish metrics to RabbitMQ."""
        try:
            if not self.channel or self.channel.is_closed:
                await self.connect_rabbitmq()

            exchange = self.config['rabbitmq']['exchange']['name']
            routing_key_template = self.config['rabbitmq'].get('routing_key', 'metrics.triton.{type}')

            for metric in metrics:
                routing_key = routing_key_template.format(
                    type=metric.get('type', 'unknown'),
                    name=metric['name']
                )

                self.channel.basic_publish(
                    exchange=exchange,
                    routing_key=routing_key,
                    body=json.dumps(metric),
                    properties=pika.BasicProperties(
                        delivery_mode=2,  # Make message persistent
                        timestamp=int(time.time()),
                        content_type='application/json'
                    )
                )

        except Exception as e:
            logger.error(f"Failed to publish metrics: {e}")
            if self.connection:
                try:
                    self.connection.close()
                except:
                    pass
            self.connection = None
            self.channel = None

    async def run(self) -> None:
        """Run the metrics publisher."""
        try:
            # Initialize connections
            await self.setup_http_session()
            await self.connect_rabbitmq()

            collection_interval = self.config['metrics'].get('collection_interval', 15)

            while not self.should_exit:
                start_time = time.time()

                # Collect and publish metrics
                metrics = await self.collect_metrics()
                if metrics:
                    await self.publish_metrics(metrics)
                    logger.info(f"Published {len(metrics)} metrics")

                # Wait for next collection interval
                elapsed = time.time() - start_time
                wait_time = max(0, collection_interval - elapsed)
                await asyncio.sleep(wait_time)

        except Exception as e:
            logger.error(f"Error in metrics publisher: {e}")
            self.should_exit = True

        finally:
            # Cleanup
            if self.session:
                await self.session.close()
            if self.connection and not self.connection.is_closed:
                self.connection.close()

async def main():
    """Main entry point."""
    # Get config path from environment or use default
    config_path = os.environ.get('CONFIG_PATH', 'config.yaml')

    # Create publisher instance
    publisher = TritonMetricsPublisher(config_path)

    # Run the publisher
    await publisher.run()

if __name__ == "__main__":
    asyncio.run(main())

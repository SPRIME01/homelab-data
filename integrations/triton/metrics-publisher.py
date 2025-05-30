#!/usr/bin/env python3

import os
import sys
import json
import time
import yaml
import logging
import asyncio
import aiohttp
import aio_pika # Changed from pika
import signal # Added for signal handling
from typing import Dict, List, Any, Optional
from dataclasses import dataclass, asdict # Added asdict
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
        """
        Initializes the Triton metrics publisher with configuration and runtime state.
        
        Loads configuration from the specified YAML file, sets up placeholders for RabbitMQ and HTTP connections, initializes the event loop, prepares metrics configuration, and creates synchronization primitives for connection management.
        """
        self.config = self._load_config(config_path)
        self.connection: Optional[aio_pika.RobustConnection] = None
        self.channel: Optional[aio_pika.Channel] = None
        self.session: Optional[aiohttp.ClientSession] = None
        self.should_exit = False
        self.loop = asyncio.get_running_loop()

        self.metrics_config = self._setup_metrics_config()
        self.last_values: Dict[str, float] = {} # Ensure type
        self._connection_retry_lock = asyncio.Lock()


    def _load_config(self, config_path: str) -> Dict:
        """
        Loads and validates configuration from a YAML file.
        
        Reads the specified YAML file, ensuring required sections ('rabbitmq', 'triton', 'metrics') and the 'exchange' key within 'rabbitmq' are present. Exits the program if the configuration is invalid or missing required keys.
        
        Args:
            config_path: Path to the YAML configuration file.
        
        Returns:
            A dictionary containing the loaded configuration.
        """
        try:
            with open(config_path, 'r') as f:
                config = yaml.safe_load(f)
            required_keys = ['rabbitmq', 'triton', 'metrics']
            for key in required_keys:
                if key not in config:
                    raise ValueError(f"Missing required configuration section: {key}")
            if 'exchange' not in config['rabbitmq']:
                raise ValueError("Missing 'rabbitmq.exchange' configuration.")
            return config
        except Exception as e:
            logger.error(f"Failed to load configuration: {e}")
            sys.exit(1)

    def _setup_metrics_config(self) -> Dict[str, MetricConfig]:
        """
        Parses the metrics collection configuration and returns a mapping of metric names to MetricConfig objects.
        
        Returns:
            A dictionary where each key is a metric name and each value is a MetricConfig instance representing the configuration for that metric.
        """
        metrics_cfg = {}
        for metric in self.config['metrics'].get('collect', []):
            metrics_cfg[metric['name']] = MetricConfig(
                name=metric['name'], type=metric.get('type', 'gauge'),
                include_labels=metric.get('include_labels', []),
                aggregation=metric.get('aggregation'), description=metric.get('description')
            )
        return metrics_cfg

    async def _signal_handler_async(self, signum: int) -> None:
        """
        Handles termination signals asynchronously by setting the exit flag for graceful shutdown.
        
        Args:
            signum: The signal number received.
        """
        logger.info(f"Received signal {signum}, initiating shutdown...")
        self.should_exit = True

    async def connect_rabbitmq(self) -> bool:
        """
        Attempts to establish an asynchronous connection to RabbitMQ with retry logic.
        
        Returns:
            True if the connection and exchange declaration succeed, False otherwise.
        
        This method uses a lock to prevent concurrent connection attempts and retries the connection up to the configured maximum number of attempts, waiting between retries. If already connected, it returns immediately. Returns False if the maximum number of attempts is reached or shutdown is initiated.
        """
        if self._connection_retry_lock.locked():
            logger.debug("RabbitMQ connection attempt already in progress.")
            return False
        
        async with self._connection_retry_lock:
            if self.connection and not self.connection.is_closed:
                return True

            rabbitmq_config = self.config['rabbitmq']
            connection_url = (
                f"amqp://{rabbitmq_config['username']}:{rabbitmq_config['password']}@"
                f"{rabbitmq_config['host']}:{rabbitmq_config['port']}/"
                f"{rabbitmq_config.get('vhost', '')}"
            )
            retry_delay = rabbitmq_config.get('retry_delay', 5)
            max_attempts = rabbitmq_config.get('connection_attempts', 3)
            attempt = 0

            while not self.should_exit and attempt < max_attempts:
                attempt += 1
                try:
                    logger.info(f"Attempting RabbitMQ connection (attempt {attempt}/{max_attempts})")
                    self.connection = await aio_pika.connect_robust(
                        connection_url, loop=self.loop, heartbeat=rabbitmq_config.get('heartbeat', 60)
                    )
                    self.channel = await self.connection.channel()
                    
                    exchange_config = self.config['rabbitmq']['exchange']
                    await self.channel.declare_exchange(
                        name=exchange_config.get('name', 'metrics'),
                        type=aio_pika.ExchangeType(exchange_config.get('type', 'topic')),
                        durable=exchange_config.get('durable', True)
                    )
                    logger.info("Successfully connected to RabbitMQ and declared exchange.")
                    return True
                except (aio_pika.exceptions.AMQPConnectionError, ConnectionRefusedError) as e:
                    logger.error(f"RabbitMQ connection failed (attempt {attempt}/{max_attempts}): {e}")
                except Exception as e:
                    logger.error(f"Unexpected error during RabbitMQ setup (attempt {attempt}/{max_attempts}): {e}")
                
                if attempt < max_attempts and not self.should_exit:
                    await asyncio.sleep(retry_delay)
            
            logger.error("Max RabbitMQ connection attempts reached or shutdown initiated.")
            return False


    async def setup_http_session(self) -> None:
        """
        Initializes an aiohttp ClientSession for communicating with the Triton metrics endpoint if one does not already exist or is closed.
        """
        if self.session is None or self.session.closed:
            # You might want to pass connector_owner=False if session is managed outside this class instance
            self.session = aiohttp.ClientSession(loop=self.loop) 
            logger.info("aiohttp ClientSession created.")


    async def collect_metrics(self) -> List[Dict]:
        """
        Fetches and parses metrics from the Triton server's Prometheus endpoint.
        
        Attempts to retrieve metrics using an HTTP GET request, parses the response into metric families, and filters them based on the configured metrics. Extracts relevant labels, computes deltas for counters, and includes optional descriptions and aggregation settings. Returns a list of metric dictionaries ready for publishing. Returns an empty list if the HTTP session is unavailable or if any error occurs during collection or parsing.
        """
        if not self.session or self.session.closed:
            # This indicates an issue, as setup_http_session should be called first
            logger.error("aiohttp session not available or closed. Attempting to set up.")
            await self.setup_http_session() # Try to set it up again.
            if not self.session: # If still not available
                 logger.error("Failed to set up aiohttp session. Cannot collect metrics.")
                 return []


        try:
            triton_config = self.config['triton']
            metrics_url = f"http://{triton_config['host']}:{triton_config['metrics_port']}/metrics"
            
            async with self.session.get(metrics_url) as response:
                response.raise_for_status()
                metrics_text = await response.text()

            parsed_metrics = []
            for family in text_string_to_metric_families(metrics_text):
                if family.name in self.metrics_config:
                    metric_cfg = self.metrics_config[family.name]
                    for sample in family.samples:
                        labels = {k: v for k, v in sample.labels.items() if k in metric_cfg.include_labels}
                        metric_data = {
                            "name": family.name, "type": metric_cfg.type, "value": sample.value,
                            "labels": labels, "timestamp": datetime.utcnow().isoformat()
                        }
                        if metric_cfg.description: metric_data["description"] = metric_cfg.description
                        if metric_cfg.type == "counter":
                            metric_key = f"{family.name}:{json.dumps(labels, sort_keys=True)}"
                            last_val = self.last_values.get(metric_key, 0.0) # Ensure float
                            delta = sample.value - last_val
                            self.last_values[metric_key] = sample.value
                            metric_data["delta"] = delta
                        if metric_cfg.aggregation: metric_data["aggregation"] = metric_cfg.aggregation
                        parsed_metrics.append(metric_data)
            return parsed_metrics
        except aiohttp.ClientError as e: # More specific HTTP errors
            logger.error(f"HTTP error collecting metrics from {metrics_url}: {e}")
        except Exception as e:
            logger.error(f"Failed to collect or parse metrics from {metrics_url}: {e}", exc_info=True)
        return []


    async def publish_metrics(self, metrics: List[Dict]) -> None:
        """
        Publishes a list of metrics to RabbitMQ as JSON messages.
        
        Attempts to reconnect to RabbitMQ if the channel is unavailable. Each metric is published to the configured exchange with a routing key based on its type and name. Logs errors for individual message failures and handles channel closure by marking the channel as unusable for future attempts.
        """
        if self.should_exit or not metrics:
            return

        if not self.channel or self.channel.is_closed:
            logger.warning("RabbitMQ channel unavailable. Attempting to reconnect.")
            if not await self.connect_rabbitmq():
                logger.error("Failed to reconnect to RabbitMQ. Cannot publish metrics.")
                return
        
        # This check is crucial after potential reconnection
        if not self.channel or self.channel.is_closed:
            logger.error("Channel is not available even after attempting reconnection for publishing.")
            return

        exchange_name = self.config['rabbitmq']['exchange'].get('name', 'metrics')
        routing_key_template = self.config['rabbitmq'].get('routing_key', 'metrics.triton.{type}')
        
        try:
            # Get exchange object. Ensure=True will try to declare it if it doesn't exist,
            # but it should have been declared in connect_rabbitmq.
            exchange = await self.channel.get_exchange(exchange_name, ensure=False) 

            for metric in metrics:
                try:
                    routing_key = routing_key_template.format(type=metric.get('type', 'unknown'), name=metric['name'])
                    message_body = json.dumps(metric).encode('utf-8')
                    message = aio_pika.Message(
                        body=message_body,
                        delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                        timestamp=datetime.utcnow(),
                        content_type='application/json'
                    )
                    await exchange.publish(message, routing_key=routing_key)
                except Exception as e: # Log per-message publish error
                    logger.error(f"Failed to publish individual metric {metric.get('name')}: {e}")
            logger.debug(f"Published {len(metrics)} metrics to exchange '{exchange_name}'.")
        except aio_pika.exceptions.ChannelClosed as e:
            logger.error(f"Channel closed while publishing metrics: {e}. Will attempt reconnect on next cycle.")
            self.channel = None # Mark channel as unusable
        except Exception as e:
            logger.error(f"Failed to publish metrics batch to exchange '{exchange_name}': {e}", exc_info=True)


    async def run(self) -> None:
        """
        Runs the main asynchronous loop for collecting and publishing Triton metrics.
        
        Continuously collects metrics from the Triton server and publishes them to RabbitMQ at the configured interval. Ensures required connections are established before each cycle and handles graceful shutdown when the exit flag is set.
        """
        await self.setup_http_session() # Setup session once at start

        collection_interval = self.config['metrics'].get('collection_interval', 15)

        while not self.should_exit:
            start_time = time.monotonic() # Use monotonic for interval calculation
            
            if not await self.connect_rabbitmq(): # Ensure connection before collecting/publishing
                logger.error("Cannot proceed without RabbitMQ connection. Retrying after interval.")
            else:
                collected_metrics = await self.collect_metrics()
                if collected_metrics:
                    await self.publish_metrics(collected_metrics)
                    logger.info(f"Collected and attempted publishing for {len(collected_metrics)} metrics.")
            
            elapsed = time.monotonic() - start_time
            wait_time = max(0, collection_interval - elapsed)
            
            if self.should_exit: break # Check exit condition before sleep
            try:
                await asyncio.sleep(wait_time)
            except asyncio.CancelledError: # Handle task cancellation during sleep
                logger.info("Sleep cancelled, likely shutting down.")
                break
        
        logger.info("Metrics publisher run loop ended.")


    async def stop(self): # Added explicit stop method
        """
        Performs a graceful shutdown of the metrics publisher.
        
        Closes the HTTP client session and RabbitMQ connection if they are open, and sets the exit flag to stop the main loop.
        """
        logger.info("Stopping metrics publisher...")
        self.should_exit = True # Ensure exit flag is set
        if self.session and not self.session.closed:
            await self.session.close()
            logger.info("aiohttp ClientSession closed.")
        if self.connection and not self.connection.is_closed:
            await self.connection.close()
            logger.info("RabbitMQ connection closed.")
        logger.info("Metrics publisher stopped.")


async def main_async():
    """
    Runs the asynchronous Triton metrics publisher with graceful shutdown and error handling.
    
    Initializes the publisher from configuration, registers signal handlers for termination,
    executes the main publishing loop, and ensures resources are cleaned up on exit.
    """
    config_path = os.environ.get('CONFIG_PATH', 'config.yaml')
    publisher = TritonMetricsPublisher(config_path)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda s=sig: asyncio.create_task(publisher._signal_handler_async(s)))

    try:
        await publisher.run()
    except Exception as e:
        logger.error(f"Unhandled error in metrics publisher main run: {e}", exc_info=True)
    finally:
        await publisher.stop() # Ensure cleanup is called

if __name__ == "__main__":
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        logger.info("Application interrupted by user.")
    except Exception as e:
        logger.critical(f"Application terminated with unhandled exception: {e}", exc_info=True)
    finally:
        logger.info("Application shutdown finalized.")

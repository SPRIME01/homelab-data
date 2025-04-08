#!/usr/bin/env python3
"""
Data Transformation Pipeline for Homelab Data Mesh

This service consumes messages from RabbitMQ, applies configurable transformations,
and publishes the results to output destinations.
To run the pipline: `python pipeline.py --config config/sensor-enrichment.yaml`.

"""

import os
import sys
import json
import time
import logging
import signal
import asyncio
import importlib
from typing import Dict, List, Any, Callable, Optional, Union
import traceback
from datetime import datetime
from pathlib import Path
from functools import partial
import multiprocessing as mp
from concurrent.futures import ThreadPoolExecutor

import yaml
import pika
import pika.exceptions
from pika.adapters.asyncio_connection import AsyncioConnection
from prometheus_client import start_http_server, Counter, Gauge, Summary, Histogram

# Import schema validation utilities
sys.path.append(str(Path(__file__).parent.parent))
try:
    from schemas.validation import SchemaEnforcer, ValidationError
except ImportError:
    logging.warning("Schema validation module not found. Schema validation will be disabled.")
    SchemaEnforcer = None
    ValidationError = Exception

# Try to import enML for enhanced ML-based transformations
try:
    import enml
    ENML_AVAILABLE = True
except ImportError:
    ENML_AVAILABLE = False
    logging.info("enML not available. ML-based transformations will be disabled.")

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('pipeline.log')
    ]
)
logger = logging.getLogger("pipeline")

# Prometheus metrics
MESSAGES_PROCESSED = Counter('pipeline_messages_processed_total', 'Total number of processed messages', ['pipeline', 'status'])
PROCESSING_TIME = Summary('pipeline_processing_seconds', 'Time spent processing messages', ['pipeline'])
QUEUE_DEPTH = Gauge('pipeline_queue_depth', 'Number of messages in queue', ['pipeline', 'queue'])
TRANSFORMATION_ERRORS = Counter('pipeline_transformation_errors_total', 'Number of transformation errors', ['pipeline', 'transformation'])
PAYLOAD_SIZE = Histogram('pipeline_payload_size_bytes', 'Size of message payloads in bytes', ['pipeline'])
PIPELINE_HEALTH = Gauge('pipeline_health', 'Pipeline health status (1=healthy, 0=unhealthy)', ['pipeline'])

class TransformationError(Exception):
    """Exception raised when a transformation fails."""
    pass

class ConfigurationError(Exception):
    """Exception raised when there's an issue with configuration."""
    pass

class DataPipeline:
    """
    Data transformation pipeline that processes messages from RabbitMQ sources,
    applies transformations, and publishes results to destinations.
    """

    def __init__(self, config_path: str):
        """
        Initialize the data pipeline with configuration.

        Args:
            config_path: Path to the YAML configuration file
        """
        self.config_path = config_path
        self.config = self._load_config(config_path)
        self.name = self.config.get('name', 'unnamed-pipeline')
        self.description = self.config.get('description', '')

        # Get logging level from config or environment
        log_level = os.environ.get('LOG_LEVEL', self.config.get('logging', {}).get('level', 'INFO'))
        logger.setLevel(log_level)

        # Set up schema validation if available
        self.schema_enforcer = None
        if SchemaEnforcer is not None and self.config.get('schema_validation', {}).get('enabled', False):
            schemas_dir = self.config.get('schema_validation', {}).get('schemas_dir')
            self.schema_enforcer = SchemaEnforcer(schemas_dir=schemas_dir)
            logger.info(f"Schema validation enabled with schemas from {schemas_dir}")

        # Initialize connections and channels
        self.connection = None
        self.channel = None
        self.should_exit = False

        # Register signal handlers
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

        # Set up transformation registry
        self.transformations = self._setup_transformations()

        # Set pipeline health to unhealthy until startup completes
        PIPELINE_HEALTH.labels(pipeline=self.name).set(0)

        # Configure thread pool
        max_workers = self.config.get('performance', {}).get('max_workers')
        if max_workers is None:
            max_workers = min(32, (os.cpu_count() or 4) + 4)  # Default from ThreadPoolExecutor
        self.executor = ThreadPoolExecutor(max_workers=max_workers)

        logger.info(f"Initialized pipeline '{self.name}': {self.description}")
        logger.info(f"Using {max_workers} worker threads")

    def _load_config(self, config_path: str) -> Dict:
        """
        Load and validate configuration from YAML file.

        Args:
            config_path: Path to the configuration file

        Returns:
            Dict containing the configuration

        Raises:
            ConfigurationError: If the configuration is invalid
        """
        try:
            with open(config_path, 'r') as f:
                config = yaml.safe_load(f)

            # Validate required configuration sections
            required_sections = ['name', 'sources', 'transformations', 'destinations']
            missing_sections = [section for section in required_sections if section not in config]

            if missing_sections:
                raise ConfigurationError(f"Missing required configuration sections: {', '.join(missing_sections)}")

            # Expand environment variables in configuration strings
            config = self._expand_env_vars(config)

            return config

        except (yaml.YAMLError, OSError) as e:
            raise ConfigurationError(f"Failed to load configuration from {config_path}: {str(e)}")

    def _expand_env_vars(self, config: Union[Dict, List, str, Any]) -> Union[Dict, List, str, Any]:
        """
        Recursively expand environment variables in configuration values.

        Args:
            config: Configuration value (dict, list, or string)

        Returns:
            Configuration with environment variables expanded
        """
        if isinstance(config, dict):
            return {k: self._expand_env_vars(v) for k, v in config.items()}
        elif isinstance(config, list):
            return [self._expand_env_vars(item) for item in config]
        elif isinstance(config, str):
            # Expand environment variables in the format ${VAR_NAME}
            import re
            pattern = r'\${([A-Za-z0-9_]+)}'
            matches = re.findall(pattern, config)

            result = config
            for var_name in matches:
                var_value = os.environ.get(var_name, '')
                result = result.replace(f'${{{var_name}}}', var_value)

            return result
        else:
            return config

    def _setup_transformations(self) -> Dict[str, Callable]:
        """
        Set up transformation functions from configuration.

        Returns:
            Dict mapping transformation names to functions
        """
        transformations = {}

        # Load built-in transformations
        built_ins = {
            'filter': self._transform_filter,
            'map': self._transform_map,
            'enrich': self._transform_enrich,
            'aggregate': self._transform_aggregate,
            'format_convert': self._transform_format_convert,
            'schema_validate': self._transform_schema_validate
        }

        # Add ML-based transformations if enML is available
        if ENML_AVAILABLE:
            built_ins.update({
                'ml_classify': self._transform_ml_classify,
                'ml_extract': self._transform_ml_extract,
                'ml_enrich': self._transform_ml_enrich,
                'ml_anomaly': self._transform_ml_anomaly,
            })

        # Add user-defined transformations from Python modules
        custom_modules = self.config.get('custom_transformations', {}).get('modules', [])
        for module_path in custom_modules:
            try:
                module = importlib.import_module(module_path)
                # Look for functions prefixed with 'transform_'
                for attr_name in dir(module):
                    if attr_name.startswith('transform_'):
                        func_name = attr_name[len('transform_'):]  # Remove prefix
                        transformations[func_name] = getattr(module, attr_name)
                        logger.info(f"Loaded custom transformation '{func_name}' from {module_path}")
            except ImportError as e:
                logger.error(f"Failed to import custom transformation module {module_path}: {str(e)}")

        # Add built-in transformations (don't override custom ones with the same name)
        for name, func in built_ins.items():
            if name not in transformations:
                transformations[name] = func

        return transformations

    def _signal_handler(self, signum: int, _) -> None:
        """
        Handle termination signals.

        Args:
            signum: Signal number
        """
        logger.info(f"Received signal {signum}, shutting down...")
        self.should_exit = True

    async def connect(self) -> None:
        """
        Connect to RabbitMQ with retry logic.
        """
        while not self.should_exit:
            try:
                # Set up connection parameters
                params = pika.ConnectionParameters(
                    host=self.config['rabbitmq']['host'],
                    port=self.config['rabbitmq']['port'],
                    virtual_host=self.config['rabbitmq'].get('vhost', '/'),
                    credentials=pika.PlainCredentials(
                        username=self.config['rabbitmq']['username'],
                        password=self.config['rabbitmq']['password']
                    ),
                    heartbeat=self.config['rabbitmq'].get('heartbeat', 60),
                    connection_attempts=self.config['rabbitmq'].get('connection_attempts', 3),
                    retry_delay=self.config['rabbitmq'].get('retry_delay', 5),
                    blocked_connection_timeout=self.config['rabbitmq'].get('blocked_timeout', 300)
                )

                # Create connection and channel
                self.connection = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: pika.BlockingConnection(params)
                )
                self.channel = await asyncio.get_event_loop().run_in_executor(
                    None, self.connection.channel
                )

                # Configure prefetch
                prefetch_count = self.config.get('performance', {}).get('prefetch_count', 10)
                await asyncio.get_event_loop().run_in_executor(
                    None, lambda: self.channel.basic_qos(prefetch_count=prefetch_count)
                )

                # Declare exchanges if configured
                if self.config.get('declare_exchanges', True):
                    await self._declare_exchanges()

                # Declare queues if configured
                if self.config.get('declare_queues', True):
                    await self._declare_queues()

                logger.info("Connected to RabbitMQ")
                return

            except (pika.exceptions.AMQPConnectionError, pika.exceptions.AMQPChannelError) as e:
                logger.error(f"Failed to connect to RabbitMQ: {str(e)}")

                # Close connection if it exists
                if self.connection and not self.connection.is_closed:
                    try:
                        await asyncio.get_event_loop().run_in_executor(
                            None, self.connection.close
                        )
                    except:
                        pass

                self.connection = None
                self.channel = None

                # Wait before retrying
                retry_delay = self.config['rabbitmq'].get('retry_delay', 5)
                logger.info(f"Retrying in {retry_delay} seconds...")
                await asyncio.sleep(retry_delay)

    async def _declare_exchanges(self) -> None:
        """
        Declare RabbitMQ exchanges from configuration.
        """
        for exchange in self.config.get('declare_resources', {}).get('exchanges', []):
            try:
                name = exchange['name']
                exchange_type = exchange.get('type', 'topic')
                durable = exchange.get('durable', True)
                auto_delete = exchange.get('auto_delete', False)

                await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: self.channel.exchange_declare(
                        exchange=name,
                        exchange_type=exchange_type,
                        durable=durable,
                        auto_delete=auto_delete
                    )
                )
                logger.debug(f"Declared exchange {name} of type {exchange_type}")
            except Exception as e:
                logger.error(f"Failed to declare exchange {exchange.get('name')}: {str(e)}")

    async def _declare_queues(self) -> None:
        """
        Declare RabbitMQ queues from configuration.
        """
        for queue in self.config.get('declare_resources', {}).get('queues', []):
            try:
                name = queue['name']
                durable = queue.get('durable', True)
                exclusive = queue.get('exclusive', False)
                auto_delete = queue.get('auto_delete', False)
                arguments = queue.get('arguments', {})

                await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: self.channel.queue_declare(
                        queue=name,
                        durable=durable,
                        exclusive=exclusive,
                        auto_delete=auto_delete,
                        arguments=arguments
                    )
                )
                logger.debug(f"Declared queue {name}")

                # Set up bindings
                for binding in queue.get('bindings', []):
                    exchange = binding['exchange']
                    routing_key = binding.get('routing_key', '#')

                    await asyncio.get_event_loop().run_in_executor(
                        None,
                        lambda: self.channel.queue_bind(
                            queue=name,
                            exchange=exchange,
                            routing_key=routing_key
                        )
                    )
                    logger.debug(f"Bound queue {name} to exchange {exchange} with routing key {routing_key}")

            except Exception as e:
                logger.error(f"Failed to declare queue {queue.get('name')}: {str(e)}")

    async def start(self) -> None:
        """
        Start the data pipeline.
        """
        try:
            # Start metrics server if enabled
            metrics_config = self.config.get('monitoring', {}).get('metrics', {})
            if metrics_config.get('enabled', False):
                metrics_port = metrics_config.get('port', 8000)
                start_http_server(metrics_port)
                logger.info(f"Started metrics server on port {metrics_port}")

            # Connect to RabbitMQ
            await self.connect()

            # Set up consumers for each source
            for source in self.config['sources']:
                queue_name = source['queue']
                consumer_tag = f"pipeline-{self.name}-{queue_name}"

                # Create a callback for this source
                callback = partial(self._on_message, source=source)

                # Start consuming
                await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: self.channel.basic_consume(
                        queue=queue_name,
                        on_message_callback=callback,
                        consumer_tag=consumer_tag,
                        auto_ack=False
                    )
                )

                logger.info(f"Started consuming from queue {queue_name}")
                QUEUE_DEPTH.labels(pipeline=self.name, queue=queue_name).set(0)

            # Set pipeline as healthy now that startup is complete
            PIPELINE_HEALTH.labels(pipeline=self.name).set(1)
            logger.info(f"Pipeline {self.name} is now running")

            # Keep running until signal is received
            while not self.should_exit:
                if self.connection and self.connection.is_closed:
                    logger.warning("RabbitMQ connection closed unexpectedly, reconnecting...")
                    await self.connect()

                await asyncio.sleep(1)

        except Exception as e:
            logger.error(f"Pipeline startup error: {str(e)}")
            self.should_exit = True

        finally:
            await self.stop()

    async def stop(self) -> None:
        """
        Stop the data pipeline gracefully.
        """
        logger.info("Shutting down data pipeline...")

        # Set pipeline as unhealthy during shutdown
        PIPELINE_HEALTH.labels(pipeline=self.name).set(0)

        # Close RabbitMQ connection if open
        if self.connection and not self.connection.is_closed:
            try:
                await asyncio.get_event_loop().run_in_executor(
                    None, self.connection.close
                )
                logger.info("Closed RabbitMQ connection")
            except Exception as e:
                logger.error(f"Error closing RabbitMQ connection: {str(e)}")

        # Shut down executor
        self.executor.shutdown(wait=True)
        logger.info("Pipeline shutdown complete")

    def _on_message(self, ch, method, properties, body, source: Dict) -> None:
        """
        Handle incoming messages from RabbitMQ.

        Args:
            ch: Channel
            method: Delivery method
            properties: Message properties
            body: Message body
            source: Source configuration
        """
        start_time = time.time()
        message_id = properties.message_id or f"msg-{time.time()}"
        source_name = source.get('name', source['queue'])

        try:
            # Parse message based on content type
            content_type = properties.content_type or 'application/json'
            message = self._parse_message(body, content_type)

            # Update payload size metric
            PAYLOAD_SIZE.labels(pipeline=self.name).observe(len(body))

            # Process message through transformations
            result = self._process_message(message, source)

            # Publish results to destinations
            self._publish_results(result, properties)

            # Acknowledge the message
            ch.basic_ack(delivery_tag=method.delivery_tag)

            # Update metrics
            MESSAGES_PROCESSED.labels(pipeline=self.name, status="success").inc()
            processing_time = time.time() - start_time
            PROCESSING_TIME.labels(pipeline=self.name).observe(processing_time)

            logger.debug(f"Processed message {message_id} from {source_name} in {processing_time:.3f}s")

        except Exception as e:
            logger.error(f"Error processing message {message_id} from {source_name}: {str(e)}")
            logger.debug(traceback.format_exc())

            # Handle message failure according to retry policy
            retry_policy = source.get('retry', {}).get('policy', 'requeue')
            max_retries = source.get('retry', {}).get('max_retries', 3)

            retry_count = 0
            if properties.headers and 'x-retry-count' in properties.headers:
                retry_count = properties.headers['x-retry-count']

            if retry_policy == 'requeue' and retry_count < max_retries:
                # Increment retry count
                new_properties = pika.BasicProperties(
                    content_type=properties.content_type,
                    content_encoding=properties.content_encoding,
                    headers={
                        **(properties.headers or {}),
                        'x-retry-count': retry_count + 1,
                        'x-last-error': str(e)
                    },
                    delivery_mode=properties.delivery_mode,
                    priority=properties.priority,
                    correlation_id=properties.correlation_id,
                    reply_to=properties.reply_to,
                    expiration=properties.expiration,
                    message_id=properties.message_id,
                    timestamp=properties.timestamp,
                    type=properties.type,
                    user_id=properties.user_id,
                    app_id=properties.app_id
                )

                # Reject message for requeue
                ch.basic_reject(delivery_tag=method.delivery_tag, requeue=True)
                logger.info(f"Requeued message {message_id} for retry ({retry_count + 1}/{max_retries})")

            elif retry_policy == 'dead-letter' or retry_count >= max_retries:
                # Send to dead-letter exchange/queue if configured
                dead_letter_exchange = source.get('retry', {}).get('dead_letter_exchange')
                dead_letter_routing_key = source.get('retry', {}).get('dead_letter_routing_key', method.routing_key)

                if dead_letter_exchange:
                    try:
                        # Add error information to headers
                        headers = properties.headers or {}
                        headers.update({
                            'x-error': str(e),
                            'x-failed-at': datetime.utcnow().isoformat(),
                            'x-source-queue': source['queue'],
                            'x-original-routing-key': method.routing_key
                        })

                        # Create new properties with error information
                        error_properties = pika.BasicProperties(
                            content_type=properties.content_type,
                            content_encoding=properties.content_encoding,
                            headers=headers,
                            delivery_mode=properties.delivery_mode,
                            priority=properties.priority,
                            correlation_id=properties.correlation_id,
                            reply_to=properties.reply_to,
                            expiration=properties.expiration,
                            message_id=properties.message_id,
                            timestamp=properties.timestamp,
                            type=properties.type,
                            user_id=properties.user_id,
                            app_id=properties.app_id
                        )

                        # Publish to dead-letter exchange
                        self.channel.basic_publish(
                            exchange=dead_letter_exchange,
                            routing_key=dead_letter_routing_key,
                            body=body,
                            properties=error_properties
                        )

                        # Acknowledge the original message
                        ch.basic_ack(delivery_tag=method.delivery_tag)
                        logger.info(f"Sent message {message_id} to dead-letter exchange {dead_letter_exchange}")

                    except Exception as dlq_error:
                        logger.error(f"Failed to send message to dead-letter exchange: {str(dlq_error)}")
                        # If dead-lettering fails, reject without requeue
                        ch.basic_reject(delivery_tag=method.delivery_tag, requeue=False)
                else:
                    # No dead-letter exchange configured, reject without requeue
                    ch.basic_reject(delivery_tag=method.delivery_tag, requeue=False)
                    logger.info(f"Rejected message {message_id} without requeue after {retry_count} retries")
            else:
                # Unknown retry policy, reject without requeue
                ch.basic_reject(delivery_tag=method.delivery_tag, requeue=False)
                logger.warning(f"Unknown retry policy '{retry_policy}', rejected message {message_id}")

            # Update metrics
            MESSAGES_PROCESSED.labels(pipeline=self.name, status="error").inc()

    def _parse_message(self, body: bytes, content_type: str) -> Any:
        """
        Parse message body based on content type.

        Args:
            body: Raw message body
            content_type: Content type of the message

        Returns:
            Parsed message

        Raises:
            ValueError: If message parsing fails
        """
        if content_type == 'application/json':
            return json.loads(body)
        elif content_type == 'text/plain':
            return body.decode('utf-8')
        elif content_type == 'application/x-yaml':
            return yaml.safe_load(body)
        else:
            # For unknown content types, return raw bytes
            logger.warning(f"Unknown content type: {content_type}, returning raw bytes")
            return body

    def _process_message(self, message: Any, source: Dict) -> Any:
        """
        Process a message through the configured transformation pipeline.

        Args:
            message: The message to process
            source: Source configuration

        Returns:
            Transformed message

        Raises:
            TransformationError: If a transformation fails
        """
        result = message
        source_name = source.get('name', source['queue'])

        # Apply source-specific transformations first
        for transform_config in source.get('transformations', []):
            result = self._apply_transformation(result, transform_config, f"{source_name}:source")

        # Apply global transformations
        pipeline_name = self.config.get('name', 'pipeline')
        for transform_config in self.config.get('transformations', []):
            result = self._apply_transformation(result, transform_config, pipeline_name)

        return result

    def _apply_transformation(self, data: Any, transform_config: Dict, context: str) -> Any:
        """
        Apply a single transformation to data.

        Args:
            data: Input data
            transform_config: Transformation configuration
            context: Context string for error reporting

        Returns:
            Transformed data

        Raises:
            TransformationError: If transformation fails
        """
        transform_type = transform_config.get('type')
        if not transform_type:
            raise TransformationError(f"Missing transformation type in configuration")

        if transform_type not in self.transformations:
            raise TransformationError(f"Unknown transformation type: {transform_type}")

        transform_name = transform_config.get('name', transform_type)

        try:
            # Get the transformation function
            transform_func = self.transformations[transform_type]

            # Apply the transformation
            result = transform_func(data, transform_config)

            # If result is None, skip further processing
            if result is None:
                logger.debug(f"Transformation {transform_name} returned None, skipping message")
                raise TransformationError(f"Transformation {transform_name} filtered out the message")

            return result

        except Exception as e:
            # Update error metrics
            TRANSFORMATION_ERRORS.labels(pipeline=self.name, transformation=transform_name).inc()

            # Check if error should be propagated or ignored
            if transform_config.get('continue_on_error', False):
                logger.warning(f"Transformation {transform_name} failed in {context}, but continuing: {str(e)}")
                # Return original data
                return data
            else:
                logger.error(f"Transformation {transform_name} failed in {context}: {str(e)}")
                raise TransformationError(f"Transformation {transform_name} failed: {str(e)}") from e

    def _publish_results(self, result: Any, properties: pika.BasicProperties) -> None:
        """
        Publish processing results to destinations.

        Args:
            result: Processed result
            properties: Original message properties
        """
        if not self.channel or self.channel.is_closed:
            raise RuntimeError("RabbitMQ channel is closed")

        # Get correlation ID for tracking
        correlation_id = properties.correlation_id or properties.message_id or str(time.time())

        # Publish to each destination
        for destination in self.config['destinations']:
            try:
                exchange = destination['exchange']
                routing_key = destination['routing_key']

                # Apply routing key template if result is a dictionary
                if isinstance(result, dict) and '{' in routing_key and '}' in routing_key:
                    # Extract placeholders and replace with values from result
                    import re
                    placeholders = re.findall(r'\{([^}]+)\}', routing_key)

                    for placeholder in placeholders:
                        if placeholder in result:
                            value = str(result[placeholder]).replace('.', '_').replace(' ', '_')
                            routing_key = routing_key.replace(f"{{{placeholder}}}", value)

                # Serialize result based on content type
                content_type = destination.get('content_type', 'application/json')
                body = self._serialize_message(result, content_type)

                # Create message properties
                dest_properties = pika.BasicProperties(
                    content_type=content_type,
                    content_encoding='utf-8',
                    delivery_mode=2,  # Persistent
                    correlation_id=correlation_id,
                    timestamp=int(time.time()),
                    message_id=str(time.time()),  # Generate new message ID
                    app_id=self.name
                )

                # Publish message
                self.channel.basic_publish(
                    exchange=exchange,
                    routing_key=routing_key,
                    body=body,
                    properties=dest_properties
                )

                logger.debug(f"Published result to exchange {exchange} with routing key {routing_key}")

            except Exception as e:
                logger.error(f"Failed to publish to destination {destination.get('name', exchange)}: {str(e)}")
                # Don't re-raise, continue with other destinations

    def _serialize_message(self, message: Any, content_type: str) -> bytes:
        """
        Serialize message based on content type.

        Args:
            message: Message to serialize
            content_type: Target content type

        Returns:
            Serialized message as bytes

        Raises:
            ValueError: If serialization fails
        """
        try:
            if content_type == 'application/json':
                return json.dumps(message).encode('utf-8')
            elif content_type == 'text/plain':
                if isinstance(message, (dict, list)):
                    return json.dumps(message).encode('utf-8')
                return str(message).encode('utf-8')
            elif content_type == 'application/x-yaml':
                return yaml.dump(message).encode('utf-8')
            else:
                # For unknown content types, try JSON or return as-is
                if isinstance(message, (dict, list)):
                    return json.dumps(message).encode('utf-8')
                elif isinstance(message, str):
                    return message.encode('utf-8')
                elif isinstance(message, bytes):
                    return message
                else:
                    return str(message).encode('utf-8')
        except Exception as e:
            raise ValueError(f"Failed to serialize message as {content_type}: {str(e)}")

    # === Built-in transformations ===

    def _transform_filter(self, data: Any, config: Dict) -> Any:
        """
        Filter transformation - keeps or drops messages based on conditions.

        Args:
            data: Input data
            config: Transformation configuration

        Returns:
            Data if it passes the filter, None otherwise
        """
        conditions = config.get('conditions', [])
        mode = config.get('mode', 'all')  # 'all' or 'any'

        if not isinstance(data, dict):
            # Can only filter dictionary data
            return data

        results = []
        for condition in conditions:
            field = condition.get('field')
            operator = condition.get('operator', 'eq')
            value = condition.get('value')

            # Handle nested fields using dot notation
            field_value = data
            if field:
                for part in field.split('.'):
                    if isinstance(field_value, dict) and part in field_value:
                        field_value = field_value[part]
                    else:
                        field_value = None
                        break

            # Apply operator
            if operator == 'eq':
                results.append(field_value == value)
            elif operator == 'neq':
                results.append(field_value != value)
            elif operator == 'gt':
                results.append(field_value > value)
            elif operator == 'gte':
                results.append(field_value >= value)
            elif operator == 'lt':
                results.append(field_value < value)
            elif operator == 'lte':
                results.append(field_value <= value)
            elif operator == 'in':
                results.append(field_value in value)
            elif operator == 'nin':
                results.append(field_value not in value)
            elif operator == 'contains':
                results.append(value in field_value if field_value else False)
            elif operator == 'exists':
                results.append(field_value is not None)
            elif operator == 'regex':
                import re
                pattern = re.compile(value)
                results.append(bool(pattern.match(str(field_value))) if field_value else False)
            else:
                logger.warning(f"Unknown operator: {operator}")
                results.append(False)

        # Combine results based on mode
        if mode == 'all' and all(results):
            return data
        elif mode == 'any' and any(results):
            return data
        else:
            return None

    def _transform_map(self, data: Any, config: Dict) -> Any:
        """
        Map transformation - extracts fields or creates new structure.

        Args:
            data: Input data
            config: Transformation configuration

        Returns:
            Mapped data
        """
        if not isinstance(data, dict):
            # Can only map dictionary data
            return data

        mapping = config.get('mapping', {})
        result = {}

        for dest_key, source_path in mapping.items():
            if isinstance(source_path, str):
                # Simple field mapping
                value = data
                for part in source_path.split('.'):
                    if isinstance(value, dict) and part in value:
                        value = value[part]
                    else:
                        value = None
                        break
                result[dest_key] = value
            elif isinstance(source_path, dict) and 'value' in source_path:
                # Static value
                result[dest_key] = source_path['value']
            elif isinstance(source_path, dict) and 'template' in source_path:
                # Template string - simple substitution
                template = source_path['template']
                import re

                # Replace {field.path} with actual values
                def replace_placeholder(match):
                    field_path = match.group(1)
                    value = data
                    for part in field_path.split('.'):
                        if isinstance(value, dict) and part in value:
                            value = value[part]
                        else:
                            value = ''
                            break
                    return str(value)

                result[dest_key] = re.sub(r'\{([^}]+)\}', replace_placeholder, template)

        # Include original data if configured
        if config.get('include_original', False):
            result.update(data)

        return result

    def _transform_enrich(self, data: Any, config: Dict) -> Any:
        """
        Enrich transformation - adds data from external sources or lookups.

        Args:
            data: Input data
            config: Transformation configuration

        Returns:
            Enriched data
        """
        if not isinstance(data, dict):
            # Can only enrich dictionary data
            return data

        # Create a copy to avoid modifying the original
        result = dict(data)

        # Handle different enrichment types
        enrichment_type = config.get('enrichment_type', 'static')

        if enrichment_type == 'static':
            # Add static values
            static_values = config.get('values', {})
            for key, value in static_values.items():
                # Support dot notation for nested keys
                target = result
                parts = key.split('.')

                # Navigate to the correct nesting level
                for part in parts[:-1]:
                    if part not in target:
                        target[part] = {}
                    target = target[part]

                # Set the value at the final level
                target[parts[-1]] = value

        elif enrichment_type == 'lookup':
            # Lookup values from a dictionary
            lookup_key = config.get('lookup_key')
            lookup_table = config.get('lookup_table', {})
            default = config.get('default')
            target_field = config.get('target_field')

            # Extract lookup key value from data
            lookup_value = data
            if lookup_key:
                for part in lookup_key.split('.'):
                    if isinstance(lookup_value, dict) and part in lookup_value:
                        lookup_value = lookup_value[part]
                    else:
                        lookup_value = None
                        break

            # Perform lookup
            if lookup_value is not None:
                lookup_result = lookup_table.get(str(lookup_value), default)

                # Store result
                if target_field:
                    # Support dot notation for target field
                    target = result
                    parts = target_field.split('.')

                    # Navigate to the correct nesting level
                    for part in parts[:-1]:
                        if part not in target:
                            target[part] = {}
                        target = target[part]

                    # Set the value at the final level
                    target[parts[-1]] = lookup_result
                else:
                    # Merge with result if it's a dictionary
                    if isinstance(lookup_result, dict):
                        result.update(lookup_result)

        elif enrichment_type == 'file':
            # Load data from a file
            file_path = config.get('file_path')
            file_format = config.get('file_format', 'json')
            target_field = config.get('target_field')

            if file_path:
                try:
                    with open(file_path, 'r') as f:
                        if file_format == 'json':
                            file_data = json.load(f)
                        elif file_format == 'yaml':
                            file_data = yaml.safe_load(f)
                        elif file_format == 'csv':
                            import csv
                            reader = csv.DictReader(f)
                            file_data = list(reader)
                        else:
                            file_data = f.read()

                    # Store result
                    if target_field:
                        # Support dot notation for target field
                        target = result
                        parts = target_field.split('.')

                        # Navigate to the correct nesting level
                        for part in parts[:-1]:
                            if part not in target:
                                target[part] = {}
                            target = target[part]

                        # Set the value at the final level
                        target[parts[-1]] = file_data
                    else:
                        # Merge with result if it's a dictionary
                        if isinstance(file_data, dict):
                            result.update(file_data)
                except Exception as e:
                    logger.error(f"Failed to load enrichment data from file {file_path}: {str(e)}")

        elif enrichment_type == 'timestamp':
            # Add timestamp fields
            format_string = config.get('format', '%Y-%m-%dT%H:%M:%S.%fZ')
            target_field = config.get('target_field', 'timestamp')

            timestamp = datetime.utcnow()
            formatted = timestamp.strftime(format_string)

            # Store timestamp
            # Support dot notation for target field
            target = result
            parts = target_field.split('.')

            # Navigate to the correct nesting level
            for part in parts[:-1]:
                if part not in target:
                    target[part] = {}
                target = target[part]

            # Set the value at the final level
            target[parts[-1]] = formatted

            # Add ISO format if requested
            if config.get('add_iso', False):
                iso_field = config.get('iso_field', 'timestamp_iso')

                # Support dot notation for ISO field
                target = result
                parts = iso_field.split('.')

                # Navigate to the correct nesting level
                for part in parts[:-1]:
                    if part not in target:
                        target[part] = {}
                    target = target[part]

                # Set the value at the final level
                target[parts[-1]] = timestamp.isoformat()

            # Add epoch if requested
            if config.get('add_epoch', False):
                epoch_field = config.get('epoch_field', 'timestamp_epoch')

                # Support dot notation for epoch field
                target = result
                parts = epoch_field.split('.')

                # Navigate to the correct nesting level
                for part in parts[:-1]:
                    if part not in target:
                        target[part] = {}
                    target = target[part]

                # Set the value at the final level
                target[parts[-1]] = timestamp.timestamp()

        return result

    def _transform_aggregate(self, data: Any, config: Dict) -> Any:
        """
        Aggregate transformation - combines multiple messages into one.

        Args:
            data: Input data
            config: Transformation configuration

        Returns:
            Aggregated data or original data
        """
        # Not implemented in stateless mode - this would require message
        # storage and triggered aggregation, which is beyond the scope
        # of a simple transformation
        logger.warning("Aggregate transformation requires stateful processing, not implemented")
        return data

    def _transform_format_convert(self, data: Any, config: Dict) -> Any:
        """
        Format conversion transformation.

        Args:
            data: Input data
            config: Transformation configuration

        Returns:
            Converted data
        """
        source_format = config.get('source_format', 'json')
        target_format = config.get('target_format', 'json')

        # Special case: no conversion needed
        if source_format == target_format:
            return data

        # Special case: already in memory format
        if source_format == 'memory':
            if target_format == 'json':
                # Just ensure it's JSON serializable
                try:
                    json.dumps(data)  # Test serialization
                    return data
                except:
                    return str(data)
            elif target_format == 'yaml':
                try:
                    yaml.dump(data)  # Test serialization
                    return data
                except:
                    return str(data)
            elif target_format == 'string':
                return str(data)
            else:
                logger.warning(f"Unsupported target format: {target_format}")
                return data

        # Conversion from specific formats
        try:
            if source_format == 'json' and isinstance(data, str):
                # Parse JSON string
                parsed = json.loads(data)

                if target_format == 'memory':
                    return parsed
                elif target_format == 'yaml':
                    return yaml.dump(parsed)
                elif target_format == 'string':
                    return str(parsed)

            elif source_format == 'yaml' and isinstance(data, str):
                # Parse YAML string
                parsed = yaml.safe_load(data)

                if target_format == 'memory':
                    return parsed
                elif target_format == 'json':
                    return json.dumps(parsed)
                elif target_format == 'string':
                    return str(parsed)

            elif source_format == 'csv' and isinstance(data, str):
                # Parse CSV string
                import csv
                import io

                reader = csv.DictReader(io.StringIO(data))
                parsed = list(reader)

                if target_format == 'memory':
                    return parsed
                elif target_format == 'json':
                    return json.dumps(parsed)
                elif target_format == 'yaml':
                    return yaml.dump(parsed)
                elif target_format == 'string':
                    return str(parsed)

            # Add more conversions as needed
        except Exception as e:
            logger.error(f"Format conversion error: {str(e)}")

        # Default: return data as-is
        return data

    def _transform_schema_validate(self, data: Any, config: Dict) -> Any:
        """
        Schema validation transformation.

        Args:
            data: Input data
            config: Transformation configuration

        Returns:
            Validated data (possibly with defaults)
        """
        if not self.schema_enforcer:
            logger.warning("Schema validation requested but schema enforcer is not available")
            return data

        schema_id = config.get('schema_id')
        version = config.get('version')
        mode = config.get('mode', 'validate')  # validate, validate_or_fix, skip

        if not schema_id:
            logger.warning("Schema validation requested but no schema_id specified")
            return data

        try:
            if mode == 'validate':
                # Strict validation
                return self.schema_enforcer.validate_message(data, schema_id, version)
            elif mode == 'validate_or_fix':
                # Try to fix common issues
                return self.schema_enforcer.validate_or_fix_message(data, schema_id, version)
            elif mode == 'skip':
                # Just try validation but continue even if it fails
                try:
                    return self.schema_enforcer.validate_message(data, schema_id, version)
                except ValidationError:
                    return data
            else:
                logger.warning(f"Unknown schema validation mode: {mode}")
                return data
        except ValidationError as e:
            # Propagate validation error
            raise TransformationError(f"Schema validation failed for {schema_id}: {e.message}")
        except Exception as e:
            logger.error(f"Schema validation error: {str(e)}")
            raise TransformationError(f"Schema validation error: {str(e)}")

    # === ML-based transformations (requires enML) ===

    def _transform_ml_classify(self, data: Any, config: Dict) -> Any:
        """
        ML classification transformation (requires enML).

        Args:
            data: Input data
            config: Transformation configuration

        Returns:
            Data with classification results
        """
        if not ENML_AVAILABLE:
            raise TransformationError("ML transformations require enML which is not available")

        # Extract text or other features from data
        source_field = config.get('source_field', 'text')
        target_field = config.get('target_field', 'classification')
        model_name = config.get('model', 'default')

        # Extract source data
        source_value = data
        if isinstance(data, dict) and source_field:
            for part in source_field.split('.'):
                if isinstance(source_value, dict) and part in source_value:
                    source_value = source_value[part]
                else:
                    source_value = None
                    break

        if source_value is None:
            logger.warning(f"Source field {source_field} not found in data")
            return data

        try:
            # Use enML for classification
            classifier = enml.Classification(model_name)
            classification = classifier.classify(source_value)

            # Add classification to result
            result = data if isinstance(data, dict) else {"input": data}

            if isinstance(result, dict):
                # Add classification to result, support dot notation for target field
                target = result
                parts = target_field.split('.')

                # Navigate to the correct nesting level
                for part in parts[:-1]:
                    if part not in target:
                        target[part] = {}
                    target = target[part]

                # Set the classification at the final level
                target[parts[-1]] = classification

                return result
            else:
                logger.warning("Cannot add classification to non-dict data")
                return data
        except Exception as e:
            logger.error(f"ML classification error: {str(e)}")
            if config.get('continue_on_error', False):
                return data
            else:
                raise TransformationError(f"ML classification error: {str(e)}")

    def _transform_ml_extract(self, data: Any, config: Dict) -> Any:
        """
        ML entity extraction transformation (requires enML).

        Args:
            data: Input data
            config: Transformation configuration

        Returns:
            Data with extracted entities
        """
        if not ENML_AVAILABLE:
            raise TransformationError("ML transformations require enML which is not available")

        # Extract text from data
        source_field = config.get('source_field', 'text')
        target_field = config.get('target_field', 'entities')
        model_name = config.get('model', 'default')
        entity_types = config.get('entity_types')  # Optional: specific types to extract

        # Extract source data
        source_value = data
        if isinstance(data, dict) and source_field:
            for part in source_field.split('.'):
                if isinstance(source_value, dict) and part in source_value:
                    source_value = source_value[part]
                else:
                    source_value = None
                    break

        if source_value is None:
            logger.warning(f"Source field {source_field} not found in data")
            return data

        try:
            # Use enML for entity extraction
            extractor = enml.EntityExtraction(model_name)

            # Extract entities, optionally filtering by type
            if entity_types:
                entities = extractor.extract(source_value, entity_types=entity_types)
            else:
                entities = extractor.extract(source_value)

            # Add entities to result
            result = data if isinstance(data, dict) else {"input": data}

            if isinstance(result, dict):
                # Add entities to result, support dot notation for target field
                target = result
                parts = target_field.split('.')

                # Navigate to the correct nesting level
                for part in parts[:-1]:
                    if part not in target:
                        target[part] = {}
                    target = target[part]

                # Set the entities at the final level
                target[parts[-1]] = entities

                return result
            else:
                logger.warning("Cannot add entities to non-dict data")
                return data
        except Exception as e:
            logger.error(f"ML entity extraction error: {str(e)}")
            if config.get('continue_on_error', False):
                return data
            else:
                raise TransformationError(f"ML entity extraction error: {str(e)}")

    def _transform_ml_enrich(self, data: Any, config: Dict) -> Any:
        """
        ML enrichment transformation (requires enML).

        Args:
            data: Input data
            config: Transformation configuration

        Returns:
            Enriched data
        """
        if not ENML_AVAILABLE:
            raise TransformationError("ML transformations require enML which is not available")

        # This is a more flexible ML transformation that can add various ML-derived fields
        enrichment_type = config.get('enrichment_type', 'sentiment')
        source_field = config.get('source_field', 'text')
        target_field = config.get('target_field')
        model_name = config.get('model', 'default')

        # Extract source data
        source_value = data
        if isinstance(data, dict) and source_field:
            for part in source_field.split('.'):
                if isinstance(source_value, dict) and part in source_value:
                    source_value = source_value[part]
                else:
                    source_value = None
                    break

        if source_value is None:
            logger.warning(f"Source field {source_field} not found in data")
            return data

        try:
            # Apply ML enrichment based on type
            result = None

            if enrichment_type == 'sentiment':
                # Use enML for sentiment analysis
                analyzer = enml.SentimentAnalysis(model_name)
                sentiment = analyzer.analyze(source_value)
                result = sentiment
            elif enrichment_type == 'summarize':
                # Use enML for text summarization
                summarizer = enml.Summarization(model_name)
                summary = summarizer.summarize(source_value)
                result = summary
            elif enrichment_type == 'keywords':
                # Use enML for keyword extraction
                keyword_extractor = enml.KeywordExtraction(model_name)
                keywords = keyword_extractor.extract(source_value)
                result = keywords
            elif enrichment_type == 'language':
                # Use enML for language detection
                detector = enml.LanguageDetection(model_name)
                language = detector.detect(source_value)
                result = language
            else:
                logger.warning(f"Unknown ML enrichment type: {enrichment_type}")
                return data

            # Add result to data
            if isinstance(data, dict) and target_field:
                # Add result to data, support dot notation for target field
                target = data
                parts = target_field.split('.')

                # Navigate to the correct nesting level
                for part in parts[:-1]:
                    if part not in target:
                        target[part] = {}
                    target = target[part]

                # Set the result at the final level
                target[parts[-1]] = result

                return data
            else:
                # Return the enrichment result directly if data is not a dict or no target field
                return result

        except Exception as e:
            logger.error(f"ML enrichment error: {str(e)}")
            if config.get('continue_on_error', False):
                return data
            else:
                raise TransformationError(f"ML enrichment error: {str(e)}")

    def _transform_ml_anomaly(self, data: Any, config: Dict) -> Any:
        """
        ML anomaly detection transformation (requires enML).

        Args:
            data: Input data
            config: Transformation configuration

        Returns:
            Data with anomaly detection results
        """
        if not ENML_AVAILABLE:
            raise TransformationError("ML transformations require enML which is not available")

        # Extract features from data
        source_field = config.get('source_field')  # Field containing numeric features
        target_field = config.get('target_field', 'anomaly')
        model_name = config.get('model', 'default')

        # Extract source data
        source_value = data
        if isinstance(data, dict) and source_field:
            for part in source_field.split('.'):
                if isinstance(source_value, dict) and part in source_value:
                    source_value = source_value[part]
                else:
                    source_value = None
                    break

        # For anomaly detection, data should be numeric or a list of numerics
        if source_value is None:
            logger.warning(f"Source field {source_field} not found in data")
            return data

        try:
            # Use enML for anomaly detection
            detector = enml.AnomalyDetection(model_name)

            # Detect anomalies
            anomaly_result = detector.detect(source_value)

            # Add anomaly result to data
            result = data if isinstance(data, dict) else {"input": data}

            if isinstance(result, dict):
                # Add anomaly result, support dot notation for target field
                target = result
                parts = target_field.split('.')

                # Navigate to the correct nesting level
                for part in parts[:-1]:
                    if part not in target:
                        target[part] = {}
                    target = target[part]

                # Set the anomaly result at the final level
                target[parts[-1]] = anomaly_result

                return result
            else:
                logger.warning("Cannot add anomaly result to non-dict data")
                return data
        except Exception as e:
            logger.error(f"ML anomaly detection error: {str(e)}")
            if config.get('continue_on_error', False):
                return data
            else:
                raise TransformationError(f"ML anomaly detection error: {str(e)}")

async def main():
    """Main entry point."""
    import argparse

    parser = argparse.ArgumentParser(description='Data Transformation Pipeline')
    parser.add_argument('--config', '-c', type=str, required=True,
                      help='Path to YAML configuration file')
    parser.add_argument('--metrics-port', '-p', type=int,
                      help='Port for Prometheus metrics server')
    parser.add_argument('--log-level', '-l', type=str, choices=['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'],
                      default='INFO', help='Logging level')

    args = parser.parse_args()

    # Set log level
    logging.getLogger().setLevel(args.log_level)

    # Override metrics port if specified
    if args.metrics_port:
        try:
            start_http_server(args.metrics_port)
            logger.info(f"Started metrics server on port {args.metrics_port}")
        except Exception as e:
            logger.error(f"Failed to start metrics server: {str(e)}")

    # Create and run pipeline
    try:
        pipeline = DataPipeline(args.config)
        await pipeline.start()
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
    except Exception as e:
        logger.error(f"Pipeline error: {str(e)}")
        traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__":
    asyncio.run(main())

#!/usr/bin/env python3

import os
import sys
import json
import time
import yaml
import uuid
import logging
import asyncio
import signal
import numpy as np
import pika
import aiohttp
import tritonclient.grpc as triton_grpc
import tritonclient.http as triton_http
from typing import Dict, List, Union, Any, Optional, Tuple
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from io import BytesIO

# Configure logging
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('inference_handler.log')
    ]
)
logger = logging.getLogger("inference_handler")

# Define constants
DEFAULT_CONFIG_PATH = "config.yaml"
DEFAULT_BATCH_SIZE = 4
DEFAULT_BATCH_TIMEOUT_MS = 100
DEFAULT_INFERENCE_TIMEOUT_MS = 10000
DEFAULT_MAX_RETRIES = 3

class TritonClientType(Enum):
    HTTP = "http"
    GRPC = "grpc"

@dataclass
class InferenceRequest:
    """Represents a single inference request."""
    request_id: str
    model_name: str
    model_version: str
    inputs: Dict[str, Any]
    outputs: List[str]
    priority: int = 0
    timeout_ms: int = DEFAULT_INFERENCE_TIMEOUT_MS
    received_time: float = field(default_factory=time.time)
    correlation_id: Optional[str] = None
    reply_to: Optional[str] = None
    routing_key: Optional[str] = None
    raw_request: Dict = field(default_factory=dict)
    context_id: Optional[str] = None  # Add context ID to request

@dataclass
class BatchedRequests:
    """Represents a batch of inference requests for a specific model."""
    model_name: str
    model_version: str
    requests: List[InferenceRequest] = field(default_factory=list)
    created_time: float = field(default_factory=time.time)
    max_batch_size: int = DEFAULT_BATCH_SIZE
    timeout_ms: int = DEFAULT_BATCH_TIMEOUT_MS

    @property
    def is_full(self) -> bool:
        """Check if batch is full."""
        return len(self.requests) >= self.max_batch_size

    @property
    def is_timed_out(self) -> bool:
        """Check if batch has timed out."""
        elapsed_ms = (time.time() - self.created_time) * 1000
        return elapsed_ms >= self.timeout_ms

    @property
    def should_process(self) -> bool:
        """Check if batch should be processed."""
        return self.is_full or (self.is_timed_out and len(self.requests) > 0)

    @property
    def highest_priority(self) -> int:
        """Get highest priority in batch."""
        if not self.requests:
            return 0
        return max(req.priority for req in self.requests)

from mcp_handler import ModelContextManager, ConversationContext

class InferenceHandler:
    """Handles inference requests from RabbitMQ and forwards to Triton."""

    def __init__(self, config_path: str = DEFAULT_CONFIG_PATH):
        """Initialize the inference handler."""
        self.config = self._load_config(config_path)
        self.connection = None
        self.channel = None
        self.should_exit = False

        # Initialize client type
        client_type = self.config.get("triton", {}).get("client_type", "grpc")
        self.client_type = TritonClientType(client_type.lower())

        # Batched requests by model
        self.batch_lock = asyncio.Lock()
        self.batched_requests = {}

        # Thread pool for blocking operations
        self.executor = ThreadPoolExecutor(
            max_workers=self.config.get("handler", {}).get("max_workers", 10)
        )

        # Setup signal handlers
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

        # Initialize Model Context Protocol if enabled
        self.mcp_enabled = self.config.get("advanced_features", {}).get("model_context_protocol", {}).get("enabled", False)
        self.context_manager = None
        if self.mcp_enabled:
            self.context_manager = ModelContextManager(self.config.get("advanced_features", {}).get("model_context_protocol", {}))
            logger.info("Model Context Protocol (MCP) enabled")

    def _load_config(self, config_path: str) -> Dict:
        """Load configuration from YAML file."""
        try:
            with open(config_path, 'r') as f:
                config = yaml.safe_load(f)

            # Validate required configuration
            required_keys = ['rabbitmq', 'triton', 'models']
            for key in required_keys:
                if key not in config:
                    raise ValueError(f"Missing required configuration section: {key}")

            return config
        except Exception as e:
            logger.error(f"Failed to load configuration: {e}")
            sys.exit(1)

    def _signal_handler(self, signum: int, frame) -> None:
        """Handle system signals."""
        logger.info(f"Received signal {signum}, initiating graceful shutdown")
        self.should_exit = True

    async def connect_rabbitmq(self) -> None:
        """Connect to RabbitMQ with retry logic."""
        while not self.should_exit:
            try:
                # Setup connection parameters
                parameters = pika.ConnectionParameters(
                    host=self.config['rabbitmq']['host'],
                    port=self.config['rabbitmq']['port'],
                    credentials=pika.PlainCredentials(
                        self.config['rabbitmq']['username'],
                        self.config['rabbitmq']['password']
                    ),
                    heartbeat=self.config['rabbitmq'].get('heartbeat', 60),
                    virtual_host=self.config['rabbitmq'].get('vhost', '/'),
                    connection_attempts=3
                )

                # Create blocking connection and channel
                self.connection = pika.BlockingConnection(parameters)
                self.channel = self.connection.channel()

                # Declare exchanges
                for exchange in self.config['rabbitmq'].get('exchanges', []):
                    self.channel.exchange_declare(
                        exchange=exchange['name'],
                        exchange_type=exchange['type'],
                        durable=exchange.get('durable', True)
                    )

                # Declare and bind queues
                for queue in self.config['rabbitmq'].get('queues', []):
                    self.channel.queue_declare(
                        queue=queue['name'],
                        durable=queue.get('durable', True),
                        arguments={
                            'x-max-priority': queue.get('max_priority', 10),
                            'x-message-ttl': queue.get('message_ttl', 60000)
                        }
                    )

                    # Bind queue to exchange
                    if 'bindings' in queue:
                        for binding in queue['bindings']:
                            self.channel.queue_bind(
                                queue=queue['name'],
                                exchange=binding['exchange'],
                                routing_key=binding['routing_key']
                            )

                logger.info("Successfully connected to RabbitMQ")
                return
            except Exception as e:
                logger.error(f"Failed to connect to RabbitMQ: {e}")
                await asyncio.sleep(5)

    @asynccontextmanager
    async def get_triton_client(self, model_name: str):
        """Get a Triton client for the specified model."""
        try:
            # Get model specific configuration
            model_config = next(
                (m for m in self.config['models'] if m['name'] == model_name),
                None
            )

            if not model_config:
                logger.warning(f"No configuration found for model {model_name}, using default")
                model_config = {}

            # Determine client type (HTTP or gRPC)
            if model_config.get('client_type'):
                client_type = TritonClientType(model_config['client_type'].lower())
            else:
                client_type = self.client_type

            # Get Triton server URL
            triton_url = model_config.get('url', self.config['triton']['url'])

            if client_type == TritonClientType.GRPC:
                # For gRPC, extract host and port
                if ':' in triton_url:
                    host, port = triton_url.split(':')
                    client = triton_grpc.InferenceServerClient(
                        url=f"{host}:{port}",
                        verbose=False
                    )
                else:
                    client = triton_grpc.InferenceServerClient(
                        url=f"{triton_url}:8001",
                        verbose=False
                    )
            else:
                # For HTTP
                if not triton_url.startswith(('http://', 'https://')):
                    triton_url = f"http://{triton_url}"
                client = triton_http.InferenceServerClient(
                    url=triton_url,
                    verbose=False
                )

            yield client

        except Exception as e:
            logger.error(f"Error creating Triton client: {e}")
            raise

    async def add_to_batch(self, request: InferenceRequest) -> None:
        """Add an inference request to the appropriate batch."""
        model_key = f"{request.model_name}:{request.model_version}"

        async with self.batch_lock:
            if model_key not in self.batched_requests:
                # Get model-specific batch configuration
                model_config = next(
                    (m for m in self.config['models'] if m['name'] == request.model_name),
                    {}
                )

                max_batch_size = model_config.get('max_batch_size', DEFAULT_BATCH_SIZE)
                batch_timeout_ms = model_config.get('batch_timeout_ms', DEFAULT_BATCH_TIMEOUT_MS)

                self.batched_requests[model_key] = BatchedRequests(
                    model_name=request.model_name,
                    model_version=request.model_version,
                    max_batch_size=max_batch_size,
                    timeout_ms=batch_timeout_ms
                )

            # Add to existing batch
            self.batched_requests[model_key].requests.append(request)

            logger.debug(
                f"Added request to batch {model_key}. "
                f"Batch size: {len(self.batched_requests[model_key].requests)}/{self.batched_requests[model_key].max_batch_size}"
            )

    async def process_batches(self) -> None:
        """Periodically check and process batches of requests."""
        while not self.should_exit:
            batches_to_process = []

            # Check for batches that are ready to process
            async with self.batch_lock:
                for model_key, batch in list(self.batched_requests.items()):
                    if batch.should_process:
                        batches_to_process.append((model_key, batch))
                        # Remove from main dictionary
                        del self.batched_requests[model_key]

            # Process batches
            if batches_to_process:
                for model_key, batch in batches_to_process:
                    logger.info(f"Processing batch for {model_key} with {len(batch.requests)} requests")
                    asyncio.create_task(self.process_batch(batch))

            # Short sleep to avoid CPU spinning
            await asyncio.sleep(0.01)

    async def process_batch(self, batch: BatchedRequests) -> None:
        """Process a batch of inference requests."""
        if not batch.requests:
            return

        try:
            model_name = batch.model_name
            model_version = batch.model_version

            # Get model configuration
            model_config = next(
                (m for m in self.config['models'] if m['name'] == model_name),
                {}
            )

            # Sort requests by priority (high to low)
            batch.requests.sort(key=lambda r: r.priority, reverse=True)

            # Prepare batched inputs
            input_tensors = {}
            request_map = {}

            # Batch compatible requests
            for i, request in enumerate(batch.requests):
                # Map request ID to position in batch for output distribution
                request_id = request.request_id
                request_map[i] = request

                # Process each input
                for input_name, input_data in request.inputs.items():
                    # Create tensor entry if it doesn't exist
                    if input_name not in input_tensors:
                        input_tensors[input_name] = []

                    # Add input data to the appropriate tensor
                    input_tensors[input_name].append(input_data)

            # Check if any requests in batch use context
            contextual_requests = []
            if self.mcp_enabled:
                contextual_requests = [req for req in batch.requests if hasattr(req, 'context_id') and req.context_id]

                # Process contextual requests individually if needed
                if contextual_requests:
                    for req in contextual_requests:
                        # Get context
                        context = await self.context_manager.get_context(req.context_id)
                        if context:
                            # Augment the request inputs with context if needed
                            if "text_input" in req.inputs and "conversation" in context.context_data:
                                # Example: prepend conversation history for language models
                                history = context.context_data["conversation"]
                                prefix = ""
                                # Include last N turns (adjust as needed)
                                for turn in history[-3:]:  # Last 3 turns
                                    prefix += f"User: {turn['user_input']}\nAssistant: {turn['model_output']}\n"
                                prefix += f"User: {req.inputs['text_input']}\nAssistant: "
                                req.inputs["text_input"] = prefix

            # Execute inference with batched data
            async with self.get_triton_client(model_name) as client:
                if self.client_type == TritonClientType.GRPC:
                    infer_result = await self._infer_grpc(
                        client, model_name, model_version, input_tensors, batch.requests
                    )
                else:
                    infer_result = await self._infer_http(
                        client, model_name, model_version, input_tensors, batch.requests
                    )

                # Process results
                await self._process_inference_results(infer_result, batch, request_map)

            # Update context for contextual requests
            if self.mcp_enabled and contextual_requests:
                for i, req in enumerate(contextual_requests):
                    if req.context_id:
                        # Extract this request's slice of the output
                        if output_name in output_data and output_data[output_name] is not None:
                            output_array = output_data[output_name]
                            if len(output_array) > i:
                                result = self._convert_numpy_to_json(output_array[i])
                                # Update context with the new turn
                                input_text = req.inputs.get("original_text", req.inputs.get("text_input", ""))
                                await self.context_manager.update_context(
                                    req.context_id,
                                    input_text,
                                    result
                                )

        except Exception as e:
            logger.error(f"Error processing batch for {batch.model_name}: {e}")
            # Handle the error and notify about failed requests
            await self._handle_batch_failure(batch, str(e))

    async def _infer_grpc(self, client, model_name, model_version, input_tensors, requests):
        """Perform inference using gRPC client."""
        # Convert inputs to Triton format
        triton_inputs = []

        for input_name, input_values in input_tensors.items():
            # Determine input type and shape
            sample = input_values[0]
            if isinstance(sample, (bytes, str)):
                # String/bytes input
                if isinstance(sample, str):
                    input_values = [val.encode('utf-8') for val in input_values]

                infer_input = triton_grpc.InferInput(input_name, [len(input_values)], "BYTES")
                infer_input.set_data_from_numpy(np.array(input_values, dtype=np.object_))
            else:
                # Numeric input (convert to numpy)
                try:
                    batch_array = np.array(input_values)
                    infer_input = triton_grpc.InferInput(
                        input_name, [len(input_values)] + list(batch_array.shape[1:]),
                        triton_grpc.utils.np_to_triton_dtype(batch_array.dtype)
                    )
                    infer_input.set_data_from_numpy(batch_array)
                except Exception as e:
                    logger.error(f"Error converting input {input_name} to numpy: {e}")
                    raise

            triton_inputs.append(infer_input)

        # Prepare outputs
        output_names = []
        for req in requests:
            output_names.extend(req.outputs)
        output_names = list(set(output_names))  # Deduplicate

        triton_outputs = [triton_grpc.InferRequestedOutput(name) for name in output_names]

        # Execute inference
        response = await asyncio.get_event_loop().run_in_executor(
            self.executor,
            lambda: client.infer(
                model_name=model_name,
                model_version=model_version if model_version else "",
                inputs=triton_inputs,
                outputs=triton_outputs,
                client_timeout=max(req.timeout_ms for req in requests) / 1000.0
            )
        )

        return response

    async def _infer_http(self, client, model_name, model_version, input_tensors, requests):
        """Perform inference using HTTP client."""
        # Convert inputs to Triton format
        triton_inputs = []

        for input_name, input_values in input_tensors.items():
            # Determine input type and shape
            sample = input_values[0]
            if isinstance(sample, (bytes, str)):
                # String/bytes input
                if isinstance(sample, str):
                    input_values = [val.encode('utf-8') for val in input_values]

                infer_input = triton_http.InferInput(input_name, [len(input_values)], "BYTES")
                infer_input.set_data_from_numpy(np.array(input_values, dtype=np.object_))
            else:
                # Numeric input (convert to numpy)
                try:
                    batch_array = np.array(input_values)
                    infer_input = triton_http.InferInput(
                        input_name, [len(input_values)] + list(batch_array.shape[1:]),
                        triton_http.utils.np_to_triton_dtype(batch_array.dtype)
                    )
                    infer_input.set_data_from_numpy(batch_array)
                except Exception as e:
                    logger.error(f"Error converting input {input_name} to numpy: {e}")
                    raise

            triton_inputs.append(infer_input)

        # Prepare outputs
        output_names = []
        for req in requests:
            output_names.extend(req.outputs)
        output_names = list(set(output_names))  # Deduplicate

        triton_outputs = [triton_http.InferRequestedOutput(name) for name in output_names]

        # Execute inference
        response = await asyncio.get_event_loop().run_in_executor(
            self.executor,
            lambda: client.infer(
                model_name=model_name,
                model_version=model_version if model_version else "",
                inputs=triton_inputs,
                outputs=triton_outputs,
                client_timeout=max(req.timeout_ms for req in requests) / 1000.0
            )
        )

        return response

    async def _process_inference_results(self, inference_result, batch, request_map):
        """Process the results of inference and publish responses."""
        try:
            # Get output data as numpy arrays
            output_data = {}
            for output_name in set().union(*(req.outputs for req in batch.requests)):
                try:
                    output_data[output_name] = inference_result.as_numpy(output_name)
                except Exception as e:
                    logger.error(f"Error extracting output {output_name}: {e}")
                    output_data[output_name] = None

            # Process each request's result
            for i, request in request_map.items():
                response = {
                    "request_id": request.request_id,
                    "model_name": batch.model_name,
                    "model_version": batch.model_version,
                    "outputs": {},
                    "timestamp": datetime.utcnow().isoformat(),
                    "processing_time_ms": (time.time() - request.received_time) * 1000
                }

                # Extract outputs for this request
                for output_name in request.outputs:
                    if output_name in output_data and output_data[output_name] is not None:
                        output_array = output_data[output_name]

                        # Extract this request's slice of the output
                        if len(output_array) > i:
                            # Convert various numpy types to JSON-serializable format
                            result = self._convert_numpy_to_json(output_array[i])
                            response["outputs"][output_name] = result
                        else:
                            logger.warning(f"Output index {i} out of bounds for output {output_name}")
                            response["outputs"][output_name] = None
                    else:
                        response["outputs"][output_name] = None

                # Publish response back to RabbitMQ
                await self._publish_response(request, response)

        except Exception as e:
            logger.error(f"Error processing inference results: {e}")
            await self._handle_batch_failure(batch, str(e))

    def _convert_numpy_to_json(self, data):
        """Convert numpy data to JSON-serializable format."""
        if isinstance(data, np.ndarray):
            if data.dtype.kind == 'S':  # Byte strings
                return data.tobytes().decode('utf-8', errors='replace')
            elif data.dtype.kind == 'U':  # Unicode strings
                return str(data)
            else:
                return data.tolist()
        elif isinstance(data, (np.number, np.bool_)):
            return data.item()
        elif isinstance(data, bytes):
            return data.decode('utf-8', errors='replace')
        else:
            return data

    async def _publish_response(self, request, response):
        """Publish inference response to RabbitMQ."""
        try:
            if not self.channel or self.channel.is_closed:
                await self.connect_rabbitmq()

            # Determine where to send the response
            exchange = self.config['rabbitmq'].get('response_exchange', '')
            routing_key = request.routing_key or self.config['rabbitmq'].get('response_routing_key', 'ai.inference.results')

            # Set up message properties
            properties = pika.BasicProperties(
                correlation_id=request.correlation_id,
                content_type='application/json',
                delivery_mode=2  # Persistent message
            )

            # If reply_to is specified, use direct reply
            if request.reply_to:
                routing_key = request.reply_to
                exchange = ''  # Direct reply uses default exchange

            # Publish the message
            self.channel.basic_publish(
                exchange=exchange,
                routing_key=routing_key,
                body=json.dumps(response).encode(),
                properties=properties
            )

            logger.debug(f"Published response for request {request.request_id}")

        except Exception as e:
            logger.error(f"Failed to publish response: {e}")

    async def _handle_batch_failure(self, batch, error_message):
        """Handle failure of an entire batch by notifying about each request."""
        for request in batch.requests:
            error_response = {
                "request_id": request.request_id,
                "model_name": batch.model_name,
                "model_version": batch.model_version,
                "error": error_message,
                "timestamp": datetime.utcnow().isoformat(),
                "processing_time_ms": (time.time() - request.received_time) * 1000
            }
            await self._publish_response(request, error_response)

    def _parse_message(self, body, properties, routing_key):
        """Parse an incoming RabbitMQ message into an InferenceRequest."""
        try:
            # Parse message body
            if isinstance(body, bytes):
                message = json.loads(body.decode('utf-8'))
            else:
                message = json.loads(body)

            # Validate required fields
            if 'model_name' not in message:
                raise ValueError("Missing required field: model_name")

            # Validate inputs are present
            if 'inputs' not in message or not message['inputs']:
                raise ValueError("Missing or empty inputs field")

            # Extract context ID if MCP is enabled
            context_id = None
            if self.mcp_enabled and "context_id" in message:
                context_id = message["context_id"]
                logger.debug(f"Request with context ID: {context_id}")

            # Validate context ID if present
            if context_id and not isinstance(context_id, str):
                raise ValueError("Invalid context ID format")

            # Create inference request
            request = InferenceRequest(
                request_id=message.get('request_id', str(uuid.uuid4())),
                model_name=message['model_name'],
                model_version=message.get('model_version', ""),
                inputs=message['inputs'],
                outputs=message.get('outputs', []),
                priority=message.get('priority', 0),
                timeout_ms=message.get('timeout_ms', DEFAULT_INFERENCE_TIMEOUT_MS),
                correlation_id=properties.correlation_id,
                reply_to=properties.reply_to,
                routing_key=routing_key,
                raw_request=message,
                context_id=context_id  # Add context ID to request
            )

            return request

        except json.JSONDecodeError:
            logger.error("Failed to decode JSON message")
            raise
        except KeyError as e:
            logger.error(f"Missing required field in message: {e}")
            raise
        except Exception as e:
            logger.error(f"Error parsing message: {e}")
            raise

    async def start_consumer(self):
        """Start consuming messages from RabbitMQ."""
        if not self.channel:
            await self.connect_rabbitmq()

        def message_callback(ch, method, properties, body):
            """Process RabbitMQ message."""
            try:
                # Parse message
                request = self._parse_message(body, properties, method.routing_key)

                # Add request to batch
                asyncio.run_coroutine_threadsafe(
                    self.add_to_batch(request),
                    asyncio.get_event_loop()
                )

                # Acknowledge message
                ch.basic_ack(delivery_tag=method.delivery_tag)

            except Exception as e:
                logger.error(f"Error processing message: {e}")
                # Reject message
                ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)

        # Setup QoS
        prefetch_count = self.config['rabbitmq'].get('prefetch_count', 10)
        self.channel.basic_qos(prefetch_count=prefetch_count)

        # Start consuming from all configured queues
        for queue in self.config['rabbitmq'].get('queues', []):
            queue_name = queue['name']
            self.channel.basic_consume(
                queue=queue_name,
                on_message_callback=message_callback
            )
            logger.info(f"Started consuming from queue: {queue_name}")

        logger.info("RabbitMQ consumer started")

    async def run(self):
        """Run the inference handler."""
        # Connect to RabbitMQ
        await self.connect_rabbitmq()

        # Start batch processing task
        batch_task = asyncio.create_task(self.process_batches())

        # Start consumer in a separate thread
        await asyncio.get_event_loop().run_in_executor(
            self.executor,
            lambda: self.start_consumer()
        )

        # Keep the consumer running in a separate thread
        await asyncio.get_event_loop().run_in_executor(
            self.executor,
            lambda: self._start_consuming()
        )

        # Wait for shutdown
        while not self.should_exit:
            await asyncio.sleep(1)

        # Clean shutdown
        batch_task.cancel()
        if self.connection and self.connection.is_open:
            self.connection.close()
        self.executor.shutdown()

        logger.info("Inference handler shutdown complete")

    def _start_consuming(self):
        """Start the blocking consume operation."""
        try:
            self.channel.start_consuming()
        except Exception as e:
            logger.error(f"Error in consumer: {e}")
            self.should_exit = True

async def main():
    """Main entry point."""
    # Get config path from environment or use default
    config_path = os.environ.get("CONFIG_PATH", DEFAULT_CONFIG_PATH)

    # Create handler instance
    handler = InferenceHandler(config_path)

    # Run the handler
    await handler.run()

if __name__ == "__main__":
    # Setup environment
    asyncio.run(main())

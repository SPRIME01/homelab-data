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
import aio_pika # Changed from pika
import aiohttp # Already present, ensure it's used if http calls are made by this service directly
import tritonclient.grpc as triton_grpc
import tritonclient.http as triton_http
from typing import Dict, List, Union, Any, Optional, Tuple
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, asdict # Added asdict
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
# DEFAULT_MAX_RETRIES = 3 # This was unused

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
    routing_key: Optional[str] = None # Original routing key of the request
    raw_request: Dict = field(default_factory=dict)
    context_id: Optional[str] = None

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
        """
        Returns True if the batch has reached its maximum allowed size.
        """
        return len(self.requests) >= self.max_batch_size

    @property
    def is_timed_out(self) -> bool:
        """
        Determines whether the batch has exceeded its configured timeout.
        
        Returns:
            True if the batch's age in milliseconds is greater than or equal to its timeout; otherwise, False.
        """
        elapsed_ms = (time.time() - self.created_time) * 1000
        return elapsed_ms >= self.timeout_ms

    @property
    def should_process(self) -> bool:
        """
        Determines whether the batch is ready for processing.
        
        Returns:
            True if the batch is full or has timed out and contains at least one request; otherwise, False.
        """
        return self.is_full or (self.is_timed_out and len(self.requests) > 0)

    @property
    def highest_priority(self) -> int:
        """
        Returns the highest priority value among all requests in the batch.
        
        If the batch contains no requests, returns 0.
        """
        if not self.requests: return 0
        return max(req.priority for req in self.requests)

# Assuming mcp_handler.py is in the same directory or PYTHONPATH
from mcp_handler import ModelContextManager # Removed ConversationContext as it's not directly used here

class InferenceHandler:
    def __init__(self, config_path: str = DEFAULT_CONFIG_PATH):
        """
        Initializes the InferenceHandler with configuration, batching, and context management.
        
        Loads configuration from the specified path, sets up RabbitMQ connection placeholders, initializes batching structures and locks, configures the Triton client type, and prepares a thread pool executor for inference calls. If Model Context Protocol (MCP) is enabled in the configuration, initializes the context manager for conversational context tracking.
        """
        self.config = self._load_config(config_path)
        self.connection: Optional[aio_pika.RobustConnection] = None
        self.channel: Optional[aio_pika.Channel] = None
        self.should_exit = False
        self.loop = asyncio.get_running_loop() # Get loop in init

        client_type_str = self.config.get("triton", {}).get("client_type", "grpc")
        self.client_type = TritonClientType(client_type_str.lower())

        self.batch_lock = asyncio.Lock()
        self.batched_requests: Dict[str, BatchedRequests] = {}
        
        self.executor = ThreadPoolExecutor(
            max_workers=self.config.get("handler", {}).get("max_workers", os.cpu_count() or 1)
        )
        
        self.mcp_enabled = self.config.get("advanced_features", {}).get("model_context_protocol", {}).get("enabled", False)
        self.context_manager: Optional[ModelContextManager] = None
        if self.mcp_enabled:
            mcp_config = self.config.get("advanced_features", {}).get("model_context_protocol", {})
            self.context_manager = ModelContextManager(mcp_config)
            logger.info("Model Context Protocol (MCP) enabled")

        self._consuming_tags: List[str] = []


    def _load_config(self, config_path: str) -> Dict:
        """
        Loads and validates the YAML configuration file.
        
        Reads the configuration from the specified path, ensuring required sections ('rabbitmq', 'triton', 'models') are present. Exits the application if loading or validation fails.
        
        Args:
            config_path: Path to the YAML configuration file.
        
        Returns:
            A dictionary containing the loaded configuration.
        """
        try:
            with open(config_path, 'r') as f:
                config = yaml.safe_load(f)
            required_keys = ['rabbitmq', 'triton', 'models']
            for key in required_keys:
                if key not in config:
                    raise ValueError(f"Missing required configuration section: {key}")
            return config
        except Exception as e:
            logger.error(f"Failed to load configuration: {e}")
            sys.exit(1)

    async def _signal_handler_async(self, signum: int) -> None:
        """
        Handles termination signals by initiating a graceful shutdown.
        
        Sets a flag to indicate that the service should exit upon receiving a termination signal.
        """
        logger.info(f"Received signal {signum}, initiating graceful shutdown")
        self.should_exit = True

    async def connect_rabbitmq(self) -> None:
        """
        Establishes an asynchronous connection to RabbitMQ and declares exchanges and queues.
        
        Attempts to connect to RabbitMQ using configuration parameters, sets up exchanges and queues with specified properties, and applies queue bindings. Retries connection on failure until successful or shutdown is requested.
        """
        rabbitmq_config = self.config['rabbitmq']
        connection_url = (
            f"amqp://{rabbitmq_config['username']}:{rabbitmq_config['password']}@"
            f"{rabbitmq_config['host']}:{rabbitmq_config['port']}/"
            f"{rabbitmq_config.get('vhost', '')}" 
        )
        
        retry_delay = rabbitmq_config.get('retry_delay', 5)
        
        while not self.should_exit:
            try:
                self.connection = await aio_pika.connect_robust(
                    connection_url,
                    loop=self.loop,
                    heartbeat=rabbitmq_config.get('heartbeat', 60)
                )
                self.channel = await self.connection.channel()
                await self.channel.set_qos(prefetch_count=rabbitmq_config.get('prefetch_count', 10))

                logger.info("Successfully connected to RabbitMQ.")

                for ex_conf in self.config['rabbitmq'].get('exchanges', []):
                    await self.channel.declare_exchange(
                        name=ex_conf['name'],
                        type=aio_pika.ExchangeType(ex_conf['type']),
                        durable=ex_conf.get('durable', True)
                    )
                
                for q_conf in self.config['rabbitmq'].get('queues', []):
                    queue = await self.channel.declare_queue(
                        name=q_conf['name'],
                        durable=q_conf.get('durable', True),
                        arguments={
                            'x-max-priority': q_conf.get('max_priority', 10),
                            'x-message-ttl': q_conf.get('message_ttl', 60000)
                        }
                    )
                    if 'bindings' in q_conf:
                        for binding in q_conf['bindings']:
                            await queue.bind(
                                exchange=binding['exchange'],
                                routing_key=binding['routing_key']
                            )
                return
            except (aio_pika.exceptions.AMQPConnectionError, ConnectionRefusedError) as e:
                logger.error(f"Failed to connect to RabbitMQ: {e}. Retrying in {retry_delay}s...")
            except Exception as e: # Catch other potential errors
                logger.error(f"An unexpected error occurred during RabbitMQ setup: {e}. Retrying in {retry_delay}s...")
            
            if not self.should_exit:
                 await asyncio.sleep(retry_delay)


    @asynccontextmanager
    async def get_triton_client(self, model_name: str):
        # This method remains largely the same as Triton client instantiation is not async
        # but the context manager itself is async.
        """
        Asynchronously provides a Triton inference client for the specified model as a context manager.
        
        Selects the appropriate Triton client type (gRPC or HTTP) and server URL based on model-specific or global configuration. Yields the instantiated client for use within an async context. Ensures proper cleanup of the client if applicable.
        
        Args:
            model_name: The name of the model for which to create the Triton client.
        
        Yields:
            An instance of the appropriate Triton inference client for the specified model.
        
        Raises:
            Exception: If client creation fails or configuration is invalid.
        """
        client = None # Define client outside try to ensure it's in scope for finally
        try:
            model_conf = next((m for m in self.config['models'] if m['name'] == model_name), None)
            effective_conf = model_conf if model_conf else {}
            
            client_type_str = effective_conf.get('client_type', self.config.get("triton", {}).get("client_type", "grpc"))
            client_type_enum = TritonClientType(client_type_str.lower())
            
            triton_url = effective_conf.get('url', self.config['triton']['url'])

            if client_type_enum == TritonClientType.GRPC:
                host, port_str = triton_url.split(':') if ':' in triton_url else (triton_url, "8001")
                client = triton_grpc.InferenceServerClient(url=f"{host}:{port_str}", verbose=False)
            else: # HTTP
                processed_url = triton_url if triton_url.startswith(('http://', 'https://')) else f"http://{triton_url}"
                client = triton_http.InferenceServerClient(url=processed_url, verbose=False)
            
            yield client
        except Exception as e:
            logger.error(f"Error creating Triton client for model {model_name}: {e}")
            raise
        finally:
            if client and hasattr(client, 'close'): # Some clients might have a close method
                 try:
                     if self.client_type == TritonClientType.GRPC: # gRPC client has close()
                         client.close()
                 except Exception as e:
                     logger.error(f"Error closing Triton client: {e}")


    async def add_to_batch(self, request: InferenceRequest) -> None:
        """
        Adds an inference request to the appropriate batch for its model and version.
        
        If no batch exists for the specified model and version, a new batch is created using model-specific or default batch configuration. This method is thread-safe and ensures requests are grouped for efficient batched inference.
        """
        model_key = f"{request.model_name}:{request.model_version}"
        async with self.batch_lock:
            if model_key not in self.batched_requests:
                model_config = next((m for m in self.config['models'] if m['name'] == request.model_name), {})
                self.batched_requests[model_key] = BatchedRequests(
                    model_name=request.model_name,
                    model_version=request.model_version,
                    max_batch_size=model_config.get('max_batch_size', DEFAULT_BATCH_SIZE),
                    timeout_ms=model_config.get('batch_timeout_ms', DEFAULT_BATCH_TIMEOUT_MS)
                )
            self.batched_requests[model_key].requests.append(request)
            logger.debug(f"Added request to batch {model_key}. Size: {len(self.batched_requests[model_key].requests)}")

    async def process_batches(self) -> None:
        """
        Continuously monitors and processes inference batches that are ready for execution.
        
        Collects batches that are either full or have timed out, removes them from the batch queue, and schedules their processing asynchronously. This loop runs until a shutdown signal is received.
        """
        while not self.should_exit:
            batches_to_process_list: List[BatchedRequests] = []
            async with self.batch_lock:
                for model_key, batch_obj in list(self.batched_requests.items()):
                    if batch_obj.should_process:
                        batches_to_process_list.append(batch_obj)
                        del self.batched_requests[model_key]
            
            for batch_to_process in batches_to_process_list:
                logger.info(f"Processing batch for {batch_to_process.model_name} with {len(batch_to_process.requests)} requests")
                asyncio.create_task(self.process_single_batch(batch_to_process)) # Renamed for clarity
            await asyncio.sleep(0.01) # Yield control

    async def process_single_batch(self, batch: BatchedRequests) -> None: # Renamed from process_batch
        """
        Processes a batch of inference requests for a specific model and publishes results.
        
        Aggregates input tensors from all requests in the batch, optionally augments inputs with conversational context if enabled, and performs inference using the appropriate Triton client (gRPC or HTTP). Publishes inference results for each request via RabbitMQ. On error, sends failure responses for all requests in the batch.
        """
        if not batch.requests: return
        # ... (rest of the _infer_grpc, _infer_http, _process_inference_results, _convert_numpy_to_json logic remains largely the same)
        # ... but they will be called by this method.
        # For brevity, I'll skip pasting the entire infer/processing logic but it's assumed to be here.
        # Key is that Triton client calls are already run in executor.
        # The _publish_response method will need to be updated.
        try:
            model_name = batch.model_name
            model_version = batch.model_version

            input_tensors: Dict[str, list] = {}
            request_map: Dict[int, InferenceRequest] = {}

            for i, request in enumerate(batch.requests):
                request_map[i] = request
                for input_name, input_data in request.inputs.items():
                    if input_name not in input_tensors:
                        input_tensors[input_name] = []
                    input_tensors[input_name].append(input_data)
            
            # MCP related logic - assuming self.context_manager is checked for None if mcp_enabled
            if self.mcp_enabled and self.context_manager:
                for req in batch.requests: # Simplified loop for example
                    if req.context_id:
                        context = await self.context_manager.get_context(req.context_id)
                        if context and "text_input" in req.inputs and "conversation" in context.context_data:
                            # Simplified context augmentation
                            history = context.context_data["conversation"]
                            prefix = "".join(f"User: {t['user_input']}\nAssistant: {t['model_output']}\n" for t in history[-3:])
                            req.inputs["text_input"] = prefix + f"User: {req.inputs['text_input']}\nAssistant: "
            
            async with self.get_triton_client(model_name) as client:
                # Determine client type for this specific model to call the right infer method
                model_config = next((m for m in self.config['models'] if m['name'] == model_name), {})
                client_type_str = model_config.get('client_type', self.config.get("triton", {}).get("client_type", "grpc"))
                current_client_type = TritonClientType(client_type_str.lower())

                if current_client_type == TritonClientType.GRPC:
                    infer_result = await self._infer_grpc(client, model_name, model_version, input_tensors, batch.requests)
                else:
                    infer_result = await self._infer_http(client, model_name, model_version, input_tensors, batch.requests)
                
                await self._process_inference_results(infer_result, batch, request_map, current_client_type) # Pass client type

        except Exception as e:
            logger.error(f"Error processing batch for {batch.model_name}: {e}", exc_info=True)
            await self._handle_batch_failure(batch, str(e))


    async def _infer_grpc(self, client: triton_grpc.InferenceServerClient, model_name, model_version, input_tensors, requests):
        # ... (existing _infer_grpc logic, ensure it uses self.executor for client.infer)
        """
        Performs batched inference on a Triton Inference Server using the gRPC client.
        
        Prepares input tensors and requested outputs from the batch, then asynchronously
        invokes the Triton gRPC client's `infer` method in a thread pool executor. Handles
        both byte/string and numeric input types.
        
        Args:
            client: The Triton gRPC InferenceServerClient instance.
            model_name: Name of the model to use for inference.
            model_version: Version of the model to use, or None for default.
            input_tensors: Dictionary mapping input names to lists of input values.
            requests: List of InferenceRequest objects in the batch.
        
        Returns:
            The inference result returned by the Triton gRPC client.
        """
        triton_inputs = []
        for input_name, input_values in input_tensors.items():
            sample = input_values[0]
            if isinstance(sample, (bytes, str)):
                if isinstance(sample, str): input_values = [val.encode('utf-8') for val in input_values]
                infer_input = triton_grpc.InferInput(input_name, [len(input_values)], "BYTES")
                infer_input.set_data_from_numpy(np.array(input_values, dtype=np.object_))
            else:
                batch_array = np.array(input_values)
                infer_input = triton_grpc.InferInput(input_name, [len(input_values)] + list(batch_array.shape[1:]), triton_grpc.utils.np_to_triton_dtype(batch_array.dtype))
                infer_input.set_data_from_numpy(batch_array)
            triton_inputs.append(infer_input)
        
        output_names = list(set(o for req in requests for o in req.outputs))
        triton_outputs = [triton_grpc.InferRequestedOutput(name) for name in output_names]
        
        return await self.loop.run_in_executor(
            self.executor, client.infer, model_name, triton_inputs, 
            model_version=model_version or "", outputs=triton_outputs, 
            client_timeout=max(req.timeout_ms for req in requests) / 1000.0
        )

    async def _infer_http(self, client: triton_http.InferenceServerClient, model_name, model_version, input_tensors, requests):
        # ... (existing _infer_http logic, ensure it uses self.executor for client.infer)
        """
        Performs an asynchronous inference request to a Triton Inference Server using the HTTP client.
        
        Aggregates input tensors and requested outputs from a batch of inference requests, prepares them for Triton HTTP inference, and executes the inference call in a thread pool executor to avoid blocking the event loop.
        
        Args:
            client: The Triton HTTP InferenceServerClient instance.
            model_name: Name of the model to use for inference.
            model_version: Specific version of the model, or None for default.
            input_tensors: Dictionary mapping input names to lists of input values for the batch.
            requests: List of InferenceRequest objects representing the batched requests.
        
        Returns:
            The inference result returned by the Triton HTTP client.
        """
        triton_inputs = []
        for input_name, input_values in input_tensors.items():
            sample = input_values[0]
            if isinstance(sample, (bytes, str)): # String/bytes input
                if isinstance(sample, str): input_values = [val.encode('utf-8') for val in input_values]
                infer_input = triton_http.InferInput(input_name, [len(input_values)], "BYTES")
                infer_input.set_data_from_numpy(np.array(input_values, dtype=np.object_))
            else: # Numeric input
                batch_array = np.array(input_values)
                infer_input = triton_http.InferInput(input_name, [len(input_values)] + list(batch_array.shape[1:]), triton_http.utils.np_to_triton_dtype(batch_array.dtype))
                infer_input.set_data_from_numpy(batch_array)
            triton_inputs.append(infer_input)

        output_names = list(set(o for req in requests for o in req.outputs))
        triton_outputs = [triton_http.InferRequestedOutput(name) for name in output_names]

        return await self.loop.run_in_executor(
            self.executor, client.infer, model_name, triton_inputs, 
            model_version=model_version or "", outputs=triton_outputs, 
            client_timeout=max(req.timeout_ms for req in requests) / 1000.0
        )

    async def _process_inference_results(self, inference_result, batch: BatchedRequests, request_map: Dict[int, InferenceRequest], client_type: TritonClientType):
        # ... (existing logic, ensure _convert_numpy_to_json is called)
        # ... (and _publish_response is called)
        # Note: inference_result type depends on client (grpc.InferResult or http.InferResult)
        # as_numpy is available on both.
        """
        Processes inference results for a batch of requests and publishes responses.
        
        Extracts output tensors from the inference result, converts them to JSON-serializable formats, and sends a response for each request in the batch. If model context protocol (MCP) is enabled, updates the conversation context with the original input and primary model output.
        """
        output_data: Dict[str, Optional[np.ndarray]] = {}
        all_output_names = set().union(*(req.outputs for req in batch.requests))

        for output_name in all_output_names:
            try:
                output_data[output_name] = inference_result.as_numpy(output_name)
            except Exception as e: # Use generic triton client exception if available
                logger.error(f"Error extracting output {output_name}: {e}")
                output_data[output_name] = None

        for i, request in request_map.items():
            response_payload = {
                "request_id": request.request_id, "model_name": batch.model_name,
                "model_version": batch.model_version, "outputs": {},
                "timestamp": datetime.utcnow().isoformat(),
                "processing_time_ms": (time.time() - request.received_time) * 1000
            }
            for output_name_req in request.outputs:
                if output_data.get(output_name_req) is not None:
                    output_array = output_data[output_name_req]
                    if output_array is not None and len(output_array) > i : # Check length
                        response_payload["outputs"][output_name_req] = self._convert_numpy_to_json(output_array[i])
                    else:
                        logger.warning(f"Output index {i} out of bounds for output {output_name_req}")
                        response_payload["outputs"][output_name_req] = None
                else:
                    response_payload["outputs"][output_name_req] = None
            
            await self._publish_response(request, response_payload)

            # MCP Update
            if self.mcp_enabled and self.context_manager and request.context_id:
                # This assumes 'outputs' in response_payload is what's needed for context.
                # This might need adjustment based on actual model output structure.
                # For simplicity, let's assume the first output is the primary one for conversation.
                main_output_for_context = None
                if request.outputs and response_payload["outputs"].get(request.outputs[0]) is not None:
                     main_output_for_context = response_payload["outputs"][request.outputs[0]]
                elif response_payload["outputs"]: # Fallback to any output if specific not found
                     main_output_for_context = next(iter(response_payload["outputs"].values()), None)

                if main_output_for_context is not None:
                    original_input_text = request.raw_request.get("inputs", {}).get("text_input", "") # Or however original input is stored
                    await self.context_manager.update_context(
                        request.context_id,
                        original_input_text, # This should be the user's original input, not augmented one
                        main_output_for_context
                    )


    def _convert_numpy_to_json(self, data: Any) -> Any:
        # ... (existing logic)
        """
        Converts numpy arrays and scalar types to JSON-serializable Python objects.
        
        Supports conversion of numpy arrays (including byte and unicode types), numpy scalars, and bytes to formats compatible with JSON serialization. Non-numpy types are returned unchanged.
        """
        if isinstance(data, np.ndarray):
            if data.dtype.kind == 'S': return data.tobytes().decode('utf-8', errors='replace')
            if data.dtype.kind == 'U': return str(data)
            return data.tolist()
        if isinstance(data, (np.number, np.bool_)): return data.item()
        if isinstance(data, bytes): return data.decode('utf-8', errors='replace')
        return data

    async def _publish_response(self, request: InferenceRequest, response_data: Dict) -> None:
        """
        Publishes an inference response message to the appropriate RabbitMQ exchange and routing key.
        
        If the request specifies a `reply_to` property, the response is sent to the default exchange using that routing key; otherwise, the configured response exchange and routing key are used. The response is serialized as JSON and published with persistent delivery mode.
        """
        if not self.channel or self.channel.is_closed:
            logger.error("Cannot publish response, RabbitMQ channel is not available.")
            # Optionally, try to reconnect or queue internally, but for now, log and drop.
            return
        try:
            exchange_name = self.config['rabbitmq'].get('response_exchange', '') # Default to default exchange for direct reply-to
            routing_key_res = request.reply_to or self.config['rabbitmq'].get('response_routing_key', 'ai.inference.results')
            
            # If using reply_to, exchange_name should be empty for default exchange
            if request.reply_to:
                exchange_name = ''

            exchange = await self.channel.get_exchange(exchange_name, ensure=True) if exchange_name else self.channel.default_exchange

            message = aio_pika.Message(
                body=json.dumps(response_data).encode('utf-8'),
                correlation_id=request.correlation_id,
                content_type='application/json',
                delivery_mode=aio_pika.DeliveryMode.PERSISTENT
            )
            await exchange.publish(message, routing_key=routing_key_res)
            logger.debug(f"Published response for request {request.request_id} to {exchange_name}/{routing_key_res}")
        except Exception as e:
            logger.error(f"Failed to publish response for {request.request_id}: {e}", exc_info=True)

    async def _handle_batch_failure(self, batch: BatchedRequests, error_message: str):
        """
        Handles a failed batch by logging the error and sending an error response for each request in the batch.
        
        Each response includes the request ID, model information, error message, timestamp, and processing time.
        """
        logger.error(f"Batch failure for model {batch.model_name}: {error_message}")
        for request in batch.requests:
            error_response = {
                "request_id": request.request_id, "model_name": batch.model_name,
                "model_version": batch.model_version, "error": error_message,
                "timestamp": datetime.utcnow().isoformat(),
                "processing_time_ms": (time.time() - request.received_time) * 1000
            }
            await self._publish_response(request, error_response)

    def _parse_message(self, body: bytes, properties: aio_pika.spec.BasicProperties, routing_key: Optional[str]) -> InferenceRequest:
        # ... (adapt to use aio_pika properties if different, but structure is similar)
        """
        Parses a RabbitMQ message body and properties into an InferenceRequest object.
        
        Validates the presence of required fields and extracts optional context information if enabled. Raises an exception if the message is invalid.
        """
        try:
            message_dict = json.loads(body.decode('utf-8'))
            if 'model_name' not in message_dict or 'inputs' not in message_dict or not message_dict['inputs']:
                raise ValueError("Missing model_name or inputs in message")

            context_id = None
            if self.mcp_enabled and "context_id" in message_dict:
                context_id = message_dict["context_id"]
                if not isinstance(context_id, str): raise ValueError("Invalid context_id format")
            
            return InferenceRequest(
                request_id=message_dict.get('request_id', str(uuid.uuid4())),
                model_name=message_dict['model_name'],
                model_version=message_dict.get('model_version', ""),
                inputs=message_dict['inputs'],
                outputs=message_dict.get('outputs', []),
                priority=message_dict.get('priority', 0),
                timeout_ms=message_dict.get('timeout_ms', DEFAULT_INFERENCE_TIMEOUT_MS),
                correlation_id=properties.correlation_id,
                reply_to=properties.reply_to,
                routing_key=routing_key, # Store original routing key
                raw_request=message_dict,
                context_id=context_id
            )
        except Exception as e:
            logger.error(f"Error parsing message: {e}", exc_info=True)
            raise # Re-raise to be caught by on_message handler

    async def on_message(self, message: aio_pika.IncomingMessage) -> None:
        """
        Asynchronously processes an incoming RabbitMQ message for inference requests.
        
        Parses the message, validates its contents, and adds the resulting inference request to the appropriate batch. Acknowledges valid messages, rejects messages with validation errors, and negatively acknowledges messages with other errors without requeuing.
        """
        async with message.process(ignore_processed=True): # auto ack/nack context
            try:
                request = self._parse_message(message.body, message.properties, message.routing_key)
                await self.add_to_batch(request)
                await message.ack()
            except ValueError as e: # Specific error for parsing/validation
                logger.error(f"Message validation/parsing error: {e}. Rejecting message.")
                await message.reject(requeue=False) # Reject permanently
            except Exception as e:
                logger.error(f"Unhandled error processing message: {e}", exc_info=True)
                await message.nack(requeue=False) # Nack without requeue, or configure requeue policy

    async def start_consumers(self) -> None: # Renamed from start_consumer
        """
        Starts asynchronous consumers for all configured RabbitMQ queues.
        
        Initializes message consumption on each queue specified in the configuration, storing consumer tags for later management. Logs errors if any queue fails to start consuming and warns if no consumers are started.
        """
        if not self.channel:
            logger.error("Cannot start consumers, channel is not available.")
            return
        
        self._consuming_tags.clear() # Clear previous tags if reconnecting
        for q_conf in self.config['rabbitmq'].get('queues', []):
            queue_name = q_conf['name']
            try:
                queue = await self.channel.get_queue(queue_name) # Ensure queue exists (declared in connect)
                consumer_tag = await queue.consume(self.on_message)
                self._consuming_tags.append(consumer_tag)
                logger.info(f"Started consuming from queue: {queue_name} with tag: {consumer_tag}")
            except Exception as e:
                logger.error(f"Failed to start consuming from queue {queue_name}: {e}", exc_info=True)
        
        if not self._consuming_tags:
            logger.warning("No consumers were successfully started.")


    async def run(self):
        # Setup signal handlers for async context
        """
        Runs the main asynchronous event loop for the inference handler service.
        
        Initializes signal handlers for graceful shutdown, establishes and monitors the RabbitMQ connection, starts batch processing and message consumers, and manages reconnection logic. On shutdown, cancels batch processing, stops consumers, closes connections, and cleans up resources.
        """
        for sig in (signal.SIGINT, signal.SIGTERM):
            self.loop.add_signal_handler(sig, lambda s=sig: asyncio.create_task(self._signal_handler_async(s)))

        await self.connect_rabbitmq()
        if not self.channel:
            logger.error("Failed to establish RabbitMQ channel after connect. Exiting.")
            return

        batch_task = asyncio.create_task(self.process_batches())
        await self.start_consumers()

        while not self.should_exit:
            # Check RabbitMQ connection health periodically
            if not self.connection or self.connection.is_closed:
                logger.warning("RabbitMQ connection lost. Attempting to reconnect and restart consumers...")
                await self.connect_rabbitmq() # connect_robust handles retries
                if self.channel:
                    await self.start_consumers() # Re-subscribe consumers
                else:
                    logger.error("Failed to re-establish RabbitMQ channel. May not be consuming.")
            await asyncio.sleep(5) # Check every 5 seconds

        logger.info("Shutting down inference handler...")
        if batch_task:
            batch_task.cancel()
            try:
                await batch_task
            except asyncio.CancelledError:
                logger.info("Batch processing task cancelled.")
        
        if self.channel and not self.channel.is_closed:
            for tag in self._consuming_tags:
                try:
                    logger.info(f"Cancelling consumer: {tag}")
                    await self.channel.cancel(tag)
                except Exception as e:
                    logger.error(f"Error cancelling consumer {tag}: {e}")
        
        if self.connection and not self.connection.is_closed:
            await self.connection.close()
        
        self.executor.shutdown(wait=True)
        logger.info("Inference handler shutdown complete.")


async def main_async(): # Renamed for clarity
    """
    Asynchronously starts the inference handler service using the configuration file path from the environment or default.
    
    Initializes the InferenceHandler and runs its main event loop until shutdown.
    """
    config_path = os.environ.get("CONFIG_PATH", DEFAULT_CONFIG_PATH)
    handler = InferenceHandler(config_path=config_path) # Pass loop if needed by constructor
    await handler.run()

if __name__ == "__main__":
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        logger.info("Application interrupted by user.")
    except Exception as e:
        logger.critical(f"Application terminated with unhandled exception: {e}", exc_info=True)
    finally:
        logger.info("Application shutdown finalized.")

# Removed _start_consuming method as its pika-specific blocking logic is replaced by async consumer setup

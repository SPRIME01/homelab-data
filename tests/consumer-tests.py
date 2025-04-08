#!/usr/bin/env python3

'''
RabbitMQ Consumer Testing Framework

A comprehensive framework for testing RabbitMQ message consumers that provides:

Features:
- End-to-end testing of message processing pipelines
- Schema validation for messages
- Performance and reliability metrics collection
- Integration with monitoring and CI/CD tools

Test Coverage:
- Message processing correctness and data validation
- Error handling and dead-letter queue behavior
- Retry mechanisms and failure recovery
- Concurrent message processing
- Performance benchmarking (throughput, latency)

Execution Modes:
- Standalone pytest execution for local development
- ZenML pipeline integration for MLOps workflows
- Airflow DAG for scheduled production testing

Observability:
- Real-time Prometheus metrics
- JSON test reports for CI/CD integration
- Visual throughput and latency graphs
- Detailed logging of test execution

Configuration & Usage:
1. Configure test settings in config.yaml
2. Implement consumer(s) to process test queue messages
3. Run tests: python consumer-tests.py [--mode {pytest,zenml,all}]
4. Review results in output directory

Requirements:
- Python 3.7+
- RabbitMQ server
- Optional: ZenML, Airflow for pipeline integration
'''
import os
import sys
import json
import time
import uuid
import logging
import asyncio
import pytest
import yaml
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional, Callable, Generator
from dataclasses import dataclass, field
from pathlib import Path
from functools import wraps
import statistics
import tempfile

# Third-party imports
import pika
import aio_pika
from aio_pika.abc import AbstractIncomingMessage
from zenml.steps import step
from zenml.pipelines import pipeline
from airflow import DAG
from airflow.operators.python import PythonOperator
from prometheus_client import start_http_server, Counter, Histogram, Summary
import pandas as pd
import matplotlib.pyplot as plt

# Add parent directory to path for importing from other modules
sys.path.append(str(Path(__file__).parent.parent))
try:
    from schemas.validation import SchemaEnforcer
except ImportError:
    print("WARNING: Schema validation module not found. Schema validation will be disabled.")
    SchemaEnforcer = None

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('consumer_tests.log')
    ]
)
logger = logging.getLogger("consumer_tests")

# Prometheus metrics
MESSAGES_CONSUMED = Counter('test_messages_consumed_total', 'Number of test messages consumed')
MESSAGES_PROCESSED = Counter('test_messages_processed_total', 'Number of processed messages', ['status'])
PROCESSING_LATENCY = Histogram('test_processing_latency_seconds',
                               'Message processing latency',
                               buckets=[0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0])
CONSUMER_ERROR_RATE = Counter('test_consumer_errors_total', 'Number of consumer errors')
THROUGHPUT_GAUGE = Histogram('test_throughput_messages_per_second', 'Consumer throughput in messages per second')

@dataclass
class TestConfig:
    """Test configuration container."""
    rabbitmq: Dict[str, Any]
    schemas_dir: str = "schemas"
    test_data_dir: str = "test_data"
    output_dir: str = "test_results"
    prometheus_port: int = 9091
    parallelism: int = 4
    default_timeout: int = 10
    batch_sizes: List[int] = field(default_factory=lambda: [1, 10, 100, 1000])
    test_duration: int = 60  # seconds for long-running tests

@dataclass
class ConsumerTestCase:
    """Test case definition for a consumer."""
    name: str
    input_queue: str
    dead_letter_queue: Optional[str]
    output_queue: Optional[str]
    schema_id: Optional[str]
    message_generator: Callable
    message_count: int = 10
    expected_success_count: int = 10
    expected_error_count: int = 0
    expected_retry_count: Optional[int] = None
    timeout: int = 10
    properties: Dict[str, Any] = field(default_factory=dict)

@dataclass
class ConsumerTestResult:
    """Results from a consumer test."""
    success: bool
    messages_sent: int
    messages_processed: int
    messages_failed: int
    retried_messages: int
    processing_time: float
    throughput: float  # messages per second
    latency_stats: Dict[str, float]
    details: Dict[str, Any] = field(default_factory=dict)

def timed(f):
    """Decorator to measure function execution time."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        start_time = time.time()
        result = f(*args, **kwargs)
        execution_time = time.time() - start_time
        return result, execution_time
    return wrapper

class MessageConsumerTester:
    """Test framework for RabbitMQ message consumers."""

    def __init__(self, config_path: str = "config.yaml"):
        """Initialize with configuration."""
        self.config = self._load_config(config_path)
        self.connection = None
        self.channel = None
        self.schema_enforcer = None
        self._setup_directories()
        self._setup_schema_validation()

        # Start Prometheus metrics server if enabled
        if self.config.prometheus_port:
            try:
                start_http_server(self.config.prometheus_port)
                logger.info(f"Started Prometheus metrics server on port {self.config.prometheus_port}")
            except Exception as e:
                logger.warning(f"Failed to start Prometheus metrics server: {e}")

    def _load_config(self, config_path: str) -> TestConfig:
        """Load test configuration from YAML file."""
        try:
            if not Path(config_path).exists():
                logger.warning(f"Config file {config_path} not found, using default configuration")
                return TestConfig(
                    rabbitmq={
                        "host": "localhost",
                        "port": 5672,
                        "vhost": "/",
                        "username": "guest",
                        "password": "guest"
                    }
                )

            with open(config_path, 'r') as f:
                config_data = yaml.safe_load(f)

            return TestConfig(**config_data)

        except Exception as e:
            logger.error(f"Error loading configuration: {e}")
            raise

    def _setup_directories(self) -> None:
        """Create needed directories."""
        os.makedirs(self.config.output_dir, exist_ok=True)
        os.makedirs(self.config.test_data_dir, exist_ok=True)

    def _setup_schema_validation(self) -> None:
        """Initialize schema validation if available."""
        if SchemaEnforcer is not None:
            try:
                self.schema_enforcer = SchemaEnforcer(schemas_dir=self.config.schemas_dir)
                logger.info(f"Schema validation enabled with schemas from {self.config.schemas_dir}")
            except Exception as e:
                logger.error(f"Failed to initialize schema validation: {e}")
                self.schema_enforcer = None
        else:
            logger.warning("Schema validation module not available")

    async def connect(self) -> None:
        """Establish connection to RabbitMQ."""
        try:
            self.connection = await aio_pika.connect_robust(
                host=self.config.rabbitmq["host"],
                port=self.config.rabbitmq["port"],
                login=self.config.rabbitmq["username"],
                password=self.config.rabbitmq["password"],
                virtualhost=self.config.rabbitmq.get("vhost", "/")
            )
            self.channel = await self.connection.channel()
            await self.channel.set_qos(prefetch_count=self.config.parallelism)
            logger.info("Connected to RabbitMQ")
        except Exception as e:
            logger.error(f"Failed to connect to RabbitMQ: {e}")
            raise

    async def close(self) -> None:
        """Close connection to RabbitMQ."""
        if self.connection and not self.connection.is_closed:
            await self.connection.close()
            logger.info("Closed RabbitMQ connection")

    async def declare_test_queue(self, queue_name: str, dead_letter_exchange: Optional[str] = None) -> aio_pika.Queue:
        """Declare a test queue."""
        try:
            queue_args = {}
            if dead_letter_exchange:
                queue_args["x-dead-letter-exchange"] = dead_letter_exchange
                queue_args["x-dead-letter-routing-key"] = f"{queue_name}.dlq"

            queue = await self.channel.declare_queue(
                queue_name,
                durable=True,
                arguments=queue_args
            )
            logger.info(f"Declared queue {queue_name}")
            return queue
        except Exception as e:
            logger.error(f"Failed to declare queue {queue_name}: {e}")
            raise

    async def purge_queue(self, queue_name: str) -> None:
        """Purge a queue to ensure a clean test environment."""
        try:
            queue = await self.channel.declare_queue(queue_name, passive=True)
            await queue.purge()
            logger.info(f"Purged queue {queue_name}")
        except Exception as e:
            logger.error(f"Failed to purge queue {queue_name}: {e}")

    async def run_test_case(self, test_case: ConsumerTestCase) -> ConsumerTestResult:
        """Run a single consumer test case."""
        if not self.connection or self.connection.is_closed:
            await self.connect()

        # Set up collection of results
        messages_processed = 0
        messages_failed = 0
        retried_messages = 0
        processed_messages = {}
        latencies = []
        start_time = time.time()

        # Declare and purge test queues
        await self.purge_queue(test_case.input_queue)
        input_queue = await self.declare_test_queue(test_case.input_queue)

        if test_case.output_queue:
            await self.purge_queue(test_case.output_queue)
            output_queue = await self.declare_test_queue(test_case.output_queue)

        if test_case.dead_letter_queue:
            await self.purge_queue(test_case.dead_letter_queue)
            dlq_queue = await self.declare_test_queue(test_case.dead_letter_queue)

        # Set up message tracking and result future
        result_future = asyncio.Future()
        test_correlation_id = f"test-{uuid.uuid4().hex}"

        # Set up consumer for output queue if specified
        if test_case.output_queue:
            async def on_output_message(message: AbstractIncomingMessage) -> None:
                nonlocal messages_processed
                async with message.process():
                    body = message.body
                    try:
                        data = json.loads(body.decode())
                        message_id = data.get('id', message.message_id)
                        correlation_id = message.correlation_id.decode() if message.correlation_id else None

                        if correlation_id == test_correlation_id:
                            messages_processed += 1
                            processed_messages[message_id] = data
                            MESSAGES_PROCESSED.labels(status="success").inc()

                            # Calculate processing latency if timestamp is available
                            if 'sent_timestamp' in data:
                                latency = time.time() - data['sent_timestamp']
                                latencies.append(latency)
                                PROCESSING_LATENCY.observe(latency)

                            if messages_processed >= test_case.expected_success_count:
                                if not result_future.done():
                                    result_future.set_result(True)
                    except Exception as e:
                        logger.error(f"Error processing output message: {e}")

            output_consumer = await output_queue.consume(on_output_message)

        # Set up consumer for dead-letter queue if specified
        if test_case.dead_letter_queue:
            async def on_dlq_message(message: AbstractIncomingMessage) -> None:
                nonlocal messages_failed
                async with message.process():
                    body = message.body
                    try:
                        data = json.loads(body.decode())
                        correlation_id = message.correlation_id.decode() if message.correlation_id else None

                        if correlation_id == test_correlation_id:
                            messages_failed += 1
                            MESSAGES_PROCESSED.labels(status="error").inc()

                            if messages_failed >= test_case.expected_error_count:
                                if test_case.expected_success_count == 0 and not result_future.done():
                                    result_future.set_result(True)
                    except Exception as e:
                        logger.error(f"Error processing DLQ message: {e}")

            dlq_consumer = await dlq_queue.consume(on_dlq_message)

        # Generate and publish test messages
        messages_sent = 0
        try:
            for i in range(test_case.message_count):
                # Generate a test message
                message_data = test_case.message_generator(i)
                message_id = message_data.get('id', f"msg-{i}-{uuid.uuid4().hex}")
                message_data['id'] = message_id
                message_data['sent_timestamp'] = time.time()

                # Validate against schema if specified
                if test_case.schema_id and self.schema_enforcer:
                    try:
                        message_data = self.schema_enforcer.validate_message(
                            message_data, test_case.schema_id
                        )
                    except Exception as e:
                        logger.error(f"Validation error for message {i}: {e}")
                        continue

                # Create message properties
                headers = {"test_case": test_case.name, **test_case.properties.get('headers', {})}

                # Publish the message
                try:
                    await self.channel.default_exchange.publish(
                        aio_pika.Message(
                            body=json.dumps(message_data).encode(),
                            content_type="application/json",
                            correlation_id=test_correlation_id,
                            message_id=message_id,
                            headers=headers
                        ),
                        routing_key=test_case.input_queue
                    )

                    messages_sent += 1
                    MESSAGES_CONSUMED.inc()

                except Exception as e:
                    logger.error(f"Error publishing message {i}: {e}")
                    CONSUMER_ERROR_RATE.inc()

                # Small delay to avoid overwhelming the broker
                await asyncio.sleep(0.001)

        except Exception as e:
            logger.error(f"Error in test case {test_case.name}: {e}")
            CONSUMER_ERROR_RATE.inc()

        # Wait for expected messages or timeout
        try:
            await asyncio.wait_for(result_future, timeout=test_case.timeout)
        except asyncio.TimeoutError:
            logger.warning(f"Timeout waiting for message processing in test {test_case.name}")

        # Calculate test metrics
        end_time = time.time()
        test_duration = end_time - start_time
        throughput = messages_processed / test_duration if test_duration > 0 else 0
        THROUGHPUT_GAUGE.observe(throughput)

        # Calculate latency statistics
        latency_stats = {
            "min": min(latencies) if latencies else 0,
            "max": max(latencies) if latencies else 0,
            "avg": statistics.mean(latencies) if latencies else 0,
            "p50": statistics.median(latencies) if len(latencies) >= 2 else 0,
            "p95": statistics.quantiles(latencies, n=20)[19] if len(latencies) >= 20 else 0,
            "p99": statistics.quantiles(latencies, n=100)[99] if len(latencies) >= 100 else 0
        }

        # Cancel consumers
        if test_case.output_queue:
            await output_consumer.cancel()

        if test_case.dead_letter_queue:
            await dlq_consumer.cancel()

        # Determine test success
        success = (
            messages_processed >= test_case.expected_success_count and
            messages_failed == test_case.expected_error_count and
            (test_case.expected_retry_count is None or retried_messages == test_case.expected_retry_count)
        )

        # Return test results
        return ConsumerTestResult(
            success=success,
            messages_sent=messages_sent,
            messages_processed=messages_processed,
            messages_failed=messages_failed,
            retried_messages=retried_messages,
            processing_time=test_duration,
            throughput=throughput,
            latency_stats=latency_stats,
            details={
                "test_case": test_case.name,
                "input_queue": test_case.input_queue,
                "output_queue": test_case.output_queue,
                "dead_letter_queue": test_case.dead_letter_queue,
                "processed_messages": processed_messages
            }
        )

    @step
    def generate_sensor_data(self, index: int) -> Dict[str, Any]:
        """Generate sample sensor data."""
        return {
            "sensor_id": f"test_sensor_{index % 10}",
            "type": "temperature",
            "value": 20.0 + (index % 10),
            "unit": "C",
            "timestamp": datetime.utcnow().isoformat(),
            "location": {
                "room": f"room_{index % 5}",
                "floor": f"floor_{index % 3}"
            },
            "test_id": str(uuid.uuid4())
        }

    @step
    def generate_metrics_data(self, index: int) -> Dict[str, Any]:
        """Generate sample metrics data."""
        return {
            "metric_name": f"test_metric_{index % 5}",
            "value": index * 1.5,
            "timestamp": datetime.utcnow().isoformat(),
            "host": f"host_{index % 3}",
            "component": f"component_{index % 4}",
            "type": "gauge",
            "test_id": str(uuid.uuid4())
        }

    @step
    def generate_error_data(self, index: int) -> Dict[str, Any]:
        """Generate data that will cause consumer errors."""
        if index % 3 == 0:
            # Malformed data missing required fields
            return {
                "partial_data": True,
                "timestamp": datetime.utcnow().isoformat()
            }
        else:
            # Valid data
            return self.generate_sensor_data(index)

    @step
    def run_processing_correctness_test(self) -> ConsumerTestResult:
        """Test message processing correctness."""
        test_case = ConsumerTestCase(
            name="processing_correctness",
            input_queue="test.correctness.input",
            output_queue="test.correctness.output",
            dead_letter_queue=None,
            schema_id="sensor-reading",
            message_generator=self.generate_sensor_data,
            message_count=10,
            expected_success_count=10,
            timeout=30
        )

        loop = asyncio.get_event_loop()
        return loop.run_until_complete(self.run_test_case(test_case))

    @step
    def run_error_handling_test(self) -> ConsumerTestResult:
        """Test consumer error handling."""
        test_case = ConsumerTestCase(
            name="error_handling",
            input_queue="test.error.input",
            output_queue="test.error.output",
            dead_letter_queue="test.error.dlq",
            schema_id="sensor-reading",
            message_generator=self.generate_error_data,
            message_count=10,
            expected_success_count=6,  # Valid messages should be processed
            expected_error_count=4,    # Invalid messages should go to DLQ
            timeout=30
        )

        loop = asyncio.get_event_loop()
        return loop.run_until_complete(self.run_test_case(test_case))

    @step
    def run_retry_behavior_test(self) -> ConsumerTestResult:
        """Test retry behavior handling."""
        # For retry behavior, we need to simulate a temporary failure
        # This requires a custom consumer mock setup which would be part of
        # the test environment, not this test framework directly

        test_case = ConsumerTestCase(
            name="retry_behavior",
            input_queue="test.retry.input",
            output_queue="test.retry.output",
            dead_letter_queue="test.retry.dlq",
            schema_id="sensor-reading",
            message_generator=self.generate_sensor_data,
            message_count=10,
            expected_success_count=8,    # Eventually succeed
            expected_error_count=2,      # Some permanently fail
            expected_retry_count=4,      # Some messages get retried
            timeout=45  # Longer timeout for retry tests
        )

        loop = asyncio.get_event_loop()
        return loop.run_until_complete(self.run_test_case(test_case))

    @step
    def run_performance_test(self, batch_size: int) -> ConsumerTestResult:
        """Test consumer performance with different batch sizes."""
        test_case = ConsumerTestCase(
            name=f"performance_batch_{batch_size}",
            input_queue=f"test.performance.input.{batch_size}",
            output_queue=f"test.performance.output.{batch_size}",
            dead_letter_queue=None,
            schema_id="metrics-data",
            message_generator=self.generate_metrics_data,
            message_count=batch_size,
            expected_success_count=batch_size,
            timeout=max(60, batch_size // 20)  # Scale timeout with batch size
        )

        loop = asyncio.get_event_loop()
        return loop.run_until_complete(self.run_test_case(test_case))

    @step
    def run_concurrency_test(self) -> ConsumerTestResult:
        """Test concurrent message consumption."""
        # To test concurrency, we publish messages very quickly and verify
        # that multiple messages are processed in parallel

        test_case = ConsumerTestCase(
            name="concurrency_test",
            input_queue="test.concurrency.input",
            output_queue="test.concurrency.output",
            dead_letter_queue=None,
            schema_id=None,
            message_generator=self.generate_metrics_data,
            message_count=50,
            expected_success_count=50,
            timeout=30
        )

        loop = asyncio.get_event_loop()
        return loop.run_until_complete(self.run_test_case(test_case))

    @step
    def generate_test_report(self, results: List[ConsumerTestResult]) -> Dict[str, Any]:
        """Generate a test report from multiple test results."""
        # Create a test report
        report = {
            "timestamp": datetime.utcnow().isoformat(),
            "overall_success": all(r.success for r in results),
            "tests_passed": sum(1 for r in results if r.success),
            "tests_failed": sum(1 for r in results if not r.success),
            "total_messages_sent": sum(r.messages_sent for r in results),
            "total_messages_processed": sum(r.messages_processed for r in results),
            "total_messages_failed": sum(r.messages_failed for r in results),
            "average_throughput": statistics.mean([r.throughput for r in results]),
            "test_results": [self._result_to_dict(r) for r in results]
        }

        # Save report to file
        report_path = os.path.join(self.config.output_dir, f"consumer_test_report_{int(time.time())}.json")
        with open(report_path, 'w') as f:
            json.dump(report, f, indent=2)

        # Generate visualizations
        self._generate_visualizations(results)

        return report

    def _result_to_dict(self, result: ConsumerTestResult) -> Dict[str, Any]:
        """Convert test result to dictionary."""
        return {
            "name": result.details.get("test_case"),
            "success": result.success,
            "messages_sent": result.messages_sent,
            "messages_processed": result.messages_processed,
            "messages_failed": result.messages_failed,
            "retried_messages": result.retried_messages,
            "processing_time": result.processing_time,
            "throughput": result.throughput,
            "latency_stats": result.latency_stats,
            "details": result.details
        }

    def _generate_visualizations(self, results: List[ConsumerTestResult]) -> None:
        """Generate visualizations from test results."""
        # Prepare data for plotting
        df = pd.DataFrame([self._result_to_dict(r) for r in results])

        # Plot throughput comparison
        plt.figure(figsize=(10, 6))
        bars = plt.bar(df['name'], df['throughput'], color='skyblue')

        # Add value labels on top of bars
        for bar in bars:
            height = bar.get_height()
            plt.text(bar.get_x() + bar.get_width()/2., height + 0.1,
                    f'{height:.1f}',
                    ha='center', va='bottom', rotation=0)

        plt.title('Message Processing Throughput by Test')
        plt.xlabel('Test Case')
        plt.ylabel('Messages per Second')
        plt.xticks(rotation=45, ha='right')
        plt.tight_layout()
        plt.savefig(os.path.join(self.config.output_dir, 'throughput_comparison.png'))

        # Plot latency stats
        if len(results) > 0 and all('latency_stats' in self._result_to_dict(r) for r in results):
            plt.figure(figsize=(12, 8))

            test_names = [r.details.get("test_case") for r in results]
            avg_latencies = [r.latency_stats['avg'] * 1000 for r in results]  # Convert to ms
            p95_latencies = [r.latency_stats['p95'] * 1000 for r in results]  # Convert to ms
            p99_latencies = [r.latency_stats['p99'] * 1000 for r in results]  # Convert to ms

            x = range(len(test_names))
            width = 0.25

            plt.bar([i - width for i in x], avg_latencies, width, label='Avg Latency', color='skyblue')
            plt.bar(x, p95_latencies, width, label='95th Percentile', color='orange')
            plt.bar([i + width for i in x], p99_latencies, width, label='99th Percentile', color='salmon')

            plt.title('Message Processing Latency by Test')
            plt.xlabel('Test Case')
            plt.ylabel('Latency (ms)')
            plt.xticks(x, test_names, rotation=45, ha='right')
            plt.legend()
            plt.tight_layout()
            plt.savefig(os.path.join(self.config.output_dir, 'latency_comparison.png'))

@pipeline
def consumer_test_pipeline() -> Dict[str, Any]:
    """ZenML pipeline for consumer testing."""
    tester = MessageConsumerTester()

    # Run individual test steps
    correctness_result = tester.run_processing_correctness_test()
    error_handling_result = tester.run_error_handling_test()
    retry_behavior_result = tester.run_retry_behavior_test()
    concurrency_result = tester.run_concurrency_test()

    # Run performance tests with different batch sizes
    performance_results = []
    for batch_size in [10, 100, 1000]:
        result = tester.run_performance_test(batch_size)
        performance_results.append(result)

    # Combine all results
    all_results = [
        correctness_result,
        error_handling_result,
        retry_behavior_result,
        concurrency_result
    ] + performance_results

    # Generate report
    report = tester.generate_test_report(all_results)
    return report

def create_airflow_dag():
    """Create an Airflow DAG for scheduled consumer testing."""
    from airflow import DAG
    from airflow.operators.python import PythonOperator
    from datetime import datetime, timedelta

    default_args = {
        'owner': 'homelab',
        'depends_on_past': False,
        'start_date': datetime(2023, 1, 1),
        'email_on_failure': True,
        'email_on_retry': False,
        'retries': 1,
        'retry_delay': timedelta(minutes=5),
    }

    dag = DAG(
        'consumer_tests',
        default_args=default_args,
        description='RabbitMQ Consumer Tests',
        schedule_interval=timedelta(days=1),
        catchup=False
    )

    def run_test_suite(**kwargs):
        from zenml.client import Client

        # Initialize ZenML
        client = Client()

        # Run the pipeline
        pipeline_instance = consumer_test_pipeline()
        pipeline_instance.run()

        return "Tests completed successfully"

    test_task = PythonOperator(
        task_id='run_consumer_tests',
        python_callable=run_test_suite,
        dag=dag,
    )

    return dag

# Create Airflow DAG if Airflow is available
try:
    dag = create_airflow_dag()
except ImportError:
    logger.warning("Airflow not available. DAG not created.")

# Pytest fixtures and tests
@pytest.fixture(scope="session")
def consumer_tester():
    """Fixture for the consumer tester."""
    tester = MessageConsumerTester()
    yield tester
    # Cleanup resources
    if tester.connection:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            loop.create_task(tester.close())
        else:
            loop.run_until_complete(tester.close())

@pytest.fixture(scope="session")
def event_loop():
    """Custom event loop for async tests."""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()

@pytest.mark.asyncio
async def test_processing_correctness(consumer_tester):
    """Test basic message processing correctness."""
    # Connect to RabbitMQ
    await consumer_tester.connect()

    # Run test case
    test_case = ConsumerTestCase(
        name="processing_correctness",
        input_queue="test.correctness.input",
        output_queue="test.correctness.output",
        dead_letter_queue=None,
        schema_id="sensor-reading",
        message_generator=consumer_tester.generate_sensor_data,
        message_count=10,
        expected_success_count=10,
        timeout=30
    )

    result = await consumer_tester.run_test_case(test_case)

    # Assertions
    assert result.success, f"Processing correctness test failed: {result}"
    assert result.messages_sent == test_case.message_count
    assert result.messages_processed >= test_case.expected_success_count
    assert result.messages_failed == 0

@pytest.mark.asyncio
async def test_error_handling(consumer_tester):
    """Test error handling with invalid messages."""
    # Connect to RabbitMQ
    await consumer_tester.connect()

    # Run test case
    test_case = ConsumerTestCase(
        name="error_handling",
        input_queue="test.error.input",
        output_queue="test.error.output",
        dead_letter_queue="test.error.dlq",
        schema_id="sensor-reading",
        message_generator=consumer_tester.generate_error_data,
        message_count=10,
        expected_success_count=6,
        expected_error_count=4,
        timeout=30
    )

    result = await consumer_tester.run_test_case(test_case)

    # Assertions
    assert result.success, f"Error handling test failed: {result}"
    assert result.messages_sent == test_case.message_count
    assert result.messages_processed >= test_case.expected_success_count
    assert result.messages_failed == test_case.expected_error_count

@pytest.mark.asyncio
async def test_performance(consumer_tester):
    """Test consumer performance."""
    # Connect to RabbitMQ
    await consumer_tester.connect()

    # Run test case with smaller batch for tests
    batch_size = 50
    test_case = ConsumerTestCase(
        name=f"performance_batch_{batch_size}",
        input_queue=f"test.performance.input.{batch_size}",
        output_queue=f"test.performance.output.{batch_size}",
        dead_letter_queue=None,
        schema_id="metrics-data",
        message_generator=consumer_tester.generate_metrics_data,
        message_count=batch_size,
        expected_success_count=batch_size,
        timeout=60
    )

    result = await consumer_tester.run_test_case(test_case)

    # Assertions
    assert result.success, f"Performance test failed: {result}"
    assert result.messages_sent == test_case.message_count
    assert result.messages_processed >= test_case.expected_success_count
    assert result.throughput > 0, "Throughput should be greater than 0"

    # Additional assertions for performance characteristics
    assert result.latency_stats["avg"] < 1.0, "Average latency should be less than 1 second"

@pytest.mark.asyncio
async def test_concurrency(consumer_tester):
    """Test concurrent message processing."""
    # Connect to RabbitMQ
    await consumer_tester.connect()

    # Run test case
    test_case = ConsumerTestCase(
        name="concurrency_test",
        input_queue="test.concurrency.input",
        output_queue="test.concurrency.output",
        dead_letter_queue=None,
        schema_id=None,
        message_generator=consumer_tester.generate_metrics_data,
        message_count=50,
        expected_success_count=50,
        timeout=30
    )

    result = await consumer_tester.run_test_case(test_case)

    # Assertions
    assert result.success, f"Concurrency test failed: {result}"
    assert result.messages_sent == test_case.message_count
    assert result.messages_processed >= test_case.expected_success_count

    # Check that the throughput is reasonable for concurrent processing
    # This assumes your consumer is actually processing messages concurrently
    assert result.throughput > 5, "Throughput too low for concurrent processing"

def main():
    """Main entry point for running test suite directly."""
    import argparse

    parser = argparse.ArgumentParser(description='RabbitMQ Consumer Tests')
    parser.add_argument('--config', '-c', type=str, default='config.yaml',
                       help='Path to configuration YAML file')
    parser.add_argument('--output', '-o', type=str, default='test_results',
                       help='Directory to store test results')
    parser.add_argument('--mode', '-m', type=str, choices=['pytest', 'zenml', 'all'],
                       default='all', help='Test execution mode')
    args = parser.parse_args()

    # Update config paths based on command line args
    if args.output:
        os.makedirs(args.output, exist_ok=True)

    # Run in appropriate mode
    if args.mode in ('pytest', 'all'):
        # Run with pytest
        pytest_args = [
            __file__,
            '-v',
            '--html=report.html',
            '--self-contained-html'
        ]
        pytest.main(pytest_args)

    if args.mode in ('zenml', 'all'):
        try:
            # Run with ZenML pipeline
            pipeline_instance = consumer_test_pipeline()
            pipeline_instance.run()
        except Exception as e:
            logger.error(f"Error running ZenML pipeline: {e}")

if __name__ == "__main__":
    main()

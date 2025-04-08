#!/usr/bin/env python3

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

# Third-party imports
import pika
import aio_pika
import jsonschema
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
        logging.FileHandler('producer_tests.log')
    ]
)
logger = logging.getLogger("producer_tests")

# Prometheus metrics
MESSAGES_PUBLISHED = Counter('test_messages_published_total', 'Number of test messages published')
MESSAGES_CONFIRMED = Counter('test_messages_confirmed_total', 'Number of published messages confirmed')
PUBLISHING_LATENCY = Histogram('test_publishing_latency_seconds',
                             'Message publishing latency', buckets=[0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0])
VALIDATION_ERRORS = Counter('test_validation_errors_total', 'Number of message validation errors')
PRODUCER_ERROR_RATE = Counter('test_producer_errors_total', 'Number of producer errors')

@dataclass
class TestConfig:
    """Test configuration container."""
    rabbitmq: Dict[str, Any]
    schemas_dir: str = "schemas"
    test_data_dir: str = "test_data"
    output_dir: str = "test_results"
    prometheus_port: int = 9090
    parallelism: int = 4
    default_timeout: int = 10
    batch_sizes: List[int] = field(default_factory=lambda: [1, 10, 100, 1000])
    test_duration: int = 60  # seconds for long-running tests

@dataclass
class ProducerTestCase:
    """Test case definition for a producer."""
    name: str
    exchange: str
    routing_key: str
    schema_id: Optional[str]
    message_generator: Callable
    message_count: int = 10
    expected_confirms: int = 10
    timeout: int = 10
    properties: Dict[str, Any] = field(default_factory=dict)
    expected_errors: int = 0

@dataclass
class ProducerTestResult:
    """Results from a producer test."""
    success: bool
    messages_sent: int
    messages_confirmed: int
    validation_errors: int
    producer_errors: int
    latency_stats: Dict[str, float]
    throughput: float  # messages per second
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

class MessageProducerTester:
    """Test framework for RabbitMQ message producers."""

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
            self.connection = await aio_pika.connect(
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

    async def declare_test_exchange(self, exchange_name: str, exchange_type: str = "topic") -> None:
        """Declare a test exchange."""
        try:
            await self.channel.declare_exchange(
                name=exchange_name,
                type=exchange_type,
                durable=True
            )
            logger.info(f"Declared exchange {exchange_name} of type {exchange_type}")
        except Exception as e:
            logger.error(f"Failed to declare exchange {exchange_name}: {e}")
            raise

    async def run_test_case(self, test_case: ProducerTestCase) -> ProducerTestResult:
        """Run a single producer test case."""
        if not self.connection or self.connection.is_closed:
            await self.connect()

        # Set up collection of results
        confirms_received = 0
        validation_errors = 0
        producer_errors = 0
        latencies = []
        start_time = time.time()

        # Declare the test exchange
        await self.declare_test_exchange(test_case.exchange)

        # Create a temporary queue to verify message delivery
        result_queue = await self.channel.declare_queue(
            name=f"test_queue_{uuid.uuid4().hex}",
            auto_delete=True
        )
        await result_queue.bind(
            exchange=test_case.exchange,
            routing_key=test_case.routing_key
        )

        # Set up a confirmation callback for the consumer
        confirmation_future = asyncio.get_event_loop().create_future()
        received_messages = []

        async def on_message(message: aio_pika.IncomingMessage) -> None:
            nonlocal confirms_received, received_messages
            async with message.process():
                confirms_received += 1
                received_messages.append(message)
                MESSAGES_CONFIRMED.inc()
                if confirms_received >= test_case.expected_confirms:
                    if not confirmation_future.done():
                        confirmation_future.set_result(True)

        # Start consuming messages
        await result_queue.consume(on_message)

        # Generate and publish test messages
        messages_sent = 0
        try:
            for i in range(test_case.message_count):
                # Generate a test message
                message_data = test_case.message_generator(i)

                # Validate against schema if specified
                if test_case.schema_id and self.schema_enforcer:
                    try:
                        message_data = self.schema_enforcer.validate_message(
                            message_data, test_case.schema_id
                        )
                    except Exception as e:
                        logger.error(f"Validation error for message {i}: {e}")
                        validation_errors += 1
                        VALIDATION_ERRORS.inc()
                        continue

                # Create message properties
                props = {
                    "message_id": f"{test_case.name}_{i}_{int(time.time())}",
                    "timestamp": int(time.time()),
                    **test_case.properties
                }

                # Publish the message
                try:
                    message = aio_pika.Message(
                        body=json.dumps(message_data).encode(),
                        content_type="application/json",
                        content_encoding="utf-8",
                        **props
                    )

                    publish_start = time.time()
                    await self.channel.default_exchange.publish(
                        message,
                        routing_key=f"{test_case.exchange}.{test_case.routing_key}"
                    )
                    latency = time.time() - publish_start
                    latencies.append(latency)
                    PUBLISHING_LATENCY.observe(latency)

                    messages_sent += 1
                    MESSAGES_PUBLISHED.inc()

                except Exception as e:
                    logger.error(f"Error publishing message {i}: {e}")
                    producer_errors += 1
                    PRODUCER_ERROR_RATE.inc()

                # Small delay to avoid overwhelming the broker
                await asyncio.sleep(0.001)

        except Exception as e:
            logger.error(f"Error in test case {test_case.name}: {e}")
            producer_errors += 1
            PRODUCER_ERROR_RATE.inc()

        # Wait for confirmations or timeout
        try:
            await asyncio.wait_for(confirmation_future, timeout=test_case.timeout)
        except asyncio.TimeoutError:
            logger.warning(f"Timeout waiting for message confirmations in test {test_case.name}")

        # Calculate test metrics
        end_time = time.time()
        test_duration = end_time - start_time
        throughput = messages_sent / test_duration if messages_sent > 0 else 0

        # Calculate latency statistics
        latency_stats = {
            "min": min(latencies) if latencies else 0,
            "max": max(latencies) if latencies else 0,
            "avg": statistics.mean(latencies) if latencies else 0,
            "p50": statistics.median(latencies) if latencies else 0,
            "p95": statistics.quantiles(latencies, n=20)[19] if len(latencies) >= 20 else 0,
            "p99": statistics.quantiles(latencies, n=100)[99] if len(latencies) >= 100 else 0
        }

        # Determine test success
        success = (
            messages_sent >= test_case.message_count and
            confirms_received >= test_case.expected_confirms and
            validation_errors <= test_case.expected_errors and
            producer_errors == 0
        )

        # Delete the temporary queue
        await result_queue.delete()

        # Return test results
        return ProducerTestResult(
            success=success,
            messages_sent=messages_sent,
            messages_confirmed=confirms_received,
            validation_errors=validation_errors,
            producer_errors=producer_errors,
            latency_stats=latency_stats,
            throughput=throughput,
            details={
                "test_case": test_case.name,
                "exchange": test_case.exchange,
                "routing_key": test_case.routing_key,
                "schema_id": test_case.schema_id,
                "duration_seconds": test_duration
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
    def generate_event_data(self, index: int) -> Dict[str, Any]:
        """Generate sample event data."""
        return {
            "event_type": "state_changed",
            "entity_id": f"light.room_{index % 5}",
            "to_state": "on" if index % 2 == 0 else "off",
            "from_state": "off" if index % 2 == 0 else "on",
            "timestamp": datetime.utcnow().isoformat(),
            "context": {
                "id": str(uuid.uuid4()),
                "parent_id": None,
                "user_id": f"user_{index % 3}"
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
    def run_message_format_validation_test(self) -> ProducerTestResult:
        """Test message format validation."""
        test_case = ProducerTestCase(
            name="message_format_validation",
            exchange="test.format.validation",
            routing_key="sensor.data",
            schema_id="sensor-reading",
            message_generator=self.generate_sensor_data,
            message_count=10,
            expected_confirms=10,
            timeout=5
        )

        loop = asyncio.get_event_loop()
        return loop.run_until_complete(self.run_test_case(test_case))

    @step
    def run_publishing_reliability_test(self) -> ProducerTestResult:
        """Test publishing reliability with a larger message volume."""
        test_case = ProducerTestCase(
            name="publishing_reliability",
            exchange="test.reliability",
            routing_key="events.#",
            schema_id="device-state-change",
            message_generator=self.generate_event_data,
            message_count=100,
            expected_confirms=95,  # Allow for some message loss
            timeout=15
        )

        loop = asyncio.get_event_loop()
        return loop.run_until_complete(self.run_test_case(test_case))

    @step
    def run_error_handling_test(self) -> ProducerTestResult:
        """Test error handling with invalid messages."""
        # A message generator that produces invalid data every 3rd message
        def generate_invalid_sensor_data(index: int) -> Dict[str, Any]:
            if index % 3 == 0:
                # Invalid data - missing required fields
                return {
                    "sensor_id": f"test_sensor_{index}",
                    # Missing value and timestamp fields
                    "test_id": str(uuid.uuid4())
                }
            else:
                return self.generate_sensor_data(index)

        test_case = ProducerTestCase(
            name="error_handling",
            exchange="test.error.handling",
            routing_key="sensor.data",
            schema_id="sensor-reading",
            message_generator=generate_invalid_sensor_data,
            message_count=15,
            expected_confirms=10,  # Expect only valid messages
            expected_errors=5,     # Expect 5 validation errors
            timeout=10
        )

        loop = asyncio.get_event_loop()
        return loop.run_until_complete(self.run_test_case(test_case))

    @step
    def run_performance_test(self, batch_size: int) -> ProducerTestResult:
        """Test producer performance with different batch sizes."""
        test_case = ProducerTestCase(
            name=f"performance_test_batch_{batch_size}",
            exchange="test.performance",
            routing_key="metrics.#",
            schema_id="system-metric",
            message_generator=self.generate_metrics_data,
            message_count=batch_size,
            expected_confirms=int(batch_size * 0.95),  # Allow for some message loss
            timeout=max(30, batch_size // 50)  # Adjust timeout based on batch size
        )

        loop = asyncio.get_event_loop()
        return loop.run_until_complete(self.run_test_case(test_case))

    @step
    def generate_test_report(self, results: List[ProducerTestResult]) -> Dict[str, Any]:
        """Generate a test report from multiple test results."""
        # Create a test report
        report = {
            "timestamp": datetime.utcnow().isoformat(),
            "overall_success": all(r.success for r in results),
            "tests_passed": sum(1 for r in results if r.success),
            "tests_failed": sum(1 for r in results if not r.success),
            "total_messages_sent": sum(r.messages_sent for r in results),
            "total_messages_confirmed": sum(r.messages_confirmed for r in results),
            "total_validation_errors": sum(r.validation_errors for r in results),
            "total_producer_errors": sum(r.producer_errors for r in results),
            "average_throughput": statistics.mean([r.throughput for r in results]),
            "test_results": [self._result_to_dict(r) for r in results]
        }

        # Save report to file
        report_path = os.path.join(self.config.output_dir, f"producer_test_report_{int(time.time())}.json")
        with open(report_path, 'w') as f:
            json.dump(report, f, indent=2)

        # Generate visualizations
        self._generate_visualizations(results)

        return report

    def _result_to_dict(self, result: ProducerTestResult) -> Dict[str, Any]:
        """Convert test result to dictionary."""
        return {
            "name": result.details.get("test_case"),
            "success": result.success,
            "messages_sent": result.messages_sent,
            "messages_confirmed": result.messages_confirmed,
            "validation_errors": result.validation_errors,
            "producer_errors": result.producer_errors,
            "latency_stats": result.latency_stats,
            "throughput": result.throughput,
            "details": result.details
        }

    def _generate_visualizations(self, results: List[ProducerTestResult]) -> None:
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

        plt.title('Message Publishing Throughput by Test')
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

            plt.title('Message Publishing Latency by Test')
            plt.xlabel('Test Case')
            plt.ylabel('Latency (ms)')
            plt.xticks(x, test_names, rotation=45, ha='right')
            plt.legend()
            plt.tight_layout()
            plt.savefig(os.path.join(self.config.output_dir, 'latency_comparison.png'))

@pipeline
def producer_test_pipeline() -> Dict[str, Any]:
    """ZenML pipeline for producer testing."""
    tester = MessageProducerTester()

    # Run individual test steps
    format_result = tester.run_message_format_validation_test()
    reliability_result = tester.run_publishing_reliability_test()
    error_handling_result = tester.run_error_handling_test()

    # Run performance tests with different batch sizes
    performance_results = []
    for batch_size in [10, 100, 1000]:
        result = tester.run_performance_test(batch_size)
        performance_results.append(result)

    # Combine all results
    all_results = [format_result, reliability_result, error_handling_result] + performance_results

    # Generate report
    report = tester.generate_test_report(all_results)
    return report

def create_airflow_dag():
    """Create an Airflow DAG for scheduled producer testing."""
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
        'producer_tests',
        default_args=default_args,
        description='RabbitMQ Producer Tests',
        schedule_interval=timedelta(days=1),
        catchup=False
    )

    def run_test_suite(**kwargs):
        from zenml.client import Client

        # Initialize ZenML
        client = Client()

        # Run the pipeline
        pipeline_instance = producer_test_pipeline()
        pipeline_instance.run()

        return "Tests completed successfully"

    test_task = PythonOperator(
        task_id='run_producer_tests',
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
def producer_tester():
    """Fixture for the producer tester."""
    tester = MessageProducerTester()
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
async def test_message_format_validation(producer_tester):
    """Test message format validation."""
    # Connect to RabbitMQ
    await producer_tester.connect()

    # Run test case
    test_case = ProducerTestCase(
        name="message_format_validation",
        exchange="test.format.validation",
        routing_key="sensor.data",
        schema_id="sensor-reading",
        message_generator=producer_tester.generate_sensor_data,
        message_count=10,
        expected_confirms=10,
        timeout=5
    )

    result = await producer_tester.run_test_case(test_case)

    # Assertions
    assert result.success, f"Format validation test failed: {result}"
    assert result.messages_sent == test_case.message_count
    assert result.messages_confirmed >= test_case.expected_confirms
    assert result.validation_errors == 0
    assert result.producer_errors == 0

@pytest.mark.asyncio
async def test_publishing_reliability(producer_tester):
    """Test publishing reliability with a larger volume."""
    # Connect to RabbitMQ
    await producer_tester.connect()

    # Run test case
    test_case = ProducerTestCase(
        name="publishing_reliability",
        exchange="test.reliability",
        routing_key="events.#",
        schema_id="device-state-change",
        message_generator=producer_tester.generate_event_data,
        message_count=100,
        expected_confirms=95,  # Allow for some message loss
        timeout=15
    )

    result = await producer_tester.run_test_case(test_case)

    # Assertions
    assert result.success, f"Reliability test failed: {result}"
    assert result.messages_sent == test_case.message_count
    assert result.messages_confirmed >= test_case.expected_confirms
    assert result.validation_errors == 0
    assert result.producer_errors == 0

@pytest.mark.asyncio
async def test_error_handling(producer_tester):
    """Test error handling with invalid messages."""
    # Connect to RabbitMQ
    await producer_tester.connect()

    # Define a generator for invalid data
    def generate_invalid_sensor_data(index: int) -> Dict[str, Any]:
        if index % 3 == 0:
            # Invalid data - missing required fields
            return {
                "sensor_id": f"test_sensor_{index}",
                # Missing value and timestamp fields
                "test_id": str(uuid.uuid4())
            }
        else:
            return producer_tester.generate_sensor_data(index)

    # Run test case
    test_case = ProducerTestCase(
        name="error_handling",
        exchange="test.error.handling",
        routing_key="sensor.data",
        schema_id="sensor-reading",
        message_generator=generate_invalid_sensor_data,
        message_count=15,
        expected_confirms=10,  # Expect only valid messages
        expected_errors=5,     # Expect 5 validation errors
        timeout=10
    )

    result = await producer_tester.run_test_case(test_case)

    # Assertions
    assert result.success, f"Error handling test failed: {result}"
    assert result.messages_sent <= test_case.message_count
    assert result.messages_confirmed >= test_case.expected_confirms
    assert result.validation_errors == test_case.expected_errors
    assert result.producer_errors == 0

@pytest.mark.asyncio
@pytest.mark.parametrize("batch_size", [10, 100])
async def test_performance(producer_tester, batch_size):
    """Test producer performance with different batch sizes."""
    # Connect to RabbitMQ
    await producer_tester.connect()

    # Run test case
    test_case = ProducerTestCase(
        name=f"performance_test_batch_{batch_size}",
        exchange="test.performance",
        routing_key="metrics.#",
        schema_id="system-metric",
        message_generator=producer_tester.generate_metrics_data,
        message_count=batch_size,
        expected_confirms=int(batch_size * 0.95),  # Allow for some message loss
        timeout=max(30, batch_size // 50)  # Adjust timeout based on batch size
    )

    result = await producer_tester.run_test_case(test_case)

    # Assertions
    assert result.success, f"Performance test with batch size {batch_size} failed: {result}"
    assert result.messages_sent == test_case.message_count
    assert result.messages_confirmed >= test_case.expected_confirms
    assert result.validation_errors == 0
    assert result.producer_errors == 0
    assert result.throughput > 0, "Throughput should be greater than 0"

def main():
    """Main entry point for running test suite directly."""
    import argparse

    parser = argparse.ArgumentParser(description='RabbitMQ Producer Tests')
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
            pipeline_instance = producer_test_pipeline()
            pipeline_instance.run()
        except Exception as e:
            logger.error(f"Error running ZenML pipeline: {e}")

if __name__ == "__main__":
    main()

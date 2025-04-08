"""
Message Flow Testing Framework for Distributed Systems

A comprehensive testing framework for verifying message flows in distributed systems.
Supports RabbitMQ message queues with features for:
- Tracing message paths through multiple services
- Measuring latency and throughput
- Visualizing message flow graphs
- Prometheus metrics collection

Usage:
    # Run tests with pytest
    pytest message_flow_tests.py -v

    # Execute as ZenML pipeline
    zenml pipeline run message_flow_test_pipeline --config path/to/config.yaml

    # Deploy as Airflow DAG
    cp message_flow_tests.py /path/to/airflow/dags/

Requires:
    - RabbitMQ server
    - Python 3.7+
    - Dependencies: pika, networkx, matplotlib, pytest, zenml, airflow
"""
import os
import sys
import json
import time
import yaml
import logging
import pytest
import pika
import networkx as nx
import matplotlib.pyplot as plt
from typing import Dict, List, Any, Optional, Generator
from datetime import datetime, timedelta
from contextlib import contextmanager
from dataclasses import dataclass, field
from zenml.pipelines import pipeline
from zenml.steps import step
from airflow import DAG
from airflow.operators.python import PythonOperator
from influxdb_client import InfluxDBClient
from prometheus_client import start_http_server, Counter, Histogram, Summary

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("message_flow_test")

# Metrics for monitoring test execution
MESSAGES_SENT = Counter('test_messages_sent_total', 'Number of test messages sent')
MESSAGES_RECEIVED = Counter('test_messages_received_total', 'Number of test messages received')
FLOW_LATENCY = Histogram('test_flow_latency_seconds', 'Message flow latency')
TEST_DURATION = Summary('test_duration_seconds', 'Test execution duration')

@dataclass
class MessageFlowConfig:
    """Configuration for message flow testing."""
    input_exchange: str
    output_queues: List[str]
    routing_key: str
    expected_transformations: List[Dict]
    timeout: int = 30
    batch_size: int = 100
    verify_order: bool = False
    cleanup_data: bool = True

@dataclass
class FlowTestResult:
    """Results from a message flow test."""
    success: bool
    messages_sent: int
    messages_received: int
    latency_ms: float
    errors: List[str] = field(default_factory=list)
    flow_trace: Dict = field(default_factory=dict)

class MessageTracer:
    """Utility for tracing messages through the system."""

    def __init__(self):
        self.graph = nx.DiGraph()
        self.traces = {}

    def add_trace(self, message_id: str, node: str, timestamp: float):
        """Add a trace point for a message."""
        if message_id not in self.traces:
            self.traces[message_id] = []
        self.traces[message_id].append((node, timestamp))

        # Update flow graph
        if len(self.traces[message_id]) > 1:
            prev_node, _ = self.traces[message_id][-2]
            self.graph.add_edge(prev_node, node)

    def get_flow_diagram(self, output_path: str):
        """Generate a flow diagram showing message paths."""
        plt.figure(figsize=(12, 8))
        pos = nx.spring_layout(self.graph)
        nx.draw(self.graph, pos, with_labels=True, node_color='lightblue',
                node_size=1500, arrowsize=20)
        plt.savefig(output_path)
        plt.close()

    def get_latency_report(self) -> Dict:
        """Generate latency statistics for message flows."""
        latencies = {}
        for message_id, trace in self.traces.items():
            if len(trace) >= 2:
                start = trace[0][1]
                end = trace[-1][1]
                latencies[message_id] = end - start

        return {
            "min": min(latencies.values()) if latencies else 0,
            "max": max(latencies.values()) if latencies else 0,
            "avg": sum(latencies.values()) / len(latencies) if latencies else 0,
            "total_messages": len(latencies)
        }

class MessageFlowTester:
    """Main class for testing message flows."""

    def __init__(self, config_path: str):
        """Initialize with configuration file."""
        self.config = self._load_config(config_path)
        self.tracer = MessageTracer()
        self.connection = None
        self.channel = None

    def _load_config(self, config_path: str) -> Dict:
        """Load test configuration from YAML file."""
        with open(config_path, 'r') as f:
            return yaml.safe_load(f)

    @contextmanager
    def _rabbitmq_connection(self):
        """Context manager for RabbitMQ connection."""
        try:
            self.connection = pika.BlockingConnection(
                pika.ConnectionParameters(
                    host=self.config['rabbitmq']['host'],
                    port=self.config['rabbitmq']['port'],
                    credentials=pika.PlainCredentials(
                        username=self.config['rabbitmq']['username'],
                        password=self.config['rabbitmq']['password']
                    )
                )
            )
            self.channel = self.connection.channel()
            yield
        finally:
            if self.connection and not self.connection.is_closed:
                self.connection.close()

    @step
    def prepare_test_messages(self, flow_config: MessageFlowConfig) -> List[Dict]:
        """Prepare test messages with tracing headers."""
        messages = []
        for i in range(flow_config.batch_size):
            message_id = f"test_{int(time.time())}_{i}"
            message = {
                "id": message_id,
                "timestamp": datetime.utcnow().isoformat(),
                "test_data": f"Test message {i}",
                "expected_transformations": flow_config.expected_transformations
            }
            messages.append(message)
        return messages

    @step
    def publish_messages(self, messages: List[Dict], flow_config: MessageFlowConfig) -> None:
        """Publish test messages to input exchange."""
        with self._rabbitmq_connection():
            for message in messages:
                self.channel.basic_publish(
                    exchange=flow_config.input_exchange,
                    routing_key=flow_config.routing_key,
                    body=json.dumps(message),
                    properties=pika.BasicProperties(
                        message_id=message['id'],
                        timestamp=int(time.time()),
                        headers={"test_flow": True}
                    )
                )
                self.tracer.add_trace(message['id'], flow_config.input_exchange, time.time())
                MESSAGES_SENT.inc()

    @step
    def verify_message_processing(self, messages: List[Dict], flow_config: MessageFlowConfig) -> FlowTestResult:
        """Verify message processing through the system."""
        received_messages = {queue: [] for queue in flow_config.output_queues}
        errors = []

        def callback(ch, method, properties, body, queue):
            try:
                message = json.loads(body)
                if properties.headers and properties.headers.get("test_flow"):
                    received_messages[queue].append(message)
                    self.tracer.add_trace(message['id'], queue, time.time())
                    MESSAGES_RECEIVED.inc()
            except Exception as e:
                errors.append(f"Error processing message in {queue}: {str(e)}")

        with self._rabbitmq_connection():
            # Set up consumers for all output queues
            consumers = []
            for queue in flow_config.output_queues:
                consumer_tag = self.channel.basic_consume(
                    queue=queue,
                    on_message_callback=lambda ch, method, props, body:
                        callback(ch, method, props, body, queue)
                )
                consumers.append((queue, consumer_tag))

            # Wait for messages
            deadline = time.time() + flow_config.timeout
            while time.time() < deadline:
                self.connection.process_data_events()
                if all(len(received_messages[q]) >= len(messages) for q in flow_config.output_queues):
                    break
                time.sleep(0.1)

            # Cancel consumers
            for queue, consumer_tag in consumers:
                self.channel.basic_cancel(consumer_tag)

        # Calculate results
        total_received = sum(len(msgs) for msgs in received_messages.values())
        success = total_received >= len(messages) * len(flow_config.output_queues)
        latency_report = self.tracer.get_latency_report()

        return FlowTestResult(
            success=success,
            messages_sent=len(messages),
            messages_received=total_received,
            latency_ms=latency_report['avg'] * 1000,
            errors=errors,
            flow_trace=self.tracer.traces
        )

@pipeline
def message_flow_test_pipeline(config_path: str, flow_name: str):
    """ZenML pipeline for message flow testing."""
    tester = MessageFlowTester(config_path)
    flow_config = tester.config['flows'][flow_name]

    # Create test messages
    messages = tester.prepare_test_messages(flow_config)

    # Publish messages
    tester.publish_messages(messages, flow_config)

    # Verify processing
    result = tester.verify_message_processing(messages, flow_config)

    # Generate visualization
    tester.tracer.get_flow_diagram(f"flow_diagram_{flow_name}.png")

    return result

def create_airflow_dag(config_path: str):
    """Create Airflow DAG for running message flow tests."""
    with open(config_path, 'r') as f:
        config = yaml.safe_load(f)

    dag = DAG(
        'message_flow_tests',
        default_args={
            'owner': 'homelab',
            'start_date': datetime(2023, 1, 1),
            'retries': 1,
            'retry_delay': timedelta(minutes=5),
        },
        schedule_interval='@daily',
        catchup=False
    )

    def run_flow_test(flow_name, **context):
        result = message_flow_test_pipeline(config_path, flow_name)
        if not result.success:
            raise Exception(f"Flow test failed: {result.errors}")
        return result

    # Create tasks for each flow
    for flow_name in config['flows'].keys():
        PythonOperator(
            task_id=f'test_{flow_name}',
            python_callable=run_flow_test,
            op_kwargs={'flow_name': flow_name},
            dag=dag
        )

    return dag

@pytest.fixture(scope="session")
def flow_tester(tmp_path_factory):
    """Pytest fixture for message flow testing."""
    config_path = os.getenv('FLOW_TEST_CONFIG', 'flow_test_config.yaml')
    tester = MessageFlowTester(config_path)

    # Start metrics server
    metrics_port = int(os.getenv('METRICS_PORT', '9090'))
    start_http_server(metrics_port)

    return tester

def test_simple_flow(flow_tester):
    """Test a simple message flow."""
    flow_config = flow_tester.config['flows']['simple_flow']
    with TEST_DURATION.time():
        result = message_flow_test_pipeline(
            flow_tester.config['config_path'],
            'simple_flow'
        )

    assert result.success, f"Flow test failed: {result.errors}"
    assert result.messages_received == result.messages_sent * len(flow_config['output_queues'])
    assert result.latency_ms < flow_config.get('max_latency_ms', 1000)

def test_transformation_flow(flow_tester):
    """Test a flow with transformations."""
    flow_config = flow_tester.config['flows']['transform_flow']
    with TEST_DURATION.time():
        result = message_flow_test_pipeline(
            flow_tester.config['config_path'],
            'transform_flow'
        )

    assert result.success, f"Flow test failed: {result.errors}"

    # Verify transformations
    for trace in result.flow_trace.values():
        transformed_data = trace[-1][0]  # Get final state
        for transform in flow_config['expected_transformations']:
            assert transform['check'](transformed_data), \
                f"Transformation check failed: {transform['description']}"

if __name__ == "__main__":
    # Run tests through pytest when executed directly
    pytest.main([__file__, "-v", "--capture=no"])

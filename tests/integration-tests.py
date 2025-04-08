'''
Integration Tests for Home Lab Data Pipelines

A comprehensive test suite for validating data flows and component integrations
in a home lab environment.

Components Tested:
- Home Assistant event publishing and command handling
- Triton inference server request/response flow
- Metrics collection and InfluxDB storage
- Alert notification system
- Data transformation pipelines

Key Features:
- Asynchronous testing with pytest-asyncio
- Pipeline orchestration via ZenML
- Scheduled execution through Airflow
- Prometheus metrics and monitoring
- HTML test reports
- Configurable test environment

Requirements:
    pytest >= 7.0
    pytest-asyncio >= 0.18
    pytest-html >= 3.0
    zenml >= 0.20
    apache-airflow >= 2.0

Usage:
    # Set config path
    export TEST_CONFIG=config.json

    # Run tests
    python integration-tests.py

The tests validate core functionality while following testing best practices
like proper isolation, error handling, and resource cleanup.
'''
import os
import sys
import json
import time
import pytest
import logging
import asyncio
from typing import Dict, List, Any, Optional
from datetime import datetime, timedelta
from pathlib import Path
from functools import partial

import aiohttp
from homeassistant_api import Client
import pika
import tritonclient.http
from influxdb_client import InfluxDBClient
from zenml.steps import step
from zenml.pipelines import pipeline
from airflow import DAG
from airflow.operators.python import PythonOperator
from prometheus_client import start_http_server, Counter, Summary

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('integration_test.log')
    ]
)
logger = logging.getLogger("integration_test")

# Test metrics
TEST_RUNS = Counter('integration_test_runs_total', 'Number of test runs')
TEST_FAILURES = Counter('integration_test_failures_total', 'Number of test failures', ['component'])
TEST_DURATION = Summary('integration_test_duration_seconds', 'Test execution time')

class IntegrationTestConfig:
    """Configuration for integration tests."""

    def __init__(self, config_path: str):
        with open(config_path) as f:
            self.config = json.load(f)

        # Set up environment-specific configurations
        self.setup_environment()

    def setup_environment(self):
        """Setup test environment based on configuration."""
        # Start metrics server if enabled
        if self.config.get('metrics', {}).get('enabled', True):
            metrics_port = self.config.get('metrics', {}).get('port', 9090)
            start_http_server(metrics_port)
            logger.info(f"Started metrics server on port {metrics_port}")

@step
def prepare_test_data() -> Dict:
    """Prepare test data for integration testing."""
    return {
        "sensor_data": {
            "temperature": 21.5,
            "humidity": 45,
            "motion": True,
            "timestamp": datetime.utcnow().isoformat()
        },
        "inference_request": {
            "model_name": "comfort_analysis",
            "inputs": {
                "temperature": 21.5,
                "humidity": 45
            }
        },
        "alert_data": {
            "severity": "warning",
            "source": "motion_sensor",
            "message": "Motion detected in living room"
        }
    }

@pytest.fixture(scope="session")
async def test_config():
    """Fixture for test configuration."""
    config_path = os.getenv('TEST_CONFIG', 'config.json')
    return IntegrationTestConfig(config_path)

@pytest.fixture(scope="session")
async def hass_client(test_config):
    """Fixture for Home Assistant API client."""
    client = Client(
        test_config.config['hass']['url'],
        test_config.config['hass']['token']
    )
    yield client
    await client.close()

@pytest.fixture(scope="session")
async def rabbitmq_connection(test_config):
    """Fixture for RabbitMQ connection."""
    connection = await connect_rabbitmq(test_config.config['rabbitmq'])
    yield connection
    await connection.close()

@pipeline(enable_cache=False)
def data_flow_test_pipeline(
    config: Dict,
    test_data: Dict
) -> Dict:
    """
    ZenML pipeline for testing data flow through the system.
    """
    # Test steps will be registered here
    results = {}

    # Step 1: Test Home Assistant to RabbitMQ flow
    results['hass_to_rabbitmq'] = test_hass_to_rabbitmq_flow(config, test_data)

    # Step 2: Test RabbitMQ to Triton flow
    results['rabbitmq_to_triton'] = test_rabbitmq_to_triton_flow(config, test_data)

    # Step 3: Test metrics flow
    results['metrics_flow'] = test_metrics_flow(config, test_data)

    # Step 4: Test alert flow
    results['alert_flow'] = test_alert_flow(config, test_data)

    # Step 5: Test transformations
    results['transformations'] = test_transformations(config, test_data)

    return results

def create_test_dag(
    dag_id: str,
    config: Dict,
    schedule: Optional[str] = None
) -> DAG:
    """Create Airflow DAG for running integration tests."""

    default_args = {
        'owner': 'homelab',
        'start_date': datetime(2023, 1, 1),
        'retries': 2,
        'retry_delay': timedelta(minutes=5)
    }

    dag = DAG(
        dag_id,
        default_args=default_args,
        schedule_interval=schedule or '@daily',
        catchup=False
    )

    def run_integration_tests(**context):
        """Run integration test suite."""
        with TEST_DURATION.time():
            test_data = prepare_test_data()
            results = data_flow_test_pipeline(config, test_data)

            # Store results
            context['task_instance'].xcom_push(
                key='test_results',
                value=results
            )

            return results

    # Create tasks for different test components
    components = [
        'hass_integration',
        'triton_integration',
        'metrics_collection',
        'alert_handling',
        'data_transformation'
    ]

    tasks = {}
    for component in components:
        tasks[component] = PythonOperator(
            task_id=f'test_{component}',
            python_callable=partial(run_integration_tests, component=component),
            dag=dag,
            provide_context=True
        )

    # Set up task dependencies
    tasks['hass_integration'] >> tasks['data_transformation']
    tasks['triton_integration'] >> tasks['data_transformation']
    tasks['metrics_collection'] >> tasks['alert_handling']

    return dag

class TestHomeAssistantIntegration:
    """Test suite for Home Assistant integration."""

    @pytest.mark.asyncio
    async def test_event_publishing(self, hass_client, rabbitmq_connection):
        """Test Home Assistant event publishing to RabbitMQ."""
        try:
            # Set up test event listener
            event_received = asyncio.Event()

            # Create channel and bind to test queue
            channel = await rabbitmq_connection.channel()
            await channel.queue_declare('test_events')

            # Set up consumer
            async def on_message(message):
                data = json.loads(message.body)
                if data['event_type'] == 'test_event':
                    event_received.set()

            await channel.basic_consume(
                queue='test_events',
                callback=on_message
            )

            # Trigger test event in Home Assistant
            await hass_client.fire_event(
                'test_event',
                {'test_data': 'integration_test'}
            )

            # Wait for event to be received
            try:
                await asyncio.wait_for(event_received.wait(), timeout=10.0)
                assert event_received.is_set(), "Event was not received in RabbitMQ"
            except asyncio.TimeoutError:
                pytest.fail("Timeout waiting for event in RabbitMQ")

        except Exception as e:
            TEST_FAILURES.labels(component='hass_integration').inc()
            logger.error(f"Home Assistant integration test failed: {e}")
            raise

class TestTritonIntegration:
    """Test suite for Triton Inference Server integration."""

    @pytest.mark.asyncio
    async def test_inference_request_flow(self, rabbitmq_connection, test_config):
        """Test inference request flow through RabbitMQ to Triton."""
        try:
            # Create test inference request
            request = {
                "model_name": "test_model",
                "inputs": {"data": [1.0, 2.0, 3.0]}
            }

            # Set up result listener
            result_received = asyncio.Event()
            result_data = None

            # Create channel and bind to result queue
            channel = await rabbitmq_connection.channel()
            await channel.queue_declare('test_results')

            async def on_result(message):
                nonlocal result_data
                result_data = json.loads(message.body)
                result_received.set()

            await channel.basic_consume(
                queue='test_results',
                callback=on_result
            )

            # Publish request
            await channel.basic_publish(
                exchange='inference_requests',
                routing_key='test_model',
                body=json.dumps(request)
            )

            # Wait for result
            try:
                await asyncio.wait_for(result_received.wait(), timeout=30.0)
                assert result_data is not None, "No inference result received"
                assert 'outputs' in result_data, "Invalid inference result format"
            except asyncio.TimeoutError:
                pytest.fail("Timeout waiting for inference result")

        except Exception as e:
            TEST_FAILURES.labels(component='triton_integration').inc()
            logger.error(f"Triton integration test failed: {e}")
            raise

class TestMetricsCollection:
    """Test suite for metrics collection."""

    @pytest.mark.asyncio
    async def test_metrics_flow(self, test_config):
        """Test metrics collection and storage."""
        try:
            # Connect to InfluxDB
            client = InfluxDBClient(
                url=test_config.config['influxdb']['url'],
                token=test_config.config['influxdb']['token'],
                org=test_config.config['influxdb']['org']
            )

            # Generate test metrics
            test_metric = {
                "name": "test_metric",
                "value": 42.0,
                "timestamp": datetime.utcnow().isoformat()
            }

            # Write test metric
            write_api = client.write_api()
            write_api.write(
                bucket=test_config.config['influxdb']['bucket'],
                record=test_metric
            )

            # Query metric back
            query_api = client.query_api()
            result = query_api.query(
                f'''from(bucket: "{test_config.config['influxdb']['bucket']}")
                    |> range(start: -1m)
                    |> filter(fn: (r) => r["_measurement"] == "test_metric")'''
            )

            assert len(result) > 0, "Metric was not stored in InfluxDB"

        except Exception as e:
            TEST_FAILURES.labels(component='metrics_collection').inc()
            logger.error(f"Metrics collection test failed: {e}")
            raise

class TestAlertHandling:
    """Test suite for alert handling."""

    @pytest.mark.asyncio
    async def test_alert_flow(self, rabbitmq_connection, test_config):
        """Test alert generation and notification flow."""
        try:
            # Create test alert
            alert = {
                "severity": "critical",
                "source": "test_integration",
                "message": "Test alert message"
            }

            # Set up notification listener
            notification_received = asyncio.Event()

            # Create channel and bind to notification queue
            channel = await rabbitmq_connection.channel()
            await channel.queue_declare('test_notifications')

            async def on_notification(message):
                data = json.loads(message.body)
                if data['source'] == 'test_integration':
                    notification_received.set()

            await channel.basic_consume(
                queue='test_notifications',
                callback=on_notification
            )

            # Publish alert
            await channel.basic_publish(
                exchange='alerts',
                routing_key='critical',
                body=json.dumps(alert)
            )

            # Wait for notification
            try:
                await asyncio.wait_for(notification_received.wait(), timeout=10.0)
                assert notification_received.is_set(), "Notification was not received"
            except asyncio.TimeoutError:
                pytest.fail("Timeout waiting for notification")

        except Exception as e:
            TEST_FAILURES.labels(component='alert_handling').inc()
            logger.error(f"Alert handling test failed: {e}")
            raise

class TestDataTransformation:
    """Test suite for data transformation pipelines."""

    @pytest.mark.asyncio
    async def test_transformation_pipeline(self, rabbitmq_connection, test_config):
        """Test data transformation pipeline."""
        try:
            # Create test data
            input_data = {
                "temperature": 68.0,  # Fahrenheit
                "timestamp": "2023-09-15T10:00:00Z",
                "source": "test_sensor"
            }

            # Set up transformed data listener
            data_transformed = asyncio.Event()
            transformed_data = None

            # Create channel and bind to transformation output queue
            channel = await rabbitmq_connection.channel()
            await channel.queue_declare('transformed_data')

            async def on_transformed_data(message):
                nonlocal transformed_data
                transformed_data = json.loads(message.body)
                data_transformed.set()

            await channel.basic_consume(
                queue='transformed_data',
                callback=on_transformed_data
            )

            # Publish input data
            await channel.basic_publish(
                exchange='raw_data',
                routing_key='temperature',
                body=json.dumps(input_data)
            )

            # Wait for transformed data
            try:
                await asyncio.wait_for(data_transformed.wait(), timeout=10.0)
                assert transformed_data is not None, "No transformed data received"

                # Verify transformations
                assert 'temperature_celsius' in transformed_data, "Temperature not converted to Celsius"
                assert abs(transformed_data['temperature_celsius'] - 20.0) < 0.1, "Incorrect temperature conversion"

            except asyncio.TimeoutError:
                pytest.fail("Timeout waiting for transformed data")

        except Exception as e:
            TEST_FAILURES.labels(component='data_transformation').inc()
            logger.error(f"Data transformation test failed: {e}")
            raise

def main():
    """Main entry point for running integration tests."""
    TEST_RUNS.inc()

    # Load configuration
    config_path = os.getenv('TEST_CONFIG', 'config.json')
    if not os.path.exists(config_path):
        logger.error(f"Configuration file not found: {config_path}")
        sys.exit(1)

    # Run tests with pytest
    status = pytest.main([
        __file__,
        '-v',
        '--html=report.html',
        '--self-contained-html'
    ])

    # Create Airflow DAG if running in Airflow context
    if os.getenv('AIRFLOW_HOME'):
        dag = create_test_dag(
            'integration_tests',
            json.load(open(config_path))
        )

    sys.exit(status)

if __name__ == "__main__":
    main()

#!/usr/bin/env python3

import argparse
import json
import logging
import os
import re
import requests
import sys
import time
import uuid
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple, Union
import unittest
import yaml
import tempfile
import concurrent.futures
import socket
import statistics
from contextlib import contextmanager
import matplotlib.pyplot as plt
import shutil
import subprocess
import importlib.util
import psutil
import pytest
import aiohttp

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("n8n-workflow-tester")

# For tracking memory usage and performance
import resource

class TestOutcome(Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    ERROR = "ERROR"
    SKIP = "SKIP"

class ServiceMockType(Enum):
    HTTP = "http"
    RABBITMQ = "rabbitmq"
    DATABASE = "database"
    FILESYSTEM = "filesystem"
    CUSTOM = "custom"

class N8nWorkflowTester:
    """Main testing framework for n8n workflows."""

    def __init__(
        self,
        n8n_url: str = None,
        n8n_api_key: str = None,
        workflows_dir: str = None,
        mocks_dir: str = None,
        tests_dir: str = None,
        output_dir: str = None,
        config_file: str = None,
        concurrency: int = 1,
        timeout: int = 60,
        verbose: bool = False
    ):
        """Initialize the testing framework.

        Args:
            n8n_url: Base URL of the n8n instance
            n8n_api_key: API key for accessing the n8n API
            workflows_dir: Directory containing workflow files
            mocks_dir: Directory containing service mocks
            tests_dir: Directory containing test cases
            output_dir: Directory to store test outputs
            config_file: Path to configuration file
            concurrency: Number of concurrent tests to run
            timeout: Timeout in seconds for workflow execution
            verbose: Whether to print verbose output
        """
        self.config = self._load_config(config_file)

        # Override config with provided parameters
        self.n8n_url = n8n_url or self.config.get('n8n_url')
        self.n8n_api_key = n8n_api_key or self.config.get('n8n_api_key')
        self.workflows_dir = Path(workflows_dir or self.config.get('workflows_dir', './workflows'))
        self.mocks_dir = Path(mocks_dir or self.config.get('mocks_dir', './mocks'))
        self.tests_dir = Path(tests_dir or self.config.get('tests_dir', './tests'))
        self.output_dir = Path(output_dir or self.config.get('output_dir', './test-reports'))
        self.concurrency = concurrency or self.config.get('concurrency', 1)
        self.timeout = timeout or self.config.get('timeout', 60)
        self.verbose = verbose or self.config.get('verbose', False)

        # Initialize test results storage
        self.results = {
            'tests': [],
            'summary': {
                'total': 0,
                'passed': 0,
                'failed': 0,
                'skipped': 0,
                'errors': 0,
                'start_time': datetime.now().isoformat(),
                'end_time': None,
                'duration': 0
            }
        }

        # Create output directory if it doesn't exist
        self.output_dir.mkdir(parents=True, exist_ok=True)

        # Store active mocks to be cleaned up later
        self.active_mocks = []

        # Initialize the mock servers
        self.mock_servers = {}

        # Set up verbose logging
        if self.verbose:
            logger.setLevel(logging.DEBUG)

        logger.debug(f"Initialized n8n workflow tester with URL: {self.n8n_url}")

    def _load_config(self, config_file: str) -> Dict[str, Any]:
        """Load configuration from YAML file."""
        if not config_file:
            return {}

        try:
            with open(config_file, 'r') as f:
                config = yaml.safe_load(f)
            return config
        except Exception as e:
            logger.warning(f"Error loading config file {config_file}: {e}")
            return {}

    def discover_tests(self, pattern: str = None) -> List[Dict[str, Any]]:
        """Discover test files in the test directory."""
        test_files = []

        # Use pattern if provided or default to all YAML files
        pattern = pattern or '**/*.yaml'

        for test_file in self.tests_dir.glob(pattern):
            try:
                with open(test_file, 'r') as f:
                    test_data = yaml.safe_load(f)

                # Validate test file structure
                if not self._validate_test_file(test_data):
                    logger.warning(f"Invalid test file: {test_file}")
                    continue

                test_data['_file_path'] = str(test_file)
                test_files.append(test_data)

            except Exception as e:
                logger.error(f"Error parsing test file {test_file}: {e}")

        logger.info(f"Discovered {len(test_files)} test files")
        return test_files

    def _validate_test_file(self, test_data: Dict[str, Any]) -> bool:
        """Validate the structure of a test file."""
        # Basic validation
        required_keys = ['workflow', 'name', 'tests']
        if not all(key in test_data for key in required_keys):
            return False

        # Check for at least one test case
        if not test_data['tests'] or not isinstance(test_data['tests'], list):
            return False

        # Validate test cases
        for test_case in test_data['tests']:
            if not isinstance(test_case, dict):
                return False
            if 'name' not in test_case:
                return False

        return True

    def run_tests(self, test_files: List[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Run all specified tests or discover and run all tests."""
        start_time = time.time()

        # Discover tests if not provided
        if test_files is None:
            test_files = self.discover_tests()

        # Update total test count
        total_tests = sum(len(test_file['tests']) for test_file in test_files)
        self.results['summary']['total'] = total_tests

        logger.info(f"Starting to run {total_tests} tests from {len(test_files)} test files")

        # Run tests concurrently based on concurrency setting
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.concurrency) as executor:
            futures = []

            for test_file in test_files:
                for test_case in test_file['tests']:
                    futures.append(
                        executor.submit(
                            self._run_test_case,
                            test_file,
                            test_case
                        )
                    )

            # Process results as they complete
            for future in concurrent.futures.as_completed(futures):
                try:
                    result = future.result()
                    self.results['tests'].append(result)
                    self._update_summary(result)
                except Exception as e:
                    logger.error(f"Error running test: {e}")

        # Record end time and duration
        end_time = time.time()
        duration = end_time - start_time

        self.results['summary']['end_time'] = datetime.now().isoformat()
        self.results['summary']['duration'] = duration

        logger.info(f"Tests completed in {duration:.2f} seconds")
        logger.info(f"Results: {self.results['summary']['passed']} passed, "
                   f"{self.results['summary']['failed']} failed, "
                   f"{self.results['summary']['errors']} errors, "
                   f"{self.results['summary']['skipped']} skipped")

        # Generate reports
        self.generate_reports()

        return self.results

    def _update_summary(self, result: Dict[str, Any]) -> None:
        """Update the summary statistics based on a test result."""
        outcome = result.get('outcome')

        if outcome == TestOutcome.PASS.value:
            self.results['summary']['passed'] += 1
        elif outcome == TestOutcome.FAIL.value:
            self.results['summary']['failed'] += 1
        elif outcome == TestOutcome.ERROR.value:
            self.results['summary']['errors'] += 1
        elif outcome == TestOutcome.SKIP.value:
            self.results['summary']['skipped'] += 1

    def _run_test_case(self, test_file: Dict[str, Any], test_case: Dict[str, Any]) -> Dict[str, Any]:
        """Run a single test case."""
        test_name = test_case.get('name', 'Unnamed test')
        workflow_name = test_file.get('workflow')
        test_id = str(uuid.uuid4())

        logger.info(f"Running test: {test_name} for workflow: {workflow_name}")

        result = {
            'id': test_id,
            'name': test_name,
            'workflow': workflow_name,
            'file': test_file.get('_file_path'),
            'start_time': datetime.now().isoformat(),
            'end_time': None,
            'duration': 0,
            'outcome': TestOutcome.ERROR.value,  # Default to ERROR, update later
            'details': {},
            'performance': {},
            'error': None
        }

        # Skip test if specified
        if test_case.get('skip', False):
            result['outcome'] = TestOutcome.SKIP.value
            result['end_time'] = datetime.now().isoformat()
            logger.info(f"Skipping test: {test_name}")
            return result

        try:
            # Start measuring resource usage
            start_time = time.time()
            start_memory = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss

            # Load the workflow
            workflow_data = self._load_workflow(workflow_name)

            # Setup test environment with mocks
            self._setup_test_environment(test_file, test_case)

            # Prepare input data
            input_data = test_case.get('input', {})

            # Run the workflow execution
            execution_result = self._execute_workflow(workflow_data, input_data)

            # Validate the results
            validation_result = self._validate_result(execution_result, test_case.get('expected', {}))

            # Measure resource usage
            end_time = time.time()
            end_memory = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss

            # Record performance metrics
            result['performance'] = {
                'duration_ms': round((end_time - start_time) * 1000, 2),
                'memory_kb': end_memory - start_memory,
                'cpu_time': resource.getrusage(resource.RUSAGE_SELF).ru_utime
            }

            # Update result with validation outcome
            result['outcome'] = TestOutcome.PASS.value if validation_result['success'] else TestOutcome.FAIL.value
            result['details'] = {
                'workflow_execution': execution_result,
                'validation': validation_result
            }

        except Exception as e:
            logger.error(f"Error in test {test_name}: {str(e)}")
            result['outcome'] = TestOutcome.ERROR.value
            result['error'] = str(e)

        finally:
            # Clean up test environment
            self._cleanup_test_environment()

            # Update end time and duration
            result['end_time'] = datetime.now().isoformat()
            result['duration'] = time.time() - start_time

        return result

    def _load_workflow(self, workflow_name: str) -> Dict[str, Any]:
        """Load a workflow definition from file or n8n API."""
        # First try to load from local file
        try:
            workflow_path = self.workflows_dir / f"{workflow_name}.json"
            if workflow_path.exists():
                with open(workflow_path, 'r') as f:
                    return json.load(f)
        except Exception as e:
            logger.error(f"Error loading workflow from file: {e}")

        # If not found locally, try to load from n8n API
        if self.n8n_url and self.n8n_api_key:
            try:
                response = requests.get(
                    f"{self.n8n_url}/workflows",
                    headers={
                        "X-N8N-API-KEY": self.n8n_api_key
                    }
                )
                response.raise_for_status()

                # Find workflow by name
                workflows = response.json()
                for workflow in workflows:
                    if workflow.get('name') == workflow_name:
                        return workflow

            except Exception as e:
                logger.error(f"Error loading workflow from API: {e}")

        raise FileNotFoundError(f"Workflow {workflow_name} not found")

    def _setup_test_environment(self, test_file: Dict[str, Any], test_case: Dict[str, Any]) -> None:
        """Set up the test environment with service mocks."""
        # Get the mocks to set up
        mocks = test_case.get('mocks', [])
        if not mocks:
            return

        for mock_config in mocks:
            mock_type = mock_config.get('type')
            mock_name = mock_config.get('name')

            if not mock_type or not mock_name:
                logger.warning(f"Invalid mock configuration: {mock_config}")
                continue

            try:
                # Load the mock
                mock_instance = self._load_mock(mock_type, mock_name)

                # Configure and start the mock
                mock_instance.configure(mock_config.get('config', {}))
                mock_instance.start()

                # Store in active mocks for cleanup
                self.active_mocks.append(mock_instance)

            except Exception as e:
                logger.error(f"Error setting up mock {mock_name}: {e}")
                raise

    def _load_mock(self, mock_type: str, mock_name: str) -> 'BaseMock':
        """Load a mock service by type and name."""
        if mock_type == ServiceMockType.HTTP.value:
            return HttpMock(mock_name, self.mocks_dir)
        elif mock_type == ServiceMockType.RABBITMQ.value:
            return RabbitMQMock(mock_name, self.mocks_dir)
        elif mock_type == ServiceMockType.DATABASE.value:
            return DatabaseMock(mock_name, self.mocks_dir)
        elif mock_type == ServiceMockType.FILESYSTEM.value:
            return FilesystemMock(mock_name, self.mocks_dir)
        elif mock_type == ServiceMockType.CUSTOM.value:
            return CustomMock(mock_name, self.mocks_dir)
        else:
            raise ValueError(f"Unknown mock type: {mock_type}")

    def _cleanup_test_environment(self) -> None:
        """Clean up all active mocks after test execution."""
        for mock in self.active_mocks:
            try:
                mock.stop()
            except Exception as e:
                logger.error(f"Error stopping mock {mock.name}: {e}")

        # Clear the active mocks list
        self.active_mocks = []

    def _execute_workflow(self, workflow_data: Dict[str, Any], input_data: Dict[str, Any]) -> Dict[str, Any]:
        """Execute a workflow with the given input data."""
        # If n8n API is available, use it
        if self.n8n_url and self.n8n_api_key:
            return self._execute_workflow_via_api(workflow_data, input_data)
        else:
            # Otherwise, simulate execution locally
            return self._simulate_workflow_execution(workflow_data, input_data)

    def _execute_workflow_via_api(self, workflow_data: Dict[str, Any], input_data: Dict[str, Any]) -> Dict[str, Any]:
        """Execute a workflow using the n8n API."""
        workflow_id = workflow_data.get('id')

        if not workflow_id:
            # If workflow doesn't exist in n8n yet, create it temporarily
            try:
                response = requests.post(
                    f"{self.n8n_url}/workflows",
                    headers={
                        "X-N8N-API-KEY": self.n8n_api_key,
                        "Content-Type": "application/json"
                    },
                    json=workflow_data
                )
                response.raise_for_status()
                workflow_id = response.json().get('id')

                # Flag to delete after execution
                temp_workflow = True
            except Exception as e:
                logger.error(f"Error creating temporary workflow: {e}")
                raise
        else:
            temp_workflow = False

        try:
            # Execute the workflow
            response = requests.post(
                f"{self.n8n_url}/workflows/{workflow_id}/execute",
                headers={
                    "X-N8N-API-KEY": self.n8n_api_key,
                    "Content-Type": "application/json"
                },
                json={
                    "data": input_data
                },
                timeout=self.timeout
            )
            response.raise_for_status()

            execution_result = response.json()
            return execution_result

        except requests.Timeout:
            raise TimeoutError(f"Workflow execution timed out after {self.timeout} seconds")
        except Exception as e:
            logger.error(f"Error executing workflow via API: {e}")
            raise
        finally:
            # Remove temporary workflow if created
            if temp_workflow and workflow_id:
                try:
                    requests.delete(
                        f"{self.n8n_url}/workflows/{workflow_id}",
                        headers={
                            "X-N8N-API-KEY": self.n8n_api_key
                        }
                    )
                except Exception as e:
                    logger.error(f"Error deleting temporary workflow: {e}")

    def _simulate_workflow_execution(self, workflow_data: Dict[str, Any], input_data: Dict[str, Any]) -> Dict[str, Any]:
        """Simulate workflow execution locally for testing purposes."""
        logger.info("Simulating workflow execution locally")

        # In a real implementation, this would use n8n libraries to run the workflow
        # This is a simplified simulation

        # Get the trigger node as the entry point
        trigger_node = None
        for node in workflow_data.get('nodes', []):
            if self._is_trigger_node(node):
                trigger_node = node
                break

        if not trigger_node:
            raise ValueError("No trigger node found in workflow")

        # Simulate execution starting from trigger node
        execution_data = {
            'nodes': {},
            'output': {},
            'success': True,
            'startTime': datetime.now().isoformat(),
            'endTime': None,
            'executionTime': 0
        }

        try:
            # Start with the trigger node
            node_output = {
                'json': input_data
            }

            # Store node result
            execution_data['nodes'][trigger_node['name']] = {
                'input': input_data,
                'output': node_output
            }

            # Find next nodes to execute
            connections = workflow_data.get('connections', {})
            visited_nodes = set([trigger_node['name']])
            output = self._follow_workflow_path(
                workflow_data,
                connections,
                trigger_node['name'],
                node_output,
                visited_nodes,
                execution_data
            )

            # Store final output
            execution_data['output'] = output

        except Exception as e:
            logger.error(f"Error simulating workflow execution: {e}")
            execution_data['success'] = False
            execution_data['error'] = str(e)

        # Record end time
        execution_data['endTime'] = datetime.now().isoformat()
        execution_data['executionTime'] = (
            datetime.fromisoformat(execution_data['endTime']) -
            datetime.fromisoformat(execution_data['startTime'])
        ).total_seconds() * 1000  # Convert to milliseconds

        return execution_data

    def _is_trigger_node(self, node: Dict[str, Any]) -> bool:
        """Check if a node is a trigger node."""
        # Check node type contains 'trigger'
        node_type = node.get('type', '').lower()
        return any(trigger_type in node_type for trigger_type in ['trigger', 'webhook', 'schedule', 'start'])

    def _follow_workflow_path(
        self,
        workflow_data: Dict[str, Any],
        connections: Dict[str, Any],
        current_node: str,
        input_data: Dict[str, Any],
        visited_nodes: Set[str],
        execution_data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Follow a workflow execution path recursively."""
        # Find connections from the current node
        node_connections = connections.get(current_node, {})
        if not node_connections:
            return input_data  # End of the path

        # Process main connections
        main_connections = node_connections.get('main', [])

        output = input_data

        for connection in main_connections:
            target_node_name = connection.get('node')

            # Skip already visited nodes to avoid cycles
            if target_node_name in visited_nodes:
                continue

            visited_nodes.add(target_node_name)

            # Find the target node definition
            target_node = None
            for node in workflow_data.get('nodes', []):
                if node.get('name') == target_node_name:
                    target_node = node
                    break

            if not target_node:
                logger.warning(f"Target node {target_node_name} not found in workflow")
                continue

            # Simulate node execution
            node_output = self._simulate_node_execution(target_node, input_data)

            # Store node result
            execution_data['nodes'][target_node_name] = {
                'input': input_data,
                'output': node_output
            }

            # Continue following the path
            output = self._follow_workflow_path(
                workflow_data,
                connections,
                target_node_name,
                node_output,
                visited_nodes,
                execution_data
            )

        return output

    def _simulate_node_execution(self, node: Dict[str, Any], input_data: Dict[str, Any]) -> Dict[str, Any]:
        """Simulate execution of a single node."""
        # This is a simplified simulation
        # In a real implementation, this would execute node-specific logic

        node_type = node.get('type', '')

        # Run a simple transformation based on node type
        if 'function' in node_type.lower():
            # Get function code from parameters
            function_code = node.get('parameters', {}).get('functionCode', '')
            if function_code:
                # Very basic simulation - don't actually execute arbitrary code
                return {'json': {'simulated_function_result': True}}

        elif 'http' in node_type.lower():
            # Simulate HTTP request
            return {'json': {'simulated_http_response': True}}

        elif 'if' in node_type.lower():
            # Simulate conditional node
            # Randomly choose a path for simulation
            import random
            if random.choice([True, False]):
                return {'json': input_data.get('json', {})}
            else:
                return None

        elif 'set' in node_type.lower():
            # Simulate Set node (simple pass-through)
            return {'json': input_data.get('json', {})}

        # Default pass-through for other node types
        return {'json': input_data.get('json', {})}

    def _validate_result(self, execution_result: Dict[str, Any], expected: Dict[str, Any]) -> Dict[str, Any]:
        """Validate execution results against expected outputs."""
        validation_result = {
            'success': True,
            'errors': [],
            'details': {}
        }

        # If we expect specific node outputs
        if 'nodes' in expected:
            for node_name, expected_output in expected['nodes'].items():
                # Check if node exists in execution result
                if node_name not in execution_result.get('nodes', {}):
                    validation_result['success'] = False
                    validation_result['errors'].append(f"Missing node {node_name} in execution result")
                    continue

                # Get actual node output
                actual_output = execution_result['nodes'][node_name].get('output', {})

                # Validate node output
                node_validation = self._validate_json(actual_output, expected_output)
                validation_result['details'][node_name] = node_validation

                if not node_validation['success']:
                    validation_result['success'] = False
                    validation_result['errors'].extend([
                        f"Node {node_name}: {error}" for error in node_validation['errors']
                    ])

        # If we expect specific final output
        if 'output' in expected:
            output_validation = self._validate_json(
                execution_result.get('output', {}),
                expected['output']
            )
            validation_result['details']['final_output'] = output_validation

            if not output_validation['success']:
                validation_result['success'] = False
                validation_result['errors'].extend([
                    f"Final output: {error}" for error in output_validation['errors']
                ])

        # If we expect success/failure state
        if 'success' in expected:
            if execution_result.get('success') != expected['success']:
                validation_result['success'] = False
                validation_result['errors'].append(
                    f"Expected success={expected['success']}, "
                    f"got success={execution_result.get('success')}"
                )

        return validation_result

    def _validate_json(self, actual: Any, expected: Any) -> Dict[str, Any]:
        """Validate JSON data against expected pattern."""
        validation_result = {
            'success': True,
            'errors': [],
            'actual': actual,
            'expected': expected
        }

        try:
            if isinstance(expected, dict):
                if not isinstance(actual, dict):
                    validation_result['success'] = False
                    validation_result['errors'].append(f"Expected dict, got {type(actual).__name__}")
                    return validation_result

                # Check for special comparison operators in keys
                for key, expected_value in expected.items():
                    # Regular key
                    if not key.startswith('$'):
                        if key not in actual:
                            validation_result['success'] = False
                            validation_result['errors'].append(f"Missing key: {key}")
                        else:
                            # Recurse into nested objects
                            nested_validation = self._validate_json(actual[key], expected_value)
                            if not nested_validation['success']:
                                validation_result['success'] = False
                                validation_result['errors'].extend([
                                    f"{key}.{error}" for error in nested_validation['errors']
                                ])

                    # Special comparison: $exists
                    elif key == '$exists':
                        if isinstance(expected_value, str):
                            exists = expected_value in actual
                            if not exists:
                                validation_result['success'] = False
                                validation_result['errors'].append(f"Key {expected_value} does not exist")
                        elif isinstance(expected_value, list):
                            for field in expected_value:
                                exists = field in actual
                                if not exists:
                                    validation_result['success'] = False
                                    validation_result['errors'].append(f"Key {field} does not exist")

                    # Special comparison: $type
                    elif key == '$type':
                        for field, expected_type in expected_value.items():
                            if field not in actual:
                                validation_result['success'] = False
                                validation_result['errors'].append(f"Missing key for type check: {field}")
                            else:
                                actual_type = type(actual[field]).__name__
                                if actual_type != expected_type:
                                    validation_result['success'] = False
                                    validation_result['errors'].append(
                                        f"Type mismatch for {field}: expected {expected_type}, "
                                        f"got {actual_type}"
                                    )

                    # Special comparison: $regex
                    elif key == '$regex':
                        for field, pattern in expected_value.items():
                            if field not in actual:
                                validation_result['success'] = False
                                validation_result['errors'].append(f"Missing key for regex check: {field}")
                            else:
                                if not re.match(pattern, str(actual[field])):
                                    validation_result['success'] = False
                                    validation_result['errors'].append(
                                        f"Regex match failed for {field}: {pattern}"
                                    )

            elif isinstance(expected, list):
                if not isinstance(actual, list):
                    validation_result['success'] = False
                    validation_result['errors'].append(f"Expected list, got {type(actual).__name__}")
                    return validation_result

                # Check list length if expected list is not empty
                if expected and len(actual) != len(expected):
                    validation_result['success'] = False
                    validation_result['errors'].append(
                        f"List length mismatch: expected {len(expected)}, got {len(actual)}"
                    )

                # Validate each item in the list
                for i, (actual_item, expected_item) in enumerate(zip(actual, expected)):
                    item_validation = self._validate_json(actual_item, expected_item)
                    if not item_validation['success']:
                        validation_result['success'] = False
                        validation_result['errors'].extend([
                            f"[{i}].{error}" for error in item_validation['errors']
                        ])

            else:
                # Direct comparison for primitive types
                if actual != expected:
                    validation_result['success'] = False
                    validation_result['errors'].append(
                        f"Value mismatch: expected {expected}, got {actual}"
                    )

        except Exception as e:
            validation_result['success'] = False
            validation_result['errors'].append(f"Validation error: {str(e)}")

        return validation_result

    def generate_reports(self) -> None:
        """Generate test reports in various formats."""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        # JSON report
        json_path = self.output_dir / f"test_report_{timestamp}.json"
        with open(json_path, 'w') as f:
            json.dump(self.results, f, indent=2)

        # HTML report
        html_path = self.output_dir / f"test_report_{timestamp}.html"
        self._generate_html_report(html_path)

        # Performance charts
        chart_path = self.output_dir / f"performance_charts_{timestamp}.png"
        self._generate_performance_charts(chart_path)

        logger.info(f"Reports generated in {self.output_dir}")

    def _generate_html_report(self, output_path: Path) -> None:
        """Generate an HTML report from test results."""
        try:
            # Load HTML template
            template_path = Path(__file__).parent / "templates" / "report_template.html"

            if not template_path.exists():
                # Create simple template on the fly if not found
                template = """
                <!DOCTYPE html>
                <html>
                <head>
                    <title>n8n Workflow Test Report</title>
                    <style>
                        body { font-family: Arial, sans-serif; margin: 20px; }
                        .header { background-color: #f8f9fa; padding: 20px; border-radius: 5px; }
                        .summary { display: flex; margin: 20px 0; }
                        .summary-box { flex: 1; padding: 15px; margin-right: 10px; border-radius: 5px; color: white; }
                        .pass { background-color: #28a745; }
                        .fail { background-color: #dc3545; }
                        .error { background-color: #fd7e14; }
                        .skip { background-color: #6c757d; }
                        table { width: 100%; border-collapse: collapse; }
                        th, td { border: 1px solid #ddd; padding: 8px; text-align: left; }
                        th { background-color: #f2f2f2; }
                        .passed { background-color: rgba(40, 167, 69, 0.2); }
                        .failed { background-color: rgba(220, 53, 69, 0.2); }
                        .error { background-color: rgba(253, 126, 20, 0.2); }
                        .skipped { background-color: rgba(108, 117, 125, 0.2); }
                        .details { margin-top: 5px; }
                        pre { background-color: #f8f9fa; padding: 10px; border-radius: 5px; overflow: auto; }
                    </style>
                </head>
                <body>
                    <div class="header">
                        <h1>n8n Workflow Test Report</h1>
                        <p>Start Time: {{start_time}}</p>
                        <p>End Time: {{end_time}}</p>
                        <p>Duration: {{duration}} seconds</p>
                    </div>

                    <div class="summary">
                        <div class="summary-box pass">
                            <h2>Passed</h2>
                            <p>{{summary_passed}}</p>
                        </div>
                        <div class="summary-box fail">
                            <h2>Failed</h2>
                            <p>{{summary_failed}}</p>
                        </div>
                        <div class="summary-box error">
                            <h2>Errors</h2>
                            <p>{{summary_errors}}</p>
                        </div>
                        <div class="summary-box skip">
                            <h2>Skipped</h2>
                            <p>{{summary_skipped}}</p>
                        </div>
                    </div>

                    <h2>Test Results</h2>
                    <table>
                        <tr>
                            <th>Test</th>
                            <th>Workflow</th>
                            <th>Outcome</th>
                            <th>Duration (ms)</th>
                            <th>Memory (KB)</th>
                            <th>Details</th>
                        </tr>
                        {{test_rows}}
                    </table>
                </body>
                </html>
                """
            else:
                with open(template_path, 'r') as f:
                    template = f.read()

            # Prepare summary values
            summary = self.results['summary']

            # Prepare test rows
            test_rows = ""
            for test in self.results['tests']:
                outcome_class = {
                    TestOutcome.PASS.value: "passed",
                    TestOutcome.FAIL.value: "failed",
                    TestOutcome.ERROR.value: "error",
                    TestOutcome.SKIP.value: "skipped"
                }.get(test['outcome'], "")

                # Format details based on outcome
                if test['outcome'] == TestOutcome.PASS.value:
                    details = "<details><summary>View Details</summary><pre>Test passed</pre></details>"
                elif test['outcome'] == TestOutcome.FAIL.value:
                    details = f"<details><summary>View Details</summary><pre>{json.dumps(test.get('details', {}).get('validation', {}).get('errors', []), indent=2)}</pre></details>"
                elif test['outcome'] == TestOutcome.ERROR.value:
                    details = f"<details><summary>View Details</summary><pre>{test.get('error', 'Unknown error')}</pre></details>"
                else:  # SKIP
                    details = "<pre>Test skipped</pre>"

                test_rows += f"""
                <tr class="{outcome_class}">
                    <td>{test['name']}</td>
                    <td>{test['workflow']}</td>
                    <td>{test['outcome']}</td>
                    <td>{test.get('performance', {}).get('duration_ms', 'N/A')}</td>
                    <td>{test.get('performance', {}).get('memory_kb', 'N/A')}</td>
                    <td class="details">{details}</td>
                </tr>
                """

            # Replace placeholders in template
            html_content = template
            html_content = html_content.replace("{{start_time}}", summary.get('start_time', 'N/A'))
            html_content = html_content.replace("{{end_time}}", summary.get('end_time', 'N/A'))
            html_content = html_content.replace("{{duration}}", str(round(summary.get('duration', 0), 2)))
            html_content = html_content.replace("{{summary_passed}}", str(summary.get('passed', 0)))
            html_content = html_content.replace("{{summary_failed}}", str(summary.get('failed', 0)))
            html_content = html_content.replace("{{summary_errors}}", str(summary.get('errors', 0)))
            html_content = html_content.replace("{{summary_skipped}}", str(summary.get('skipped', 0)))
            html_content = html_content.replace("{{test_rows}}", test_rows)

            # Write to file
            with open(output_path, 'w') as f:
                f.write(html_content)

        except Exception as e:
            logger.error(f"Error generating HTML report: {e}")

    def _generate_performance_charts(self, output_path: Path) -> None:
        """Generate performance charts from test results."""
        try:
            plt.figure(figsize=(12, 10))

            # Extract performance data
            test_names = []
            durations = []
            memory_usages = []

            for test in self.results['tests']:
                # Skip tests without performance data
                if 'performance' not in test:
                    continue

                test_names.append(test['name'])
                durations.append(test['performance'].get('duration_ms', 0))
                memory_usages.append(test['performance'].get('memory_kb', 0))

            if not test_names:
                logger.warning("No performance data available for charts")
                return

            # Create subplots
            plt.subplot(2, 1, 1)
            bars = plt.bar(test_names, durations, color='skyblue')
            plt.title('Execution Duration (ms)')
            plt.xticks(rotation=45, ha='right')
            plt.tight_layout()

            # Add values on top of bars
            for bar in bars:
                height = bar.get_height()
                plt.text(bar.get_x() + bar.get_width()/2., 1.05*height,
                        f'{height:.1f}',
                        ha='center', va='bottom', rotation=0)

            plt.subplot(2, 1, 2)
            bars = plt.bar(test_names, memory_usages, color='lightgreen')
            plt.title('Memory Usage (KB)')
            plt.xticks(rotation=45, ha='right')
            plt.tight_layout()

            # Add values on top of bars
            for bar in bars:
                height = bar.get_height()
                plt.text(bar.get_x() + bar.get_width()/2., 1.05*height,
                        f'{height:.1f}',
                        ha='center', va='bottom', rotation=0)

            plt.tight_layout()
            plt.savefig(output_path)
            plt.close()

        except Exception as e:
            logger.error(f"Error generating performance charts: {e}")


# Base class for service mocks
class BaseMock:
    """Base class for all service mocks."""

    def __init__(self, name: str, mocks_dir: Path):
        self.name = name
        self.mocks_dir = mocks_dir
        self.running = False
        self.config = {}

    def configure(self, config: Dict[str, Any]) -> None:
        """Configure the mock with provided settings."""
        self.config.update(config)

    def start(self) -> None:
        """Start the mock service."""
        self.running = True

    def stop(self) -> None:
        """Stop the mock service."""
        self.running = False

    def get_info(self) -> Dict[str, Any]:
        """Get information about the running mock."""
        return {
            'name': self.name,
            'type': self.__class__.__name__,
            'running': self.running,
            'config': self.config
        }


class HttpMock(BaseMock):
    """Mock for HTTP services."""

    def __init__(self, name: str, mocks_dir: Path):
        super().__init__(name, mocks_dir)
        self.server = None
        self.port = None
        self._find_free_port()
        self.endpoints = {}

    def _find_free_port(self) -> None:
        """Find a free port to use for the mock server."""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(('localhost', 0))
            self.port = s.getsockname()[1]

    def configure(self, config: Dict[str, Any]) -> None:
        """Configure HTTP endpoints and responses."""
        super().configure(config)
        self.endpoints = config.get('endpoints', {})

    def start(self) -> None:
        """Start the HTTP mock server."""
        import threading
        import http.server
        import json

        # Define request handler
        class MockHttpHandler(http.server.BaseHTTPRequestHandler):
            parent = self

            def do_GET(self):
                self._handle_request('GET')

            def do_POST(self):
                self._handle_request('POST')

            def do_PUT(self):
                self._handle_request('PUT')

            def do_DELETE(self):
                self._handle_request('DELETE')

            def _handle_request(self, method):
                # Get endpoint config
                endpoint_key = f"{method}:{self.path}"
                if endpoint_key in self.parent.endpoints:
                    config = self.parent.endpoints[endpoint_key]

                    # Get request body if present
                    content_length = int(self.headers.get('Content-Length', 0))
                    request_body = self.rfile.read(content_length) if content_length > 0 else None

                    # Set response code
                    self.send_response(config.get('status', 200))

                    # Set headers
                    for name, value in config.get('headers', {}).items():
                        self.send_header(name, value)
                    self.end_headers()

                    # Send response body
                    response_body = config.get('body', '')
                    if isinstance(response_body, dict):
                        response_body = json.dumps(response_body)
                    self.wfile.write(response_body.encode('utf-8'))
                else:
                    # Default 404 response
                    self.send_response(404)
                    self.send_header('Content-Type', 'application/json')
                    self.end_headers()
                    self.wfile.write(json.dumps({
                        'error': 'Not Found',
                        'message': f"No mock configured for {method} {self.path}"
                    }).encode('utf-8'))

        # Create and start server
        self.server = http.server.HTTPServer(('localhost', self.port), MockHttpHandler)
        self.server_thread = threading.Thread(target=self.server.serve_forever)
        self.server_thread.daemon = True
        self.server_thread.start()

        logger.info(f"Started HTTP mock server on port {self.port}")
        super().start()

    def stop(self) -> None:
        """Stop the HTTP mock server."""
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.server = None

        logger.info(f"Stopped HTTP mock server on port {self.port}")
        super().stop()

    def get_info(self) -> Dict[str, Any]:
        """Get information about the HTTP mock server."""
        info = super().get_info()
        info['url'] = f"http://localhost:{self.port}"
        info['endpoints'] = list(self.endpoints.keys())
        return info


class RabbitMQMock(BaseMock):
    """Mock for RabbitMQ messaging."""

    def __init__(self, name: str, mocks_dir: Path):
        super().__init__(name, mocks_dir)
        self.port = None
        self._find_free_port()
        self.exchanges = {}
        self.queues = {}
        self.bindings = {}
        self.messages = []

    def _find_free_port(self) -> None:
        """Find a free port to use for the mock server."""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(('localhost', 0))
            self.port = s.getsockname()[1]

    def configure(self, config: Dict[str, Any]) -> None:
        """Configure RabbitMQ exchanges, queues, and messages."""
        super().configure(config)
        self.exchanges = config.get('exchanges', {})
        self.queues = config.get('queues', {})
        self.bindings = config.get('bindings', [])
        self.messages = config.get('messages', [])

    def start(self) -> None:
        """Start the RabbitMQ mock server."""
        import threading
        import socket

        # Create socket server for basic AMQP simulation
        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server.bind(('localhost', self.port))
        self.server.listen(5)

        # Start server in separate thread
        self.server_thread = threading.Thread(target=self._run_server)
        self.server_thread.daemon = True
        self.server_thread.start()

        logger.info(f"Started RabbitMQ mock server on port {self.port}")
        super().start()

    def _run_server(self) -> None:
        """Run the mock AMQP server."""
        try:
            while True:
                client, _ = self.server.accept()
                client.close()  # Very basic simulation, just accept and close
        except:
            pass

    def stop(self) -> None:
        """Stop the RabbitMQ mock server."""
        if hasattr(self, 'server') and self.server:
            self.server.close()

        logger.info(f"Stopped RabbitMQ mock server on port {self.port}")
        super().stop()

    def get_info(self) -> Dict[str, Any]:
        """Get information about the RabbitMQ mock server."""
        info = super().get_info()
        info['url'] = f"amqp://localhost:{self.port}"
        info['exchanges'] = list(self.exchanges.keys())
        info['queues'] = list(self.queues.keys())
        return info


class DatabaseMock(BaseMock):
    """Mock for database services."""

    def __init__(self, name: str, mocks_dir: Path):
        super().__init__(name, mocks_dir)
        self.port = None
        self._find_free_port()
        self.db_type = 'sqlite'  # Default
        self.tables = {}
        self.queries = {}
        self.db_file = None

    def _find_free_port(self) -> None:
        """Find a free port to use for the mock server."""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(('localhost', 0))
            self.port = s.getsockname()[1]

    def configure(self, config: Dict[str, Any]) -> None:
        """Configure database tables and query responses."""
        super().configure(config)
        self.db_type = config.get('type', 'sqlite')
        self.tables = config.get('tables', {})
        self.queries = config.get('queries', {})

    def start(self) -> None:
        """Start the database mock server."""
        if self.db_type == 'sqlite':
            import sqlite3
            import tempfile

            # Create a temporary database file
            self.db_file = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
            self.db_path = self.db_file.name
            self.db_file.close()

            # Create connection
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()

            # Create tables
            for table_name, table_def in self.tables.items():
                # Create table
                columns = table_def.get('columns', [])
                if columns:
                    columns_sql = ', '.join(columns)
                    cursor.execute(f"CREATE TABLE {table_name} ({columns_sql})")

                    # Insert data if provided
                    data = table_def.get('data', [])
                    if data:
                        for row in data:
                            placeholders = ', '.join(['?'] * len(row))
                            cursor.execute(f"INSERT INTO {table_name} VALUES ({placeholders})", row)

            conn.commit()
            conn.close()

        logger.info(f"Started {self.db_type} database mock")
        super().start()

    def stop(self) -> None:
        """Stop the database mock server."""
        if self.db_type == 'sqlite' and self.db_path:
            # Remove temporary database file
            try:
                os.unlink(self.db_path)
            except:
                pass

        logger.info(f"Stopped {self.db_type} database mock")
        super().stop()

    def get_info(self) -> Dict[str, Any]:
        """Get information about the database mock."""
        info = super().get_info()
        info['type'] = self.db_type
        if self.db_type == 'sqlite':
            info['path'] = self.db_path if hasattr(self, 'db_path') else None
        else:
            info['url'] = f"{self.db_type}://localhost:{self.port}"
        info['tables'] = list(self.tables.keys())
        return info


class FilesystemMock(BaseMock):
    """Mock for filesystem operations."""

    def __init__(self, name: str, mocks_dir: Path):
        super().__init__(name, mocks_dir)
        self.temp_dir = None
        self.files = {}
        self.directories = []

    def configure(self, config: Dict[str, Any]) -> None:
        """Configure mock filesystem structure."""
        super().configure(config)
        self.files = config.get('files', {})
        self.directories = config.get('directories', [])

    def start(self) -> None:
        """Set up the mock filesystem."""
        # Create a temporary directory
        self.temp_dir = tempfile.mkdtemp()

        # Create directories
        for directory in self.directories:
            dir_path = os.path.join(self.temp_dir, directory)
            os.makedirs(dir_path, exist_ok=True)

        # Create files
        for file_path, content in self.files.items():
            full_path = os.path.join(self.temp_dir, file_path)

            # Ensure directory exists
            os.makedirs(os.path.dirname(full_path), exist_ok=True)

            # Write content
            with open(full_path, 'w') as f:
                if isinstance(content, (dict, list)):
                    json.dump(content, f, indent=2)
                else:
                    f.write(str(content))

        logger.info(f"Created filesystem mock at {self.temp_dir}")
        super().start()

    def stop(self) -> None:
        """Clean up the mock filesystem."""
        if self.temp_dir and os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)

        logger.info("Cleaned up filesystem mock")
        super().stop()

    def get_info(self) -> Dict[str, Any]:
        """Get information about the filesystem mock."""
        info = super().get_info()
        info['path'] = self.temp_dir
        info['files'] = list(self.files.keys())
        info['directories'] = self.directories
        return info


class CustomMock(BaseMock):
    """Custom mock using Python code."""

    def __init__(self, name: str, mocks_dir: Path):
        super().__init__(name, mocks_dir)
        self.module = None
        self.instance = None

    def configure(self, config: Dict[str, Any]) -> None:
        """Configure the custom mock."""
        super().configure(config)

        # Get mock implementation file
        mock_file = config.get('implementation')
        if not mock_file:
            mock_file = self.name + '.py'

        self.implementation_path = self.mocks_dir / 'custom' / mock_file

        # Check if file exists
        if not self.implementation_path.exists():
            raise FileNotFoundError(f"Custom mock implementation not found: {self.implementation_path}")

    def start(self) -> None:
        """Start the custom mock."""
        # Import the mock module
        spec = importlib.util.spec_from_file_location(
            f"custom_mock_{self.name}",
            self.implementation_path
        )
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)

        # Initialize the mock
        if hasattr(self.module, 'Mock'):
            self.instance = self.module.Mock(self.config)
            self.instance.start()
        else:
            raise ImportError(f"Custom mock {self.name} does not define a Mock class")

        logger.info(f"Started custom mock: {self.name}")
        super().start()

    def stop(self) -> None:
        """Stop the custom mock."""
        if self.instance:
            self.instance.stop()

        logger.info(f"Stopped custom mock: {self.name}")
        super().stop()

    def get_info(self) -> Dict[str, Any]:
        """Get information about the custom mock."""
        info = super().get_info()
        info['implementation'] = str(self.implementation_path)

        # Get additional info from the mock instance
        if self.instance and hasattr(self.instance, 'get_info'):
            info.update(self.instance.get_info())

        return info


def main():
    """Main entry point for command-line usage."""
    parser = argparse.ArgumentParser(description='n8n Workflow Testing Framework')

    parser.add_argument('--n8n-url', help='URL of the n8n instance')
    parser.add_argument('--n8n-api-key', help='API key for the n8n instance')
    parser.add_argument('--workflows-dir', help='Directory containing workflow files')
    parser.add_argument('--mocks-dir', help='Directory containing service mocks')
    parser.add_argument('--tests-dir', help='Directory containing test files')
    parser.add_argument('--output-dir', help='Directory for test reports')
    parser.add_argument('--config', help='Path to configuration file')
    parser.add_argument('--pattern', help='Pattern for test file discovery')
    parser.add_argument('--concurrency', type=int, help='Number of concurrent tests to run')
    parser.add_argument('--timeout', type=int, help='Timeout in seconds for workflow execution')
    parser.add_argument('--verbose', action='store_true', help='Enable verbose output')

    args = parser.parse_args()

    # Initialize framework
    tester = N8nWorkflowTester(
        n8n_url=args.n8n_url,
        n8n_api_key=args.n8n_api_key,
        workflows_dir=args.workflows_dir,
        mocks_dir=args.mocks_dir,
        tests_dir=args.tests_dir,
        output_dir=args.output_dir,
        config_file=args.config,
        concurrency=args.concurrency,
        timeout=args.timeout,
        verbose=args.verbose
    )

    # Discover and run tests
    test_files = tester.discover_tests(args.pattern)
    results = tester.run_tests(test_files)

    # Exit with status code based on test results
    success = (
        results['summary']['failed'] == 0 and
        results['summary']['errors'] == 0
    )
    sys.exit(0 if success else 1)


if __name__ == '__main__':
    main()

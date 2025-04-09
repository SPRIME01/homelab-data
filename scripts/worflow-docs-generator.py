#!/usr/bin/env python3

import os
import json
import re
import argparse
import datetime
import shutil
import logging
from pathlib import Path
from typing import Dict, List, Optional, Any, Set, Tuple
import subprocess
import base64
from collections import defaultdict

# Try to import optional dependencies
try:
    import graphviz
    GRAPHVIZ_AVAILABLE = True
except ImportError:
    GRAPHVIZ_AVAILABLE = False

try:
    import pymdown_extensions
    PYMDOWN_AVAILABLE = True
except ImportError:
    PYMDOWN_AVAILABLE = False

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("workflow-docs-generator")

# Define constants
DEFAULT_WORKFLOWS_DIR = "/home/sprime01/homelab/homelab-data/n8n/workflows"
DEFAULT_OUTPUT_DIR = "/home/sprime01/homelab/homelab-data/docs/workflows"
DEFAULT_TEMPLATE_DIR = "/home/sprime01/homelab/homelab-data/templates"
DEFAULT_ASSETS_DIR = "assets"
NODE_TYPE_ICONS = {
    "n8n-nodes-base.httpRequest": "🌐",
    "n8n-nodes-base.function": "🔧",
    "n8n-nodes-base.if": "🔀",
    "n8n-nodes-rabbitmq-enhanced.rabbitMqEnhanced": "🐇",
    "n8n-nodes-triton.triton": "🧠",
    "n8n-nodes-base.set": "📝",
    "n8n-nodes-base.switch": "🔄",
    "n8n-nodes-base.merge": "🔗",
    "n8n-nodes-base.scheduleTrigger": "⏰",
    "n8n-nodes-base.webhook": "📡",
    "n8n-nodes-base.executeCommand": "🖥️",
    "default": "📦"
}

class WorkflowDocsGenerator:
    def __init__(
        self,
        workflows_dir: str,
        output_dir: str,
        template_dir: Optional[str] = None,
        assets_dir: Optional[str] = None,
        generate_diagrams: bool = True,
        include_parameters: bool = True,
        categories_file: Optional[str] = None
    ):
        self.workflows_dir = Path(workflows_dir)
        self.output_dir = Path(output_dir)
        self.template_dir = Path(template_dir) if template_dir else None
        self.assets_dir = assets_dir or DEFAULT_ASSETS_DIR
        self.assets_path = Path(output_dir) / self.assets_dir
        self.generate_diagrams = generate_diagrams and GRAPHVIZ_AVAILABLE
        self.include_parameters = include_parameters
        self.categories = self._load_categories(categories_file) if categories_file else {}

        # Ensure output directory exists
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.assets_path.mkdir(parents=True, exist_ok=True)

        # Load templates
        self._load_templates()

    def _load_categories(self, categories_file: str) -> Dict[str, str]:
        """Load workflow categories from a JSON file."""
        try:
            with open(categories_file, 'r') as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"Could not load categories file: {e}")
            return {}

    def _load_templates(self) -> None:
        """Load template files for documentation generation."""
        self.workflow_template = """# {name}

{tags_section}

## Description
{description}

## Overview
{overview}

## Workflow Diagram
{diagram_section}

## Nodes
{nodes_section}

## Connections
{connections_section}

## Execution
{execution_section}

## Dependencies
{dependencies_section}

## Additional Notes
{notes_section}

## Last Updated
{last_updated}
"""
        self.index_template = """# n8n Workflows Documentation

This directory contains documentation for all n8n workflows in the homelab environment.

## Categories
{categories_section}

## All Workflows
{workflows_section}

## Statistics
- Total workflows: {total_workflows}
- Total nodes: {total_nodes}
- Total connections: {total_connections}
- Categories: {category_count}

## Last Generated
{last_generated}
"""

        # If template directory exists, try to load custom templates
        if self.template_dir and self.template_dir.exists():
            try:
                workflow_template_path = self.template_dir / "workflow.md.template"
                if workflow_template_path.exists():
                    with open(workflow_template_path, 'r') as f:
                        self.workflow_template = f.read()

                index_template_path = self.template_dir / "index.md.template"
                if index_template_path.exists():
                    with open(index_template_path, 'r') as f:
                        self.index_template = f.read()
            except Exception as e:
                logger.warning(f"Could not load custom templates: {e}")

    def find_workflows(self) -> List[Path]:
        """Find all workflow JSON files recursively."""
        if not self.workflows_dir.exists():
            logger.error(f"Workflows directory does not exist: {self.workflows_dir}")
            return []

        workflow_files = []
        for path in self.workflows_dir.rglob("*.json"):
            if path.is_file():
                workflow_files.append(path)

        logger.info(f"Found {len(workflow_files)} workflow files")
        return workflow_files

    def parse_workflow(self, workflow_path: Path) -> Dict[str, Any]:
        """Parse a workflow JSON file and extract relevant information."""
        try:
            with open(workflow_path, 'r') as f:
                workflow_data = json.load(f)

            # Extract basic information
            workflow_info = {
                "name": workflow_data.get("name", "Unnamed Workflow"),
                "path": workflow_path,
                "id": workflow_path.stem,
                "relative_path": workflow_path.relative_to(self.workflows_dir),
                "tags": workflow_data.get("tags", []),
                "nodes": workflow_data.get("nodes", []),
                "connections": workflow_data.get("connections", {}),
                "settings": workflow_data.get("settings", {}),
                "active": workflow_data.get("active", False),
                "node_count": len(workflow_data.get("nodes", [])),
                "connection_count": sum(len(connections) for node_connections in workflow_data.get("connections", {}).values()
                                     for connections in node_connections.values()),
                "trigger_nodes": [],
                "node_types": set(),
                "staticData": workflow_data.get("staticData", {})
            }

            # Extract trigger nodes
            for node in workflow_info["nodes"]:
                node_type = node.get("type", "")
                workflow_info["node_types"].add(node_type)

                # Check if it's a trigger node (based on common trigger node types)
                if any(trigger_type in node_type.lower() for trigger_type in ["trigger", "webhook", "polling"]):
                    workflow_info["trigger_nodes"].append(node)

            # Determine category from tags or directory structure
            category = "Uncategorized"
            if workflow_info["id"] in self.categories:
                category = self.categories[workflow_info["id"]]
            elif workflow_info["tags"]:
                # Use the first tag as category
                category = workflow_info["tags"][0].capitalize()
            elif len(workflow_info["relative_path"].parts) > 1:
                # Use parent directory as category
                category = workflow_info["relative_path"].parts[0].capitalize()

            workflow_info["category"] = category

            return workflow_info

        except Exception as e:
            logger.error(f"Error parsing workflow {workflow_path}: {e}")
            return {}

    def generate_diagram(self, workflow_info: Dict[str, Any]) -> Optional[str]:
        """Generate a visual diagram of the workflow using graphviz."""
        if not self.generate_diagrams or not GRAPHVIZ_AVAILABLE:
            return None

        try:
            # Create a new directed graph
            dot = graphviz.Digraph(
                comment=f"Workflow: {workflow_info['name']}",
                format='png',
                engine='dot',
                graph_attr={'rankdir': 'LR', 'splines': 'polyline', 'nodesep': '0.8', 'ranksep': '1.0'},
                node_attr={'shape': 'box', 'style': 'rounded,filled', 'fontname': 'Arial', 'fontsize': '10', 'height': '0.4'},
                edge_attr={'fontname': 'Arial', 'fontsize': '9'}
            )

            # Add nodes to the graph
            node_map = {}
            for node in workflow_info["nodes"]:
                node_id = node.get("name", "Unknown")
                node_type = node.get("type", "unknown")
                node_map[node_id] = node

                # Get node icon based on type
                icon = NODE_TYPE_ICONS.get(node_type, NODE_TYPE_ICONS["default"])

                # Determine node color based on type
                if any(trigger_type in node_type.lower() for trigger_type in ["trigger", "webhook", "polling"]):
                    color = "#5BA0D0"  # Blue for trigger nodes
                elif "function" in node_type.lower():
                    color = "#FFCC66"  # Yellow for function nodes
                elif "if" in node_type.lower() or "switch" in node_type.lower():
                    color = "#CC99FF"  # Purple for conditional nodes
                elif "http" in node_type.lower():
                    color = "#66CC99"  # Green for HTTP nodes
                else:
                    color = "#E8E8E8"  # Gray for other nodes

                dot.node(node_id, f"{icon} {node_id}", fillcolor=color)

            # Add connections to the graph
            for source_node, targets in workflow_info["connections"].items():
                if source_node not in node_map:
                    continue

                for connection_type, connections in targets.items():
                    for connection in connections:
                        target_node = connection.get("node")
                        if target_node not in node_map:
                            continue

                        # Label based on connection type
                        if connection_type == "main":
                            label = ""
                        else:
                            label = connection_type

                        dot.edge(source_node, target_node, label=label)

            # Render the graph to a file
            workflow_id = workflow_info["id"]
            output_file = f"{workflow_id}_diagram"
            diagram_path = self.assets_path / output_file
            dot.render(str(diagram_path), cleanup=True)

            return f"{self.assets_dir}/{output_file}.png"

        except Exception as e:
            logger.error(f"Error generating diagram for {workflow_info['name']}: {e}")
            return None

    def document_node(self, node: Dict[str, Any]) -> str:
        """Generate documentation for a single node."""
        node_name = node.get("name", "Unknown")
        node_type = node.get("type", "unknown")
        node_type_name = node_type.split(".")[-1] if "." in node_type else node_type

        # Get node icon
        icon = NODE_TYPE_ICONS.get(node_type, NODE_TYPE_ICONS["default"])

        # Start with basic node information
        doc = f"### {icon} {node_name}\n\n"
        doc += f"**Type**: `{node_type_name}`\n\n"

        # Include position for layout information
        position = node.get("position", {})
        if position:
            doc += f"**Position**: x={position.get('x', 0)}, y={position.get('y', 0)}\n\n"

        # Include parameters if specified
        if self.include_parameters and "parameters" in node:
            params = node["parameters"]

            # Sanitize parameters for display (remove credentials and large values)
            safe_params = {}
            for key, value in params.items():
                if key.lower() in ("credentials", "auth", "token", "password", "secret", "key"):
                    safe_params[key] = "**REDACTED**"
                elif isinstance(value, str) and len(value) > 100:
                    safe_params[key] = f"{value[:100]}... (truncated)"
                else:
                    safe_params[key] = value

            doc += "**Parameters**:\n```json\n"
            doc += json.dumps(safe_params, indent=2)
            doc += "\n```\n\n"

        # Add notes if present
        if "notes" in node:
            doc += f"**Notes**: {node['notes']}\n\n"

        return doc

    def document_connections(self, workflow_info: Dict[str, Any]) -> str:
        """Generate documentation for workflow connections."""
        if not workflow_info["connections"]:
            return "No connections defined in this workflow."

        connections_doc = ""

        for source_node, targets in workflow_info["connections"].items():
            connections_doc += f"### From: {source_node}\n\n"

            for connection_type, connections in targets.items():
                connections_doc += f"- **{connection_type}**:\n"

                for connection in connections:
                    target_node = connection.get("node", "Unknown")
                    connections_doc += f"  - → To: **{target_node}**"

                    if "index" in connection:
                        connections_doc += f" (index: {connection['index']})"

                    connections_doc += "\n"

            connections_doc += "\n"

        return connections_doc

    def generate_workflow_doc(self, workflow_info: Dict[str, Any]) -> str:
        """Generate full documentation for a workflow."""
        # Generate diagram if enabled
        diagram_path = self.generate_diagram(workflow_info)
        diagram_section = f"![Workflow Diagram]({diagram_path})" if diagram_path else "Diagram generation is disabled or failed."

        # Generate tags section
        tags = workflow_info.get("tags", [])
        tags_section = ""
        if tags:
            tags_section = "Tags: " + ", ".join([f"`{tag}`" for tag in tags])

        # Generate description
        description = f"Workflow ID: `{workflow_info['id']}`\n\n"
        description += f"Category: **{workflow_info['category']}**\n\n"

        # Extract an overview (this could be enhanced if workflows have a description field)
        overview = f"This workflow contains {workflow_info['node_count']} nodes and {workflow_info['connection_count']} connections.\n\n"

        # Add trigger information
        if workflow_info["trigger_nodes"]:
            overview += "### Triggers\n\n"
            for trigger in workflow_info["trigger_nodes"]:
                trigger_name = trigger.get("name", "Unknown")
                trigger_type = trigger.get("type", "unknown").split(".")[-1]
                overview += f"- **{trigger_name}** ({trigger_type})\n"
            overview += "\n"

        # Generate nodes documentation
        nodes_section = ""
        for node in workflow_info["nodes"]:
            nodes_section += self.document_node(node)

        # Generate connections documentation
        connections_section = self.document_connections(workflow_info)

        # Generate execution section
        execution_section = "### Execution Settings\n\n"
        settings = workflow_info.get("settings", {})

        if settings:
            execution_order = settings.get("executionOrder", "unknown")
            save_executions = settings.get("saveManualExecutions", False)
            caller_policy = settings.get("callerPolicy", "unknown")
            error_workflow = settings.get("errorWorkflow", "None")

            execution_section += f"- **Execution Order**: {execution_order}\n"
            execution_section += f"- **Save Manual Executions**: {save_executions}\n"
            execution_section += f"- **Caller Policy**: {caller_policy}\n"
            execution_section += f"- **Error Workflow**: {error_workflow}\n"
        else:
            execution_section += "No specific execution settings defined."

        # Generate dependencies section
        dependencies_section = "### Required Components\n\n"
        node_types = workflow_info["node_types"]

        # Check for specific dependencies
        dependencies = set()
        if any("rabbitmq" in nt.lower() for nt in node_types):
            dependencies.add("RabbitMQ Message Broker")
        if any("triton" in nt.lower() for nt in node_types):
            dependencies.add("Triton Inference Server")
        if any("http" in nt.lower() for nt in node_types):
            dependencies.add("HTTP API Access")
        if any("homeassistant" in nt.lower() for nt in node_types):
            dependencies.add("Home Assistant")

        if dependencies:
            dependencies_section += "This workflow requires the following components:\n\n"
            for dep in dependencies:
                dependencies_section += f"- {dep}\n"
        else:
            dependencies_section += "No external dependencies identified."

        # Generate notes section
        notes_section = ""

        # Fill the template
        doc = self.workflow_template.format(
            name=workflow_info["name"],
            tags_section=tags_section,
            description=description,
            overview=overview,
            diagram_section=diagram_section,
            nodes_section=nodes_section,
            connections_section=connections_section,
            execution_section=execution_section,
            dependencies_section=dependencies_section,
            notes_section=notes_section,
            last_updated=datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        )

        return doc

    def generate_index(self, workflows: List[Dict[str, Any]]) -> str:
        """Generate an index page with links to all workflow documentation."""
        # Group workflows by category
        categories = defaultdict(list)
        for workflow in workflows:
            category = workflow["category"]
            categories[category].append(workflow)

        # Generate categories section
        categories_section = ""
        for category, category_workflows in sorted(categories.items()):
            categories_section += f"### {category}\n\n"
            for workflow in sorted(category_workflows, key=lambda w: w["name"]):
                tags_str = " ".join([f"`{tag}`" for tag in workflow.get("tags", [])])
                categories_section += f"- [{workflow['name']}]({workflow['id']}.md) - {workflow['node_count']} nodes {tags_str}\n"
            categories_section += "\n"

        # Generate alphabetical list of all workflows
        workflows_section = "### Alphabetical List\n\n"
        for workflow in sorted(workflows, key=lambda w: w["name"]):
            workflows_section += f"- [{workflow['name']}]({workflow['id']}.md) - {workflow['category']}\n"

        # Calculate statistics
        total_workflows = len(workflows)
        total_nodes = sum(w["node_count"] for w in workflows)
        total_connections = sum(w["connection_count"] for w in workflows)
        category_count = len(categories)

        # Fill the template
        index = self.index_template.format(
            categories_section=categories_section,
            workflows_section=workflows_section,
            total_workflows=total_workflows,
            total_nodes=total_nodes,
            total_connections=total_connections,
            category_count=category_count,
            last_generated=datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        )

        return index

    def generate_docs(self) -> None:
        """Main method to generate all documentation."""
        logger.info(f"Starting workflow documentation generation from {self.workflows_dir}")

        # Find all workflow files
        workflow_files = self.find_workflows()
        if not workflow_files:
            logger.error("No workflow files found. Exiting.")
            return

        # Parse all workflows
        workflows = []
        for workflow_file in workflow_files:
            workflow_info = self.parse_workflow(workflow_file)
            if workflow_info:
                workflows.append(workflow_info)

        if not workflows:
            logger.error("No valid workflows found. Exiting.")
            return

        logger.info(f"Parsed {len(workflows)} valid workflows")

        # Generate documentation for each workflow
        for workflow in workflows:
            logger.info(f"Generating documentation for {workflow['name']}")
            doc = self.generate_workflow_doc(workflow)

            # Write to file
            output_file = self.output_dir / f"{workflow['id']}.md"
            with open(output_file, 'w') as f:
                f.write(doc)

            logger.info(f"Documentation written to {output_file}")

        # Generate index
        logger.info("Generating index page")
        index = self.generate_index(workflows)
        index_file = self.output_dir / "README.md"
        with open(index_file, 'w') as f:
            f.write(index)

        logger.info(f"Index written to {index_file}")
        logger.info("Documentation generation complete")


def main():
    parser = argparse.ArgumentParser(description='Generate documentation for n8n workflows')
    parser.add_argument('--workflows-dir', default=DEFAULT_WORKFLOWS_DIR,
                        help=f'Directory containing n8n workflow JSON files (default: {DEFAULT_WORKFLOWS_DIR})')
    parser.add_argument('--output-dir', default=DEFAULT_OUTPUT_DIR,
                        help=f'Output directory for documentation (default: {DEFAULT_OUTPUT_DIR})')
    parser.add_argument('--template-dir', default=None,
                        help='Directory containing custom template files')
    parser.add_argument('--assets-dir', default=DEFAULT_ASSETS_DIR,
                        help=f'Directory for generated assets (relative to output dir) (default: {DEFAULT_ASSETS_DIR})')
    parser.add_argument('--no-diagrams', action='store_true',
                        help='Disable diagram generation')
    parser.add_argument('--no-parameters', action='store_true',
                        help='Exclude node parameters from documentation')
    parser.add_argument('--categories-file', default=None,
                        help='JSON file mapping workflow IDs to categories')
    parser.add_argument('--verbose', '-v', action='store_true',
                        help='Enable verbose logging')

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # Check if graphviz is available
    if not GRAPHVIZ_AVAILABLE and not args.no_diagrams:
        logger.warning("Graphviz not available. Install with 'pip install graphviz' for diagram generation.")

    # Initialize and run the generator
    generator = WorkflowDocsGenerator(
        workflows_dir=args.workflows_dir,
        output_dir=args.output_dir,
        template_dir=args.template_dir,
        assets_dir=args.assets_dir,
        generate_diagrams=not args.no_diagrams,
        include_parameters=not args.no_parameters,
        categories_file=args.categories_file
    )

    generator.generate_docs()


if __name__ == "__main__":
    main()

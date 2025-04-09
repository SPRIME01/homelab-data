#!/usr/bin/env python3

import json
import os
import sys
from pathlib import Path
import jsonschema

# n8n workflow schema - simplified version
WORKFLOW_SCHEMA = {
    "type": "object",
    "required": ["name", "nodes", "connections"],
    "properties": {
        "name": {"type": "string"},
        "nodes": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["parameters", "name", "type"],
            }
        },
        "connections": {"type": "object"},
        "settings": {"type": "object"},
        "tags": {
            "type": "array",
            "items": {"type": "string"}
        }
    }
}

def validate_workflow(file_path):
    """Validate a single workflow file against the schema."""
    try:
        with open(file_path, 'r') as f:
            workflow = json.load(f)
        jsonschema.validate(instance=workflow, schema=WORKFLOW_SCHEMA)
        print(f"✓ {file_path} - Valid")
        return True
    except jsonschema.exceptions.ValidationError as e:
        print(f"✗ {file_path} - Invalid: {e.message}")
        return False
    except json.JSONDecodeError as e:
        print(f"✗ {file_path} - Invalid JSON: {e}")
        return False

def main(workflows_dir):
    """Validate all workflow files in the directory."""
    workflow_path = Path(workflows_dir)
    if not workflow_path.exists():
        print(f"Error: Directory {workflows_dir} does not exist")
        sys.exit(1)

    valid = True
    for file_path in workflow_path.glob('**/*.json'):
        if not validate_workflow(file_path):
            valid = False

    if not valid:
        sys.exit(1)

if __name__ == '__main__':
    if len(sys.argv) != 2:
        print("Usage: validate_workflows.py <workflows_directory>")
        sys.exit(1)
    main(sys.argv[1])

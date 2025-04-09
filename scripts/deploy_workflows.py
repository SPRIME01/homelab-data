#!/usr/bin/env python3

import argparse
import json
import os
import requests
from pathlib import Path
import time

class N8nDeployer:
    def __init__(self, base_url, api_key):
        self.base_url = base_url.rstrip('/')
        self.headers = {
            'X-N8N-API-KEY': api_key,
            'Content-Type': 'application/json'
        }

    def get_existing_workflows(self):
        """Get all existing workflows from n8n instance."""
        response = requests.get(
            f"{self.base_url}/api/v1/workflows",
            headers=self.headers
        )
        response.raise_for_status()
        return {w['name']: w['id'] for w in response.json()}

    def deploy_workflow(self, workflow_data, existing_id=None):
        """Deploy a workflow, either creating new or updating existing."""
        if existing_id:
            url = f"{self.base_url}/api/v1/workflows/{existing_id}"
            method = requests.put
        else:
            url = f"{self.base_url}/api/v1/workflows"
            method = requests.post

        response = method(url, headers=self.headers, json=workflow_data)
        response.raise_for_status()
        return response.json()

    def activate_workflow(self, workflow_id):
        """Activate a workflow."""
        url = f"{self.base_url}/api/v1/workflows/{workflow_id}/activate"
        response = requests.post(url, headers=self.headers)
        response.raise_for_status()
        return response.json()

def main():
    parser = argparse.ArgumentParser(description='Deploy n8n workflows')
    parser.add_argument('--environment', required=True, choices=['staging', 'production'])
    args = parser.parse_args()

    # Get environment variables
    n8n_url = os.environ.get('N8N_URL')
    n8n_api_key = os.environ.get('N8N_API_KEY')

    if not all([n8n_url, n8n_api_key]):
        print("Error: Missing required environment variables")
        exit(1)

    deployer = N8nDeployer(n8n_url, n8n_api_key)
    existing_workflows = deployer.get_existing_workflows()

    # Deploy workflows
    workflows_dir = Path('homelab-data/n8n/workflows')
    for workflow_file in workflows_dir.glob('**/*.json'):
        print(f"Processing {workflow_file}...")

        with open(workflow_file) as f:
            workflow_data = json.load(f)

        try:
            workflow_name = workflow_data['name']
            existing_id = existing_workflows.get(workflow_name)

            # Deploy the workflow
            result = deployer.deploy_workflow(workflow_data, existing_id)
            print(f"Successfully deployed {workflow_name}")

            # Activate if needed
            if workflow_data.get('active', False):
                deployer.activate_workflow(result['id'])
                print(f"Activated workflow {workflow_name}")

            # Add small delay between deployments
            time.sleep(1)

        except Exception as e:
            print(f"Error deploying {workflow_file}: {e}")
            exit(1)

if __name__ == '__main__':
    main()

# 🔒 Homelab Data Integration Repository

Welcome to the data integration repository! This repository manages configurations, scripts, and workflows for a comprehensive homelab data mesh. It facilitates communication and data processing between various services like Home Assistant, AI inference servers, metrics collectors, and more, primarily leveraging RabbitMQ as a message broker.

## 📋 Contents
- RabbitMQ topology configurations (`rabbitmq/`)
- n8n workflows and custom nodes (`n8n/`)
- Python-based service integration scripts (`integrations/`)
- Data transformation pipelines and custom transformers (`transformations/`)
- JSON schemas for data validation (`schemas/`)
- Test configurations and scripts (`tests/`)
- Project documentation (`docs/`)

## 🚨 Critical Security Warning 🚨

A **hardcoded encryption key** has been identified in the `n8n-nodes-rabbitmq-enhanced` custom n8n node (specifically within its `MessageTransformer` helper class, if used for encryption).

**Risk:** If this key is not changed, any messages encrypted by this node can be decrypted by anyone with access to this codebase, potentially exposing sensitive information.

**Action Required:** Users **MUST** change this hardcoded key before using the encryption feature of the `n8n-nodes-rabbitmq-enhanced` node in any environment, especially if processing sensitive data. Future versions of this node should externalize this key to a secure credential store.

## 🛠️ Setup

Setting up and running this project involves multiple components and services.

1.  **Clone this repository.**
2.  **Core Service Dependencies:** Ensure the following services are installed, configured, and accessible on your network:
    *   RabbitMQ (Message Broker)
    *   Home Assistant (Smart Home Hub)
    *   Triton Inference Server (AI Model Serving)
    *   InfluxDB (Time-series Database)
    *   MinIO (Object Storage)
    *   n8n (Workflow Automation)
3.  **Configuration Files:** Many components rely on YAML configuration files (e.g., within `integrations/` subdirectories, `transformations/`, `tests/`). Where applicable, copy any `.example` files to their active names (e.g., `config.yaml.example` to `config.yaml`) and customize them according to your environment.
4.  **Environment Variables:** Configure the necessary environment variables. These are crucial for service operation and testing.
    *   `RABBITMQ_USERNAME`: Username for RabbitMQ.
    *   `RABBITMQ_PASSWORD`: Password for RabbitMQ.
    *   `TEST_CONFIG`: Absolute path to the `config.json` file required by some integration tests (e.g., `tests/integration-tests.py`). This file's structure would need to be derived from `tests/integration-config.yaml` or other sources.
    *   Service-specific `CONFIG_PATH`: Some Python services (e.g., in `integrations/`) might look for a `CONFIG_PATH` environment variable to locate their respective `config.yaml` files.
5.  **Python Dependencies:** Install required Python packages for scripts in `integrations/`, `transformations/`, `scripts/`, and `tests/`. Virtual environments are recommended. (A comprehensive `requirements.txt` or per-component requirements should be added).
6.  **n8n Custom Nodes:** If using the custom n8n nodes, they need to be built and installed into your n8n instance. Refer to n8n documentation for installing custom nodes.
7.  **Start your services and scripts.** (Detailed operational guides are currently a documentation gap).

## 📁 Repository Structure
```
├── docs/            # Project documentation, diagrams
├── integrations/    # Python services for connecting to external systems (Home Assistant, InfluxDB, etc.)
├── n8n/             # n8n workflow files and custom node definitions
│   ├── custom-nodes/  # Source code for custom n8n nodes
│   └── workflows/     # Exported n8n workflow JSON files
├── rabbitmq/        # RabbitMQ topology configurations (exchanges, queues, bindings)
├── schemas/         # JSON schemas for data validation and the Python validation script
├── scripts/         # Utility and helper scripts
├── tests/           # Test configurations and scripts (consumer, integration, etc.)
├── transformations/ # Custom data transformation pipeline and transformer definitions
├── .gitignore       # Specifies intentionally untracked files
├── LICENSE          # Project license
└── README.md        # This file
```
*(Note: `data/` directory for local data is mentioned in the old README but should be gitignored and managed locally by the user.)*

## 🧩 Key Components Overview

*   **`integrations/`**: Contains Python-based services that act as connectors or consumers for various parts of the data mesh (e.g., listening to Home Assistant events, consuming data for InfluxDB, archiving to MinIO, handling Triton inference communication).
*   **`transformations/`**: Houses a custom data transformation pipeline (`pipeline.py`) and individual transformer components (`transformers.py`). This allows for flexible processing and modification of data flowing through the system.
*   **`schemas/`**: Defines JSON schemas used for validating data structures across the project. Includes a Python script (`validation.py`) for performing these validations.
*   **`n8n/custom-nodes/`**: Provides custom nodes for n8n to enhance its capabilities for interacting with project-specific services or implementing unique logic within n8n workflows.

## ⚙️ Configuration
Detailed configuration instructions for each component are currently a work in progress and should be added to the `docs/` directory or within the respective component's subdirectory. Key aspects to configure typically include:
*   Service endpoints (e.g., RabbitMQ host, Home Assistant URL).
*   Credentials for accessing services.
*   Specific parameters for transformations, integrations, or n8n nodes.

## 🧪 Testing

The `tests/` directory contains various test suites, including:
*   Consumer tests (e.g., `tests/consumer-tests.py` with `tests/consumer-tests-config.yaml`).
*   Integration tests (e.g., `tests/integration-tests.py` which expects a `config.json` potentially derived from `tests/integration-config.yaml` and specified via `TEST_CONFIG` environment variable).
*   Message flow tests.
*   Producer tests.

**Note:** Comprehensive documentation on how to set up the test environment, configure test-specific files (like the format of `config.json`), and run these tests is currently **missing** and needs to be created. Ensure required environment variables like `RABBITMQ_USERNAME`, `RABBITMQ_PASSWORD`, and `TEST_CONFIG` are set before running tests.

## 🤝 Contributing
1. Create a feature branch.
2. Make your changes.
3. Add or update documentation relevant to your changes.
4. Add tests for new features or bug fixes.
5. Ensure your code passes any linting or pre-commit checks (a `.pre-commit-config.yaml` exists).
6. Submit a pull request.

## 📝 License
MIT License - See LICENSE file for details.

## ⚠️ General Security Note
Ensure all sensitive data (credentials, API keys, etc.) is managed securely, preferably through environment variables, dedicated secrets management systems, or n8n's credential management. Avoid committing sensitive information directly to the repository. Regularly review `.gitignore` to prevent accidental commits.

# n8n-nodes-triton

This is a custom n8n node for integrating with NVIDIA Triton Inference Server. It allows you to:

1. Run inference on machine learning models hosted on Triton
2. Get model information and metadata
3. Check server health
4. Get server capabilities and available models

## Installation

### Local Installation (Recommended for Homelab)

1. Go to your n8n installation folder
2. Install the package:
   ```
   npm install /path/to/homelab-data/n8n/custom-nodes/n8n-nodes-triton
   ```
3. Start or restart n8n

### Global Installation

```
npm install -g n8n-nodes-triton
```

## Usage

After installation, you'll have access to the "Triton Inference Server" node in n8n.

### Authentication

1. Create a credential for "Triton API"
2. Enter the server URL (e.g., http://triton-inference-server:8000)
3. Optionally add an API key if your Triton server requires authentication

### Operations

#### Run Inference

Send data to a Triton model for inference:

1. Select the model name from your Triton server
2. Specify model version (optional)
3. Configure input and output tensor names
4. Choose the appropriate data type for your model
5. Provide input shape if needed (or let it be determined automatically)
6. Configure advanced options like batching and timeout
7. Add post-processing functions if needed

#### Get Model Information

Retrieve detailed information about a model:

1. Select the model name
2. Specify version (optional)

#### Check Server Health

Verify that the Triton server is operational.

#### Get Server Metadata

Retrieve information about server capabilities and configuration.

## Advanced Features

### Batching

You can batch multiple inputs together for efficiency:

1. Configure the "Batch Size" option
2. Provide an array of inputs in your data field

### Data Pre-processing and Post-processing

The node supports:

1. Automatic data type conversion
2. Reshaping of input and output data
3. Custom post-processing via JavaScript functions

### Error Handling

The node provides detailed error messages for troubleshooting, including:

1. Connection issues
2. Data format problems
3. Model-specific errors

## Examples

### Basic Inference Example

```json
{
  "nodes": [
    {
      "parameters": {
        "operation": "inference",
        "model": "resnet50",
        "inputDataField": "image",
        "inputName": "input",
        "outputName": "output",
        "inputDataType": "FP32",
        "options": {
          "timeout": 30000
        }
      },
      "name": "Triton",
      "type": "n8n-nodes-triton.triton",
      "typeVersion": 1,
      "position": [
        620,
        300
      ]
    }
  ]
}
```

## Troubleshooting

- **Connection Issues**: Verify the server URL and network connectivity
- **Authentication Errors**: Check API key if authentication is enabled
- **Data Format Errors**: Ensure your input data matches the expected format for the model
- **Timeout Errors**: Increase the timeout value for complex models

## License

MIT

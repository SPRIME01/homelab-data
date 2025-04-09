# n8n-nodes-rabbitmq-enhanced

This package provides enhanced RabbitMQ integration for n8n with advanced messaging features that go beyond the built-in RabbitMQ node capabilities.

## Features

### 1. Advanced Message Routing
- Support for dynamic routing keys with expression-based routing
- Multiple exchange type support (direct, topic, headers, fanout)
- Conditional routing based on message content

### 2. Header-Based Filtering
- Filter messages based on headers with various match types:
  - Equals, Contains, Begins With, Ends With, Regex
- Multiple header filters can be combined
- Support for application-specific headers

### 3. Batch Publishing
- Publish multiple messages in a single operation
- Configurable batch sizes
- Individual routing keys per message in batch

### 4. Message Transformation
- Compression (gzip) support for large messages
- End-to-end encryption with AES-256-CBC
- Custom transformation functions with JavaScript
- Schema validation for message content

### 5. Dead Letter Handling
- Automatic configuration of dead letter exchanges
- Custom routing keys for rejected messages
- Configurable message TTL and expiration

### 6. Priority Queue Support
- Message priority levels (0-9)
- Queue max priority configuration
- Priority consumer setup

## Installation

### Local Installation

```bash
cd /path/to/n8n
npm install /home/sprime01/homelab/homelab-data/n8n/custom-nodes/n8n-nodes-rabbitmq-enhanced
```

### Global Installation

```bash
npm install -g /home/sprime01/homelab/homelab-data/n8n/custom-nodes/n8n-nodes-rabbitmq-enhanced
```

## Usage

### Connection Setup

1. Create a new credential of type "RabbitMQ Enhanced"
2. Enter your RabbitMQ connection details:
   - Host and port
   - Username and password
   - Virtual host
   - Optional TLS/SSL settings

### Basic Publishing

The simplest way to publish a message:

1. Add the "RabbitMQ Enhanced" node
2. Select operation "Publish" or "Publish to Queue"
3. Enter exchange or queue name
4. Enter message content
5. Configure optional settings
6. Connect to your workflow

### Advanced Features

#### Message Transformation

To transform messages before sending:

1. Enable "Apply Transformation" option
2. Select transformation type:
   - Compress: Reduces message size with gzip compression
   - Encrypt: Secures message content with AES-256 encryption
   - Transform with Function: Apply custom JavaScript transformation
   - Schema Validation: Validate against JSON schema

Example transformation function:
```javascript
// Input is the message content
const enriched = {
  ...input,
  timestamp: new Date().toISOString(),
  source: "n8n",
  version: "1.0"
};
return enriched;
```

#### Batch Publishing

To publish multiple messages at once:

1. Select "Batch Publish" operation
2. Specify the field containing an array of messages
3. Configure options like content type and persistence
4. Each array item becomes a separate message

#### Header-Based Message Filtering

When consuming messages:

1. Enable "Header Filtering" in consume options
2. Add header filters with name, value, and match type
3. Only messages matching all filters will be processed

## API Reference

### Operations

| Operation | Description |
|-----------|-------------|
| Publish | Send a message to an exchange |
| Publish to Queue | Send a message directly to a queue |
| Batch Publish | Send multiple messages at once |
| Consume | Receive messages from a queue |
| Purge Queue | Remove all messages from a queue |
| Assert Exchange | Create or verify an exchange exists |
| Assert Queue | Create or verify a queue exists |
| Delete Queue | Remove a queue |
| Bind Queue | Connect a queue to an exchange |

### Options

Common options available for most operations:

- **Content Type**: MIME type of the message (default: application/json)
- **Persistent**: Whether messages survive broker restarts
- **Priority**: Message importance (0-9, higher = more important)
- **Expiration**: Time-to-live in milliseconds
- **Custom Headers**: Key-value pairs for message metadata

## Comparison with Built-in Node

| Feature | Built-in RabbitMQ | RabbitMQ Enhanced |
|---------|-------------------|-------------------|
| Publish | ✓ | ✓ |
| Consume | ✓ | ✓ |
| Message Headers | Basic | Advanced with filtering |
| Batch Operations | ✗ | ✓ |
| Message Transformation | ✗ | ✓ |
| Dead Letter Support | ✗ | ✓ |
| Priority Queues | ✗ | ✓ |
| SSL/TLS Support | ✓ | ✓ |
| Dynamic Routing | ✗ | ✓ |

## Examples

### IoT Data Processing

```json
{
  "nodes": [
    {
      "parameters": {
        "operation": "consume",
        "queue": "sensor-data",
        "consumeOptions": {
          "headerFiltering": true,
          "headerFilters": {
            "filter": [
              {
                "name": "device-type",
                "value": "temperature",
                "matchType": "equals"
              }
            ]
          }
        }
      },
      "name": "Get Temperature Data",
      "type": "n8n-nodes-rabbitmq-enhanced.rabbitMqEnhanced",
      "position": [400, 300]
    }
  ]
}
```

### Secure Message Publishing

```json
{
  "nodes": [
    {
      "parameters": {
        "operation": "publish",
        "exchange": "secure-exchange",
        "routingKey": "confidential",
        "content": "={{$json.sensitiveData}}",
        "applyTransformation": true,
        "transformationType": "encrypt"
      },
      "name": "Send Encrypted Message",
      "type": "n8n-nodes-rabbitmq-enhanced.rabbitMqEnhanced",
      "position": [600, 300]
    }
  ]
}
```

## Troubleshooting

- **Connection Issues**: Verify your RabbitMQ server is running and accessible
- **Permission Errors**: Check user permissions on vhost, exchanges, and queues
- **Message Format Issues**: Ensure content matches expected format for consumers

## Development

To modify or extend this node:

1. Clone the repository
2. Install dependencies with `npm install`
3. Make changes to the source code
4. Build with `npm run build`
5. Link to your n8n installation for testing

## License

MIT

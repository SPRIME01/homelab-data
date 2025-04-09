# Setting Up RabbitMQ Triggers for n8n Workflows

This guide provides step-by-step instructions for integrating RabbitMQ with n8n workflows in a homelab environment, allowing you to trigger workflows based on messages in RabbitMQ queues.

## Table of Contents

1. [Prerequisites](#prerequisites)
2. [Creating Dedicated Queues for Workflow Triggers](#creating-dedicated-queues-for-workflow-triggers)
3. [Configuring the RabbitMQ Node in n8n](#configuring-the-rabbitmq-node-in-n8n)
4. [Setting Up Message Filtering and Routing](#setting-up-message-filtering-and-routing)
5. [Handling Different Message Formats](#handling-different-message-formats)
6. [Implementing Error Handling and Dead Letter Queues](#implementing-error-handling-and-dead-letter-queues)
7. [Common Trigger Patterns and Examples](#common-trigger-patterns-and-examples)
8. [Troubleshooting](#troubleshooting)

## Prerequisites

- RabbitMQ server installed and running in your homelab
- n8n instance installed and running
- Basic understanding of message queuing concepts
- Network connectivity between n8n and RabbitMQ

## Creating Dedicated Queues for Workflow Triggers

Dedicated queues help isolate your workflow triggers and make your system more maintainable.

### Step 1: Access RabbitMQ Management Interface

Access your RabbitMQ management interface at `http://rabbitmq.local:15672` (adjust hostname as needed).

### Step 2: Create Exchange

1. Go to the "Exchanges" tab
2. Click "Add a new exchange"
3. Configure the exchange:
   - Name: `home-assistant-events` (example for home automation)
   - Type: `topic` (recommended for flexible routing)
   - Durability: `Durable` (messages survive broker restart)
   - Auto Delete: `No`
   - Internal: `No`

### Step 3: Create Queues

1. Go to the "Queues" tab
2. Click "Add a new queue"
3. For each workflow trigger, create a dedicated queue:

   **Example 1: Motion Detection Queue**
   - Name: `motion-detection-events`
   - Durability: `Durable`
   - Auto Delete: `No`
   - Arguments (optional):
     - `x-message-ttl`: `3600000` (1 hour in milliseconds)
     - `x-max-length`: `1000` (limit queue size)

   **Example 2: Temperature Alerts Queue**
   - Name: `temperature-alerts`
   - Durability: `Durable`
   - Auto Delete: `No`

   **Example 3: Voice Commands Queue**
   - Name: `home-assistant-voice-commands`
   - Durability: `Durable`
   - Auto Delete: `No`

### Step 4: Bind Queues to Exchange

1. Go to the "Exchanges" tab, select your exchange
2. Scroll down to "Bindings"
3. Set up bindings for each queue:

   **For motion detection:**
   - To queue: `motion-detection-events`
   - Routing key: `sensors.motion.#`

   **For temperature alerts:**
   - To queue: `temperature-alerts`
   - Routing key: `sensors.temperature.alert.#`

   **For voice commands:**
   - To queue: `home-assistant-voice-commands`
   - Routing key: `voice.command.#`

### Step 5: Set Up User Permissions

Create a dedicated user with limited permissions for your n8n workflows:

1. Go to "Admin" → "Users"
2. Add a new user (e.g., `n8n-workflows`)
3. Set permissions:
   - Configure regexp: `.*`
   - Write regexp: `.*`
   - Read regexp: `.*`

For production, limit permissions more strictly to only the required resources.

## Configuring the RabbitMQ Node in n8n

### Step 1: Install RabbitMQ Enhanced Node

If you're using the custom enhanced RabbitMQ node:

1. Go to n8n settings
2. Navigate to "Community nodes"
3. Install `n8n-nodes-rabbitmq-enhanced`

### Step 2: Create a RabbitMQ Credential

1. Go to n8n Credentials
2. Click "Create new credentials"
3. Select "RabbitMQ Enhanced" or "RabbitMQ"
4. Fill in the details:
   - Host: `rabbitmq.local` (adjust as needed)
   - Port: `5672`
   - Username: `n8n-workflows` (the user created earlier)
   - Password: `your-secure-password`
   - Vhost: `/` (or your custom vhost)

### Step 3: Create a New Workflow with RabbitMQ Trigger

1. Create a new workflow
2. Add a "RabbitMQ Enhanced" node as a trigger
3. Configure the node:
   - Operation: `Consume`
   - Queue: `motion-detection-events` (or your queue name)
   - Credentials: Select the credentials created in step 2
   - Options:
     - Max Messages: `1` (process one message at a time)
     - No Acknowledgement: `false` (ensure messages are processed before removed)
     - JSON Parse: `true` (if your messages are in JSON format)

## Setting Up Message Filtering and Routing

### Basic Header Filtering

1. In your RabbitMQ trigger node, enable "Header Filtering":
   ```json
   {
     "parameters": {
       "operation": "consume",
       "queue": "motion-detection-events",
       "consumeOptions": {
         "noAck": false,
         "maxMessages": 1,
         "headerFiltering": true,
         "headerFilters": {
           "filter": [
             {
               "name": "sensor-type",
               "value": "motion",
               "matchType": "equals"
             }
           ]
         }
       }
     }
   }
   ```

### Advanced Filtering with Function Node

For more complex filtering, add a Function node after the RabbitMQ node:

1. Add a "Function" node
2. Use code to filter messages:
   ```javascript
   // Filter messages based on specific criteria
   const messages = $input.item.json.messages || [];
   const filteredMessages = messages.filter(msg => {
     const content = typeof msg.content === 'string' ? JSON.parse(msg.content) : msg.content;

     // Example: Only process messages with specific attributes
     return content.zone === 'living_room' && content.confidence > 0.8;
   });

   // Return filtered messages
   return {
     json: {
       messages: filteredMessages,
       count: filteredMessages.length
     }
   };
   ```

### Topic-Based Routing

To implement topic-based routing in RabbitMQ:

1. Use routing keys following the pattern: `category.subcategory.action`
   - Example: `sensors.motion.detected`
   - Example: `sensors.temperature.exceeded`

2. Bind queues with routing patterns:
   - Specific device: `sensors.motion.living_room`
   - All motions: `sensors.motion.#`
   - All sensors: `sensors.#`

## Handling Different Message Formats

### JSON Messages

Most common format, handled automatically if "JSON Parse" is enabled:

```javascript
// In Function node after RabbitMQ trigger
const messages = $input.item.json.messages || [];
const processedData = [];

messages.forEach(msg => {
  const content = typeof msg.content === 'string' ? JSON.parse(msg.content) : msg.content;

  // Process JSON content
  processedData.push({
    deviceId: content.device_id,
    value: content.value,
    timestamp: content.timestamp
  });
});

return { json: { data: processedData } };
```

### Binary Data

For handling binary data like images:

```javascript
// In Function node
const messages = $input.item.json.messages || [];
const message = messages[0];
const content = message.content;

// For base64 encoded binary data
if (typeof content === 'string' && content.startsWith('data:image')) {
  // Process image data
  const base64Data = content.split(',')[1];
  return {
    json: {
      hasImage: true,
      imageData: base64Data,
      metadata: message.properties
    }
  };
}

return { json: { hasImage: false } };
```

### Text Messages

For plain text messages:

```javascript
// In Function node
const messages = $input.item.json.messages || [];
const textContent = messages.map(msg => {
  if (typeof msg.content === 'string') {
    try {
      // Try to parse as JSON first
      return JSON.parse(msg.content);
    } catch (e) {
      // Not JSON, treat as plain text
      return { text: msg.content };
    }
  }
  return msg.content;
});

return { json: { messages: textContent } };
```

## Implementing Error Handling and Dead Letter Queues

### Step 1: Create a Dead Letter Exchange and Queue

In RabbitMQ management interface:

1. Create a new exchange:
   - Name: `dead-letter-exchange`
   - Type: `direct`
   - Durability: `Durable`

2. Create a new queue:
   - Name: `failed-messages`
   - Durability: `Durable`
   - Arguments:
     - `x-message-ttl`: `604800000` (7 days in milliseconds)

3. Bind the queue to the exchange:
   - To queue: `failed-messages`
   - Routing key: `failed`

### Step 2: Configure Source Queues with Dead Letter Configuration

When creating workflow queues, add these arguments:

- `x-dead-letter-exchange`: `dead-letter-exchange`
- `x-dead-letter-routing-key`: `failed`

Example:

import { IExecuteFunctions } from 'n8n-core';
import {
  INodeExecutionData,
  INodeType,
  INodeTypeDescription,
  NodeOperationError,
} from 'n8n-workflow';
import { RabbitMqEnhancedConnection } from './RabbitMqEnhancedConnection';
import { MessageTransformer } from './MessageTransformer';
import * as amqplib from 'amqplib';
import { v4 as uuid } from 'uuid';

export class RabbitMqEnhanced implements INodeType {
  description: INodeTypeDescription = {
    displayName: 'RabbitMQ Enhanced',
    name: 'rabbitMqEnhanced',
    icon: 'file:rabbitmq.svg',
    group: ['transform'],
    version: 1,
    subtitle: '={{$parameter["operation"] + ": " + ($parameter["queue"] || $parameter["exchange"] || "")}}',
    description: 'Enhanced RabbitMQ node with advanced features',
    defaults: {
      name: 'RabbitMQ Enhanced',
    },
    inputs: ['main'],
    outputs: ['main'],
    credentials: [
      {
        name: 'rabbitMqEnhanced',
        required: true,
      },
    ],
    properties: [
      {
        displayName: 'Operation',
        name: 'operation',
        type: 'options',
        noDataExpression: true,
        options: [
          {
            name: 'Publish',
            value: 'publish',
            description: 'Publish a message to an exchange',
          },
          {
            name: 'Publish to Queue',
            value: 'publishToQueue',
            description: 'Publish a message directly to a queue',
          },
          {
            name: 'Batch Publish',
            value: 'batchPublish',
            description: 'Publish multiple messages at once',
          },
          {
            name: 'Consume',
            value: 'consume',
            description: 'Consume messages from a queue',
          },
          {
            name: 'Purge Queue',
            value: 'purgeQueue',
            description: 'Purge all messages from a queue',
          },
          {
            name: 'Assert Exchange',
            value: 'assertExchange',
            description: 'Create or check an exchange',
          },
          {
            name: 'Assert Queue',
            value: 'assertQueue',
            description: 'Create or check a queue',
          },
          {
            name: 'Delete Queue',
            value: 'deleteQueue',
            description: 'Delete a queue',
          },
          {
            name: 'Bind Queue',
            value: 'bindQueue',
            description: 'Bind a queue to an exchange',
          },
        ],
        default: 'publish',
      },

      // ----------------------------------
      //         publish / publishToQueue
      // ----------------------------------
      {
        displayName: 'Exchange',
        name: 'exchange',
        type: 'string',
        displayOptions: {
          show: {
            operation: [
              'publish',
              'batchPublish',
              'assertExchange',
              'bindQueue',
            ],
          },
        },
        default: '',
        placeholder: 'exchange-name',
        description: 'Name of the exchange to publish to',
      },
      {
        displayName: 'Exchange Type',
        name: 'exchangeType',
        type: 'options',
        displayOptions: {
          show: {
            operation: [
              'assertExchange',
            ],
          },
        },
        options: [
          {
            name: 'Direct',
            value: 'direct',
            description: 'Direct exchange type',
          },
          {
            name: 'Topic',
            value: 'topic',
            description: 'Topic exchange type',
          },
          {
            name: 'Headers',
            value: 'headers',
            description: 'Headers exchange type',
          },
          {
            name: 'Fanout',
            value: 'fanout',
            description: 'Fanout exchange type',
          },
        ],
        default: 'direct',
        description: 'Type of exchange',
      },
      {
        displayName: 'Routing Key',
        name: 'routingKey',
        type: 'string',
        displayOptions: {
          show: {
            operation: [
              'publish',
              'bindQueue',
            ],
          },
        },
        default: '',
        placeholder: 'routing-key',
        description: 'The routing key to use for message routing',
      },
      {
        displayName: 'Advanced Routing',
        name: 'advancedRouting',
        type: 'boolean',
        displayOptions: {
          show: {
            operation: [
              'publish',
            ],
          },
        },
        default: false,
        description: 'Whether to use advanced routing options',
      },
      {
        displayName: 'Dynamic Routing Key',
        name: 'dynamicRoutingKey',
        type: 'string',
        displayOptions: {
          show: {
            operation: [
              'publish',
            ],
            advancedRouting: [
              true,
            ],
          },
        },
        default: '={{ $json.category + "." + $json.severity }}',
        description: 'Expression to generate routing key dynamically',
      },
      {
        displayName: 'Queue',
        name: 'queue',
        type: 'string',
        displayOptions: {
          show: {
            operation: [
              'publishToQueue',
              'consume',
              'purgeQueue',
              'assertQueue',
              'deleteQueue',
              'bindQueue',
            ],
          },
        },
        default: '',
        placeholder: 'queue-name',
        description: 'Name of the queue to publish to',
      },
      {
        displayName: 'Content',
        name: 'content',
        type: 'string',
        displayOptions: {
          show: {
            operation: [
              'publish',
              'publishToQueue',
            ],
          },
        },
        typeOptions: {
          rows: 5,
        },
        default: '',
        description: 'The content of the message to be sent',
      },
      {
        displayName: 'Batch Input Field',
        name: 'batchInputField',
        type: 'string',
        displayOptions: {
          show: {
            operation: [
              'batchPublish',
            ],
          },
        },
        default: 'data',
        description: 'The name of the input field containing the array of messages',
      },
      {
        displayName: 'Options',
        name: 'options',
        type: 'collection',
        placeholder: 'Add Option',
        default: {},
        displayOptions: {
          show: {
            operation: [
              'publish',
              'publishToQueue',
              'batchPublish',
              'assertExchange',
              'assertQueue',
            ],
          },
        },
        options: [
          {
            displayName: 'Content Type',
            name: 'contentType',
            type: 'string',
            default: 'application/json',
            description: 'MIME content type of the message',
          },
          {
            displayName: 'Content Encoding',
            name: 'contentEncoding',
            type: 'string',
            default: 'utf-8',
            description: 'MIME content encoding of the message',
          },
          {
            displayName: 'Persistent',
            name: 'persistent',
            type: 'boolean',
            default: true,
            description: 'Whether the message should survive broker restarts',
          },
          {
            displayName: 'Priority',
            name: 'priority',
            type: 'number',
            typeOptions: {
              minValue: 0,
              maxValue: 9,
            },
            default: 0,
            description: 'Message priority (0-9)',
          },
          {
            displayName: 'Expiration',
            name: 'expiration',
            type: 'string',
            default: '',
            placeholder: '30000',
            description: 'Message expiration time in milliseconds',
          },
          {
            displayName: 'User ID',
            name: 'userId',
            type: 'string',
            default: '',
            description: 'User ID',
          },
          {
            displayName: 'App ID',
            name: 'appId',
            type: 'string',
            default: 'n8n',
            description: 'Application ID',
          },
          {
            displayName: 'Mandatory',
            name: 'mandatory',
            type: 'boolean',
            default: false,
            description: 'Whether the message is mandatory',
          },
          {
            displayName: 'Immediate',
            name: 'immediate',
            type: 'boolean',
            default: false,
            description: 'Whether the message should be delivered immediately',
          },
          {
            displayName: 'Correlation ID',
            name: 'correlationId',
            type: 'string',
            default: '',
            description: 'Correlation identifier',
          },
          {
            displayName: 'Reply To',
            name: 'replyTo',
            type: 'string',
            default: '',
            description: 'Address to reply to',
          },
          {
            displayName: 'Message ID',
            name: 'messageId',
            type: 'string',
            default: '',
            description: 'Message identifier',
          },
          {
            displayName: 'Timestamp',
            name: 'timestamp',
            type: 'boolean',
            default: true,
            description: 'Whether to add a timestamp to the message',
          },
          {
            displayName: 'Durable',
            name: 'durable',
            type: 'boolean',
            displayOptions: {
              show: {
                '/operation': [
                  'assertExchange',
                  'assertQueue',
                ],
              },
            },
            default: true,
            description: 'Whether the exchange/queue should survive broker restarts',
          },
          {
            displayName: 'Auto Delete',
            name: 'autoDelete',
            type: 'boolean',
            displayOptions: {
              show: {
                '/operation': [
                  'assertExchange',
                  'assertQueue',
                ],
              },
            },
            default: false,
            description: 'Whether the exchange/queue should be deleted when no longer used',
          },
          {
            displayName: 'Dead Letter Exchange',
            name: 'deadLetterExchange',
            type: 'string',
            displayOptions: {
              show: {
                '/operation': [
                  'assertQueue',
                ],
              },
            },
            default: '',
            description: 'Exchange to which messages will be republished if they are rejected or expire',
          },
          {
            displayName: 'Dead Letter Routing Key',
            name: 'deadLetterRoutingKey',
            type: 'string',
            displayOptions: {
              show: {
                '/operation': [
                  'assertQueue',
                ],
              },
            },
            default: '',
            description: 'Routing key to use when dead-lettering messages',
          },
          {
            displayName: 'Max Priority',
            name: 'maxPriority',
            type: 'number',
            displayOptions: {
              show: {
                '/operation': [
                  'assertQueue',
                ],
              },
            },
            default: 0,
            description: 'Maximum priority level for the queue (0-255)',
          },
          {
            displayName: 'Message TTL',
            name: 'messageTtl',
            type: 'number',
            displayOptions: {
              show: {
                '/operation': [
                  'assertQueue',
                ],
              },
            },
            default: 0,
            description: 'Time-to-live for messages in milliseconds (0 = no TTL)',
          },
          {
            displayName: 'Custom Headers',
            name: 'headers',
            placeholder: 'Add Header',
            type: 'fixedCollection',
            typeOptions: {
              multipleValues: true,
            },
            default: {},
            options: [
              {
                name: 'header',
                displayName: 'Header',
                values: [
                  {
                    displayName: 'Name',
                    name: 'name',
                    type: 'string',
                    default: '',
                    description: 'Name of the header',
                  },
                  {
                    displayName: 'Value',
                    name: 'value',
                    type: 'string',
                    default: '',
                    description: 'Value to set for the header',
                  },
                ],
              },
            ],
          },
        ],
      },

      // ----------------------------------
      //         consume
      // ----------------------------------
      {
        displayName: 'Consume Options',
        name: 'consumeOptions',
        type: 'collection',
        placeholder: 'Add Option',
        default: {},
        displayOptions: {
          show: {
            operation: [
              'consume',
            ],
          },
        },
        options: [
          {
            displayName: 'Consumer Tag',
            name: 'consumerTag',
            type: 'string',
            default: '',
            description: 'Consumer tag to use',
          },
          {
            displayName: 'No Local',
            name: 'noLocal',
            type: 'boolean',
            default: false,
            description: 'Don\'t deliver own messages',
          },
          {
            displayName: 'No Ack',
            name: 'noAck',
            type: 'boolean',
            default: false,
            description: 'No acknowledgment needed',
          },
          {
            displayName: 'Exclusive',
            name: 'exclusive',
            type: 'boolean',
            default: false,
            description: 'Request exclusive consumer access',
          },
          {
            displayName: 'Priority',
            name: 'priority',
            type: 'number',
            default: 0,
            description: 'Consumer priority',
          },
          {
            displayName: 'Arguments',
            name: 'arguments',
            placeholder: 'Add Argument',
            type: 'fixedCollection',
            typeOptions: {
              multipleValues: true,
            },
            default: {},
            options: [
              {
                name: 'argument',
                displayName: 'Argument',
                values: [
                  {
                    displayName: 'Name',
                    name: 'name',
                    type: 'string',
                    default: '',
                    description: 'Name of the argument',
                  },
                  {
                    displayName: 'Value',
                    name: 'value',
                    type: 'string',
                    default: '',
                    description: 'Value of the argument',
                  },
                ],
              },
            ],
          },
          {
            displayName: 'Header Filtering',
            name: 'headerFiltering',
            type: 'boolean',
            default: false,
            description: 'Whether to filter messages based on headers',
          },
          {
            displayName: 'Header Filters',
            name: 'headerFilters',
            placeholder: 'Add Filter',
            type: 'fixedCollection',
            typeOptions: {
              multipleValues: true,
            },
            displayOptions: {
              show: {
                headerFiltering: [
                  true,
                ],
              },
            },
            default: {},
            options: [
              {
                name: 'filter',
                displayName: 'Filter',
                values: [
                  {
                    displayName: 'Header Name',
                    name: 'name',
                    type: 'string',
                    default: '',
                    description: 'Name of the header to filter on',
                  },
                  {
                    displayName: 'Value',
                    name: 'value',
                    type: 'string',
                    default: '',
                    description: 'Value to filter for',
                  },
                  {
                    displayName: 'Match Type',
                    name: 'matchType',
                    type: 'options',
                    options: [
                      {
                        name: 'Equals',
                        value: 'equals',
                      },
                      {
                        name: 'Contains',
                        value: 'contains',
                      },
                      {
                        name: 'Begins With',
                        value: 'beginsWith',
                      },
                      {
                        name: 'Ends With',
                        value: 'endsWith',
                      },
                      {
                        name: 'Regex',
                        value: 'regex',
                      },
                    ],
                    default: 'equals',
                    description: 'How to match the header value',
                  },
                ],
              },
            ],
          },
          {
            displayName: 'Max Messages',
            name: 'maxMessages',
            type: 'number',
            default: 10,
            description: 'Maximum number of messages to consume',
          },
          {
            displayName: 'Timeout',
            name: 'timeout',
            type: 'number',
            default: 60000,
            description: 'Time in milliseconds to wait for messages',
          },
        ],
      },

      // ----------------------------------
      //         Message Transformation
      // ----------------------------------
      {
        displayName: 'Apply Transformation',
        name: 'applyTransformation',
        type: 'boolean',
        default: false,
        description: 'Whether to transform the message before sending',
        displayOptions: {
          show: {
            operation: [
              'publish',
              'publishToQueue',
              'batchPublish',
            ],
          },
        },
      },
      {
        displayName: 'Transformation Type',
        name: 'transformationType',
        type: 'options',
        displayOptions: {
          show: {
            operation: [
              'publish',
              'publishToQueue',
              'batchPublish',
            ],
            applyTransformation: [
              true,
            ],
          },
        },
        options: [
          {
            name: 'Compress',
            value: 'compress',
            description: 'Compress the message content',
          },
          {
            name: 'Encrypt',
            value: 'encrypt',
            description: 'Encrypt the message content',
          },
          {
            name: 'Transform with Function',
            value: 'function',
            description: 'Transform with custom function',
          },
          {
            name: 'Schema Validation',
            value: 'validate',
            description: 'Validate against schema',
          },
        ],
        default: 'function',
      },
      {
        displayName: 'Transformation Function',
        name: 'transformationFunction',
        type: 'string',
        displayOptions: {
          show: {
            operation: [
              'publish',
              'publishToQueue',
              'batchPublish',
            ],
            applyTransformation: [
              true,
            ],
            transformationType: [
              'function',
            ],
          },
        },
        typeOptions: {
          rows: 4,
          editor: 'code',
          language: 'javascript',
        },
        default: 'return {\n  transformed: true,\n  ...input\n};',
        description: 'Function to transform the message. Input is the message content as an object.',
      },
      {
        displayName: 'Schema',
        name: 'schema',
        type: 'string',
        displayOptions: {
          show: {
            operation: [
              'publish',
              'publishToQueue',
              'batchPublish',
            ],
            applyTransformation: [
              true,
            ],
            transformationType: [
              'validate',
            ],
          },
        },
        typeOptions: {
          rows: 4,
          editor: 'code',
          language: 'json',
        },
        default: '{\n  "type": "object",\n  "properties": {\n    "name": { "type": "string" }\n  }\n}',
        description: 'JSON schema to validate against',
      },
    ],
  };

  async execute(this: IExecuteFunctions): Promise<INodeExecutionData[][]> {
    const items = this.getInputData();
    const returnData: INodeExecutionData[] = [];
    let responseData;

    const operation = this.getNodeParameter('operation', 0) as string;

    // Get credentials
    const credentials = await this.getCredentials('rabbitMqEnhanced');

    const rabbitmqConnection = new RabbitMqEnhancedConnection(credentials);
    let conn: amqplib.Connection;
    let channel: amqplib.Channel;

    try {
      conn = await rabbitmqConnection.connect();
      channel = await conn.createChannel();

      // Execute operation
      for (let i = 0; i < items.length; i++) {
        try {
          if (operation === 'publish') {
            // Publish a message to an exchange
            const exchange = this.getNodeParameter('exchange', i) as string;
            const advancedRouting = this.getNodeParameter('advancedRouting', i, false) as boolean;
            const routingKey = advancedRouting
              ? this.getNodeParameter('dynamicRoutingKey', i, '') as string
              : this.getNodeParameter('routingKey', i, '') as string;
            const content = this.getNodeParameter('content', i) as string;
            const options = this.getNodeParameter('options', i, {}) as any;

            // Transform message if needed
            let messageContent = content;
            const applyTransformation = this.getNodeParameter('applyTransformation', i, false) as boolean;

            if (applyTransformation) {
              const transformationType = this.getNodeParameter('transformationType', i) as string;
              const transformer = new MessageTransformer();

              switch (transformationType) {
                case 'compress':
                  messageContent = await transformer.compress(messageContent);
                  options.contentEncoding = 'gzip';
                  break;
                case 'encrypt':
                  messageContent = await transformer.encrypt(messageContent, 'n8n-secret-key');
                  options.contentEncoding = 'encrypted';
                  break;
                case 'function':
                  const transformationFunction = this.getNodeParameter('transformationFunction', i) as string;
                  messageContent = await transformer.transformWithFunction(messageContent, transformationFunction);
                  break;
                case 'validate':
                  const schema = this.getNodeParameter('schema', i) as string;
                  await transformer.validateWithSchema(messageContent, schema);
                  break;
              }
            }

            // Convert message options
            const messageOptions = this.prepareMessageOptions(options);

            // Publish the message
            channel.publish(
              exchange,
              routingKey,
              Buffer.from(messageContent),
              messageOptions,
            );

            responseData = {
              success: true,
              exchange,
              routingKey,
              messageId: messageOptions.messageId,
            };
          }

          else if (operation === 'publishToQueue') {
            // Publish a message directly to a queue
            const queue = this.getNodeParameter('queue', i) as string;
            const content = this.getNodeParameter('content', i) as string;
            const options = this.getNodeParameter('options', i, {}) as any;

            // Transform message if needed
            let messageContent = content;
            const applyTransformation = this.getNodeParameter('applyTransformation', i, false) as boolean;

            if (applyTransformation) {
              const transformationType = this.getNodeParameter('transformationType', i) as string;
              const transformer = new MessageTransformer();

              switch (transformationType) {
                case 'compress':
                  messageContent = await transformer.compress(messageContent);
                  options.contentEncoding = 'gzip';
                  break;
                case 'encrypt':
                  messageContent = await transformer.encrypt(messageContent, 'n8n-secret-key');
                  options.contentEncoding = 'encrypted';
                  break;
                case 'function':
                  const transformationFunction = this.getNodeParameter('transformationFunction', i) as string;
                  messageContent = await transformer.transformWithFunction(messageContent, transformationFunction);
                  break;
                case 'validate':
                  const schema = this.getNodeParameter('schema', i) as string;
                  await transformer.validateWithSchema(messageContent, schema);
                  break;
              }
            }

            // Convert message options
            const messageOptions = this.prepareMessageOptions(options);

            // Publish to queue
            channel.sendToQueue(
              queue,
              Buffer.from(messageContent),
              messageOptions,
            );

            responseData = {
              success: true,
              queue,
              messageId: messageOptions.messageId,
            };
          }

          else if (operation === 'batchPublish') {
            const exchange = this.getNodeParameter('exchange', i) as string;
            const batchInputField = this.getNodeParameter('batchInputField', i) as string;
            const options = this.getNodeParameter('options', i, {}) as any;
            const messages = items[i].json[batchInputField];

            if (!Array.isArray(messages)) {
              throw new NodeOperationError(
                this.getNode(),
                `The value of field "${batchInputField}" is not an array`,
                { itemIndex: i },
              );
            }

            const publishedMessages = [];
            const transformer = new MessageTransformer();
            const applyTransformation = this.getNodeParameter('applyTransformation', i, false) as boolean;
            const transformationType = applyTransformation ? this.getNodeParameter('transformationType', i) as string : null;

            // Process each message in the batch
            for (const message of messages) {
              let content = typeof message === 'object' ? JSON.stringify(message) : message.toString();
              const messageOptions = this.prepareMessageOptions(options);

              // Apply transformation if needed
              if (applyTransformation && transformationType) {
                switch (transformationType) {
                  case 'compress':
                    content = await transformer.compress(content);
                    messageOptions.contentEncoding = 'gzip';
                    break;
                  case 'encrypt':
                    content = await transformer.encrypt(content, 'n8n-secret-key');
                    messageOptions.contentEncoding = 'encrypted';
                    break;
                  case 'function':
                    const transformationFunction = this.getNodeParameter('transformationFunction', i) as string;
                    content = await transformer.transformWithFunction(content, transformationFunction);
                    break;
                  case 'validate':
                    const schema = this.getNodeParameter('schema', i) as string;
                    await transformer.validateWithSchema(content, schema);
                    break;
                }
              }

              // Use a unique routing key per message if it has a routing property
              const routingKey = message.routing || '';

              // Publish to exchange
              channel.publish(
                exchange,
                routingKey,
                Buffer.from(content),
                messageOptions,
              );

              publishedMessages.push({
                messageId: messageOptions.messageId,
                routingKey,
              });
            }

            responseData = {
              success: true,
              exchange,
              publishedCount: publishedMessages.length,
              messages: publishedMessages,
            };
          }

          else if (operation === 'consume') {
            const queue = this.getNodeParameter('queue', i) as string;
            const consumeOptions = this.getNodeParameter('consumeOptions', i, {}) as any;

            // Set up consumer options
            const options: amqplib.Options.Consume = {
              consumerTag: consumeOptions.consumerTag || '',
              noLocal: consumeOptions.noLocal || false,
              noAck: consumeOptions.noAck || false,
              exclusive: consumeOptions.exclusive || false,
            };

            if (consumeOptions.priority) {
              options.priority = consumeOptions.priority;
            }

            if (consumeOptions.arguments && consumeOptions.arguments.argument) {
              options.arguments = {};
              for (const arg of consumeOptions.arguments.argument) {
                options.arguments[arg.name] = arg.value;
              }
            }

            // Set up header filtering if enabled
            const headerFiltering = consumeOptions.headerFiltering || false;
            let headerFilters = [];

            if (headerFiltering && consumeOptions.headerFilters && consumeOptions.headerFilters.filter) {
              headerFilters = consumeOptions.headerFilters.filter;
            }

            // Consume messages
            const maxMessages = consumeOptions.maxMessages || 10;
            const timeout = consumeOptions.timeout || 60000;

            const messages = await this.consumeMessages(
              channel,
              queue,
              options,
              maxMessages,
              timeout,
              headerFilters,
            );

            responseData = {
              success: true,
              queue,
              messageCount: messages.length,
              messages,
            };
          }

          else if (operation === 'purgeQueue') {
            const queue = this.getNodeParameter('queue', i) as string;

            const purgeResponse = await channel.purgeQueue(queue);

            responseData = {
              success: true,
              queue,
              messageCount: purgeResponse.messageCount,
            };
          }

          else if (operation === 'assertExchange') {
            const exchange = this.getNodeParameter('exchange', i) as string;
            const exchangeType = this.getNodeParameter('exchangeType', i) as string;
            const options = this.getNodeParameter('options', i, {}) as any;

            const exchangeOptions: amqplib.Options.AssertExchange = {
              durable: options.durable !== false,
              autoDelete: options.autoDelete || false,
            };

            if (options.arguments) {
              exchangeOptions.arguments = {};
              for (const arg of options.arguments) {
                exchangeOptions.arguments[arg.name] = arg.value;
              }
            }

            await channel.assertExchange(exchange, exchangeType, exchangeOptions);

            responseData = {
              success: true,
              exchange,
              type: exchangeType,
              options: exchangeOptions,
            };
          }

          else if (operation === 'assertQueue') {
            const queue = this.getNodeParameter('queue', i) as string;
            const options = this.getNodeParameter('options', i, {}) as any;

            const queueOptions: amqplib.Options.AssertQueue = {
              durable: options.durable !== false,
              autoDelete: options.autoDelete || false,
            };

            // Set up dead letter exchange if specified
            if (options.deadLetterExchange) {
              queueOptions.arguments = queueOptions.arguments || {};
              queueOptions.arguments['x-dead-letter-exchange'] = options.deadLetterExchange;

              if (options.deadLetterRoutingKey) {
                queueOptions.arguments['x-dead-letter-routing-key'] = options.deadLetterRoutingKey;
              }
            }

            // Set max priority if specified
            if (options.maxPriority) {
              queueOptions.arguments = queueOptions.arguments || {};
              queueOptions.arguments['x-max-priority'] = options.maxPriority;
            }

            // Set message TTL if specified
            if (options.messageTtl) {
              queueOptions.arguments = queueOptions.arguments || {};
              queueOptions.arguments['x-message-ttl'] = options.messageTtl;
            }

            const queueResult = await channel.assertQueue(queue, queueOptions);

            responseData = {
              success: true,
              queue: queueResult.queue,
              messageCount: queueResult.messageCount,
              consumerCount: queueResult.consumerCount,
              options: queueOptions,
            };
          }

          else if (operation === 'deleteQueue') {
            const queue = this.getNodeParameter('queue', i) as string;

            const deleteResponse = await channel.deleteQueue(queue);

            responseData = {
              success: true,
              queue,
              messageCount: deleteResponse.messageCount,
            };
          }

          else if (operation === 'bindQueue') {
            const queue = this.getNodeParameter('queue', i) as string;
            const exchange = this.getNodeParameter('exchange', i) as string;
            const routingKey = this.getNodeParameter('routingKey', i, '') as string;

            await channel.bindQueue(queue, exchange, routingKey);

            responseData = {
              success: true,
              queue,
              exchange,
              routingKey,
            };
          }

          returnData.push({
            json: responseData,
            pairedItem: { item: i },
          });
        } catch (error) {
          if (this.continueOnFail()) {
            returnData.push({
              json: {
                success: false,
                error: error.message,
              },
              pairedItem: { item: i },
            });
            continue;
          }
          throw error;
        }
      }

      return [returnData];
    } catch (error) {
      if (this.continueOnFail()) {
        return [[{ json: { success: false, error: error.message } }]];
      }
      throw error;
    } finally {
      // Close channel and connection
      if (channel) {
        try {
          await channel.close();
        } catch (error) {
          // Ignore
        }
      }

      if (conn) {
        try {
          await conn.close();
        } catch (error) {
          // Ignore
        }
      }
    }
  }

  // Helper method to prepare message options from node parameters
  private prepareMessageOptions(options: any): amqplib.Options.Publish {
    const messageOptions: amqplib.Options.Publish = {
      contentType: options.contentType || 'application/json',
      contentEncoding: options.contentEncoding || 'utf-8',
      persistent: options.persistent !== false,
      messageId: options.messageId || uuid(),
    };

    if (options.priority !== undefined) {
      messageOptions.priority = options.priority;
    }

    if (options.expiration) {
      messageOptions.expiration = options.expiration.toString();
    }

    if (options.userId) {
      messageOptions.userId = options.userId;
    }

    if (options.appId) {
      messageOptions.appId = options.appId;
    }

    if (options.mandatory !== undefined) {
      messageOptions.mandatory = options.mandatory;
    }

    if (options.immediate !== undefined) {
      messageOptions.immediate = options.immediate;
    }

    if (options.correlationId) {
      messageOptions.correlationId = options.correlationId;
    }

    if (options.replyTo) {
      messageOptions.replyTo = options.replyTo;
    }

    if (options.timestamp) {
      messageOptions.timestamp = Math.floor(Date.now() / 1000);
    }

    // Add custom headers if present
    if (options.headers && options.headers.header) {
      messageOptions.headers = {};

      for (const header of options.headers.header) {
        messageOptions.headers[header.name] = header.value;
      }
    }

    return messageOptions;
  }

  // Helper method to consume messages with filtering
  private async consumeMessages(
    channel: amqplib.Channel,
    queue: string,
    options: amqplib.Options.Consume,
    maxMessages: number,
    timeout: number,
    headerFilters: any[],
  ): Promise<any[]> {
    return new Promise((resolve, reject) => {
      const messages: any[] = [];
      let messageCount = 0;
      let consumerTag: string;
      let timeoutId: NodeJS.Timeout;

      const consumeCallback = async (msg: amqplib.ConsumeMessage | null) => {
        try {
          if (msg === null) {
            // Consumer was cancelled
            clearTimeout(timeoutId);
            resolve(messages);
            return;
          }

          // Check header filters if specified
          if (headerFilters.length > 0 && msg.properties.headers) {
            const headers = msg.properties.headers;
            let matched = true;

            for (const filter of headerFilters) {
              const headerValue = headers[filter.name];

              if (headerValue === undefined) {
                matched = false;
                break;
              }

              const filterValue = filter.value;
              const matchType = filter.matchType || 'equals';

              switch (matchType) {
                case 'equals':
                  if (headerValue !== filterValue) {
                    matched = false;
                  }
                  break;
                case 'contains':
                  if (!headerValue.includes(filterValue)) {
                    matched = false;
                  }
                  break;
                case 'beginsWith':
                  if (!headerValue.startsWith(filterValue)) {
                    matched = false;
                  }
                  break;
                case 'endsWith':
                  if (!headerValue.endsWith(filterValue)) {
                    matched = false;
                  }
                  break;
                case 'regex':
                  if (!new RegExp(filterValue).test(headerValue)) {
                    matched = false;
                  }
                  break;
              }

              if (!matched) {
                break;
              }
            }

            if (!matched) {
              // Skip this message and acknowledge it
              if (!options.noAck) {
                channel.ack(msg);
              }
              return;
            }
          }

          // Process the message
          let content = msg.content.toString();

          // Try to parse as JSON
          try {
            content = JSON.parse(content);
          } catch (e) {
            // Not JSON, leave as string
          }

          messages.push({
            content,
            properties: msg.properties,
            fields: msg.fields,
          });

          // Acknowledge the message if required
          if (!options.noAck) {
            channel.ack(msg);
          }

          messageCount++;

          // Stop consuming if we've reached the maximum
          if (messageCount >= maxMessages) {
            clearTimeout(timeoutId);
            await channel.cancel(consumerTag);
            resolve(messages);
          }
        } catch (error) {
          clearTimeout(timeoutId);
          await channel.cancel(consumerTag).catch(() => {});
          reject(error);
        }
      };

      // Start consuming
      channel.consume(queue, consumeCallback, options)
        .then((result) => {
          consumerTag = result.consumerTag;

          // Set timeout
          timeoutId = setTimeout(async () => {
            try {
              await channel.cancel(consumerTag);
              resolve(messages);
            } catch (error) {
              reject(error);
            }
          }, timeout);
        })
        .catch((error) => {
          reject(error);
        });
    });
  }
}

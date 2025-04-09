import {
  ICredentialType,
  INodeProperties,
} from 'n8n-workflow';

export class RabbitMqEnhanced implements ICredentialType {
  name = 'rabbitMqEnhanced';
  displayName = 'RabbitMQ Enhanced';
  documentationUrl = 'https://www.rabbitmq.com/';
  properties: INodeProperties[] = [
    {
      displayName: 'Host',
      name: 'host',
      type: 'string',
      default: 'localhost',
      required: true,
    },
    {
      displayName: 'Port',
      name: 'port',
      type: 'number',
      default: 5672,
      required: true,
    },
    {
      displayName: 'Virtual Host',
      name: 'vhost',
      type: 'string',
      default: '/',
      description: 'The name of the virtual host',
    },
    {
      displayName: 'User',
      name: 'username',
      type: 'string',
      default: 'guest',
      required: true,
    },
    {
      displayName: 'Password',
      name: 'password',
      type: 'string',
      typeOptions: {
        password: true,
      },
      default: 'guest',
      required: true,
    },
    {
      displayName: 'Connection Mode',
      name: 'connectionMode',
      type: 'options',
      options: [
        {
          name: 'Basic',
          value: 'basic',
        },
        {
          name: 'TLS/SSL',
          value: 'tls',
        },
      ],
      default: 'basic',
    },
    {
      displayName: 'SSL CA Certificate',
      name: 'ca',
      type: 'string',
      typeOptions: {
        rows: 5,
      },
      displayOptions: {
        show: {
          connectionMode: [
            'tls',
          ],
        },
      },
      default: '',
      description: 'SSL CA certificate content',
    },
    {
      displayName: 'Reject Unauthorized Certificates',
      name: 'rejectUnauthorized',
      type: 'boolean',
      displayOptions: {
        show: {
          connectionMode: [
            'tls',
          ],
        },
      },
      default: true,
      description: 'Whether to reject connections with certificates from untrusted CAs',
    },
    {
      displayName: 'Connection Timeout',
      name: 'timeout',
      type: 'number',
      default: 30000,
      description: 'Timeout in milliseconds for connection attempts',
    },
    {
      displayName: 'Heartbeat Interval',
      name: 'heartbeat',
      type: 'number',
      default: 30,
      description: 'Heartbeat interval in seconds',
    },
  ];
}

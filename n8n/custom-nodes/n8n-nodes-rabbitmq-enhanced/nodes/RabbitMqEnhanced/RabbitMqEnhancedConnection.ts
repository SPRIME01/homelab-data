import * as amqplib from 'amqplib';
import * as fs from 'fs';

export class RabbitMqEnhancedConnection {
  private credentials: {
    host: string;
    port: number;
    vhost: string;
    username: string;
    password: string;
    connectionMode: string;
    ca?: string;
    rejectUnauthorized?: boolean;
    timeout?: number;
    heartbeat?: number;
  };

  constructor(credentials: any) {
    this.credentials = credentials;
  }

  public async connect(): Promise<amqplib.Connection> {
    const {
      host,
      port,
      vhost,
      username,
      password,
      connectionMode,
      ca,
      rejectUnauthorized,
      timeout,
      heartbeat,
    } = this.credentials;

    const protocol = connectionMode === 'tls' ? 'amqps' : 'amqp';

    // Set up connection options
    const socketOptions: any = {
      timeout: timeout || 30000,
    };

    if (connectionMode === 'tls') {
      socketOptions.cert = ca || undefined;
      socketOptions.rejectUnauthorized = rejectUnauthorized !== false;
    }

    const connectOptions: any = {
      protocol,
      hostname: host,
      port,
      username,
      password,
      vhost: vhost || '/',
      locale: 'en_US',
      frameMax: 0,
      heartbeat: heartbeat || 30,
      socketOptions,
    };

    // Create connection URL
    const connectUrl = `${protocol}://${username}:${encodeURIComponent(password)}@${host}:${port}/${encodeURIComponent(vhost || '/')}`;

    try {
      return await amqplib.connect(connectUrl, connectOptions);
    } catch (error) {
      throw new Error(`Failed to connect to RabbitMQ: ${error.message}`);
    }
  }
}

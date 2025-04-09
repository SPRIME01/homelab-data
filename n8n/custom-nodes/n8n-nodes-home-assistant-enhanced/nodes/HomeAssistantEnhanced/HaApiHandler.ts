import axios, { AxiosInstance } from 'axios';

export class HaApiHandler {
  private api: AxiosInstance;

  constructor(credentials: { hostUrl: string; accessToken: string; timeout: number; verifySSL: boolean }) {
    this.api = axios.create({
      baseURL: credentials.hostUrl,
      timeout: credentials.timeout,
      headers: {
        'Authorization': `Bearer ${credentials.accessToken}`,
        'Content-Type': 'application/json',
      },
      httpsAgent: credentials.verifySSL ? undefined : new (require('https')).Agent({
        rejectUnauthorized: false,
      }),
    });
  }

  async getStates() {
    const response = await this.api.get('/api/states');
    return response.data;
  }

  async getHistory(entityId?: string, startTime?: string) {
    const params = new URLSearchParams();
    if (entityId) params.append('filter_entity_id', entityId);
    if (startTime) params.append('start_time', startTime);

    const response = await this.api.get(`/api/history/period${startTime ? `/${startTime}` : ''}`, {
      params,
    });
    return response.data;
  }

  async callService(domain: string, service: string, data: any) {
    const response = await this.api.post(`/api/services/${domain}/${service}`, data);
    return response.data;
  }

  async renderTemplate(template: string) {
    const response = await this.api.post('/api/template', { template });
    return response.data;
  }

  async executeBatchOperation(operation: { type: string; config: any }) {
    switch (operation.type) {
      case 'callService':
        return this.callService(
          operation.config.domain,
          operation.config.service,
          operation.config.data
        );
      case 'getState':
        const states = await this.getStates();
        return states.find((s: any) => s.entity_id === operation.config.entityId);
      default:
        throw new Error(`Unknown batch operation type: ${operation.type}`);
    }
  }
}

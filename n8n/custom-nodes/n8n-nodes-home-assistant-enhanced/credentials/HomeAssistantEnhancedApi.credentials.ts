import {
  ICredentialType,
  INodeProperties,
} from 'n8n-workflow';

export class HomeAssistantEnhancedApi implements ICredentialType {
  name = 'homeAssistantEnhancedApi';
  displayName = 'Home Assistant Enhanced API';
  documentationUrl = 'https://www.home-assistant.io/docs/authentication/';
  properties: INodeProperties[] = [
    {
      displayName: 'Host URL',
      name: 'hostUrl',
      type: 'string',
      default: 'http://homeassistant.local:8123',
      required: true,
    },
    {
      displayName: 'Long-Lived Access Token',
      name: 'accessToken',
      type: 'string',
      typeOptions: {
        password: true,
      },
      required: true,
    },
    {
      displayName: 'Verify SSL',
      name: 'verifySSL',
      type: 'boolean',
      default: true,
    },
    {
      displayName: 'Connection Timeout',
      name: 'timeout',
      type: 'number',
      default: 10000,
      description: 'Timeout in milliseconds',
    },
  ];
}

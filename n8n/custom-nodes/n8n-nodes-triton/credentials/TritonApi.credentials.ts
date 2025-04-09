import {
  ICredentialType,
  INodeProperties,
} from 'n8n-workflow';

export class TritonApi implements ICredentialType {
  name = 'tritonApi';
  displayName = 'Triton API';
  documentationUrl = 'https://github.com/triton-inference-server/server';
  properties: INodeProperties[] = [
    {
      displayName: 'Server URL',
      name: 'url',
      type: 'string',
      default: 'http://localhost:8000',
      placeholder: 'http://triton-inference-server:8000',
      description: 'URL of the Triton Inference Server HTTP endpoint',
      required: true,
    },
    {
      displayName: 'API Key',
      name: 'apiKey',
      type: 'string',
      typeOptions: {
        password: true,
      },
      default: '',
      description: 'API key for authentication (if enabled on server)',
      required: false,
    },
    {
      displayName: 'SSL Certificate Validation',
      name: 'allowUnauthorizedCerts',
      type: 'boolean',
      default: false,
      description: 'Whether to allow connections to SSL sites without valid certificates',
    },
  ];
}

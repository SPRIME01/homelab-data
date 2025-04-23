import {
  IExecuteFunctions,
  INodeExecutionData,
  INodeType,
  INodeTypeDescription,
  NodeOperationError,
} from 'n8n-workflow';
import axios, { AxiosRequestConfig } from 'axios';

export class Triton implements INodeType {
  description: INodeTypeDescription = {
    displayName: 'Triton Inference Server',
    name: 'triton',
    icon: 'file:triton.svg',
    group: ['transform'],
    version: 1,
    subtitle: '={{$parameter["operation"] + ": " + $parameter["model"]}}',
    description: 'Interact with Triton Inference Server',
    defaults: {
      name: 'Triton',
    },
    inputs: ['main'],
    outputs: ['main'],
    credentials: [
      {
        name: 'tritonApi',
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
            name: 'Run Inference',
            value: 'inference',
          },
          {
            name: 'Get Model Information',
            value: 'modelInfo',
          },
          {
            name: 'Get Server Health',
            value: 'serverHealth',
          },
          {
            name: 'Get Server Metadata',
            value: 'serverMetadata',
          },
        ],
        default: 'inference',
      },

      // MODEL SELECTION - Only show if operation is inference or modelInfo
      {
        displayName: 'Model Name',
        name: 'model',
        type: 'string',
        default: '',
        required: true,
        description: 'Name of the model in Triton server',
        displayOptions: {
          show: {
            operation: ['inference', 'modelInfo'],
          },
        },
      },
      {
        displayName: 'Model Version',
        name: 'version',
        type: 'string',
        default: '',
        description: 'Version of the model to use (leave empty for default)',
        displayOptions: {
          show: {
            operation: ['inference', 'modelInfo'],
          },
        },
      },

      // INFERENCE OPTIONS - Only show if operation is inference
      {
        displayName: 'Input Data Field',
        name: 'inputDataField',
        type: 'string',
        default: 'data',
        description: 'Name of the field in the input items that contains the data for inference',
        displayOptions: {
          show: {
            operation: ['inference'],
          },
        },
      },
      {
        displayName: 'Input Name',
        name: 'inputName',
        type: 'string',
        default: 'input',
        description: 'Name of the input tensor as expected by the model',
        displayOptions: {
          show: {
            operation: ['inference'],
          },
        },
      },
      {
        displayName: 'Input Data Type',
        name: 'inputDataType',
        type: 'options',
        options: [
          {
            name: 'Float (FP32)',
            value: 'FP32',
          },
          {
            name: 'Int (INT32)',
            value: 'INT32',
          },
          {
            name: 'String',
            value: 'STRING',
          },
          {
            name: 'Boolean',
            value: 'BOOL',
          },
          {
            name: 'Int64 (INT64)',
            value: 'INT64',
          },
          {
            name: 'Float16 (FP16)',
            value: 'FP16',
          },
        ],
        default: 'FP32',
        description: 'Data type of the input tensor',
        displayOptions: {
          show: {
            operation: ['inference'],
          },
        },
      },
      {
        displayName: 'Input Shape',
        name: 'inputShape',
        type: 'string',
        default: '',
        placeholder: '[1, 3, 224, 224] or leave empty for automatic',
        description: 'Shape of input tensor as array (leave empty to determine automatically)',
        displayOptions: {
          show: {
            operation: ['inference'],
          },
        },
      },
      {
        displayName: 'Output Name',
        name: 'outputName',
        type: 'string',
        default: 'output',
        description: 'Name of the output tensor as defined by the model',
        displayOptions: {
          show: {
            operation: ['inference'],
          },
        },
      },

      // ADVANCED OPTIONS
      {
        displayName: 'Options',
        name: 'options',
        type: 'collection',
        placeholder: 'Add Option',
        default: {},
        options: [
          {
            displayName: 'Batch Size',
            name: 'batchSize',
            type: 'number',
            default: 1,
            description: 'Number of inputs to batch together in a single request',
          },
          {
            displayName: 'Timeout (ms)',
            name: 'timeout',
            type: 'number',
            default: 30000,
            description: 'Request timeout in milliseconds',
          },
          {
            displayName: 'Result Field Name',
            name: 'resultFieldName',
            type: 'string',
            default: 'inferenceResult',
            description: 'Name of the output field that will contain inference results',
          },
          {
            displayName: 'Include Raw Response',
            name: 'includeRawResponse',
            type: 'boolean',
            default: false,
            description: 'Whether to include the complete raw response in the output',
          },
          {
            displayName: 'Include Model Metadata',
            name: 'includeMetadata',
            type: 'boolean',
            default: false,
            description: 'Whether to include model metadata in the response',
          },
          {
            displayName: 'Post-Processing Function (Advanced)',
            name: 'postProcessingFunction',
            type: 'string',
            typeOptions: {
              editor: 'code',
            },
            default: '',
            description: 'JavaScript function that can transform the results. Must return the processed data. Example: return data.map(x => x * 2)',
            noDataExpression: true,
          },
        ],
      },
    ],
  };

  async execute(this: IExecuteFunctions): Promise<INodeExecutionData[][]> {
    const items = this.getInputData();
    const returnData: INodeExecutionData[] = [];

    // Get credentials
    const credentials = await this.getCredentials('tritonApi');
    const baseUrl = credentials.url as string;
    const apiKey = credentials.apiKey as string;
    const allowUnauthorizedCerts = credentials.allowUnauthorizedCerts as boolean;

    // Prepare request configuration
    const axiosConfig: AxiosRequestConfig = {
      timeout: 30000,
      headers: {},
    };

    if (apiKey) {
      axiosConfig.headers!['Authorization'] = `Bearer ${apiKey}`;
    }

    if (allowUnauthorizedCerts) {
      axiosConfig.httpsAgent = new (require('https')).Agent({
        rejectUnauthorized: false,
      });
    }

    // Process each input item
    for (let i = 0; i < items.length; i++) {
      try {
        const operation = this.getNodeParameter('operation', i) as string;

        // Common options for all operations
        const options = this.getNodeParameter('options', i, {}) as {
          batchSize?: number;
          timeout?: number;
          resultFieldName?: string;
          includeRawResponse?: boolean;
          includeMetadata?: boolean;
          postProcessingFunction?: string;
        };

        if (options.timeout) {
          axiosConfig.timeout = options.timeout;
        }

        const resultFieldName = options.resultFieldName || 'inferenceResult';

        // Execute based on operation
        let responseData;

        if (operation === 'serverHealth') {
          // Check server health
          responseData = await this.checkServerHealth(baseUrl, axiosConfig);
        }
        else if (operation === 'serverMetadata') {
          // Get server metadata
          responseData = await this.getServerMetadata(baseUrl, axiosConfig);
        }
        else if (operation === 'modelInfo') {
          // Get model information
          const model = this.getNodeParameter('model', i) as string;
          const version = this.getNodeParameter('version', i, '') as string;

          responseData = await this.getModelInfo(baseUrl, model, version, axiosConfig);
        }
        else if (operation === 'inference') {
          // Run inference
          const model = this.getNodeParameter('model', i) as string;
          const version = this.getNodeParameter('version', i, '') as string;
          const inputDataField = this.getNodeParameter('inputDataField', i) as string;
          const inputName = this.getNodeParameter('inputName', i) as string;
          const outputName = this.getNodeParameter('outputName', i) as string;
          const inputDataType = this.getNodeParameter('inputDataType', i) as string;
          const inputShapeStr = this.getNodeParameter('inputShape', i, '') as string;

          // Get the input data
          const inputData = items[i].json[inputDataField];
          if (!inputData) {
            throw new NodeOperationError(
              this.getNode(),
              `No input data found in field "${inputDataField}"`,
              { itemIndex: i }
            );
          }

          // Validate input data
          if (!Array.isArray(inputData)) {
            throw new NodeOperationError(
              this.getNode(),
              `Input data must be an array`,
              { itemIndex: i }
            );
          }

          // Parse input shape if provided
          let inputShape: number[] | undefined;
          if (inputShapeStr) {
            try {
              inputShape = JSON.parse(inputShapeStr);
              if (!Array.isArray(inputShape)) {
                throw new Error('Input shape must be an array');
              }
            } catch (error) {
              throw new NodeOperationError(
                this.getNode(),
                `Invalid input shape format: ${error.message}`,
                { itemIndex: i }
              );
            }
          }

          // Run inference
          responseData = await this.runInference(
            baseUrl,
            model,
            version,
            inputData,
            inputName,
            outputName,
            inputDataType,
            inputShape,
            options.batchSize || 1,
            axiosConfig
          );

          // Apply post-processing if specified
          if (options.postProcessingFunction) {
            try {
              const postProcessFn = new Function('data', options.postProcessingFunction);
              responseData.result = postProcessFn(responseData.result);
            } catch (error) {
              throw new NodeOperationError(
                this.getNode(),
                `Error in post-processing function: ${error.message}`,
                { itemIndex: i }
              );
            }
          }
        }

        // Prepare output
        const newItem: INodeExecutionData = {
          json: {
            ...items[i].json,
          },
          binary: items[i].binary,
        };

        // Set the result field
        newItem.json[resultFieldName] = responseData.result;

        // Include additional data if requested
        if (options.includeRawResponse && responseData.raw) {
          newItem.json[`${resultFieldName}_raw`] = responseData.raw;
        }

        if (options.includeMetadata && responseData.metadata) {
          newItem.json[`${resultFieldName}_metadata`] = responseData.metadata;
        }

        returnData.push(newItem);
      } catch (error) {
        if (this.continueOnFail()) {
          const newItem: INodeExecutionData = {
            json: {
              ...items[i].json,
              error: error.message,
            },
            binary: items[i].binary,
          };
          returnData.push(newItem);
          continue;
        }
        throw error;
      }
    }

    return [returnData];
  }

  // Helper methods for different operations

  /**
   * Check the health of the Triton server
   */
  private async checkServerHealth(
    baseUrl: string,
    config: AxiosRequestConfig
  ): Promise<{ result: { status: string; healthy: boolean }, raw?: any }> {
    try {
      const response = await axios.get(`${baseUrl}/v2/health/ready`, config);
      return {
        result: {
          status: 'ok',
          healthy: true,
        },
        raw: response.data,
      };
    } catch (error) {
      return {
        result: {
          status: error.message,
          healthy: false,
        },
        raw: error.response?.data,
      };
    }
  }

  /**
   * Get server metadata
   */
  private async getServerMetadata(
    baseUrl: string,
    config: AxiosRequestConfig
  ): Promise<{ result: any, raw?: any }> {
    const response = await axios.get(`${baseUrl}/v2`, config);
    return {
      result: response.data,
      raw: response.data,
    };
  }

  /**
   * Get model information
   */
  private async getModelInfo(
    baseUrl: string,
    model: string,
    version: string,
    config: AxiosRequestConfig
  ): Promise<{ result: any, raw?: any, metadata?: any }> {
    const versionPath = version ? `/versions/${version}` : '';
    const response = await axios.get(`${baseUrl}/v2/models/${model}${versionPath}`, config);

    // Get detailed model metadata
    const metadataResponse = await axios.get(
      `${baseUrl}/v2/models/${model}${versionPath}/config`,
      config
    );

    return {
      result: response.data,
      raw: response.data,
      metadata: metadataResponse.data,
    };
  }

  /**
   * Run model inference
   */
  private async runInference(
    baseUrl: string,
    model: string,
    version: string,
    inputData: any,
    inputName: string,
    outputName: string,
    dataType: string,
    shape: number[] | undefined,
    batchSize: number,
    config: AxiosRequestConfig
  ): Promise<{ result: any, raw?: any, metadata?: any }> {
    // Prepare input data
    const processedData = this.prepareInputData(inputData, dataType, shape, batchSize);

    // Construct request payload
    const payload = {
      inputs: [
        {
          name: inputName,
          shape: processedData.shape,
          datatype: dataType,
          data: processedData.data,
        },
      ],
      outputs: [
        {
          name: outputName,
        },
      ],
    };

    // Create URL with optional version
    const versionPath = version ? `/versions/${version}` : '';
    const url = `${baseUrl}/v2/models/${model}${versionPath}/infer`;

    // Send inference request
    const response = await axios.post(url, payload, config);

    // Process the response
    const outputs = response.data.outputs || [];
    let result;

    if (outputs.length > 0) {
      result = this.processOutput(outputs[0]);
    } else {
      result = null;
    }

    return {
      result,
      raw: response.data,
    };
  }

  /**
   * Prepare input data for inference
   */
  private prepareInputData(
    data: any,
    dataType: string,
    providedShape: number[] | undefined,
    batchSize: number
  ): { data: any[]; shape: number[] } {
    // Handle different input types
    let processedData: any[];
    let shape: number[];

    // Ensure data is an array
    if (!Array.isArray(data)) {
      data = [data];
    }

    // Handle batching
    if (batchSize > 1 && data.length > 1) {
      // Slice data to match batch size
      data = data.slice(0, batchSize);
    }

    // Process based on data type
    switch (dataType) {
      case 'FP32':
      case 'FP16':
        // For float types, convert all values to numbers
        processedData = data.flat().map(Number);
        break;

      case 'INT32':
      case 'INT64':
        // For integer types, convert to integers
        processedData = data.flat().map(val => Math.floor(Number(val)));
        break;

      case 'BOOL':
        // For boolean, convert to 0 and 1
        processedData = data.flat().map(val => val ? 1 : 0);
        break;

      case 'STRING':
        // For strings, ensure all values are strings
        processedData = data.flat().map(String);
        break;

      default:
        throw new Error(`Unsupported data type: ${dataType}`);
    }

    // Determine shape
    if (providedShape) {
      shape = providedShape;
    } else {
      // Auto-determine shape based on input data
      if (Array.isArray(data[0])) {
        // If input is nested array, use that structure
        shape = [data.length, data[0].length];
      } else {
        // If flat array, use simple batch dimension
        shape = [data.length];
      }
    }

    return { data: processedData, shape };
  }

  /**
   * Process model output
   */
  private processOutput(output: any): any {
    // Extract relevant information from output
    const { data, shape, datatype } = output;

    // Process different output types
    switch (datatype) {
      case 'FP32':
      case 'FP16':
      case 'INT32':
      case 'INT64':
        // Numeric types - reshape if needed
        if (shape.length > 1) {
          return this.reshapeArray(data, shape);
        }
        return data;

      case 'BOOL':
        // Convert 0/1 to boolean
        return data.map(val => Boolean(val));

      case 'STRING':
        // String data
        return data;

      default:
        // Return raw data for unknown types
        return data;
    }
  }

  /**
   * Reshape a flat array into a multidimensional array based on shape
   */
  private reshapeArray(data: any[], shape: number[]): any[] {
    if (shape.length === 0) return data;

    // Handle 1D case
    if (shape.length === 1) {
      return data.slice(0, shape[0]);
    }

    // Handle multidimensional case
    const result = [];
    const size = shape.slice(1).reduce((a, b) => a * b, 1);

    for (let i = 0; i < shape[0]; i++) {
      const start = i * size;
      const chunk = data.slice(start, start + size);
      result.push(this.reshapeArray(chunk, shape.slice(1)));
    }

    return result;
  }
}

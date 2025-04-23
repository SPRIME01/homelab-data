import {
  IExecuteFunctions,
  INodeType,
  INodeTypeDescription,
  INodeExecutionData,
} from 'n8n-workflow';
import { HaApiHandler } from './HaApiHandler';
import { StateHistoryAnalyzer } from './StateHistoryAnalyzer';
import { TemplateRenderer } from './TemplateRenderer';
import { EntityFilter } from './EntityFilter';
import { EventMatcher } from './EventMatcher';

export class HomeAssistantEnhanced implements INodeType {
  description: INodeTypeDescription = {
    displayName: 'Home Assistant Enhanced',
    name: 'homeAssistantEnhanced',
    icon: 'file:home-assistant.svg',
    group: ['smart home'],
    version: 1,
    subtitle: '={{$parameter["operation"]}}',
    description: 'Enhanced Home Assistant integration with advanced features',
    defaults: {
      name: 'Home Assistant Enhanced',
    },
    inputs: ['main'],
    outputs: ['main'],
    credentials: [
      {
        name: 'homeAssistantEnhancedApi',
        required: true,
      },
    ],
    properties: [
      {
        displayName: 'Operation',
        name: 'operation',
        type: 'options',
        options: [
          {
            name: 'Get States',
            value: 'getStates',
            description: 'Get states with advanced filtering',
          },
          {
            name: 'Get History',
            value: 'getHistory',
            description: 'Get state history with analysis',
          },
          {
            name: 'Call Service',
            value: 'callService',
            description: 'Call service with enhanced features',
          },
          {
            name: 'Listen for Events',
            value: 'listenEvents',
            description: 'Listen for events with pattern matching',
          },
          {
            name: 'Render Template',
            value: 'renderTemplate',
            description: 'Render Home Assistant template',
          },
          {
            name: 'Batch Operation',
            value: 'batchOperation',
            description: 'Execute multiple operations at once',
          },
        ],
        default: 'getStates',
        noDataExpression: true,
        required: true,
      },

      // Advanced Entity Filtering
      {
        displayName: 'Entity Filter',
        name: 'entityFilter',
        type: 'json',
        default: '{"domain": "light", "attributes": {"brightness": {">": 50}}}',
        description: 'Advanced entity filter in JSON format',
        displayOptions: {
          show: {
            operation: ['getStates'],
          },
        },
      },

      // History Analysis Options
      {
        displayName: 'Analysis Type',
        name: 'analysisType',
        type: 'options',
        options: [
          {
            name: 'State Changes',
            value: 'stateChanges',
          },
          {
            name: 'Duration Analysis',
            value: 'duration',
          },
          {
            name: 'Pattern Detection',
            value: 'pattern',
          },
        ],
        default: 'stateChanges',
        displayOptions: {
          show: {
            operation: ['getHistory'],
          },
        },
      },

      // Template Rendering
      {
        displayName: 'Template',
        name: 'template',
        type: 'string',
        typeOptions: {
          rows: 4,
        },
        default: '',
        placeholder: '{{ states.sensor.temperature.state | float > 25 }}',
        displayOptions: {
          show: {
            operation: ['renderTemplate'],
          },
        },
      },

      // Event Pattern Matching
      {
        displayName: 'Event Pattern',
        name: 'eventPattern',
        type: 'json',
        default: '{"event_type": "state_changed", "data": {"entity_id": "sensor.*"}}',
        description: 'Event pattern to match in JSON format',
        displayOptions: {
          show: {
            operation: ['listenEvents'],
          },
        },
      },

      // Batch Operations
      {
        displayName: 'Operations',
        name: 'operations',
        type: 'fixedCollection',
        typeOptions: {
          multipleValues: true,
        },
        displayOptions: {
          show: {
            operation: ['batchOperation'],
          },
        },
        default: {},
        options: [
          {
            name: 'operation',
            displayName: 'Operation',
            values: [
              {
                displayName: 'Operation Type',
                name: 'type',
                type: 'options',
                options: [
                  {
                    name: 'Call Service',
                    value: 'callService',
                  },
                  {
                    name: 'Get State',
                    value: 'getState',
                  },
                ],
                default: 'callService',
              },
              {
                displayName: 'Configuration',
                name: 'config',
                type: 'json',
                default: '{}',
              },
            ],
          },
        ],
      },
    ],
  };

  async execute(this: IExecuteFunctions): Promise<INodeExecutionData[][]> {
    const operation = this.getNodeParameter('operation', 0) as string;
    const credentials = await this.getCredentials('homeAssistantEnhancedApi');

    const apiHandler = new HaApiHandler(credentials);
    const stateAnalyzer = new StateHistoryAnalyzer();
    const templateRenderer = new TemplateRenderer();
    const entityFilter = new EntityFilter();
    const eventMatcher = new EventMatcher();

    let result: any;

    try {
      switch (operation) {
        case 'getStates': {
          const filterConfig = this.getNodeParameter('entityFilter', 0) as object;
          const states = await apiHandler.getStates();
          result = entityFilter.filterEntities(states, filterConfig);
          break;
        }

        case 'getHistory': {
          const analysisType = this.getNodeParameter('analysisType', 0) as string;
          const history = await apiHandler.getHistory();
          result = stateAnalyzer.analyze(history, analysisType);
          break;
        }

        case 'renderTemplate': {
          const template = this.getNodeParameter('template', 0) as string;
          result = await templateRenderer.render(template, apiHandler);
          break;
        }

        case 'listenEvents': {
          const pattern = this.getNodeParameter('eventPattern', 0) as object;
          result = await eventMatcher.listen(pattern, apiHandler);
          break;
        }

        case 'batchOperation': {
          const operations = this.getNodeParameter('operations', 0) as {
            operation: Array<{ type: string; config: string }>;
          };
          result = await Promise.all(
            operations.operation.map(async (op) => {
              try {
                return await apiHandler.executeBatchOperation(op);
              } catch (error) {
                // Retry logic for failed API calls
                for (let attempt = 1; attempt <= 3; attempt++) {
                  try {
                    return await apiHandler.executeBatchOperation(op);
                  } catch (retryError) {
                    if (attempt === 3) {
                      throw retryError;
                    }
                    await new Promise((resolve) => setTimeout(resolve, 1000 * attempt));
                  }
                }
              }
            })
          );
          break;
        }

        default:
          throw new Error(`Operation ${operation} is not implemented`);
      }

      // Return the results
      return [this.helpers.returnJsonArray(result)];

    } catch (error) {
      if (this.continueOnFail()) {
        return [[{ json: { error: error.message } }]];
      }
      throw error;
    }
  }
}

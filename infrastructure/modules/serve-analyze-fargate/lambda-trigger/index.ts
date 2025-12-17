import { SFNClient, StartExecutionCommand } from '@aws-sdk/client-sfn';

const sfn = new SFNClient({});

const STATE_MACHINE_ARN = process.env.STATE_MACHINE_ARN!;
const CLUSTER_NAME = process.env.ECS_CLUSTER_NAME!;
const TASK_DEFINITION = process.env.TASK_DEFINITION_ARN!;
const SUBNET_IDS = process.env.SUBNET_IDS!.split(',');
const SECURITY_GROUP_ID = process.env.SECURITY_GROUP_ID!;
const S3_OUTPUT_BUCKET = process.env.S3_OUTPUT_BUCKET!;
const SNS_TOPIC_ARN = process.env.SNS_TOPIC_ARN!;
const V2_DLQ_URL = process.env.V2_DLQ_URL || '';

// ALB/S3 Pipeline Request (legacy)
interface LegacyPipelineRequest {
  campaign?: string;
  csvS3Path: string;
  testMode?: boolean;
  skipClassification?: boolean;
  skipClustering?: boolean;
  apiUrl?: string;
  environment?: string;
}

// Unified SQS Pipeline Request (supports both modes)
interface SQSPipelineRequest {
  mode?: 'cluster' | 'classify';  // default: 'classify' for backwards compat
  poll_id: string;
  campaign_id?: string;
  s3_bucket?: string;
  s3_key: string;
  // Required for classify mode
  question_text?: string;
  options?: string[];
  // Optional callbacks
  callback?: {
    success_url?: string;
    failure_url?: string;
  };
}

interface PipelineResponse {
  taskArn: string;
  campaign: string;
  inputS3Path: string;
  outputS3Path: string;
  message: string;
  pipelineMode: string;
}

interface S3Event {
  Records: Array<{
    eventName: string;
    s3: {
      bucket: {
        name: string;
      };
      object: {
        key: string;
      };
    };
  }>;
}

interface SQSEvent {
  Records: Array<{
    messageId: string;
    body: string;
    eventSource: string;
  }>;
}

interface SQSBatchResponse {
  batchItemFailures: Array<{ itemIdentifier: string }>;
}

function extractCampaignFromS3Path(s3Path: string): string {
  const parts = s3Path.split('/');
  const filename = parts[parts.length - 1];
  return filename.replace(/\.csv$/i, '');
}

function isS3Event(event: any): event is S3Event {
  return event.Records && Array.isArray(event.Records) && event.Records[0]?.s3;
}

function isSQSEvent(event: any): event is SQSEvent {
  return event.Records && Array.isArray(event.Records) && event.Records[0]?.eventSource === 'aws:sqs';
}

function isALBEvent(event: any): boolean {
  return event.requestContext?.elb !== undefined;
}

function extractBucketFromS3Path(s3Path: string): string | undefined {
  const match = s3Path.match(/^s3:\/\/([^\/]+)/);
  return match ? match[1] : undefined;
}

function getApiUrlFromBucket(bucketName: string): string {
  if (bucketName.includes('-qa')) {
    return 'https://ai-qa.goodparty.org/serve/messages';
  } else if (bucketName.includes('-prod')) {
    return 'https://ai.goodparty.org/serve/messages';
  } else {
    return 'https://ai-dev.goodparty.org/serve/messages';
  }
}

// Cluster Pipeline (open-ended questions - discovers themes)
async function processClusterPipeline(request: LegacyPipelineRequest, triggerSource: string, bucketName?: string): Promise<PipelineResponse> {
  const campaign = request.campaign || extractCampaignFromS3Path(request.csvS3Path);
  const timestamp = Date.now();
  const outputPrefix = `output/${campaign}/${timestamp}/`;

  const s3InputPath = request.csvS3Path;
  const effectiveBucketName = bucketName || extractBucketFromS3Path(request.csvS3Path);

  if (!request.apiUrl && !effectiveBucketName) {
    throw new Error(`Unable to determine API URL: csvS3Path "${request.csvS3Path}" is not a valid S3 path`);
  }

  const apiUrl = request.apiUrl || getApiUrlFromBucket(effectiveBucketName!);

  const environmentOverrides = [
    { Name: 'CAMPAIGN_NAME', Value: campaign },
    { Name: 'S3_INPUT_PATH', Value: s3InputPath },
    { Name: 'S3_OUTPUT_PATH', Value: `s3://${S3_OUTPUT_BUCKET}/${outputPrefix}` },
    { Name: 'API_URL', Value: apiUrl },
    { Name: 'ENVIRONMENT', Value: request.environment || 'production' },
    { Name: 'TEST_MODE', Value: String(request.testMode || false) },
    { Name: 'SKIP_CLASSIFICATION', Value: String(request.skipClassification || false) },
    { Name: 'SKIP_CLUSTERING', Value: String(request.skipClustering || false) },
    { Name: 'PIPELINE_MODE', Value: 'cluster' },
  ];

  const stepFunctionInput = {
    cluster: CLUSTER_NAME,
    taskDefinition: TASK_DEFINITION,
    subnets: SUBNET_IDS,
    securityGroups: [SECURITY_GROUP_ID],
    environment: environmentOverrides,
    tags: [
      { Key: 'S3InputPath', Value: s3InputPath },
      { Key: 'Campaign', Value: campaign },
      { Key: 'TriggerSource', Value: triggerSource },
      { Key: 'PipelineMode', Value: 'cluster' },
    ],
    campaign: campaign,
    s3Path: s3InputPath,
    snsTopicArn: SNS_TOPIC_ARN,
    pipelineMode: 'cluster',
  };

  const executionName = `v1-${campaign}-${timestamp}`;

  const startExecutionCommand = new StartExecutionCommand({
    stateMachineArn: STATE_MACHINE_ARN,
    name: executionName,
    input: JSON.stringify(stepFunctionInput),
  });

  const response = await sfn.send(startExecutionCommand);

  if (!response.executionArn) {
    throw new Error('Failed to start Step Functions execution');
  }

  return {
    taskArn: response.executionArn,
    campaign: campaign,
    inputS3Path: s3InputPath,
    outputS3Path: `s3://${S3_OUTPUT_BUCKET}/${outputPrefix}`,
    message: 'V1 Pipeline (clustering) execution started',
    pipelineMode: 'cluster',
  };
}

// Classify Pipeline (closed questions - classifies into predefined options)
async function processClassifyPipeline(request: SQSPipelineRequest): Promise<PipelineResponse> {
  const campaign = request.campaign_id || request.poll_id;
  const timestamp = Date.now();
  const outputPrefix = `output_v2/${request.poll_id}/${timestamp}/`;

  const s3Bucket = request.s3_bucket || S3_OUTPUT_BUCKET;
  const s3InputPath = `s3://${s3Bucket}/${request.s3_key}`;

  const environmentOverrides = [
    { Name: 'CAMPAIGN_NAME', Value: campaign },
    { Name: 'POLL_ID', Value: request.poll_id },
    { Name: 'S3_INPUT_PATH', Value: s3InputPath },
    { Name: 'S3_OUTPUT_PATH', Value: `s3://${S3_OUTPUT_BUCKET}/${outputPrefix}` },
    { Name: 'ENVIRONMENT', Value: 'production' },
    { Name: 'PIPELINE_MODE', Value: 'classify' },
    { Name: 'QUESTION_TEXT', Value: request.question_text },
    { Name: 'OPTIONS_JSON', Value: JSON.stringify(request.options) },
    { Name: 'CALLBACK_SUCCESS_URL', Value: request.callback?.success_url || '' },
    { Name: 'CALLBACK_FAILURE_URL', Value: request.callback?.failure_url || '' },
  ];

  const stepFunctionInput = {
    cluster: CLUSTER_NAME,
    taskDefinition: TASK_DEFINITION,
    subnets: SUBNET_IDS,
    securityGroups: [SECURITY_GROUP_ID],
    environment: environmentOverrides,
    tags: [
      { Key: 'S3InputPath', Value: s3InputPath },
      { Key: 'Campaign', Value: campaign },
      { Key: 'PollId', Value: request.poll_id },
      { Key: 'TriggerSource', Value: 'SQS' },
      { Key: 'PipelineMode', Value: 'classify' },
    ],
    campaign: campaign,
    pollId: request.poll_id,
    s3Path: s3InputPath,
    snsTopicArn: SNS_TOPIC_ARN,
    pipelineMode: 'classify',
    v2DlqUrl: V2_DLQ_URL,
    originalRequest: request,
  };

  const executionName = `v2-${request.poll_id}-${timestamp}`;

  const startExecutionCommand = new StartExecutionCommand({
    stateMachineArn: STATE_MACHINE_ARN,
    name: executionName,
    input: JSON.stringify(stepFunctionInput),
  });

  const response = await sfn.send(startExecutionCommand);

  if (!response.executionArn) {
    throw new Error('Failed to start Step Functions execution');
  }

  return {
    taskArn: response.executionArn,
    campaign: campaign,
    inputS3Path: s3InputPath,
    outputS3Path: `s3://${S3_OUTPUT_BUCKET}/${outputPrefix}`,
    message: 'V2 Pipeline (classification) execution started',
    pipelineMode: 'classify',
  };
}

// Cluster Pipeline via SQS (open-ended questions)
async function processClusterPipelineViaSQS(request: SQSPipelineRequest): Promise<PipelineResponse> {
  const campaign = request.campaign_id || request.poll_id;
  const timestamp = Date.now();
  const outputPrefix = `output/${campaign}/${timestamp}/`;

  const s3Bucket = request.s3_bucket || S3_OUTPUT_BUCKET;
  const s3InputPath = `s3://${s3Bucket}/${request.s3_key}`;

  const environmentOverrides = [
    { Name: 'CAMPAIGN_NAME', Value: campaign },
    { Name: 'S3_INPUT_PATH', Value: s3InputPath },
    { Name: 'S3_OUTPUT_PATH', Value: `s3://${S3_OUTPUT_BUCKET}/${outputPrefix}` },
    { Name: 'ENVIRONMENT', Value: 'production' },
    { Name: 'PIPELINE_MODE', Value: 'cluster' },
    { Name: 'CALLBACK_SUCCESS_URL', Value: request.callback?.success_url || '' },
    { Name: 'CALLBACK_FAILURE_URL', Value: request.callback?.failure_url || '' },
  ];

  const stepFunctionInput = {
    cluster: CLUSTER_NAME,
    taskDefinition: TASK_DEFINITION,
    subnets: SUBNET_IDS,
    securityGroups: [SECURITY_GROUP_ID],
    environment: environmentOverrides,
    tags: [
      { Key: 'S3InputPath', Value: s3InputPath },
      { Key: 'Campaign', Value: campaign },
      { Key: 'PollId', Value: request.poll_id },
      { Key: 'TriggerSource', Value: 'SQS' },
      { Key: 'PipelineMode', Value: 'cluster' },
    ],
    campaign: campaign,
    pollId: request.poll_id,
    s3Path: s3InputPath,
    snsTopicArn: SNS_TOPIC_ARN,
    pipelineMode: 'cluster',
    originalRequest: request,
  };

  const executionName = `cluster-${request.poll_id}-${timestamp}`;

  const startExecutionCommand = new StartExecutionCommand({
    stateMachineArn: STATE_MACHINE_ARN,
    name: executionName,
    input: JSON.stringify(stepFunctionInput),
  });

  const response = await sfn.send(startExecutionCommand);

  if (!response.executionArn) {
    throw new Error('Failed to start Step Functions execution');
  }

  return {
    taskArn: response.executionArn,
    campaign: campaign,
    inputS3Path: s3InputPath,
    outputS3Path: `s3://${S3_OUTPUT_BUCKET}/${outputPrefix}`,
    message: 'Cluster Pipeline (open-ended) execution started',
    pipelineMode: 'cluster',
  };
}

export const handler = async (event: any): Promise<{ statusCode: number; body: string } | SQSBatchResponse> => {
  try {
    // Handle SQS events (supports both cluster and classify modes)
    if (isSQSEvent(event)) {
      console.log('Processing SQS event');
      const batchItemFailures: Array<{ itemIdentifier: string }> = [];

      for (const record of event.Records) {
        try {
          const request: SQSPipelineRequest = JSON.parse(record.body);

          if (!request.poll_id || !request.s3_key) {
            throw new Error('Missing required fields: poll_id and s3_key');
          }

          // Determine mode: default to 'classify' for backwards compatibility
          const mode = request.mode || 'classify';

          if (mode === 'cluster') {
            console.log(`Cluster Pipeline request: poll_id=${request.poll_id}`);
            const result = await processClusterPipelineViaSQS(request);
            console.log(`Cluster Pipeline started: ${result.taskArn}`);
          } else {
            // classify mode
            if (!request.question_text) {
              throw new Error('Missing required field for classify mode: question_text');
            }

            if (!Array.isArray(request.options)) {
              request.options = [];
            }

            console.log(`Classify Pipeline request: poll_id=${request.poll_id}, options=${JSON.stringify(request.options)}`);
            const result = await processClassifyPipeline(request);
            console.log(`Classify Pipeline started: ${result.taskArn}`);
          }

        } catch (error: any) {
          console.error(`Failed to process SQS message ${record.messageId}:`, error);
          batchItemFailures.push({ itemIdentifier: record.messageId });
        }
      }

      return { batchItemFailures };
    }

    // Handle S3 events (Cluster Pipeline - legacy trigger)
    if (isS3Event(event)) {
      console.log('Processing S3 event (Cluster Pipeline)');
      const record = event.Records[0];
      const bucketName = record.s3.bucket.name;
      const objectKey = decodeURIComponent(record.s3.object.key.replace(/\+/g, ' '));

      if (!objectKey.endsWith('.csv')) {
        console.log(`Skipping non-CSV file: ${objectKey}`);
        return {
          statusCode: 200,
          body: JSON.stringify({ message: 'Skipped non-CSV file' }),
        };
      }

      // Only trigger cluster for input/ prefix
      if (!objectKey.startsWith('input/')) {
        console.log(`Skipping file not in input/ prefix: ${objectKey}`);
        return {
          statusCode: 200,
          body: JSON.stringify({ message: 'Skipped file outside input/ prefix' }),
        };
      }

      const request: LegacyPipelineRequest = {
        csvS3Path: `s3://${bucketName}/${objectKey}`,
        environment: 'production',
      };

      console.log(`Cluster Pipeline S3 trigger: ${request.csvS3Path}`);
      const result = await processClusterPipeline(request, 'S3Upload', bucketName);

      return {
        statusCode: 202,
        body: JSON.stringify(result),
      };
    }

    // Handle ALB events (Cluster Pipeline - API trigger)
    if (isALBEvent(event)) {
      console.log('Processing ALB event (Cluster Pipeline)');
      const request: LegacyPipelineRequest = JSON.parse(event.body || '{}');

      if (!request.csvS3Path) {
        return {
          statusCode: 400,
          body: JSON.stringify({ error: 'csvS3Path is required' }),
        };
      }

      const result = await processClusterPipeline(request, 'ALB');

      return {
        statusCode: 202,
        body: JSON.stringify(result),
      };
    }

    console.error('Unknown event type:', JSON.stringify(event));
    return {
      statusCode: 400,
      body: JSON.stringify({ error: 'Unsupported event type' }),
    };

  } catch (error: any) {
    console.error('Error in Lambda handler:', error);
    return {
      statusCode: 500,
      body: JSON.stringify({ error: error.message }),
    };
  }
};

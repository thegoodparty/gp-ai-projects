import { SFNClient, StartExecutionCommand } from '@aws-sdk/client-sfn';

const sfn = new SFNClient({});

const STATE_MACHINE_ARN = process.env.STATE_MACHINE_ARN!;
const CLUSTER_NAME = process.env.ECS_CLUSTER_NAME!;
const TASK_DEFINITION = process.env.TASK_DEFINITION_ARN!;
const SUBNET_IDS = process.env.SUBNET_IDS!.split(',');
const SECURITY_GROUP_ID = process.env.SECURITY_GROUP_ID!;
const S3_OUTPUT_BUCKET = process.env.S3_OUTPUT_BUCKET!;
const SNS_TOPIC_ARN = process.env.SNS_TOPIC_ARN!;

interface PipelineRequest {
  campaign?: string;
  csvS3Path: string;
  testMode?: boolean;
  skipClassification?: boolean;
  skipClustering?: boolean;
  apiUrl?: string;
  environment?: string;
}

interface PipelineResponse {
  taskArn: string;
  campaign: string;
  inputS3Path: string;
  outputS3Path: string;
  message: string;
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

function extractCampaignFromS3Path(s3Path: string): string {
  const parts = s3Path.split('/');
  const filename = parts[parts.length - 1];
  return filename.replace(/\.csv$/i, '');
}

function isS3Event(event: any): event is S3Event {
  return event.Records && Array.isArray(event.Records) && event.Records[0]?.s3;
}

function isALBEvent(event: any): boolean {
  return event.requestContext?.elb !== undefined;
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

async function processPipeline(request: PipelineRequest, triggerSource: string, bucketName?: string): Promise<PipelineResponse> {
  const campaign = request.campaign || extractCampaignFromS3Path(request.csvS3Path);
  const timestamp = Date.now();
  const outputPrefix = `output/${campaign}/${timestamp}/`;

  const s3InputPath = request.csvS3Path;

  const apiUrl = request.apiUrl || (bucketName ? getApiUrlFromBucket(bucketName) : 'https://ai-dev.goodparty.org');

  const environmentOverrides = [
    { Name: 'CAMPAIGN_NAME', Value: campaign },
    { Name: 'S3_INPUT_PATH', Value: s3InputPath },
    { Name: 'S3_OUTPUT_PATH', Value: `s3://${S3_OUTPUT_BUCKET}/${outputPrefix}` },
    { Name: 'API_URL', Value: apiUrl },
    { Name: 'ENVIRONMENT', Value: request.environment || 'production' },
    { Name: 'TEST_MODE', Value: String(request.testMode || false) },
    { Name: 'SKIP_CLASSIFICATION', Value: String(request.skipClassification || false) },
    { Name: 'SKIP_CLUSTERING', Value: String(request.skipClustering || false) },
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
    ],
    campaign: campaign,
    s3Path: s3InputPath,
    snsTopicArn: SNS_TOPIC_ARN,
  };

  const executionName = `${campaign}-${timestamp}`;

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
    message: 'Pipeline execution started with automatic retry',
  };
}

export const handler = async (event: any): Promise<{ statusCode: number; body: string }> => {
  try {
    let request: PipelineRequest;
    let triggerSource: string;
    let bucketName: string | undefined;

    if (isS3Event(event)) {
      console.log('Processing S3 event');
      const record = event.Records[0];
      bucketName = record.s3.bucket.name;
      const objectKey = decodeURIComponent(record.s3.object.key.replace(/\+/g, ' '));

      if (!objectKey.endsWith('.csv')) {
        console.log(`Skipping non-CSV file: ${objectKey}`);
        return {
          statusCode: 200,
          body: JSON.stringify({ message: 'Skipped non-CSV file' }),
        };
      }

      request = {
        csvS3Path: `s3://${bucketName}/${objectKey}`,
        environment: 'production',
      };
      triggerSource = 'S3Upload';

      console.log(`S3 trigger: ${request.csvS3Path}`);
    } else if (isALBEvent(event)) {
      console.log('Processing ALB event');
      request = JSON.parse(event.body || '{}');

      if (!request.csvS3Path) {
        return {
          statusCode: 400,
          body: JSON.stringify({ error: 'csvS3Path is required' }),
        };
      }
      triggerSource = 'ALB';
    } else {
      console.error('Unknown event type:', JSON.stringify(event));
      return {
        statusCode: 400,
        body: JSON.stringify({ error: 'Unsupported event type' }),
      };
    }

    const result = await processPipeline(request, triggerSource, bucketName);

    return {
      statusCode: 202,
      body: JSON.stringify(result),
    };
  } catch (error: any) {
    console.error('Error starting pipeline task:', error);
    return {
      statusCode: 500,
      body: JSON.stringify({ error: error.message }),
    };
  }
};

import {
  SecretsManagerClient,
  GetSecretValueCommand,
} from '@aws-sdk/client-secrets-manager';

interface SNSEvent {
  Records: Array<{
    Sns: {
      Message: string;
      Subject?: string;
    };
  }>;
}

interface TaskFailureMessage {
  alarm: string;
  environment: string;
  cluster: string;
  taskArn: string;
  stoppedReason: string;
  exitCode: number;
  time: string;
  logs: string;
}

interface AISecrets {
  SLACK_WEBHOOK_URL: string;
  GEMINI_API_KEY?: string;
  SERVE_API_KEY?: string;
}

const SECRET_NAME = process.env.SECRET_NAME!;
const SECRET_REGION = process.env.SECRET_REGION || process.env.AWS_REGION!;

const secretsClient = new SecretsManagerClient({ region: SECRET_REGION });

let cachedSlackWebhookUrl: string | null = null;

async function getSlackWebhookUrl(): Promise<string> {
  if (cachedSlackWebhookUrl) {
    return cachedSlackWebhookUrl;
  }

  try {
    const response = await secretsClient.send(
      new GetSecretValueCommand({
        SecretId: SECRET_NAME,
      })
    );

    if (!response.SecretString) {
      throw new Error('Secret string is empty');
    }

    const secrets: AISecrets = JSON.parse(response.SecretString);
    cachedSlackWebhookUrl = secrets.SLACK_WEBHOOK_URL;

    if (!cachedSlackWebhookUrl) {
      throw new Error('SLACK_WEBHOOK_URL not found in secret');
    }

    return cachedSlackWebhookUrl;
  } catch (error) {
    console.error('Error fetching secret:', error);
    throw error;
  }
}

function extractTaskId(taskArn: string): string {
  const parts = taskArn.split('/');
  return parts[parts.length - 1].substring(0, 8);
}

function getColorForExitCode(exitCode: number): string {
  if (exitCode === 1) return '#FF0000';
  if (exitCode === 137) return '#FF6600';
  if (exitCode === 143) return '#FF9900';
  return '#CC0000';
}

function formatSlackMessage(message: TaskFailureMessage): any {
  const taskId = extractTaskId(message.taskArn);
  const color = getColorForExitCode(message.exitCode);

  return {
    username: 'ECS Pipeline Monitor',
    icon_emoji: ':rotating_light:',
    attachments: [
      {
        color: color,
        title: ':x: ECS Task Failed',
        title_link: message.logs,
        fields: [
          {
            title: 'Environment',
            value: message.environment,
            short: true,
          },
          {
            title: 'Task ID',
            value: taskId,
            short: true,
          },
          {
            title: 'Exit Code',
            value: String(message.exitCode),
            short: true,
          },
          {
            title: 'Time',
            value: new Date(message.time).toLocaleString(),
            short: true,
          },
          {
            title: 'Stopped Reason',
            value: message.stoppedReason,
            short: false,
          },
        ],
        footer: 'ECS Task Failure Monitor',
        footer_icon: 'https://a0.awsstatic.com/libra-css/images/logos/aws_logo_smile_1200x630.png',
        ts: Math.floor(new Date(message.time).getTime() / 1000),
        actions: [
          {
            type: 'button',
            text: 'View Logs',
            url: message.logs,
            style: 'danger',
          },
        ],
      },
    ],
  };
}

function formatStepFunctionsFailure(messageText: string): any {
  return {
    username: 'Pipeline Monitor',
    icon_emoji: ':rotating_light:',
    attachments: [
      {
        color: '#FF0000',
        title: ':x: Pipeline Failed',
        text: messageText,
        footer: 'Step Functions Pipeline Monitor',
        footer_icon: 'https://a0.awsstatic.com/libra-css/images/logos/aws_logo_smile_1200x630.png',
        ts: Math.floor(Date.now() / 1000),
      },
    ],
  };
}

export const handler = async (event: SNSEvent): Promise<void> => {
  console.log('Received SNS event:', JSON.stringify(event));

  const slackWebhookUrl = await getSlackWebhookUrl();

  for (const record of event.Records) {
    try {
      let slackPayload: any;

      try {
        const message: TaskFailureMessage = JSON.parse(record.Sns.Message);
        slackPayload = formatSlackMessage(message);
        console.log('Processing as ECS task failure');
      } catch (parseError) {
        console.log('Processing as Step Functions text message');
        slackPayload = formatStepFunctionsFailure(record.Sns.Message);
      }

      console.log('Sending to Slack:', JSON.stringify(slackPayload));

      const response = await fetch(slackWebhookUrl, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify(slackPayload),
      });

      if (!response.ok) {
        const errorText = await response.text();
        throw new Error(`Slack API error: ${response.status} - ${errorText}`);
      }

      console.log('Successfully sent to Slack');
    } catch (error) {
      console.error('Error processing notification:', error);
      throw error;
    }
  }
};

import json
import os
import boto3
from datetime import datetime, timedelta

ecs = boto3.client('ecs')

def handler(event, context):
    try:
        if event.get('requestContext', {}).get('elb'):
            body = json.loads(event.get('body', '{}'))
        else:
            body = event if isinstance(event, dict) else {}

        hubspot_table = body.get('hubspot_table', 'dbt.m_general__candidacy')
        ddhq_table = body.get('ddhq_table', 'dbt.stg_airbyte_source__ddhq_gdrive_election_results')

        embedding_batch_size = str(body.get('embedding_batch_size', 100))
        embedding_max_workers = str(body.get('embedding_max_workers', 80))
        matching_batch_size = str(body.get('matching_batch_size', 1000))
        matching_max_workers = str(body.get('matching_max_workers', 2000))

        run_id = datetime.utcnow().strftime('%Y%m%d_%H%M%S')
        s3_output_prefix = f"output/{run_id}"

        environment_overrides = [
            {'name': 'RUN_ID', 'value': run_id},
            {'name': 'S3_OUTPUT_PREFIX', 'value': s3_output_prefix},
            {'name': 'HUBSPOT_TABLE', 'value': hubspot_table},
            {'name': 'DDHQ_TABLE', 'value': ddhq_table},
            {'name': 'EMBEDDING_BATCH_SIZE', 'value': embedding_batch_size},
            {'name': 'EMBEDDING_MAX_WORKERS', 'value': embedding_max_workers},
            {'name': 'MATCHING_BATCH_SIZE', 'value': matching_batch_size},
            {'name': 'MATCHING_MAX_WORKERS', 'value': matching_max_workers}
        ]

        response = ecs.run_task(
            cluster=os.environ['ECS_CLUSTER_NAME'],
            taskDefinition=os.environ['TASK_DEFINITION_ARN'],
            launchType='FARGATE',
            tags=[{'key': 'Project', 'value': 'ddhq-matcher'}],
            networkConfiguration={
                'awsvpcConfiguration': {
                    'subnets': os.environ['SUBNET_IDS'].split(','),
                    'securityGroups': [os.environ['SECURITY_GROUP_ID']],
                    'assignPublicIp': 'DISABLED'
                }
            },
            overrides={
                'containerOverrides': [
                    {
                        'name': 'ddhq-matcher',
                        'environment': environment_overrides
                    }
                ]
            }
        )

        task_arn = response['tasks'][0]['taskArn']
        estimated_completion = (datetime.utcnow() + timedelta(minutes=30)).isoformat() + 'Z'

        result = {
            'status': 'STARTED',
            'run_id': run_id,
            's3_output': {
                'bucket': os.environ['S3_OUTPUT_BUCKET'],
                'prefix': s3_output_prefix,
                'file': f"s3://{os.environ['S3_OUTPUT_BUCKET']}/{s3_output_prefix}/matches.parquet"
            },
            'task_arn': task_arn,
            'estimated_completion': estimated_completion,
            'config': {
                'hubspot_table': hubspot_table,
                'ddhq_table': ddhq_table,
                'embedding_batch_size': int(embedding_batch_size),
                'embedding_max_workers': int(embedding_max_workers),
                'matching_batch_size': int(matching_batch_size),
                'matching_max_workers': int(matching_max_workers)
            }
        }

        if event.get('requestContext', {}).get('elb'):
            return {
                'statusCode': 202,
                'headers': {
                    'Content-Type': 'application/json'
                },
                'body': json.dumps(result),
                'isBase64Encoded': False
            }
        else:
            return result

    except Exception as e:
        error_response = {
            'error': str(e),
            'message': 'Failed to start DDHQ matcher task'
        }

        if event.get('requestContext', {}).get('elb'):
            return {
                'statusCode': 500,
                'headers': {
                    'Content-Type': 'application/json'
                },
                'body': json.dumps(error_response),
                'isBase64Encoded': False
            }
        else:
            raise

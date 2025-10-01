#!/usr/bin/env python3

import boto3
import sys
from botocore.exceptions import ClientError

sys.path.append('/Users/collinpark/work/gp-ai-projects')
from shared.logger import get_logger

logger = get_logger(__name__)


def clear_dynamodb_table(table_name: str = 'serve-messages-dev', region: str = 'us-west-2'):
    """
    Clear all items from a DynamoDB table
    """
    dynamodb = boto3.resource('dynamodb', region_name=region)
    table = dynamodb.Table(table_name)

    logger.info(f"Clearing DynamoDB table: {table_name}")

    try:
        scan_kwargs = {}
        deleted_count = 0

        while True:
            response = table.scan(**scan_kwargs)
            items = response.get('Items', [])

            if not items:
                break

            with table.batch_writer() as batch:
                for item in items:
                    batch.delete_item(
                        Key={
                            'campaign_id': item['campaign_id'],
                            'record_id': item['record_id']
                        }
                    )
                    deleted_count += 1

            logger.info(f"Deleted {deleted_count} items so far...")

            if 'LastEvaluatedKey' not in response:
                break

            scan_kwargs['ExclusiveStartKey'] = response['LastEvaluatedKey']

        logger.info(f"✅ Successfully deleted {deleted_count} items from {table_name}")
        return deleted_count

    except ClientError as e:
        logger.error(f"Failed to clear table: {e}")
        raise


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description='Clear DynamoDB table')
    parser.add_argument('--table', default='serve-messages-dev', help='Table name')
    parser.add_argument('--region', default='us-west-2', help='AWS region')

    args = parser.parse_args()

    clear_dynamodb_table(args.table, args.region)
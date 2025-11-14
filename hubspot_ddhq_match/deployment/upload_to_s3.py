#!/usr/bin/env python3
"""
Upload pipeline results to S3 with verification.
"""

import os
import sys
import boto3
from botocore.exceptions import ClientError


def upload_file_to_s3(local_file: str, bucket: str, key: str) -> bool:
    """
    Upload a file to S3 and verify the upload.

    Args:
        local_file: Path to local file
        bucket: S3 bucket name
        key: S3 object key (path in bucket)

    Returns:
        True if upload successful and verified, False otherwise
    """
    if not os.path.exists(local_file):
        print(f"❌ Error: Local file not found: {local_file}")
        return False

    try:
        s3_client = boto3.client('s3')

        file_size = os.path.getsize(local_file)
        print(f"📤 Uploading {os.path.basename(local_file)} ({file_size:,} bytes)...")

        s3_client.upload_file(local_file, bucket, key)

        print("🔍 Verifying upload...")
        head_response = s3_client.head_object(Bucket=bucket, Key=key)

        uploaded_size = head_response['ContentLength']
        if uploaded_size != file_size:
            print(f"❌ Error: Size mismatch - local: {file_size}, uploaded: {uploaded_size}")
            return False

        print(f"✅ Upload verified: s3://{bucket}/{key}")
        return True

    except ClientError as e:
        print(f"❌ S3 upload failed: {e}")
        return False
    except Exception as e:
        print(f"❌ Unexpected error: {e}")
        return False


if __name__ == "__main__":
    if len(sys.argv) != 4:
        print("Usage: python upload_to_s3.py <local_file> <s3_bucket> <s3_key>")
        sys.exit(1)

    local_file = sys.argv[1]
    bucket = sys.argv[2]
    key = sys.argv[3]

    success = upload_file_to_s3(local_file, bucket, key)
    sys.exit(0 if success else 1)

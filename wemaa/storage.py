"""Private screenshot storage; credentials are supplied only at runtime."""
from functools import lru_cache

import boto3
from botocore.config import Config

from wemaa.config import settings


@lru_cache
def get_client():
    return boto3.client(
        "s3", endpoint_url=settings.MINIO_ENDPOINT,
        aws_access_key_id=settings.MINIO_ACCESS_KEY,
        aws_secret_access_key=settings.MINIO_SECRET_KEY,
        region_name=settings.MINIO_REGION,
        config=Config(signature_version="s3v4", retries={"max_attempts": 2}),
    )

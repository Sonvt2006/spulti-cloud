"""Lưu nội dung checkpoint trên S3."""

import json
import os
from uuid import uuid4

import boto3
from botocore.config import Config


class S3CheckpointStore:
    def __init__(self):
        self.bucket = os.getenv("SPULTI_CHECKPOINT_BUCKET")
        if not self.bucket:
            raise RuntimeError("Chưa cấu hình SPULTI_CHECKPOINT_BUCKET")

        self.client = boto3.client(
            "s3",
            region_name=os.getenv("AWS_REGION") or None,
            config=Config(
                connect_timeout=5,
                read_timeout=15,
                retries={
                    "max_attempts": 2,
                    "mode": "standard",
                },
            ),
        )

    def save(self, payload: dict) -> dict:
        job_id = payload["job_id"]
        step = payload["last_completed_step"]

        # Mỗi lần lưu dùng key riêng, tránh ghi đè checkpoint đang được dùng.
        key = f"checkpoints/{job_id}/{step}-{uuid4().hex}.json"

        self.client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            ContentType="application/json",
        )

        # Chỉ metadata này được lưu trong RDS.
        return {
            "storage": "s3",
            "bucket": self.bucket,
            "key": key,
            "status": payload["status"],
        }

    def load(self, reference: dict) -> dict:
        response = self.client.get_object(
            Bucket=reference["bucket"],
            Key=reference["key"],
        )

        body = response["Body"]
        try:
            return json.loads(body.read())
        finally:
            body.close()

"""Lưu nội dung checkpoint trên S3."""

import base64
import json
import os
import re
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
                signature_version="s3v4",
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

    def file_upload(self, job_id: str, size: int, sha256: str) -> dict:
        key = f"checkpoints/{job_id}/files/{uuid4().hex}.tar"
        checksum = base64.b64encode(bytes.fromhex(sha256)).decode()
        reference = {"key": key, "size": size, "sha256": sha256}
        url = self.client.generate_presigned_url(
            "put_object",
            Params={
                "Bucket": self.bucket,
                "Key": key,
                "ContentLength": size,
                "ChecksumSHA256": checksum,
            },
            ExpiresIn=600,
        )
        return {
            "url": url,
            "headers": {"x-amz-checksum-sha256": checksum},
            "reference": reference,
        }

    def verify_files(self, job_id: str, reference: dict) -> None:
        pattern = rf"checkpoints/{re.escape(job_id)}/files/[a-f0-9]{{32}}\.tar"
        if not re.fullmatch(pattern, reference["key"]):
            raise ValueError("File snapshot belongs to another job or has invalid key")
        result = self.client.head_object(
            Bucket=self.bucket, Key=reference["key"], ChecksumMode="ENABLED"
        )
        checksum = base64.b64encode(bytes.fromhex(reference["sha256"])).decode()
        if (
            result["ContentLength"] != reference["size"]
            or result.get("ChecksumSHA256") != checksum
        ):
            raise ValueError("File snapshot size/checksum mismatch")

    def file_download(self, job_id: str, reference: dict) -> dict:
        self.verify_files(job_id, reference)
        return {
            "url": self.client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self.bucket, "Key": reference["key"]},
                ExpiresIn=600,
            )
        }

"""`S3CompatibleStorage` (boto3: S3, R2, Garage). Checksums `when_required` (ADR-012 §6)."""

from typing import IO, Any

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from core import storage as base


class S3CompatibleStorage:
    def __init__(self, *, bucket: str, client: Any) -> None:
        self.bucket, self.client = bucket, client

    def put(
        self, key: str, stream: IO[bytes], *, content_type: str, content_length: int,
        metadata: dict[str, str] | None = None,
    ) -> base.ObjectInfo:  # fmt: skip
        self.client.put_object(
            Bucket=self.bucket, Key=base.check_key(key), Body=stream, ContentType=content_type,
            ContentLength=content_length, Metadata=dict(metadata or {}),
        )  # fmt: skip
        return base.ObjectInfo(key, content_length, content_type, dict(metadata or {}))

    def open(self, key: str) -> IO[bytes]:
        return self.client.get_object(Bucket=self.bucket, Key=base.check_key(key))["Body"]  # type: ignore[no-any-return]

    def head(self, key: str) -> base.ObjectInfo | None:
        try:
            meta = self.client.head_object(Bucket=self.bucket, Key=base.check_key(key))
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                return None
            raise
        size, content_type = meta["ContentLength"], meta["ContentType"]
        return base.ObjectInfo(key, size, content_type, meta.get("Metadata", {}))

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=base.check_key(key))

    def presign_get(
        self, key: str, *, expires_in: int, filename: str, content_type: str,
        disposition: str = "attachment",
    ) -> str:  # fmt: skip
        """`content_type` sale de `files.mime_type` (magic bytes), no del objeto subido."""
        params = {
            "Bucket": self.bucket,
            "Key": base.check_key(key),
            "ResponseContentType": content_type,
            "ResponseContentDisposition": base.disposition_header(
                filename, disposition, content_type
            ),
            "ResponseCacheControl": base.PRIVATE_CACHE,
        }
        return self.client.generate_presigned_url(  # type: ignore[no-any-return]
            "get_object", Params=params, ExpiresIn=base.check_expiry(expires_in)
        )

    def presign_put(
        self, key: str, *, expires_in: int, content_type: str, content_length: int,
        max_bytes: int,
    ) -> base.PresignedUpload:  # fmt: skip
        """`ContentLength` firmado (verificado en Garage real: otro tamaño → 403)."""
        headers = base.upload_headers(content_type, content_length, max_bytes)
        params = {"Bucket": self.bucket, "Key": base.check_key(key), "ContentType": content_type,
                  "ContentLength": content_length}  # fmt: skip
        url = self.client.generate_presigned_url(
            "put_object", Params=params, ExpiresIn=base.check_expiry(expires_in)
        )
        return base.PresignedUpload(url, headers, content_length)


def from_settings() -> S3CompatibleStorage:
    required = ("STORAGE_BUCKET", "STORAGE_ACCESS_KEY_ID", "STORAGE_SECRET_ACCESS_KEY")
    if missing := [name for name in required if not getattr(settings, name)]:
        raise ImproperlyConfigured("Falta configuración de storage: " + ", ".join(missing))
    config = Config(
        signature_version="s3v4",
        s3={"addressing_style": settings.STORAGE_ADDRESSING_STYLE},
        request_checksum_calculation="when_required",
        response_checksum_validation="when_required",
        connect_timeout=5,
        read_timeout=30,
        retries={"max_attempts": 3, "mode": "standard"},
    )
    client = boto3.client(
        "s3",
        endpoint_url=settings.STORAGE_ENDPOINT_URL or None,
        region_name=settings.STORAGE_REGION,
        aws_access_key_id=settings.STORAGE_ACCESS_KEY_ID,
        aws_secret_access_key=settings.STORAGE_SECRET_ACCESS_KEY,
        config=config,
    )
    return S3CompatibleStorage(bucket=settings.STORAGE_BUCKET, client=client)

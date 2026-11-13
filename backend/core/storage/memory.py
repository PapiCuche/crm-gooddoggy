"""`InMemoryStorage` para tests unitarios (ADR-008 §1). Nunca en producción."""

import io
from typing import IO

from core import storage as base


class InMemoryStorage:
    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, base.ObjectInfo]] = {}

    def put(
        self, key: str, stream: IO[bytes], *, content_type: str, content_length: int,
        metadata: dict[str, str] | None = None,
    ) -> base.ObjectInfo:  # fmt: skip
        base.check_key(key)
        data = stream.read()
        if len(data) != content_length:
            raise ValueError("content_length no coincide con los bytes recibidos")
        info = base.ObjectInfo(key, len(data), content_type, dict(metadata or {}))
        self.objects[key] = (data, info)
        return info

    def open(self, key: str) -> IO[bytes]:
        return io.BytesIO(self.objects[base.check_key(key)][0])

    def head(self, key: str) -> base.ObjectInfo | None:
        entry = self.objects.get(base.check_key(key))
        return entry[1] if entry else None

    def delete(self, key: str) -> None:
        self.objects.pop(base.check_key(key), None)

    def presign_get(
        self, key: str, *, expires_in: int, filename: str, content_type: str,
        disposition: str = "attachment",
    ) -> str:  # fmt: skip
        base.check_expiry(expires_in)
        header = base.disposition_header(filename, disposition, content_type)
        return (f"memory://{base.check_key(key)}?type={content_type}&disposition={header}"
                f"&cache={base.PRIVATE_CACHE}&expires={expires_in}")  # fmt: skip

    def presign_put(
        self, key: str, *, expires_in: int, content_type: str, content_length: int,
        max_bytes: int,
    ) -> base.PresignedUpload:  # fmt: skip
        headers = base.upload_headers(content_type, content_length, max_bytes)
        base.check_expiry(expires_in)
        return base.PresignedUpload(f"memory://{base.check_key(key)}", headers, content_length)

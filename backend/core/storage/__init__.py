"""Object storage S3-compatible (ADR-008): único módulo con boto3; claves `org/{org}/…` sin
nombres originales y URLs firmadas cortas. Diseño: docs/architecture/storage-http.md."""

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import IO, Any, Protocol
from urllib.parse import quote
from uuid import UUID

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.utils.module_loading import import_string

MAX_PRESIGN_SECONDS = 300
PURPOSES = frozenset({"message-media", "quote-pdf", "import", "avatar", "kb", "attachment"})
_UUID = "[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
KEY = re.compile(rf"org/{_UUID}/(?:{'|'.join(PURPOSES)})/[0-9]{{4}}/(?:0[1-9]|1[0-2])/{_UUID}")
INLINE_TYPES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp", "application/pdf"})
PRIVATE_CACHE = "private, no-store"  # toda descarga firmada: ni el navegador ni un proxy la guardan


@dataclass(frozen=True, slots=True)
class ObjectInfo:
    key: str
    size: int
    content_type: str
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PresignedUpload:  # `headers` (tipo y tamaño) van firmados: el cliente los envía tal cual
    url: str
    headers: dict[str, str]
    content_length: int


class ObjectStorageService(Protocol):
    def put(
        self, key: str, stream: IO[bytes], *, content_type: str, content_length: int,
        metadata: dict[str, str] | None = None,
    ) -> ObjectInfo: ...  # fmt: skip
    def open(self, key: str) -> IO[bytes]: ...
    def head(self, key: str) -> ObjectInfo | None: ...
    def delete(self, key: str) -> None: ...
    def ping(self) -> None: ...  # readiness: barato y sin efectos; lanza si no responde
    def presign_get(
        self, key: str, *, expires_in: int, filename: str, content_type: str,
        disposition: str = "attachment",
    ) -> str: ...  # fmt: skip
    def presign_put(
        self, key: str, *, expires_in: int, content_type: str, content_length: int,
        max_bytes: int,
    ) -> PresignedUpload: ...  # fmt: skip


def tenant_key(
    organization_id: UUID, purpose: str, file_id: UUID, now: datetime | None = None
) -> str:
    if purpose not in PURPOSES:
        raise ValueError(f"purpose inválido: {purpose!r}")
    when = now or datetime.now(UTC)
    return f"org/{organization_id}/{purpose}/{when:%Y}/{when:%m}/{file_id}"


def check_key(key: str) -> str:
    """Solo claves con la forma de `tenant_key` (sin nombres originales ni `..`)."""
    if not KEY.fullmatch(key):
        raise ValueError("Clave de storage inválida")
    return key


def upload_headers(content_type: str, content_length: int, max_bytes: int) -> dict[str, str]:
    """Tamaño declarado validado contra la política ANTES de emitir la URL."""
    if not 0 < content_length <= max_bytes:
        raise ValueError(f"content_length debe estar entre 1 y max_bytes ({max_bytes})")
    return {"Content-Type": content_type, "Content-Length": str(content_length)}


def check_expiry(expires_in: int) -> int:
    if not 0 < expires_in <= MAX_PRESIGN_SECONDS:
        raise ValueError(f"expires_in debe estar entre 1 y {MAX_PRESIGN_SECONDS} s")
    return expires_in


def disposition_header(filename: str, disposition: str, content_type: str) -> str:
    """RFC 6266: `filename` ASCII saneado + `filename*` UTF-8. `inline` solo para tipos seguros."""
    if disposition not in ("attachment", "inline"):
        raise ValueError("disposition inválida")
    if content_type not in INLINE_TYPES:
        disposition = "attachment"  # HTML, SVG… nunca se renderizan desde el storage
    name = filename.strip()[:100] or "archivo"
    ascii_name = "".join(c for c in name if c.isascii() and (c.isalnum() or c in "._- "))
    encoded = quote(name, safe="")
    return f"{disposition}; filename=\"{ascii_name or 'archivo'}\"; filename*=UTF-8''{encoded}"


def storage() -> ObjectStorageService:
    """Implementación por configuración (`STORAGE_BACKEND`), sin cambios de código."""
    backends: dict[str, Any] = {
        "memory": "core.storage.memory.InMemoryStorage",
        "s3": "core.storage.s3.from_settings",
    }
    if settings.STORAGE_BACKEND not in backends:
        raise ImproperlyConfigured(f"STORAGE_BACKEND desconocido: {settings.STORAGE_BACKEND!r}")
    return import_string(backends[settings.STORAGE_BACKEND])()  # type: ignore[no-any-return]


def ping(timeout: float) -> None:
    """Readiness (F1-14): un `HEAD` del bucket con `timeout` segundos y un solo intento; nunca
    escribe. Lanza si el almacenamiento no responde o falta su configuración."""
    if settings.STORAGE_BACKEND == "s3":
        from core.storage.s3 import from_settings

        from_settings(timeout=timeout).ping()
    else:
        storage().ping()

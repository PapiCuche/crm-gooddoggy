"""Medios salientes (F7-09b; ADR-029 §7, ADR-008): la imagen o el documento que sube una persona.

Es dato no confiable. El tipo sale de los magic bytes (JPEG y PNG, imagen; PDF, documento), nunca
del que declara el cliente; el tamaño, de los bytes leídos (`media.Sink`), con un tope por tipo (a
confirmar). Sin antivirus (D-F7-04) no se guarda nada salvo con `MEDIA_SEND_UNSCANNED`. El blob va
al almacén privado **antes** de la transacción del comando, que crea el archivo
(`scan_status = PENDING`) y el mensaje; si el comando falla, o era un reintento, quien llama lo
borra con `discard`. Un archivo saliente nunca se sirve al navegador (`selectors.SERVED` pide un
mensaje entrante). Propósito `message-media-out` (F7-09b2), que el reclamo exige: un saliente nunca
lleva un blob entrante. La ruta lo lee con `Capped`. Registros: ids y la clase de un error."""

import re
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from django.conf import settings
from django.core.files.uploadhandler import FileUploadHandler, StopUpload

from apps.conversations import media
from core import storage
from core.ids import new_id
from core.observability.logging import get_logger
from core.tenancy.context import TenantContext

logger = get_logger(__name__)
TYPES = {"image/jpeg": "IMAGE", "image/png": "IMAGE", "application/pdf": "DOCUMENT"}
CAPS = {"IMAGE": 5 << 20, "DOCUMENT": 10 << 20}  # bytes; decisión del propietario abierta
LABELS = {"IMAGE": "Imagen", "DOCUMENT": "Documento"}  # el cuerpo de un medio sin pie de foto
NAME_MAX = 100
CAPTION_MAX = 1024  # el pie de foto que admite WhatsApp, en unidades UTF-16
PURPOSE = "message-media-out"


class Refused(Exception):  # noqa: N818 — un motivo cerrado, no un fallo
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class Prepared:
    """El blob ya guardado y lo que el comando escribe de él en `files`."""

    file_id: UUID
    key: str
    kind: str
    mime: str
    size: int
    sha256: str
    name: str


def units(text: str) -> int:
    """La longitud en unidades UTF-16 (un emoji fuera del plano básico cuenta 2)."""
    return len(text.encode("utf-16-le")) // 2


class Capped(FileUploadHandler):
    """**Un** archivo, `file`, de hasta `limit` bytes, con su `sha256` al paso (`sink`), nunca en
    `request.FILES`. Otro archivo u otro nombre (`ONE_FILE`) o un byte de más paran la lectura."""

    def __init__(self, limit: int) -> None:
        super().__init__()
        self.limit, self.refused = limit, ""
        self.sink: media.Sink | None = None

    def new_file(self, field_name: str, *args: Any, **kwargs: Any) -> None:
        super().new_file(field_name, *args, **kwargs)
        if self.sink is not None or field_name != "file":
            self.refused = "ONE_FILE"
            raise StopUpload
        self.sink = media.Sink()

    def receive_data_chunk(self, raw_data: bytes, start: int) -> None:
        assert self.sink is not None  # noqa: S101 — `new_file` lo crea antes del primer trozo
        if self.sink.size + len(raw_data) > self.limit:
            self.refused = "MEDIA_TOO_LARGE"
            raise StopUpload
        self.sink.write(raw_data)

    def file_complete(self, file_size: int) -> None:
        pass  # el archivo es `sink`


def sniff(head: bytes) -> str | None:
    if head[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    return "application/pdf" if head[:5] == b"%PDF-" else None


def filename(name: str | None, kind: str, mime: str) -> str:
    """El nombre que ve el cliente: uno fijo para una imagen; el del documento, sin ruta, sin
    caracteres invisibles ni de control, acotado y siempre `.pdf`."""
    if kind == "IMAGE":
        return f"imagen.{media.EXTENSIONS[mime]}"
    base = "".join(c for c in re.split(r"[\\/]", name or "")[-1] if c.isprintable()).strip()
    stem = base[:-4] if base.lower().endswith(".pdf") else base
    return (stem[: NAME_MAX - 4].strip() or "documento") + ".pdf"


def prepare(ctx: TenantContext, sink: media.Sink, name: str | None) -> Prepared:
    """El tipo real y el tope de lo leído, y el blob en el almacén. `Refused`: sin antivirus
    (`MEDIA_NOT_SCANNED`, antes de mirar nada), tipo no permitido o mayor que su tope."""
    if not settings.MEDIA_SEND_UNSCANNED:
        raise Refused("MEDIA_NOT_SCANNED")
    sink.file.seek(0)
    mime = sniff(sink.file.read(8))
    if mime is None:
        raise Refused("MEDIA_TYPE_NOT_ALLOWED")
    kind = TYPES[mime]
    if sink.size > CAPS[kind]:
        raise Refused("MEDIA_TOO_LARGE")
    file_id = new_id()
    key = storage.tenant_key(ctx.organization_id, PURPOSE, file_id)
    digest, shown = sink.digest.hexdigest(), filename(name, kind, mime)
    made = Prepared(file_id, key, kind, mime, sink.size, digest, shown)
    sink.file.seek(0)
    try:
        storage.storage().put(made.key, sink.file, content_type=mime, content_length=sink.size)
    except Exception:
        discard(made)  # puede quedar a medias
        raise
    return made


def check(ctx: TenantContext, prepared: Any) -> Prepared:
    """Lo que el comando exige de un archivo preparado: de esta organización, con la clave de su
    id, un tipo permitido y dentro del tope; si no, `ValueError`."""
    ok = isinstance(prepared, Prepared) and TYPES.get(prepared.mime) == prepared.kind
    prefix = f"org/{ctx.organization_id}/{PURPOSE}/"
    ok = ok and prepared.key.startswith(prefix) and prepared.key.endswith(f"/{prepared.file_id}")
    if not ok or not 0 < prepared.size <= CAPS[prepared.kind]:
        raise ValueError("medio: no preparado para esta organización")
    storage.check_key(prepared.key)
    return prepared  # type: ignore[no-any-return]


def discard(prepared: Prepared) -> None:
    try:
        storage.storage().delete(prepared.key)
    except Exception as error:  # el blob queda huérfano (conciliación, ADR-008)
        logger.error("media.outbound_delete_failed", file_id=str(prepared.file_id),
                     error=type(error).__name__)  # fmt: skip

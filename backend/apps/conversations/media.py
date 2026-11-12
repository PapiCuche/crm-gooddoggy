"""Medios entrantes de WhatsApp (F7-09a1b; ADR-029 §7): el adjunto de un mensaje y su máquina de
estados en la aplicación (la base la hace cumplir además: disparador de `0021`). La descarga, la
tarea `fetch_media` y el alta desde `inbound.receive` llegan con F7-09a1c. Un intento: **reclamar**
en una transacción (tope por organización, cuenta `CONNECTED` con credencial viva, `attempt_id`
nuevo, plazo de 300 s); bajar fuera de toda transacción; **guardar** el blob con la clave del
intento (nunca se sobrescribe) y cerrar en otra transacción solo si el reclamo sigue siendo suyo;
si no, o si la base falla, se borra solo ese blob. Lo que declara Meta no se guarda. Los registros
llevan ids y códigos cerrados: nunca la URL, el token ni el texto de un error."""

import datetime as dt
import hashlib
import re
import tempfile
from dataclasses import dataclass
from typing import IO, Any
from uuid import UUID

from django.conf import settings
from django.db import connections
from django.db.models import Q
from django.utils import timezone

from apps.conversations.models import (
    META_ID,
    ChannelAccount,
    Credential,
    Message,
    MessageAttachment,
)
from apps.files.models import File
from core import storage
from core.ids import new_id
from core.observability.logging import get_logger
from core.outbox import emit
from core.tenancy.context import TenantContext
from core.tenancy.scope import require_scope, tenant_scope

logger = get_logger(__name__)
KINDS = {"image": "IMAGE", "audio": "AUDIO", "video": "VIDEO", "sticker": "STICKER"}
MAX_ATTEMPTS = 5
BACKOFF = (30, 120, 600, 3600)  # segundos de espera tras el intento 1, 2, 3 y 4
LEASE = dt.timedelta(seconds=300)  # mayor que el presupuesto de un intento (120 s, F7-09a1c)
BUSY_WAIT = 30  # con el tope de la organización lleno, la tarea vuelve en esto
RETRY = "RETRY"

EXTENSIONS = {
    "image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "audio/ogg": "ogg",
    "audio/amr": "amr", "audio/aac": "aac", "audio/mpeg": "mp3", "audio/mp4": "m4a",
    "video/mp4": "mp4", "video/3gpp": "3gp",
}  # fmt: skip


@dataclass(frozen=True, slots=True)
class Claim:
    attachment_id: UUID
    attempt_id: UUID
    kind: str
    media_id: str
    number: str
    credential: Credential


def attach(ctx: TenantContext, message: Message, kind: Any, payload: dict[str, Any]) -> None:
    """El adjunto `PENDING` de un mensaje entrante recién creado, en su transacción, y su evento
    (solo ids; el handler que encola la descarga y la llamada desde `inbound.receive` llegan con
    F7-09a1c). Solo imagen, audio, video y sticker con un id de medio de solo dígitos (va en la
    ruta de la URL de la Graph API): lo demás, documentos incluidos, no tiene adjunto."""
    media = payload.get(kind) if isinstance(kind, str) and kind in KINDS else None
    media_id = media.get("id") if isinstance(media, dict) else None
    if not isinstance(media_id, str) or not re.fullmatch(META_ID, media_id):
        return
    made = MessageAttachment.objects.using(require_scope(ctx)).create(
        message_id=message.pk, kind=KINDS[str(kind)], external_media_id=media_id,
        download_status="PENDING", attempts=0,
    )  # fmt: skip
    emit(ctx, "message_attachment.pending", aggregate_type="message_attachment",
         aggregate_id=made.pk)  # fmt: skip


def _close(alias: str, pk: UUID, status: str, code: str | None = None, **more: Any) -> None:
    MessageAttachment.objects.using(alias).filter(pk=pk).update(
        download_status=status, failure_code=code, updated_at=timezone.now(), **more
    )


def claim(ctx: TenantContext, attachment_id: UUID) -> Claim | int | None:
    """Reclama el adjunto para un intento. `None` si no hay nada que hacer (no existe, es de otra
    organización, está cerrado, espera su turno, o se cerró aquí sin llamar a Meta); los segundos
    tras los que volver a mirar si lo tiene otro intento vivo (un mensaje de Celery repetido tras
    matar un worker: vuelve al vencer el plazo) o si la organización ya está en su tope."""
    alias = require_scope(ctx)
    with connections[alias].cursor() as cursor:  # el tope se cuenta y se toma uno a uno
        key = f"media:{ctx.organization_id}"
        cursor.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", [key])
    now = timezone.now()
    due = Q(download_status="PENDING") & (Q(not_before__isnull=True) | Q(not_before__lte=now))
    due |= Q(download_status="DOWNLOADING", lease_until__lte=now)
    rows = MessageAttachment.objects.using(alias).select_for_update(skip_locked=True)
    row = rows.filter(due, pk=attachment_id).first()
    if row is None:
        held = MessageAttachment.objects.using(alias).filter(
            pk=attachment_id, download_status="DOWNLOADING"
        ).values_list("lease_until", flat=True).first()  # fmt: skip
        return None if held is None else max(int((held - now).total_seconds()), 0) + 1
    if row.attempts >= MAX_ATTEMPTS:  # el último intento murió sin cerrar
        _close(alias, row.pk, "FAILED", "UNAVAILABLE")
        return None
    live = Q(download_status="DOWNLOADING", lease_until__gt=now)
    if MessageAttachment.objects.using(alias).filter(live).count() >= settings.MEDIA_PER_ORG:
        return BUSY_WAIT
    message = Message.objects.using(alias).get(pk=row.message_id)
    account = ChannelAccount.objects.using(alias).filter(
        pk=message.channel_account_id, platform="WHATSAPP", status="CONNECTED",
        deleted_at__isnull=True,
    ).first()  # fmt: skip
    if account is None:
        _close(alias, row.pk, "FAILED", "CHANNEL_UNAVAILABLE")
        return None
    credential = (
        Credential.objects.using(alias)
        .filter(pk=account.credential_id, owner_id=account.pk, revoked_at__isnull=True)
        .first()
    )
    if credential is None:  # sin credencial viva no se llama a Meta
        _close(alias, row.pk, "FAILED", "TOKEN_INVALID")
        return None
    attempt = new_id()
    _close(
        alias, row.pk, "DOWNLOADING", attempts=row.attempts + 1, attempt_id=attempt,
        lease_until=now + LEASE, not_before=None,
    )  # fmt: skip
    return Claim(row.pk, attempt, row.kind, row.external_media_id,
                 account.external_account_id, credential)  # fmt: skip


def _mine(alias: str, got: Claim) -> MessageAttachment | None:
    """La fila bloqueada, solo si el reclamo sigue siendo de este intento."""
    rows = MessageAttachment.objects.using(alias).select_for_update()
    return rows.filter(pk=got.attachment_id, download_status="DOWNLOADING",
                       attempt_id=got.attempt_id).first()  # fmt: skip


def finish(ctx: TenantContext, got: Claim, outcome: str) -> tuple[str, int | None]:
    """Cierra un intento que no guardó nada: `RETRY` vuelve a `PENDING` con espera (o `FAILED
    UNAVAILABLE` agotados los intentos), `EXPIRED`, o `FAILED` con el código. Devuelve el resultado
    y, si hay que volver, en cuántos segundos. Un reclamo perdido no cambia nada (`LOST`)."""
    alias = require_scope(ctx)
    row = _mine(alias, got)
    if row is None:
        return "LOST", None
    if outcome == RETRY and row.attempts >= MAX_ATTEMPTS:
        outcome = "UNAVAILABLE"
    if outcome == RETRY:
        wait = BACKOFF[min(row.attempts, len(BACKOFF)) - 1]
        _close(alias, row.pk, "PENDING", not_before=timezone.now() + dt.timedelta(seconds=wait))
        return RETRY, wait
    if outcome == "EXPIRED":
        _close(alias, row.pk, "EXPIRED")
    else:
        _close(alias, row.pk, "FAILED", outcome)
    return outcome, None


class Sink:
    """Escribe en un temporal y calcula el `sha256` y el tamaño al paso."""

    def __init__(self) -> None:
        self.file: IO[bytes] = tempfile.SpooledTemporaryFile(max_size=1 << 20)  # noqa: SIM115
        self.digest, self.size = hashlib.sha256(), 0

    def write(self, data: Any) -> int:
        written = self.file.write(data)
        self.digest.update(memoryview(data)[:written])
        self.size += written
        return written


def _drop(key: str, got: Claim) -> None:
    try:
        storage.storage().delete(key)
    except Exception as error:  # el blob queda huérfano (conciliación: fuera de alcance)
        logger.error("media.blob_delete_failed", attachment_id=str(got.attachment_id),
                     error=type(error).__name__)  # fmt: skip


def store(ctx: TenantContext, got: Claim, sink: Sink, mime: str) -> tuple[str, int | None]:
    """Guarda lo bajado (tipo ya comprobado): el blob primero, con la clave del intento, y luego
    el archivo y `DONE` en una transacción, solo si el reclamo sigue siendo suyo. Un almacén que
    falla vuelve a `PENDING` con espera, sin fila; un reclamo perdido o una base que falla borran
    solo ese blob (`LOST`, o la excepción tal cual)."""
    if mime not in EXTENSIONS:
        raise ValueError("tipo no permitido")
    key = storage.tenant_key(ctx.organization_id, "message-media", got.attempt_id)
    sink.file.seek(0)
    try:
        storage.storage().put(key, sink.file, content_type=mime, content_length=sink.size)
    except Exception as error:
        logger.warning("media.store_failed", attachment_id=str(got.attachment_id),
                       error=type(error).__name__)  # fmt: skip
        with tenant_scope(ctx):
            return finish(ctx, got, RETRY)
    kept = False
    try:
        with tenant_scope(ctx):
            alias = require_scope(ctx)
            if _mine(alias, got) is not None:
                File.objects.using(alias).create(
                    id=got.attempt_id, storage_key=key, original_name=f"{got.kind.lower()}."
                    f"{EXTENSIONS[mime]}", mime_type=mime, size_bytes=sink.size,
                    sha256=sink.digest.hexdigest(), purpose="message-media",
                    scan_status="PENDING",
                )  # fmt: skip
                _close(alias, got.attachment_id, "DONE", file_id=got.attempt_id)
                kept = True
    finally:
        if not kept:  # reclamo perdido o la base falló: solo el blob de este intento
            _drop(key, got)
    return ("DONE" if kept else "LOST"), None

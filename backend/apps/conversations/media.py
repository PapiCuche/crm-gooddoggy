"""Medios entrantes de WhatsApp (F7-09a1b, F7-09a1c; ADR-029 §7): el adjunto que deja
`inbound.receive` y la descarga que hace `tasks.fetch_media` al almacén privado (`files`,
`message-media`, sin antivirus: `scan_status = PENDING`, no se sirve a nadie).

Un intento: **reclamar** en una transacción (tope por organización, cuenta `CONNECTED` con
credencial viva, `attempt_id` nuevo, plazo de 300 s; la espera y el plazo con el reloj de la
base, que también los hace cumplir, `0022`), preguntar el id a la Graph API y bajar el binario
**fuera** de toda transacción (presupuesto de 120 s; la URL no se guarda), mirar el tipo real por
magic bytes, guardar el blob con la clave del intento y **cerrar** solo si el reclamo sigue siendo
suyo. Lo que declara Meta (tipo, tamaño, nombre, `sha256`) no se guarda. Registros: ids, códigos
y la clase de un error; nunca la URL, el token ni el texto de un error."""

import datetime as dt
import hashlib
import json
import re
import tempfile
import time
from dataclasses import dataclass
from typing import IO, Any, cast
from uuid import UUID

from django.conf import settings
from django.db import connections
from django.db.models import Q
from django.db.models.functions import Now
from django.utils import timezone

from apps.conversations import credentials, graph
from apps.conversations.models import (
    MEDIA_FAILURES,
    META_ID,
    ChannelAccount,
    Credential,
    Message,
    MessageAttachment,
)
from apps.files.models import File
from core import http, sealed, storage
from core.ids import new_id
from core.observability.logging import get_logger
from core.outbox import emit
from core.tenancy.context import TenantContext, current
from core.tenancy.scope import require_scope, tenant_scope

logger = get_logger(__name__)
KINDS = {"image": "IMAGE", "audio": "AUDIO", "video": "VIDEO", "sticker": "STICKER"}
CAPS = {"IMAGE": 5 << 20, "STICKER": 1 << 20, "AUDIO": 16 << 20, "VIDEO": 16 << 20}  # a confirmar
MAX_ATTEMPTS = 5
BACKOFF = (30, 120, 600, 3600)  # segundos de espera tras el intento 1, 2, 3 y 4
LEASE = dt.timedelta(seconds=300)
# Lo que queda antes de cada consulta y de la descarga; una consulta a Graph tiene sus propios
# tiempos (`core.http.request`), así que un intento real puede rozar ~180 s: menos que el plazo.
BUDGET = 120.0
BUSY_WAIT = 30  # con el tope de la organización lleno, la tarea vuelve en esto
RETRY = "RETRY"
OUTCOMES = frozenset({RETRY, "EXPIRED", *MEDIA_FAILURES})
FTYP = {  # la marca principal de una caja `ftyp`, por clase
    b"M4A ": ("AUDIO", "audio/mp4"),
    **dict.fromkeys((b"isom", b"iso2", b"mp41", b"mp42", b"avc1"), ("VIDEO", "video/mp4")),
    **dict.fromkeys((b"3gp4", b"3gp5", b"3gp6"), ("VIDEO", "video/3gpp")),
}
EXTENSIONS = {
    "image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "audio/ogg": "ogg",
    "audio/amr": "amr", "audio/aac": "aac", "audio/mpeg": "mp3", "audio/mp4": "m4a",
    "video/mp4": "mp4", "video/3gpp": "3gp",
}  # fmt: skip


class Stop(Exception):  # noqa: N818 — el resultado cerrado de un intento, no un fallo
    def __init__(self, outcome: str) -> None:
        super().__init__(outcome)
        self.outcome = outcome


@dataclass(frozen=True, slots=True)
class Claim:
    attachment_id: UUID
    attempt_id: UUID
    kind: str
    media_id: str
    number: str
    credential_id: UUID


def attach(ctx: TenantContext, message: Message, kind: Any, payload: dict[str, Any]) -> None:
    """El adjunto `PENDING` de un mensaje **recién creado** (solo lo llama esa rama de
    `inbound.receive`), en su transacción, y su evento. Solo imagen, audio, video y sticker con un
    id de solo dígitos (va en la URL)."""
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
    """El reclamo de un intento; `None` si no hay nada que hacer (o se cerró sin llamar a Meta); o
    en cuántos segundos volver si otro intento tiene el plazo vivo, la espera de un reintento aún
    no venció (una tarea que llega antes, con el reloj del worker) o la organización está en su
    tope. Los segundos se cuentan con el reloj de la base."""
    alias = require_scope(ctx)
    with connections[alias].cursor() as cursor:  # el tope se cuenta y se toma uno a uno
        key = f"media:{ctx.organization_id}"
        cursor.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", [key])
    now = Now()  # `statement_timestamp()`; el disparador (`0022`) mira el de su propia sentencia
    due = Q(download_status="PENDING") & (Q(not_before__isnull=True) | Q(not_before__lte=now))
    due |= Q(download_status="DOWNLOADING", lease_until__lte=now)
    rows = MessageAttachment.objects.using(alias).select_for_update(skip_locked=True)
    row = rows.filter(due, pk=attachment_id).first()
    if row is None:  # ¿un intento vivo, o una espera que aún no venció? Se vuelve a mirar después
        held = (
            MessageAttachment.objects.using(alias)
            .filter(
                Q(download_status="DOWNLOADING") | Q(download_status="PENDING", not_before__gt=now),
                pk=attachment_id,
            )
            .annotate(at=Now())
            .values_list("download_status", "lease_until", "not_before", "at")
        )
        if (found := held.first()) is None:
            return None
        status, lease, wait, at = found
        return max(int(((lease if status == "DOWNLOADING" else wait) - at).total_seconds()), 0) + 1
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
    credential = Credential.objects.using(alias).filter(
        pk=account.credential_id, owner_id=account.pk, revoked_at__isnull=True
    ).first()  # fmt: skip
    if credential is None:  # sin credencial viva no se llama a Meta
        _close(alias, row.pk, "FAILED", "TOKEN_INVALID")
        return None
    attempt = new_id()
    _close(
        alias, row.pk, "DOWNLOADING", attempts=row.attempts + 1, attempt_id=attempt,
        lease_until=now + LEASE, not_before=None,
    )  # fmt: skip
    return Claim(row.pk, attempt, row.kind, row.external_media_id,
                 account.external_account_id, credential.pk)  # fmt: skip


def _mine(alias: str, got: Claim) -> MessageAttachment | None:  # bloqueada, si sigue siendo suya
    rows = MessageAttachment.objects.using(alias).select_for_update()
    return rows.filter(pk=got.attachment_id, download_status="DOWNLOADING",
                       attempt_id=got.attempt_id).first()  # fmt: skip


def finish(ctx: TenantContext, got: Claim, outcome: str) -> tuple[str, int | None]:
    """Cierra un intento sin archivo: `RETRY` (con espera; agotado, `UNAVAILABLE`), `EXPIRED` o un
    código de `FAILED`; otro valor es `ValueError`. Un reclamo perdido no cambia nada (`LOST`)."""
    if outcome not in OUTCOMES:
        raise ValueError("resultado de un intento desconocido")
    alias = require_scope(ctx)
    row = _mine(alias, got)
    if row is None:
        return "LOST", None
    if outcome == RETRY and row.attempts >= MAX_ATTEMPTS:
        outcome = "UNAVAILABLE"
    if outcome == RETRY:
        wait = BACKOFF[min(row.attempts, len(BACKOFF)) - 1]
        _close(alias, row.pk, "PENDING", not_before=Now() + dt.timedelta(seconds=wait))
        return RETRY, wait
    if outcome == "EXPIRED":
        _close(alias, row.pk, "EXPIRED")
    else:
        _close(alias, row.pk, "FAILED", outcome)
    return outcome, None


def _frame(head: bytes, tagged: bool) -> tuple[str, str] | None:
    """Una cabecera de trama de audio MPEG (MP3) o ADTS (AAC) válida."""
    if len(head) < 4 or head[0] != 0xFF or head[1] & 0xE0 != 0xE0:
        return None
    if (head[1] >> 1) & 3 == 0:  # capa 00: ADTS, y nunca tras una etiqueta ID3
        ok = head[1] & 0xF6 == 0xF0 and (head[2] >> 2) & 0xF <= 12 and not tagged
        return ("AUDIO", "audio/aac") if ok else None
    bitrate, rate = head[2] >> 4, (head[2] >> 2) & 3
    if (head[1] >> 3) & 3 == 1 or bitrate in (0, 15) or rate == 3:  # valores reservados
        return None
    return "AUDIO", "audio/mpeg"


def sniff(blob: IO[bytes]) -> tuple[str, str] | None:
    """(clase, tipo MIME) por magic bytes, o `None` si no es un tipo permitido de ninguna clase.
    Un ID3 vale solo si tras la etiqueta empieza una trama MP3 válida (un políglota, no)."""
    blob.seek(0)
    head = blob.read(16)
    if head[:3] == b"\xff\xd8\xff":
        return "IMAGE", "image/jpeg"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "IMAGE", "image/png"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "STICKER", "image/webp"
    if head[:4] == b"OggS":
        return "AUDIO", "audio/ogg"
    if head[:6] == b"#!AMR\n":
        return "AUDIO", "audio/amr"
    if head[4:8] == b"ftyp" and int.from_bytes(head[:4], "big") >= 16:
        return FTYP.get(head[8:12])
    if head[:3] == b"ID3" and len(head) >= 10:
        if any(b & 0x80 for b in head[6:10]):
            return None
        size = sum(b << (7 * (3 - i)) for i, b in enumerate(head[6:10]))
        blob.seek(10 + size + (10 if head[5] & 0x10 else 0))
        return _frame(blob.read(4), True)
    return _frame(head[:4], False)


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


def _lookup(auth: dict[str, str], got: Claim) -> str:
    """La URL fresca del medio según la Graph API (en cada intento; nunca se guarda)."""
    url = (
        f"https://{settings.META_GRAPH_HOST}/{settings.META_GRAPH_VERSION}/{got.media_id}"
        f"?phone_number_id={got.number}"
    )
    try:
        reply = http.request("GET", url, headers=auth)
        status, raw = reply.status, reply.data
    except http.BlockedDestination, http.HTTPError, OSError:
        raise Stop(RETRY) from None
    try:
        found = json.loads(raw)
    except ValueError, RecursionError:
        found = None
    error = found.get("error") if isinstance(found, dict) else None
    code = error.get("code") if isinstance(error, dict) else None
    if status == 429 or status >= 500 or code in graph.THROTTLED:  # nada dice del token
        raise Stop(RETRY)
    if code == 190 or status in (401, 403):
        raise Stop("TOKEN_INVALID")
    if status in (404, 410):
        raise Stop("EXPIRED")
    link = found.get("url") if status == 200 and isinstance(found, dict) else None
    if not isinstance(link, str):
        raise Stop("UNAVAILABLE")
    return link


def _download(token: str, got: Claim, deadline: float) -> Sink:
    auth = {"Authorization": f"Bearer {token}"}  # solo a Graph y a `META_MEDIA_HOSTS`
    for again in (True, False):  # una URL que ya no vale: otra fresca, una vez
        if deadline - time.monotonic() <= 1:
            raise Stop(RETRY)
        url = _lookup(auth, got)
        try:  # un host fuera de la lista es definitivo; un DNS que falla dentro de `download`, no
            http.check_url(url, settings.META_MEDIA_HOSTS)
        except http.BlockedDestination:
            raise Stop("UNAVAILABLE") from None
        sink, status = Sink(), 0
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 1:
                raise Stop(RETRY)
            status = http.download(
                url, cast(IO[bytes], sink), max_bytes=CAPS[got.kind],
                hosts=settings.META_MEDIA_HOSTS, headers=auth, seconds=remaining,
            )  # fmt: skip
        except http.ResponseTooLarge:
            raise Stop("TOO_LARGE") from None
        except http.UnsupportedEncoding:
            raise Stop("UNAVAILABLE") from None
        except http.BlockedDestination, http.HTTPError, OSError:  # p. ej., `getaddrinfo` falló
            raise Stop(RETRY) from None
        finally:
            if status != 200:
                sink.file.close()
        if status == 200:
            return sink
        if status == 401:
            raise Stop("TOKEN_INVALID")
        if status == 429 or status >= 500:
            raise Stop(RETRY)
        if status not in (403, 404, 410):
            raise Stop("UNAVAILABLE")
        if not again:
            raise Stop("EXPIRED")
    raise AssertionError("no llega")  # pragma: no cover


def _drop(key: str, got: Claim) -> None:
    try:
        storage.storage().delete(key)
    except Exception as error:  # el blob queda huérfano (conciliación: fuera de alcance)
        logger.error("media.blob_delete_failed", attachment_id=str(got.attachment_id),
                     error=type(error).__name__)  # fmt: skip


def store(ctx: TenantContext, got: Claim, sink: Sink, mime: str) -> tuple[str, int | None]:
    """El blob primero, y el archivo y `DONE` solo si el reclamo sigue siendo suyo; si no, o si la
    base falla, se borra solo ese blob. Un almacén que falla: se borra su clave (puede quedar a
    medias) y reintento, sin fila. Nunca dentro de una transacción ni de un `tenant_scope`: el blob
    no se deshace con un rollback del llamador (`RuntimeError`)."""
    if current() is not None or connections["default"].in_atomic_block:
        raise RuntimeError("media.store abre su propia transacción")
    if mime not in EXTENSIONS:
        raise ValueError("tipo no permitido")
    key = storage.tenant_key(ctx.organization_id, "message-media", got.attempt_id)
    sink.file.seek(0)
    try:
        storage.storage().put(key, sink.file, content_type=mime, content_length=sink.size)
    except Exception as error:
        logger.warning("media.store_failed", attachment_id=str(got.attachment_id),
                       error=type(error).__name__)  # fmt: skip
        _drop(key, got)
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
                # Antes del COMMIT: si el COMMIT falla, el blob se queda (borrarlo podría dejar
                # una fila `DONE` ya confirmada sin su blob); huérfano, a la conciliación (ADR-008).
                kept = True
    finally:
        if not kept:  # reclamo perdido o la base falló: solo el blob de este intento
            _drop(key, got)
    return ("DONE" if kept else "LOST"), None


def _token(ctx: TenantContext, got: Claim) -> str:
    """El token, solo para este intento: la credencial se vuelve a leer (revocada tras el reclamo:
    `TOKEN_INVALID`, sin llamar a Meta)."""
    with tenant_scope(ctx):
        live = Credential.objects.using(require_scope(ctx)).filter(
            pk=got.credential_id, revoked_at__isnull=True
        ).first()  # fmt: skip
    if live is None:
        raise Stop("TOKEN_INVALID")
    try:
        return credentials.open_token(live)
    except sealed.SealError:  # sin la clave de esa versión: es del despliegue, se reintenta
        raise Stop(RETRY if not sealed.has_key(live.key_version) else "TOKEN_INVALID") from None


def fetch(organization_id: UUID, attachment_id: UUID) -> tuple[str, int | None]:
    """Un intento de descarga. Devuelve el resultado (un código cerrado) y, si la tarea tiene que
    volver, en cuántos segundos. Uno de otra organización o que no existe: `SKIPPED`, sin más."""
    ctx = TenantContext(organization_id, "celery")
    with tenant_scope(ctx):
        got = claim(ctx, attachment_id)
    if got is None:
        return "SKIPPED", None
    if isinstance(got, int):
        return "WAIT", got
    sink = None
    try:
        token = _token(ctx, got)
        try:
            sink = _download(token, got, time.monotonic() + BUDGET)
        finally:
            del token
        found = sniff(sink.file)
        if found is None or found[0] != got.kind:
            raise Stop("TYPE_NOT_ALLOWED" if found is None else "TYPE_MISMATCH")
        return store(ctx, got, sink, found[1])
    except Stop as stop:
        with tenant_scope(ctx):
            return finish(ctx, got, stop.outcome)
    finally:
        if sink is not None:
            sink.file.close()

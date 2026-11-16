"""El antivirus de los medios entrantes (F7-15; ADR-008, ADR-029 §7, D-F7-04).

Tras la descarga (`media.store` emite `file.stored`), `tasks.scan_media` lee el blob del almacén en
flujo y lo pasa a `clamd` (`core.antivirus`, INSTREAM con tope y plazo). Un veredicto real escribe
`CLEAN` o `INFECTED`; sin veredicto (clamd caído, plazo, almacén que falla) se reintenta con espera
y, agotados los intentos, queda `ERROR`. Solo un `PENDING` cambia: nada pisa un veredicto.
`INFECTED` y `ERROR` dejan auditoría y una alerta (`media.alert.infected`, `media.alert.scan_error`)
con ids y la firma, nunca bytes, nombre ni clave. El barredor (`stale`) vuelve a encolar un
`PENDING` sin tarea viva y (F7-15c) un `ERROR` reciente, que vuelve a `PENDING`: si clamd volvió,
queda con veredicto sin pedir que reenvíen el archivo. Como mucho una ronda cada `RESCAN_AFTER`
mientras el archivo es más reciente que `RESCAN_WINDOW`: no más de tres rondas, con sus alertas."""

import datetime as dt
from uuid import UUID

from django.db.models import Q
from django.db.models.functions import Now

from apps.audit.services import Entity, record
from apps.conversations import media
from apps.files.models import File
from core import antivirus, storage
from core.observability import reporting
from core.observability.logging import get_logger
from core.tenancy.context import ActorType, TenantContext
from core.tenancy.scope import require_scope, tenant_scope

logger = get_logger(__name__)
PURPOSE = "message-media"
MAX_ATTEMPTS = 4
BACKOFF = (30, 120, 600)  # segundos de espera tras el intento 1, 2 y 3 (menos que `MAX_COUNTDOWN`)
SECONDS = 60.0  # plazo de un escaneo (envío y respuesta)
CAP = max(media.CAPS.values())  # ningún medio entrante pasa de aquí (`media.CAPS`)
STALE = dt.timedelta(minutes=15)  # un `PENDING` sin intento desde hace esto: sin tarea viva
RESCAN_AFTER = dt.timedelta(hours=2)  # un `ERROR` sin intento desde hace esto vuelve a escanearse
RESCAN_WINDOW = dt.timedelta(hours=6)  # ...solo si el archivo es más reciente que esto
ALERT = "media.alert.infected"
ALERTS = {"INFECTED": ALERT, "ERROR": "media.alert.scan_error"}


def _pending(alias: str, file_id: UUID) -> str | None:
    """La clave del blob de un `PENDING` de medios entrantes, marcando el intento; si no, `None`."""
    rows = File.objects.using(alias).filter(pk=file_id, purpose=PURPOSE, scan_status="PENDING")
    key = rows.values_list("storage_key", flat=True).first()
    if key is not None:
        rows.update(scan_checked_at=Now())
    return key


def _verdict(key: str) -> antivirus.Verdict:
    """El veredicto de `clamd`; `antivirus.Unavailable` si no lo hay. Cualquier otro fallo (el
    almacén al abrir o a mitad de la lectura) también: se reintenta y acaba en `ERROR`,
    nunca en una tarea que cae una y otra vez con el barredor."""
    try:
        blob = storage.storage().open(key)
        try:
            return antivirus.scan(blob, max_bytes=CAP, seconds=SECONDS)
        finally:
            try:  # cerrar no deshace un veredicto real
                blob.close()
            except Exception as error:  # noqa: BLE001
                logger.warning("media.scan_close_failed", error=type(error).__name__)
    except antivirus.Unavailable:
        raise
    except Exception as error:
        raise antivirus.Unavailable(type(error).__name__) from None


def _write(ctx: TenantContext, file_id: UUID, status: str, signature: str | None) -> bool:
    alias = require_scope(ctx)
    rows = File.objects.using(alias).filter(pk=file_id, scan_status="PENDING")
    if not rows.update(scan_status=status, scan_checked_at=Now()):
        return False  # otra tarea ya le dio veredicto
    if status != "CLEAN":
        record(
            ctx, f"file.scan_{status.lower()}", Entity("file", file_id),
            {"scan_status": ["PENDING", status]}, {"signature": signature},
            actor=(ActorType.SYSTEM, None),
        )  # fmt: skip
    return True


def alert(ctx: TenantContext, name: str, signature: str | None, **ids: UUID) -> None:
    """La alerta de un veredicto (también la de un envío infectado, F7-15b): ids y la firma."""
    shown = {key: str(value) for key, value in ids.items()}
    context = {"organization_id": str(ctx.organization_id), **shown, "signature": signature}
    logger.error(name, **context)
    try:
        reporting.reporter().capture_message(name, context, fingerprint=[name])
    except Exception:  # noqa: BLE001 — un reporte roto no deshace el veredicto
        logger.warning("media.alert.report_failed", **shown)


def scan(organization_id: UUID, file_id: UUID, attempt: int) -> tuple[str, int | None]:
    """Un intento. Devuelve el resultado (`CLEAN`, `INFECTED`, `ERROR`, `RETRY` o `SKIPPED`) y, con
    `RETRY`, en cuántos segundos volver. Uno ajeno, inexistente o ya con veredicto: `SKIPPED`."""
    ctx = TenantContext(organization_id, "celery")
    with tenant_scope(ctx):
        key = _pending(require_scope(ctx), file_id)
    if key is None:
        return "SKIPPED", None
    signature = None
    try:  # fuera de toda transacción: el escaneo puede tardar `SECONDS`
        found = _verdict(key)
        status, signature = found.status, found.signature
    except antivirus.Unavailable as error:
        logger.warning("media.scan_unavailable", file_id=str(file_id), attempt=attempt,
                       error=str(error))  # fmt: skip
        if attempt < MAX_ATTEMPTS:
            return "RETRY", BACKOFF[min(attempt, len(BACKOFF)) - 1]
        status = "ERROR"
    with tenant_scope(ctx):
        written = _write(ctx, file_id, status, signature)
    if written and status in ALERTS:  # una caída de clamd que deja todo en `ERROR` también se ve
        alert(ctx, ALERTS[status], signature, file_id=file_id)
    return (status if written else "SKIPPED"), None


def stale(ctx: TenantContext, limit: int) -> list[UUID]:
    """Hasta `limit` archivos de medios entrantes que escanear otra vez: un `PENDING` sin intento
    (ni barrido) desde hace `STALE`, y creado antes (su tarea se perdió o nunca hubo: los de antes
    de F7-15), o un `ERROR` sin intento desde hace `RESCAN_AFTER` y creado hace menos de
    `RESCAN_WINDOW` (F7-15c), que vuelve a `PENDING` con su auditoría. Los marca en la misma
    transacción, para no volver a barrerlos antes de tiempo."""
    alias = require_scope(ctx)
    old = Now() - STALE
    idle = Q(scan_checked_at__isnull=True) | Q(scan_checked_at__lte=old)
    lost = Q(idle, scan_status="PENDING", created_at__lte=old)
    failed = Q(
        scan_status="ERROR", created_at__gte=Now() - RESCAN_WINDOW,
        scan_checked_at__lte=Now() - RESCAN_AFTER,
    )  # fmt: skip
    rows = File.objects.using(alias).select_for_update(skip_locked=True)
    found = list(
        rows.filter(lost | failed, purpose=PURPOSE)
        .order_by("created_at", "pk").values_list("pk", "scan_status")[:limit]
    )  # fmt: skip
    marked = [pk for pk, _ in found]
    File.objects.using(alias).filter(pk__in=marked).update(
        scan_status="PENDING", scan_checked_at=Now()
    )
    retried = {"scan_status": ["ERROR", "PENDING"]}
    for pk, was in found:
        if was == "ERROR":
            record(ctx, "file.scan_retried", Entity("file", pk), retried,
                   actor=(ActorType.SYSTEM, None))  # fmt: skip
    return marked

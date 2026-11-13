"""La ingesta de los webhooks de Meta (F7-03b y F7-03c; ADR-029 §3 y §4): guardar cada ítem
firmado, reclamarlo, enrutarlo al tenant y cerrarlo.

`webhook_ingress` y `webhook_failures` son de plataforma: `crm_app` no tiene ningún privilegio
sobre ellas y solo entra por las funciones `SECURITY DEFINER` de las migraciones (`0008`: insertar;
`0009`: el resto; `0033`: registrar un rechazo). Aquí está todo lo que las llama. El enrutado
**nunca** usa la organización que diga el payload: sale de `resolve_channel_account` (la cuenta
que la propia organización registró), y el procesador solo enruta y marca: un ítem enrutado queda
`PROCESSED` sin efecto hasta F7-04.
"""

import json
import math
import re
import time
from collections import Counter
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from django.db import DataError, connection, transaction
from psycopg.types.json import Jsonb

from apps.audit.services import Entity, record
from apps.conversations import delivery, inbound
from apps.conversations.models import ChannelAccount
from core.observability.logging import get_logger
from core.tenancy.context import ActorType, TenantContext
from core.tenancy.scope import tenant_scope

logger = get_logger(__name__)
PROVIDER = "meta"
LEASE_SECONDS = 60  # el reclamo caduca: un proceso que muere no deja el ítem para siempre
BROKER_WAIT = 3  # segundos que la ruta pública espera a un broker que no contesta
_NO_WAIT = {"max_retries": 0, "socket_connect_timeout": BROKER_WAIT, "socket_timeout": BROKER_WAIT}
SQLSTATE = re.compile(r"[0-9A-Z]{5}")
UNKNOWN_AFTER = 3  # intentos tras los que un estado sin mensaje se cierra como `IGNORED`
WAKE_EVERY = 1.0  # segundos entre dos avisos de un mismo proceso: el resto lo recoge el barrido
_last_wake = 0.0
_now = time.monotonic  # separado para que una prueba mueva el reloj


@dataclass(frozen=True, slots=True)
class Claimed:
    id: UUID
    kind: str
    waba_id: str
    phone_number_id: str | None
    payload: dict[str, Any]
    attempts: int


def clean(value: Any) -> Any:
    """El payload sin lo que `jsonb` no guarda y un cliente podría escribir: `\\u0000` (se sustituye
    por U+FFFD) y los sustitutos sueltos (por `?`). La profundidad ya está acotada (`MAX_DEPTH`)."""
    if isinstance(value, float) and not math.isfinite(value):
        return None  # `1e999` llega como infinito, y `jsonb` no guarda `Infinity`: un 503 eterno
    if isinstance(value, str):
        return value.replace("\x00", "\ufffd").encode("utf-8", "replace").decode("utf-8")
    if isinstance(value, list):
        return [clean(item) for item in value]
    if isinstance(value, dict):
        return {clean(key): clean(item) for key, item in value.items()}
    return value


def _call(sql: str, params: list[Any]) -> list[tuple[Any, ...]]:
    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        rows: list[tuple[Any, ...]] = cursor.fetchall()
        return rows


def insert(kind: str, key: str, waba_id: str, number: str | None, payload: dict[str, Any]) -> bool:
    """Guarda un ítem validado, idempotente por `(proveedor, clave)`. Devuelve si era nuevo."""
    sql = "SELECT public.webhook_ingress_insert(%s, %s, %s, %s, %s, %s)"
    found = _call(sql, [PROVIDER, key, kind, waba_id, number, Jsonb(clean(payload))])
    return found[0][0] is not None


def refuse(item: Any, sqlstate: str | None) -> bool:
    """Registra un ítem que la base nunca guardará: `DEAD`, sin payload y con el motivo
    `REFUSED_<sqlstate>` en `webhook_failures`, para que se vea y alerte. El hash lo calcula la
    base igual que al guardar (del `jsonb`), así que se compara con el de cualquier otra fila.
    Idempotente por clave, y nunca toca un ítem que ya estaba guardado. Devuelve si era nuevo."""
    reason = f"REFUSED_{sqlstate}" if SQLSTATE.fullmatch(sqlstate or "") else "REFUSED"
    sql = "SELECT public.webhook_ingress_refuse(%s, %s, %s, %s, %s, %s, %s)"
    payload = Jsonb(clean(item.payload))
    args = [PROVIDER, item.key, item.kind, item.waba_id, item.phone_number_id, payload, reason]
    return _call(sql, args)[0][0] is not None


def _insert_all(items: list[Any]) -> int:
    """Todos los ítems en una sola sentencia, en el orden dado; devuelve cuántos eran nuevos."""
    rows = [
        {"k": i.key, "t": i.kind, "w": i.waba_id, "n": i.phone_number_id, "p": clean(i.payload)}
        for i in items
    ]
    sql = (
        "SELECT count(public.webhook_ingress_insert(%s, e->>'k', e->>'t', e->>'w', e->>'n',"
        " e->'p')) FROM jsonb_array_elements(%s) AS e"
    )
    return int(_call(sql, [PROVIDER, Jsonb(rows)])[0][0])


def _store(items: list[Any], refused: list[Any]) -> int:
    """Intenta el lote entero con un solo punto de guardado; si la base rechaza un ítem, deshace
    ese intento y prueba cada mitad, hasta aislarlo y registrarlo con `refuse`. Unos 2·log2(n)
    puntos de guardado por ítem rechazado, no uno por ítem: más de 64 desbordan la caché de
    subtransacciones del proceso y frenan las instantáneas de todas las sesiones."""
    try:
        with transaction.atomic():
            return _insert_all(items)
    except DataError as error:  # nunca lo guardará: un 503 lo repetiría 7 días en vano
        if len(items) > 1:
            half = len(items) // 2
            return _store(items[:half], refused) + _store(items[half:], refused)
        refuse(items[0], getattr(error.__cause__, "sqlstate", None))
        refused.append(items[0])
        return 0


def store(items: list[Any]) -> int:
    """Guarda todos los ítems de una entrega en una transacción, en orden de clave (dos entregas
    que se solapan bloquean en el mismo orden). Un ítem que la base rechaza (`DataError`) no
    envenena el lote: queda registrado con `refuse` y el resto se guarda. Cualquier otro error
    deshace la entrega entera y sube (la ruta responde 503 y Meta reintenta; repetir no duplica).
    Devuelve cuántos eran nuevos."""
    refused: list[Any] = []
    with transaction.atomic():
        new = _store(sorted(items, key=lambda item: item.key), refused) if items else 0
    if refused:
        logger.error("meta.webhook.refused", count=len(refused))  # solo cuántos, nunca el contenido
    return new


def claim(limit: int = 50, lease: int = LEASE_SECONDS) -> list[Claimed]:
    rows = _call("SELECT * FROM public.webhook_ingress_claim(%s, %s)", [limit, lease])
    # El jsonb llega como texto por el cursor de Django.
    return [Claimed(r[0], r[1], r[2], r[3], json.loads(r[4]), r[5]) for r in rows]


def mark(
    item: Claimed,
    status: str,
    organization_id: UUID | None = None,
    channel_account_id: UUID | None = None,
    reason: str | None = None,
) -> str | None:
    """Cierra un ítem reclamado; devuelve su estado final (`DEAD` si agotó los intentos) o `None`
    si ya no estaba reclamado por este intento (caducó y lo tiene otro, o ya se cerró)."""
    sql = "SELECT public.webhook_ingress_mark(%s, %s, %s, %s, %s, %s)"
    found = _call(
        sql, [item.id, item.attempts, status, organization_id, channel_account_id, reason]
    )
    return found[0][0]  # type: ignore[no-any-return]


def listing(status: str | None = None, limit: int = 50) -> list[tuple[Any, ...]]:
    """Solo metadatos, sin el payload (para el panel de F7-11)."""
    return _call("SELECT * FROM public.webhook_ingress_list(%s, %s)", [status, limit])


def purge(days: int = 90, limit: int = 1000) -> int:
    """Borra lo que lleva más de `days` días (90 es el tope, no el plazo normal)."""
    return int(_call("SELECT public.webhook_ingress_purge(%s, %s)", [days, limit])[0][0])


def resolve(platform: str, number: str) -> tuple[UUID, UUID] | None:
    """(organización, cuenta) de la cuenta viva de ese número, o `None`: sin enumerar nada más."""
    found = _call("SELECT * FROM public.resolve_channel_account(%s, %s)", [platform, number])
    return (found[0][0], found[0][1]) if found else None


def wake() -> None:
    """Avisa al procesador de que hay ítems nuevos, sin retener la ruta pública: una conexión propia
    al broker que no reintenta ni espera más de `BROKER_WAIT`. Si falla, el barrido de beat lo
    recoge igual; nunca estropea la respuesta a Meta. Como mucho uno por segundo y proceso: cada
    aviso abre una conexión (con el broker caído, espera hasta `BROKER_WAIT`)."""
    global _last_wake
    now = _now()
    if now - _last_wake < WAKE_EVERY:
        return
    _last_wake = now
    from apps.conversations.tasks import process_webhook_ingress

    try:
        broker = process_webhook_ingress.app.connection_for_write(
            connect_timeout=BROKER_WAIT, transport_options=_NO_WAIT
        )
        with broker:
            process_webhook_ingress.apply_async(expires=60, retry=False, connection=broker)
    except Exception:
        logger.warning("meta.webhook.wake_failed")


def route(item: Claimed) -> tuple[str, UUID | None, UUID | None, str | None]:
    """A dónde va un ítem: (estado, organización, cuenta, motivo). La organización es la de la
    cuenta registrada, nunca una del payload; el WABA del evento tiene que ser el de la cuenta."""
    if item.phone_number_id is None:
        return "IGNORED", None, None, "NO_NUMBER"  # cuenta y plantillas: F7-10 y F7-11
    found = resolve("WHATSAPP", item.phone_number_id)
    if found is None:
        return "IGNORED", None, None, "UNKNOWN_NUMBER"
    organization_id, account_id = found
    context = TenantContext(organization_id, "webhook", actor_type=ActorType.INTEGRATION)
    with tenant_scope(context):
        account = ChannelAccount.objects.filter(pk=account_id).first()
        if account is None or account.external_parent_id != item.waba_id:
            logger.warning("meta.webhook.waba_mismatch", channel_account_id=str(account_id))
            return "IGNORED", None, None, "WABA_MISMATCH"
        seen(context, account)
        if item.kind == "message" and (reason := inbound.receive(context, account, item.payload)):
            return "IGNORED", organization_id, account_id, reason
        if item.kind == "status":
            try:
                if reason := delivery.apply(context, account, item.payload):
                    return "IGNORED", organization_id, account_id, reason
            except delivery.NotYet:  # `seen` vale; el reintento cubre solo la carrera con el envío
                if item.attempts >= UNKNOWN_AFTER:  # un mensaje que no salió por el CRM: se cierra
                    logger.info("meta.webhook.status_unknown_message")  # y su payload se vacía
                    return "IGNORED", organization_id, account_id, "UNKNOWN_MESSAGE"
                return "FAILED", None, None, "MESSAGE_NOT_FOUND"
    return "PROCESSED", organization_id, account_id, None


def seen(ctx: TenantContext, account: ChannelAccount) -> None:
    """El primer evento firmado enrutado a una cuenta `PENDING` es la señal de «webhook visto»
    (ADR-029 §2): limpia `WEBHOOK_NOT_SEEN` y, solo si ya no queda otro detalle y la cuenta tiene
    credencial y comprobación de salud (`connected_ck`), pasa a `CONNECTED`. Si no, sigue `PENDING`
    (la comprobación de salud es de F7-06). `DISCONNECTED` y `ERROR` no cambian."""
    if account.status != "PENDING":
        return
    # Varios primeros eventos a la vez: se relee bajo el bloqueo de la fila y solo el primero cambia
    # la cuenta y la audita (los demás ya la ven distinta).
    account = ChannelAccount.objects.select_for_update(no_key=True).get(pk=account.pk)
    if account.status != "PENDING":
        return
    before = (account.status, account.status_detail)
    if account.status_detail == "WEBHOOK_NOT_SEEN":
        account.status_detail = None
    ready = account.credential_id is not None and account.last_healthcheck_at is not None
    if account.status_detail is None and ready:
        account.status = "CONNECTED"
    if before == (account.status, account.status_detail):
        return
    account.version += 1
    account.save(update_fields=["status", "status_detail", "version", "updated_at"])
    change = {"status": [before[0], account.status], "status_detail": [before[1], None]}
    record(ctx, "channel_account.seen", Entity("channel_account", account.pk), change)


def handle(item: Claimed) -> str:
    try:
        status, organization_id, account_id, reason = route(item)
        return mark(item, status, organization_id, account_id, reason) or "LOST"
    except inbound.Throttled:  # el tope de contactos nuevos: no se pierde, se reintenta con espera
        logger.warning("meta.webhook.contact_cap", phone_number_id=item.phone_number_id)
        return mark(item, "FAILED", reason="RATE_LIMITED") or "LOST"
    except Exception as error:  # el motivo es un código: nada del error ni del payload
        logger.error("meta.webhook.process_failed", error=type(error).__name__)
        try:
            return mark(item, "FAILED", reason="ERROR") or "LOST"
        except Exception:
            logger.error("meta.webhook.mark_failed")  # el reclamo caduca y se reintenta
            return "LOST"


def process(batch_size: int = 50, rounds: int = 10) -> dict[str, int]:
    """Reclama lotes y los enruta hasta vaciar lo pendiente (o `rounds` lotes). Devuelve cuántos
    quedaron en cada estado. Dos procesos a la vez no reclaman el mismo ítem."""
    done: Counter[str] = Counter()
    for _ in range(rounds):
        batch = claim(batch_size)
        done.update(handle(item) for item in batch)
        if len(batch) < batch_size:
            break
    return dict(done)

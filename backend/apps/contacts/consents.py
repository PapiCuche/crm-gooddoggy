"""Comandos del consentimiento de un contacto (F7-08; ADR-010, 02 §E.5): conceder y revocar.

Sin permisos (los decide la API, o el canal que actúa como sistema). Bloquean el contacto vivo
(`FOR NO KEY UPDATE`, como las notas y el `PATCH`) antes de leer o escribir su fila, así que dos
cambios a la vez del mismo contacto se serializan. Cada cambio real actualiza el estado vigente
(`ContactConsent`), añade su evento al historial inmutable (`ContactConsentEvent`) y se audita;
repetir lo que ya vale no escribe nada. La evidencia lleva solo ids y etiquetas cortas, nunca el
texto de un mensaje ni datos del cliente, y la auditoría no la copia. Cada cambio deja también su
evento en el outbox (`consent.granted` o `consent.revoked`, F7-08b) con ids, canal y propósito.

Las palabras del cliente (F7b-05d) llevan el instante del proveedor (`said_at`): la fila guarda el
de la última (`keyword_at`, aunque no cambiara nada) y una más vieja, entregada tarde, no la toca.
Decide la hora de Meta, nunca el orden de proceso (F7b-05e): dos lotes a la vez se serializan con
el bloqueo del contacto, y el segundo compara con la hora que dejó el primero. En un empate gana la
baja, en cualquier orden.

F7b-05f: se ordena por la hora cruda de Meta en segundos enteros (dos palabras del mismo segundo
empatan y gana la baja), aunque el reloj del servidor vaya detrás. Un alta con una hora más allá de
«ahora» más `KEYWORD_AHEAD` no cuenta: ni concede ni guarda su hora, así nunca frena una baja
posterior. Una baja guarda su hora aunque pase ese margen: una hora falsa en el futuro frena las
altas por palabra hasta entonces (del lado de la baja; un cambio manual sigue sin tocar la hora).
Una baja sin hora del proveedor (o con una que no vale) guarda nuestra hora más `KEYWORD_AHEAD`, o
la última guardada si es mayor: ningún alta dentro del margen la levanta (en un empate, la baja);
el cliente vuelve a escribir ALTA pasado ese minuto. Un alta sin hora del proveedor no cuenta
(decisión del propietario en #937): el orden de llegada ya no decide nada.

F7b-05g: una hora de Meta más vieja que «ahora» menos `KEYWORD_STALE` (más que los reintentos de
Meta) no se cree y cuenta como sin hora: la baja revoca igual, el alta no cuenta. Una concesión
manual (`grant_consent`) que cambia el estado borra `keyword_at`: la baja siguiente, con la hora que
sea o sin ella, siempre revoca. Una revocación manual no la toca (ninguna palabra la levanta).
"""

import datetime as dt
from uuid import UUID

from django.db import transaction
from django.utils import timezone

from apps.audit.services import Entity, record
from apps.contacts.models import (
    Channel,
    ConsentSource,
    ConsentStatus,
    Contact,
    ContactConsent,
    ContactConsentEvent,
    Purpose,
)
from core.outbox import emit
from core.tenancy.context import TenantContext
from core.tenancy.scope import require_scope

EVIDENCE_KEYS = frozenset({"message_id", "ingress_id", "form", "note"})
EVIDENCE_VALUE_MAX = 200
KEYWORD_AHEAD = dt.timedelta(seconds=60)  # cuánto puede ir el reloj de Meta por delante del nuestro
KEYWORD_STALE = dt.timedelta(hours=48)  # los reintentos de Meta (hasta 36 h) más un margen


def _evidence(value: dict[str, str] | None) -> dict[str, str]:
    """Solo claves conocidas, con texto corto: la evidencia no es un cajón de datos."""
    found = value or {}
    if not isinstance(found, dict) or not set(found) <= EVIDENCE_KEYS:
        raise ValueError("evidencia no admitida")
    if any(not isinstance(v, str) or len(v) > EVIDENCE_VALUE_MAX for v in found.values()):
        raise ValueError("evidencia no admitida")
    return dict(found)


class _AltaIgnored(Exception):
    """Un alta sin hora del proveedor, o con una más allá del margen: no cuenta (F7b-05f)."""


def _said(said_at: dt.datetime | None, status: str) -> dt.datetime | None:
    """La hora que ordena la palabra: la de Meta en segundos enteros (F7b-05f); `None` si es más
    vieja que «ahora» - `KEYWORD_STALE` (F7b-05g). `_AltaIgnored` si es un alta más allá de «ahora»
    + `KEYWORD_AHEAD`; una baja vale con cualquier hora futura."""
    if said_at is None:
        return None
    said, now = said_at.replace(microsecond=0), timezone.now().replace(microsecond=0)
    if said < now - KEYWORD_STALE:
        return None  # imposible por lo vieja: como sin hora
    ahead = said > now + KEYWORD_AHEAD
    if ahead and status == ConsentStatus.GRANTED:
        raise _AltaIgnored
    return said


def _write(
    ctx: TenantContext, contact_id: UUID, channel: str, purpose: str, status: str, source: str,
    evidence: dict[str, str] | None, *, lift: bool = False,
    said_at: dt.datetime | None = None,
) -> tuple[ContactConsent, bool]:  # fmt: skip
    alias = require_scope(ctx)
    if channel not in Channel.values or purpose not in Purpose.values:
        raise ValueError("canal o propósito no admitidos")
    if source not in ConsentSource.values or (
        source == ConsentSource.KEYWORD_STOP and status != ConsentStatus.REVOKED
    ):
        raise ValueError("origen no admitido")
    proof = _evidence(evidence)
    with transaction.atomic(using=alias):
        live = Contact.objects.using(alias).filter(deleted_at__isnull=True)
        live.select_for_update(no_key=True).get(pk=contact_id)
        said_at = _said(said_at, status)  # con el reloj leído ya dentro del bloqueo
        if lift and said_at is None:  # decisión del propietario (#937): un alta sin hora no cuenta
            raise _AltaIgnored
        where = {"contact_id": contact_id, "channel": channel, "purpose": purpose}
        row = ContactConsent.objects.using(alias).filter(**where).first()
        last = None if row is None else row.keyword_at
        if said_at is None and source == ConsentSource.KEYWORD_STOP:  # una baja sin hora (F7b-05f)
            said_at = timezone.now().replace(microsecond=0) + KEYWORD_AHEAD  # todo el margen
            said_at = said_at if last is None else max(said_at, last)  # en un empate, la baja
        if row is not None and said_at is not None and last is not None and (
            said_at < last or (said_at == last and status == ConsentStatus.GRANTED)
        ):  # fmt: skip
            return row, False  # una palabra más vieja no revierte la última; en un empate, baja
        firmer = (  # F7b-05f: otro origen sobre una baja por palabra se registra (no se levanta)
            row is not None and row.status == status and row.source == ConsentSource.KEYWORD_STOP
            and source != ConsentSource.KEYWORD_STOP
        )  # fmt: skip
        if row is not None and (
            (row.status == status and not firmer)
            or (lift and row.source != ConsentSource.KEYWORD_STOP)
        ):  # fmt: skip
            if said_at is not None and said_at != last:  # la última palabra, aunque no cambie
                row.keyword_at = said_at
                row.save(using=alias, update_fields=["keyword_at", "updated_at"])
            return row, False  # un alta no levanta una baja que no fue una palabra
        now, before = timezone.now(), row.status if row else None
        manual = status == ConsentStatus.GRANTED and not lift  # F7b-05g: borra la hora
        stamp = {"granted_at" if status == ConsentStatus.GRANTED else "revoked_at": now}
        values = {
            "status": status, "source": source, "evidence": proof,
            "recorded_by_user_id": ctx.user_id, **stamp,
            **({"keyword_at": said_at} if said_at is not None or manual else {}),
        }  # fmt: skip
        if row is None:
            row = ContactConsent.objects.using(alias).create(**where, **values)
        else:
            for field, value in values.items():
                setattr(row, field, value)
            row.save(using=alias, update_fields=[*values, "updated_at"])
        ContactConsentEvent.objects.using(alias).create(
            **where, status=status, source=source, evidence=proof,
            recorded_by_user_id=ctx.user_id, occurred_at=now,
        )  # fmt: skip
        action = "consent.granted" if status == ConsentStatus.GRANTED else "consent.revoked"
        about = {**{k: str(v) for k, v in where.items()}, "source": source}
        change = {"status": [before, status]}
        if firmer:  # la misma baja, con un origen que un alta no levanta
            change["source"] = [ConsentSource.KEYWORD_STOP, source]
        record(ctx, action, Entity("contact_consent", row.pk), change, about)
        emit(ctx, action, aggregate_type="contact_consent", aggregate_id=row.pk, payload=about)
        return row, True


def grant_consent(
    ctx: TenantContext, *, contact_id: UUID, channel: str, purpose: str, source: str,
    evidence: dict[str, str] | None = None,
) -> tuple[ContactConsent, bool]:  # fmt: skip
    """Concede el consentimiento para ese canal y propósito. Devuelve la fila vigente y si cambió
    algo (ya concedido: `False`, sin evento). Si cambia, borra `keyword_at` (F7b-05g): así una baja
    por palabra posterior siempre revoca, aunque la guardada fuera futura. `ValueError` si el canal,
    el propósito, el origen o la evidencia no valen (`KEYWORD_STOP` solo revoca); `DoesNotExist`
    si el contacto no es de la organización o está borrado."""
    return _write(ctx, contact_id, channel, purpose, ConsentStatus.GRANTED, source, evidence)


def revoke_consent(
    ctx: TenantContext, *, contact_id: UUID, channel: str, purpose: str, source: str,
    evidence: dict[str, str] | None = None,
) -> tuple[ContactConsent, bool]:  # fmt: skip
    """Revoca el consentimiento (también el de quien nunca lo dio: queda la baja registrada). Sobre
    una baja por palabra, otro origen se registra (F7b-05f): así un alta ya no la levanta. Mismos
    errores y mismo resultado que `grant_consent`."""
    return _write(ctx, contact_id, channel, purpose, ConsentStatus.REVOKED, source, evidence)


def keyword_stop(
    ctx: TenantContext, *, contact_id: UUID, channel: str, purpose: str,
    said_at: dt.datetime | None, evidence: dict[str, str] | None = None,
) -> bool:  # fmt: skip
    """La baja por palabra (F7-08b): revoca con `KEYWORD_STOP`, salvo que la fila ya tenga una
    palabra posterior (`said_at`, el instante del proveedor; `None` si no lo dio o no vale, y una
    más vieja que `KEYWORD_STALE` igual: entonces cuenta nuestra hora más `KEYWORD_AHEAD`, o la
    última guardada si es mayor, F7b-05f y F7b-05g). Un alta de la misma hora no la detiene: en un
    empate gana la baja. Devuelve si cambió algo. Mismos errores que `revoke_consent`."""
    return _write(
        ctx, contact_id, channel, purpose, ConsentStatus.REVOKED, ConsentSource.KEYWORD_STOP,
        evidence, said_at=said_at,
    )[1]  # fmt: skip


def lift_keyword_stop(
    ctx: TenantContext, *, contact_id: UUID, channel: str, purpose: str,
    said_at: dt.datetime | None = None, evidence: dict[str, str] | None = None,
) -> bool:  # fmt: skip
    """El alta por palabra (F7-08b): concede si la fila vigente está `REVOKED` por una palabra de
    baja (`KEYWORD_STOP`) o si no hay fila (F7b-05e: así guarda su hora y una BAJA más vieja,
    entregada después, no revoca). Revocada por otra vía no la levanta (`False`), pero guarda su
    hora. No revierte una palabra posterior a `said_at`, ni una baja de la misma hora; sin
    `said_at`, con uno más allá de «ahora» + `KEYWORD_AHEAD` o más viejo que `KEYWORD_STALE` no hace
    nada (F7b-05f y F7b-05g; decisión del propietario en #937). Devuelve si cambió algo. Mismos
    errores que `grant_consent`."""
    try:
        return _write(
            ctx, contact_id, channel, purpose, ConsentStatus.GRANTED,
            ConsentSource.CUSTOMER_MESSAGE, evidence, lift=True, said_at=said_at,
        )[1]  # fmt: skip
    except _AltaIgnored:
        return False  # ni concede ni guarda una hora: nunca frena ni levanta una baja

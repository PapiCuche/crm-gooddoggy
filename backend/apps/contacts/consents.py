"""Comandos del consentimiento de un contacto (F7-08; ADR-010, 02 §E.5): conceder y revocar.

Sin permisos (los decide la API, o el canal que actúa como sistema). Bloquean el contacto vivo
(`FOR NO KEY UPDATE`, como las notas y el `PATCH`) antes de leer o escribir su fila, así que dos
cambios a la vez del mismo contacto se serializan. Cada cambio real actualiza el estado vigente
(`ContactConsent`), añade su evento al historial inmutable (`ContactConsentEvent`) y se audita;
repetir lo que ya vale no escribe nada. La evidencia lleva solo ids y etiquetas cortas, nunca el
texto de un mensaje ni datos del cliente, y la auditoría no la copia. Cada cambio deja también su
evento en el outbox (`consent.granted` o `consent.revoked`, F7-08b) con ids, canal y propósito.
"""

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


def _evidence(value: dict[str, str] | None) -> dict[str, str]:
    """Solo claves conocidas, con texto corto: la evidencia no es un cajón de datos."""
    found = value or {}
    if not isinstance(found, dict) or not set(found) <= EVIDENCE_KEYS:
        raise ValueError("evidencia no admitida")
    if any(not isinstance(v, str) or len(v) > EVIDENCE_VALUE_MAX for v in found.values()):
        raise ValueError("evidencia no admitida")
    return dict(found)


def _write(
    ctx: TenantContext, contact_id: UUID, channel: str, purpose: str, status: str, source: str,
    evidence: dict[str, str] | None,
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
        where = {"contact_id": contact_id, "channel": channel, "purpose": purpose}
        row = ContactConsent.objects.using(alias).filter(**where).first()
        if row is not None and row.status == status:
            return row, False
        now, before = timezone.now(), row.status if row else None
        stamp = {"granted_at" if status == ConsentStatus.GRANTED else "revoked_at": now}
        values = {
            "status": status, "source": source, "evidence": proof,
            "recorded_by_user_id": ctx.user_id, **stamp,
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
        record(ctx, action, Entity("contact_consent", row.pk), {"status": [before, status]}, about)
        emit(ctx, action, aggregate_type="contact_consent", aggregate_id=row.pk, payload=about)
        return row, True


def grant_consent(
    ctx: TenantContext, *, contact_id: UUID, channel: str, purpose: str, source: str,
    evidence: dict[str, str] | None = None,
) -> tuple[ContactConsent, bool]:  # fmt: skip
    """Concede el consentimiento para ese canal y propósito. Devuelve la fila vigente y si cambió
    algo (ya concedido: `False`, sin evento). `ValueError` si el canal, el propósito, el origen o
    la evidencia no valen (`KEYWORD_STOP` solo revoca); `DoesNotExist` si el contacto no es de la
    organización o está borrado."""
    return _write(ctx, contact_id, channel, purpose, ConsentStatus.GRANTED, source, evidence)


def revoke_consent(
    ctx: TenantContext, *, contact_id: UUID, channel: str, purpose: str, source: str,
    evidence: dict[str, str] | None = None,
) -> tuple[ContactConsent, bool]:  # fmt: skip
    """Revoca el consentimiento (también el de quien nunca lo dio: queda la baja registrada).
    Mismos errores y mismo resultado que `grant_consent`."""
    return _write(ctx, contact_id, channel, purpose, ConsentStatus.REVOKED, source, evidence)

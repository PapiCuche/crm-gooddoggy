"""Comandos de etapa de una oportunidad: mover, ganar, perder y reabrir (F5-03c; E09-06, 08, 09).

No comprueban permisos: los declarará la API (F5-04), la única que los importa (contrato de
import-linter). Una oportunidad cambia de etapa solo aquí, y cada paso deja en la misma
transacción su fila de historial (con los segundos en la etapa que deja), su auditoría y la
versión siguiente. `move_opportunity` va de OPEN a OPEN; ganar y perder llevan a la etapa WON o
LOST del pipeline, y reabrir devuelve una cerrada a una etapa OPEN y borra sus datos de cierre.

Bloqueos, en este orden: el pipeline (`FOR SHARE`), la oportunidad (`FOR NO KEY UPDATE`) y, al
perder, el motivo. Desactivar el pipeline o una etapa bloquea la fila del pipeline: espera a los
pasos en curso y después ve sus oportunidades abiertas (D-F5-01). La etapa de destino se lee ya
con los bloqueos tomados, así que una desactivada a la vez se ve.
"""

from typing import Any
from uuid import UUID

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.audit.services import Entity, record
from apps.deals.commands import StaleVersion, _integer, _uuid
from apps.deals.models import LostReason, Opportunity, OpportunityStageHistory, PipelineStage
from apps.deals.opportunities import (
    NOTE_MAX,
    BadReference,
    OpportunityClosed,
    _decimal,
    _line,
    _locked,
    _pipeline,
    _reference,
    _touch,
)
from core.tenancy.context import TenantContext
from core.tenancy.scope import require_scope

_REQUIRABLE = ("amount", "next_action", "next_action_at", "expected_close_date", "assigned_user_id")


class ClosingStage(Exception):
    """La etapa de destino es WON o LOST: se llega ganando o perdiendo, no moviendo."""


class NotClosed(Exception):
    """La oportunidad está abierta: solo se reabre una ganada o perdida."""


class MissingFields(ValueError):
    """La etapa de destino exige datos que la oportunidad no tiene (`requires_fields`)."""

    def __init__(self, fields: list[str]) -> None:
        super().__init__(fields)
        self.fields = fields


def _begin(ctx: TenantContext, alias: str, opportunity_id: Any, version: Any) -> Opportunity:
    """Valida los argumentos, toma los bloqueos y comprueba la versión. `DoesNotExist` si no es
    de la organización o está borrada."""
    opportunity_id = _uuid(opportunity_id, "oportunidad")
    _integer(version, 1, 2**31 - 1, "versión")
    live = Opportunity.objects.using(alias).filter(deleted_at__isnull=True)
    _pipeline(
        alias,
        ctx.organization_id,
        live.values_list("pipeline_id", flat=True).get(pk=opportunity_id),
    )
    opp = _locked(alias, opportunity_id)
    if opp.version != version:
        raise StaleVersion(opportunity_id)
    return opp


def _stage(alias: str, opp: Opportunity, **where: Any) -> PipelineStage:
    """La primera etapa activa del pipeline de la oportunidad que cumple `where`."""
    stages = PipelineStage.objects.using(alias).filter(
        pipeline_id=opp.pipeline_id, is_active=True, **where
    )
    found: PipelineStage | None = stages.order_by("sort_order", "id").first()
    if found is None:
        raise BadReference("stage_id")
    return found


def _enter(
    ctx: TenantContext, alias: str, opp: Opportunity, stage: PipelineStage, action: str,
    extra: dict[str, list[Any]],
) -> None:  # fmt: skip
    """Lleva la oportunidad a `stage` con el cambio de estado y de datos de `extra`
    (`{campo: [antes, después]}`): historial, probabilidad de la etapa, reloj de la etapa y
    auditoría. `MissingFields` si la etapa exige datos que faltan."""
    now = timezone.now()
    wanted: dict[str, list[Any]] = {
        "stage_id": [opp.stage_id, stage.pk],
        "status": [opp.status, stage.stage_type],
        "probability": [opp.probability, stage.default_probability],
        "stage_entered_at": [opp.stage_entered_at, now],
        **extra,
    }
    changes = {k: v for k, v in wanted.items() if v[0] != v[1]}
    after = {k: v[1] for k, v in wanted.items()}
    if missing := [
        f
        for f in _REQUIRABLE
        if stage.requires_fields.get(f) is True and not after.get(f, getattr(opp, f))
    ]:
        raise MissingFields(missing)
    before = PipelineStage.objects.using(alias).get(pk=opp.stage_id)
    seconds = max(0, int((now - opp.stage_entered_at).total_seconds()))
    try:
        OpportunityStageHistory.objects.using(alias).create(
            opportunity=opp, from_stage=before, to_stage=stage, changed_by_user_id=ctx.user_id,
            changed_at=now, seconds_in_previous_stage=seconds,
        )  # fmt: skip
        _touch(ctx, alias, opp, changes)
    except IntegrityError as error:  # quien actúa no es miembro de la organización
        raise _reference(error) from None
    shown = {k: v for k, v in changes.items() if k != "stage_entered_at"}
    if note := shown.get("lost_reason_note"):
        shown["lost_reason_note"] = [
            None if note[0] is None else "[texto]",
            "[texto]" if note[1] else None,
        ]
    about = {
        "from_stage": before.code,
        "to_stage": stage.code,
        "seconds_in_previous_stage": seconds,
    }
    record(ctx, action, Entity("opportunity", opp.pk, opp.number), shown, about)


def move_opportunity(
    ctx: TenantContext, *, opportunity_id: UUID, version: int, stage_id: UUID
) -> Opportunity:
    """Mueve una oportunidad abierta a otra etapa OPEN activa de su pipeline: toma la
    probabilidad de la etapa, reinicia su reloj, cuenta como actividad y deja historial y
    auditoría (`opportunity.moved`); si ya está en ella, no escribe. `StaleVersion`;
    `OpportunityClosed` si no está abierta; `BadReference("stage_id")` si la etapa no es activa
    ni de su pipeline; `ClosingStage` si es WON o LOST (se gana o se pierde); `MissingFields`;
    `DoesNotExist`."""
    alias = require_scope(ctx)
    target = _uuid(stage_id, "etapa")
    with transaction.atomic(using=alias):
        opp = _begin(ctx, alias, opportunity_id, version)
        if opp.status != "OPEN":
            raise OpportunityClosed(opp.pk)
        stage = _stage(alias, opp, pk=target)
        if stage.stage_type != "OPEN":
            raise ClosingStage(target)
        if stage.pk != opp.stage_id:
            _enter(ctx, alias, opp, stage, "opportunity.moved", {})
    return opp


def win_opportunity(
    ctx: TenantContext, *, opportunity_id: UUID, version: int, amount: str
) -> Opportunity:
    """Gana una oportunidad abierta por el importe final (mayor que 0): la lleva a la etapa WON
    de su pipeline con `won_at`, probabilidad 100, historial y auditoría (`opportunity.won`).
    Mismos errores que `move_opportunity`, sin `ClosingStage`."""
    alias, final = require_scope(ctx), _decimal(amount, "importe final")
    if final <= 0:
        raise ValueError("importe final: mayor que 0")
    with transaction.atomic(using=alias):
        opp = _begin(ctx, alias, opportunity_id, version)
        if opp.status != "OPEN":
            raise OpportunityClosed(opp.pk)
        extra: dict[str, list[Any]] = {
            "amount": [opp.amount, final],
            "won_at": [None, timezone.now()],
        }
        _enter(ctx, alias, opp, _stage(alias, opp, stage_type="WON"), "opportunity.won", extra)
    return opp


def lose_opportunity(
    ctx: TenantContext, *, opportunity_id: UUID, version: int, lost_reason_id: UUID,
    note: str | None = None,
) -> Opportunity:  # fmt: skip
    """Pierde una oportunidad abierta por un motivo activo de la organización (obligatorio) y una
    nota opcional: la lleva a la primera etapa LOST de su pipeline con `lost_at`, probabilidad 0,
    historial y auditoría (`opportunity.lost`, con la nota como `[texto]`). `BadReference
    ("lost_reason_id")` si el motivo no existe, está borrado o inactivo. Mismos errores que
    `win_opportunity`; si falla, no escribe nada."""
    alias, reason = require_scope(ctx), _uuid(lost_reason_id, "motivo")
    text = None if note is None else _line(note, "nota", NOTE_MAX) or None
    with transaction.atomic(using=alias):
        opp = _begin(ctx, alias, opportunity_id, version)
        if opp.status != "OPEN":
            raise OpportunityClosed(opp.pk)
        usable = LostReason.objects.using(alias).filter(
            pk=reason, is_active=True, deleted_at__isnull=True
        )
        if not usable.select_for_update(no_key=True).values_list("pk", flat=True)[:1]:
            raise BadReference("lost_reason_id")
        extra: dict[str, list[Any]] = {
            "lost_at": [None, timezone.now()],
            "lost_reason_id": [None, reason],
            "lost_reason_note": [None, text],
        }
        _enter(ctx, alias, opp, _stage(alias, opp, stage_type="LOST"), "opportunity.lost", extra)
    return opp


def reopen_opportunity(
    ctx: TenantContext, *, opportunity_id: UUID, version: int, stage_id: UUID
) -> Opportunity:
    """Devuelve una oportunidad ganada o perdida a una etapa OPEN activa de su pipeline y borra
    sus datos de cierre (fecha, motivo y nota); el importe se queda. Historial y auditoría
    (`opportunity.reopened`). `NotClosed` si está abierta; `BadReference("stage_id")` si la etapa
    no es OPEN, activa y de su pipeline; `StaleVersion`, `MissingFields`, `DoesNotExist`."""
    alias, target = require_scope(ctx), _uuid(stage_id, "etapa")
    with transaction.atomic(using=alias):
        opp = _begin(ctx, alias, opportunity_id, version)
        if opp.status == "OPEN":
            raise NotClosed(opp.pk)
        cleared = ("won_at", "lost_at", "lost_reason_id", "lost_reason_note")
        extra: dict[str, list[Any]] = {field: [getattr(opp, field), None] for field in cleared}
        _enter(ctx, alias, opp, _stage(alias, opp, pk=target, stage_type="OPEN"),
               "opportunity.reopened", extra)  # fmt: skip
    return opp

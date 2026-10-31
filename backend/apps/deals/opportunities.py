"""Comandos de oportunidades: crear, cambiar, asignar y borrar (F5-03b; E09, modelo de datos §E.6).

No comprueban permisos: los declarará la API (F5-04), la única que los importa (contrato de
import-linter). Validan los tipos exactos (`ValueError`, nunca `TypeError` ni `IntegrityError`) y
auditan con el antes y el después; el texto libre (título, próxima acción) se audita como
`[texto]` y las notas de las líneas no. Sin cambios no escriben ni auditan. Bloqueos: contacto,
pipeline y variantes nuevas (`FOR SHARE`, estas por id, como el catálogo), oportunidad, miembro.
"""

import datetime as dt
from collections.abc import Sequence
from decimal import Decimal
from typing import Any
from uuid import UUID

from django.db import IntegrityError, connections, transaction
from django.db.models import F
from django.utils import timezone

from apps.audit.services import Entity, record
from apps.contacts.models import Contact
from apps.deals.commands import StaleVersion, _diff, _integer, _text, _uuid
from apps.deals.models import (
    Opportunity,
    OpportunityItem,
    OpportunitySource,
    OpportunityStageHistory,
    Pipeline,
    PipelineStage,
    Priority,
)
from apps.deals.numbers import next_opportunity_number
from apps.organizations.selectors import lock_active_member
from core.money import parse_amount
from core.tenancy.context import TenantContext
from core.tenancy.scope import require_scope
from core.text import line
from core.text import name as clean_name

TITLE_MAX, ACTION_MAX, NOTE_MAX, ITEMS_MAX, QUANTITY_MAX = 200, 200, 1000, 50, 1_000_000
_CREATE = frozenset(
    {"amount", "priority", "source", "next_action", "next_action_at", "expected_close_date"}
)
_UPDATE = _CREATE | {"title", "probability", "items"}
_ITEM = frozenset({"variant_id", "quantity", "estimated_unit_price", "notes"})
_REFERENCES = {  # FK compuesta violada → el campo que la causó
    "opportunities_contact_fk": "contact_id",
    "opportunities_assigned_fk": "assigned_user_id",
    "opportunity_items_variant_fk": "variant_id",
    "opportunities_created_by_fk": "created_by_user_id",  # quien actúa no es miembro
    "opportunities_updated_by_fk": "updated_by_user_id",
    "opportunities_deleted_by_fk": "deleted_by_user_id",
    "opp_history_changed_by_fk": "changed_by_user_id",
}
TOP = Decimal("999999999999.99")  # lo que cabe en NUMERIC(14,2): cada línea y su suma
_EARLIEST, _LATEST = dt.datetime(2000, 1, 1, tzinfo=dt.UTC), dt.datetime(2100, 1, 1, tzinfo=dt.UTC)


class BadReference(ValueError):
    """Una referencia que no sirve (contacto, pipeline, responsable, variante o quien actúa, que
    no son de la organización, están borrados, inactivos o no son miembros). `field` dice cuál."""

    def __init__(self, field: str) -> None:
        super().__init__(field)
        self.field = field


class OpportunityClosed(Exception):
    """La oportunidad está ganada o perdida: no se edita sin reabrirla."""


def _decimal(value: Any, what: str) -> Decimal:
    """Un importe de `core.money` (texto o `Decimal`, 12 enteras y 2 decimales, sin signo)."""
    bad = ValueError(f"{what}: un importe con 12 cifras enteras y 2 decimales")
    if isinstance(value, bool) or not isinstance(value, str | Decimal):
        raise bad
    if isinstance(value, Decimal) and not (value.is_finite() and abs(value.adjusted()) <= 12):
        raise bad
    try:
        return parse_amount(value)
    except ValueError, TypeError, ArithmeticError:
        raise bad from None


def _line(value: Any, what: str, limit: int) -> str:
    text = _text(value, what)
    if len(text) > limit * 4:  # antes de normalizar: un texto enorme no se procesa
        raise ValueError(f"{what}: hasta {limit} caracteres")
    return line(text, limit)


def _moment(value: Any, what: str) -> dt.datetime:
    if not (
        isinstance(value, dt.datetime)
        and value.utcoffset() is not None
        and _EARLIEST <= value <= _LATEST
    ):
        raise ValueError(f"{what}: fecha y hora con zona horaria, entre los años 2000 y 2100")
    return value.astimezone(dt.UTC)  # un desfase extremo (+23:59) no cabe en PostgreSQL


def _values(fields: dict[str, Any]) -> dict[str, Any]:
    """Lo que se envía de una oportunidad, limpio y validado (solo esos campos)."""
    values = dict(fields)
    if "title" in values:
        values["title"] = clean_name(_line(values["title"], "título", TITLE_MAX), TITLE_MAX)
    if "amount" in values:
        values["amount"] = _decimal(values["amount"], "importe")
    for field, choices in (("priority", Priority.values), ("source", OpportunitySource.values)):
        if field in values and not (isinstance(values[field], str) and values[field] in choices):
            raise ValueError(f"{field}: uno de {', '.join(choices)}")
    if values.get("next_action") is not None:
        values["next_action"] = _line(values["next_action"], "próxima acción", ACTION_MAX)
        if not values["next_action"]:
            raise ValueError("próxima acción: texto, o None para quitarla")
    if values.get("next_action_at") is not None:
        values["next_action_at"] = _moment(values["next_action_at"], "próxima acción, fecha")
    if (day := values.get("expected_close_date")) is not None and not (
        type(day) is dt.date and 2000 <= day.year <= 2100
    ):
        raise ValueError("fecha esperada de cierre: una fecha entre los años 2000 y 2100")
    if "probability" in values:
        _integer(values["probability"], 0, 100, "probabilidad")
    return values


def _items(raw: Any) -> dict[UUID, dict[str, Any]]:
    """Las líneas como se guardarán, por variante: hasta 50, una por variante."""
    if not isinstance(raw, list | tuple) or len(raw) > ITEMS_MAX:
        raise ValueError(f"las líneas, una lista de hasta {ITEMS_MAX} objetos")
    rows: dict[UUID, dict[str, Any]] = {}
    total = Decimal(0)
    for spec in raw:
        if (
            not isinstance(spec, dict)
            or set(spec) - _ITEM
            or {"variant_id", "quantity"} - set(spec)
        ):
            raise ValueError(
                f"cada línea es un objeto con {sorted(_ITEM)}, y variant_id y quantity"
            )
        variant = _uuid(spec["variant_id"], "variante")
        if variant in rows:
            raise ValueError("variante repetida")
        _integer(spec["quantity"], 1, QUANTITY_MAX, "cantidad")
        price = _decimal(spec.get("estimated_unit_price", "0"), "precio")
        total += spec["quantity"] * price
        if total > TOP:
            raise ValueError(f"líneas: cantidad por precio, y su suma, hasta {TOP}")
        rows[variant] = {
            "quantity": spec["quantity"],
            "estimated_unit_price": price,
            "notes": _line(spec.get("notes", ""), "notas", NOTE_MAX),
        }
    return rows


def _audited(field: str, value: Any) -> Any:
    return "[texto]" if field in ("title", "next_action") and value is not None else value


def _shown(rows: dict[UUID, dict[str, Any]]) -> list[dict[str, Any]]:
    """Las líneas para la auditoría: sin las notas, solo si las hay."""
    return [
        {"variant_id": str(v), "quantity": r["quantity"], "has_notes": bool(r["notes"]),
         "estimated_unit_price": str(r["estimated_unit_price"])}
        for v, r in sorted(rows.items(), key=lambda pair: str(pair[0]))
    ]  # fmt: skip


def _entity(opp: Opportunity) -> Entity:
    return Entity("opportunity", opp.pk, opp.number)


def _reference(error: IntegrityError) -> Exception:
    for constraint, field in _REFERENCES.items():
        if constraint in str(error):
            return BadReference(field)
    raise error


def _locked(alias: str, opportunity_id: UUID) -> Opportunity:
    live = Opportunity.objects.using(alias).filter(deleted_at__isnull=True)
    found: Opportunity = live.select_for_update(no_key=True).get(pk=opportunity_id)
    return found


def _touch(ctx: TenantContext, alias: str, opp: Opportunity, changes: dict[str, Any]) -> None:
    """Guarda `changes` (`{campo: [antes, después]}`), cuenta como actividad y sube la versión."""
    for field, (_, value) in changes.items():
        setattr(opp, field, value)
    opp.last_activity_at = timezone.now()
    opp.updated_by_user_id = ctx.user_id
    opp.version = F("version") + 1
    stamps = ["last_activity_at", "updated_by_user_id", "version", "updated_at"]
    opp.save(using=alias, update_fields=[*changes, *stamps])
    opp.refresh_from_db(using=alias, fields=["version"])


def _current_items(alias: str, opp: Opportunity) -> dict[UUID, dict[str, Any]]:
    found = OpportunityItem.objects.using(alias).filter(opportunity_id=opp.pk)
    return {
        i.variant_id: {"quantity": i.quantity, "notes": i.notes,
                       "estimated_unit_price": i.estimated_unit_price}
        for i in found
    }  # fmt: skip


def _sync_items(
    alias: str, organization_id: UUID, opp: Opportunity,
    now: dict[UUID, dict[str, Any]], wanted: dict[UUID, dict[str, Any]],
) -> None:  # fmt: skip
    """Deja las líneas como `wanted` (borra, cambia, añade); una variante nueva ha de estar viva
    y activa (`FOR SHARE`: borrarla a la vez espera)."""
    if added := [v for v in wanted if v not in now]:
        with connections[alias].cursor() as cursor:
            cursor.execute(
                "SELECT id FROM public.product_variants WHERE organization_id = %s "
                "AND id = ANY(%s) AND deleted_at IS NULL AND status = 'ACTIVE' "
                "ORDER BY id FOR SHARE",
                [organization_id, added],
            )
            if len(cursor.fetchall()) != len(added):
                raise BadReference("variant_id")
    items = OpportunityItem.objects.using(alias)
    items.filter(opportunity_id=opp.pk).exclude(variant_id__in=wanted).delete()
    for variant, row in wanted.items():
        if variant in now and now[variant] != row:
            items.filter(opportunity_id=opp.pk, variant_id=variant).update(
                **row, updated_at=timezone.now()
            )
    items.bulk_create(
        OpportunityItem(organization_id=organization_id, opportunity=opp, variant_id=v, **row)
        for v, row in wanted.items()
        if v not in now
    )


def _pipeline(alias: str, organization_id: UUID, pipeline_id: UUID | None) -> Pipeline:
    """El pipeline vivo y activo (o el predeterminado), `FOR SHARE`: desactivarlo espera al alta."""
    which = "is_default" if pipeline_id is None else "id = %s AND is_active"
    query = (
        f"SELECT id FROM public.pipelines WHERE organization_id = %s AND {which} "  # noqa: S608
        "AND deleted_at IS NULL FOR SHARE"
    )
    with connections[alias].cursor() as cursor:
        cursor.execute(query, [organization_id, *([] if pipeline_id is None else [pipeline_id])])
        row = cursor.fetchone()
    if row is None:
        raise BadReference("pipeline_id")
    found: Pipeline = Pipeline.objects.using(alias).get(pk=row[0])
    return found


def create_opportunity(
    ctx: TenantContext,
    *,
    contact_id: UUID,
    title: str,
    pipeline_id: UUID | None = None,
    assigned_user_id: UUID | None = None,
    items: Sequence[dict[str, Any]] = (),
    **fields: Any,
) -> Opportunity:
    """Crea una oportunidad abierta de un contacto vivo, con su número, en la primera etapa OPEN
    activa del pipeline (el predeterminado, sin `pipeline_id`) y su probabilidad, la moneda del
    pipeline y su primera fila de historial; responsable, quien actúa si no se dice otro (miembro
    activo). `items`: `{variant_id, quantity, estimated_unit_price?, notes?}` de variantes vivas
    y activas. Audita `opportunity.created`. `BadReference` con el campo; `ValueError`."""
    alias = require_scope(ctx)
    if unknown := set(fields) - _CREATE:
        raise ValueError(f"campos no admitidos: {sorted(unknown)}")
    values = _values({"title": title, **fields})
    contact_id = _uuid(contact_id, "contacto")
    pipeline_id = None if pipeline_id is None else _uuid(pipeline_id, "pipeline")
    owner = ctx.user_id if assigned_user_id is None else _uuid(assigned_user_id, "responsable")
    rows = _items(items)
    try:
        with transaction.atomic(using=alias):
            live = Contact.objects.using(alias).filter(pk=contact_id, deleted_at__isnull=True)
            if not live.select_for_update(no_key=True).values_list("pk", flat=True)[:1]:
                raise BadReference("contact_id")
            pipeline = _pipeline(alias, ctx.organization_id, pipeline_id)
            stages = PipelineStage.objects.using(alias).filter(
                pipeline_id=pipeline.pk, stage_type="OPEN", is_active=True
            )
            stage = stages.order_by("sort_order", "id").first()
            if stage is None:
                raise BadReference("pipeline_id")
            if owner is not None and not lock_active_member(owner):
                raise BadReference("assigned_user_id")
            now = timezone.now()
            opp: Opportunity = Opportunity.objects.using(alias).create(
                number=next_opportunity_number(ctx), contact_id=contact_id, pipeline=pipeline,
                stage=stage, status="OPEN", assigned_user_id=owner, currency=pipeline.currency,
                probability=stage.default_probability, last_activity_at=now, stage_entered_at=now,
                created_by_user_id=ctx.user_id, updated_by_user_id=ctx.user_id, **values,
            )  # fmt: skip
            OpportunityStageHistory.objects.using(alias).create(
                opportunity=opp, to_stage=stage, changed_by_user_id=ctx.user_id, changed_at=now
            )
            _sync_items(alias, ctx.organization_id, opp, {}, rows)
            shown = {
                **values, "contact_id": contact_id, "pipeline_id": pipeline.pk,
                "stage_id": stage.pk, "status": "OPEN", "assigned_user_id": owner,
                "currency": pipeline.currency, "probability": stage.default_probability,
            }  # fmt: skip
            changes = {k: [None, _audited(k, v)] for k, v in shown.items()}
            changes["items"] = [None, _shown(rows)]
            record(ctx, "opportunity.created", _entity(opp), changes)
    except IntegrityError as error:
        raise _reference(error) from None
    return opp


def update_opportunity(
    ctx: TenantContext, *, opportunity_id: UUID, version: int, **fields: Any
) -> Opportunity:
    """Cambia lo que se envía de una oportunidad abierta y viva (`items` reemplaza las líneas), si
    nadie la cambió desde `version`, y audita `opportunity.updated`; sin cambios no escribe.
    `DoesNotExist`; `StaleVersion`; `OpportunityClosed`; `BadReference`; `ValueError`."""
    alias = require_scope(ctx)
    if unknown := set(fields) - _UPDATE:
        raise ValueError(f"campos no admitidos: {sorted(unknown)}")
    values = _values({k: v for k, v in fields.items() if k != "items"})
    wanted = _items(fields["items"]) if "items" in fields else None
    opportunity_id = _uuid(opportunity_id, "oportunidad")
    _integer(version, 1, 2**31 - 1, "versión")
    try:
        with transaction.atomic(using=alias):
            opp = _locked(alias, opportunity_id)
            if opp.version != version:
                raise StaleVersion(opportunity_id)
            if opp.status != "OPEN":
                raise OpportunityClosed(opportunity_id)
            diff = _diff(opp, values)
            now = _current_items(alias, opp) if wanted is not None else {}
            items_changed = wanted is not None and now != wanted
            if diff or items_changed:
                changes = {
                    k: [_audited(k, old), _audited(k, new)] for k, (old, new) in diff.items()
                }
                if wanted is not None and items_changed:
                    _sync_items(alias, ctx.organization_id, opp, now, wanted)
                    changes["items"] = [_shown(now), _shown(wanted)]
                _touch(ctx, alias, opp, diff)
                record(ctx, "opportunity.updated", _entity(opp), changes)
    except IntegrityError as error:
        raise _reference(error) from None
    return opp


def assign_opportunity(
    ctx: TenantContext, *, opportunity_id: UUID, version: int, assigned_user_id: UUID
) -> Opportunity:
    """Deja la oportunidad viva con ese responsable, que es miembro activo de la organización, si
    nadie la cambió desde `version`, y lo audita (`opportunity.assigned`); si ya lo es, no escribe.
    Bloquea primero la oportunidad y después la membresía, como `assign_contact`. `BadReference`
    si no es miembro activo; `DoesNotExist` y `StaleVersion`, como `update_opportunity`."""
    alias = require_scope(ctx)
    owner = _uuid(assigned_user_id, "responsable")
    opportunity_id = _uuid(opportunity_id, "oportunidad")
    _integer(version, 1, 2**31 - 1, "versión")
    try:
        with transaction.atomic(using=alias):
            opp = _locked(alias, opportunity_id)
            if opp.version != version:
                raise StaleVersion(opportunity_id)
            if changes := _diff(opp, {"assigned_user_id": owner}):
                if not lock_active_member(owner):
                    raise BadReference("assigned_user_id")
                _touch(ctx, alias, opp, changes)
                record(ctx, "opportunity.assigned", _entity(opp), changes)
    except IntegrityError as error:
        raise _reference(error) from None
    return opp


def delete_opportunity(ctx: TenantContext, *, opportunity_id: UUID) -> None:
    """Borra una oportunidad (borrado lógico, abierta o cerrada) y lo audita
    (`opportunity.deleted`). `DoesNotExist` si no es de la organización o ya está borrada."""
    alias = require_scope(ctx)
    opportunity_id = _uuid(opportunity_id, "oportunidad")
    try:
        with transaction.atomic(using=alias):
            opp = _locked(alias, opportunity_id)
            gone = {"deleted_at": [None, timezone.now()], "deleted_by_user_id": [None, ctx.user_id]}
            _touch(ctx, alias, opp, gone)
            record(ctx, "opportunity.deleted", _entity(opp))
    except IntegrityError as error:
        raise _reference(error) from None

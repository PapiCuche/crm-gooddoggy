"""Comandos de pipelines, etapas y motivos de pérdida (F5-01; E09-05, modelo de datos §E.6).

No comprueban permisos: los declarará la API (`pipelines.manage`, F5-02), la única que los importa
(contrato de import-linter). Validan lo que guardan y auditan cada cambio (sin datos personales).

Invariantes tras cada comando: una etapa WON activa, al menos una LOST y una OPEN activas, y un
pipeline predeterminado vivo y activo por organización. Los pipelines toman un bloqueo consultivo
por organización; las etapas, la fila del pipeline (`FOR NO KEY UPDATE`).
"""

import re
from collections.abc import Callable, Iterable, Sequence
from typing import Any
from uuid import UUID

from django.db import IntegrityError, transaction
from django.db.models import F, Max

from apps.audit.services import Entity, record
from apps.deals.models import (
    CODE,
    COLOR,
    REASON_CODE,
    LostReason,
    Pipeline,
    PipelineStage,
    StageType,
)
from apps.deals.seed import lock_pipelines
from core.tenancy.context import TenantContext
from core.tenancy.scope import require_scope
from core.text import name as clean_name

NAME_MAX, CODE_MAX, DAYS_MAX, ORDER_MAX = 100, 30, 32767, 32767
CURRENCIES = ("PEN",)  # D-PRC-2: solo PEN en el MVP; la columna ya admite otras
CLOSED = {StageType.WON: 100, StageType.LOST: 0}  # la probabilidad de lo cerrado
STAGE_FIELDS = frozenset(
    {"name", "stage_type", "default_probability", "stale_after_days", "color", "requires_fields"}
)
NEW_STAGE = STAGE_FIELDS | {"code", "is_active"}
_PIPELINE_FIELDS = frozenset({"name", "kind", "sort_order", "is_active", "is_default"})
_REASON_FIELDS = frozenset({"name", "sort_order", "is_active"})


class StaleVersion(Exception):
    """Otra escritura cambió el pipeline o el motivo después de la versión que se editaba."""


class CodeTaken(Exception):
    """Ya hay un pipeline (o un motivo, o una etapa del pipeline) con ese código."""


class PipelineIsDefault(Exception):
    """Es el predeterminado: no se desactiva ni deja de serlo sin elegir otro."""


class Invalid(ValueError):
    """Un dato que no sirve; `field` es el campo del cuerpo de la API (400)."""

    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field


class StageInvariant(ValueError):
    """El cambio dejaría el pipeline sin su única etapa WON, sin una LOST o sin una OPEN."""


def _code(value: str, pattern: str, fold: Callable[[str], str]) -> str:
    text = fold(value.strip()) if value.isascii() else ""
    if not (len(text) <= CODE_MAX and re.fullmatch(pattern, text)):
        raise ValueError(f"código con letras sin acento, cifras y guiones, hasta {CODE_MAX}")
    return text


def _integer(value: Any, low: int, high: int, what: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError(f"{what}: un entero de {low} a {high}")


def _flag(value: Any, what: str) -> bool:
    """Un booleano de verdad: Django guardaría `"False"` o `0` como `False`, pero las invariantes
    leen la verdad de Python. Compartido por los comandos de pipelines y etapas."""
    if not isinstance(value, bool):
        raise ValueError(f"{what}: verdadero o falso")
    return value


def _text(value: Any, what: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{what}: un texto")
    return value


def _uuid(value: Any, what: str) -> UUID:
    if isinstance(value, UUID):
        return value
    try:
        return UUID(_text(value, what))
    except ValueError:
        raise ValueError(f"{what}: un identificador") from None


def _stage_values(fields: dict[str, Any]) -> dict[str, Any]:
    """Lo que se envía de una etapa, limpio y validado (solo esos campos)."""
    values = dict(fields)
    if "name" in values:
        values["name"] = clean_name(_text(values["name"], "nombre"), NAME_MAX)
    if "code" in values:
        values["code"] = _code(_text(values["code"], "código"), CODE, str.lower)
    if "stage_type" in values and values["stage_type"] not in StageType.values:
        raise ValueError("tipo de etapa no admitido")
    if "default_probability" in values:
        _integer(values["default_probability"], 0, 100, "probabilidad")
    if values.get("stale_after_days") is not None:
        _integer(values["stale_after_days"], 1, DAYS_MAX, "días hasta estancarse")
    if "color" in values:
        values["color"] = _text(values["color"], "color").strip().lower()
        if not re.fullmatch(COLOR, values["color"]):
            raise ValueError("un color #RRGGBB, como #1f9d55")
    if "requires_fields" in values and not isinstance(values["requires_fields"], dict):
        raise ValueError("requires_fields: un objeto")
    if "is_active" in values:
        _flag(values["is_active"], "is_active")
    return values


def _closed_probability(stage_type: str, probability: int) -> None:
    if stage_type in CLOSED and probability != CLOSED[StageType(stage_type)]:
        raise Invalid(
            "default_probability",
            f"una etapa {stage_type} tiene probabilidad {CLOSED[StageType(stage_type)]}",
        )


def _check(stages: Iterable[tuple[str, bool]]) -> None:
    """`(tipo, activa)` de todas las etapas del pipeline tal como quedarían."""
    kinds = list(stages)
    live = [kind for kind, active in kinds if active]
    if [kind for kind, _ in kinds].count("WON") != 1 or live.count("WON") != 1:
        raise StageInvariant("el pipeline lleva exactamente una etapa WON, activa")
    if "LOST" not in live:
        raise StageInvariant("el pipeline lleva al menos una etapa LOST activa")
    if "OPEN" not in live:
        raise StageInvariant("el pipeline lleva al menos una etapa OPEN activa")


def _new_stage(spec: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(spec, dict):
        raise ValueError("cada etapa es un objeto")
    if unknown := set(spec) - NEW_STAGE:
        raise ValueError(f"campos de etapa no admitidos: {sorted(unknown)}")
    if missing := {"name", "code", "stage_type", "default_probability", "color"} - set(spec):
        raise ValueError(f"faltan campos de la etapa: {sorted(missing)}")
    values = _stage_values(
        {"stale_after_days": None, "requires_fields": {}, "is_active": True, **spec}
    )
    _closed_probability(values["stage_type"], values["default_probability"])
    return values


def _no_open_opportunities(alias: str, pipeline_id: UUID, stage_id: UUID | None = None) -> None:
    """Gancho (F5-03): desactivar un pipeline o una etapa con oportunidades abiertas no se permite
    sin migrarlas (02 §E.6). Aún no existe `opportunities`: no hay nada que comprobar. Cuando
    exista, lanzará `OpenOpportunities` aquí y se activará su test (decisión en phase-5.md)."""


def _live(alias: str) -> Any:
    return Pipeline.objects.using(alias).filter(deleted_at__isnull=True)


def _locked(alias: str, pipeline_id: UUID) -> Pipeline:
    pipeline: Pipeline = _live(alias).select_for_update(no_key=True).get(pk=pipeline_id)
    return pipeline


def _stages(alias: str, pipeline_id: UUID) -> list[PipelineStage]:
    found = PipelineStage.objects.using(alias).filter(pipeline_id=pipeline_id)
    return list(found.order_by("sort_order", "id"))


def _renumber(alias: str, ordered: Sequence[PipelineStage]) -> None:
    for index, stage in enumerate(ordered):
        if stage.sort_order != index:
            stage.sort_order = index
            stage.save(using=alias, update_fields=["sort_order", "updated_at"])


def _diff(obj: Any, values: dict[str, Any]) -> dict[str, list[Any]]:
    return {f: [getattr(obj, f), v] for f, v in values.items() if getattr(obj, f) != v}


def _save(ctx: TenantContext, alias: str, obj: Any, changes: dict[str, list[Any]]) -> None:
    for field, (_, value) in changes.items():
        setattr(obj, field, value)
    obj.updated_by_user_id = ctx.user_id
    obj.version = F("version") + 1
    obj.save(using=alias, update_fields=[*changes, "updated_by_user_id", "version", "updated_at"])
    obj.refresh_from_db(using=alias, fields=["version"])


def _taken(error: IntegrityError, *constraints: str) -> Exception:
    if not any(c in str(error) for c in constraints):
        raise error
    return CodeTaken()


def _drop_default(ctx: TenantContext, alias: str) -> None:
    """El predeterminado actual deja de serlo, con su auditoría."""
    for other in _live(alias).select_for_update(no_key=True).filter(is_default=True):
        other.is_default = False
        _save(ctx, alias, other, {"is_default": [True, False]})
        record(
            ctx,
            "pipeline.updated",
            Entity("pipeline", other.pk, other.code),
            {"is_default": [True, False]},
        )


def _pipeline_values(fields: dict[str, Any]) -> dict[str, Any]:
    values = dict(fields)
    if "name" in values:
        values["name"] = clean_name(_text(values["name"], "nombre"), NAME_MAX)
    if "kind" in values and values["kind"] not in Pipeline.Kind.values:
        raise ValueError("tipo de pipeline no admitido")
    if "sort_order" in values:
        _integer(values["sort_order"], -32768, 32767, "orden")
    for flag in ("is_active", "is_default"):
        if flag in values:
            _flag(values[flag], flag)
    return values


def create_pipeline(
    ctx: TenantContext,
    *,
    name: str,
    code: str,
    kind: str,
    stages: Sequence[dict[str, Any]],
    currency: str = "PEN",
    sort_order: int = 0,
    is_default: bool | None = None,
) -> Pipeline:
    """Crea un pipeline con sus etapas en una transacción y lo audita. Las etapas se numeran en el
    orden recibido. Es el predeterminado si se pide o si la organización no tiene
    otro, aunque se diga que no; pedirlo quita la marca al anterior. `ValueError` si algo no vale,
    `StageInvariant` si las etapas no cumplen las invariantes (una WON, una LOST, una OPEN),
    `CodeTaken`; sin escribir nada."""
    alias = require_scope(ctx)
    if currency not in CURRENCIES:
        raise ValueError(f"solo {', '.join(CURRENCIES)}")
    flags = {} if is_default is None else {"is_default": is_default}
    values = _pipeline_values({"name": name, "kind": kind, "sort_order": sort_order, **flags}) | {
        "code": _code(_text(code, "código"), CODE, str.lower),
        "currency": currency,
    }
    values.pop("is_default", None)
    if not isinstance(stages, list | tuple):
        raise ValueError("las etapas son una lista de objetos")
    rows = [_new_stage(spec) for spec in stages]
    if len({row["code"] for row in rows}) != len(rows):
        raise Invalid("stages", "códigos de etapa repetidos")
    _check((row["stage_type"], row["is_active"]) for row in rows)
    try:
        with transaction.atomic(using=alias):
            lock_pipelines(alias, ctx.organization_id)
            had_default = _live(alias).filter(is_default=True).exists()
            if is_default and had_default:
                _drop_default(ctx, alias)
            is_default = bool(is_default) or not had_default  # la primera siempre lo es
            pipeline: Pipeline = Pipeline.objects.using(alias).create(
                **values, is_default=is_default, created_by_user_id=ctx.user_id,
                updated_by_user_id=ctx.user_id,
            )  # fmt: skip
            PipelineStage.objects.using(alias).bulk_create(
                PipelineStage(
                    organization_id=ctx.organization_id, pipeline=pipeline, sort_order=order, **row
                )
                for order, row in enumerate(rows)
            )
            changes = {k: [None, v] for k, v in {**values, "is_default": is_default}.items()}
            changes["stages"] = [None, [row["code"] for row in rows]]
            record(ctx, "pipeline.created", Entity("pipeline", pipeline.pk, pipeline.code), changes)
    except IntegrityError as error:
        raise _taken(error, "pipelines_code_uq") from None
    return pipeline


def update_pipeline(
    ctx: TenantContext, *, pipeline_id: UUID, version: int, **fields: Any
) -> Pipeline:
    """Cambia lo que se envía de un pipeline vivo, si nadie lo cambió desde `version`, y lo audita
    con el antes y el después; sin cambios no escribe. `is_default=True` quita la marca al
    anterior en la misma transacción. `DoesNotExist` si no es de la organización o está borrado;
    `StaleVersion`; `PipelineIsDefault` si se desactiva el predeterminado o se le quita la marca
    (se elige otro); `ValueError` si se marca uno inactivo. El código y la moneda no cambian."""
    alias = require_scope(ctx)
    if unknown := set(fields) - _PIPELINE_FIELDS:
        raise ValueError(f"campos no admitidos: {sorted(unknown)}")
    values = _pipeline_values(fields)
    pipeline_id = _uuid(pipeline_id, "pipeline")
    _integer(version, 1, 2**31 - 1, "versión")
    with transaction.atomic(using=alias):
        lock_pipelines(alias, ctx.organization_id)
        pipeline = _locked(alias, pipeline_id)
        if pipeline.version != version:
            raise StaleVersion(pipeline_id)
        changes = _diff(pipeline, values)
        active = values.get("is_active", pipeline.is_active)
        default = values.get("is_default", pipeline.is_default)
        if pipeline.is_default and not (default and active):
            raise PipelineIsDefault(pipeline_id)
        if default and not active:
            raise ValueError("un pipeline inactivo no puede ser el predeterminado")
        if "is_active" in changes and not active:
            _no_open_opportunities(alias, pipeline.pk)
        if default and not pipeline.is_default:
            _drop_default(ctx, alias)
        if changes:
            _save(ctx, alias, pipeline, changes)
            record(ctx, "pipeline.updated", Entity("pipeline", pipeline.pk, pipeline.code), changes)
    return pipeline


def add_stage(
    ctx: TenantContext, *, pipeline_id: UUID, position: int | None = None, **fields: Any
) -> PipelineStage:
    """Añade una etapa al pipeline y lo audita (`stage.added`). `position` es su lugar entre las
    demás (empezando en 0); sin él, una OPEN va tras la última OPEN y otra, al final. Las demás se
    renumeran. `StageInvariant` si el pipeline dejaría de cumplir sus invariantes (p. ej., una
    segunda WON), `CodeTaken` si su código ya está en el pipeline, `ValueError`, `DoesNotExist`."""
    alias = require_scope(ctx)
    values = _new_stage(fields)
    pipeline_id = _uuid(pipeline_id, "pipeline")
    if position is not None:
        _integer(position, 0, 32767, "posición")
    try:
        with transaction.atomic(using=alias):
            pipeline = _locked(alias, pipeline_id)
            stages = _stages(alias, pipeline.pk)
            _check(
                [
                    *((s.stage_type, s.is_active) for s in stages),
                    (values["stage_type"], values["is_active"]),
                ]
            )
            if position is None:
                opened = [i for i, s in enumerate(stages) if s.stage_type == "OPEN"]
                position = (
                    opened[-1] + 1 if opened and values["stage_type"] == "OPEN" else len(stages)
                )
            if not 0 <= position <= len(stages):
                raise ValueError(f"posición de 0 a {len(stages)}")
            stage: PipelineStage = PipelineStage.objects.using(alias).create(
                pipeline=pipeline, sort_order=position, **values
            )
            _renumber(alias, [*stages[:position], stage, *stages[position:]])
            changes = {k: [None, v] for k, v in {**values, "sort_order": position}.items()}
            changes["pipeline_id"] = [None, str(pipeline.pk)]
            record(ctx, "stage.added", Entity("stage", stage.pk, stage.code), changes)
    except IntegrityError as error:
        raise _taken(error, "pipeline_stages_code_uq") from None
    return stage


def update_stage(ctx: TenantContext, *, stage_id: UUID, **fields: Any) -> PipelineStage:
    """Cambia lo que se envía de una etapa y lo audita con el antes y el después (`stage.updated`);
    sin cambios no escribe. La probabilidad de una WON es 100 y la de una LOST, 0: cambiar el tipo
    pide la que le corresponde. `StageInvariant` si el cambio dejaría al pipeline sin su WON, sin
    una LOST o sin una OPEN; desactivar o sacar de OPEN pasa por el gancho de las oportunidades.
    El código y el orden no cambian aquí. `DoesNotExist` si no es de la organización."""
    alias = require_scope(ctx)
    if unknown := set(fields) - (STAGE_FIELDS | {"is_active"}):
        raise ValueError(f"campos no admitidos: {sorted(unknown)}")
    values = _stage_values(fields)
    stage_id = _uuid(stage_id, "etapa")
    with transaction.atomic(using=alias):
        owner = PipelineStage.objects.using(alias).values_list("pipeline_id", flat=True)
        pipeline = _locked(alias, owner.get(pk=stage_id))
        stages = _stages(alias, pipeline.pk)
        stage = next(s for s in stages if s.pk == stage_id)
        changes = _diff(stage, values)
        if changes:
            for field, (_, value) in changes.items():
                setattr(stage, field, value)
            _closed_probability(stage.stage_type, stage.default_probability)
            _check((s.stage_type, s.is_active) for s in stages)
            leaves_open = "stage_type" in changes and changes["stage_type"][0] == "OPEN"
            if ("is_active" in changes and not stage.is_active) or leaves_open:
                _no_open_opportunities(alias, pipeline.pk, stage.pk)
            stage.save(using=alias, update_fields=[*changes, "updated_at"])
            record(ctx, "stage.updated", Entity("stage", stage.pk, stage.code), changes)
    return stage


def reorder_stages(
    ctx: TenantContext, *, pipeline_id: UUID, stage_ids: Sequence[UUID]
) -> list[PipelineStage]:
    """Deja las etapas del pipeline en el orden de `stage_ids`, que las lleva todas (también las
    inactivas) una vez cada una, y lo audita (`stage.reordered`, con los códigos antes y después);
    si ya están en ese orden, no escribe. `ValueError` si la lista no es esa; `DoesNotExist`."""
    alias = require_scope(ctx)
    if not isinstance(stage_ids, list | tuple):
        raise ValueError("las etapas, una lista de identificadores")
    wanted = [_uuid(i, "etapa") for i in stage_ids]
    pipeline_id = _uuid(pipeline_id, "pipeline")
    with transaction.atomic(using=alias):
        pipeline = _locked(alias, pipeline_id)
        stages = _stages(alias, pipeline.pk)
        by_id = {s.pk: s for s in stages}
        if len(wanted) != len(set(wanted)) or set(wanted) != set(by_id):
            raise ValueError("el orden lleva todas las etapas del pipeline, una vez cada una")
        ordered = [by_id[i] for i in wanted]
        if ordered != stages:
            _renumber(alias, ordered)
            changes = {"order": [[s.code for s in stages], [s.code for s in ordered]]}
            record(ctx, "stage.reordered", Entity("pipeline", pipeline.pk, pipeline.code), changes)
    return ordered


def create_lost_reason(
    ctx: TenantContext, *, name: str, code: str, sort_order: int | None = None
) -> LostReason:
    """Crea un motivo de pérdida, activo, y lo audita. Sin `sort_order`, va al final. `CodeTaken`
    si ya hay uno vivo con ese código (`MAYÚSCULAS_Y_GUION_BAJO`); sin escribir nada."""
    alias = require_scope(ctx)
    values: dict[str, Any] = {
        "name": clean_name(_text(name, "nombre"), NAME_MAX),
        "code": _code(_text(code, "código"), REASON_CODE, str.upper),
    }
    if sort_order is not None:
        _integer(sort_order, -ORDER_MAX - 1, ORDER_MAX, "orden")
        values["sort_order"] = sort_order
    try:
        with transaction.atomic(using=alias):
            if sort_order is None:
                last = LostReason.objects.using(alias).aggregate(last=Max("sort_order"))["last"]
                values["sort_order"] = 0 if last is None else last + 1
                if values["sort_order"] > ORDER_MAX:
                    raise ValueError(f"orden: el último ya es {ORDER_MAX}; indica uno")
            reason: LostReason = LostReason.objects.using(alias).create(
                **values, created_by_user_id=ctx.user_id, updated_by_user_id=ctx.user_id
            )
            changes = {k: [None, v] for k, v in values.items()}
            record(
                ctx, "lost_reason.created", Entity("lost_reason", reason.pk, reason.code), changes
            )
    except IntegrityError as error:
        raise _taken(error, "lost_reasons_code_uq") from None
    return reason


def update_lost_reason(
    ctx: TenantContext, *, lost_reason_id: UUID, version: int, **fields: Any
) -> LostReason:
    """Cambia el nombre, el orden o si está activo, si nadie lo cambió desde `version`, y lo
    audita con el antes y el después; sin cambios no escribe. `StaleVersion`; `DoesNotExist` si no
    es de la organización o está borrado. El código no cambia."""
    alias = require_scope(ctx)
    _integer(version, 1, 2**31 - 1, "versión")
    if unknown := set(fields) - _REASON_FIELDS:
        raise ValueError(f"campos no admitidos: {sorted(unknown)}")
    values = dict(fields)
    if "name" in values:
        values["name"] = clean_name(_text(values["name"], "nombre"), NAME_MAX)
    if "is_active" in values:
        _flag(values["is_active"], "is_active")
    if "sort_order" in values:
        _integer(values["sort_order"], -ORDER_MAX - 1, ORDER_MAX, "orden")
    with transaction.atomic(using=alias):
        live = LostReason.objects.using(alias).filter(deleted_at__isnull=True)
        reason: LostReason = live.select_for_update(no_key=True).get(pk=lost_reason_id)
        if reason.version != version:
            raise StaleVersion(lost_reason_id)
        if changes := _diff(reason, values):
            _save(ctx, alias, reason, changes)
            record(
                ctx, "lost_reason.updated", Entity("lost_reason", reason.pk, reason.code), changes
            )
    return reason

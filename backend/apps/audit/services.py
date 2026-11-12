"""API pública de auditoría (ADR-011): `record()` en la transacción del `tenant_scope` activo,
siempre redactada y solo INSERT (docs/architecture/outbox-audit.md).
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from uuid import UUID

from django.core.serializers.json import DjangoJSONEncoder
from django.db import connections

from core.ids import new_id
from core.outbox import NAME, TYPE
from core.redaction import redact, redact_text
from core.tenancy.context import ActorType, TenantContext
from core.tenancy.scope import require_scope

LABEL_MAX = 200
_INSERT = (
    "INSERT INTO audit_logs (id, organization_id, occurred_at, actor_type, actor_id, "
    "actor_label, action, entity_type, entity_id, entity_label, changes, metadata, "
    "correlation_id, result) VALUES (%s, app_current_tenant(), now(), %s, %s, %s, %s, %s, %s, "
    "%s, %s::jsonb, %s::jsonb, %s, %s)"
)


class Result(StrEnum):
    SUCCESS = "SUCCESS"
    DENIED = "DENIED"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class Entity:
    type: str  # "quote", "contact"…
    id: UUID | None = None
    label: str | None = None  # etiqueta humana congelada (p. ej., "COT-000123")


def _json(value: Mapping[str, Any]) -> str:
    return json.dumps(redact(value), cls=DjangoJSONEncoder)


def record(
    ctx: TenantContext,
    action: str,
    entity: Entity,
    changes: Mapping[str, Any] | None = None,
    metadata: Mapping[str, Any] | None = None,
    *,
    result: Result = Result.SUCCESS,
    actor_label: str | None = None,
    actor: tuple[str, UUID | None] | None = None,  # solo (SYSTEM, None): el sistema que actúa
) -> UUID:
    """`changes = {"campo": [antes, después]}`; `action = "modulo.accion"` (p. ej., quote.sent)."""
    alias = require_scope(ctx)
    if not NAME.fullmatch(action):
        raise ValueError(f"action inválida: {action!r}")
    if not TYPE.fullmatch(entity.type):
        raise ValueError(f"entity.type inválido: {entity.type!r}")
    changes = dict(changes or {})
    if any(not isinstance(v, list | tuple) or len(v) != 2 for v in changes.values()):
        raise ValueError("changes: cada campo debe ser [antes, después]")
    if actor is not None and actor != (ActorType.SYSTEM, None):
        raise ValueError("actor: solo (SYSTEM, None); una persona la pone el contexto")
    audit_id = new_id()
    actor_type, actor_id = actor if actor is not None else (ctx.actor_type, ctx.actor_id)
    with connections[alias].cursor() as cursor:
        cursor.execute(
            _INSERT,
            [
                audit_id,
                actor_type,
                actor_id,
                redact_text(actor_label)[:LABEL_MAX] if actor_label else None,
                action,
                entity.type,
                entity.id,
                redact_text(entity.label)[:LABEL_MAX] if entity.label else None,
                _json(changes),
                _json(metadata or {}),
                ctx.correlation_id,
                Result(result),
            ],
        )
    return audit_id

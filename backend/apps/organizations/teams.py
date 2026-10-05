"""Comandos de equipos (F2-53 y F2-54, E01-09): crear uno y cambiar sus datos.

Como los de sucursales, no comprueban permisos (`organizations` no importa `access`) y por eso
no son API pública: solo los importa la API del módulo, que declara `teams.manage` (contrato
de import-linter). Validan lo que guardan, también para quien no llega por HTTP, y auditan
cada cambio.
"""

import re
from collections.abc import Callable
from functools import partial
from typing import Any
from uuid import UUID

from django.db import IntegrityError, transaction

from apps.audit.services import Entity, record
from apps.organizations.models import Team
from apps.organizations.text import line, name
from core.tenancy.context import TenantContext
from core.tenancy.scope import require_scope

SLUG = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")  # la misma forma que impone la tabla
SLUG_MAX, NAME_MAX, DESCRIPTION_MAX = 50, 100, 255


class TeamSlugTaken(Exception):
    """Ya hay un equipo con ese `slug` en la organización."""


def team_slug(value: str) -> str:
    """El `slug` como se guarda: en minúsculas. Solo ASCII: `lower()` convertiría otras letras
    en letras ASCII (el signo kelvin en «k») y dos `slug` distintos acabarían iguales."""
    slug = value.strip().lower() if value.isascii() else ""
    if not (len(slug) <= SLUG_MAX and SLUG.fullmatch(slug)):
        raise ValueError(f"letras sin acento, cifras y guiones entre ellas, hasta {SLUG_MAX}")
    return slug


def team_strategy(value: str) -> str:
    if value not in Team.Strategy.values:
        raise ValueError("estrategia de asignación desconocida")
    return value


CLEAN: dict[str, Callable[[str], str]] = {
    "slug": team_slug,
    "name": partial(name, limit=NAME_MAX),
    "description": partial(line, limit=DESCRIPTION_MAX),
    "assignment_strategy": team_strategy,
}
CREATE = frozenset(CLEAN)
UPDATE = CREATE - {"slug"} | {"is_active"}  # el `slug` no cambia: es la clave del equipo


def cleaned(fields: dict[str, Any], allowed: frozenset[str] = CREATE) -> dict[str, Any]:
    """Cada campo como se guarda. `ValueError` con el nombre del primero que no sirve."""
    if unknown := set(fields) - allowed:
        raise ValueError(f"campos no admitidos: {sorted(unknown)}")
    result: dict[str, Any] = {}
    for field, value in fields.items():
        try:
            if field == "is_active":
                if not isinstance(value, bool):
                    raise ValueError("tiene que ser verdadero o falso")
                result[field] = value
                continue
            if not isinstance(value, str):
                raise ValueError("tiene que ser texto")
            result[field] = CLEAN[field](value)
        except ValueError as error:
            raise ValueError(f"{field}: {error}") from None
    return result


def create_team(ctx: TenantContext, **fields: Any) -> Team:
    """Crea un equipo activo en la organización de `ctx` y lo audita. `slug` y `name` son
    obligatorios. Lanza `TeamSlugTaken` si el `slug` ya existe, sin escribir nada."""
    alias = require_scope(ctx)
    if missing := {"slug", "name"} - set(fields):
        raise ValueError(f"campos obligatorios: {sorted(missing)}")
    values = {"assignment_strategy": str(Team.Strategy.MANUAL), **cleaned(fields)}
    try:
        with transaction.atomic(using=alias):  # savepoint: el equipo y su auditoría, o nada
            team: Team = Team.objects.using(alias).create(**values)
            changes = {field: [None, value] for field, value in values.items() if value != ""}
            record(ctx, "team.created", Entity("team", team.pk, team.slug), changes)
    except IntegrityError as error:
        if "teams_org_slug_uq" not in str(error):
            raise
        raise TeamSlugTaken(values["slug"]) from None
    return team


def update_team(ctx: TenantContext, *, team_id: UUID, **fields: Any) -> Team:
    """Cambia lo que se envía de un equipo de la organización de `ctx` y lo audita con el antes
    y el después. Si nada cambia, no escribe ni audita. Un equipo de otra organización no
    existe: `DoesNotExist`."""
    alias = require_scope(ctx)
    values = cleaned(fields, UPDATE)
    with transaction.atomic(using=alias):  # savepoint: el cambio y su auditoría, o nada
        rows = Team.objects.using(alias).select_for_update(no_key=True)
        team: Team = rows.get(pk=team_id)
        changes = {
            field: [getattr(team, field), value]
            for field, value in values.items()
            if getattr(team, field) != value
        }
        if changes:
            for field, (_, value) in changes.items():
                setattr(team, field, value)
            team.save(using=alias, update_fields=[*changes, "updated_at"])
            record(ctx, "team.updated", Entity("team", team.pk, team.slug), changes)
    return team

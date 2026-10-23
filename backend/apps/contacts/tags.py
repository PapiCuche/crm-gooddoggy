"""Comandos de etiquetas (F4-11, E05-03; modelo de datos §E.5): el catálogo de la organización.

No comprueban permisos: los declara la API (`settings.manage`), la única que los importa
(contrato de import-linter). Validan lo que guardan y auditan cada cambio.
"""

import re
from collections.abc import Callable
from typing import Any
from uuid import UUID

from django.db import IntegrityError, transaction

from apps.audit.services import Entity, record
from apps.contacts.models import TAG_COLOR, Tag
from core.tenancy.context import TenantContext
from core.tenancy.scope import require_scope
from core.text import name as clean_name

NAME_MAX = 50
TAKEN_CONSTRAINT = "tags_org_target_name_uq"
_COLOR = re.compile(TAG_COLOR)


class TagNameTaken(Exception):
    """Ya hay una etiqueta con ese nombre (sin distinguir mayúsculas), activa o no."""


def tag_name(value: str) -> str:
    return clean_name(value, NAME_MAX)


def tag_color(value: str) -> str:
    """`#rrggbb`, guardado en minúsculas."""
    color = value.strip().lower()
    if not _COLOR.fullmatch(color):
        raise ValueError("un color #RRGGBB, como #1f9d55")
    return color


def _entity(tag: Tag) -> Entity:
    return Entity("tag", tag.pk, tag.name)


def _taken(error: IntegrityError, name: str) -> Exception:
    if TAKEN_CONSTRAINT not in str(error):
        raise error
    return TagNameTaken(name)


def create_tag(ctx: TenantContext, *, name: str, color: str) -> Tag:
    """Crea una etiqueta de contactos, activa, y lo audita. `TagNameTaken` sin escribir nada."""
    alias = require_scope(ctx)
    values = {"name": tag_name(name), "color": tag_color(color)}
    try:
        with transaction.atomic(using=alias):
            tag: Tag = Tag.objects.using(alias).create(**values, applies_to=Tag.Target.CONTACT)
            changes = {field: [None, value] for field, value in values.items()}
            record(ctx, "tag.created", _entity(tag), changes)
    except IntegrityError as error:
        raise _taken(error, values["name"]) from None
    return tag


def update_tag(ctx: TenantContext, *, tag_id: UUID, **fields: Any) -> Tag:
    """Cambia el nombre, el color o si está activa, y lo audita con el antes y el después; si
    nada cambia, no escribe. `DoesNotExist` si no es de la organización; `TagNameTaken`."""
    alias = require_scope(ctx)
    cleaners: dict[str, Callable[[Any], Any]] = {
        "name": tag_name,
        "color": tag_color,
        "is_active": bool,
    }
    if unknown := set(fields) - set(cleaners):
        raise ValueError(f"campos no admitidos: {sorted(unknown)}")
    values = {field: cleaners[field](value) for field, value in fields.items()}
    try:
        with transaction.atomic(using=alias):
            tag: Tag = Tag.objects.using(alias).select_for_update(no_key=True).get(pk=tag_id)
            changes = {
                field: [getattr(tag, field), value]
                for field, value in values.items()
                if getattr(tag, field) != value
            }
            if changes:
                for field in changes:
                    setattr(tag, field, values[field])
                tag.save(using=alias, update_fields=[*changes, "updated_at"])
                record(ctx, "tag.updated", _entity(tag), changes)
    except IntegrityError as error:
        raise _taken(error, values.get("name", "")) from None
    return tag

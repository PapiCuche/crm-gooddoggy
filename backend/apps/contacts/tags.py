"""Comandos de etiquetas (F4-11, E05-03; modelo de datos §E.5): el catálogo de la organización
y ponerlas o quitarlas de un contacto.

No comprueban permisos: los declara la API (`settings.manage` para el catálogo; `contacts.view`
y `contacts.update` en alcance para etiquetar), la única que los importa (contrato de
import-linter). Validan lo que guardan y auditan cada cambio.
"""

import re
from collections.abc import Callable
from typing import Any
from uuid import UUID

from django.db import IntegrityError, transaction

from apps.audit.services import Entity, record
from apps.contacts.models import TAG_COLOR, Contact, ContactTag, Tag
from core.tenancy.context import TenantContext
from core.tenancy.scope import require_scope
from core.text import name as clean_name

NAME_MAX = 50
TAKEN_CONSTRAINT = "tags_org_target_name_uq"
_COLOR = re.compile(TAG_COLOR)


class TagNameTaken(Exception):
    """Ya hay una etiqueta con ese nombre (sin distinguir mayúsculas), activa o no."""


class TagInactive(Exception):
    """Una etiqueta desactivada no se pone en un contacto más."""


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


def contact_tag(alias: str, tag_id: UUID) -> Tag:
    """Una etiqueta de contactos de la organización, activa o no. `DoesNotExist` si no."""
    found: Tag = Tag.objects.using(alias).get(pk=tag_id, applies_to=Tag.Target.CONTACT)
    return found


def tag_contact(ctx: TenantContext, *, contact: Contact, tag_id: UUID) -> bool:
    """Pone la etiqueta en el contacto y lo audita (`contact.tagged`); si ya la lleva, no hace
    nada, aunque la etiqueta esté desactivada. Devuelve si la puso. `DoesNotExist` si la
    etiqueta no es de contactos de la organización; `TagInactive` si está desactivada.

    Quien llama tiene bloqueado el contacto (la API, al decidir el alcance): dos peticiones a la
    vez sobre el mismo contacto se ordenan, y la segunda ya ve la etiqueta puesta. Una etiqueta
    desactivada mientras se ponía queda como si se hubiera puesto justo antes: sigue en el
    contacto, como las demás que ya la llevaban."""
    alias = require_scope(ctx)
    tag = contact_tag(alias, tag_id)
    links = ContactTag.objects.using(alias)
    if links.filter(contact_id=contact.pk, tag_id=tag.pk).exists():
        return False
    if not tag.is_active:
        raise TagInactive(tag.pk)
    with transaction.atomic(using=alias):
        links.create(contact_id=contact.pk, tag_id=tag.pk, added_by_user_id=ctx.user_id)
        changes = {"tag_id": [None, str(tag.pk)]}
        record(ctx, "contact.tagged", Entity("contact", contact.pk), changes, {"tag": tag.name})
    return True


def untag_contact(ctx: TenantContext, *, contact: Contact, tag_id: UUID) -> bool:
    """Quita la etiqueta del contacto, activa o no, y lo audita (`contact.untagged`); si no la
    lleva, no hace nada. Devuelve si la quitó. `DoesNotExist` si la etiqueta no es de contactos
    de la organización. Como `tag_contact`, con el contacto bloqueado por quien llama."""
    alias = require_scope(ctx)
    tag = contact_tag(alias, tag_id)
    with transaction.atomic(using=alias):
        removed, _ = (
            ContactTag.objects.using(alias).filter(contact_id=contact.pk, tag_id=tag.pk).delete()
        )
        if removed:
            changes = {"tag_id": [str(tag.pk), None]}
            record(
                ctx, "contact.untagged", Entity("contact", contact.pk), changes, {"tag": tag.name}
            )
    return bool(removed)

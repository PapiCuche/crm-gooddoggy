"""Comandos de las notas de un cliente (F4-09). Sin permisos (los decide la API, su importador) y
auditados sin el cuerpo, que puede ser dato personal (D-F4-04). Bloquean el contacto vivo (`FOR
NO KEY UPDATE`, como su `PATCH`) antes que la nota: ninguna entra en un contacto que se borra."""

import unicodedata
from typing import Any
from uuid import UUID

from django.db import transaction
from django.db.models import F
from django.utils import timezone

from apps.audit.services import Entity, record
from apps.contacts.models import Contact, Note
from core.tenancy.context import TenantContext
from core.tenancy.scope import require_scope

BODY_MAX = 5000
_BIDI = set(map(chr, [*range(0x202A, 0x202F), *range(0x2066, 0x206A)]))  # reordenan lo que se ve


class StaleNote(Exception):
    """Otra escritura cambió la nota después de la versión que se editaba."""


def clean_body(value: str) -> str:
    """Texto (no HTML) en NFC, sin espacios exteriores; todo salto de línea pasa a `\\n`."""
    text = unicodedata.normalize("NFC", "\n".join(value.splitlines())).strip()
    if any(unicodedata.category(c) == "Cc" and c not in "\n\t" or c in _BIDI for c in text):
        raise ValueError("solo texto, sin caracteres de control")
    if not text or len(text) > BODY_MAX:
        raise ValueError(f"entre 1 y {BODY_MAX} caracteres")
    return text


def _locked(alias: str, contact_id: UUID, note_id: UUID | None = None) -> Any:
    live = {"deleted_at__isnull": True}
    Contact.objects.using(alias).filter(**live).select_for_update(no_key=True).get(pk=contact_id)
    if note_id is None:
        return None
    found = Note.objects.using(alias).filter(contact_id=contact_id, **live)
    return found.select_for_update(no_key=True).get(pk=note_id)


def create_note(ctx: TenantContext, *, contact_id: UUID, body: str, is_pinned: bool) -> Note:
    """Añade una nota de quien actúa a un contacto vivo y la audita."""
    alias, text = require_scope(ctx), clean_body(body)
    with transaction.atomic(using=alias):
        _locked(alias, contact_id)
        note: Note = Note.objects.using(alias).create(
            contact_id=contact_id, body=text, is_pinned=is_pinned, author_user_id=ctx.user_id
        )
        changes, about = {"is_pinned": [None, is_pinned]}, {"contact_id": str(contact_id)}
        record(ctx, "note.created", Entity("note", note.pk), changes, about)
    return note


def update_note(
    ctx: TenantContext, *, contact_id: UUID, note_id: UUID, version: int, **new: Any
) -> Note:
    """Cambia `body` o `is_pinned` si la `version` sigue vigente (`StaleNote` si no); sin cambios
    no escribe. Del cuerpo, la auditoría solo dice que cambió."""
    if set(new) - {"body", "is_pinned"}:
        raise ValueError("campos no admitidos")
    alias, is_pinned = require_scope(ctx), new.get("is_pinned")
    text = None if new.get("body") is None else clean_body(new["body"])
    with transaction.atomic(using=alias):
        note: Note = _locked(alias, contact_id, note_id)
        if note.version != version:
            raise StaleNote(note_id)
        changes, about = {}, dict[str, Any](contact_id=str(contact_id))
        if is_pinned is not None and is_pinned != note.is_pinned:
            changes["is_pinned"], note.is_pinned = [note.is_pinned, is_pinned], is_pinned
        if text is not None and text != note.body:
            about["body_changed"], note.body = True, text
        if len(changes) + len(about) > 1:
            note.updated_by_user_id, note.version = ctx.user_id, F("version") + 1
            fields = ["body", "is_pinned", "updated_by_user_id", "version", "updated_at"]
            note.save(using=alias, update_fields=fields)
            note.refresh_from_db(using=alias, fields=["version"])
            record(ctx, "note.updated", Entity("note", note.pk), changes, about)
    return note


def delete_note(ctx: TenantContext, *, contact_id: UUID, note_id: UUID) -> None:
    """Borrado lógico de una nota viva, auditado."""
    alias = require_scope(ctx)
    with transaction.atomic(using=alias):
        note: Note = _locked(alias, contact_id, note_id)
        note.deleted_at, note.deleted_by_user_id = timezone.now(), ctx.user_id
        note.version = F("version") + 1
        fields = ["deleted_at", "deleted_by_user_id", "version", "updated_at"]
        note.save(using=alias, update_fields=fields)
        record(ctx, "note.deleted", Entity("note", note.pk), None, {"contact_id": str(contact_id)})

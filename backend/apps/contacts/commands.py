"""Comandos de contactos (F4-01; E05-01, modelo de datos §E.5).

No comprueban permisos: los declarará la API (F4-02, F4-03), la única que los importa (contrato
de import-linter). Validan lo que guardan y auditan cada cambio. Datos personales: ningún error
repite el valor recibido, y la auditoría guarda el documento enmascarado (04 §N.1).
"""

import re
from typing import Any
from uuid import UUID

from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone

from apps.audit.services import Entity, record
from apps.contacts.models import PHONE_E164, SEPARATORS, Contact, dialed
from core.tenancy.context import TenantContext
from core.tenancy.scope import require_scope
from core.text import line
from core.text import name as clean_name

NAME_MAX, PART_MAX, EMAIL_MAX = 200, 100, 254
_PHONE = re.compile(PHONE_E164)
DOCUMENTS = {  # la forma de cada tipo, ya en mayúsculas
    Contact.DocumentType.DNI: re.compile(r"[0-9]{8}"),
    Contact.DocumentType.RUC: re.compile(r"[0-9]{11}"),
    Contact.DocumentType.CE: re.compile(r"[A-Z0-9]{8,12}"),
    Contact.DocumentType.PASSPORT: re.compile(r"[A-Z0-9]{6,12}"),
}
_REFERENCES = {  # FK compuesta violada → el campo que la causó
    "contacts_owner_fk": "owner_user_id",
    "contacts_branch_fk": "branch_id",
    "contacts_created_by_fk": "created_by_user_id",
}
FIELDS = frozenset(
    {
        "display_name",
        "first_name",
        "last_name",
        "primary_phone_e164",
        "primary_email",
        "document_type",
        "document_number",
        "source",
    }
)
_DOCUMENT = frozenset({"document_type", "document_number"})


class BadReference(ValueError):
    """El responsable no es miembro de la organización, o la sucursal no es suya."""

    def __init__(self, field: str) -> None:
        super().__init__(field)
        self.field = field


class StaleContact(Exception):
    """Otra escritura cambió el contacto después de la versión que se editaba."""


def phone(value: str | None) -> str | None:
    """El teléfono en E.164 (`+51987654321`), sin separadores. No adivina el país: sin `+` (o
    `00`) delante, se rechaza. Vacío es sin teléfono."""
    if value is None or not (digits := dialed(value)):
        return None
    if not _PHONE.fullmatch(digits):  # `[0-9]`: solo cifras ASCII
        raise ValueError("en formato internacional: + y el código del país, hasta 15 cifras")
    return digits


def email(value: str | None) -> str | None:
    """El correo tal como se escribió, sin espacios exteriores. Vacío es sin correo."""
    if value is None or not (text := value.strip()):
        return None
    try:
        validate_email(text)
    except ValidationError:
        raise ValueError("correo no válido") from None
    if len(text) > EMAIL_MAX:
        raise ValueError(f"hasta {EMAIL_MAX} caracteres")
    return text


def document(kind: str | None, number: str | None) -> tuple[str | None, str | None]:
    """El tipo y el número, los dos o ninguno; el número en mayúsculas, sin espacios ni guiones,
    con la forma de su tipo."""
    raw = SEPARATORS.sub("", number or "")
    code = raw.upper() if raw.isascii() else "?"  # antes de `upper()`: «ﬁ» pasaría a «FI»
    if not kind and not code:
        return None, None
    if not kind or not code:
        raise ValueError("el tipo y el número del documento van juntos")
    if kind not in DOCUMENTS:
        raise ValueError("tipo de documento no admitido")
    if not DOCUMENTS[Contact.DocumentType(kind)].fullmatch(code):
        raise ValueError(f"el número no tiene la forma de un {kind}")
    return kind, code


def masked(number: str | None) -> str | None:
    """El número de un documento como lo guarda la auditoría: solo sus 3 últimos caracteres."""
    if number is None:
        return None
    return "*" * max(len(number) - 3, 0) + number[-3:]


def _clean(values: dict[str, Any]) -> dict[str, Any]:
    clean = dict(values)
    for part in ("first_name", "last_name"):
        if part in clean:
            clean[part] = line(clean[part] or "", PART_MAX)
    if "display_name" in clean:
        clean["display_name"] = clean_name(clean["display_name"] or "", NAME_MAX)
    if "primary_phone_e164" in clean:
        clean["primary_phone_e164"] = phone(clean["primary_phone_e164"])
    if "primary_email" in clean:
        clean["primary_email"] = email(clean["primary_email"])
    if "source" in clean and clean["source"] not in Contact.Source.values:
        raise ValueError("origen no admitido")
    return clean


def _audited(field: str, value: Any) -> Any:
    if field == "document_number":
        return masked(value)
    return str(value) if isinstance(value, UUID) else value


def _reference(error: IntegrityError) -> Exception:
    text = str(error)
    for constraint, field in _REFERENCES.items():
        if constraint in text:
            return BadReference(field)
    raise error


def _live(alias: str) -> Any:
    return Contact.objects.using(alias).filter(deleted_at__isnull=True)


def create_contact(
    ctx: TenantContext,
    *,
    display_name: str | None = None,
    first_name: str = "",
    last_name: str = "",
    primary_phone_e164: str | None = None,
    primary_email: str | None = None,
    document_type: str | None = None,
    document_number: str | None = None,
    source: str = Contact.Source.MANUAL,
    owner_user_id: UUID | None = None,
    branch_id: UUID | None = None,
) -> Contact:
    """Crea un contacto y lo audita. Sin `display_name`, se forma con el nombre y el apellido.
    `ValueError` si un dato no vale y `BadReference` si el responsable o la sucursal no son de
    la organización, sin escribir nada. Quien lo crea queda en `created_by_user_id`."""
    alias = require_scope(ctx)
    values = _clean(
        {
            "first_name": first_name,
            "last_name": last_name,
            "primary_phone_e164": primary_phone_e164,
            "primary_email": primary_email,
            "source": source,
        }
    )
    composed = " ".join(p for p in (values["first_name"], values["last_name"]) if p)
    values["display_name"] = clean_name(display_name or composed, NAME_MAX)
    values["document_type"], values["document_number"] = document(document_type, document_number)
    try:
        with transaction.atomic(using=alias):
            contact: Contact = Contact.objects.using(alias).create(
                **values,
                owner_user_id=owner_user_id,
                branch_id=branch_id,
                created_by_user_id=ctx.user_id,
            )
            changes = {
                field: [None, _audited(field, value)]
                for field, value in {
                    **values,
                    "owner_user_id": owner_user_id,
                    "branch_id": branch_id,
                }.items()
                if value not in (None, "")
            }
            record(ctx, "contact.created", Entity("contact", contact.pk), changes)
    except IntegrityError as error:
        raise _reference(error) from None
    return contact


def update_contact(ctx: TenantContext, *, contact_id: UUID, version: int, **fields: Any) -> Contact:
    """Cambia lo que se envía de un contacto vivo, si nadie lo cambió desde `version`, y lo
    audita con el antes y el después; si nada cambia, no escribe. El tipo y el número del
    documento se validan juntos. `DoesNotExist` si no es de la organización o está borrado;
    `StaleContact` si la versión no es la vigente. El responsable y la sucursal no cambian aquí
    (`contacts.assign`, F4-08)."""
    alias = require_scope(ctx)
    if unknown := set(fields) - FIELDS:
        raise ValueError(f"campos no admitidos: {sorted(unknown)}")
    values = _clean({k: v for k, v in fields.items() if k not in _DOCUMENT})
    with transaction.atomic(using=alias):
        contact: Contact = _live(alias).select_for_update(no_key=True).get(pk=contact_id)
        if contact.version != version:
            raise StaleContact(contact_id)
        if _DOCUMENT & set(fields):
            values["document_type"], values["document_number"] = document(
                fields.get("document_type", contact.document_type),
                fields.get("document_number", contact.document_number),
            )
        changes = {
            field: [_audited(field, getattr(contact, field)), _audited(field, value)]
            for field, value in values.items()
            if getattr(contact, field) != value
        }
        if changes:
            for field in changes:
                setattr(contact, field, values[field])
            contact.updated_by_user_id = ctx.user_id
            contact.version = F("version") + 1
            updated = [*changes, "updated_by_user_id", "version", "updated_at"]
            contact.save(using=alias, update_fields=updated)
            contact.refresh_from_db(using=alias, fields=["version"])
            record(ctx, "contact.updated", Entity("contact", contact.pk), changes)
    return contact


def delete_contact(ctx: TenantContext, *, contact_id: UUID) -> None:
    """Borra un contacto (borrado lógico) y lo audita. `DoesNotExist` si no es de la
    organización o ya está borrado."""
    alias = require_scope(ctx)
    with transaction.atomic(using=alias):
        contact: Contact = _live(alias).select_for_update(no_key=True).get(pk=contact_id)
        contact.deleted_at = timezone.now()
        contact.deleted_by_user_id = ctx.user_id
        contact.version = F("version") + 1
        contact.save(
            using=alias, update_fields=["deleted_at", "deleted_by_user_id", "version", "updated_at"]
        )
        record(ctx, "contact.deleted", Entity("contact", contact.pk))

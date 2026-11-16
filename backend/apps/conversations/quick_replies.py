"""Comandos de respuestas rápidas y sus categorías, y el resolvedor de variables (F6-08a1, E06-10).

No comprueban permisos: los declarará la API (`quick_replies.manage` para gestionar; usar pide
`conversations.view` y `messages.send`), la única que los importa (contrato de import-linter).
Validan lo que guardan y auditan cada cambio sin el texto en claro: del cuerpo queda su huella.

Variables: una lista cerrada (`VARIABLES`). Guardar un cuerpo con otra variable, con `{{` o `}}`
que no formen una permitida o con llaves triples es un `Invalid` en `body`. `render` sustituye en
una sola pasada: lo que trae un valor (un contacto llamado `{{org.name}}`) nunca se interpreta.
Activar y desactivar es `update_*` con `is_active`; borrar es lógico y deja la fila inactiva.
"""

import hashlib
import re
from collections.abc import Mapping
from typing import Any
from uuid import UUID

from django.db import IntegrityError, transaction
from django.db.models import F, Max
from django.utils import timezone

from apps.audit.services import Entity, record
from apps.conversations import selectors
from apps.conversations.models import SHORTCUT, QuickReply, QuickReplyCategory
from apps.conversations.text import clean_body
from core.tenancy.context import TenantContext
from core.tenancy.scope import require_scope
from core.text import name as clean_name

VARIABLES = ("contact.first_name", "contact.name", "org.name")
# Posesivo y sin espacios en la clave: lineal, sin retroceso, aun con 4096 espacios tras `{{`.
TOKEN = re.compile(r"\{\{\s*+([^{}\s]*+)\s*+\}\}")
NAME_MAX, ORDER_MAX = 100, 32767
_CATEGORY_FIELDS = frozenset({"name", "sort_order", "is_active"})
_REPLY_FIELDS = frozenset({"shortcut", "title", "category_id", "body", "is_active"})
Row = QuickReply | QuickReplyCategory
Kind = type[QuickReply] | type[QuickReplyCategory]


class StaleVersion(Exception):
    """Otra escritura la cambió después de la versión que se editaba."""


class Taken(Exception):
    """Ya hay una viva con ese nombre (categoría) o una activa con ese atajo (respuesta)."""


class CategoryInUse(Exception):
    """La categoría tiene respuestas vivas: se mueven o se borran antes."""


class Invalid(ValueError):
    """Un dato que no sirve; `field` es el campo del cuerpo de la API (400)."""

    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field


def variables(body: str) -> list[str]:
    """Las variables del cuerpo, en orden; `Invalid` si una no es de la lista o si quedan llaves
    dobles sueltas o triples. El texto del error no repite lo recibido."""
    found = [match[1] for match in TOKEN.finditer(body)]
    if any(key not in VARIABLES for key in found):
        raise Invalid("body", f"variables admitidas: {', '.join(VARIABLES)}")
    rest = TOKEN.sub("", body)
    triple = "{{{" in body or "}}}" in body  # `{{{contact.name}}}` daría `{Ana}`
    if triple or "{{" in rest or "}}" in rest:
        raise Invalid("body", "llaves dobles solo para una variable, como {{contact.name}}")
    return found


def render(body: str, values: Mapping[str, str | None]) -> tuple[str, list[str]]:
    """El cuerpo con sus variables resueltas y las que faltaron (sin valor), sin repetir. Pura:
    los valores no se vuelven a leer como variables y una clave fuera de la lista queda vacía."""
    missing: list[str] = []

    def value(match: re.Match[str]) -> str:
        found = values.get(match[1]) if match[1] in VARIABLES else None
        if not found and match[1] not in missing:
            missing.append(match[1])
        return found or ""

    return TOKEN.sub(value, body), missing


def _flag(value: Any) -> bool:
    if not isinstance(value, bool):
        raise ValueError("verdadero o falso")
    return value


def _text(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("un texto")
    return value


def _order(value: Any) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not -ORDER_MAX - 1 <= value <= ORDER_MAX
    ):
        raise ValueError(f"un entero de {-ORDER_MAX - 1} a {ORDER_MAX}")
    return int(value)


def _shortcut(value: Any) -> str:
    text = _text(value).strip().lower()
    if not (text.isascii() and re.fullmatch(SHORTCUT, text)):
        raise ValueError("hasta 32 letras sin acento, cifras, _ o -, sin la /")
    return text


def _body_with_variables(value: Any) -> str:
    text = clean_body(value)
    variables(text)
    return text


def _id(value: Any) -> UUID:
    try:
        return value if isinstance(value, UUID) else UUID(_text(value))
    except ValueError:
        raise ValueError("un identificador") from None


RULES = {
    "name": lambda v: clean_name(_text(v), NAME_MAX),
    "title": lambda v: clean_name(_text(v), NAME_MAX),
    "sort_order": _order,
    "is_active": _flag,
    "shortcut": _shortcut,
    "body": _body_with_variables,
    "category_id": _id,
}


def _clean(fields: dict[str, Any]) -> dict[str, Any]:
    """Los campos enviados, limpios y validados; un error es un `Invalid` en su campo."""
    cleaned = {}
    for key, value in fields.items():
        try:
            cleaned[key] = RULES[key](value)
        except ValueError as error:
            raise Invalid(key, str(error)) from None
    return cleaned


def fingerprint(text: str) -> str:
    """Lo que la auditoría guarda de un cuerpo: los 16 primeros caracteres de su SHA-256."""
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()[:16]


def _audit(ctx: TenantContext, action: str, obj: Row, changes: dict[str, list[Any]]) -> None:
    if "body" in changes:  # la huella, nunca el texto (que la auditoría además tacharía)
        changes = {k: v for k, v in changes.items() if k != "body"} | {
            "body_fingerprint": [v and fingerprint(v) for v in changes["body"]]
        }
    if isinstance(obj, QuickReply):
        entity = Entity("quick_reply", obj.pk, obj.shortcut)
    else:
        entity = Entity("quick_reply_category", obj.pk, obj.name)
    record(ctx, f"{entity.type}.{action}", entity, changes)


def _category(alias: str, category_id: UUID) -> QuickReplyCategory:
    """La categoría viva, bloqueada contra un borrado a la vez (`FOR NO KEY UPDATE`)."""
    live = QuickReplyCategory.objects.using(alias).filter(deleted_at__isnull=True)
    found: QuickReplyCategory | None = (
        live.select_for_update(no_key=True).filter(pk=category_id).first()
    )
    if found is None:  # de otra organización (RLS), inexistente o borrada
        raise Invalid("category_id", "una categoría viva de la organización")
    return found


def _create(ctx: TenantContext, model: Kind, values: dict[str, Any]) -> Any:
    alias = require_scope(ctx)
    try:
        with transaction.atomic(using=alias):
            if model is QuickReply:
                _category(alias, values["category_id"])
            elif "sort_order" not in values:
                last = model.objects.using(alias).aggregate(last=Max("sort_order"))["last"]
                values["sort_order"] = 0 if last is None else min(last + 1, ORDER_MAX)
            stamps = {"created_by_user_id": ctx.user_id, "updated_by_user_id": ctx.user_id}
            made = model.objects.using(alias).create(**values, **stamps)
            _audit(ctx, "created", made, {k: [None, v] for k, v in values.items()})
    except IntegrityError as error:
        raise _taken(error) from None
    return made


def _write(ctx: TenantContext, model: Kind, pk: UUID, version: int | None,
           values: dict[str, Any], action: str = "updated") -> Any:  # fmt: skip
    """Cambia `values` de la fila viva `pk` (bloqueada) si sigue en `version` (`None`: sin
    comprobarla) y lo audita; sin cambios no escribe. `DoesNotExist` si no es de la organización."""
    alias = require_scope(ctx)
    try:
        with transaction.atomic(using=alias):
            live = model.objects.using(alias).filter(deleted_at__isnull=True)
            obj = live.select_for_update(no_key=True).get(pk=pk)
            if version is not None and obj.version != version:
                raise StaleVersion(pk)
            moved = values.get("category_id")
            if moved is not None and moved != getattr(obj, "category_id", None):
                _category(alias, moved)
            if model is QuickReplyCategory and action == "deleted":
                in_use = QuickReply.objects.using(alias).filter(category_id=pk)
                if in_use.filter(deleted_at__isnull=True).exists():
                    raise CategoryInUse(pk)
            changes = {k: [getattr(obj, k), v] for k, v in values.items() if getattr(obj, k) != v}
            if changes:
                for key, (_, value) in changes.items():
                    setattr(obj, key, value)
                obj.updated_by_user_id = ctx.user_id
                obj.version = F("version") + 1
                keys = [*changes, "updated_by_user_id", "version", "updated_at"]
                obj.save(using=alias, update_fields=keys)
                obj.refresh_from_db(using=alias, fields=["version"])
                _audit(ctx, action, obj, changes)
    except IntegrityError as error:
        raise _taken(error) from None
    return obj


def _taken(error: IntegrityError) -> Exception:
    if not any(c in str(error) for c in ("qr_categories_name_uq", "quick_replies_shortcut_uq")):
        raise error
    return Taken()


def _version(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value < 2**31:
        raise Invalid("version", "un entero positivo")
    return int(value)


def _fields(fields: dict[str, Any], allowed: frozenset[str]) -> dict[str, Any]:
    if unknown := set(fields) - allowed:  # nunca `organization_id`, ni nada del contexto
        raise ValueError(f"campos no admitidos: {sorted(unknown)}")
    return _clean(fields)


def create_category(ctx: TenantContext, *, name: str, sort_order: int | None = None) -> Any:
    """Crea una categoría activa, al final si no se indica el orden. `Taken` si el nombre ya es
    de una viva (sin distinguir mayúsculas)."""
    wanted: dict[str, Any] = {"name": name}
    if sort_order is not None:
        wanted["sort_order"] = sort_order
    return _create(ctx, QuickReplyCategory, _clean(wanted))


def update_category(ctx: TenantContext, *, category_id: UUID, version: int, **fields: Any) -> Any:
    """Cambia nombre, orden o si está activa. `StaleVersion`, `Taken`, `DoesNotExist`."""
    values = _fields(fields, _CATEGORY_FIELDS)
    return _write(ctx, QuickReplyCategory, category_id, _version(version), values)


def delete_category(ctx: TenantContext, *, category_id: UUID) -> Any:
    """Borrado lógico; `CategoryInUse` si tiene respuestas vivas, activas o no."""
    gone = {"deleted_at": timezone.now(), "deleted_by_user_id": ctx.user_id, "is_active": False}
    return _write(ctx, QuickReplyCategory, category_id, None, gone, "deleted")


def create_reply(ctx: TenantContext, *, shortcut: str, title: str, category_id: UUID,
                 body: str) -> Any:  # fmt: skip
    """Crea una respuesta activa en una categoría viva. `Taken` si el atajo ya es de una activa;
    `Invalid` con su campo (una variable fuera de la lista, en `body`)."""
    wanted = {"shortcut": shortcut, "title": title, "category_id": category_id, "body": body}
    return _create(ctx, QuickReply, _clean(wanted))


def update_reply(ctx: TenantContext, *, quick_reply_id: UUID, version: int, **fields: Any) -> Any:
    """Cambia atajo, título, categoría, cuerpo o si está activa (activar y desactivar). Activarla
    con un atajo que ya es de otra activa es `Taken`. `StaleVersion`, `DoesNotExist`."""
    values = _fields(fields, _REPLY_FIELDS)
    return _write(ctx, QuickReply, quick_reply_id, _version(version), values)


def delete_reply(ctx: TenantContext, *, quick_reply_id: UUID) -> Any:
    """Borrado lógico: deja de estar activa y su atajo queda libre."""
    gone = {"deleted_at": timezone.now(), "deleted_by_user_id": ctx.user_id, "is_active": False}
    return _write(ctx, QuickReply, quick_reply_id, None, gone, "deleted")


def use_reply(ctx: TenantContext, *, quick_reply_id: UUID) -> QuickReply:
    """Suma un uso (F6-08a2) a una respuesta que se puede usar (`selectors.quick_replies`), con
    `F()` y sin tocar su `version` ni auditar: es un contador. `DoesNotExist` si no se puede usar.
    No envía nada: el texto resuelto lo devuelve la API y el envío sigue siendo el de siempre."""
    alias = require_scope(ctx)
    usable = selectors.quick_replies().using(alias).filter(pk=quick_reply_id)
    if not usable.update(usage_count=F("usage_count") + 1):
        raise QuickReply.DoesNotExist(quick_reply_id)
    found: QuickReply = usable.get()
    return found

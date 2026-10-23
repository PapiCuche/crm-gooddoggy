"""Lecturas de contactos, por organización (RLS y `TenantManager`). El permiso y el alcance los
aplica quien las sirve (`HasPermission` y `ScopeFilter`, o `scoped()`)."""

import re
from collections.abc import Iterable
from typing import Any
from uuid import UUID

from django.db.models import Exists, F, OuterRef, Prefetch, Q, QuerySet, prefetch_related_objects
from django.db.models.functions import Lower
from django.db.models.lookups import BuiltinLookup

from apps.contacts.models import Contact, ContactTag, Note, Tag, dialed

_PHONE_QUERY = re.compile(r"\+[0-9]{1,15}")  # el comienzo de un E.164, solo cifras ASCII


class _Prefix(BuiltinLookup):  # type: ignore[type-arg]  # sin `__class_getitem__`
    """`^@` (`starts_with`). Con FORCE RLS, PostgreSQL solo lleva al índice las condiciones
    *leakproof*: `^@` lo es y `LIKE` no, así que el prefijo del teléfono usa su índice."""

    lookup_name = "prefix"  # sin el `::text` ni el `%` de `startswith`

    def get_rhs_op(self, connection: Any, rhs: str) -> str:
        return f"^@ {rhs}"


def contacts() -> QuerySet[Contact]:
    """Los contactos no borrados. El orden lo pone la paginación (ADR-016)."""
    return Contact.objects.filter(deleted_at__isnull=True)


def search(found: QuerySet[Contact], text: str) -> QuerySet[Contact]:
    """Los de `found` que casan con `text` (F4-07); vacío, todos. Solo filtra: el orden sigue
    siendo el de la paginación, y lo que `found` no tenía (otra organización, fuera del alcance)
    no aparece.

    - Con `+` (o `00`) y cifras, tras quitar los separadores que admite el teléfono: el teléfono
      empieza por ese prefijo E.164 (`+51 987` → `+51987…`).
    - Si no: el nombre visible contiene el texto o el correo empieza por él, sin distinguir
      mayúsculas; `%`, `_` y `\\` son literales. Unas cifras sin `+` son texto: no se adivina
      el país (D-F4-03).
    """
    if not text:
        return found
    if _PHONE_QUERY.fullmatch(number := dialed(text)):
        return found.filter(_Prefix(F("primary_phone_e164"), number))
    return found.filter(Q(display_name__icontains=text) | Q(primary_email__istartswith=text))


def notes(contact_id: UUID) -> QuerySet[Note]:  # ver el contacto lo decide quien las sirve
    return Note.objects.filter(contact_id=contact_id, deleted_at__isnull=True)


def tags() -> QuerySet[Tag]:
    """Las etiquetas de contactos de la organización, activas o no (F4-11)."""
    return Tag.objects.filter(applies_to=Tag.Target.CONTACT)


def _tag_links() -> Prefetch:  # type: ignore[type-arg]
    links = ContactTag.objects.select_related("tag").order_by(Lower("tag__name"), "tag_id")
    return Prefetch("tag_links", queryset=links)


def with_tags(found: QuerySet[Contact]) -> QuerySet[Contact]:
    """Con sus etiquetas por nombre, en una consulta más para toda la página (sin N+1)."""
    return found.prefetch_related(_tag_links())


def load_tags(found: Iterable[Contact]) -> None:
    """Lo mismo para contactos ya leídos (la respuesta de una escritura)."""
    prefetch_related_objects(list(found), _tag_links())


def tagged(found: QuerySet[Contact], tag_id: UUID) -> QuerySet[Contact]:
    """Los de `found` que llevan la etiqueta, activa o no. Una de otra organización no casa."""
    links = ContactTag.objects.filter(contact_id=OuterRef("pk"), tag_id=tag_id)
    return found.filter(Exists(links))

"""Comandos de categorías (F3-04, E02-01): crear en el árbol. Renombrar, mover y borrar llegan
con F3-04b (#314).

No comprueban permisos: los declara la API (`categories.manage`), la única que los importa
(contrato de import-linter). Validan lo que guardan y auditan cada cambio.
"""

from typing import Any
from uuid import UUID

from django.db import IntegrityError, connections, transaction

from apps.audit.services import Entity, record
from apps.catalog.models import Category
from apps.catalog.slugs import clean_slug, slug_from
from core.tenancy.context import TenantContext
from core.tenancy.scope import require_scope
from core.text import name as clean_name

NAME_MAX = 100
TAKEN_CONSTRAINT = "categories_org_path_uq"


class CategorySlugTaken(Exception):
    """Ya hay una categoría con ese slug bajo el mismo padre."""


class BadParent(ValueError):
    """El padre no sirve: no existe o la categoría pasaría de `Category.MAX_DEPTH` niveles."""


def category_name(value: str) -> str:
    return clean_name(value, NAME_MAX)


def _lock_tree(alias: str, ctx: TenantContext) -> None:
    """Un escritor a la vez en el árbol de la organización, hasta el fin de la transacción.

    Los bloqueos de fila no bastan en READ COMMITTED: la consulta que bloquea la rama no ve las
    hijas que otra transacción añade (o mueve dentro) mientras espera, y esas quedarían con un
    `path` viejo; y dos movimientos cruzados se bloquean en orden opuesto (deadlock, 500). Las
    escrituras de categorías son pocas: se serializan por organización, y cada sentencia que
    sigue ve lo que confirmó la anterior."""
    with connections[alias].cursor() as cursor:
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            [f"categories:{ctx.organization_id}"],
        )


def _live(alias: str) -> Any:
    return Category.objects.using(alias).filter(deleted_at__isnull=True)


def _parent(alias: str, parent_id: UUID | None) -> Category | None:
    """El padre, bloqueado: así una rama no se mueve mientras se le añade una hija."""
    if parent_id is None:
        return None
    try:
        parent: Category = _live(alias).select_for_update(no_key=True).get(pk=parent_id)
    except Category.DoesNotExist:
        raise BadParent("la categoría padre no existe") from None
    return parent


def _taken(error: IntegrityError, slug: str) -> CategorySlugTaken:
    if TAKEN_CONSTRAINT not in str(error):
        raise error
    return CategorySlugTaken(slug)


def create_category(
    ctx: TenantContext, *, name: str, slug: str | None = None, parent_id: UUID | None = None
) -> Category:
    """Crea una categoría en la raíz o dentro de `parent_id`, y lo audita. Sin `slug`, se forma
    con el nombre. `CategorySlugTaken` si el padre ya tiene una con ese slug."""
    alias = require_scope(ctx)
    cleaned = category_name(name)
    chosen = clean_slug(slug) if slug is not None else slug_from(cleaned)
    try:
        with transaction.atomic(using=alias):
            _lock_tree(alias, ctx)
            parent = _parent(alias, parent_id)
            depth = parent.depth + 1 if parent else 1
            if depth > Category.MAX_DEPTH:
                raise BadParent(f"hasta {Category.MAX_DEPTH} niveles")
            path = f"{parent.path if parent else '/'}{chosen}/"
            category: Category = Category.objects.using(alias).create(
                name=cleaned, slug=chosen, parent=parent, path=path, depth=depth
            )
            changes = {"name": [None, cleaned], "path": [None, path]}
            record(ctx, "category.created", Entity("category", category.pk, path), changes)
    except IntegrityError as error:
        raise _taken(error, chosen) from None
    return category

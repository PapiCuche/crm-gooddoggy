"""Comandos de categorías (F3-04, E02-01): crear, renombrar, mover y borrar en el árbol.

No comprueban permisos: los declara la API (`categories.manage`), la única que los importa
(contrato de import-linter). Validan lo que guardan, mantienen `path` y `depth` de toda la rama
en la misma transacción y auditan cada cambio.
"""

from typing import Any
from uuid import UUID

from django.db import IntegrityError, connections, transaction
from django.utils import timezone

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


class CategoryHasChildren(Exception):
    """No se borra una categoría que tiene hijas: antes hay que moverlas o borrarlas."""


class BadParent(ValueError):
    """El padre no sirve: no existe, es la propia categoría o una de sus descendientes, o la
    rama pasaría de `Category.MAX_DEPTH` niveles."""


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


def rename_category(ctx: TenantContext, *, category_id: UUID, name: str) -> Category:
    """Cambia el nombre (el slug y la rama no cambian) y lo audita; el mismo nombre no escribe.
    Una categoría borrada o de otra organización: `DoesNotExist`."""
    alias = require_scope(ctx)
    cleaned = category_name(name)
    with transaction.atomic(using=alias):
        _lock_tree(alias, ctx)
        category: Category = _live(alias).select_for_update(no_key=True).get(pk=category_id)
        if category.name != cleaned:
            changes = {"name": [category.name, cleaned]}
            category.name = cleaned
            category.save(using=alias, update_fields=["name", "updated_at"])
            record(ctx, "category.updated", Entity("category", category.pk, category.path), changes)
    return category


def move_category(ctx: TenantContext, *, category_id: UUID, parent_id: UUID | None) -> Category:
    """Mueve la categoría (con toda su rama) a la raíz o dentro de `parent_id`, y lo audita.
    Recalcula `path` y `depth` de cada descendiente en la misma transacción. `BadParent` si el
    destino es la propia categoría o una descendiente, o si la rama pasaría de los niveles."""
    alias = require_scope(ctx)
    with transaction.atomic(using=alias):
        _lock_tree(alias, ctx)
        node: Category = _live(alias).select_for_update(no_key=True).get(pk=category_id)
        branch = list(
            _live(alias)
            .select_for_update(no_key=True)
            .filter(path__startswith=node.path)
            .order_by("depth", "id")
        )
        parent = _parent(alias, parent_id)
        if parent is not None and parent.path.startswith(node.path):
            raise BadParent("no se mueve dentro de sí misma ni de una de sus descendientes")
        if (parent.pk if parent else None) == node.parent_id:
            return node  # ya está ahí: nada que escribir
        base = parent.depth if parent else 0
        tallest = max(item.depth for item in branch) - node.depth + 1
        if base + tallest > Category.MAX_DEPTH:
            raise BadParent(f"la rama pasaría de {Category.MAX_DEPTH} niveles")
        old_path, new_path = node.path, f"{parent.path if parent else '/'}{node.slug}/"
        shift, now = base + 1 - node.depth, timezone.now()
        try:
            with transaction.atomic(using=alias):
                for item in branch:
                    item.path = new_path + item.path[len(old_path) :]
                    item.depth += shift
                    item.updated_at = now  # `bulk_update` no aplica `auto_now`
                    if item.pk == node.pk:
                        item.parent = parent
                Category.objects.using(alias).bulk_update(
                    branch, ["path", "depth", "parent", "updated_at"]
                )
                # Las borradas de la rama también: una restauración futura no debe encontrar
                # rutas viejas. No cuentan para el índice único (solo las vivas).
                gone = list(
                    Category.objects.using(alias).filter(
                        deleted_at__isnull=False, path__startswith=old_path
                    )
                )
                for item in gone:
                    item.path = new_path + item.path[len(old_path) :]
                    item.depth += shift
                Category.objects.using(alias).bulk_update(gone, ["path", "depth"])
        except IntegrityError as error:
            raise _taken(error, node.slug) from None
        changes = {"path": [old_path, new_path]}
        metadata = {"descendants": len(branch) - 1}
        record(ctx, "category.moved", Entity("category", node.pk, new_path), changes, metadata)
    moved: Category = next(item for item in branch if item.pk == node.pk)
    return moved


def delete_category(ctx: TenantContext, *, category_id: UUID) -> None:
    """Borra una categoría sin hijas (borrado lógico) y lo audita; su slug queda libre bajo ese
    padre. `CategoryHasChildren` si tiene hijas vivas."""
    alias = require_scope(ctx)
    with transaction.atomic(using=alias):
        _lock_tree(alias, ctx)
        category: Category = _live(alias).select_for_update(no_key=True).get(pk=category_id)
        if _live(alias).filter(parent_id=category.pk).exists():
            raise CategoryHasChildren(category.path)
        category.deleted_at = timezone.now()
        category.deleted_by_user_id = ctx.user_id
        category.save(using=alias, update_fields=["deleted_at", "deleted_by_user_id", "updated_at"])
        record(
            ctx,
            "category.deleted",
            Entity("category", category.pk, category.path),
            {"deleted_at": [None, category.deleted_at.isoformat()]},
        )

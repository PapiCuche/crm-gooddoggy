"""Comandos de marcas (F3-01, E02-01): crear, renombrar y borrar.

No comprueban permisos: los declara la API (`brands.manage`), la única que los importa
(contrato de import-linter). Validan lo que guardan y auditan cada cambio.
"""

from typing import Any
from uuid import UUID

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.audit.services import Entity, record
from apps.catalog.models import Brand, Product
from apps.catalog.slugs import clean_slug, slug_from
from core.tenancy.context import TenantContext
from core.tenancy.scope import require_scope
from core.text import name as clean_name

NAME_MAX = 100
TAKEN_CONSTRAINT = "brands_org_slug_uq"


class BrandInUse(Exception):
    """No se borra una marca que tienen productos vivos."""


class BrandSlugTaken(Exception):
    """Ya hay una marca no borrada con ese slug en la organización."""


def brand_name(value: str) -> str:
    return clean_name(value, NAME_MAX)


brand_slug = clean_slug  # la forma de un slug del catálogo (`apps.catalog.slugs`)


def create_brand(ctx: TenantContext, *, name: str, slug: str | None = None) -> Brand:
    """Crea una marca en la organización de `ctx` y lo audita. Sin `slug`, se forma del nombre.
    `BrandSlugTaken` si ya existe uno igual entre las no borradas, sin escribir nada."""
    alias = require_scope(ctx)
    cleaned = brand_name(name)
    chosen = brand_slug(slug) if slug is not None else slug_from(cleaned)
    try:
        with transaction.atomic(using=alias):  # savepoint: la marca y su auditoría, o nada
            brand: Brand = Brand.objects.using(alias).create(name=cleaned, slug=chosen)
            changes = {"name": [None, cleaned], "slug": [None, chosen]}
            record(ctx, "brand.created", Entity("brand", brand.pk, brand.slug), changes)
    except IntegrityError as error:
        if TAKEN_CONSTRAINT not in str(error):
            raise
        raise BrandSlugTaken(chosen) from None
    return brand


def _live(alias: str) -> Any:
    return Brand.objects.using(alias).select_for_update(no_key=True).filter(deleted_at__isnull=True)


def rename_brand(ctx: TenantContext, *, brand_id: UUID, name: str) -> Brand:
    """Cambia el nombre de una marca no borrada y lo audita; el slug no cambia. Si el nombre ya
    es ese, no escribe ni audita. Una marca borrada o de otra organización: `DoesNotExist`."""
    alias = require_scope(ctx)
    cleaned = brand_name(name)
    with transaction.atomic(using=alias):
        brand: Brand = _live(alias).get(pk=brand_id)
        if brand.name != cleaned:
            changes = {"name": [brand.name, cleaned]}
            brand.name = cleaned
            brand.save(using=alias, update_fields=["name", "updated_at"])
            record(ctx, "brand.updated", Entity("brand", brand.pk, brand.slug), changes)
    return brand


def delete_brand(ctx: TenantContext, *, brand_id: UUID) -> None:
    """Borra una marca (borrado lógico) y lo audita. Su slug queda libre. Una marca ya borrada
    o de otra organización: `DoesNotExist`. `BrandInUse` si la usan productos vivos."""
    alias = require_scope(ctx)
    with transaction.atomic(using=alias):
        # `_live` la bloquea antes de mirar sus productos: quien crea o edita uno la bloquea igual,
        # así que su producto ya está confirmado (y se ve) o espera a que esta acabe y la ve borrada
        brand: Brand = _live(alias).get(pk=brand_id)
        products = Product.objects.using(alias).filter(deleted_at__isnull=True)
        if products.filter(brand_id=brand.pk).exists():
            raise BrandInUse(brand.slug)
        brand.deleted_at = timezone.now()
        brand.deleted_by_user_id = ctx.user_id
        brand.save(using=alias, update_fields=["deleted_at", "deleted_by_user_id", "updated_at"])
        record(
            ctx,
            "brand.deleted",
            Entity("brand", brand.pk, brand.slug),
            {"deleted_at": [None, brand.deleted_at.isoformat()]},
        )

"""Catálogo (Fase 3, E02; modelo de datos §E.7). Tablas tenant-owned con FORCE RLS."""

from django.db import models

from core.db.models import TenantModel, uuid7_primary_key


class Brand(TenantModel):
    """Marca de los productos de una organización. `slug` es su clave estable: única entre las
    no borradas, así que borrar una libera su slug. El borrado es lógico (`deleted_at`)."""

    id = uuid7_primary_key()
    name = models.CharField(max_length=100)
    slug = models.CharField(max_length=63)
    logo_file_id = models.UUIDField(null=True, blank=True)  # la subida llega con E02-05
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    deleted_at = models.DateTimeField(null=True, blank=True)
    deleted_by_user_id = models.UUIDField(null=True, blank=True)

    class Meta:
        db_table = "brands"
        constraints = [
            models.UniqueConstraint(
                fields=["organization_id", "slug"],
                condition=models.Q(deleted_at__isnull=True),
                name="brands_org_slug_uq",
            ),
            models.UniqueConstraint(  # destino de las FK compuestas `brand_id` que vendrán
                fields=["organization_id", "id"], name="brands_org_id_uq"
            ),
            models.CheckConstraint(
                condition=models.Q(slug__regex=r"^[a-z0-9]+(-[a-z0-9]+)*$"),
                name="brands_slug_ck",
            ),
        ]

    def __str__(self) -> str:
        return self.slug


class Category(TenantModel):
    """Categoría del catálogo, en árbol (modelo de datos §E.7). `path` materializa la rama con
    los slugs (`/celulares/iphone/`): único por organización entre las no borradas, así que un
    slug se repite en ramas distintas pero no dentro del mismo padre. `depth` empieza en 1."""

    MAX_DEPTH = 5

    id = uuid7_primary_key()
    parent = models.ForeignKey(  # misma organización: FK compuesta en la migración
        "self",
        models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        db_constraint=False,
        db_index=False,
    )
    name = models.CharField(max_length=100)
    slug = models.CharField(max_length=63)
    path = models.CharField(max_length=400)  # MAX_DEPTH × (63 + 1) + 1, con holgura
    depth = models.PositiveSmallIntegerField()
    sort_order = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    deleted_at = models.DateTimeField(null=True, blank=True)
    deleted_by_user_id = models.UUIDField(null=True, blank=True)

    class Meta:
        db_table = "categories"
        indexes = [
            models.Index(fields=["organization_id", "parent"], name="categories_org_parent_idx"),
            # Las ramas se buscan por prefijo de `path` (`LIKE '/a/%'`): sin esta clase de
            # operador el índice no sirve para eso con la colación por defecto.
            models.Index(
                fields=["organization_id", "path"],
                opclasses=["uuid_ops", "varchar_pattern_ops"],
                name="categories_org_path_like_idx",
            ),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["organization_id", "path"],
                condition=models.Q(deleted_at__isnull=True),
                name="categories_org_path_uq",
            ),
            models.UniqueConstraint(  # destino de las FK compuestas `category_id` y del padre
                fields=["organization_id", "id"], name="categories_org_id_uq"
            ),
            models.CheckConstraint(
                condition=models.Q(slug__regex=r"^[a-z0-9]+(-[a-z0-9]+)*$"),
                name="categories_slug_ck",
            ),
            models.CheckConstraint(
                condition=models.Q(depth__gte=1, depth__lte=5), name="categories_depth_ck"
            ),
            models.CheckConstraint(  # la raíz no tiene padre y viceversa
                condition=models.Q(parent__isnull=True, depth=1)
                | models.Q(parent__isnull=False, depth__gt=1),
                name="categories_root_ck",
            ),
        ]

    def __str__(self) -> str:
        return self.path

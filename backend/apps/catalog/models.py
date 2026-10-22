"""Catálogo (Fase 3, E02; modelo de datos §E.7). Tablas tenant-owned con FORCE RLS."""

from django.db import models
from django.db.models import F
from django.db.models.functions import Collate

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
            # La rama se busca como un rango de `path` en orden de bytes (`categories._branch`):
            # con la colación por defecto, el índice único no sirve para eso.
            models.Index(
                F("organization_id"), Collate(F("path"), "C"), name="categories_org_path_c_idx"
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


class Product(TenantModel):
    """Producto comercial (iPhone 16 Pro) o servicio (Cambio de batería), modelo de datos §E.7.
    Nunca lleva precio ni stock: eso es de sus variantes (`Product != Variant`). Marca y
    categoría, si las tiene, son de la misma organización (FKs compuestas en la migración)."""

    class Type(models.TextChoices):
        PHYSICAL = "PHYSICAL"
        SERVICE = "SERVICE"

    class Status(models.TextChoices):
        DRAFT = "DRAFT"
        ACTIVE = "ACTIVE"
        DISCONTINUED = "DISCONTINUED"

    id = uuid7_primary_key()
    product_type = models.CharField(max_length=16, choices=Type.choices, default=Type.PHYSICAL)
    brand = models.ForeignKey(
        Brand,
        models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        db_constraint=False,
        db_index=False,
    )
    category = models.ForeignKey(
        Category,
        models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        db_constraint=False,
        db_index=False,
    )
    name = models.CharField(max_length=200)
    slug = models.CharField(max_length=63)
    description = models.TextField(blank=True)
    short_description = models.CharField(max_length=255, blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.DRAFT)
    tracks_inventory = models.BooleanField(default=True)
    ai_visible = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    deleted_at = models.DateTimeField(null=True, blank=True)
    deleted_by_user_id = models.UUIDField(null=True, blank=True)

    class Meta:
        db_table = "products"
        indexes = [
            models.Index(fields=["organization_id", "brand"], name="products_org_brand_idx"),
            models.Index(fields=["organization_id", "category"], name="products_org_category_idx"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["organization_id", "slug"],
                condition=models.Q(deleted_at__isnull=True),
                name="products_org_slug_uq",
            ),
            models.UniqueConstraint(fields=["organization_id", "id"], name="products_org_id_uq"),
            models.CheckConstraint(
                condition=models.Q(slug__regex=r"^[a-z0-9]+(-[a-z0-9]+)*$"),
                name="products_slug_ck",
            ),
            models.CheckConstraint(  # un servicio no tiene stock
                condition=~models.Q(product_type="SERVICE", tracks_inventory=True),
                name="products_service_stock_ck",
            ),
        ]

    def __str__(self) -> str:
        return self.slug


class ProductVariant(TenantModel):
    """La unidad vendible (modelo de datos §E.7): todo precio, stock y línea de cotización apunta
    aquí. Un producto sin opciones tiene una variante por defecto (`is_default`). El SKU es único
    por organización entre las variantes vivas y se guarda en mayúsculas."""

    class Condition(models.TextChoices):
        NEW = "NEW"
        OPEN_BOX = "OPEN_BOX"
        REFURBISHED = "REFURBISHED"
        USED = "USED"

    class Status(models.TextChoices):
        ACTIVE = "ACTIVE"
        INACTIVE = "INACTIVE"
        DISCONTINUED = "DISCONTINUED"

    id = uuid7_primary_key()
    product = models.ForeignKey(
        Product, models.PROTECT, related_name="variants", db_constraint=False, db_index=False
    )
    sku = models.CharField(max_length=64)
    barcode = models.CharField(max_length=64, blank=True)
    name = models.CharField(max_length=255)
    condition = models.CharField(max_length=16, choices=Condition.choices, default=Condition.NEW)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.ACTIVE)
    is_default = models.BooleanField(default=False)
    ai_visible = models.BooleanField(default=True)
    sort_order = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    deleted_at = models.DateTimeField(null=True, blank=True)
    deleted_by_user_id = models.UUIDField(null=True, blank=True)

    class Meta:
        db_table = "product_variants"
        indexes = [
            models.Index(
                fields=["organization_id", "product"], name="product_variants_org_prod_idx"
            )
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["organization_id", "sku"],
                condition=models.Q(deleted_at__isnull=True),
                name="product_variants_org_sku_uq",
            ),
            models.UniqueConstraint(  # una sola variante por defecto viva por producto
                fields=["product"],
                condition=models.Q(is_default=True, deleted_at__isnull=True),
                name="product_variants_one_default_uq",
            ),
            models.UniqueConstraint(
                fields=["organization_id", "id"], name="product_variants_org_id_uq"
            ),
            models.CheckConstraint(
                condition=models.Q(sku__regex=r"^[A-Z0-9]([A-Z0-9._-]*[A-Z0-9])?$"),
                name="product_variants_sku_ck",
            ),
        ]

    def __str__(self) -> str:
        return self.sku

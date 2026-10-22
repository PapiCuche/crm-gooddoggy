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

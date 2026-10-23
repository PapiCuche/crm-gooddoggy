"""Precios (Fase 3, E03; modelo de datos §E.8, ADR-006). Tablas tenant-owned con FORCE RLS."""

from django.contrib.postgres.constraints import ExclusionConstraint
from django.contrib.postgres.fields import RangeOperators
from django.db import models

from core.db.models import TenantModel, uuid7_primary_key

# Los permisos de lectura de precios (03 §H): uno decide quién ve cada lista.
VIEW, WHOLESALE, DISTRIBUTOR, INTERNAL = (
    "prices.view",
    "prices.view_wholesale",
    "prices.view_distributor",
    "prices.view_internal",
)
READ_PERMISSIONS = (VIEW, WHOLESALE, DISTRIBUTOR, INTERNAL)
OVERLAP = "product_prices_no_overlap_ex"  # la `EXCLUDE`: su violación es un 409, nunca un 500


class Kind(models.TextChoices):
    RETAIL = "RETAIL"
    CASH = "CASH"
    TRANSFER = "TRANSFER"
    WHOLESALE = "WHOLESALE"
    DISTRIBUTOR = "DISTRIBUTOR"
    INTERNAL = "INTERNAL"
    CUSTOM = "CUSTOM"


class PaymentCondition(models.TextChoices):
    CASH = "CASH"
    BANK_TRANSFER = "BANK_TRANSFER"
    CARD = "CARD"


class PriceList(TenantModel):
    """Una lista de precios (Regular, Mayorista…). `code` y `currency` no cambian. La ve quien
    tiene su `required_permission` (o `prices.manage`). Una sola predeterminada por moneda entre las
    no borradas, activa y con `prices.view`. Borrado lógico."""

    Kind = Kind
    PaymentCondition = PaymentCondition

    id = uuid7_primary_key()
    code = models.CharField(max_length=30)
    name = models.CharField(max_length=100)
    kind = models.CharField(max_length=16, choices=Kind.choices)
    currency = models.CharField(max_length=3)  # ISO 4217
    is_default = models.BooleanField(default=False)
    required_permission = models.CharField(max_length=32)
    ai_exposable = models.BooleanField(default=False)  # sin efecto hasta la Fase 8
    payment_condition = models.CharField(  # noqa: DJ001
        max_length=16, choices=PaymentCondition.choices, null=True, blank=True
    )
    is_active = models.BooleanField(default=True)
    sort_order = models.IntegerField(default=0)
    created_by_user_id = models.UUIDField(null=True, blank=True)  # FKs compuestas a membresías
    updated_by_user_id = models.UUIDField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    deleted_at = models.DateTimeField(null=True, blank=True)
    deleted_by_user_id = models.UUIDField(null=True, blank=True)

    class Meta:
        db_table = "price_lists"
        constraints = [
            models.UniqueConstraint(fields=["organization_id", "code"], name="price_lists_code_uq"),
            models.UniqueConstraint(
                fields=["organization_id", "currency"],
                condition=models.Q(is_default=True, deleted_at__isnull=True),
                name="price_lists_default_uq",
            ),
            models.UniqueConstraint(  # destino de las FK compuestas `price_list_id`
                fields=["organization_id", "id"], name="price_lists_org_id_uq"
            ),
            models.UniqueConstraint(  # y de la de `product_prices`, que lleva su moneda (F3-42)
                fields=["organization_id", "id", "currency"], name="price_lists_currency_uq"
            ),
            models.CheckConstraint(
                condition=models.Q(code__regex=r"^[A-Z0-9]+(-[A-Z0-9]+)*$"),
                name="price_lists_code_ck",
            ),
            models.CheckConstraint(
                condition=models.Q(currency__regex=r"^[A-Z]{3}$"), name="price_lists_currency_ck"
            ),
            models.CheckConstraint(
                condition=models.Q(kind__in=Kind.values), name="price_lists_kind_ck"
            ),
            models.CheckConstraint(
                condition=models.Q(required_permission__in=READ_PERMISSIONS),
                name="price_lists_permission_ck",
            ),
            models.CheckConstraint(  # NULL pasa: sin condición de pago
                condition=models.Q(payment_condition__in=PaymentCondition.values),
                name="price_lists_payment_ck",
            ),
            models.CheckConstraint(  # la predeterminada está activa
                condition=models.Q(is_default=False) | models.Q(is_active=True),
                name="price_lists_default_active_ck",
            ),
            models.CheckConstraint(  # y la ve quien ve precios (03 §H, 04 §K.2)
                condition=models.Q(is_default=False) | models.Q(required_permission=VIEW),
                name="price_lists_default_view_ck",
            ),
        ]

    def __str__(self) -> str:
        return self.code


class ProductPrice(TenantModel):
    """El precio de una variante en una lista durante `[starts_at, ends_at)` (§E.8, ADR-006 §1).
    La tabla es el historial: cambiar un precio cierra el vigente e inserta otro. Inmutable salvo
    el cierre de su vigencia [INM]: el runtime solo puede cambiar `ends_at`. La `EXCLUDE` impide
    dos precios solapados del mismo par variante × lista, aunque falle el servicio."""

    id = uuid7_primary_key()
    variant = models.ForeignKey(  # misma organización: FKs compuestas en la migración
        "catalog.ProductVariant",
        models.PROTECT,
        related_name="+",
        db_constraint=False,
        db_index=False,
    )
    price_list = models.ForeignKey(  # con su moneda: la FK compuesta lleva `currency`
        PriceList, models.PROTECT, related_name="+", db_constraint=False, db_index=False
    )
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    currency = models.CharField(max_length=3)
    tax_included = models.BooleanField(default=True)  # IGV incluido (D-PRC-1)
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField(null=True, blank=True)  # NULL: sin fin
    change_reason = models.CharField(max_length=500)
    previous_price = models.ForeignKey(  # el que regía en `starts_at` al escribirla
        "self",
        models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        db_constraint=False,
        db_index=False,
    )
    created_by_user_id = models.UUIDField(null=True, blank=True)  # FK compuesta a membresías
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "product_prices"
        constraints = [
            models.UniqueConstraint(  # destino de la FK compuesta `previous_price_id`
                fields=["organization_id", "id"], name="product_prices_org_id_uq"
            ),
            models.CheckConstraint(
                condition=models.Q(amount__gt=0), name="product_prices_amount_ck"
            ),
            models.CheckConstraint(
                condition=models.Q(ends_at__isnull=True)
                | models.Q(ends_at__gt=models.F("starts_at")),
                name="product_prices_validity_ck",
            ),
            ExclusionConstraint(
                name=OVERLAP,
                index_type="gist",
                expressions=[
                    ("organization_id", RangeOperators.EQUAL),
                    ("variant", RangeOperators.EQUAL),
                    ("price_list", RangeOperators.EQUAL),
                    (
                        models.Func(
                            "starts_at", "ends_at", models.Value("[)"), function="TSTZRANGE"
                        ),
                        RangeOperators.OVERLAPS,
                    ),
                ],
            ),
        ]

    def __str__(self) -> str:
        return f"{self.amount} {self.currency}"

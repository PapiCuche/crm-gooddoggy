"""Cotizaciones (Fase 5, E13; modelo de datos §E.6). Tablas tenant-owned con FORCE RLS y FKs
compuestas (contacto, oportunidad, lista de precios, variante y personas, en la migración).
Todo importe es `Decimal` con dos decimales; los precios vienen del `PricingService` y la línea
los **congela** (nombre, SKU, precio de lista, promoción y precio final): lo que cambie después en
el catálogo o en las listas no cambia una cotización. Nada se borra: se cancela, y `crm_app` no
tiene `DELETE` ni `TRUNCATE`."""

from django.db import models

from core.db.models import TenantModel, uuid7_primary_key

NUMBER = r"^COT-[0-9]{6,}$"  # `COT-000123`; pasado el millón crece (core.sequences)
QUANTITY_MAX = 1_000_000


class QuoteStatus(models.TextChoices):
    DRAFT = "DRAFT"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    APPROVED = "APPROVED"
    SENT = "SENT"
    VIEWED = "VIEWED"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"


TERMINAL = (QuoteStatus.ACCEPTED, QuoteStatus.REJECTED, QuoteStatus.EXPIRED, QuoteStatus.CANCELLED)


class TaxMode(models.TextChoices):
    INCLUDED = "INCLUDED"  # los precios ya llevan IGV (D-PRC-1)
    EXCLUDED = "EXCLUDED"


class DiscountType(models.TextChoices):
    NONE = "NONE"
    PERCENT = "PERCENT"
    AMOUNT = "AMOUNT"


class Quote(TenantModel):
    """Una cotización de un contacto (y, si viene de ella, de una oportunidad suya). Con IGV
    incluido: `total` es lo que paga el cliente, `tax_total` el IGV que lleva y `subtotal` lo
    demás; `discount_total` es lo que se ahorra frente a la lista (promociones y descuentos)."""

    Status, Tax, Discount = QuoteStatus, TaxMode, DiscountType

    id = uuid7_primary_key()
    number = models.CharField(max_length=20)
    revision = models.PositiveSmallIntegerField(default=1)
    contact = models.ForeignKey(
        "contacts.Contact", models.PROTECT, related_name="+", db_constraint=False, db_index=False
    )
    opportunity = models.ForeignKey(
        "deals.Opportunity",
        models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        db_constraint=False,
        db_index=False,
    )
    price_list = models.ForeignKey(
        "pricing.PriceList", models.PROTECT, related_name="+", db_constraint=False, db_index=False
    )
    status = models.CharField(max_length=16, choices=QuoteStatus.choices, default=QuoteStatus.DRAFT)
    currency = models.CharField(max_length=3)
    tax_mode = models.CharField(max_length=8, choices=TaxMode.choices, default=TaxMode.INCLUDED)
    tax_rate = models.DecimalField(max_digits=5, decimal_places=4)
    subtotal = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    discount_total = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    tax_total = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    total = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    valid_until = models.DateTimeField(null=True, blank=True)
    notes_to_customer = models.CharField(max_length=2000, default="", blank=True)
    internal_notes = models.CharField(max_length=2000, default="", blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancel_reason = models.CharField(max_length=500, null=True, blank=True)  # noqa: DJ001
    created_by_user_id = models.UUIDField(null=True, blank=True)
    updated_by_user_id = models.UUIDField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    version = models.PositiveIntegerField(default=1)

    class Meta:
        db_table = "quotes"
        indexes = [
            models.Index(
                fields=["organization_id", "contact", "status"], name="quotes_org_contact_idx"
            ),
            models.Index(
                fields=["organization_id", "opportunity"],
                condition=models.Q(opportunity__isnull=False),
                name="quotes_org_opportunity_idx",
            ),
            models.Index(  # las que vencen: la tarea de expiración (F5-09c)
                fields=["organization_id", "valid_until"],
                condition=models.Q(status__in=["SENT", "VIEWED"]),
                name="quotes_org_expiry_idx",
            ),
        ]
        constraints = [
            models.UniqueConstraint(fields=["organization_id", "id"], name="quotes_org_id_uq"),
            models.UniqueConstraint(  # las líneas apuntan a la cotización con su moneda
                fields=["organization_id", "id", "currency"], name="quotes_org_id_currency_uq"
            ),
            models.UniqueConstraint(
                fields=["organization_id", "number", "revision"], name="quotes_number_uq"
            ),
            models.CheckConstraint(
                condition=models.Q(number__regex=NUMBER), name="quotes_number_ck"
            ),
            models.CheckConstraint(condition=models.Q(revision__gte=1), name="quotes_revision_ck"),
            models.CheckConstraint(
                condition=models.Q(status__in=QuoteStatus.values), name="quotes_status_ck"
            ),
            models.CheckConstraint(
                condition=models.Q(tax_mode__in=TaxMode.values), name="quotes_tax_mode_ck"
            ),
            models.CheckConstraint(
                condition=models.Q(currency__regex="^[A-Z]{3}$"), name="quotes_currency_ck"
            ),
            models.CheckConstraint(
                condition=models.Q(tax_rate__gte=0, tax_rate__lt=1), name="quotes_tax_rate_ck"
            ),
            models.CheckConstraint(  # los importes cuadran: lo que se paga es lo neto más el IGV
                condition=models.Q(
                    subtotal__gte=0,
                    discount_total__gte=0,
                    tax_total__gte=0,
                    total=models.F("subtotal") + models.F("tax_total"),
                ),
                name="quotes_totals_ck",
            ),
            models.CheckConstraint(  # solo lo cancelado lleva su fecha
                condition=models.Q(status="CANCELLED", cancelled_at__isnull=False)
                | (
                    ~models.Q(status="CANCELLED")
                    & models.Q(cancelled_at__isnull=True, cancel_reason__isnull=True)
                ),
                name="quotes_cancelled_ck",
            ),
            models.CheckConstraint(condition=models.Q(version__gte=1), name="quotes_version_ck"),
        ]

    def __str__(self) -> str:
        return self.number


class QuoteItem(TenantModel):
    """Una línea con su snapshot: la variante, su nombre y su SKU, el precio de lista que regía, la
    promoción que se aplicó, el precio unitario tras ella y el final. Con el `PricingService` el
    precio sale de la lista y el descuento de la línea (F5-09b) lo baja, nunca lo sube."""

    id = uuid7_primary_key()
    quote = models.ForeignKey(
        Quote, models.PROTECT, related_name="items", db_constraint=False, db_index=False
    )
    sort_order = models.PositiveSmallIntegerField()
    variant = models.ForeignKey(
        "catalog.ProductVariant",
        models.PROTECT,
        related_name="+",
        db_constraint=False,
        db_index=False,
    )
    product_name_snapshot = models.CharField(max_length=200)
    variant_name_snapshot = models.CharField(max_length=301)
    sku_snapshot = models.CharField(max_length=64)
    quantity = models.PositiveIntegerField()
    list_unit_price = models.DecimalField(max_digits=14, decimal_places=2)
    price_id_snapshot = models.UUIDField(null=True, blank=True)
    promotion_id_snapshot = models.UUIDField(null=True, blank=True)
    promotion_name_snapshot = models.CharField(max_length=100, null=True, blank=True)  # noqa: DJ001
    unit_price = models.DecimalField(max_digits=14, decimal_places=2)
    discount_type = models.CharField(
        max_length=8, choices=DiscountType.choices, default=DiscountType.NONE
    )
    discount_value = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    discount_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    final_unit_price = models.DecimalField(max_digits=14, decimal_places=2)
    line_total = models.DecimalField(max_digits=14, decimal_places=2)
    currency = models.CharField(max_length=3)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "quote_items"
        constraints = [
            models.UniqueConstraint(  # también el índice de las líneas de una cotización, en orden
                fields=["organization_id", "quote", "sort_order"], name="quote_items_order_uq"
            ),
            models.UniqueConstraint(
                fields=["organization_id", "quote", "variant"], name="quote_items_variant_uq"
            ),
            models.CheckConstraint(
                condition=models.Q(quantity__gte=1, quantity__lte=QUANTITY_MAX),
                name="quote_items_quantity_ck",
            ),
            models.CheckConstraint(
                condition=models.Q(discount_type__in=DiscountType.values),
                name="quote_items_discount_ck",
            ),
            models.CheckConstraint(  # un descuento no es negativo; en porcentaje, hasta 100
                condition=models.Q(discount_value__gte=0)
                & (~models.Q(discount_type="PERCENT") | models.Q(discount_value__lte=100)),
                name="quote_items_discount_value_ck",
            ),
            models.CheckConstraint(  # el precio solo baja: lista, promoción, descuento
                condition=models.Q(
                    list_unit_price__gt=0,
                    final_unit_price__gt=0,
                    discount_amount__gte=0,
                    unit_price__lte=models.F("list_unit_price"),
                    final_unit_price__lte=models.F("unit_price"),
                ),
                name="quote_items_prices_ck",
            ),
            models.CheckConstraint(
                condition=models.Q(line_total=models.F("quantity") * models.F("final_unit_price")),
                name="quote_items_total_ck",
            ),
            models.CheckConstraint(
                condition=models.Q(currency__regex="^[A-Z]{3}$"), name="quote_items_currency_ck"
            ),
        ]

    def __str__(self) -> str:
        return self.sku_snapshot

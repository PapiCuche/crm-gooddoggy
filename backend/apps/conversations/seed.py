"""Las categorías de respuestas rápidas con las que nace toda organización (F6-08a1, 02 §E.6).

La llama el alta (`apps.provisioning.seeds`) para las organizaciones nuevas; las que ya existían
las siembra la migración `0038`, con su propia copia de los nombres. No audita: el alta ya lo
hace. No pisa lo que la organización cambió: solo siembra si nunca tuvo ninguna (ni borrada).
"""

from django.db import transaction

from apps.conversations.models import QuickReplyCategory
from core.tenancy.context import TenantContext
from core.tenancy.scope import require_scope

CATEGORIES = ("Ventas", "Precios", "Stock", "Delivery", "Garantías", "Pagos", "Atención general")


def ensure_quick_reply_categories(ctx: TenantContext) -> bool:
    """Deja la organización del contexto con sus categorías. Devuelve si las creó."""
    alias = require_scope(ctx)
    categories = QuickReplyCategory.objects.using(alias)
    with transaction.atomic(using=alias):
        if categories.exists():
            return False
        categories.bulk_create(
            QuickReplyCategory(organization_id=ctx.organization_id, name=name, sort_order=order)
            for order, name in enumerate(CATEGORIES)
        )
    return True

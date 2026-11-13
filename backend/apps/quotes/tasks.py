"""El vencimiento de las cotizaciones (F5-09c3), con el patrón del de las reservas (F3-34): el
barredor de plataforma (beat, 5 minutos) encola `quotes.expire` por organización activa (leída de
`organizations`, sin RLS); la tarea vuelve a comprobar que lo sea, lista las vencidas en un
`tenant_scope` corto y vence cada una en el suyo (`lock_timeout`; topes por pasada y tiempo)."""

import time
from collections import Counter
from uuid import UUID

from django.db import connection
from django.utils import timezone

from apps.organizations.models import Organization
from apps.quotes import answers
from apps.quotes.models import Quote
from core.observability.logging import get_logger
from core.tenancy.celery import platform_task
from core.tenancy.context import TenantContext
from core.tenancy.resolution import ACCESSIBLE_STATUSES
from core.tenancy.scope import tenant_scope

logger = get_logger(__name__)
MAX_PER_RUN = 500  # por organización y pasada: el resto, en el siguiente barrido
RUN_SECONDS = 120.0
QUEUE_EXPIRES = 300  # sin workers no se acumulan barridos viejos
LOCK_TIMEOUT_MS = 10_000


def expire_due(ctx: TenantContext, max_per_run: int) -> Counter[str]:
    """Vence las cotizaciones vencidas de la organización de `ctx`, cada una en su transacción.
    Cuenta `expired`, `skipped` (otro la respondió o la venció antes) y `failed`."""
    done: Counter[str] = Counter(expired=0, skipped=0, failed=0)
    started = time.monotonic()
    with tenant_scope(ctx):
        due = Quote.objects.filter(status__in=answers.OPEN, valid_until__lte=timezone.now())
        found = list(due.order_by("valid_until", "pk").values_list("pk", flat=True)[:max_per_run])
    for pk in found:
        try:
            with tenant_scope(ctx) as tenant:
                with connection.cursor() as cursor:  # solo esta transacción
                    cursor.execute(
                        "SELECT set_config('lock_timeout', %s, true)", [f"{LOCK_TIMEOUT_MS}ms"]
                    )
                done["expired" if answers.expire_quote(tenant, pk) else "skipped"] += 1
        except Exception as error:  # noqa: BLE001 — una cotización no detiene el resto
            logger.error("quotes.expiry_failed", organization_id=str(ctx.organization_id),
                         quote_id=str(pk), error=type(error).__name__)  # fmt: skip
            done["failed"] += 1
        if time.monotonic() - started > RUN_SECONDS:
            break
    return done


@platform_task(name="quotes.expire", ignore_result=True)
def expire_quotes(organization_id: str, max_per_run: int = MAX_PER_RUN) -> dict[str, int]:
    """Vence las cotizaciones vencidas de una organización activa (la pone el barredor)."""
    org = UUID(str(organization_id))
    if not Organization.objects.filter(pk=org, status__in=ACCESSIBLE_STATUSES).exists():
        logger.warning("quotes.expire_org_inactive", organization_id=str(org))
        return dict(Counter(expired=0, skipped=0, failed=0))  # un mensaje no la activa
    done = expire_due(TenantContext(org, "celery"), max(1, min(int(max_per_run), MAX_PER_RUN)))
    logger.info("quotes.expired", organization_id=str(org), **done)
    return dict(done)


@platform_task(name="quotes.expire_sweep", ignore_result=True)
def expire_sweep() -> dict[str, int]:
    """Encola `quotes.expire` por cada organización activa; un fallo no detiene las demás."""
    done = {"organizations": 0, "enqueued": 0, "failed": 0}
    found = Organization.objects.filter(status__in=ACCESSIBLE_STATUSES).order_by("pk")
    for organization_id in list(found.values_list("pk", flat=True)):
        done["organizations"] += 1
        try:
            expire_quotes.apply_async(
                kwargs={"organization_id": str(organization_id)}, expires=QUEUE_EXPIRES
            )
            done["enqueued"] += 1
        except Exception as error:  # noqa: BLE001
            logger.error("quotes.expire_enqueue_failed", organization_id=str(organization_id),
                         error=type(error).__name__)  # fmt: skip
            done["failed"] += 1
    logger.info("quotes.expiry_swept", **done)
    return done

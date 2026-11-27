"""F11-05a (ADR-049 §3, §7): `automations.sweep` (beat, 60 s) encola `automations.evaluate` por
organización accesible. Con `AUTOMATIONS_ENABLED` apagado ninguna de las dos toca la base."""

from uuid import UUID

from apps.automations import sweep as sweeper
from apps.automations.commands import globally_on
from apps.organizations.models import Organization
from core.observability.logging import get_logger
from core.tenancy.celery import platform_task
from core.tenancy.context import TenantContext
from core.tenancy.resolution import ACCESSIBLE_STATUSES

logger = get_logger(__name__)
QUEUE_EXPIRES = 60  # sin workers no se acumulan pasadas viejas


@platform_task(name="automations.evaluate", ignore_result=True)
def evaluate(organization_id: str) -> dict[str, int]:
    """Una pasada de las reglas de una organización accesible (la encola el barrido)."""
    org = UUID(str(organization_id))
    if (
        not globally_on()
        or not Organization.objects.filter(pk=org, status__in=ACCESSIBLE_STATUSES).exists()
    ):
        return {}  # un mensaje no activa una organización
    return dict(sweeper.evaluate(TenantContext(org, "celery")))


@platform_task(name="automations.sweep", ignore_result=True)
def sweep() -> dict[str, int]:
    """Encola `automations.evaluate` por cada organización accesible; un fallo no detiene las
    demás. Apagado (`AUTOMATIONS_ENABLED`), vuelve antes de cualquier consulta."""
    done = {"organizations": 0, "enqueued": 0, "failed": 0}
    if not globally_on():
        return done
    found = Organization.objects.filter(status__in=ACCESSIBLE_STATUSES).order_by("pk")
    for organization_id in list(found.values_list("pk", flat=True)):
        done["organizations"] += 1
        try:
            evaluate.apply_async(
                kwargs={"organization_id": str(organization_id)}, expires=QUEUE_EXPIRES
            )
            done["enqueued"] += 1
        except Exception as error:  # noqa: BLE001
            logger.error("automations.enqueue_failed", organization_id=str(organization_id),
                         error=type(error).__name__)  # fmt: skip
            done["failed"] += 1
    return done

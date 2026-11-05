from typing import Any

from core.observability.logging import get_logger
from core.observability.reporting import safe_ids
from core.outbox import subscribe
from core.outbox.models import OutboxEvent
from core.tenancy.celery import platform_task, tenant_task
from core.tenancy.context import require
from tests.tenancy_app.models import Widget


@tenant_task(name="tenancy_app.visible_widgets")
def visible_widgets() -> list[str]:
    return [str(i) for i in Widget.objects.values_list("id", flat=True)]


@tenant_task(name="tenancy_app.bound_probe", bind=True)
def bound_probe(task: Any, widget_ref: str) -> str:
    """Una tarea de tenant con `bind=True`: recibe la tarea como primer argumento."""
    return f"{task.name}:{widget_ref}"


PINGS: list[str] = []  # ejecuciones reales del cuerpo de platform_ping


@platform_task(name="tenancy_app.platform_ping")
def platform_ping() -> str:
    PINGS.append("ok")
    return "ok"


@platform_task(name="tenancy_app.count_without_tenant")
def count_without_tenant() -> int:
    return Widget.objects.count()  # sin tenant: TenantContextMissing


PROBES: list[tuple[str, str | None, bool]] = []  # (tenant, correlation_id, ¿ve el evento?)


@subscribe("test.widget.created")
@tenant_task(name="tenancy_app.outbox_probe")
def outbox_probe(event_id: str) -> None:
    ctx = require()
    get_logger("tests.probe").info("probe.ran")
    visible = OutboxEvent.objects.filter(id=event_id).exists()
    PROBES.append((str(ctx.organization_id), ctx.correlation_id, visible))


@platform_task(name="tenancy_app.log_probe")
def log_probe() -> None:
    get_logger("tests.probe").info("probe.ran")


FAILURES: list[dict[str, str]] = []  # safe_ids() vistos durante la tarea que falla


@tenant_task(name="tenancy_app.failing_probe")
def failing_probe() -> None:
    FAILURES.append(safe_ids())
    raise RuntimeError("fallo del handler")

"""#568: la firma que Celery valida en `apply_async` (`check_arguments`) de una `@tenant_task`
acepta los argumentos de tenant, con y sin `bind=True`. En Python 3.14 Celery obtiene la firma con
`inspect.signature`, que sigue `__wrapped__` hasta la función original; los tests con
`task_always_eager` o con `apply_async` sustituido no pasan por esa validación."""

import inspect
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from celery.app.task import Task

from apps.conversations import tasks as inbox_tasks
from config.celery import app
from core.outbox.publisher import publish_outbox
from core.tenancy.celery import TenantTask, _tenant_signature, tenant_task
from core.tenancy.context import TenantContextError
from tests.tenancy_app import tasks
from tests.test_conversations_dispatch import backdate, queue, state, world, writer

__all__ = ["world", "writer"]  # los fixtures, para pytest
pytestmark = pytest.mark.usefixtures("tenant_db", "no_realtime_broker")
TENANT_ORDER = ["organization_id", "actor_type", "actor_id", "correlation_id"]
TENANT = set(TENANT_ORDER)


@pytest.fixture
def broker(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Sustituye lo último que hace `apply_async` (`app.send_task`): la validación de los
    argumentos, que va antes, es la real. Devuelve lo que se habría enviado."""
    sent: list[Any] = []

    def send_task(name: str, args: Any = None, kwargs: Any = None, **options: Any) -> Any:
        sent.append((name, kwargs))
        return SimpleNamespace(id=str(uuid4()))

    monkeypatch.setattr(app, "send_task", send_task)
    return sent


def tenant_tasks() -> list[Task]:
    app.loader.import_default_modules()
    return [t for t in app.tasks.values() if getattr(t, "tenancy", None) == "tenant"]


def test_every_tenant_task_header_takes_the_tenant_arguments() -> None:
    found = {t.name for t in tenant_tasks()}
    assert "conversations.dispatch_event" in found  # `dispatch_message` es de plataforma (F7-05b)
    assert {"tenancy_app.outbox_probe", "tenancy_app.bound_probe"} <= found  # con y sin `bind`
    for task in tenant_tasks():
        assert TENANT <= set(inspect.signature(task.__header__).parameters), task.name


def test_the_organization_is_a_required_keyword_in_every_header() -> None:
    """Lo que Celery valida no puede aflojar la garantía de `TenantTask`: sin `organization_id`
    (o como posicional) la validación misma ya rechaza."""
    for task in tenant_tasks():
        org = inspect.signature(task.__header__).parameters["organization_id"]
        assert org.kind is inspect.Parameter.KEYWORD_ONLY, task.name
        assert org.default is inspect.Parameter.empty, task.name


def test_the_signature_keeps_the_function_arguments_whatever_their_shape() -> None:
    def odd(a: int, /, b: int = 1, *rest: int, c: int, **extra: int) -> None: ...

    names = list(_tenant_signature(odd).parameters)
    assert names == ["a", "b", "rest", "c", *TENANT_ORDER, "extra"]


@pytest.mark.parametrize("name", sorted(TENANT))
def test_each_tenant_argument_name_is_reserved(name: str) -> None:
    def clash(**fields: Any) -> None: ...

    clash.__signature__ = inspect.Signature(  # type: ignore[attr-defined]
        [inspect.Parameter(name, inspect.Parameter.KEYWORD_ONLY)]
    )
    with pytest.raises(TypeError, match=name):
        _tenant_signature(clash)


@pytest.mark.parametrize(
    ("task", "own"),
    [
        (tasks.outbox_probe, {"event_id": "x"}),
        (tasks.bound_probe, {"widget_ref": "w"}),
        (tasks.visible_widgets, {}),
        (inbox_tasks.dispatch_event, {"event_id": "x"}),
    ],
)
def test_apply_async_validates_and_publishes_with_the_tenant_arguments(
    task: Task, own: dict[str, Any], broker: list[Any]
) -> None:
    kwargs = {**own, "organization_id": str(uuid4()), "correlation_id": None}
    assert task.apply_async(kwargs=kwargs).id  # la validación de Celery incluida
    assert task.apply_async(kwargs=kwargs | {"actor_type": "SYSTEM", "actor_id": None}).id
    assert [name for name, _ in broker] == [task.name] * 2


def test_an_argument_the_task_does_not_take_is_still_rejected(broker: list[Any]) -> None:
    extra = {"event_id": "x", "organization_id": str(uuid4()), "raro": 1}
    with pytest.raises(TypeError):
        tasks.outbox_probe.apply_async(kwargs=extra)
    with pytest.raises(TypeError):  # falta `event_id`
        tasks.outbox_probe.apply_async(kwargs={"organization_id": str(uuid4())})
    assert broker == []


def test_a_tenant_task_still_needs_its_organization_to_be_queued(broker: list[Any]) -> None:
    with pytest.raises(TenantContextError):
        tasks.outbox_probe.apply_async(kwargs={"event_id": "x"})
    assert broker == []


def test_a_task_cannot_declare_the_arguments_the_decorator_already_takes() -> None:
    with pytest.raises(TypeError, match="correlation_id"):

        @tenant_task(name="tenancy_app.clash")
        def clash(correlation_id: str) -> None: ...


def real_apply_async(monkeypatch: pytest.MonkeyPatch, task: Any) -> list[Any]:
    """`apply_async` real (validación y envío al broker) y, además, la ejecución en el acto, para
    recorrer la entrega completa."""
    published: list[Any] = []

    def both(**options: Any) -> Any:
        published.append(TenantTask.apply_async(task._get_current_object(), **options))
        return task.apply(**options)

    monkeypatch.setattr(task, "apply_async", both)
    return published


def test_the_outbox_publisher_queues_the_dispatcher_and_the_message_is_delivered(
    world: Any, monkeypatch: pytest.MonkeyPatch, broker: list[Any]
) -> None:
    message = queue(world)
    published = real_apply_async(monkeypatch, inbox_tasks.dispatch_event)
    # `message.queued` y `conversation.updated`; sin el arreglo: TypeError, 0 publicados
    assert publish_outbox() == 2 and len(published) == 1
    names = [name for name, _ in broker]  # el evento encola el despacho, que corre en su worker
    assert names == ["conversations.dispatch_event", "conversations.dispatch_message"]
    assert state(world, message).delivery_status == "QUEUED"
    assert inbox_tasks.dispatch_message.apply(kwargs=broker[1][1]).successful()
    assert state(world, message).delivery_status == "DELIVERED"


def test_the_sweeper_queues_the_dispatcher_through_the_real_validation(
    world: Any, migrator: Any, broker: list[Any]
) -> None:
    message = queue(world)
    backdate(migrator, message, 30)
    assert inbox_tasks.sweep_queued() == 1 and len(broker) == 1  # validación real, sin ejecutar
    migrator.execute("UPDATE messages SET not_before = NULL")  # pasó su espera (el `countdown`)
    assert inbox_tasks.dispatch_message.apply(kwargs=broker[0][1]).successful()
    assert state(world, message).delivery_status == "DELIVERED"

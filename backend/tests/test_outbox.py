"""Outbox transaccional + publisher (docs/fase-0/04 §O.2) contra PostgreSQL real como crm_app."""

from collections.abc import Iterator
from dataclasses import replace
from typing import Any
from uuid import UUID

import psycopg
import pytest
from django.db import connection
from kombu.exceptions import OperationalError

from apps.audit.services import Entity, record
from core.ids import new_id
from core.outbox import emit, subscribe
from core.outbox.publisher import pending_organizations, publish_outbox
from core.tenancy.context import ActorType, TenantContext, TenantContextError
from core.tenancy.scope import tenant_scope
from tests.tenancy_app import tasks
from tests.test_tenancy import raw_count

pytestmark = pytest.mark.usefixtures("tenant_db", "no_realtime_broker")
EVENT = "test.widget.created"


def ctx(org: UUID, correlation_id: str | None = None) -> TenantContext:
    return TenantContext(org, "test", correlation_id=correlation_id)


def emit_in(org: UUID, event_type: str = EVENT, correlation_id: str | None = None) -> UUID:
    tenant = ctx(org, correlation_id)
    with tenant_scope(tenant):
        return emit(tenant, event_type, aggregate_type="widget", aggregate_id=new_id())


class Boom(Exception):
    pass


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[dict[str, Any]]]:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(tasks.outbox_probe, "apply_async", lambda **kw: calls.append(kw["kwargs"]))
    yield calls


def test_rollback_leaves_no_event_and_no_audit(
    orgs: dict[str, UUID], migrator: psycopg.Connection[Any]
) -> None:
    tenant = ctx(orgs["A"], "corr-1")
    with pytest.raises(Boom), tenant_scope(tenant):
        emit(tenant, EVENT, aggregate_type="widget", aggregate_id=orgs["widget_A"])
        record(tenant, "widget.created", Entity("widget", orgs["widget_A"]))
        raise Boom
    for table in ("outbox_events", "audit_logs"):
        assert migrator.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,)  # noqa: S608
    with tenant_scope(tenant):  # mismo cambio con commit: ambos, en la misma transacción
        emit(tenant, EVENT, aggregate_type="widget", aggregate_id=orgs["widget_A"])
        record(tenant, "widget.created", Entity("widget", orgs["widget_A"]))
    rows = migrator.execute(
        "SELECT e.occurred_at = a.occurred_at, e.correlation_id, a.correlation_id "
        "FROM outbox_events e, audit_logs a"
    ).fetchall()
    assert rows == [(True, "corr-1", "corr-1")]  # now() = hora de la transacción


def test_emit_requires_the_active_scope_and_valid_input(orgs: dict[str, UUID]) -> None:
    tenant = ctx(orgs["A"])
    with pytest.raises(TenantContextError):
        emit(tenant, EVENT, aggregate_type="widget", aggregate_id=new_id())  # sin scope
    with tenant_scope(tenant):
        for other in (replace(tenant, actor_type=ActorType.USER), ctx(orgs["B"])):
            with pytest.raises(TenantContextError):
                emit(other, EVENT, aggregate_type="widget", aggregate_id=new_id())
        for event_type, aggregate_type, payload in (
            ("Widget.Created", "widget", {}),
            ("widget", "widget", {}),
            (EVENT, "Widget", {}),
            (EVENT, "widget", {"blob": "x" * 20_000}),
        ):
            with pytest.raises(ValueError):
                emit(tenant, event_type, aggregate_type=aggregate_type, aggregate_id=new_id(),
                     payload=payload)  # fmt: skip


def test_publisher_dispatches_per_tenant_with_correlation(
    orgs: dict[str, UUID], sent: list[dict[str, Any]], migrator: psycopg.Connection[Any]
) -> None:
    a, b = emit_in(orgs["A"], correlation_id="corr-a"), emit_in(orgs["B"])
    emit_in(orgs["A"], "test.nobody.listens")  # sin suscriptores: se marca publicado
    assert raw_count("outbox_events") == 0  # T1 no vacía: sin tenant, 0 filas aunque existan
    assert set(pending_organizations(10)) == {orgs["A"], orgs["B"]}  # crm_app, sin tenant: IDs
    assert publish_outbox.apply().get() == 3
    expected = [
        {"event_id": str(a), "organization_id": str(orgs["A"]), "correlation_id": "corr-a"},
        {"event_id": str(b), "organization_id": str(orgs["B"]), "correlation_id": None},
    ]
    assert sorted(sent, key=str) == sorted(expected, key=str)
    state = migrator.execute(
        "SELECT count(*) FROM outbox_events "
        "WHERE published_at IS NOT NULL AND attempts = 1 AND last_error = ''"
    ).fetchone()
    assert state == (3,)
    assert publish_outbox.apply().get() == 0 and len(sent) == 2  # nada se reenvía
    assert pending_organizations(10) == []


def test_discovery_limit_is_bounded_inside_the_function(
    orgs: dict[str, UUID], migrator: psycopg.Connection[Any]
) -> None:
    low, high = sorted((orgs["A"], orgs["B"]))
    emit_in(high)  # el UUID mayor tiene el evento más antiguo: se ordena por antigüedad, no por ID
    emit_in(low)
    assert pending_organizations(1) == [high] and pending_organizations(2) == [high, low]
    with connection.cursor() as cursor:  # crm_app llama directamente a la función
        for limit in (None, 0, -5):  # LIMIT NULL sería "sin límite": fail-closed → 0 filas
            cursor.execute("SELECT public.outbox_pending_organizations(%s::integer)", [limit])
            assert cursor.fetchall() == [], limit
    migrator.execute(  # 1001 organizaciones más, cada una con un evento pendiente
        "WITH o AS (INSERT INTO organizations (id, slug, name, status, created_at) "
        "SELECT uuidv7(), 'bulk-' || g, 'Bulk', 'ACTIVE', now() FROM generate_series(1, 1001) g "
        "RETURNING id) INSERT INTO outbox_events (id, organization_id, event_type, "
        "aggregate_type, aggregate_id, payload, attempts, last_error) "
        "SELECT uuidv7(), id, 'test.bulk.created', 'widget', uuidv7(), '{}', 0, '' FROM o"
    )
    assert len(pending_organizations(2**31 - 1)) == 1000  # nunca más de 1000


def test_broker_failure_keeps_the_event_pending(
    orgs: dict[str, UUID], monkeypatch: pytest.MonkeyPatch, migrator: psycopg.Connection[Any]
) -> None:
    def down(**kwargs: Any) -> None:
        raise OperationalError("amqp://user:" + "clave" + "@broker caído")

    monkeypatch.setattr(tasks.outbox_probe, "apply_async", down)
    emit_in(orgs["A"])
    assert publish_outbox.apply().get() == 0
    row = migrator.execute("SELECT published_at, attempts, last_error FROM outbox_events")
    assert row.fetchone() == (None, 1, "OperationalError")  # solo la clase, nunca el mensaje


def test_handler_runs_in_the_event_tenant(orgs: dict[str, UUID]) -> None:
    event = emit_in(orgs["A"], correlation_id="corr-h")
    tasks.PROBES.clear()
    for org in ("A", "B"):
        tasks.outbox_probe.apply(
            kwargs={"event_id": str(event), "organization_id": str(orgs[org]),
                    "correlation_id": "corr-h"}
        ).get()  # fmt: skip
    assert tasks.PROBES == [(str(orgs["A"]), "corr-h", True), (str(orgs["B"]), "corr-h", False)]


def test_only_tenant_tasks_can_subscribe() -> None:
    with pytest.raises(TypeError):
        subscribe("test.platform.event")(tasks.platform_ping)
    with pytest.raises(ValueError):
        subscribe("Not A Name")

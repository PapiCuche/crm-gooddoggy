"""F3-34: el vencimiento automático de las reservas (`apps.inventory.tasks`) contra PostgreSQL
real con `crm_app`, también con transacciones a la vez (un hilo y una conexión cada una). El
tiempo se adelanta con `timezone.now`: una reserva ACTIVE no se puede retocar ni con el migrador.
Tras cada secuencia, `reserved` es la suma de las reservas ACTIVE de la celda."""

import json
import logging
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import psycopg
import pytest
from django.conf import settings
from django.db import connection
from django.utils import timezone

from apps.catalog import products
from apps.catalog.models import ProductVariant
from apps.catalog.products import update_product
from apps.inventory import reservations as res
from apps.inventory import stock, tasks
from apps.inventory.models import InventoryMovement, InventoryReservation, Warehouse
from apps.organizations.models import Organization
from config.celery import app
from core.observability.logging import json_formatter
from core.tenancy.resolution import ACCESSIBLE_STATUSES
from core.tenancy.scope import tenant_scope
from tests import test_authorization
from tests.test_inventory_reservations import (
    asked,
    consistent,
    ledger,
    refused,
    release,
    reserve,
    stocked,
)
from tests.test_memberships import ctx

world = test_authorization.world
pytestmark = pytest.mark.usefixtures("tenant_db")
Cell = tuple[UUID, UUID]
ZERO = {"expired": 0, "skipped": 0, "failed": 0, "capped": 0}


@pytest.fixture
def later(monkeypatch: pytest.MonkeyPatch) -> Callable[[float], None]:
    """Adelanta el reloj de la aplicación: lo que vencía en una hora ya venció."""
    real = timezone.now

    def go(hours: float = 2) -> None:
        monkeypatch.setattr(timezone, "now", lambda: real() + timedelta(hours=hours))

    return go


def run(org: UUID, **limits: Any) -> dict[str, int]:
    done: dict[str, int] = tasks.expire_reservations(str(org), **limits)
    return done


def states(org: UUID) -> dict[UUID, str]:
    with tenant_scope(ctx(org)):
        return dict(InventoryReservation.objects.values_list("pk", "status"))


def releases(org: UUID, ids: list[UUID]) -> list[tuple[str, str, Any, int]]:
    with tenant_scope(ctx(org)):
        rows = InventoryMovement.objects.filter(movement_type="RELEASE", reference_id__in=ids)
        found = rows.order_by("pk").values_list(
            "idempotency_key", "created_by_type", "created_by_user_id", "quantity"
        )
        return list(found)


def audited(migrator: psycopg.Connection[Any], ids: list[UUID]) -> list[tuple[Any, ...]]:
    sql = (
        "SELECT actor_type, actor_id, action, changes FROM audit_logs"
        " WHERE action = 'inventory.reservation_expired' AND entity_id = ANY(%s) ORDER BY id"
    )
    return migrator.execute(sql, [ids]).fetchall()


def test_the_job_expires_only_the_due_ones_of_its_organization_once(
    world: Any, migrator: psycopg.Connection[Any], later: Callable[[float], None]
) -> None:
    one, two = stocked(world.a, 6, world.ana), stocked(world.a, 3, world.ana, "OTRA")
    there = stocked(world.b, 3, None)
    due1 = reserve(world.a, world.ana, asked(one, 2, hours=1))[0]
    due2 = reserve(world.a, world.ana, asked(two, 1, hours=1))[0]
    live = reserve(world.a, world.ana, asked(one, 1, hours=5))[0]
    gone = reserve(world.a, world.ana, asked(one, 1, hours=1))[0]
    release(world.a, world.ana, gone.pk)
    other = reserve(world.b, None, asked(there, 2, hours=1))[0]
    with tenant_scope(ctx(world.a)):  # un almacén desactivado también devuelve sus unidades
        Warehouse.objects.filter(pk=one[1]).update(is_active=False)
    later(2)
    assert run(world.a) == {**ZERO, "expired": 2}
    want = {due1.pk: "EXPIRED", due2.pk: "EXPIRED", live.pk: "ACTIVE", gone.pk: "RELEASED"}
    assert states(world.a) == want and states(world.b) == {other.pk: "ACTIVE"}  # B, sin tocar
    assert consistent(world.a, one) == (6, 1, 5) and consistent(world.a, two) == (3, 0, 3)
    assert consistent(world.b, there) == (3, 2, 1)
    expected = [(f"R/{r.pk}/release", "SYSTEM", None, -r.quantity) for r in (due1, due2)]
    assert sorted(releases(world.a, [due1.pk, due2.pk])) == sorted(expected)
    assert releases(world.b, [other.pk]) == []
    audit = audited(migrator, [due1.pk, due2.pk, other.pk])
    assert (
        sorted((a[0], a[1], a[2]) for a in audit)
        == [("SYSTEM", None, "inventory.reservation_expired")] * 2
    )
    assert {tuple(a[3]["status"]) for a in audit} == {("ACTIVE", "EXPIRED")}
    assert run(world.a) == ZERO  # repetir no hace nada
    assert (
        len(releases(world.a, [due1.pk, due2.pk])) == 2
        and len(audited(migrator, [due1.pk, due2.pk])) == 2
    )
    assert run(world.b) == {**ZERO, "expired": 1} and consistent(world.b, there) == (3, 0, 3)
    assert states(world.a) == want and len(audited(migrator, [due1.pk, due2.pk, other.pk])) == 3
    nobody = UUID(int=1)  # una organización que no existe: no ve nada
    assert run(nobody) == ZERO


def test_a_reservation_that_is_not_yet_due_or_is_closed_is_never_expired(
    world: Any, later: Callable[[float], None]
) -> None:
    here = stocked(world.a, 4, world.ana)
    soon = reserve(world.a, world.ana, asked(here, 1, hours=3))[0]
    reserve(world.a, world.ana, asked(here, 1, hours=1))
    shut = reserve(world.a, world.ana, asked(here, 1, hours=1))[0]
    release(world.a, world.ana, shut.pk)  # cerrada antes de vencer
    later(2)
    assert run(world.a) == {**ZERO, "expired": 1}
    with tenant_scope(ctx(world.a)) as tenant:  # el servicio tampoco vence lo vigente ni lo cerrado
        assert res.expire(tenant, soon.pk) is False
        assert res.expire(tenant, UUID(int=7)) is False
        assert res.expire(tenant, shut.pk) is False
    assert states(world.a)[soon.pk] == "ACTIVE" and consistent(world.a, here) == (4, 1, 3)
    assert len(ledger(world.a, "RELEASE")) == 2 and states(world.a)[shut.pk] == "RELEASED"


def test_the_database_refuses_to_undo_or_repeat_an_expiry(
    world: Any, later: Callable[[float], None]
) -> None:
    here = stocked(world.a, 2, world.ana)
    made = reserve(world.a, world.ana, asked(here, 2, hours=1))[0]
    later(2)
    assert run(world.a) == {**ZERO, "expired": 1}
    table = "UPDATE inventory_reservations SET "  # noqa: S608
    undo = "status = 'ACTIVE', ended_at = NULL, ended_by_type = NULL, end_reason = NULL"
    refused(world.a, "una reserva cerrada", table + undo)
    refused(world.a, "una reserva cerrada", table + "status = 'RELEASED'")
    refused(world.a, "permission denied", table + "quantity = 1")  # no es del cierre
    insert = (  # un segundo RELEASE de la misma reserva, a mano: la clave derivada es única
        "INSERT INTO inventory_movements (id, organization_id, variant_id, warehouse_id,"
        " movement_type, quantity, on_hand_after, reserved_after, reference_type, reference_id,"
        " reason, idempotency_key, created_by_type, created_at) VALUES (uuidv7(), %s, %s, %s,"
        " 'RELEASE', -2, 2, 0, 'RESERVATION', %s, 'x', %s, 'SYSTEM', now())"
    )
    params = [world.a, *here, made.pk, f"R/{made.pk}/release"]
    refused(world.a, "inventory_movements_key_uq", insert, params)
    assert run(world.a) == ZERO and states(world.a) == {made.pk: "EXPIRED"}
    assert len(releases(world.a, [made.pk])) == 1 and consistent(world.a, here) == (2, 0, 2)


def overlap(
    monkeypatch: pytest.MonkeyPatch, first: Callable[[], Any], second: Callable[[], Any], *,
    where: tuple[Any, str] = (stock, "lock_cell"),
) -> list[Any]:  # fmt: skip
    """`first` toma la celda (o lo que bloquee antes de llamar a `where`) y espera dentro de su
    transacción; `second` arranca y tiene que esperar su bloqueo; `first` sigue y confirma, y
    `second` continúa."""
    paused, free, out = threading.Event(), threading.Event(), list[Any]([None, None])
    real = getattr(*where)

    def slow(*args: Any) -> Any:
        found = real(*args)
        if not paused.is_set():  # solo el primero que llega
            paused.set()
            free.wait(timeout=10)
        return found

    def go(index: int, work: Callable[[], Any]) -> None:
        try:
            out[index] = work()
        except Exception as error:  # noqa: BLE001 — el resultado lo comprueba cada test
            out[index] = type(error).__name__
        finally:
            paused.set()
            connection.close()

    monkeypatch.setattr(*where, slow)
    one, two = (threading.Thread(target=go, args=(i, w)) for i, w in enumerate((first, second)))
    one.start()
    assert paused.wait(timeout=10)
    two.start()
    two.join(timeout=1)
    waited = two.is_alive()
    free.set()
    for thread in (one, two):
        thread.join(timeout=10)
    assert waited, "el segundo no esperó al primero"
    return out


def user_release(world: Any, pk: UUID) -> Callable[[], Any]:
    return lambda: release(world.a, world.ana, pk, "Fin")


def job(world: Any) -> Callable[[], Any]:
    return lambda: run(world.a)


def test_two_jobs_at_once_write_one_release(
    world: Any, monkeypatch: pytest.MonkeyPatch, later: Callable[[float], None]
) -> None:
    here = stocked(world.a, 2, world.ana)
    made = reserve(world.a, world.ana, asked(here, 2, hours=1))[0]
    later(2)
    first, second = overlap(monkeypatch, job(world), job(world))
    assert first == {**ZERO, "expired": 1} and second == {**ZERO, "skipped": 1}
    assert releases(world.a, [made.pk]) == [(f"R/{made.pk}/release", "SYSTEM", None, -2)]
    assert consistent(world.a, here) == (2, 0, 2) and states(world.a) == {made.pk: "EXPIRED"}


@pytest.mark.parametrize("job_first", [True, False])
def test_the_job_against_a_manual_release_at_once_writes_one_release(
    world: Any, monkeypatch: pytest.MonkeyPatch, later: Callable[[float], None], job_first: bool
) -> None:
    here = stocked(world.a, 2, world.ana)
    made = reserve(world.a, world.ana, asked(here, 1, hours=1))[0]
    later(2)
    work = [job(world), user_release(world, made.pk)]
    outcome = overlap(monkeypatch, *(work if job_first else work[::-1]))
    by_job, by_user = outcome if job_first else outcome[::-1]
    # vencida: en los dos órdenes la cierra el job y la persona ya no la libera (F3-35)
    assert (by_job["expired"], by_user) == (1, "ReservationNotActive")
    assert states(world.a) == {made.pk: "EXPIRED"}
    assert len(releases(world.a, [made.pk])) == 1 and consistent(world.a, here) == (2, 0, 2)


@pytest.mark.parametrize("job_first", [True, False])
def test_the_job_against_a_reserve_of_the_last_unit_at_once(
    world: Any, monkeypatch: pytest.MonkeyPatch, later: Callable[[float], None], job_first: bool
) -> None:
    here = stocked(world.a, 1, world.ana)
    old = reserve(world.a, world.ana, asked(here, 1, hours=1))[0]
    later(2)
    new = lambda: reserve(world.a, world.ana, asked(here, 1, hours=5))  # noqa: E731
    work = [job(world), new]  # la unidad es de una vencida: reservar la usa venza quien venza
    outcome = overlap(monkeypatch, *(work if job_first else work[::-1]))
    by_job, by_reserve = outcome if job_first else outcome[::-1]
    assert by_reserve[1] is True and (by_job["expired"], by_job["skipped"]) == (
        (1, 0) if job_first else (0, 1)
    )
    assert states(world.a)[old.pk] == "EXPIRED" and consistent(world.a, here) == (1, 1, 0)
    assert releases(world.a, [old.pk]) == [(f"R/{old.pk}/release", "SYSTEM", None, -1)]


def test_many_jobs_at_once_release_each_reservation_once(
    world: Any, later: Callable[[float], None]
) -> None:
    here = stocked(world.a, 8, world.ana)
    made = [reserve(world.a, world.ana, asked(here, 1, hours=1))[0].pk for _ in range(6)]
    later(2)
    start, threads = threading.Barrier(4), 4

    def go(_: int) -> Any:
        try:
            start.wait(timeout=10)
            return run(world.a)
        except Exception as error:  # noqa: BLE001
            return type(error).__name__
        finally:
            connection.close()

    with ThreadPoolExecutor(threads) as pool:
        outcome = list(pool.map(go, range(threads)))
    assert sum(o["expired"] for o in outcome) == 6, outcome
    keys = [row[0] for row in releases(world.a, made)]
    assert sorted(keys) == sorted(f"R/{pk}/release" for pk in made)  # una por reserva
    assert consistent(world.a, here) == (8, 0, 8) and set(states(world.a).values()) == {"EXPIRED"}


def test_the_batch_and_the_run_limits_hold_across_runs(
    world: Any, monkeypatch: pytest.MonkeyPatch, later: Callable[[float], None]
) -> None:
    one, two = stocked(world.a, 9, world.ana), stocked(world.a, 9, world.ana, "OTRA")
    made = [reserve(world.a, world.ana, asked(w, 1, hours=1))[0] for w in (one, two, one, two, one)]
    later(2)
    chained: list[dict[str, Any]] = []
    monkeypatch.setattr(tasks.expire_reservations, "apply_async", lambda **kw: chained.append(kw))
    seen = [run(world.a, batch_size=1, max_per_run=2) for _ in range(3)]
    assert [(s["expired"], s["capped"]) for s in seen] == [(2, 1), (2, 1), (1, 0)]
    assert len(chained) == 2  # se encadena mientras queda y se avanzó
    second = sorted((r.variant_id, r.pk) for r in made)[1]  # sigue tras la última que tomó
    assert chained[0]["kwargs"] == {
        "organization_id": str(world.a),
        "batch_size": 1,
        "max_per_run": 2,
        "after": [str(second[0]), str(second[1])],
    }
    assert chained[0]["expires"] == tasks.QUEUE_EXPIRES
    with tenant_scope(ctx(world.a)):
        order = InventoryMovement.objects.filter(movement_type="RELEASE").order_by("pk")
        done = list(order.values_list("reference_id", "variant_id"))
    by_id = {r.pk: r.variant_id for r in made}
    assert [by_id[pk] for pk, _ in done] == [v for _, v in done]
    first_run = [(v, pk) for pk, v in done[:2]]  # por variante e id: las dos primeras
    assert first_run == sorted((r.variant_id, r.pk) for r in made)[:2]
    assert run(world.a) == ZERO and set(states(world.a).values()) == {"EXPIRED"}
    assert consistent(world.a, one) == (9, 0, 9) and len(ledger(world.a, "RELEASE")) == 5


def test_a_failing_reservation_is_logged_and_does_not_stop_the_batch(
    world: Any,
    monkeypatch: pytest.MonkeyPatch,
    later: Callable[[float], None],
    caplog: pytest.LogCaptureFixture,
) -> None:
    here = stocked(world.a, 5, world.ana)
    made = [reserve(world.a, world.ana, asked(here, 1, hours=1))[0].pk for _ in range(3)]
    bad = sorted(made)[1]
    real = res.expire

    def flaky(tenant: Any, pk: UUID) -> bool:
        if pk == bad:
            raise RuntimeError("Ana Pérez ana@example.com")  # un mensaje con datos de personas
        return real(tenant, pk)

    chained: list[Any] = []
    monkeypatch.setattr(res, "expire", flaky)
    monkeypatch.setattr(tasks.expire_reservations, "apply_async", lambda **kw: chained.append(kw))
    later(2)
    with caplog.at_level(logging.INFO):  # un error de esa fila: la de detrás, en su celda, vence
        assert run(world.a) == {**ZERO, "expired": 2, "failed": 1}
    logged = "\n".join(json_formatter().format(record) for record in caplog.records)
    failed = next(json.loads(line) for line in logged.splitlines() if "expiry_failed" in line)
    assert failed["reservation_id"] == str(bad) and failed["error"] == "RuntimeError"
    assert failed["organization_id"] == str(world.a) and "ana@example.com" not in logged
    summary = next(
        json.loads(line) for line in logged.splitlines() if "reservations_expired" in line
    )
    assert (summary["expired"], summary["failed"], summary["skipped"]) == (2, 1, 0)
    assert states(world.a) == {pk: "ACTIVE" if pk == bad else "EXPIRED" for pk in made}
    assert consistent(world.a, here) == (5, 1, 4)
    assert run(world.a) == {**ZERO, "failed": 1}  # cada barrido la reintenta, sola
    assert chained == []  # sin lote lleno no se encadena
    monkeypatch.setattr(res, "expire", real)
    assert run(world.a) == {**ZERO, "expired": 1}  # la siguiente pasada la recoge


def test_a_failure_in_the_middle_of_one_expiry_leaves_nothing_behind(
    world: Any, monkeypatch: pytest.MonkeyPatch, later: Callable[[float], None]
) -> None:
    here = stocked(world.a, 3, world.ana)
    made = [reserve(world.a, world.ana, asked(here, 1, hours=1))[0].pk for _ in range(2)]
    chained: list[Any] = []
    monkeypatch.setattr(tasks.expire_reservations, "apply_async", lambda **kw: chained.append(kw))

    def boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("tras cerrar y liberar")

    later(2)
    for step in ("_movement", "_audit"):  # entre el cierre y el RELEASE, y después de los dos
        with monkeypatch.context() as patch:
            patch.setattr(res, step, boom)
            assert run(world.a, max_per_run=1) == {**ZERO, "failed": 1, "capped": 1}
        assert set(states(world.a).values()) == {"ACTIVE"} and releases(world.a, made) == []
        assert consistent(world.a, here) == (3, 2, 1)
    assert set(states(world.a).values()) == {"ACTIVE"} and releases(world.a, made) == []
    assert consistent(world.a, here) == (3, 2, 1)
    first = [str(here[0]), str(min(made))]  # una fila mala no corta la cadena: sigue tras ella
    assert [c["kwargs"]["after"] for c in chained] == [first, first]
    assert run(world.a) == {**ZERO, "expired": 2} and consistent(world.a, here) == (3, 0, 3)


def test_a_busy_cell_makes_the_job_give_up_that_reservation_and_retry_later(
    world: Any,
    migrator: psycopg.Connection[Any],
    monkeypatch: pytest.MonkeyPatch,
    later: Callable[[float], None],
) -> None:
    here = stocked(world.a, 2, world.ana)
    made = reserve(world.a, world.ana, asked(here, 1, hours=1))[0]
    later(2)
    monkeypatch.setattr(tasks, "LOCK_TIMEOUT_MS", 200)
    with migrator.transaction():  # alguien tiene la variante bloqueada más de lo que se espera
        migrator.execute("SELECT 1 FROM product_variants WHERE id = %s FOR UPDATE", [here[0]])
        assert run(world.a) == {**ZERO, "failed": 1}
    assert states(world.a) == {made.pk: "ACTIVE"} and consistent(world.a, here) == (2, 1, 1)
    assert run(world.a) == {**ZERO, "expired": 1} and consistent(world.a, here) == (2, 0, 2)
    with tenant_scope(ctx(world.a)):  # el tiempo de espera es solo de esa transacción
        assert connection.cursor().execute("SHOW lock_timeout").fetchone() != ("200ms",)


def test_a_message_cannot_activate_an_organization_or_lift_the_limits(
    world: Any, migrator: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch,
    later: Callable[[float], None],
) -> None:  # fmt: skip
    here = stocked(world.a, 5, world.ana)
    made = [reserve(world.a, world.ana, asked(here, 1, hours=1))[0].pk for _ in range(4)]
    later(2)
    migrator.execute("UPDATE organizations SET status = 'SUSPENDED' WHERE id = %s", [world.a])
    try:  # suspendida o inexistente: no se toca nada
        assert run(world.a) == ZERO and run(UUID(int=5)) == ZERO
    finally:
        migrator.execute("UPDATE organizations SET status = 'ACTIVE' WHERE id = %s", [world.a])
    assert set(states(world.a).values()) == {"ACTIVE"} and releases(world.a, made) == []
    chained: list[Any] = []
    monkeypatch.setattr(tasks.expire_reservations, "apply_async", lambda **kw: chained.append(kw))
    assert run(world.a, batch_size=0, max_per_run=-5)["expired"] == 1  # acotados a 1
    assert chained[0]["kwargs"]["batch_size"] == chained[0]["kwargs"]["max_per_run"] == 1
    monkeypatch.setattr(tasks, "BATCH", 2)
    monkeypatch.setattr(tasks, "MAX_PER_RUN", 2)  # 10**9 pedido, tope 2
    assert run(world.a, batch_size=10**9, max_per_run=10**9)["expired"] == 2
    assert consistent(world.a, here) == (5, 1, 4)


def test_a_slow_run_stops_at_its_time_budget_inside_the_batch(
    world: Any, monkeypatch: pytest.MonkeyPatch, later: Callable[[float], None]
) -> None:
    here = stocked(world.a, 4, world.ana)
    made = [reserve(world.a, world.ana, asked(here, 1, hours=1))[0].pk for _ in range(3)]
    later(2)
    monkeypatch.setattr(tasks, "RUN_SECONDS", -1.0)  # ya pasado tras la primera fila
    chained: list[Any] = []
    monkeypatch.setattr(tasks.expire_reservations, "apply_async", lambda **kw: chained.append(kw))
    assert run(world.a) == {**ZERO, "expired": 1, "capped": 1} and len(chained) == 1
    assert (
        list(states(world.a).values()).count("EXPIRED") == 1
        and len(ledger(world.a, "RELEASE")) == 1
    )
    assert consistent(world.a, here)[1] == 2 and len(made) == 3


def test_a_failing_chain_is_logged_and_the_run_still_reports(
    world: Any, monkeypatch: pytest.MonkeyPatch, later: Callable[[float], None],
    caplog: pytest.LogCaptureFixture,
) -> None:  # fmt: skip
    here = stocked(world.a, 4, world.ana)
    for _ in range(2):
        reserve(world.a, world.ana, asked(here, 1, hours=1))
    later(2)

    def down(**kw: Any) -> None:
        raise ConnectionError("broker con ana@example.com")

    monkeypatch.setattr(tasks.expire_reservations, "apply_async", down)
    with caplog.at_level(logging.INFO):
        assert run(world.a, max_per_run=1) == {**ZERO, "expired": 1, "capped": 1}
    assert "inventory.expire_chain_failed" in caplog.text and "ana@example.com" not in caplog.text
    assert run(world.a) == {**ZERO, "expired": 1}  # el siguiente barrido la recoge


def test_a_reservation_expires_exactly_at_its_deadline_and_not_a_moment_before(
    world: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    here = stocked(world.a, 1, world.ana)
    made = reserve(world.a, world.ana, asked(here, 1, hours=1))[0]
    for moment, outcome in ((made.expires_at - timedelta(microseconds=1), 0), (made.expires_at, 1)):
        monkeypatch.setattr(timezone, "now", lambda moment=moment: moment)
        assert run(world.a)["expired"] == outcome
    assert states(world.a) == {made.pk: "EXPIRED"} and consistent(world.a, here) == (1, 0, 1)


def test_the_sweep_enqueues_each_active_organization_and_one_failing_does_not_stop_the_rest(
    world: Any,
    migrator: psycopg.Connection[Any],
    monkeypatch: pytest.MonkeyPatch,
    later: Callable[[float], None],
    caplog: pytest.LogCaptureFixture,
) -> None:
    mine, theirs = stocked(world.a, 2, world.ana), stocked(world.b, 2, None)
    one = reserve(world.a, world.ana, asked(mine, 1, hours=1))[0]
    two = reserve(world.b, None, asked(theirs, 1, hours=1))[0]
    migrator.execute("INSERT INTO organizations (id, slug, name, status, created_at) VALUES "
                     "(uuidv7(), 'suspendida', 'Suspendida', 'SUSPENDED', now())")  # fmt: skip
    suspended = migrator.execute(
        "SELECT id FROM organizations WHERE slug = 'suspendida'"
    ).fetchone()
    assert suspended is not None
    queued: list[dict[str, Any]] = []
    real = tasks.expire_reservations.apply

    def enqueue(kwargs: dict[str, str], **options: Any) -> None:
        if kwargs["organization_id"] == str(world.a):
            raise ConnectionError("broker caído")
        queued.append({"kwargs": kwargs, **options})
        real(kwargs=kwargs)  # el worker, aquí mismo

    monkeypatch.setattr(tasks.expire_reservations, "apply_async", enqueue)
    later(2)
    try:
        with caplog.at_level(logging.INFO):
            done = tasks.expire_reservations_sweep()
    finally:
        migrator.execute("DELETE FROM organizations WHERE slug = 'suspendida'")
    assert done == {"organizations": 2, "enqueued": 1, "failed": 1}  # sin la suspendida
    assert queued == [{"kwargs": {"organization_id": str(world.b)}, "expires": 300}]
    assert str(suspended[0]) not in str(queued)
    assert states(world.a) == {one.pk: "ACTIVE"} and states(world.b) == {two.pk: "EXPIRED"}
    assert "inventory.expire_enqueue_failed" in caplog.text and "broker" not in caplog.text
    assert tasks.expire_reservations_sweep() == {"organizations": 2, "enqueued": 1, "failed": 1}


def test_the_task_needs_its_organization_and_the_schedule_exists() -> None:
    with pytest.raises(TypeError):  # sin organization_id no se encola
        tasks.expire_reservations.apply_async()
    with pytest.raises(ValueError, match="badly formed"):
        tasks.expire_reservations("no-es-un-uuid")
    entry = settings.CELERY_BEAT_SCHEDULE["inventory.expire_reservations_sweep"]
    assert (entry["task"], entry["schedule"]) == ("inventory.expire_reservations_sweep", 300.0)
    app.loader.import_default_modules()
    for task in (tasks.expire_reservations, tasks.expire_reservations_sweep):
        assert task.tenancy == "platform" and task.name in app.tasks
    assert entry["task"] in app.tasks and entry["options"] == {"expires": 300}
    assert SimpleNamespace(**entry).schedule == 300.0


def following(monkeypatch: pytest.MonkeyPatch, org: UUID, **limits: Any) -> list[dict[str, int]]:
    """Una pasada y las que encadena, una tras otra (el worker, aquí mismo)."""
    queued: list[dict[str, Any]] = []
    monkeypatch.setattr(tasks.expire_reservations, "apply_async", lambda **kw: queued.append(kw))
    seen = [run(org, **limits)]
    while queued and len(seen) < 20:
        seen.append(tasks.expire_reservations(**queued.pop(0)["kwargs"]))
    assert queued == []
    return seen


def failing(monkeypatch: pytest.MonkeyPatch, bad: set[UUID]) -> None:
    real = res.expire

    def flaky(tenant: Any, pk: UUID) -> bool:
        if pk in bad:
            raise RuntimeError("siempre")
        return real(tenant, pk)

    monkeypatch.setattr(res, "expire", flaky)


def test_a_busy_cell_costs_one_attempt_and_the_rest_of_the_run_goes_on(
    world: Any, migrator: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch,
    later: Callable[[float], None],
) -> None:  # fmt: skip
    sick, fine = stocked(world.a, 9, world.ana, "MALA"), stocked(world.a, 9, world.ana, "BUENA")
    bad = [reserve(world.a, world.ana, asked(sick, 1, hours=1))[0].pk for _ in range(5)]
    good = [reserve(world.a, world.ana, asked(fine, 1, hours=1))[0].pk for _ in range(2)]
    later(2)
    monkeypatch.setattr(tasks, "LOCK_TIMEOUT_MS", 200)
    lock = "SELECT 1 FROM inventory_levels WHERE variant_id = %s AND warehouse_id = %s FOR UPDATE"
    with migrator.transaction():  # el saldo de la celda, ocupado: 5 detrás, más que el tope de 3
        migrator.execute(lock, list(sick))
        assert following(monkeypatch, world.a, max_per_run=3) == [
            {**ZERO, "expired": 2, "failed": 1}
        ]
    assert {states(world.a)[pk] for pk in good} == {"EXPIRED"} and consistent(world.a, fine)[1] == 0
    assert consistent(world.a, sick) == (9, 5, 4) and releases(world.a, bad) == []
    assert run(world.a) == {**ZERO, "expired": 5} and consistent(world.a, sick) == (9, 0, 9)


def test_a_poison_row_does_not_hold_back_the_healthy_ones_of_its_cell(
    world: Any, monkeypatch: pytest.MonkeyPatch, later: Callable[[float], None]
) -> None:
    here = stocked(world.a, 4, world.ana)
    made = sorted(reserve(world.a, world.ana, asked(here, 1, hours=1))[0].pk for _ in range(2))
    later(2)
    with monkeypatch.context() as patch:  # la primera de la celda falla siempre, no por bloqueo
        failing(patch, {made[0]})
        assert run(world.a) == {**ZERO, "expired": 1, "failed": 1}  # la sana, en la misma pasada
        assert run(world.a) == {**ZERO, "failed": 1}  # cada barrido reintenta solo la mala
    assert states(world.a) == {made[0]: "ACTIVE", made[1]: "EXPIRED"}
    assert consistent(world.a, here) == (4, 1, 3) and len(releases(world.a, made)) == 1


def test_a_run_out_of_time_without_progress_does_not_chain(
    world: Any, migrator: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch,
    later: Callable[[float], None],
) -> None:  # fmt: skip
    one, two = stocked(world.a, 2, world.ana, "UNA"), stocked(world.a, 2, world.ana, "DOS")
    for where in (one, two):
        reserve(world.a, world.ana, asked(where, 1, hours=1))
    later(2)
    with monkeypatch.context() as patch, migrator.transaction():  # un bloqueo amplio: todo espera
        patch.setattr(tasks, "RUN_SECONDS", 0.001)  # la primera espera lo agota
        migrator.execute("SELECT 1 FROM inventory_levels FOR UPDATE")
        assert following(patch, world.a) == [{**ZERO, "failed": 1, "capped": 1}]
    assert consistent(world.a, one) == (2, 1, 1) and consistent(world.a, two) == (2, 1, 1)
    assert run(world.a) == {**ZERO, "expired": 2}  # el siguiente barrido


def test_persistent_failures_beyond_the_cap_do_not_stop_the_chain_and_the_chain_ends(
    world: Any, monkeypatch: pytest.MonkeyPatch, later: Callable[[float], None]
) -> None:
    cells = [stocked(world.a, 2, world.ana, f"V{i}") for i in range(4)]  # por variant_id
    made = [reserve(world.a, world.ana, asked(c, 1, hours=1))[0].pk for c in cells]
    later(2)
    with monkeypatch.context() as patch:  # las tres primeras celdas fallan siempre; tope 1
        failing(patch, set(made[:3]))
        seen = following(patch, world.a, max_per_run=1)
        failed = {**ZERO, "failed": 1, "capped": 1}
        assert seen == [failed, failed, failed, {**ZERO, "expired": 1}]  # la de detrás, vencida
        failing(patch, set(made))  # solo quedan filas que fallan: la cadena termina igual
        assert following(patch, world.a, max_per_run=1) == [failed, failed, {**ZERO, "failed": 1}]
    assert [states(world.a)[pk] for pk in made] == ["ACTIVE"] * 3 + ["EXPIRED"]
    assert run(world.a) == {**ZERO, "expired": 3}  # el siguiente barrido empieza sin cursor
    assert all(consistent(world.a, c) == (2, 0, 2) for c in cells)
    assert sorted(r[0] for r in releases(world.a, made)) == sorted(f"R/{pk}/release" for pk in made)


def test_a_cursor_from_a_message_only_chooses_where_to_start(
    world: Any, later: Callable[[float], None]
) -> None:
    here, there = stocked(world.a, 2, world.ana), stocked(world.b, 2, None)
    made = reserve(world.a, world.ana, asked(here, 1, hours=1))[0]
    other = reserve(world.b, None, asked(there, 1, hours=1))[0]
    later(2)
    for wrong in (["x"], [str(made.pk)] * 3, ["no-es", str(made.pk)], "ab"):
        with pytest.raises(ValueError):
            run(world.a, after=wrong)
    top = str(UUID(int=2**128 - 1))
    assert run(world.a, after=[top, top]) == ZERO  # tras todas: termina sin más
    assert run(world.a, after=[str(other.variant_id), str(other.pk)]) == ZERO  # la de B, tras A
    assert states(world.b) == {other.pk: "ACTIVE"} and consistent(world.b, there) == (2, 1, 1)
    assert states(world.a) == {made.pk: "ACTIVE"}  # saltada por el cursor, no perdida:
    assert run(world.a) == {**ZERO, "expired": 1}  # el barredor empieza sin cursor
    assert consistent(world.a, here) == (2, 0, 2) and len(releases(world.a, [made.pk])) == 1


def test_a_lock_on_the_reservation_row_itself_fails_that_cell_and_not_the_others(
    world: Any, migrator: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch,
    later: Callable[[float], None],
) -> None:  # fmt: skip
    free, busy = stocked(world.a, 3, world.ana, "LIBRE"), stocked(world.a, 3, world.ana, "OCUPADA")
    held = sorted(reserve(world.a, world.ana, asked(busy, 1, hours=1))[0].pk for _ in range(2))
    loose = reserve(world.a, world.ana, asked(free, 1, hours=1))[0].pk
    later(2)
    monkeypatch.setattr(tasks, "LOCK_TIMEOUT_MS", 200)
    with migrator.transaction():  # solo la fila de la reserva: la celda está libre
        migrator.execute("SELECT 1 FROM inventory_reservations WHERE id = %s FOR UPDATE", [held[0]])
        chained: list[Any] = []
        monkeypatch.setattr(tasks.expire_reservations, "apply_async", lambda **k: chained.append(k))
        assert run(world.a) == {**ZERO, "expired": 1, "failed": 1}  # la otra de la celda: sin
        assert chained == []  # cadena (tras el cursor, pero ocupada), al siguiente barrido
    assert states(world.a) == {held[0]: "ACTIVE", held[1]: "ACTIVE", loose: "EXPIRED"}
    assert consistent(world.a, busy) == (3, 2, 1) and consistent(world.a, free) == (3, 0, 3)
    assert run(world.a) == {**ZERO, "expired": 2} and consistent(world.a, busy) == (3, 0, 3)


def test_each_row_waits_at_most_its_share_of_what_is_left_of_the_run(
    world: Any, monkeypatch: pytest.MonkeyPatch, later: Callable[[float], None]
) -> None:
    started = time.monotonic()
    assert tasks._lock_wait_ms(started) == tasks.LOCK_TIMEOUT_MS
    assert 490 <= tasks._lock_wait_ms(started - tasks.RUN_SECONDS + 2) <= 500  # 2 s entre 4
    assert tasks._lock_wait_ms(started - 10 * tasks.RUN_SECONDS) == 1  # nunca 0: sin límite
    here = stocked(world.a, 2, world.ana)
    reserve(world.a, world.ana, asked(here, 1, hours=1))
    later(2)
    seen: list[str] = []
    real = res.expire

    def spy(tenant: Any, pk: UUID) -> bool:
        seen.append(connection.cursor().execute("SHOW lock_timeout").fetchone()[0])
        return real(tenant, pk)

    monkeypatch.setattr(res, "expire", spy)
    monkeypatch.setattr(tasks, "RUN_SECONDS", 2.0)
    assert run(world.a) == {**ZERO, "expired": 1} and consistent(world.a, here) == (2, 0, 2)
    assert len(seen) == 1 and 400 <= int(seen[0].removesuffix("ms")) <= 500


@pytest.mark.parametrize("job_first", [True, False])
def test_the_job_against_a_product_rename_on_the_same_variant_does_not_deadlock(
    world: Any, monkeypatch: pytest.MonkeyPatch, later: Callable[[float], None], job_first: bool
) -> None:
    here = stocked(world.a, 2, world.ana)
    made = reserve(world.a, world.ana, asked(here, 1, hours=1))[0]
    with tenant_scope(ctx(world.a)):
        product = ProductVariant.objects.get(pk=here[0]).product_id
    later(2)

    def rename() -> Any:
        with tenant_scope(ctx(world.a, world.ana)) as tenant:
            return update_product(tenant, product_id=product, name="Nuevo nombre").name

    where = (stock, "lock_cell") if job_first else (products, "variant_name")
    work = [job(world), rename]  # el renombrado bloquea el producto y sus variantes (NO KEY)
    outcome = overlap(monkeypatch, *(work if job_first else work[::-1]), where=where)
    by_job, by_rename = outcome if job_first else outcome[::-1]
    assert by_job == {**ZERO, "expired": 1} and by_rename == "Nuevo nombre"
    with tenant_scope(ctx(world.a)):
        assert ProductVariant.objects.get(pk=here[0]).name.startswith("Nuevo nombre")
    assert states(world.a) == {made.pk: "EXPIRED"} and consistent(world.a, here) == (2, 0, 2)


def test_the_sweep_rotates_so_a_backlogged_queue_serves_every_organization(
    world: Any, migrator: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    for count in range(1, 61):  # en `count` barridos seguidos, cada una va primera una vez
        assert sorted(tasks._start(t, count) for t in range(7, 7 + count)) == list(range(count))
    ranks = [tasks._start(t, 50) for t in range(50)]  # 10 servidas por barrido, de 50
    served = [{o for o in range(50) if (o - start) % 50 < 10} for start in ranks]
    assert all(set(range(50)) <= set().union(*served[t : t + 10]) for t in range(41))
    names = [f"org-{i}" for i in range(3)]
    for name in names:
        migrator.execute("INSERT INTO organizations (id, slug, name, status, created_at) VALUES "
                         "(uuidv7(), %s, %s, 'ACTIVE', now())", [name, name])  # fmt: skip
    first: list[dict[str, str]] = []
    monkeypatch.setattr(
        tasks.expire_reservations, "apply_async", lambda kwargs, **kw: first.append(kwargs)
    )
    try:
        active = Organization.objects.filter(status__in=ACCESSIBLE_STATUSES)
        everyone = {str(o) for o in active.values_list("pk", flat=True)}
        served_orgs = set()
        for tick in range(len(everyone)):  # el worker solo llega a la primera antes de que venzan
            moment = datetime.fromtimestamp(tick * tasks.SWEEP_SECONDS + 7, tz=UTC)
            monkeypatch.setattr(timezone, "now", lambda moment=moment: moment)
            first.clear()
            assert tasks.expire_reservations_sweep()["enqueued"] == len(everyone)
            assert {f["organization_id"] for f in first} == everyone  # todas, en otro orden
            served_orgs.add(first[0]["organization_id"])
    finally:
        migrator.execute("DELETE FROM organizations WHERE slug = ANY(%s)", [names])
    assert served_orgs == everyone

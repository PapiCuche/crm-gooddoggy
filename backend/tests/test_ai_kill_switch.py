"""F8-06 (#769): el kill switch de IA, global y por organización (ADR-034 §5, §10), contra la API
falsa de Anthropic (`tests.ai_sim`, sin red) y PostgreSQL con `crm_app`. Claves ficticias."""

import threading
import time
from functools import partial
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import psycopg
import pytest
from django.db import DatabaseError, connection
from django.test import Client
from psycopg import errors

from apps.access.selectors import AccessDenied
from apps.ai_gateway import accounts, gateway, keys, switch, turns
from apps.ai_gateway.llm import Outcome
from apps.audit.services import record as audit_record
from core.api.step_up import STEP_UP_REQUIRED
from core.tenancy.scope import tenant_scope
from tests import ai_sim, test_ai_gateway
from tests.ai_sim import TEST_KEY, FakeAnthropic
from tests.test_ai_gateway import ASK, budget, call, code
from tests.test_ai_provider_accounts import crm, real_stack
from tests.test_ai_provider_test_api import COUNTED
from tests.test_authorization import give
from tests.test_channel_accounts_api import signed
from tests.test_conversations_api import UNAUTHENTICATED
from tests.test_member_branch import DENIED
from tests.test_memberships import ctx, join
from tests.test_self_context import NOT_FOUND, reply
from tests.test_self_context import signed as plain
from tests.test_team_writes import send

world = test_ai_gateway.world
__all__ = ["crm", "real_stack", "world"]  # los fixtures, para pytest
pytestmark = pytest.mark.usefixtures("tenant_db")
DB = psycopg.Connection[Any]
BASE = "/api/v1/o/org-a/ai/switch/"
OFF, ON, YES = BASE + "off/", BASE + "on/", {"notice_confirmed": True}
SET = "UPDATE ai_switches SET enabled = %s WHERE organization_id = %s"
ROWS = "SELECT organization_id, enabled, changed_by_user_id FROM ai_switches ORDER BY 1"
HELD = "SELECT count(*) FROM ai_budget_reservations"
MEMBERSHIP = "SELECT id FROM organization_memberships WHERE user_id = %s AND organization_id = %s"


def audit(migrator: DB) -> list[tuple[Any, ...]]:
    return migrator.execute(
        "SELECT action, changes, metadata, actor_id, organization_id FROM audit_logs"
        " WHERE entity_type = 'ai_switch' ORDER BY occurred_at, id"
    ).fetchall()


def ahead(before: Any, real: Any) -> Any:
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        before()
        return real(*args, **kwargs)

    return wrapper


@pytest.fixture
def spied(world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Lo que el gateway llegó a hacer: validar, leer el modelo, abrir la clave o reservar."""
    done: list[str] = []
    for module, name in ((turns, "check"), (gateway, "_model"), (keys, "open_key"),
                         (gateway, "_reserve")):  # fmt: skip
        monkeypatch.setattr(module, name, ahead(partial(done.append, name), getattr(module, name)))
    return done


def test_global_off_refuses_before_validating_or_touching_the_database(
    world: SimpleNamespace, spied: list[str], migrator: DB, settings: Any
) -> None:
    budget(migrator, world.a, "10")
    for value in (False, None, "true", 1):  # solo `True` enciende
        settings.AI_ENABLED = value
        assert code(world) == Outcome.AI_DISABLED, value
    assert spied == [] and world.fake.calls == [] and migrator.execute(HELD).fetchone() == (0,)
    settings.AI_ENABLED = True  # con los dos encendidos, llama
    call(world)
    assert spied == ["check", "_model", "open_key", "_reserve"] and len(world.fake.calls) == 1


def test_the_organization_switch_is_off_without_a_row_or_when_off_and_leaves_the_others_alone(
    world: SimpleNamespace, spied: list[str], migrator: DB, settings: Any
) -> None:
    budget(migrator, world.a, "10")
    migrator.execute("DELETE FROM ai_switches WHERE organization_id = %s", [world.a])
    assert code(world) == Outcome.AI_DISABLED  # sin fila: apagada (falla cerrado)
    migrator.execute(test_ai_gateway.SWITCH_ON, [world.a, False])
    assert code(world) == Outcome.AI_DISABLED
    # Validó (sin base) y nada más: ni el modelo, ni la clave, ni la reserva, ni la llamada.
    assert spied == ["check"] * 2 and world.fake.calls == []
    assert migrator.execute(HELD).fetchone() == (0,)
    migrator.execute(SET, [True, world.a])  # sin caché: la siguiente ya llama
    call(world)
    migrator.execute(SET, [False, world.a])
    assert code(world) == Outcome.AI_DISABLED and len(world.fake.calls) == 1
    join(world.b, world.ana)  # B, encendida, sigue llamando; con el despliegue apagado, tampoco
    with tenant_scope(ctx(world.b, world.ana)) as tenant:
        keys.store(tenant, "ANTHROPIC", TEST_KEY)
    budget(migrator, world.b, "10")
    call(world, ASK, world.b)
    settings.AI_ENABLED = False
    assert code(world, world.b) == Outcome.AI_DISABLED and len(world.fake.calls) == 2


def later(target: Any) -> threading.Thread:  # otro hilo, su conexión; no retiene el proceso
    thread = threading.Thread(target=ahead(target, lambda: connection.close()), daemon=True)
    thread.start()
    return thread


def stopper(world: SimpleNamespace, migrator: DB) -> Any:
    """ana, con `ai.kill_switch`, apaga A por el comando."""
    [(membership,)] = migrator.execute(MEMBERSHIP, [world.ana.pk, world.a]).fetchall()
    give(world.a, membership, {"ai.kill_switch": None})

    def off() -> None:
        with tenant_scope(ctx(world.a, world.ana)) as tenant:
            switch.turn_off(tenant)

    return off


def test_turning_off_waits_for_the_reservation_in_flight_and_holds_the_next_call(
    world: SimpleNamespace, migrator: DB, monkeypatch: pytest.MonkeyPatch
) -> None:
    budget(migrator, world.a, "10")
    off, outcome = stopper(world, migrator), list[Outcome]()
    inside, gate = threading.Event(), threading.Event()

    def pause() -> None:  # avisa y espera a la puerta antes de seguir
        inside.set()
        gate.wait(10)

    monkeypatch.setattr(gateway, "_reserve", ahead(pause, gateway._reserve))
    first = later(lambda: call(world))
    assert inside.wait(10)
    with tenant_scope(ctx(world.a, world.ana)) as tenant, connection.cursor() as cursor:
        cursor.execute("SET LOCAL lock_timeout = '1s'")
        assert switch.is_on(tenant) is True  # otra llamada no espera a la que reserva
    stopping = later(off)
    stopping.join(0.5)
    assert stopping.is_alive()  # apagar espera a la reserva en curso...
    gate.set()
    for thread in (first, stopping):
        thread.join(10)
    [(result, cost)] = migrator.execute("SELECT outcome, cost_usd FROM ai_llm_calls").fetchall()
    assert result == "OK" and cost > 0  # ajustada con su costo real, ya apagada
    spent = migrator.execute("SELECT spent_usd, reserved_usd FROM ai_budget_usage").fetchone()
    assert spent == (cost, 0)
    migrator.execute(SET, [True, world.a])  # ...y quien llama durante un apagado, a él
    inside.clear()
    gate.clear()
    monkeypatch.setattr(switch, "record", ahead(pause, audit_record))
    stopping = later(off)
    assert inside.wait(10)
    following = later(lambda: outcome.append(code(world)))
    following.join(0.5)
    assert following.is_alive()
    gate.set()
    for thread in (stopping, following):
        thread.join(10)
    assert outcome == [Outcome.AI_DISABLED] and len(world.fake.calls) == 1


def test_turning_off_is_not_starved_by_a_stream_of_calls(world: Any, migrator: DB) -> None:
    budget(migrator, world.a, "1000")
    off, stop, seen = stopper(world, migrator), threading.Event(), set[Any]()

    def stream() -> None:
        while not stop.is_set():
            try:
                call(world)
            except Exception as error:  # noqa: BLE001 — solo `AI_DISABLED` vale
                seen.add(getattr(error, "outcome", type(error).__name__))

    callers = [later(stream) for _ in range(16)]
    for _ in range(400):  # en plena carga; `time.sleep` no espera aquí (`world`)
        if threading.Event().wait(0.05) or len(world.fake.calls) >= 32:
            break
    started = time.monotonic()
    later(off).join(10)  # acotado: si se queda sin turno, la prueba falla y no se cuelga
    took = time.monotonic() - started
    stop.set()
    for thread in callers:
        thread.join(30)
    assert took < 2 and seen <= {Outcome.AI_DISABLED} and len(world.fake.calls) >= 32, took
    assert code(world) == Outcome.AI_DISABLED


def test_the_probe_runs_with_the_organization_off_but_never_with_the_deployment_off(
    crm: SimpleNamespace, migrator: DB, monkeypatch: pytest.MonkeyPatch, settings: Any
) -> None:
    fake = ai_sim.install(monkeypatch, FakeAnthropic())
    with tenant_scope(ctx(crm.a, crm.ana)) as tenant:
        made = keys.store(tenant, "ANTHROPIC", TEST_KEY)
    ana, test = signed(crm.ana), f"/api/v1/o/org-a/ai/provider-accounts/{made.pk}/test/"
    opened: list[str] = []  # F8-06c: con el despliegue apagado, ni se abre la clave
    monkeypatch.setattr(keys, "open_key", ahead(partial(opened.append, "key"), keys.open_key))
    assert migrator.execute(ROWS).fetchall() == []  # la organización, apagada
    assert send(ana, "post", test, None).json() == {"result": "OK"}  # se prueba antes de encender
    settings.AI_ENABLED = False
    assert ana.get(BASE).json()["global_enabled"] is False
    refused = send(ana, "post", test, None)
    assert (refused.status_code, refused.json()["code"]) == (409, "AI_DISABLED")
    with tenant_scope(ctx(crm.a, crm.ana)) as tenant, pytest.raises(accounts.Disabled):
        accounts.check(tenant, provider="ANTHROPIC")
    assert len(fake.calls) == 1 and opened == ["key"]


def test_only_the_permissions_reach_the_switch_and_turning_off_needs_no_second_factor(
    crm: SimpleNamespace, migrator: DB
) -> None:
    """vera (Vendedora): ninguno. sam (Supervisor, `ai.kill_switch`): lee y apaga sin segundo
    factor, no enciende (`PERMISSION_DENIED`, nunca `STEP_UP_REQUIRED`). bea: solo `ai.manage`."""
    for method, path, body in (("get", BASE, None), ("post", OFF, None), ("post", ON, YES)):
        for vera in (plain(crm.vera), signed(crm.vera)):
            assert reply(send(vera, method, path, body)) == DENIED, path
        assert reply(send(Client(), method, path, body)) == UNAUTHENTICATED
    for sam in (plain(crm.sam), signed(crm.sam)):
        assert reply(send(sam, "post", ON, YES)) == DENIED
    late = send(plain(crm.ana), "post", ON, YES)  # Owner sin segundo factor reciente
    assert (late.status_code, late.json()["code"]) == (403, STEP_UP_REQUIRED)
    assert migrator.execute(ROWS).fetchall() == [] and audit(migrator) == []
    assert send(signed(crm.ana), "post", ON, YES).json()["enabled"] is True
    assert plain(crm.sam).get(BASE).json()["enabled"] is True
    assert send(plain(crm.sam), "post", OFF, None).json()["enabled"] is False
    give(crm.a, crm.m["bea"], {"ai.manage": None})
    assert send(plain(crm.bea), "post", OFF, None).status_code == 200
    on = lambda tenant: switch.turn_on(tenant, notice_confirmed=True)  # noqa: E731
    for who, command in ((crm.vera, switch.read), (crm.vera, switch.turn_off), (crm.sam, on)):
        with tenant_scope(ctx(crm.a, who)) as tenant, pytest.raises(AccessDenied):
            command(tenant)  # los comandos lo comprueban también


def test_turning_on_and_off_is_audited_once_with_before_after_and_the_notice(
    crm: SimpleNamespace, migrator: DB
) -> None:
    ana, sam = signed(crm.ana), plain(crm.sam)
    assert ana.get(BASE).json() == {"enabled": False, "global_enabled": True, "changed_at": None}
    assert send(sam, "post", OFF, None).json()["enabled"] is False  # ya apagada: nada que auditar
    on = send(ana, "post", ON, YES | {"organization_id": str(crm.b)})  # se ignora
    assert on.status_code == 200 and on.json()["enabled"] is True
    assert send(ana, "post", ON, YES).status_code == 200  # ya encendida: nada
    for _ in range(2):
        assert send(sam, "post", OFF, None).json()["enabled"] is False
    assert migrator.execute(ROWS).fetchall() == [(crm.a, False, crm.sam.pk)]
    assert audit(migrator) == [
        ("ai.kill_switch.deactivated", {"enabled": [False, True]}, {"notice_confirmed": True},
         crm.ana.pk, crm.a),
        ("ai.kill_switch.activated", {"enabled": [True, False]}, {}, crm.sam.pk, crm.a),
    ]  # fmt: skip


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"notice_confirmed": False},
        {"notice_confirmed": "true"},
        {"notice_confirmed": 1},
        {"notice_confirmed": None},
        [True],
    ],
)
def test_turning_on_needs_the_notice_confirmed_exactly(crm: Any, migrator: DB, body: Any) -> None:
    assert send(signed(crm.ana), "post", ON, body).status_code == 400
    assert migrator.execute(ROWS).fetchall() == [] and audit(migrator) == []
    with tenant_scope(ctx(crm.a, crm.ana)) as tenant, pytest.raises(switch.NoticeNotConfirmed):
        switch.turn_on(tenant, notice_confirmed=False)


def test_each_organization_has_its_own_switch_and_crm_app_cannot_reach_another(
    crm: SimpleNamespace, migrator: DB
) -> None:
    give(crm.b, crm.m_luis, {"ai.manage": None})
    luis, b = signed(crm.luis), "/api/v1/o/org-b/ai/switch/"
    assert send(signed(crm.ana), "post", ON, YES).status_code == 200
    assert luis.get(b).json()["enabled"] is False  # la de A no existe para B
    assert reply(luis.get(BASE)) == NOT_FOUND and reply(send(luis, "post", OFF, None)) == NOT_FOUND
    assert send(luis, "post", b + "on/", YES).status_code == 200
    assert send(luis, "post", b + "off/", None).status_code == 200
    both = {(crm.a, True), (crm.b, False)}
    assert {(o, e) for o, e, _ in migrator.execute(ROWS).fetchall()} == both
    with tenant_scope(ctx(crm.a)), connection.cursor() as cursor:
        cursor.execute("SELECT organization_id FROM ai_switches")
        assert cursor.fetchall() == [(crm.a,)]  # RLS con FORCE
    for sql, args in (
        ("DELETE FROM ai_switches", []), ("TRUNCATE ai_switches", []),
        ("UPDATE ai_switches SET organization_id = %s", [crm.a]),  # sin permiso de columna
        ("UPDATE ai_switches SET id = uuidv7()", []),
        ("INSERT INTO ai_switches (organization_id, changed_at) VALUES (%s, now())", [crm.b]),
    ):  # fmt: skip
        with pytest.raises(DatabaseError), tenant_scope(ctx(crm.a)), connection.cursor() as cur:
            cur.execute(sql, args)
    with tenant_scope(ctx(crm.a)), connection.cursor() as cursor:  # B, intacta
        cursor.execute("UPDATE ai_switches SET enabled = true")
    assert {(o, e) for o, e, _ in migrator.execute(ROWS).fetchall()} == both


def test_crm_app_cannot_choose_the_id_of_a_switch(orgs: dict[str, UUID], migrator: DB) -> None:
    """F8-06c: solo las columnas concedidas (0009); el `id` lo pone la base, nunca `crm_app`."""
    insert = "INSERT INTO ai_switches ({}organization_id, changed_at) VALUES ({}%s, now())"
    with pytest.raises(DatabaseError) as refused, tenant_scope(ctx(orgs["A"])):
        with connection.cursor() as cursor:
            cursor.execute(insert.format("id, ", "uuidv7(), "), [orgs["A"]])
    assert isinstance(refused.value.__cause__, errors.InsufficientPrivilege)  # no otra cosa
    with tenant_scope(ctx(orgs["A"])), connection.cursor() as cursor:  # sin el `id`, entra
        cursor.execute(insert.format("", ""), [orgs["A"]])
    assert migrator.execute(ROWS).fetchall() == [(orgs["A"], False, None)]


def test_the_advisory_lock_is_per_organization(orgs: dict[str, UUID], migrator: DB) -> None:
    """F8-06c: con A apagándose (exclusivo, otra conexión), B toma el suyo sin esperar; A, no."""
    a, b = orgs["A"], orgs["B"]
    migrator.execute(test_ai_gateway.SWITCH_ON, [b, True])
    with migrator.transaction():
        migrator.execute(switch._EXCLUSIVE, [a, switch.LOCK_SALT])
        with tenant_scope(ctx(b)) as tenant, connection.cursor() as cursor:
            cursor.execute("SET LOCAL lock_timeout = '1s'")
            assert switch.is_on(tenant) is True
        with pytest.raises(DatabaseError) as waited, tenant_scope(ctx(a)) as tenant:
            with connection.cursor() as cursor:
                cursor.execute("SET LOCAL lock_timeout = '1s'")
                switch.is_on(tenant)
    assert isinstance(waited.value.__cause__, errors.LockNotAvailable)


def test_turning_off_is_never_limited_and_turning_on_counts_in_the_write_limit(
    crm: SimpleNamespace, migrator: DB, settings: Any
) -> None:
    settings.AI_ACCOUNT_THROTTLE = (2, 600)
    ana = signed(crm.ana)
    assert [send(ana, "post", ON, YES).status_code for _ in range(3)] == [200, 200, 429]
    assert [send(ana, "post", OFF, None).status_code for _ in range(3)] == [200] * 3
    assert ana.get(BASE).status_code == 200 and send(ana, "post", ON, YES).status_code == 429
    assert migrator.execute(COUNTED).fetchall() == [(f"aik:usr:{crm.ana.pk}", 4)]
    assert len(audit(migrator)) == 2  # encendida y apagada

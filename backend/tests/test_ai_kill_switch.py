"""F8-06 (#769): el kill switch de IA, global (`AI_ENABLED`) y por organización (`ai_switches`;
ADR-034 §5, §10), contra la API falsa de Anthropic (`tests.ai_sim`, sin red) y PostgreSQL con
`crm_app`. Claves ficticias."""

from types import SimpleNamespace
from typing import Any

import psycopg
import pytest
from django.db import DatabaseError, connection
from django.test import Client
from rest_framework.exceptions import NotFound

from apps.access import permissions
from apps.access.apps import extend_owner_roles
from apps.access.catalog import BY_CODE, ROLE_TEMPLATES
from apps.access.permissions import AnyOf, HasPermission
from apps.access.selectors import AccessDenied
from apps.ai_gateway import accounts, gateway, keys, switch, turns
from apps.ai_gateway.llm import Outcome
from core.api.step_up import STEP_UP_REQUIRED
from core.tenancy.scope import tenant_scope
from tests import ai_sim, test_ai_gateway
from tests.ai_sim import TEST_KEY, FakeAnthropic
from tests.conftest import migrator_conninfo
from tests.test_access import as_migrator
from tests.test_ai_gateway import ASK, USAGE, budget, call, code
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


def audit(migrator: DB) -> list[tuple[Any, ...]]:
    return migrator.execute(
        "SELECT action, changes, metadata, actor_id, organization_id FROM audit_logs"
        " WHERE entity_type = 'ai_switch' ORDER BY occurred_at, id"
    ).fetchall()


@pytest.fixture
def spied(world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Lo que el gateway llegó a hacer: validar, leer el modelo, abrir la clave o reservar."""
    done: list[str] = []

    def spy(name: str, real: Any) -> Any:
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            done.append(name)
            return real(*args, **kwargs)

        return wrapper

    for module, name in ((turns, "check"), (gateway, "_model"), (keys, "open_key"),
                         (gateway, "_reserve")):  # fmt: skip
        monkeypatch.setattr(module, name, spy(name, getattr(module, name)))
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


def test_the_switch_read_holds_the_row_until_the_reservation_commits(world: Any) -> None:
    """Apagar espera a la reserva que lo leyó encendido: tras el 200, ninguna lo ve encendido."""
    lock = "SELECT 1 FROM ai_switches WHERE organization_id = %s FOR UPDATE NOWAIT"
    with psycopg.connect(migrator_conninfo(), autocommit=True) as other:
        with tenant_scope(ctx(world.a, world.ana)) as tenant:
            assert switch.is_on(tenant) is True
            with pytest.raises(psycopg.errors.LockNotAvailable):
                other.execute(lock, [world.a])
        assert other.execute(lock, [world.a]).fetchone() == (1,)  # confirmada: libre
        with tenant_scope(ctx(world.b, world.ana)) as tenant:  # B no bloquea la de A
            assert switch.is_on(tenant) is True
            assert other.execute(lock, [world.a]).fetchone() == (1,)


def test_switching_off_cuts_the_next_call_and_the_reserved_one_still_settles_its_real_cost(
    world: SimpleNamespace, migrator: DB, monkeypatch: pytest.MonkeyPatch
) -> None:
    budget(migrator, world.a, "10")
    adapter = gateway.ADAPTERS["ANTHROPIC"]
    real = adapter.send

    def off_meanwhile(*args: Any, **kwargs: Any) -> Any:  # otra persona apaga durante la llamada
        migrator.execute(SET, [False, world.a])
        return real(*args, **kwargs)

    monkeypatch.setattr(adapter, "send", off_meanwhile)
    assert call(world).usage.input_tokens == USAGE["input_tokens"]
    [(outcome, cost)] = migrator.execute("SELECT outcome, cost_usd FROM ai_llm_calls").fetchall()
    assert outcome == "OK" and cost > 0  # ajustada con su costo real, ya apagada
    spent = migrator.execute("SELECT spent_usd, reserved_usd FROM ai_budget_usage").fetchone()
    assert spent == (cost, 0)
    assert code(world) == Outcome.AI_DISABLED and len(world.fake.calls) == 1


def test_the_probe_runs_with_the_organization_off_but_never_with_the_deployment_off(
    crm: SimpleNamespace, migrator: DB, monkeypatch: pytest.MonkeyPatch, settings: Any
) -> None:
    fake = ai_sim.install(monkeypatch, FakeAnthropic())
    with tenant_scope(ctx(crm.a, crm.ana)) as tenant:
        made = keys.store(tenant, "ANTHROPIC", TEST_KEY)
    ana, test = signed(crm.ana), f"/api/v1/o/org-a/ai/provider-accounts/{made.pk}/test/"
    assert migrator.execute(ROWS).fetchall() == []  # la organización, apagada
    assert send(ana, "post", test, None).json() == {"result": "OK"}  # se prueba antes de encender
    settings.AI_ENABLED = False
    assert ana.get(BASE).json()["global_enabled"] is False
    refused = send(ana, "post", test, None)
    assert (refused.status_code, refused.json()["code"]) == (409, "AI_DISABLED")
    with tenant_scope(ctx(crm.a, crm.ana)) as tenant, pytest.raises(accounts.Disabled):
        accounts.check(tenant, provider="ANTHROPIC")
    assert len(fake.calls) == 1


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


def test_born_with_owner_admin_and_supervisor_and_existing_organizations_only_owner(
    crm: SimpleNamespace, migrator: DB
) -> None:
    """D-F8-08 (#769). Una organización de antes no lo tiene: el despliegue lo da solo al Owner."""
    assert BY_CODE["ai.kill_switch"].is_sensitive and not BY_CODE["ai.kill_switch"].supports_scope
    born = {template.code for template in ROLE_TEMPLATES if "ai.kill_switch" in template.grants}
    assert born == {"owner", "admin", "supervisor"}
    grants = (
        "SELECT r.code FROM role_permissions g JOIN roles r ON r.id = g.role_id"
        " WHERE g.permission_code = 'ai.kill_switch' AND r.organization_id = %s ORDER BY 1"
    )
    assert [c for (c,) in migrator.execute(grants, [crm.a]).fetchall()] == sorted(born)
    migrator.execute("DELETE FROM role_permissions WHERE permission_code = 'ai.kill_switch'")
    assert as_migrator(lambda: extend_owner_roles("default")) == 1
    assert migrator.execute(grants, [crm.a]).fetchall() == [("owner",)]
    assert reply(send(plain(crm.sam), "post", OFF, None)) == DENIED  # hasta que el Owner lo dé


TWO = ("a.x", "b.y")  # basta uno con `AnyOf`; una tupla los sigue exigiendo todos; vacío, nada


@pytest.mark.parametrize(("declared", "held", "passes"), [
    (AnyOf(TWO), {"a.x"}, True), (AnyOf(TWO), {"b.y"}, True), (AnyOf(TWO), set(), False),
    (AnyOf(()), {"a.x"}, False), (TWO, {"a.x"}, False), (TWO, set(TWO), True),
    ("a.x", {"a.x"}, True),
])  # fmt: skip
def test_any_of_needs_one_code_and_a_plain_tuple_still_needs_all(
    monkeypatch: pytest.MonkeyPatch, declared: Any, held: set[str], passes: bool
) -> None:
    monkeypatch.setattr(permissions, "request_context", lambda request: object())
    monkeypatch.setattr(permissions, "has_permission", lambda ectx, code: code in held)
    monkeypatch.setattr(permissions, "can", lambda ectx, code, obj: code in held)
    view, request = SimpleNamespace(required_permissions={"GET": declared}), SimpleNamespace(
        method="GET")  # fmt: skip
    assert HasPermission().has_permission(request, view) is passes
    if isinstance(declared, AnyOf) and declared and 0 < len(held) < len(declared):
        with pytest.raises(NotFound):  # en el objeto, `AnyOf` es una tupla: los exige todos
            HasPermission().has_object_permission(request, view, object())  # type: ignore[arg-type]

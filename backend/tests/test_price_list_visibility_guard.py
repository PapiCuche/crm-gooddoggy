"""F8-07g (#1017): la base sostiene quién cambia quién ve una lista de precios. `crm_app` con SQL a
mano dentro del scope de su organización: cambiar `required_permission` o `kind` pide una persona
activa con `prices.manage` (`pricing/0010`), para que la guarda de `ai_exposable` (`0009`) no se
rodee en dos sentencias. La API (`PATCH`), el migrador y crear listas siguen igual."""

from types import SimpleNamespace
from typing import Any

import pytest
from django.core.management import call_command
from django.db import ProgrammingError

from apps.pricing.models import PriceList
from core.tenancy.context import ActorType, TenantContext
from core.tenancy.scope import tenant_scope
from tests.test_authorization import give
from tests.test_channel_accounts_api import signed
from tests.test_contacts_api import crm, real_stack
from tests.test_memberships import ctx, raw
from tests.test_pipelines_tables import as_migrator
from tests.test_price_list_ai_exposable import DB, LISTS, expose, exposed, shop
from tests.test_price_list_ai_exposable_guard import SET, done, refused
from tests.test_team_writes import send

__all__ = ["crm", "real_stack", "shop"]  # los fixtures, para pytest
pytestmark = pytest.mark.usefixtures("tenant_db")
GUARD = "price_lists_visibility_guard"
OPEN, HIDE = "required_permission = 'prices.view'", "required_permission = 'prices.view_wholesale'"
INTERNAL = "prices.view_internal"
STATE = "SELECT code, kind, required_permission, ai_exposable FROM price_lists WHERE id = %s"


def state(migrator: DB, listed: Any) -> tuple[Any, ...]:
    found = migrator.execute(STATE, [listed]).fetchone()
    assert found is not None
    return tuple(found)


def test_the_two_statement_attacks_on_the_ai_guard_fail(
    shop: SimpleNamespace, migrator: DB
) -> None:
    """vera (`ai.manage`, solo `prices.view`): abrir MAYORISTA, marcarla y cerrarla; o marcar
    REGULAR y cerrarla después. Ni por el tipo, ni volviéndola interna al desmarcarla."""
    regular, wholesale = shop.lists["regular"], shop.lists["wholesale"]
    give(shop.a, shop.m["vera"], {"ai.manage": None})
    vera = ctx(shop.a, shop.vera)
    refused(vera, OPEN, wholesale, GUARD)
    refused(vera, "ai_exposable = true", wholesale)  # sigue sin verla: la guarda de 0009
    assert state(migrator, wholesale) == (
        "MAYORISTA", "RETAIL", "prices.view_wholesale", False
    )  # fmt: skip
    done(vera, "ai_exposable = true", regular)  # la ve: la marca, como `set_ai_exposable`
    for hide in (HIDE, "kind = 'WHOLESALE'", "kind = 'INTERNAL', ai_exposable = false"):
        refused(vera, hide, regular, GUARD)
    assert state(migrator, regular) == ("REGULAR", "RETAIL", "prices.view", True)
    assert exposed(migrator) == ["REGULAR"]


def test_only_an_active_person_with_prices_manage_changes_who_sees_a_list(
    shop: SimpleNamespace, migrator: DB
) -> None:
    """Ni el sistema, ni la IA con el id del Owner, ni sam (ve MAYORISTA, no gestiona precios);
    bea (`prices.manage`) sí, por el permiso o por el tipo; suspendida o desactivada, no."""
    regular, wholesale = shop.lists["regular"], shop.lists["wholesale"]
    outsiders = (
        ctx(shop.a),
        ctx(shop.a, shop.sam),
        TenantContext(shop.a, "test", shop.ana.pk, ActorType.AI_AGENT, shop.ana.pk),
    )
    for context in outsiders:
        refused(context, OPEN, wholesale, GUARD)
        refused(context, "kind = 'WHOLESALE'", regular, GUARD)
    done(ctx(shop.a, shop.sam), "name = 'Por mayor'", wholesale)  # lo demás no lo mira
    bea = ctx(shop.a, shop.bea)
    member, user = "organization_memberships SET status", "users SET is_active"
    for broken, fixed, target in (
        (f"{member} = 'SUSPENDED'", f"{member} = 'ACTIVE'", shop.m["bea"]),
        (f"{user} = false", f"{user} = true", shop.bea.pk),
    ):
        migrator.execute(f"UPDATE {broken} WHERE id = %s", [target])
        refused(bea, OPEN, wholesale, GUARD)
        migrator.execute(f"UPDATE {fixed} WHERE id = %s", [target])
    done(bea, f"{OPEN}, kind = 'DISTRIBUTOR'", wholesale)
    done(bea, "kind = 'WHOLESALE'", regular)
    assert state(migrator, wholesale) == ("MAYORISTA", "DISTRIBUTOR", "prices.view", False)
    assert state(migrator, regular) == ("REGULAR", "WHOLESALE", "prices.view", False)
    migrator.execute(SET.format(HIDE), [regular])  # el migrador, propietario, no pasa por ella
    assert state(migrator, regular)[2] == "prices.view_wholesale"


def test_prices_manage_sees_every_list_also_for_the_ai_guard(
    shop: SimpleNamespace, migrator: DB
) -> None:
    """bea (`ai.manage` y `prices.manage`, sin `prices.view_wholesale`) marca MAYORISTA tal como
    está, con SQL y por la API: `prices.manage` cuenta en la visibilidad previa de `0009`."""
    wholesale = shop.lists["wholesale"]
    give(shop.a, shop.m["bea"], {"ai.manage": None})
    done(ctx(shop.a, shop.bea), "ai_exposable = true", wholesale)
    assert exposed(migrator) == ["MAYORISTA"]
    migrator.execute(SET.format("ai_exposable = false"), [wholesale])
    assert expose(signed(shop.bea), wholesale, {"ai_exposable": True}).status_code == 200
    assert exposed(migrator) == ["MAYORISTA"]


def test_the_app_paths_still_change_who_sees_a_list(shop: SimpleNamespace, migrator: DB) -> None:
    """`PATCH` de bea (`prices.manage`, no Owner) y del Owner; volverla interna la desmarca en la
    misma escritura (las dos guardas); crear una lista de cualquier tipo, también sin persona."""
    regular, wholesale = shop.lists["regular"], shop.lists["wholesale"]
    migrator.execute(SET.format("ai_exposable = true"), [regular])

    def patch(who: Any, listed: Any, body: dict[str, str]) -> Any:
        return send(signed(who), "patch", f"{LISTS}{listed}/", body)

    opened = patch(shop.bea, wholesale, {"required_permission": "prices.view", "kind": "RETAIL"})
    assert opened.status_code == 200, opened.json()
    assert state(migrator, wholesale) == ("MAYORISTA", "RETAIL", "prices.view", False)
    hidden = patch(shop.ana, regular, {"kind": "INTERNAL", "required_permission": INTERNAL})
    assert hidden.status_code == 200, hidden.json()
    assert state(migrator, regular) == ("REGULAR", "INTERNAL", INTERNAL, False)
    values = {"name": "Nueva", "currency": "PEN", "kind": "DISTRIBUTOR"}
    with tenant_scope(ctx(shop.a)):  # crear no pasa por la guarda
        PriceList.objects.create(code="NUEVA", required_permission=INTERNAL, **values)


def test_the_runtime_cannot_switch_it_off_and_the_reverse_removes_it(
    shop: SimpleNamespace, migrator: DB
) -> None:
    escapes = (
        f"ALTER TABLE price_lists DISABLE TRIGGER {GUARD}",
        f"DROP TRIGGER {GUARD} ON price_lists",
        f"DROP FUNCTION {GUARD}()",
        f"CREATE OR REPLACE FUNCTION {GUARD}() RETURNS trigger LANGUAGE plpgsql"
        " AS 'BEGIN RETURN NEW; END'",
    )
    for sql in escapes:
        with (
            pytest.raises(ProgrammingError, match="must be owner|permission denied"),
            tenant_scope(ctx(shop.a, shop.ana)),
        ):
            raw(sql)
    function = (
        "SELECT p.prosecdef, p.proconfig, t.tgenabled, t.tgtype FROM pg_trigger t"
        " JOIN pg_proc p ON p.oid = t.tgfoid WHERE t.tgname = %s"
    )
    # INVOKER, `search_path` fijo, siempre activo, BEFORE UPDATE FOR EACH ROW (1+2+16)
    found = migrator.execute(function, [GUARD]).fetchone()
    assert found == (False, ["search_path=pg_catalog, public, pg_temp"], "O", 19)
    installed = "SELECT count(*) FROM pg_proc WHERE proname = %s"
    try:
        with as_migrator():
            call_command("migrate", "pricing", "0009", verbosity=0)
        assert migrator.execute(function, [GUARD]).fetchone() is None
        assert migrator.execute(installed, [GUARD]).fetchone() == (0,)
        done(ctx(shop.a), OPEN, shop.lists["wholesale"])  # sin ella, como antes
    finally:
        with as_migrator():
            call_command("migrate", "pricing", verbosity=0)
    refused(ctx(shop.a), HIDE, shop.lists["wholesale"], GUARD)

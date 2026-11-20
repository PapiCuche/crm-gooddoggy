"""F8-07f (#1010): la base sostiene qué listas de precios cita la IA (D-F8-06, ADR-034 §13).
`crm_app` con SQL a mano dentro del scope de su organización: `ai_exposable` solo cambia si quien
actúa es una persona con `ai.manage` que ve la lista, una lista nace sin marcar y una interna nunca
queda marcada (el CHECK, para todo rol). `pricing/0009` apaga las internas marcadas, solo esas."""

from importlib import import_module
from types import SimpleNamespace
from uuid import UUID

import psycopg
import pytest
from django.core.management import call_command
from django.db import IntegrityError, ProgrammingError, connection

from apps.pricing.models import PriceList
from core.tenancy.context import ActorType, TenantContext
from core.tenancy.scope import tenant_scope
from tests.test_authorization import give
from tests.test_channel_accounts_api import signed
from tests.test_contacts_api import crm, real_stack
from tests.test_memberships import ctx, raw
from tests.test_pipelines_tables import as_migrator
from tests.test_price_list_ai_exposable import DB, audit, expose, exposed, shop

__all__ = ["crm", "real_stack", "shop"]  # los fixtures, para pytest
pytestmark = pytest.mark.usefixtures("tenant_db")
GUARD, INTERNAL_CK = "price_lists_ai_exposable_guard", "price_lists_internal_not_ai_ck"
SET = "UPDATE price_lists SET {} WHERE id = %s"
MARK = "UPDATE price_lists SET ai_exposable = true WHERE id = ANY(%s)"
INTERNAL = ("kind = 'INTERNAL'", "required_permission = 'prices.view_internal'")


def refused(context: TenantContext, change: str, listed: UUID, match: str = GUARD) -> None:
    with pytest.raises(IntegrityError, match=match), tenant_scope(context):
        raw(SET.format(change), [listed])


def done(context: TenantContext, change: str, listed: UUID) -> None:
    with tenant_scope(context):
        assert raw(SET.format(change), [listed]) == [(1,)]


def test_only_a_member_with_ai_manage_who_sees_the_list_changes_it_with_sql(
    shop: SimpleNamespace, migrator: DB
) -> None:
    """Ni el sistema, ni `prices.manage`, ni la IA con el id del Owner; vera (`ai.manage`, solo
    `prices.view`) cambia REGULAR pero no MAYORISTA; suspendida o desactivada, tampoco."""
    regular, wholesale = shop.lists["regular"], shop.lists["wholesale"]
    give(shop.a, shop.m["vera"], {"ai.manage": None})  # en un rol aparte de su `prices.view`
    vera = ctx(shop.a, shop.vera)
    outsiders = (
        ctx(shop.a),
        ctx(shop.a, shop.bea),
        ctx(shop.a, shop.sam),
        TenantContext(shop.a, "test", shop.ana.pk, ActorType.AI_AGENT, shop.ana.pk),
    )
    for value, before in (("true", []), ("false", ["MAYORISTA", "REGULAR"])):
        for context in outsiders:
            refused(context, f"ai_exposable = {value}", regular)
        refused(vera, f"ai_exposable = {value}", wholesale)  # no la ve
        assert exposed(migrator) == before
        done(vera, f"ai_exposable = {value}", regular)
        done(ctx(shop.a, shop.ana), f"ai_exposable = {value}", wholesale)  # el Owner ve todas
    member, user = "organization_memberships SET status", "users SET is_active"
    for broken, fixed, target in (
        (f"{member} = 'SUSPENDED'", f"{member} = 'ACTIVE'", shop.m["vera"]),
        (f"{user} = false", f"{user} = true", shop.vera.pk),
    ):
        migrator.execute(f"UPDATE {broken} WHERE id = %s", [target])
        refused(vera, "ai_exposable = true", regular)
        migrator.execute(f"UPDATE {fixed} WHERE id = %s", [target])
    done(vera, "ai_exposable = true", regular)
    assert exposed(migrator) == ["REGULAR"] and audit(migrator) == []  # SQL: sin auditoría
    assert expose(signed(shop.ana), regular, {"ai_exposable": False}).status_code == 200
    assert exposed(migrator) == [] and len(audit(migrator)) == 1  # la API, con su auditoría


def test_a_list_is_born_unmarked_even_for_the_owner(shop: SimpleNamespace, migrator: DB) -> None:
    values = {
        "name": "Nueva",
        "kind": "RETAIL",
        "currency": "PEN",
        "required_permission": "prices.view",
    }
    with pytest.raises(IntegrityError, match=GUARD), tenant_scope(ctx(shop.a, shop.ana)):
        PriceList.objects.create(code="MARCADA", ai_exposable=True, **values)
    with tenant_scope(ctx(shop.a, shop.ana)):
        PriceList.objects.create(code="NUEVA", **values)  # sin marcar, sí
    assert exposed(migrator) == []


def test_a_marked_list_becomes_internal_only_if_the_same_write_unmarks_it(
    shop: SimpleNamespace, migrator: DB
) -> None:
    """Por el tipo o por quién la ve, para todo rol; con la misma escritura que la desmarca, quien
    la edita sin `ai.manage` (el camino de `update_price_list`); desmarcarla sin más, no."""
    regular, wholesale = shop.lists["regular"], shop.lists["wholesale"]
    migrator.execute(MARK, [[regular, wholesale]])
    bea = ctx(shop.a, shop.bea)
    for change in INTERNAL:
        for context in (bea, ctx(shop.a, shop.ana)):
            refused(context, change, regular, INTERNAL_CK)
        with pytest.raises(psycopg.errors.CheckViolation, match=INTERNAL_CK):
            migrator.execute(SET.format(change), [regular])
    refused(bea, "ai_exposable = false", regular)
    assert exposed(migrator) == ["MAYORISTA", "REGULAR"]
    for listed, change in zip((regular, wholesale), INTERNAL, strict=True):
        done(bea, f"{change}, ai_exposable = false", listed)
    assert exposed(migrator) == []


def test_the_runtime_cannot_switch_the_guard_off(shop: SimpleNamespace, migrator: DB) -> None:
    escapes = (
        f"ALTER TABLE price_lists DISABLE TRIGGER {GUARD}",
        f"DROP TRIGGER {GUARD} ON price_lists",
        f"DROP FUNCTION {GUARD}()",
        f"CREATE OR REPLACE FUNCTION {GUARD}() RETURNS trigger LANGUAGE plpgsql"
        " AS 'BEGIN RETURN NEW; END'",
        f"ALTER TABLE price_lists DROP CONSTRAINT {INTERNAL_CK}",
        "SET session_replication_role = replica",
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
    # INVOKER, `search_path` fijo, siempre activo, BEFORE INSERT OR UPDATE FOR EACH ROW (1+2+4+16)
    found = migrator.execute(function, [GUARD]).fetchone()
    assert found == (False, ["search_path=pg_catalog, public, pg_temp"], "O", 23)


def test_the_migration_turns_off_only_the_marked_internal_lists_and_audits_them(
    shop: SimpleNamespace, migrator: DB
) -> None:
    """Las demás marcadas (también de otra organización) siguen citables; hacia atrás no marca
    nada y quita el disparador y el CHECK; repetirla no audita más; sin `BYPASSRLS`, se niega."""
    on = ["AJENA", "INTERNA", "MAYORISTA", "REGULAR", "STAFF"]
    installed = (
        "SELECT (SELECT count(*) FROM pg_trigger WHERE tgname = %s),"
        " (SELECT count(*) FROM pg_constraint WHERE conname = %s)"
    )
    try:
        with as_migrator():
            call_command("migrate", "pricing", "0008", verbosity=0)
        assert migrator.execute(installed, [GUARD, INTERNAL_CK]).fetchone() == (0, 0)
        migrator.execute("UPDATE price_lists SET ai_exposable = true WHERE code = ANY(%s)", [on])
        with as_migrator():
            call_command("migrate", "pricing", verbosity=0)
        assert exposed(migrator) == ["AJENA", "MAYORISTA", "REGULAR"]
        off, why = {"ai_exposable": [True, False]}, {"reason": "INTERNAL_LIST"}
        rows = sorted(row[1:] for row in audit(migrator))
        internal = sorted((shop.lists["internal"], shop.lists["internal_kind"]))
        assert rows == [(listed, off, why, "SYSTEM", None, shop.a) for listed in internal]
        assert migrator.execute(installed, [GUARD, INTERNAL_CK]).fetchone() == (1, 1)
        with as_migrator():
            call_command("migrate", "pricing", "0008", verbosity=0)
            assert exposed(migrator) == ["AJENA", "MAYORISTA", "REGULAR"]  # no-op hacia atrás
            assert migrator.execute(installed, [GUARD, INTERNAL_CK]).fetchone() == (0, 0)
            call_command("migrate", "pricing", verbosity=0)
        assert len(audit(migrator)) == 2  # nada que apagar: nada que auditar
        module = import_module("apps.pricing.migrations.0009_ai_exposable_guard")
        with connection.schema_editor() as editor, pytest.raises(RuntimeError, match="BYPASSRLS"):
            module.internal_off(None, editor)  # `crm_app`: RLS no le deja ver nada
    finally:
        with as_migrator():
            call_command("migrate", "pricing", verbosity=0)

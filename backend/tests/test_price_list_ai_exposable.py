"""F8-07b (#996): solo el propietario decide qué listas de precios puede citar la IA (D-F8-06,
ADR-034 §6, §10, §13). `PUT …/price-lists/{id}/ai-exposable/` con `ai.manage`, segundo factor
reciente, el tope por persona y auditoría; `POST` y `PATCH` ya no lo aceptan; `pricing/0008` lo
apaga en todas. Middleware, sesión, motor de autorización y PostgreSQL con el rol `crm_app`."""

from importlib import import_module
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from django.core.management import call_command
from django.db import connection
from django.test import Client
from django.utils import timezone

from apps.access.selectors import AccessDenied
from apps.pricing import price_lists as commands
from apps.pricing import services
from apps.pricing.models import PriceList
from core.api.step_up import STEP_UP_REQUIRED
from core.tenancy.scope import tenant_scope
from tests.test_ai_provider_test_api import COUNTED
from tests.test_authorization import give
from tests.test_channel_accounts_api import signed
from tests.test_contacts_api import crm, real_stack
from tests.test_conversations_api import UNAUTHENTICATED
from tests.test_member_branch import DENIED
from tests.test_memberships import ctx
from tests.test_pipelines_tables import as_migrator
from tests.test_self_context import NOT_FOUND, reply
from tests.test_self_context import signed as plain
from tests.test_team_writes import send

__all__ = ["crm", "real_stack"]  # los fixtures, para pytest
pytestmark = pytest.mark.usefixtures("tenant_db")
DB = psycopg.Connection[Any]
LISTS = "/api/v1/o/org-a/pricing/price-lists/"
MIGRATION = "apps.pricing.migrations.0008_reset_ai_exposable"
AUDIT = (
    "SELECT action, entity_id, changes, metadata, actor_type, actor_id, organization_id"
    " FROM audit_logs WHERE action = 'price_list.ai_exposable_changed' ORDER BY occurred_at, id"
)
EXPOSED = "SELECT code FROM price_lists WHERE ai_exposable ORDER BY code"


def made(org: UUID, code: str, **fields: Any) -> UUID:
    values = {"name": code.title(), "kind": "RETAIL", "currency": "PEN"}
    values |= {"required_permission": "prices.view"} | fields
    with tenant_scope(ctx(org)):
        created: PriceList = PriceList.objects.create(code=code, **values)
    return created.pk


def expose(client: Client, list_id: Any, body: Any, org: str = "org-a") -> Any:
    path = f"/api/v1/o/{org}/pricing/price-lists/{list_id}/ai-exposable/"
    return send(client, "put", path, body)


def exposed(migrator: DB) -> list[str]:
    return [row[0] for row in migrator.execute(EXPOSED).fetchall()]


def audit(migrator: DB) -> list[tuple[Any, ...]]:
    return migrator.execute(AUDIT).fetchall()


@pytest.fixture
def shop(crm: SimpleNamespace) -> SimpleNamespace:
    """En A: REGULAR, MAYORISTA, las dos internas y BORRADA; en B: AJENA. bea: `prices.manage`."""
    give(crm.a, crm.m["bea"], {"prices.view": None, "prices.manage": None})
    lists = {"regular": made(crm.a, "REGULAR"), "foreign": made(crm.b, "AJENA"),
             "wholesale": made(crm.a, "MAYORISTA", required_permission="prices.view_wholesale"),
             "internal": made(crm.a, "INTERNA", required_permission="prices.view_internal"),
             "internal_kind": made(crm.a, "STAFF", kind="INTERNAL"),
             "deleted": made(crm.a, "BORRADA", deleted_at=timezone.now())}  # fmt: skip
    return SimpleNamespace(**vars(crm), lists=lists)


def test_only_ai_manage_with_a_recent_second_factor_changes_it(
    shop: SimpleNamespace, migrator: DB
) -> None:
    """`prices.manage` no basta; sin permiso, nunca `STEP_UP_REQUIRED`; sin código, nada cambia."""
    regular, yes = shop.lists["regular"], {"ai_exposable": True}
    for who in (shop.vera, shop.sam, shop.bea):  # bea gestiona precios: no decide la IA
        for client in (plain(who), signed(who)):
            assert reply(expose(client, regular, yes)) == DENIED
    assert reply(expose(Client(), regular, yes)) == UNAUTHENTICATED
    late = expose(plain(shop.ana), regular, yes)  # Owner sin segundo factor reciente
    assert (late.status_code, late.json()["code"]) == (403, STEP_UP_REQUIRED)
    other_method = send(signed(shop.ana), "patch", f"{LISTS}{regular}/ai-exposable/", yes)
    assert reply(other_method) == DENIED  # solo `PUT`: lo demás, cerrado
    assert exposed(migrator) == [] and audit(migrator) == []
    done = expose(signed(shop.ana), regular, yes)
    assert done.status_code == 200 and done.json()["ai_exposable"] is True
    assert done.json()["id"] == str(regular) and done.json()["code"] == "REGULAR"
    give(shop.a, shop.m["bea"], {"ai.manage": None})
    late = expose(plain(shop.bea), regular, {"ai_exposable": False})
    assert (late.status_code, late.json()["code"]) == (403, STEP_UP_REQUIRED)
    assert expose(signed(shop.bea), regular, {"ai_exposable": False}).status_code == 200
    with tenant_scope(ctx(shop.a, shop.sam)) as tenant, pytest.raises(AccessDenied):
        commands.set_ai_exposable(tenant, price_list_id=regular, exposable=True)  # fmt: skip
    assert exposed(migrator) == []


def test_each_change_is_audited_once_with_before_and_after_and_a_repeat_is_not(
    shop: SimpleNamespace, migrator: DB
) -> None:
    ana, regular, wholesale = signed(shop.ana), shop.lists["regular"], shop.lists["wholesale"]
    assert expose(ana, regular, {"ai_exposable": False}).json()["ai_exposable"] is False  # ya así
    assert audit(migrator) == []
    for _ in range(2):
        assert expose(ana, regular, {"ai_exposable": True}).status_code == 200
    assert expose(ana, wholesale, {"ai_exposable": True}).status_code == 200
    assert expose(ana, regular, {"ai_exposable": False}).status_code == 200
    assert exposed(migrator) == ["MAYORISTA"]
    on, off = {"ai_exposable": [False, True]}, {"ai_exposable": [True, False]}
    user = ("USER", shop.ana.pk, shop.a)
    assert audit(migrator) == [
        ("price_list.ai_exposable_changed", regular, on, {}, *user),
        ("price_list.ai_exposable_changed", wholesale, on, {}, *user),
        ("price_list.ai_exposable_changed", regular, off, {}, *user),
    ]
    row = migrator.execute("SELECT updated_by_user_id FROM price_lists WHERE id = %s", [regular])
    assert row.fetchone() == (shop.ana.pk,)


def test_the_body_the_internal_list_and_lists_out_of_reach_are_refused(
    shop: SimpleNamespace, migrator: DB, settings: Any
) -> None:
    settings.AI_ACCOUNT_THROTTLE = (60, 600)  # cada intento cuenta en el tope
    ana, regular = signed(shop.ana), shop.lists["regular"]
    for body in ({}, {"ai_exposable": "true"}, {"ai_exposable": 1}, {"ai_exposable": None},
                 [True], "true"):  # fmt: skip
        refused = expose(ana, regular, body)
        assert refused.status_code == 400, body
        assert refused.json()["code"] == "VALIDATION_ERROR", body
    other = expose(ana, regular, {"ai_exposable": True, "organization_id": str(shop.b)})
    assert other.status_code == 400 and set(other.json()["fields"]) == {"organization_id"}
    for internal in (shop.lists["internal"], shop.lists["internal_kind"]):  # 04 §K.2: nunca
        refused = expose(ana, internal, {"ai_exposable": True})
        assert refused.status_code == 400 and set(refused.json()["fields"]) == {"ai_exposable"}
        assert "interna" in refused.json()["fields"]["ai_exposable"][0]["message"]
    for gone in (shop.lists["deleted"], shop.lists["foreign"], uuid4()):
        assert reply(expose(ana, gone, {"ai_exposable": True})) == NOT_FOUND
    assert reply(expose(ana, regular, {"ai_exposable": True}, org="org-b")) == NOT_FOUND
    give(shop.a, shop.m["vera"], {"ai.manage": None})  # solo ve REGULAR: lo demás no existe
    for hidden in (shop.lists["wholesale"], shop.lists["internal"]):  # ni el 400 de la interna
        assert reply(expose(signed(shop.vera), hidden, {"ai_exposable": True})) == NOT_FOUND
    assert exposed(migrator) == [] and audit(migrator) == []


def test_a_list_that_becomes_internal_stops_being_cited_and_is_not_cited_again_by_itself(
    shop: SimpleNamespace, migrator: DB
) -> None:
    """bea (`prices.manage`) vuelve interna una lista que el Owner marcó, por el tipo o por quién
    la ve: deja de citarse en la misma escritura, auditado a su nombre; volver atrás no la marca."""
    ana, bea = signed(shop.ana), signed(shop.bea)
    regular, wholesale = shop.lists["regular"], shop.lists["wholesale"]
    for marked in (regular, wholesale):
        assert expose(ana, marked, {"ai_exposable": True}).status_code == 200
    for marked, internal, back in (
        (regular, {"kind": "INTERNAL"}, {"kind": "RETAIL"}),
        (wholesale, {"required_permission": "prices.view_internal"},
         {"required_permission": "prices.view_wholesale"}),
    ):  # fmt: skip
        changed = send(bea, "patch", f"{LISTS}{marked}/", internal)
        assert changed.status_code == 200 and changed.json()["ai_exposable"] is False
        assert send(bea, "patch", f"{LISTS}{marked}/", back).json()["ai_exposable"] is False
    assert exposed(migrator) == []
    off, why = {"ai_exposable": [True, False]}, {"reason": "INTERNAL_LIST"}
    assert [row[1:] for row in audit(migrator)[2:]] == [
        (regular, off, why, "USER", shop.bea.pk, shop.a),
        (wholesale, off, why, "USER", shop.bea.pk, shop.a),
    ]


def test_the_engine_never_cites_an_internal_list_and_the_owner_can_always_turn_it_off(
    shop: SimpleNamespace, migrator: DB
) -> None:
    """Una interna marcada de antes (por el tipo o por quién la ve): el motor no la cita, y el
    Owner la desmarca (auditado); marcarla sigue siendo un 400."""
    internal, by_kind = shop.lists["internal"], shop.lists["internal_kind"]
    migrator.execute("UPDATE price_lists SET ai_exposable = true WHERE id = ANY(%s)",
                     [[internal, by_kind, shop.lists["regular"]]])  # fmt: skip
    with tenant_scope(ctx(shop.a)) as tenant:
        cited = services._lists(tenant, services.Audience.PUBLIC_AI)
        assert sorted(row.code for row in cited) == ["REGULAR"]
    ana = signed(shop.ana)
    for listed in (internal, by_kind):
        assert expose(ana, listed, {"ai_exposable": False}).json()["ai_exposable"] is False
        assert expose(ana, listed, {"ai_exposable": True}).status_code == 400
    assert exposed(migrator) == ["REGULAR"] and len(audit(migrator)) == 2


def test_creating_and_changing_a_list_no_longer_take_the_flag(
    shop: SimpleNamespace, migrator: DB
) -> None:
    """Un cliente antiguo que lo envía recibe 400 en ese campo, sin que se repita lo enviado, y
    nada se escribe: ni siquiera el Owner con segundo factor lo cambia por aquí."""
    ana, regular = signed(shop.ana), shop.lists["regular"]
    for value in (True, False, "marcado-secreto"):
        body = {"code": "NUEVA", "name": "Nueva", "kind": "RETAIL", "ai_exposable": value}
        for refused in (
            send(ana, "post", LISTS, body),
            send(ana, "patch", f"{LISTS}{regular}/", {"name": "Otra", "ai_exposable": value}),
        ):
            assert refused.status_code == 400 and set(refused.json()["fields"]) == {"ai_exposable"}
            assert "ai-exposable" in refused.json()["fields"]["ai_exposable"][0]["message"]
            assert "marcado-secreto" not in refused.content.decode()
    names = migrator.execute("SELECT code, name FROM price_lists WHERE id = %s", [regular])
    assert names.fetchone() == ("REGULAR", "Regular") and exposed(migrator) == []
    with tenant_scope(ctx(shop.a, shop.ana)) as tenant:
        for command, fields in (
            (commands.update_price_list, {"price_list_id": regular}),
            (commands.create_price_list, {"code": "X", "name": "X", "kind": "RETAIL"}),
        ):
            with pytest.raises(ValueError, match="ai_exposable"):
                command(tenant, **fields, ai_exposable=True)
    assert exposed(migrator) == []


def test_changing_it_counts_in_the_ai_write_limit_and_other_pricing_writes_do_not(
    shop: SimpleNamespace, migrator: DB, settings: Any
) -> None:
    settings.AI_ACCOUNT_THROTTLE = (2, 600)
    ana, regular = signed(shop.ana), shop.lists["regular"]
    patched = [send(ana, "patch", f"{LISTS}{regular}/", {"sort_order": n}) for n in range(4)]
    assert [answer.status_code for answer in patched] == [200] * 4  # editar la lista no cuenta
    assert ana.get(f"{LISTS}{regular}/").status_code == 200
    assert migrator.execute(COUNTED).fetchall() == []
    puts = [expose(ana, regular, {"ai_exposable": flag}) for flag in (True, False, True)]
    assert [answer.status_code for answer in puts] == [200, 200, 429]
    assert puts[2].json()["code"] == "RATE_LIMITED" and int(puts[2].headers["Retry-After"]) > 0
    assert expose(signed(shop.vera), regular, {"ai_exposable": True}).status_code == 403
    assert migrator.execute(COUNTED).fetchall() == [
        (f"aik:usr:{shop.ana.pk}", 3),
        (f"aik:usr:{shop.vera.pk}", 1),  # contó antes de mirar el permiso
    ]
    assert exposed(migrator) == [] and len(audit(migrator)) == 2


def test_the_migration_turns_every_list_off_and_audits_only_the_ones_it_changed(
    shop: SimpleNamespace, migrator: DB
) -> None:
    """Marcadas en dos organizaciones (también la borrada); las apagadas no dejan rastro. Hacia
    atrás no marca nada; repetirla no audita más."""
    up = "UPDATE price_lists SET ai_exposable = true WHERE code = ANY(%s)"
    on = ["AJENA", "BORRADA", "MAYORISTA", "REGULAR"]
    try:
        with as_migrator():
            call_command("migrate", "pricing", "0007", verbosity=0)
        migrator.execute(up, [on])
        assert exposed(migrator) == on
        with as_migrator():
            call_command("migrate", "pricing", verbosity=0)
        assert exposed(migrator) == []
        rows = audit(migrator)
        ids = {shop.lists[key]: key for key in ("foreign", "deleted", "wholesale", "regular")}
        assert sorted(ids[row[1]] for row in rows) == sorted(ids.values())
        reset = {"reason": "F8-07b: D-F8-06, the owner marks the lists again"}
        for row in rows:
            org = shop.b if ids[row[1]] == "foreign" else shop.a
            assert row[2:] == ({"ai_exposable": [True, False]}, reset, "SYSTEM", None, org)
        with as_migrator():
            call_command("migrate", "pricing", "0007", verbosity=0)
            assert exposed(migrator) == []  # no-op hacia atrás
            call_command("migrate", "pricing", verbosity=0)
        assert len(audit(migrator)) == 4  # nada que apagar: nada que auditar
        with connection.schema_editor() as editor, pytest.raises(RuntimeError, match="BYPASSRLS"):
            import_module(MIGRATION).reset(None, editor)  # `crm_app`: RLS no le deja ver nada
    finally:
        with as_migrator():
            call_command("migrate", "pricing", verbosity=0)

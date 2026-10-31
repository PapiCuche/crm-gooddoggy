"""F5-04c: el detalle de una oportunidad (`…/opportunities/{id}/`): alcance, 404 igual para lo
que no existe, líneas, historial de etapas y consultas constantes. Middleware, sesión, motor de
autorización y PostgreSQL con el rol `crm_app`."""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

from apps.deals import opportunities as commands
from apps.deals import opportunity_stages as stages
from core.tenancy.scope import tenant_scope
from tests import test_opportunity_scopes as scopes
from tests.test_authorization import give
from tests.test_member_branch import DENIED
from tests.test_memberships import ctx
from tests.test_opportunities_api import LIST_FIELDS, OPPS, UNAUTHENTICATED
from tests.test_opportunities_tables import make, stage, variant
from tests.test_self_context import NOT_FOUND, reply, signed

crm, sales, pipelines = scopes.crm, scopes.sales, scopes.pipelines
real_stack, pick = scopes.real_stack, scopes.pick
pytestmark = pytest.mark.usefixtures("tenant_db")
DETAIL_FIELDS = LIST_FIELDS | {
    "source", "next_action_at", "lost_reason_note", "created_at", "updated_at", "items", "history",
}  # fmt: skip


def url(sales: SimpleNamespace, key: str) -> str:
    return f"{OPPS}{sales.opps[key]}/"


def test_the_detail_is_a_404_outside_the_scope_exactly_like_a_missing_one(
    sales: SimpleNamespace,
) -> None:
    vera, sam = signed(sales.vera), signed(sales.sam)
    missing = reply(vera.get(f"{OPPS}{uuid4()}/"))
    assert missing == NOT_FOUND
    for key in ("sam", "ana", "nobody", "gone", "b"):  # fuera del alcance, borrada y de B
        assert reply(vera.get(url(sales, key))) == missing, key
    assert vera.get(url(sales, "vera")).status_code == 200
    assert sam.get(url(sales, "vera")).status_code == 200  # `TEAM`: su compañera
    assert reply(sam.get(url(sales, "ana"))) == missing
    assert reply(sam.get(url(sales, "nobody"))) == missing  # sin responsable: solo `ORGANIZATION`
    assert signed(sales.ana).get(url(sales, "nobody")).status_code == 200
    assert reply(signed(sales.ana).get(url(sales, "gone"))) == missing  # ni el Owner la ve


def test_without_the_permission_a_session_or_the_organization_nothing_is_revealed(
    sales: SimpleNamespace,
) -> None:
    assert reply(signed(sales.bea).get(url(sales, "vera"))) == DENIED
    assert reply(signed(sales.bea).get(f"{OPPS}{uuid4()}/")) == DENIED  # igual para un id ajeno
    assert reply(Client().get(url(sales, "vera"))) == UNAUTHENTICATED
    assert reply(signed(sales.luis).get(url(sales, "b"))) == NOT_FOUND  # luis no es miembro de A
    other = "/api/v1/o/org-b/opportunities/"
    assert reply(signed(sales.luis).get(f"{other}{sales.opps['b']}/")) == DENIED  # sin permiso
    assert reply(signed(sales.ana).get(f"{other}{sales.opps['b']}/")) == NOT_FOUND  # no es de B
    give(sales.b, sales.m_luis, {"opportunities.view": "ORGANIZATION"})
    luis = signed(sales.luis)
    assert luis.get(f"{other}{sales.opps['b']}/").status_code == 200
    missing = reply(luis.get(f"{other}{uuid4()}/"))
    assert reply(luis.get(f"{other}{sales.opps['vera']}/")) == missing  # la de A no existe en B
    assert reply(luis.get(url(sales, "vera"))) == NOT_FOUND  # ni se entra en A


def test_the_detail_carries_the_lines_and_the_history_in_order(crm: SimpleNamespace) -> None:
    sku, contacted = variant(crm.a).pk, stage(crm.a, "contactado").pk
    with tenant_scope(ctx(crm.a, crm.vera)) as tenant:
        made = commands.create_opportunity(
            tenant,
            contact_id=crm.ids["owned"],
            title="iPhone 16",
            amount="1500.5",
            next_action="Llamar",
            items=[
                {
                    "variant_id": sku,
                    "quantity": 2,
                    "estimated_unit_price": "750.25",
                    "notes": "Rojo",
                }
            ],
        )
        stages.move_opportunity(tenant, opportunity_id=made.pk, version=1, stage_id=contacted)
    body = signed(crm.vera).get(f"{OPPS}{made.pk}/").json()
    assert set(body) == DETAIL_FIELDS
    assert (body["number"], body["amount"], body["version"], body["probability"]) == (
        "OPP-000001", "1500.50", 2, 20,
    )  # fmt: skip
    assert body["contact"] == {"id": str(crm.ids["owned"]), "display_name": "De vera"}
    assert body["items"] == [
        {"variant_id": str(sku), "quantity": 2, "estimated_unit_price": "750.25", "notes": "Rojo"}
    ]
    history = body["history"]
    path = [(h["from_stage"] and h["from_stage"]["name"], h["to_stage"]["name"]) for h in history]
    assert path == [(None, "Nuevo"), ("Nuevo", "Contactado")]
    assert history[0]["seconds_in_previous_stage"] is None
    assert history[1]["seconds_in_previous_stage"] >= 0
    assert history[1]["changed_by_user_id"] == str(crm.vera.pk)
    assert history[1]["to_stage"]["type"] == "OPEN"
    assert body["source"] == "MANUAL" and body["lost_reason_note"] is None
    assert body["next_action"] == "Llamar" and body["stage"]["name"] == "Contactado"


def test_the_contact_name_follows_the_contact_scope_in_the_detail(sales: SimpleNamespace) -> None:
    ref = {"id": str(sales.ids["owned"])}
    sam = signed(sales.sam).get(url(sales, "vera")).json()["contact"]
    assert sam == {**ref, "display_name": None}  # la oportunidad es de su equipo; el contacto, no
    vera = signed(sales.vera).get(url(sales, "vera")).json()["contact"]
    assert vera == {**ref, "display_name": "De vera"}


def test_the_detail_costs_the_same_queries_with_more_lines_and_history(
    crm: SimpleNamespace,
) -> None:
    ana = signed(crm.ana)
    opp = make(crm.a, assigned_user_id=crm.ana.pk)
    path = [stage(crm.a, "contactado").pk, stage(crm.a, "nuevo").pk]

    def queries() -> int:
        with CaptureQueriesContext(connection) as captured:
            assert ana.get(f"{OPPS}{opp.pk}/").status_code == 200
        return len(captured)

    one = queries()
    items = [{"variant_id": variant(crm.a, f"S{i}").pk, "quantity": 1} for i in range(3)]
    with tenant_scope(ctx(crm.a, crm.ana)) as tenant:
        for step in range(4):  # más historial
            stages.move_opportunity(
                tenant, opportunity_id=opp.pk, version=1 + step, stage_id=path[step % 2]
            )
        commands.update_opportunity(tenant, opportunity_id=opp.pk, version=5, items=items)
    assert queries() == one
    body = ana.get(f"{OPPS}{opp.pk}/").json()
    assert (len(body["history"]), len(body["items"])) == (4, 3)


def test_head_follows_the_same_rules_and_nothing_writes(sales: SimpleNamespace) -> None:
    mine = url(sales, "vera")
    assert signed(sales.vera).head(mine).status_code == 200
    assert signed(sales.vera).head(mine).content == b""
    assert signed(sales.vera).head(url(sales, "sam")).status_code == 404
    assert signed(sales.bea).head(mine).status_code == 403
    assert signed(sales.luis).head(mine).status_code == 404  # no es miembro de A
    assert Client().head(mine).status_code == 401
    for method in ("post", "put", "patch", "delete"):  # esta entrega solo lee
        assert getattr(signed(sales.ana), method)(mine).status_code in (403, 405), method

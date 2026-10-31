"""F5-04e: `PATCH …/opportunities/{id}/`, con su permiso doble, su alcance, la versión y los
datos hostiles. Middleware, sesión, motor de autorización y PostgreSQL con el rol `crm_app`."""

import threading
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from django.db import connection
from django.test import Client
from django.utils import timezone

from apps.deals.models import Opportunity
from core.tenancy.scope import tenant_scope
from tests import test_opportunity_scopes as scopes
from tests.factories import make_user
from tests.test_authorization import give
from tests.test_member_branch import DENIED
from tests.test_memberships import ctx, join
from tests.test_opportunities_api import LIST_FIELDS, OPPS
from tests.test_opportunities_tables import variant
from tests.test_opportunities_write_api import (
    CREATE_HOSTILE,
    CSRF_FAILED,
    NOT_OURS,
    UNAUTHENTICATED,
    acts,
    fail,
    rows,
)
from tests.test_opportunity_detail_api import DETAIL_FIELDS
from tests.test_self_context import NOT_FOUND, reply, signed
from tests.test_team_writes import send

crm, sales, pipelines = scopes.crm, scopes.sales, scopes.pipelines
real_stack = scopes.real_stack
pytestmark = pytest.mark.usefixtures("tenant_db")


def patch(client: Client, opp: Any, body: Any) -> Any:
    return send(client, "patch", f"{OPPS}{opp}/", body)


def stored(crm: SimpleNamespace, key: Any) -> Any:
    with tenant_scope(ctx(crm.a)):
        return Opportunity.objects.get(pk=key)


def test_an_update_changes_what_is_sent_audits_it_and_returns_the_detail(
    sales: SimpleNamespace, migrator: psycopg.Connection[Any]
) -> None:
    ana, pk = signed(sales.ana), sales.opps["ana"]
    sku = variant(sales.a).pk
    sent = {
        "version": 1,
        "title": " Nuevo   título ",
        "amount": "99.9",
        "priority": "URGENT",
        "next_action": "Visitar",
        "expected_close_date": "2026-12-31",
        "probability": 55,
        "items": [{"variant_id": str(sku), "quantity": 3}],
    }
    done = patch(ana, pk, sent)
    assert done.status_code == 200, done.content
    body = done.json()
    assert set(body) == DETAIL_FIELDS
    assert (body["title"], body["amount"], body["priority"], body["version"]) == (
        "Nuevo título", "99.90", "URGENT", 2,
    )  # fmt: skip
    assert (body["probability"], body["next_action"], body["stage"]["type"]) == (
        55,
        "Visitar",
        "OPEN",
    )
    assert body["assigned_user_id"] == str(sales.ana.pk)  # sin tocar
    assert [i["quantity"] for i in body["items"]] == [3]
    assert acts(migrator)[-1] == ("opportunity.updated", sales.ana.pk)
    assert ana.get(f"{OPPS}{pk}/").json() == body
    cleared = patch(ana, pk, {"version": 2, "next_action": None, "expected_close_date": None})
    assert cleared.json()["next_action"] is None and cleared.json()["version"] == 3
    same = patch(
        ana, pk, {"version": 3, "title": "Nuevo título"}
    )  # sin cambios: ni versión ni auditoría
    assert same.status_code == 200 and same.json()["version"] == 3
    only_version = patch(ana, pk, {"version": 3})
    assert only_version.status_code == 200 and only_version.json()["version"] == 3
    assert [a for a, _ in acts(migrator)].count("opportunity.updated") == 2


def test_a_stale_version_or_a_closed_opportunity_is_a_409_that_writes_nothing(
    sales: SimpleNamespace, migrator: psycopg.Connection[Any]
) -> None:
    from apps.deals.models import PipelineStage

    ana, pk = signed(sales.ana), sales.opps["ana"]
    before = rows(migrator)
    stale = patch(ana, pk, {"version": 7, "title": "x"})
    assert stale.status_code == 409 and stale.json()["code"] == "STALE_VERSION"
    assert rows(migrator) == before and stored(sales, pk).title == "De ana"
    with tenant_scope(ctx(sales.a)):
        won = PipelineStage.objects.get(code="ganado")
        Opportunity.objects.filter(pk=pk).update(
            stage_id=won.pk, status="WON", won_at=timezone.now(), probability=100
        )
    closed = patch(ana, pk, {"version": 1, "title": "x"})
    assert closed.status_code == 409 and closed.json()["code"] == "OPPORTUNITY_CLOSED"
    assert rows(migrator) == before


def test_the_update_scope_is_crossed_with_the_view_scope(sales: SimpleNamespace) -> None:
    vera, sam, bea = signed(sales.vera), signed(sales.sam), signed(sales.bea)
    missing = reply(patch(vera, uuid4(), {"version": 1, "title": "x"}))
    assert missing == NOT_FOUND
    for key in ("sam", "ana", "nobody", "gone", "b"):  # fuera del alcance: el mismo 404
        assert reply(patch(vera, sales.opps[key], {"version": 1, "title": "x"})) == missing, key
    assert patch(vera, sales.opps["vera"], {"version": 1, "title": "Mía"}).status_code == 200
    assert patch(sam, sales.opps["vera"], {"version": 2, "title": "Del equipo"}).status_code == 200
    assert reply(patch(sam, sales.opps["ana"], {"version": 1, "title": "x"})) == missing
    assert reply(patch(bea, sales.opps["vera"], {"version": 1, "title": "x"})) == DENIED
    give(sales.a, sales.m["bea"], {"opportunities.view": "ORGANIZATION"})
    assert (
        reply(patch(bea, sales.opps["vera"], {"version": 3, "title": "x"})) == DENIED
    )  # ver no es editar
    give(sales.a, sales.m["bea"], {"opportunities.update": "OWN"})
    assert reply(patch(bea, sales.opps["vera"], {"version": 3, "title": "x"})) == missing  # `OWN`
    other = give(sales.a, sales.m["bea"], {"opportunities.update": "ORGANIZATION"})
    assert patch(bea, sales.opps["vera"], {"version": 3, "title": "Ya"}).status_code == 200
    assert other.pk and stored(sales, sales.opps["vera"]).title == "Ya"
    only_update = make_user(email="soloedita@example.com")  # editar sin poder ver
    give(sales.a, join(sales.a, only_update).pk, {"opportunities.update": "ORGANIZATION"})
    assert (
        reply(patch(signed(only_update), sales.opps["vera"], {"version": 4, "title": "x"}))
        == DENIED
    )
    assert reply(patch(signed(sales.luis), sales.opps["vera"], {"version": 1})) == NOT_FOUND
    assert reply(patch(Client(), sales.opps["vera"], {"version": 1})) == UNAUTHENTICATED
    csrf = signed(sales.ana).patch(
        f"{OPPS}{sales.opps['ana']}/", {"version": 1}, "application/json"
    )
    assert reply(csrf) == CSRF_FAILED


def test_the_client_cannot_move_an_opportunity_through_the_update(
    sales: SimpleNamespace, migrator: psycopg.Connection[Any]
) -> None:
    ana, pk, before = signed(sales.ana), sales.opps["ana"], rows(migrator)
    forbidden = {**NOT_OURS, "contact_id": str(uuid4()), "pipeline_id": str(uuid4())}
    forbidden.pop("version")
    for key, value in forbidden.items():
        assert fail(patch(ana, pk, {"version": 1, key: value})) == (
            400, "VALIDATION_ERROR", [key],
        ), key  # fmt: skip
    assert rows(migrator) == before


UPDATE_HOSTILE: list[tuple[str, Any]] = [
    ("version", None), ("version", "1"), ("version", True), ("version", 1.0), ("version", 0),
    ("version", 2**31), ("version", 10**30), ("version", [1]), ("version", {}),
    *[c for c in CREATE_HOSTILE if c[0] not in ("contact_id", "pipeline_id")],
    ("probability", -1), ("probability", 101), ("probability", "50"), ("probability", 50.5),
    ("probability", True), ("probability", None),
]  # fmt: skip


@pytest.mark.parametrize(("field", "value"), UPDATE_HOSTILE)
def test_a_hostile_update_is_a_400_on_its_field_and_writes_nothing(
    sales: SimpleNamespace, migrator: psycopg.Connection[Any], field: str, value: Any
) -> None:
    ana, pk, before = signed(sales.ana), sales.opps["ana"], rows(migrator)
    sent = {"version": 1, field: value} if field != "version" else {"version": value}
    response = patch(ana, pk, sent)
    assert fail(response) == (400, "VALIDATION_ERROR", [field]), response.content
    assert rows(migrator) == before and stored(sales, pk).version == 1


def test_a_missing_version_is_a_400(sales: SimpleNamespace) -> None:
    response = patch(signed(sales.ana), sales.opps["ana"], {"title": "x"})
    assert fail(response) == (400, "VALIDATION_ERROR", ["version"])


def test_two_updates_with_the_same_version_do_not_both_win(sales: SimpleNamespace) -> None:
    pk, outcome = sales.opps["ana"], []
    owner = sales.ana

    def run(title: str) -> None:
        try:
            response = patch(signed(owner), pk, {"version": 1, "title": title})
            outcome.append((response.status_code, response.json().get("code")))
        finally:
            connection.close()

    threads = [threading.Thread(target=run, args=(t,)) for t in ("Uno", "Dos")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
        assert not thread.is_alive()
    assert sorted(outcome, key=str) == [(200, None), (409, "STALE_VERSION")]
    assert stored(sales, pk).version == 2


def test_updating_what_a_seller_could_not_read_leaks_nothing_through_the_answer(
    sales: SimpleNamespace,
) -> None:
    """La respuesta de un cambio es el detalle: sin contacto visible, el nombre es `null`."""
    sam = signed(sales.sam)
    body = patch(sam, sales.opps["vera"], {"version": 1, "title": "Del equipo"}).json()
    assert body["contact"]["display_name"] is None  # el contacto es de vera, no del equipo
    assert LIST_FIELDS <= set(body)

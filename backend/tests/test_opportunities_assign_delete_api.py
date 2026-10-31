"""F5-04f: `POST …/opportunities/{id}/assign/` y `DELETE …/opportunities/{id}/`, con sus permisos,
su alcance, la versión y los datos hostiles. Middleware, sesión, motor de autorización y
PostgreSQL con el rol `crm_app`."""

import threading
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from django.db import connection
from django.test import Client
from django.utils import timezone

from apps.deals.models import Opportunity, PipelineStage
from apps.organizations.models import OrganizationMembership
from core.tenancy.scope import tenant_scope
from tests import test_opportunity_scopes as scopes
from tests.conftest import migrator_conninfo
from tests.factories import make_user
from tests.test_authorization import give
from tests.test_member_branch import DENIED
from tests.test_memberships import ctx, join
from tests.test_opportunities_api import OPPS, ids_of
from tests.test_opportunities_update_api import REASSIGN, patch
from tests.test_opportunities_write_api import CSRF_FAILED, UNAUTHENTICATED, acts, fail, rows
from tests.test_self_context import NOT_FOUND, reply, signed
from tests.test_team_writes import send

crm, sales, pipelines = scopes.crm, scopes.sales, scopes.pipelines
real_stack = scopes.real_stack
pytestmark = pytest.mark.usefixtures("tenant_db")


def assign(client: Client, opp: Any, body: Any) -> Any:
    return send(client, "post", f"{OPPS}{opp}/assign/", body)


def delete(client: Client, opp: Any) -> Any:
    return send(client, "delete", f"{OPPS}{opp}/", None)


def stored(crm: SimpleNamespace, key: Any) -> Any:
    with tenant_scope(ctx(crm.a)):
        return Opportunity.objects.get(pk=key)


def close(sales: SimpleNamespace, key: str) -> None:
    with tenant_scope(ctx(sales.a)):
        won = PipelineStage.objects.get(code="ganado")
        Opportunity.objects.filter(pk=sales.opps[key]).update(
            stage_id=won.pk, status="WON", won_at=timezone.now(), probability=100
        )


def test_the_owner_reassigns_and_the_scope_follows_the_new_owner(
    sales: SimpleNamespace, migrator: psycopg.Connection[Any]
) -> None:
    ana, vera, pk = signed(sales.ana), signed(sales.vera), sales.opps["vera"]
    assert vera.get(f"{OPPS}{pk}/").status_code == 200
    done = assign(ana, pk, {"assigned_user_id": str(sales.ana.pk), "version": 1})
    assert done.status_code == 200, done.content
    body = done.json()
    assert (body["assigned_user_id"], body["version"]) == (str(sales.ana.pk), 2)
    assert acts(migrator)[-1] == ("opportunity.assigned", sales.ana.pk)
    assert vera.get(f"{OPPS}{pk}/").status_code == 404  # ya no es suya
    assert ids_of(vera) == set()
    assert signed(sales.ana).get(f"{OPPS}{pk}/").json() == body
    same = assign(ana, pk, {"assigned_user_id": str(sales.ana.pk), "version": 2})
    assert same.status_code == 200 and same.json()["version"] == 2  # ya lo era: no escribe
    assert [a for a, _ in acts(migrator)].count("opportunity.assigned") == 1


def test_a_closed_opportunity_can_be_reassigned(sales: SimpleNamespace) -> None:
    close(sales, "vera")
    done = assign(signed(sales.ana), sales.opps["vera"], {
        "assigned_user_id": str(sales.sam.pk), "version": 1,
    })  # fmt: skip
    assert done.status_code == 200 and done.json()["status"] == "WON"


def test_the_assignee_must_be_an_active_member_and_every_failure_looks_the_same(
    sales: SimpleNamespace, migrator: psycopg.Connection[Any]
) -> None:
    with tenant_scope(ctx(sales.a)):
        OrganizationMembership.objects.filter(pk=sales.m["sam"]).update(status="SUSPENDED")
    ana, before = signed(sales.ana), rows(migrator)
    pk = sales.opps["vera"]
    answers = {
        assign(ana, pk, {"assigned_user_id": str(who), "version": 1}).content
        for who in (sales.luis.pk, sales.sam.pk, uuid4())  # de B, suspendido, inexistente
    }
    assert len(answers) == 1
    bad = assign(ana, pk, {"assigned_user_id": str(sales.luis.pk), "version": 1})
    assert fail(bad) == (400, "VALIDATION_ERROR", ["assigned_user_id"])
    assert rows(migrator) == before and stored(sales, pk).assigned_user_id == sales.vera.pk


def test_assigning_needs_assign_and_view_and_hides_what_is_out_of_scope(
    sales: SimpleNamespace,
) -> None:
    vera, sam, bea = signed(sales.vera), signed(sales.sam), signed(sales.bea)
    to = {"assigned_user_id": str(sales.ana.pk), "version": 1}
    missing = reply(assign(sam, uuid4(), to))
    assert missing == NOT_FOUND
    assert reply(assign(vera, sales.opps["vera"], to)) == DENIED  # el Vendedor no reasigna
    assert assign(sam, sales.opps["vera"], to).status_code == 200  # `TEAM`: su compañera
    for key in ("ana", "nobody", "gone", "b"):  # fuera del alcance: el mismo 404
        assert reply(assign(sam, sales.opps[key], to)) == missing, key
    assert reply(assign(bea, sales.opps["sam"], to)) == DENIED
    give(sales.a, sales.m["bea"], {"opportunities.view": "ORGANIZATION"})
    assert reply(assign(bea, sales.opps["sam"], to)) == DENIED  # ver no es asignar
    only_assign = make_user(email="soloasigna@example.com")  # asignar sin poder ver
    give(sales.a, join(sales.a, only_assign).pk, {"opportunities.assign": None})
    assert reply(assign(signed(only_assign), sales.opps["sam"], to)) == DENIED
    give(sales.a, sales.m["bea"], {"opportunities.assign": None})
    assert assign(bea, sales.opps["sam"], to).status_code == 200
    assert reply(assign(signed(sales.luis), sales.opps["sam"], to)) == NOT_FOUND
    assert reply(assign(Client(), sales.opps["sam"], to)) == UNAUTHENTICATED
    url = f"{OPPS}{sales.opps['ana']}/assign/"
    assert reply(signed(sales.ana).post(url, to, "application/json")) == CSRF_FAILED


def test_a_stale_version_is_a_409_and_writes_nothing(
    sales: SimpleNamespace, migrator: psycopg.Connection[Any]
) -> None:
    before = rows(migrator)
    stale = assign(
        signed(sales.ana), sales.opps["vera"], {"assigned_user_id": str(sales.ana.pk), "version": 4}
    )
    assert stale.status_code == 409 and stale.json()["code"] == "STALE_VERSION"
    assert rows(migrator) == before


@pytest.mark.parametrize(
    "body",
    [
        {}, {"version": 1}, {"assigned_user_id": None, "version": 1},
        {"assigned_user_id": 5, "version": 1}, {"assigned_user_id": "x", "version": 1},
        {"assigned_user_id": [str(uuid4())], "version": 1}, {"assigned_user_id": "", "version": 1},
        {"assigned_user_id": str(uuid4())}, {"assigned_user_id": str(uuid4()), "version": None},
        {"assigned_user_id": str(uuid4()), "version": "1"},
        {"assigned_user_id": str(uuid4()), "version": True},
        {"assigned_user_id": str(uuid4()), "version": 0},
        {"assigned_user_id": str(uuid4()), "version": 2**31},
        {"assigned_user_id": str(uuid4()), "version": 10**30},
        {"assigned_user_id": str(uuid4()), "version": 1, "contact_id": str(uuid4())},
        {"assigned_user_id": str(uuid4()), "version": 1, "organization_id": str(uuid4())},
        {"assigned_user_id": str(uuid4()), "version": 1, "status": "WON"},
    ],
)  # fmt: skip
def test_a_hostile_assignment_is_a_400_and_writes_nothing(
    sales: SimpleNamespace, migrator: psycopg.Connection[Any], body: dict[str, Any]
) -> None:
    before = rows(migrator)
    response = assign(signed(sales.ana), sales.opps["vera"], body)
    assert response.status_code == 400 and response.json()["code"] == "VALIDATION_ERROR"
    assert rows(migrator) == before and stored(sales, sales.opps["vera"]).version == 1


@pytest.mark.parametrize("raw", ["null", "[]", '"x"', "5", "{", "", "true"])
def test_an_assignment_body_that_is_not_an_object_is_a_400(
    sales: SimpleNamespace, raw: str
) -> None:
    client = signed(sales.ana)
    client.cookies["csrftoken"] = "t" * 32
    url = f"{OPPS}{sales.opps['vera']}/assign/"
    response = client.post(url, raw, "application/json", headers={"X-CSRFToken": "t" * 32})
    assert response.status_code == 400


def test_an_update_and_an_assignment_with_the_same_version_do_not_both_win(
    sales: SimpleNamespace,
) -> None:
    pk, outcome = sales.opps["ana"], []
    calls = [
        lambda c: patch(c, pk, {"version": 1, "title": "Nuevo"}),
        lambda c: assign(c, pk, {"assigned_user_id": str(sales.vera.pk), "version": 1}),
    ]

    def run(call: Any) -> None:
        try:
            response = call(signed(sales.ana))
            outcome.append((response.status_code, response.json().get("code")))
        finally:
            connection.close()

    threads = [threading.Thread(target=run, args=(c,)) for c in calls]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
        assert not thread.is_alive()
    assert sorted(outcome, key=str) == [(200, None), (409, "STALE_VERSION")]
    assert stored(sales, pk).version == 2


def test_deleting_hides_the_opportunity_everywhere_and_audits_it(
    sales: SimpleNamespace, migrator: psycopg.Connection[Any]
) -> None:
    ana, pk = signed(sales.ana), sales.opps["sam"]
    done = delete(ana, pk)
    assert (done.status_code, done.content) == (204, b"")
    assert acts(migrator)[-1] == ("opportunity.deleted", sales.ana.pk)
    assert reply(ana.get(f"{OPPS}{pk}/")) == NOT_FOUND
    assert pk not in ids_of(ana)
    assert reply(delete(ana, pk)) == NOT_FOUND  # ya borrada
    again = patch(ana, pk, {"version": 1, "title": "x"})
    assert reply(again) == NOT_FOUND
    assert (
        reply(assign(ana, pk, {"assigned_user_id": str(sales.ana.pk), "version": 1})) == NOT_FOUND
    )
    row = stored_row(migrator, pk)
    assert row[0] is not None and row[1] == sales.ana.pk  # borrado lógico, con quien lo hizo
    close(sales, "ana")
    assert delete(ana, sales.opps["ana"]).status_code == 204  # también una cerrada


def stored_row(migrator: psycopg.Connection[Any], pk: UUID) -> Any:
    found = migrator.execute(
        "SELECT deleted_at, deleted_by_user_id FROM opportunities WHERE id = %s", [pk]
    )
    return found.fetchone()


def test_deleting_needs_delete_and_view_and_follows_their_scopes(
    sales: SimpleNamespace, migrator: psycopg.Connection[Any]
) -> None:
    vera, sam, bea = signed(sales.vera), signed(sales.sam), signed(sales.bea)
    assert reply(delete(vera, sales.opps["vera"])) == DENIED  # el Vendedor no borra
    assert reply(delete(sam, sales.opps["vera"])) == DENIED  # ni el Supervisor de la plantilla
    assert reply(delete(bea, sales.opps["vera"])) == DENIED
    give(sales.a, sales.m["bea"], {"opportunities.view": "ORGANIZATION"})
    assert reply(delete(bea, sales.opps["vera"])) == DENIED  # ver no es borrar
    only_delete = make_user(email="soloborra@example.com")
    give(sales.a, join(sales.a, only_delete).pk, {"opportunities.delete": "ORGANIZATION"})
    assert reply(delete(signed(only_delete), sales.opps["vera"])) == DENIED
    give(
        sales.a, sales.m["sam"], {"opportunities.delete": "OWN"}
    )  # `TEAM` para ver, `OWN` para borrar
    missing = reply(delete(sam, uuid4()))
    assert reply(delete(sam, sales.opps["vera"])) == missing  # la ve, pero no la puede borrar
    assert reply(delete(sam, sales.opps["ana"])) == missing
    assert stored_row(migrator, sales.opps["vera"])[0] is None
    assert delete(sam, sales.opps["sam"]).status_code == 204  # la suya, sí
    assert reply(delete(signed(sales.luis), sales.opps["vera"])) == NOT_FOUND
    assert reply(delete(Client(), sales.opps["vera"])) == UNAUTHENTICATED
    token = signed(sales.ana).delete(f"{OPPS}{sales.opps['vera']}/")
    assert reply(token) == CSRF_FAILED
    give(sales.a, sales.m["bea"], {"opportunities.delete": "ORGANIZATION"})
    assert delete(bea, sales.opps["vera"]).status_code == 204
    assert reply(delete(bea, sales.opps["b"])) == missing  # nunca la de otra organización


def held_assign(
    user: Any, pk: Any, body: dict[str, Any], hold: str, params: list[Any]
) -> tuple[bool, tuple[int, Any]]:
    """Otra transacción cambia la fila y aún no confirma; la asignación espera su bloqueo."""
    holder = psycopg.connect(migrator_conninfo(), autocommit=False)
    holder.execute(hold, params)
    box: list[tuple[int, Any]] = []

    def run() -> None:
        try:
            response = assign(signed(user), pk, body)
            box.append((response.status_code, response.json().get("code")))
        finally:
            connection.close()

    thread = threading.Thread(target=run)
    thread.start()
    thread.join(timeout=1.5)
    waited = thread.is_alive()
    holder.commit()
    thread.join(timeout=20)
    holder.close()
    assert not thread.is_alive()
    return waited, box[0]


def test_a_change_in_flight_is_a_409_or_a_404_for_the_waiting_assignment_never_a_false_one(
    sales: SimpleNamespace, migrator: psycopg.Connection[Any]
) -> None:
    """La consulta que bloquea no lleva los `JOIN` de la etapa y el motivo: si la etapa cambia
    mientras se espera, la fila sigue ahí y la respuesta es el 409 de la versión, no un 404."""
    pk, to = sales.opps["vera"], str(sales.sam.pk)
    moved = "UPDATE opportunities SET version = version + 1, probability = 50 WHERE id = %s"
    waited, answer = held_assign(sales.ana, pk, {"assigned_user_id": to, "version": 1}, moved, [pk])
    assert waited and answer == (409, "STALE_VERSION")
    with tenant_scope(ctx(sales.a)):
        won = PipelineStage.objects.get(code="ganado")
    close = (
        "UPDATE opportunities SET status = 'WON', stage_id = %s, won_at = now(), probability = 100,"
        " version = version + 1 WHERE id = %s"
    )
    other = sales.opps["sam"]
    waited, answer = held_assign(
        sales.ana, other, {"assigned_user_id": to, "version": 1}, close, [won.pk, other]
    )
    assert waited and answer == (409, "STALE_VERSION")  # cambió de etapa y de versión: 409, no 404
    # una reasignación en vuelo saca la fila del alcance de quien esperaba: 404, como siempre
    waited, answer = held_assign(
        sales.vera,
        pk,
        {"assigned_user_id": str(sales.vera.pk), "version": 1},
        REASSIGN,
        [sales.sam.pk, pk],
    )
    assert waited and answer[0] == 404

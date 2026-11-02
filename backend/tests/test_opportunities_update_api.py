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

from apps.deals.models import Opportunity, PipelineStage
from core.tenancy.scope import tenant_scope
from tests import test_opportunity_scopes as scopes
from tests.conftest import migrator_conninfo
from tests.factories import make_user
from tests.test_authorization import give
from tests.test_member_branch import DENIED
from tests.test_memberships import ctx, join
from tests.test_opportunities_api import LIST_FIELDS, OPPS
from tests.test_opportunities_tables import variant
from tests.test_opportunities_write_api import (
    CREATE_HOSTILE,
    CSRF_FAILED,
    HUGE,
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
    if isinstance(value, str) and len(value) > 3 and value != HUGE:
        assert value not in response.text  # no repite lo recibido


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


def test_the_body_is_checked_before_the_row_so_a_bad_one_answers_the_same_everywhere(
    sales: SimpleNamespace,
) -> None:
    """Un cuerpo inválido da el mismo 400, byte a byte, exista la oportunidad o no, esté en el
    alcance o no; uno válido da 404 para todo lo que no es de quien escribe."""
    vera = signed(sales.vera)
    ids = {
        "in scope": sales.opps["vera"],
        "out of scope": sales.opps["ana"],
        "unknown": uuid4(),
        "deleted": sales.opps["gone"],
        "other organization": sales.opps["b"],
    }
    bad = {key: patch(vera, pk, {"version": 1, "title": ""}) for key, pk in ids.items()}
    assert {r.status_code for r in bad.values()} == {400}
    assert len({r.content for r in bad.values()}) == 1
    good = {key: patch(vera, pk, {"version": 1, "title": "Ok"}) for key, pk in ids.items()}
    assert {k: r.status_code for k, r in good.items()} == {
        "in scope": 200, "out of scope": 404, "unknown": 404, "deleted": 404,
        "other organization": 404,
    }  # fmt: skip
    assert len({r.content for k, r in good.items() if k != "in scope"}) == 1


def held_patch(
    user: Any, pk: Any, body: dict[str, Any], hold: str, params: list[Any]
) -> tuple[bool, tuple[int, Any]]:
    """Otra transacción cambia la fila y aún no confirma; el `PATCH` espera su bloqueo. Devuelve
    si esperó y qué respondió tras confirmar."""
    holder = psycopg.connect(migrator_conninfo(), autocommit=False)
    holder.execute(hold, params)
    box: list[tuple[int, Any]] = []

    def run() -> None:
        try:
            response = patch(signed(user), pk, body)
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


REASSIGN = "UPDATE opportunities SET assigned_user_id = %s, version = version + 1 WHERE id = %s"


def test_a_reassignment_in_flight_takes_the_row_out_of_the_writers_scope(
    sales: SimpleNamespace, migrator: psycopg.Connection[Any]
) -> None:
    """El alcance se decide sobre la fila bloqueada: quien esperaba ya no la ve (404), no recibe un
    409 que revelaría que existe, y no escribe nada. `OWN` y, con `TEAM`, fuera del equipo."""
    cases = (
        (sales.vera, sales.opps["vera"], sales.sam.pk),  # `OWN`: pasa a su compañero
        (sales.sam, sales.opps["vera"], sales.ana.pk),  # `TEAM`: sale del equipo
    )
    for user, pk, to in cases:
        waited, answer = held_patch(user, pk, {"version": 1, "title": "Robada"}, REASSIGN, [to, pk])
        assert waited and answer == (404, "NOT_FOUND"), user.email
        title = migrator.execute("SELECT title FROM opportunities WHERE id = %s", [pk]).fetchone()
        assert title == ("De vera",)
        restore = "UPDATE opportunities SET assigned_user_id = %s, version = 1 WHERE id = %s"
        migrator.execute(restore, [sales.vera.pk, pk])


def test_a_deletion_in_flight_is_a_404_for_the_waiting_update(sales: SimpleNamespace) -> None:
    waited, answer = held_patch(
        sales.ana, sales.opps["vera"], {"version": 1, "title": "x"},
        "UPDATE opportunities SET deleted_at = now() WHERE id = %s", [sales.opps["vera"]],
    )  # fmt: skip
    assert waited and answer == (404, "NOT_FOUND")


def test_a_stage_change_in_flight_is_a_409_never_a_false_404(
    sales: SimpleNamespace, migrator: psycopg.Connection[Any]
) -> None:
    """Tras esperar, la fila está en otra etapa o cerrada: el `PATCH` recibe el 409 de versión o
    de estado, y no un 404 por la unión con la etapa que leyó antes de esperar."""
    pk, other = sales.opps["vera"], sales.opps["sam"]
    with tenant_scope(ctx(sales.a)):
        contacted = PipelineStage.objects.get(code="contactado", pipeline__code="retail").pk
        won = PipelineStage.objects.get(code="ganado", pipeline__code="retail").pk
    moved = "UPDATE opportunities SET stage_id = %s, version = version + 1 WHERE id = %s"
    waited, answer = held_patch(sales.ana, pk, {"version": 1, "title": "x"}, moved, [contacted, pk])
    assert waited and answer == (409, "STALE_VERSION")
    closed = (  # ganada por otra petición, aquí sin subir la versión: manda el estado
        "UPDATE opportunities SET status = 'WON', stage_id = %s, won_at = now(), probability = 100 "
        "WHERE id = %s"
    )
    waited, answer = held_patch(
        sales.ana, other, {"version": 1, "title": "x"}, closed, [won, other]
    )
    assert waited and answer == (409, "OPPORTUNITY_CLOSED")
    assert migrator.execute("SELECT title FROM opportunities WHERE id = %s", [pk]).fetchone() == (
        "De vera",
    )

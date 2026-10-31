"""F5-04h: `POST …/opportunities/{id}/win/` y `…/lose/`, con sus permisos, su alcance, la versión,
el motivo de pérdida y los datos hostiles. Middleware, sesión, motor de autorización y
PostgreSQL con el rol `crm_app`."""

import json
import threading
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from django.db import connection
from django.test import Client
from django.utils import timezone

from apps.deals.models import LostReason, Opportunity, PipelineStage
from core.tenancy.scope import tenant_scope
from tests import test_opportunity_scopes as scopes
from tests.factories import make_user
from tests.test_authorization import give
from tests.test_member_branch import DENIED
from tests.test_memberships import ctx, join
from tests.test_opportunities_api import OPPS
from tests.test_opportunities_assign_delete_api import close, stored
from tests.test_opportunities_move_api import at, reopen
from tests.test_opportunities_write_api import CSRF_FAILED, UNAUTHENTICATED, acts, rows
from tests.test_self_context import NOT_FOUND, reply, signed
from tests.test_team_writes import send

crm, sales, pipelines = scopes.crm, scopes.sales, scopes.pipelines
real_stack = scopes.real_stack
pytestmark = pytest.mark.usefixtures("tenant_db")


def win(client: Client, opp: Any, body: Any) -> Any:
    return send(client, "post", f"{OPPS}{opp}/win/", body)


def lose(client: Client, opp: Any, body: Any) -> Any:
    return send(client, "post", f"{OPPS}{opp}/lose/", body)


def reason(org: UUID, code: str = "PRICE") -> UUID:
    with tenant_scope(ctx(org)):
        return LostReason.objects.get(code=code).pk  # type: ignore[no-any-return]


def test_the_owner_wins_with_the_final_amount_and_the_opportunity_closes(
    sales: SimpleNamespace, migrator: psycopg.Connection[Any]
) -> None:
    ana, pk = signed(sales.ana), sales.opps["ana"]
    done = win(ana, pk, {"amount": "1999.9", "version": 1})
    assert done.status_code == 200, done.content
    body = done.json()
    assert (body["status"], body["probability"], body["amount"], body["version"]) == (
        "WON", 100, "1999.90", 2,
    )  # fmt: skip
    assert (body["stage"]["name"], body["stage"]["type"]) == ("Ganado", "WON")
    assert body["won_at"] is not None and body["lost_at"] is None and body["lost_reason"] is None
    assert body["history"][-1]["to_stage"]["name"] == "Ganado"
    assert acts(migrator)[-1] == ("opportunity.won", sales.ana.pk)
    shut = win(ana, pk, {"amount": "5", "version": 2})  # ya cerrada
    assert (shut.status_code, shut.json()["code"]) == (409, "OPPORTUNITY_CLOSED")
    back = reopen(ana, pk, {"stage_id": at(sales, "nuevo"), "version": 2})
    assert back.status_code == 200 and back.json()["amount"] == "1999.90"  # el importe se queda


def test_the_owner_loses_with_a_reason_and_the_note_never_reaches_the_audit(
    sales: SimpleNamespace, migrator: psycopg.Connection[Any]
) -> None:
    ana, pk, price = signed(sales.ana), sales.opps["ana"], reason(sales.a)
    note = "Dijo que Juan Perez DNI 12345678 compró en otra tienda"
    done = lose(ana, pk, {"lost_reason_id": str(price), "note": f"  {note}  ", "version": 1})
    assert done.status_code == 200, done.content
    body = done.json()
    assert (body["status"], body["probability"], body["version"]) == ("LOST", 0, 2)
    assert (body["stage"]["name"], body["stage"]["type"]) == ("Perdido", "LOST")
    assert body["lost_reason"] == {"id": str(price), "name": "Precio"}
    assert body["lost_reason_note"] == note and body["lost_at"] and body["won_at"] is None
    assert acts(migrator)[-1] == ("opportunity.lost", sales.ana.pk)
    changes = migrator.execute(
        "SELECT changes::text FROM audit_logs WHERE action = 'opportunity.lost'"
    ).fetchone()
    assert changes is not None
    assert "12345678" not in changes[0] and "[texto]" in changes[0]  # la nota, como `[texto]`
    shut = lose(ana, pk, {"lost_reason_id": str(price), "version": 2})
    assert (shut.status_code, shut.json()["code"]) == (409, "OPPORTUNITY_CLOSED")


def test_the_note_is_optional_and_a_blank_one_is_none(sales: SimpleNamespace) -> None:
    ana, price = signed(sales.ana), str(reason(sales.a))
    for key, note in (("ana", None), ("vera", "   "), ("sam", "")):
        sent = {"lost_reason_id": price, "version": 1}
        sent = sent if note is None else {**sent, "note": note}
        done = lose(ana, sales.opps[key], sent)
        assert done.status_code == 200 and done.json()["lost_reason_note"] is None, key
    nulled = lose(ana, sales.opps["nobody"], {"lost_reason_id": price, "note": None, "version": 1})
    assert nulled.status_code == 200 and nulled.json()["lost_reason_note"] is None


def test_the_lost_reason_must_be_an_active_live_one_of_the_organization_with_one_answer(
    sales: SimpleNamespace, migrator: psycopg.Connection[Any]
) -> None:
    with tenant_scope(ctx(sales.a)):
        asleep = LostReason.objects.create(name="Dormido", code="ASLEEP", is_active=False)
        gone = LostReason.objects.create(name="Borrado", code="GONE", deleted_at=timezone.now())
    ana, pk, before = signed(sales.ana), sales.opps["ana"], rows(migrator)
    bad = [asleep.pk, gone.pk, reason(sales.b), uuid4()]
    answers = {lose(ana, pk, {"lost_reason_id": str(r), "version": 1}).content for r in bad}
    assert len(answers) == 1  # inactivo, borrado, de otra organización, inexistente
    response = lose(ana, pk, {"lost_reason_id": str(bad[0]), "version": 1})
    assert response.status_code == 400 and list(response.json()["fields"]) == ["lost_reason_id"]
    assert rows(migrator) == before and stored(sales, pk).status == "OPEN"


def test_winning_and_losing_need_their_own_permission_and_follow_their_scopes(
    sales: SimpleNamespace,
) -> None:
    vera, sam, bea = signed(sales.vera), signed(sales.sam), signed(sales.bea)
    price = str(reason(sales.a))
    to_win, to_lose = {"amount": "10", "version": 1}, {"lost_reason_id": price, "version": 1}
    missing = reply(win(sam, uuid4(), to_win))
    assert missing == NOT_FOUND
    assert win(vera, sales.opps["vera"], to_win).status_code == 200  # el Vendedor cierra lo suyo
    assert reply(win(vera, sales.opps["sam"], to_win)) == missing
    assert lose(sam, sales.opps["sam"], to_lose).status_code == 200
    assert reply(lose(sam, sales.opps["ana"], to_lose)) == missing  # fuera de `TEAM`
    for key in ("nobody", "gone", "b"):
        assert reply(win(sam, sales.opps[key], to_win)) == missing, key
    assert reply(win(bea, sales.opps["ana"], to_win)) == DENIED
    give(
        sales.a,
        sales.m["bea"],
        {"opportunities.view": "ORGANIZATION", "opportunities.update": "ORGANIZATION"},
    )
    assert reply(win(bea, sales.opps["ana"], to_win)) == DENIED  # ver y editar no es ganar
    assert reply(lose(bea, sales.opps["ana"], to_lose)) == DENIED
    give(sales.a, sales.m["bea"], {"opportunities.mark_won": "ORGANIZATION"})
    assert reply(lose(bea, sales.opps["ana"], to_lose)) == DENIED  # ganar no es perder
    assert win(bea, sales.opps["ana"], to_win).status_code == 200
    only_won = make_user(email="soloGana@example.com")  # ganar sin poder ver
    give(sales.a, join(sales.a, only_won).pk, {"opportunities.mark_won": "ORGANIZATION"})
    assert reply(win(signed(only_won), sales.opps["nobody"], to_win)) == DENIED
    only_lost = make_user(email="soloPierde@example.com")  # perder sin poder ver
    give(sales.a, join(sales.a, only_lost).pk, {"opportunities.mark_lost": "ORGANIZATION"})
    assert reply(lose(signed(only_lost), sales.opps["nobody"], to_lose)) == DENIED
    give(sales.a, sales.m["bea"], {"opportunities.mark_lost": "OWN"})
    assert reply(lose(bea, sales.opps["nobody"], to_lose)) == missing  # `OWN`: no es suya
    assert reply(win(signed(sales.luis), sales.opps["nobody"], to_win)) == NOT_FOUND
    assert reply(win(Client(), sales.opps["nobody"], to_win)) == UNAUTHENTICATED
    url = f"{OPPS}{sales.opps['nobody']}/lose/"
    assert reply(signed(sales.ana).post(url, to_lose, "application/json")) == CSRF_FAILED


def test_a_stale_version_a_closed_opportunity_and_a_missing_field_are_409s(
    sales: SimpleNamespace, migrator: psycopg.Connection[Any]
) -> None:
    ana, pk, before = signed(sales.ana), sales.opps["ana"], rows(migrator)
    stale = win(ana, pk, {"amount": "10", "version": 6})
    assert (stale.status_code, stale.json()["code"]) == (409, "STALE_VERSION")
    with tenant_scope(ctx(sales.a)):
        PipelineStage.objects.filter(code="perdido", pipeline__code="retail").update(
            requires_fields={"next_action": True}
        )
    price = str(reason(sales.a))
    blocked = lose(ana, pk, {"lost_reason_id": price, "version": 1})
    assert (blocked.status_code, blocked.json()["code"]) == (409, "MISSING_FIELDS")
    assert "next_action" in blocked.json()["message"]
    assert rows(migrator) == before and stored(sales, pk).status == "OPEN"  # no escribe nada
    close(sales, "vera")
    shut = lose(ana, sales.opps["vera"], {"lost_reason_id": price, "version": 1})
    assert (shut.status_code, shut.json()["code"]) == (409, "OPPORTUNITY_CLOSED")


WIN_HOSTILE = [
    {}, {"version": 1}, {"amount": "10"}, {"amount": None, "version": 1},
    {"amount": 10, "version": 1}, {"amount": 10.5, "version": 1}, {"amount": True, "version": 1},
    {"amount": "", "version": 1}, {"amount": "abc", "version": 1}, {"amount": "0", "version": 1},
    {"amount": "0.00", "version": 1}, {"amount": "-5", "version": 1},
    {"amount": "1e3", "version": 1}, {"amount": "NaN", "version": 1},
    {"amount": "Infinity", "version": 1}, {"amount": "1" * 13, "version": 1},
    {"amount": "1.234", "version": 1}, {"amount": "9" * 100_000, "version": 1},
    {"amount": "10", "version": None}, {"amount": "10", "version": "1"},
    {"amount": "10", "version": True}, {"amount": "10", "version": 0},
    {"amount": "10", "version": 10**30},
    {"amount": "10", "version": 1, "status": "WON"},
    {"amount": "10", "version": 1, "stage_id": str(uuid4())},
    {"amount": "10", "version": 1, "won_at": "2026-01-01T00:00:00Z"},
]  # fmt: skip
LOSE_HOSTILE = [
    {}, {"version": 1}, {"lost_reason_id": str(uuid4())},
    {"lost_reason_id": None, "version": 1}, {"lost_reason_id": 5, "version": 1},
    {"lost_reason_id": "x", "version": 1}, {"lost_reason_id": [str(uuid4())], "version": 1},
    {"lost_reason_id": str(uuid4()), "version": None},
    {"lost_reason_id": str(uuid4()), "version": "1"},
    {"lost_reason_id": str(uuid4()), "version": True},
    {"lost_reason_id": str(uuid4()), "version": 0},
    {"lost_reason_id": str(uuid4()), "version": 1, "note": 5},
    {"lost_reason_id": str(uuid4()), "version": 1, "note": ["x"]},
    {"lost_reason_id": str(uuid4()), "version": 1, "note": "a\nb"},
    {"lost_reason_id": str(uuid4()), "version": 1, "note": "a\x00b"},
    {"lost_reason_id": str(uuid4()), "version": 1, "note": "x" * 1001},
    {"lost_reason_id": str(uuid4()), "version": 1, "note": "x" * 100_000},
    {"lost_reason_id": str(uuid4()), "version": 1, "lost_at": "2026-01-01T00:00:00Z"},
    {"lost_reason_id": str(uuid4()), "version": 1, "status": "LOST"},
    {"lost_reason_id": str(uuid4()), "version": 1, "lost_reason_note": "x"},
]  # fmt: skip


ALLOWED = {win: {"amount", "version"}, lose: {"lost_reason_id", "note", "version"}}


@pytest.mark.parametrize(
    ("route", "body"),
    [(win, b) for b in WIN_HOSTILE] + [(lose, b) for b in LOSE_HOSTILE],
)
def test_a_hostile_close_is_a_400_on_its_field_and_writes_nothing(
    sales: SimpleNamespace, migrator: psycopg.Connection[Any], route: Any, body: dict[str, Any]
) -> None:
    before = rows(migrator)
    response = route(signed(sales.ana), sales.opps["ana"], body)
    assert response.status_code == 400 and response.json()["code"] == "VALIDATION_ERROR"
    fields = set(response.json()["fields"])
    assert "_" not in fields  # lo para el serializador, no el comando
    assert set(body) - ALLOWED[route] <= fields  # las claves desconocidas, con su nombre
    assert rows(migrator) == before and stored(sales, sales.opps["ana"]).version == 1
    assert "e" * 50 not in json.dumps(response.json())  # no repite lo recibido


@pytest.mark.parametrize("raw", ["null", "[]", '"x"', "5", "{", "", "true"])
def test_a_close_body_that_is_not_an_object_is_a_400(sales: SimpleNamespace, raw: str) -> None:
    client = signed(sales.ana)
    client.cookies["csrftoken"] = "t" * 32
    for action in ("win", "lose"):
        url = f"{OPPS}{sales.opps['ana']}/{action}/"
        response = client.post(url, raw, "application/json", headers={"X-CSRFToken": "t" * 32})
        assert response.status_code == 400


def test_winning_and_losing_with_the_same_version_let_only_one_close_it(
    sales: SimpleNamespace,
) -> None:
    pk, outcome = sales.opps["ana"], []
    price = str(reason(sales.a))
    calls = [
        lambda c: win(c, pk, {"amount": "10", "version": 1}),
        lambda c: lose(c, pk, {"lost_reason_id": price, "version": 1}),
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
        thread.join(timeout=30)
        assert not thread.is_alive()
    assert sorted(outcome, key=str) == [(200, None), (409, "STALE_VERSION")]
    closed = stored(sales, pk)
    assert closed.status in ("WON", "LOST") and closed.version == 2
    with tenant_scope(ctx(sales.a)):
        assert Opportunity.objects.get(pk=pk).status == closed.status

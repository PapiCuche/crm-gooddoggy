"""F5-04g: `POST …/opportunities/{id}/move/` y `…/reopen/`, con sus permisos, su alcance, la
versión, los errores de dominio y los datos hostiles. Middleware, sesión, motor de autorización y
PostgreSQL con el rol `crm_app`."""

import threading
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from django.db import connection
from django.test import Client

from apps.audit.services import record
from apps.deals import opportunities as commands
from apps.deals import opportunity_stages as stages
from apps.deals.models import Opportunity, Pipeline, PipelineStage
from core.tenancy.scope import tenant_scope
from tests import test_opportunity_scopes as scopes
from tests.factories import make_user
from tests.test_authorization import give
from tests.test_member_branch import DENIED
from tests.test_memberships import ctx, join
from tests.test_opportunities_api import OPPS
from tests.test_opportunities_assign_delete_api import assign, close, stored
from tests.test_opportunities_write_api import CSRF_FAILED, UNAUTHENTICATED, acts, rows
from tests.test_pipeline_commands import new as new_pipeline
from tests.test_self_context import NOT_FOUND, reply, signed
from tests.test_team_writes import send

crm, sales, pipelines = scopes.crm, scopes.sales, scopes.pipelines
real_stack = scopes.real_stack
pytestmark = pytest.mark.usefixtures("tenant_db")


def move(client: Client, opp: Any, body: Any) -> Any:
    return send(client, "post", f"{OPPS}{opp}/move/", body)


def reopen(client: Client, opp: Any, body: Any) -> Any:
    return send(client, "post", f"{OPPS}{opp}/reopen/", body)


def at(sales: SimpleNamespace, code: str, pipeline: Any = None) -> str:
    """El id de la etapa `code` del pipeline minorista (o del indicado)."""
    with tenant_scope(ctx(sales.a)):
        found = PipelineStage.objects.filter(code=code)
        found = found.filter(pipeline_id=pipeline or Pipeline.objects.get(code="retail").pk)
        return str(found.get().pk)


def test_the_owner_moves_an_opportunity_and_the_history_keeps_the_step(
    sales: SimpleNamespace, migrator: psycopg.Connection[Any]
) -> None:
    ana, pk = signed(sales.ana), sales.opps["ana"]
    done = move(ana, pk, {"stage_id": at(sales, "cotizado"), "version": 1})
    assert done.status_code == 200, done.content
    body = done.json()
    assert (body["stage"]["name"], body["stage"]["type"], body["probability"]) == (
        "Cotizado", "OPEN", 40,
    )  # fmt: skip
    assert (body["version"], body["status"]) == (2, "OPEN")
    assert [
        (h["from_stage"] and h["from_stage"]["name"], h["to_stage"]["name"])
        for h in body["history"]
    ][-1] == ("Nuevo", "Cotizado")
    assert acts(migrator)[-1] == ("opportunity.moved", sales.ana.pk)
    back = move(ana, pk, {"stage_id": at(sales, "nuevo"), "version": 2})  # también hacia atrás
    assert back.status_code == 200 and back.json()["probability"] == 10
    same = move(ana, pk, {"stage_id": at(sales, "nuevo"), "version": 3})  # ya está: no escribe
    assert same.status_code == 200 and same.json()["version"] == 3
    assert [a for a, _ in acts(migrator)].count("opportunity.moved") == 2


def test_a_closing_stage_a_closed_opportunity_and_a_stale_version_are_409s(
    sales: SimpleNamespace, migrator: psycopg.Connection[Any]
) -> None:
    ana, pk, before = signed(sales.ana), sales.opps["ana"], rows(migrator)
    for code in ("ganado", "perdido"):
        closing = move(ana, pk, {"stage_id": at(sales, code), "version": 1})
        assert (closing.status_code, closing.json()["code"]) == (409, "CLOSING_STAGE")
    stale = move(ana, pk, {"stage_id": at(sales, "cotizado"), "version": 9})
    assert (stale.status_code, stale.json()["code"]) == (409, "STALE_VERSION")
    assert rows(migrator) == before
    close(sales, "ana")
    shut = move(ana, pk, {"stage_id": at(sales, "cotizado"), "version": 1})
    assert (shut.status_code, shut.json()["code"]) == (409, "OPPORTUNITY_CLOSED")
    assert rows(migrator) == before


def test_the_stage_must_be_an_active_one_of_the_pipeline_with_one_answer_for_all(
    sales: SimpleNamespace, migrator: psycopg.Connection[Any]
) -> None:
    other = new_pipeline(sales, "mayoristas")
    with tenant_scope(ctx(sales.a)):
        PipelineStage.objects.filter(code="negociacion", pipeline__code="retail").update(
            is_active=False
        )
        foreign = PipelineStage.objects.filter(pipeline_id=other.pk, code="nuevo").get()
    with tenant_scope(ctx(sales.b)):
        theirs = PipelineStage.objects.get(code="nuevo")
    ana, pk, before = signed(sales.ana), sales.opps["ana"], rows(migrator)
    bad = [at(sales, "negociacion"), str(foreign.pk), str(theirs.pk), str(uuid4())]
    answers = {move(ana, pk, {"stage_id": s, "version": 1}).content for s in bad}
    assert len(answers) == 1  # inactiva, de otro pipeline, de otra organización, inexistente
    response = move(ana, pk, {"stage_id": bad[0], "version": 1})
    assert response.status_code == 400 and list(response.json()["fields"]) == ["stage_id"]
    assert rows(migrator) == before


def test_a_stage_that_requires_data_the_opportunity_lacks_is_a_409(
    sales: SimpleNamespace, migrator: psycopg.Connection[Any]
) -> None:
    with tenant_scope(ctx(sales.a)):
        PipelineStage.objects.filter(code="cotizado", pipeline__code="retail").update(
            requires_fields={"amount": True, "next_action": True}
        )
    ana, pk, before = signed(sales.ana), sales.opps["ana"], rows(migrator)
    blocked = move(ana, pk, {"stage_id": at(sales, "cotizado"), "version": 1})
    assert (blocked.status_code, blocked.json()["code"]) == (409, "MISSING_FIELDS")
    assert "amount" in blocked.json()["message"] and "next_action" in blocked.json()["message"]
    assert rows(migrator) == before
    patched = send(ana, "patch", f"{OPPS}{pk}/", {"version": 1, "amount": "10", "next_action": "x"})
    assert patched.status_code == 200
    assert move(ana, pk, {"stage_id": at(sales, "cotizado"), "version": 2}).status_code == 200


def test_an_opportunity_of_an_inactive_pipeline_cannot_move_and_says_so(
    sales: SimpleNamespace,
) -> None:
    other = new_pipeline(sales, "mayoristas")
    with tenant_scope(ctx(sales.a, sales.ana)) as tenant:
        made = commands.create_opportunity(
            tenant, contact_id=sales.ids["plain"], title="x", pipeline_id=other.pk
        )
    with tenant_scope(ctx(sales.a)):
        Pipeline.objects.filter(pk=other.pk).update(is_active=False)
    to = {"stage_id": at(sales, "cotizado", other.pk), "version": 1}
    blocked = move(signed(sales.ana), made.pk, to)
    assert (blocked.status_code, blocked.json()["code"]) == (409, "PIPELINE_UNAVAILABLE")
    assert stored(sales, made.pk).version == 1


def test_moving_needs_view_and_update_and_follows_the_scopes(sales: SimpleNamespace) -> None:
    vera, sam, bea = signed(sales.vera), signed(sales.sam), signed(sales.bea)
    to = {"stage_id": at(sales, "cotizado"), "version": 1}
    missing = reply(move(sam, uuid4(), to))
    assert missing == NOT_FOUND
    assert move(vera, sales.opps["vera"], to).status_code == 200  # el Vendedor mueve lo suyo
    assert reply(move(vera, sales.opps["sam"], to)) == missing
    assert move(sam, sales.opps["vera"], {**to, "version": 2}).status_code == 200  # `TEAM`
    for key in ("ana", "nobody", "gone", "b"):
        assert reply(move(sam, sales.opps[key], to)) == missing, key
    assert reply(move(bea, sales.opps["sam"], to)) == DENIED
    give(sales.a, sales.m["bea"], {"opportunities.view": "ORGANIZATION"})
    assert reply(move(bea, sales.opps["sam"], to)) == DENIED  # ver no es mover
    only_update = make_user(email="soloedita@example.com")
    give(sales.a, join(sales.a, only_update).pk, {"opportunities.update": "ORGANIZATION"})
    assert reply(move(signed(only_update), sales.opps["sam"], to)) == DENIED
    give(sales.a, sales.m["bea"], {"opportunities.update": "OWN"})
    assert reply(move(bea, sales.opps["sam"], to)) == missing  # mover es editar: `OWN`
    give(sales.a, sales.m["bea"], {"opportunities.update": "ORGANIZATION"})
    assert move(bea, sales.opps["sam"], to).status_code == 200
    assert reply(move(signed(sales.luis), sales.opps["sam"], to)) == NOT_FOUND
    assert reply(move(Client(), sales.opps["sam"], to)) == UNAUTHENTICATED
    url = f"{OPPS}{sales.opps['ana']}/move/"
    assert reply(signed(sales.ana).post(url, to, "application/json")) == CSRF_FAILED


def test_reopening_clears_the_closing_and_keeps_the_amount(
    sales: SimpleNamespace, migrator: psycopg.Connection[Any]
) -> None:
    ana, pk = signed(sales.ana), sales.opps["vera"]
    with tenant_scope(ctx(sales.a)):
        Opportunity.objects.filter(pk=pk).update(amount="250.00")
    close(sales, "vera")
    done = reopen(ana, pk, {"stage_id": at(sales, "contactado"), "version": 1})
    assert done.status_code == 200, done.content
    body = done.json()
    assert (body["status"], body["won_at"], body["lost_at"], body["lost_reason"]) == (
        "OPEN", None, None, None,
    )  # fmt: skip
    assert (body["amount"], body["version"], body["probability"]) == ("250.00", 2, 20)
    assert body["history"][-1]["to_stage"]["name"] == "Contactado"
    assert acts(migrator)[-1] == ("opportunity.reopened", sales.ana.pk)
    again = reopen(ana, pk, {"stage_id": at(sales, "nuevo"), "version": 2})  # ya está abierta
    assert (again.status_code, again.json()["code"]) == (409, "NOT_CLOSED")


def test_reopening_goes_only_to_an_open_active_stage_and_follows_the_scopes(
    sales: SimpleNamespace,
) -> None:
    close(sales, "vera")
    vera, sam, bea = signed(sales.vera), signed(sales.sam), signed(sales.bea)
    missing = reply(reopen(sam, uuid4(), {"stage_id": at(sales, "nuevo"), "version": 1}))
    for code in ("ganado", "perdido"):  # no es una etapa abierta
        bad = reopen(vera, sales.opps["vera"], {"stage_id": at(sales, code), "version": 1})
        assert bad.status_code == 400 and list(bad.json()["fields"]) == ["stage_id"]
    stale = reopen(vera, sales.opps["vera"], {"stage_id": at(sales, "nuevo"), "version": 5})
    assert (stale.status_code, stale.json()["code"]) == (409, "STALE_VERSION")
    assert (
        reply(reopen(sam, sales.opps["ana"], {"stage_id": at(sales, "nuevo"), "version": 1}))
        == missing
    )
    assert (
        reply(reopen(bea, sales.opps["vera"], {"stage_id": at(sales, "nuevo"), "version": 1}))
        == DENIED
    )
    assert (
        reopen(sam, sales.opps["vera"], {"stage_id": at(sales, "nuevo"), "version": 1}).status_code
        == 200
    )


@pytest.mark.parametrize("route", [move, reopen])
@pytest.mark.parametrize(
    "body",
    [
        {}, {"version": 1}, {"stage_id": None, "version": 1}, {"stage_id": 5, "version": 1},
        {"stage_id": "x", "version": 1}, {"stage_id": [str(uuid4())], "version": 1},
        {"stage_id": str(uuid4())}, {"stage_id": str(uuid4()), "version": None},
        {"stage_id": str(uuid4()), "version": "1"}, {"stage_id": str(uuid4()), "version": True},
        {"stage_id": str(uuid4()), "version": 0}, {"stage_id": str(uuid4()), "version": 10**30},
        {"stage_id": str(uuid4()), "version": 1, "status": "WON"},
        {"stage_id": str(uuid4()), "version": 1, "pipeline_id": str(uuid4())},
        {"stage_id": str(uuid4()), "version": 1, "amount": "5"},
    ],
)  # fmt: skip
def test_a_hostile_stage_change_is_a_400_and_writes_nothing(
    sales: SimpleNamespace, migrator: psycopg.Connection[Any], route: Any, body: dict[str, Any]
) -> None:
    before = rows(migrator)
    response = route(signed(sales.ana), sales.opps["ana"], body)
    assert response.status_code == 400 and response.json()["code"] == "VALIDATION_ERROR"
    assert rows(migrator) == before and stored(sales, sales.opps["ana"]).version == 1


@pytest.mark.parametrize("raw", ["null", "[]", '"x"', "5", "{", "", "true"])
def test_a_stage_change_body_that_is_not_an_object_is_a_400(
    sales: SimpleNamespace, raw: str
) -> None:
    client = signed(sales.ana)
    client.cookies["csrftoken"] = "t" * 32
    for action in ("move", "reopen"):
        url = f"{OPPS}{sales.opps['ana']}/{action}/"
        response = client.post(url, raw, "application/json", headers={"X-CSRFToken": "t" * 32})
        assert response.status_code == 400


def test_two_moves_and_an_assignment_with_the_same_version_let_only_one_win(
    sales: SimpleNamespace,
) -> None:
    pk, outcome = sales.opps["ana"], []
    calls = [
        lambda c: move(c, pk, {"stage_id": at(sales, "cotizado"), "version": 1}),
        lambda c: move(c, pk, {"stage_id": at(sales, "contactado"), "version": 1}),
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
        thread.join(timeout=30)
        assert not thread.is_alive()
    assert sorted(outcome, key=str) == [(200, None), (409, "STALE_VERSION"), (409, "STALE_VERSION")]
    assert stored(sales, pk).version == 2


def held_race(
    monkeypatch: pytest.MonkeyPatch, first: tuple[Any, Any], second: tuple[Any, Any]
) -> tuple[bool, dict[str, Any]]:
    """La primera petición retiene su transacción (tras auditar) y la segunda espera el bloqueo de
    la fila. Cada una es `(usuario, llamada)`; devuelve si la segunda esperó y qué respondió cada
    una."""
    wrote, release = threading.Event(), threading.Event()
    outcome: dict[str, Any] = {}

    def hold(*args: Any, **kwargs: Any) -> Any:
        done = record(*args, **kwargs)
        if threading.current_thread().name == "first":
            wrote.set()
            release.wait(timeout=10)
        return done

    monkeypatch.setattr(commands, "record", hold)
    monkeypatch.setattr(stages, "record", hold)

    def run(name: str, user: Any, call: Any) -> None:
        try:
            response = call(signed(user))
            outcome[name] = (response.status_code, response.json().get("code"))
        except Exception as error:  # noqa: BLE001 — el resultado lo comprueba el test
            outcome[name] = type(error).__name__
        finally:
            wrote.set()
            connection.close()

    threads = [
        threading.Thread(name=name, target=run, args=(name, *pair))
        for name, pair in (("first", first), ("second", second))
    ]
    threads[0].start()
    assert wrote.wait(timeout=10)
    threads[1].start()
    threads[1].join(timeout=1)
    waited = threads[1].is_alive()
    release.set()
    for thread in threads:
        thread.join(timeout=20)
        assert not thread.is_alive()
    return waited, outcome


def test_the_scope_is_decided_on_the_locked_row(
    sales: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Una reasignación que aún no ha terminado hace esperar a quien mueve: al despertar ya no la
    ve (404), no la mueve con un alcance viejo ni responde un 409 que revelaría que existe."""
    pk, target = sales.opps["vera"], at(sales, "cotizado")
    mine = {"assigned_user_id": str(sales.ana.pk), "version": 1}
    waited, outcome = held_race(
        monkeypatch,
        (sales.ana, lambda c: assign(c, pk, mine)),
        (sales.vera, lambda c: move(c, pk, {"stage_id": target, "version": 1})),
    )
    assert waited  # esperó el bloqueo de la fila
    assert outcome == {"first": (200, None), "second": (404, "NOT_FOUND")}


def test_a_move_that_changed_the_stage_makes_the_waiting_one_answer_409_not_404(
    sales: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tras el bloqueo la fila ya está en otra etapa: quien esperaba, con la misma versión, recibe
    el 409 de versión y no un 404 por la unión con la etapa que leyó antes de esperar."""
    pk, one, two = sales.opps["ana"], at(sales, "cotizado"), at(sales, "contactado")
    waited, outcome = held_race(
        monkeypatch,
        (sales.ana, lambda c: move(c, pk, {"stage_id": one, "version": 1})),
        (sales.ana, lambda c: move(c, pk, {"stage_id": two, "version": 1})),
    )
    assert waited
    assert outcome == {"first": (200, None), "second": (409, "STALE_VERSION")}

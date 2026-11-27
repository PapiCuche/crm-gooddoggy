"""F11-07 (#1179; ADR-049 §7, §8, E2): `…/automations/`. Permisos por método y rol, la semana en
`SIMULATE` antes de `ON` por la API (de `OFF` directo, 6 d 23 h, justo 7 d y al volver a
`SIMULATE`), versión vieja, parámetros cerrados, cuerpo que no elige organización ni interruptor
global y aislamiento entre organizaciones en las dos direcciones. F11-07b (#1214): el historial,
con el sujeto «sin acceso» fuera del alcance y un número de consultas que no crece con la página.
Middleware, sesión, motor de autorización y PostgreSQL con el rol `crm_app`."""

import datetime as dt
import threading
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from django.db import connection, connections
from django.test import Client
from django.test.utils import CaptureQueriesContext

from apps.access.models import MembershipRole
from apps.automations import commands
from apps.automations.api.views import STALE, TOO_SHORT
from apps.automations.params import CONVERSATION_UNANSWERED as CONV
from apps.automations.params import QUOTE_NO_RESPONSE as QUOTE
from apps.automations.params import defaults
from apps.conversations.models import Conversation
from apps.quotes.models import Quote
from core.tenancy.scope import tenant_scope
from tests import test_conversation_scopes as scopes
from tests.factories import make_user, sign_in
from tests.test_authorization import give
from tests.test_automations_foundation import AUDIT, RULE, team
from tests.test_conversations_api import UNAUTHENTICATED
from tests.test_member_branch import DENIED
from tests.test_memberships import ctx, join
from tests.test_quotes_read_api import make_quote
from tests.test_self_context import NOT_FOUND, reply, signed
from tests.test_team_writes import send

crm, inbox, real_stack = scopes.crm, scopes.inbox, scopes.real_stack
__all__ = ["crm", "inbox", "real_stack", "team"]  # los fixtures, para pytest
pytestmark = pytest.mark.usefixtures("tenant_db")
DB = psycopg.Connection[Any]
BASE = "/api/v1/o/org-a/automations/"
RULES, PAUSE, RESUME, RUNS = BASE + "rules/", BASE + "pause/", BASE + "resume/", BASE + "runs/"
RUN = (
    "INSERT INTO automation_runs (organization_id, rule_id, rule_version, rule_key, subject_type,"
    " subject_id, episode_at, step, mode, status, closed_at) SELECT %s, %s, 1, %s, %s, %s,"
    " now() - make_interval(mins => n), 1, 'SIMULATE', CASE n WHEN 1 THEN 'DONE' ELSE 'CLOSED' END,"
    " CASE n WHEN 1 THEN NULL ELSE now() END FROM generate_series(1, %s) AS n"
)
STATE = (
    "SELECT organization_id, key, mode, version, simulate_since FROM automation_rules ORDER BY 1"
)


def rule(db: DB, org: UUID, key: str, mode: str = "OFF") -> UUID:
    pk = uuid4()
    db.execute(RULE, [pk, org, key, mode, "{}", "2026-11-01" if mode == "SIMULATE" else None])
    return pk


def runs(db: DB, org: UUID, rule_id: UUID, subject: UUID, count: int = 1, kind: str = "") -> None:
    key, kind = (QUOTE, "quote") if kind == "quote" else (CONV, "conversation")
    db.execute(RUN, [org, rule_id, key, kind, subject, count])


def patch(client: Client, key: str, body: Any) -> Any:
    return send(client, "patch", RULES + key + "/", body)


def rules(response: Any) -> dict[str, dict[str, Any]]:
    assert response.status_code == 200, response.content
    return {found["key"]: found for found in response.json()["rules"]}


def test_each_role_gets_only_its_methods(team: SimpleNamespace, migrator: DB) -> None:
    """Owner y Administradora: todo. Supervisor: leer y pausar. Vendedora y sin rol: nada (403).
    De otra organización: 404. Sin sesión: 401."""
    crm = team
    calls = (("get", BASE, None), ("patch", RULES + CONV + "/", {"version": 0, "mode": "OFF"}),
             ("post", PAUSE, None), ("post", RESUME, None), ("get", RUNS, None))  # fmt: skip
    expected = {
        crm.ana: (200, 200, 200, 200, 200), crm.ada: (200, 200, 200, 200, 200),
        crm.sam: (200, 403, 200, 403, 200), crm.vera: (403,) * 5, crm.bea: (403,) * 5,
        crm.luis: (404,) * 5,
    }  # fmt: skip
    for who, statuses in expected.items():
        got = tuple(send(signed(who), m, path, body).status_code for m, path, body in calls)
        assert got == statuses, who.email
    for method, path, body in calls:
        assert reply(send(Client(), method, path, body)) == UNAUTHENTICATED
    assert reply(signed(crm.vera).get(RUNS)) == DENIED
    assert reply(signed(crm.luis).get(RUNS)) == NOT_FOUND
    assert reply(patch(signed(crm.sam), "custom", {"version": 0})) == DENIED  # permiso primero
    assert reply(patch(signed(crm.ana), "custom", {"version": 0})) == NOT_FOUND
    assert migrator.execute(STATE).fetchall() == []  # nadie configuró nada
    # `kill_switch` solo pausa; `manage` solo también pausa y reanuda; `view` solo, ninguno
    give(crm.a, crm.m["bea"], {"automations.kill_switch": None})
    bea = signed(crm.bea)
    assert [send(bea, "post", url, None).status_code for url in (PAUSE, RESUME)] == [200, 403]
    assert send(bea, "get", BASE, None).status_code == 403
    give(crm.a, crm.m["vera"], {"automations.manage": None})
    vera = signed(crm.vera)
    assert [send(vera, "post", url, None).status_code for url in (RESUME, PAUSE)] == [200, 200]
    viewer = make_user(email="vic@example.com")
    give(crm.a, join(crm.a, viewer).pk, {"automations.view": None})
    assert [send(signed(viewer), "post", url, None).status_code for url in (PAUSE, RESUME)] == [
        403, 403]  # fmt: skip


def test_reads_every_registered_rule_and_the_switches(
    crm: SimpleNamespace, settings: Any, migrator: DB
) -> None:
    ana = signed(crm.ana)
    state = ana.get(BASE).json()
    assert state == {
        "global_enabled": True, "paused": False, "paused_changed_at": None,
        "rules": [{"key": key, "mode": "OFF", "params": defaults(key), "version": 0,
                   "simulate_since": None, "on_allowed_at": None, "updated_at": None}
                  for key in (CONV, QUOTE)],
    }  # fmt: skip
    for value in (False, "true", 1, None):  # solo `True` lo enciende; la API solo lo lee
        settings.AUTOMATIONS_ENABLED = value
        assert ana.get(BASE).json()["global_enabled"] is False
    # apagado global: una regla sí pasa a `SIMULATE`, y la respuesta dice que nada se ejecuta
    done = patch(ana, CONV, {"version": 0, "mode": "SIMULATE"})
    assert done.json()["global_enabled"] is False and rules(done)[CONV]["mode"] == "SIMULATE"
    assert settings.AUTOMATIONS_ENABLED is None  # ninguna llamada lo toca
    paused = send(ana, "post", PAUSE, None).json()
    assert paused["paused"] is True and paused["paused_changed_at"] is not None
    assert set(paused) == {"global_enabled", "paused", "paused_changed_at"}
    assert send(signed(crm.sam), "get", BASE, None).json()["paused"] is True
    assert send(ana, "post", RESUME, None).json()["paused"] is False
    assert [(a, c, actor) for (a, _, c, _, actor) in migrator.execute(AUDIT).fetchall()] == [
        ("automation.rule.updated", {"mode": ["OFF", "SIMULATE"]}, crm.ana.pk),
        ("automation.paused", {"paused": [False, True]}, crm.ana.pk),
        ("automation.resumed", {"paused": [True, False]}, crm.ana.pk),
    ]


def test_on_needs_seven_days_in_simulate_through_the_api(
    crm: SimpleNamespace, migrator: DB, monkeypatch: pytest.MonkeyPatch
) -> None:
    ana = signed(crm.ana)
    refused = (409, f'{{"code":"SIMULATION_TOO_SHORT","message":"{TOO_SHORT}"}}'.encode())
    assert reply(patch(ana, QUOTE, {"version": 0, "mode": "ON"})) == refused  # OFF directo a ON
    assert migrator.execute(STATE).fetchall() == [] and migrator.execute(AUDIT).fetchall() == []
    since = dt.datetime.fromisoformat(
        rules(patch(ana, QUOTE, {"version": 0, "mode": "SIMULATE"}))[QUOTE]["simulate_since"]
    )
    week = dt.timedelta(days=7)
    assert rules(ana.get(BASE))[QUOTE]["on_allowed_at"] == f"{(since + week).isoformat()[:-6]}Z"

    def at(when: dt.datetime) -> None:
        monkeypatch.setattr(commands, "_db_now", lambda alias: when)

    for short in (dt.timedelta(days=6, hours=23), week - dt.timedelta(microseconds=1)):
        at(since + short)
        assert reply(patch(ana, QUOTE, {"version": 1, "mode": "ON"})) == refused
    at(since + dt.timedelta(days=2))  # salir y volver a SIMULATE: la cuenta vuelve a empezar
    assert rules(patch(ana, QUOTE, {"version": 1, "mode": "OFF"}))[QUOTE]["simulate_since"] is None
    again = since + dt.timedelta(days=2)
    assert rules(patch(ana, QUOTE, {"version": 2, "mode": "SIMULATE"}))[QUOTE]["version"] == 3
    at(since + week)  # 7 días desde la primera entrada, 5 desde la segunda
    assert reply(patch(ana, QUOTE, {"version": 3, "mode": "ON"})) == refused
    assert migrator.execute(STATE).fetchall() == [(crm.a, QUOTE, "SIMULATE", 3, again)]
    at(again + week)  # justo 7 días
    on = rules(patch(ana, QUOTE, {"version": 3, "mode": "ON"}))[QUOTE]
    assert [on[k] for k in ("mode", "version", "simulate_since", "on_allowed_at")] == [
        "ON",
        4,
        None,
        None,
    ]
    modes = [c.get("mode") for (_, _, c, _, _) in migrator.execute(AUDIT).fetchall()]
    assert modes == [["OFF", "SIMULATE"], ["SIMULATE", "OFF"], ["OFF", "SIMULATE"],
                     ["SIMULATE", "ON"]]  # fmt: skip


def test_stale_version_closed_params_and_a_body_that_chooses_nothing_else(
    crm: SimpleNamespace, migrator: DB, settings: Any
) -> None:
    other = rule(migrator, crm.b, CONV, "SIMULATE")
    ana = signed(crm.ana)
    sneaky = {
        "version": 0, "params": {"max_recipients": 2}, "organization_id": str(crm.b),
        "key": QUOTE, "simulate_since": "2020-01-01T00:00:00Z", "global_enabled": True,
        "updated_by_user_id": str(crm.luis.pk), "id": str(other), "mode": "OFF",
    }  # fmt: skip
    settings.AUTOMATIONS_ENABLED = False
    made = patch(ana, CONV, sneaky)
    assert made.json()["global_enabled"] is False and settings.AUTOMATIONS_ENABLED is False
    assert rules(made)[CONV]["params"]["max_recipients"] == 2
    owners = "SELECT organization_id, key, mode, version, simulate_since, updated_by_user_id, id"
    rows = migrator.execute(owners + " FROM automation_rules ORDER BY 1").fetchall()
    mine = next(row for row in rows if row[0] == crm.a)
    assert mine[1:6] == (CONV, "OFF", 1, None, crm.ana.pk) and mine[6] != other
    assert (crm.b, CONV, "SIMULATE", 1) in [row[:4] for row in rows]  # B, sin tocar
    stale = (409, f'{{"code":"STALE_VERSION","message":"{STALE}"}}'.encode())
    for version in (0, 2):
        assert reply(patch(ana, CONV, {"version": version, "mode": "SIMULATE"})) == stale
    assert reply(patch(ana, QUOTE, {"version": 3, "mode": "SIMULATE"})) == stale  # sin fila
    for bad in ({"note": "Ana Pérez"}, {"max_recipients": 0}, {"max_recipients": 4},
                {"max_recipients": "3"}, {"max_recipients": True},
                {"opens_at": "20:00"}):  # fmt: skip
        response = patch(ana, CONV, {"version": 1, "params": bad})
        assert response.status_code == 400, bad
        body = response.json()
        assert body["code"] == "VALIDATION_ERROR" and set(body["fields"]) == {"params"}
        assert "Ana" not in response.content.decode()  # nunca repite el valor
    for bad_body in ({"version": -1}, {"version": True}, {"mode": "ON"},
                     {"version": 1, "mode": "X"}, {"version": 1, "params": [1]},
                     {"version": 2**31}):  # fmt: skip
        assert patch(ana, CONV, bad_body).status_code == 400, bad_body
    assert "S3CR3T" not in patch(ana, CONV, {"version": 1, "mode": "S3CR3T"}).content.decode()
    assert [row[:4] for row in migrator.execute(STATE).fetchall()] == sorted(
        [(crm.a, CONV, "OFF", 1), (crm.b, CONV, "SIMULATE", 1)])  # fmt: skip
    assert len(migrator.execute(AUDIT).fetchall()) == 1  # solo el cambio que valía
    paused = send(ana, "post", PAUSE, {"paused": False, "global_enabled": True}).json()
    assert paused == paused | {"paused": True, "global_enabled": False}


def test_other_organizations_never_cross_in_either_direction(
    crm: SimpleNamespace, migrator: DB
) -> None:
    rule(migrator, crm.a, CONV)
    rule(migrator, crm.b, CONV, "SIMULATE")
    give(crm.b, crm.m_luis, {"automations.view": None, "automations.manage": None})
    luis, ana, other = signed(crm.luis), signed(crm.ana), BASE.replace("org-a", "org-b")
    assert rules(luis.get(other))[CONV]["mode"] == "SIMULATE"
    assert rules(ana.get(BASE))[CONV]["mode"] == "OFF"
    for method, path in (("get", other), ("post", other + "pause/"),
                         ("patch", other + "rules/" + CONV + "/")):  # fmt: skip
        assert reply(send(ana, method, path, {"version": 1, "mode": "OFF"})) == NOT_FOUND
    assert patch(luis, CONV, {"version": 1, "mode": "SIMULATE"}).status_code == 404  # A, no suya
    assert send(luis, "patch", other + "rules/" + CONV + "/", {"version": 1, "mode": "OFF"}).json()[
        "global_enabled"] is True  # fmt: skip
    assert send(luis, "post", other + "pause/", None).json()["paused"] is True
    assert ana.get(BASE).json()["paused"] is False  # pausar B no pausa A
    assert [row[:4] for row in migrator.execute(STATE).fetchall()] == sorted(
        [(crm.a, CONV, "OFF", 1), (crm.b, CONV, "OFF", 2)])  # fmt: skip


def test_two_writers_at_the_same_version_of_an_existing_rule(
    crm: SimpleNamespace, migrator: DB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """El bloqueo y `WHERE version`: una gana (200), la otra 409; un cambio y una auditoría."""
    rule(migrator, crm.a, CONV)
    real, gate, got = commands._db_now, threading.Barrier(2, timeout=10), []
    slow = lambda alias: threading.Event().wait(0.4) or real(alias)  # noqa: E731 — leen la v1
    monkeypatch.setattr(commands, "_db_now", slow)

    def write(body: dict[str, Any]) -> None:
        try:
            client = signed(crm.ana)
            gate.wait()
            got.append(patch(client, CONV, {"version": 1, **body}).status_code)
        finally:
            connections.close_all()

    bodies = ({"mode": "SIMULATE"}, {"params": {"max_recipients": 1}})
    threads = [threading.Thread(target=write, args=(body,)) for body in bodies]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert sorted(got) == [200, 409]
    assert [row[2:4] for row in migrator.execute(STATE).fetchall()] in ([("SIMULATE", 2)],
                                                                       [("OFF", 2)])  # fmt: skip
    assert len(migrator.execute(AUDIT).fetchall()) == 1


def test_an_internal_value_error_is_a_500_without_its_message(
    crm: SimpleNamespace, migrator: DB, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*args: Any, **kwargs: Any) -> None:
        raise ValueError("internal-detail")

    monkeypatch.setattr(commands, "record", broken)
    client = Client(raise_request_exception=False)
    sign_in(client, crm.ana)
    assert reply(patch(client, CONV, {"version": 0, "mode": "SIMULATE"})) == (
        500, b'{"code":"INTERNAL_ERROR"}')  # fmt: skip
    assert migrator.execute(STATE).fetchall() == []  # deshecho


NUMBERS = "SELECT id, number FROM conversations UNION ALL SELECT id, number FROM quotes"


def history(who: Any, url: str = RUNS) -> list[dict[str, Any]]:
    response = signed(who).get(url)
    assert response.status_code == 200, response.content
    body = response.content.decode()  # ni nombres de contacto ni correos
    assert not [word for word in ("vera", "nobody", "@example") if word in body]
    return response.json()["results"]  # type: ignore[no-any-return]


def shown(found: list[dict[str, Any]]) -> dict[UUID, str]:
    """El id y el número de los sujetos visibles; los demás, «sin acceso» sin id ni número."""
    hidden = [r["subject"] for r in found if not r["subject"]["visible"]]
    assert all(s == {"type": s["type"], "id": None, "number": None, "visible": False}
               for s in hidden)  # fmt: skip
    seen = [r["subject"] for r in found if r["subject"]["visible"]]
    return {UUID(s["id"]): s["number"] for s in seen}


def test_history_shows_the_subject_only_within_the_viewers_scope(
    inbox: SimpleNamespace, migrator: DB
) -> None:
    """vera (Vendedora, `OWN`) con `automations.view`: el número de sus conversaciones y
    cotizaciones; sam (Supervisor) por equipo; con `conversations.view` y sin `quotes.view`, solo
    las conversaciones; sin ninguno de los dos, nada. Un sujeto de B en una ejecución de A: «sin
    acceso» también para la Owner."""
    conv, quote = rule(migrator, inbox.a, CONV), rule(migrator, inbox.a, QUOTE)
    for pk in inbox.convs.values():  # también la de B: `subject_id` no tiene FK
        runs(migrator, inbox.a, conv, pk)
    quotes = {name: make_quote(org, who, inbox.ids[contact]) for name, org, who, contact in (
        ("vera", inbox.a, inbox.vera, "owned"), ("ana", inbox.a, inbox.ana, "plain"),
        ("b", inbox.b, inbox.luis, "b"))}  # fmt: skip
    for pk in quotes.values():
        runs(migrator, inbox.a, quote, pk, kind="quote")
    number = dict(migrator.execute(NUMBERS).fetchall())
    give(inbox.a, inbox.m["vera"], {"automations.view": None})

    def sees(who: Any) -> dict[UUID, str]:
        found = history(who)
        assert len(found) == 10
        return shown(found)

    def of(*pks: UUID) -> dict[UUID, str]:
        return {pk: number[pk] for pk in pks}

    convs, mine = inbox.convs, quotes["vera"]
    assert sees(inbox.vera) == of(convs["vera"], convs["both"], convs["nobody"], mine)
    assert sees(inbox.sam) == of(*(convs[n] for n in ("sam", "team", "both", "nobody")), mine)
    every = [pk for name, pk in convs.items() if name != "b"]
    assert sees(inbox.ana) == of(*every, mine, quotes["ana"])  # ni la conversación ni la de B
    reader = make_user(email="rita@example.com")
    give(inbox.a, join(inbox.a, reader).pk,
         {"automations.view": None, "conversations.view": "ORGANIZATION"})  # fmt: skip
    assert sees(reader) == of(*every)  # sin `quotes.view`: ninguna cotización
    quinn = make_user(email="quinn@example.com")  # `quotes.view` de la organización, sin chats
    give(inbox.a, join(inbox.a, quinn).pk, {"automations.view": None, "quotes.view":
         "ORGANIZATION", "quotes.update": "OWN"})  # fmt: skip
    assert sees(quinn) == of(mine, quotes["ana"])  # el alcance de `quotes.view`, no el de update
    with tenant_scope(ctx(inbox.a)):  # sin `conversations.view` ni `quotes.view`
        seller = MembershipRole.objects.filter(membership_id=inbox.m["vera"], role__code="seller")
        assert seller.delete()[0] == 1
    assert sees(inbox.vera) == {}


def test_history_never_crosses_organizations_and_shows_only_ids(
    inbox: SimpleNamespace, migrator: DB
) -> None:
    """Las ejecuciones de B no salen en A ni al revés; una de B sobre una conversación de A es
    «sin acceso» para quien ve todas las de B. Cada fila lleva ids y códigos, nunca nombres."""
    mine, theirs = rule(migrator, inbox.a, CONV), rule(migrator, inbox.b, CONV)
    runs(migrator, inbox.a, mine, inbox.convs["ana"])
    migrator.execute("UPDATE automation_runs SET recipients = ARRAY[%s]::uuid[], reason = NULL",
                     [inbox.m["vera"]])  # fmt: skip
    runs(migrator, inbox.b, theirs, inbox.convs["b"])
    runs(migrator, inbox.b, theirs, inbox.convs["ana"])  # el id de una conversación de A
    give(inbox.b, inbox.m_luis, {"automations.view": None, "conversations.view": "ORGANIZATION"})
    [row] = history(inbox.ana)
    assert row == row | {
        "rule_key": CONV, "rule_version": 1, "step": 1, "mode": "SIMULATE", "status": "DONE",
        "reason": None, "recipients": [str(inbox.m["vera"])], "task_ids": [], "attempts": 0,
        "closed_at": None, "subject": {"type": "conversation", "id": str(inbox.convs["ana"]),
                                       "number": row["subject"]["number"], "visible": True},
    }  # fmt: skip
    assert set(row) == {"id", "rule_key", "rule_version", "subject", "episode_at", "step", "mode",
                        "status", "reason", "recipients", "task_ids", "attempts", "created_at",
                        "closed_at"}  # fmt: skip
    other = RUNS.replace("org-a", "org-b")
    assert set(shown(history(inbox.luis, other))) == {inbox.convs["b"]}
    assert len(history(inbox.luis, other)) == 2
    assert reply(signed(inbox.ana).get(other)) == NOT_FOUND
    assert reply(signed(inbox.luis).get(RUNS)) == NOT_FOUND


def test_the_lists_hide_no_rows_the_history_would_have_to_hide(crm: SimpleNamespace) -> None:
    """El sujeto se ve con `scoped()` sobre `Conversation.objects` y `Quote.objects` (el
    historial no puede importar sus selectores ni su API: ADR-049 §1). Las listas y fichas parten
    de esos selectores con `ScopeFilter`: si un selector filtrara más filas o una vista declarara
    `hidden_unless`, el historial tendría que ocultar lo mismo."""
    from apps.conversations import selectors as chats
    from apps.conversations.api.conversations import ConversationsView, ConversationView
    from apps.quotes import selectors as quotes
    from apps.quotes.api.quotes import QuotesView, QuoteView

    for view in (ConversationsView, ConversationView, QuotesView, QuoteView):
        assert getattr(view, "hidden_unless", None) is None, view.__name__
    with tenant_scope(ctx(crm.a)):
        for listed, model in ((chats.conversations(), Conversation), (quotes.quotes(), Quote)):
            assert str(listed.values("pk").query) == str(model.objects.values("pk").query)


def test_a_page_of_runs_costs_the_same_queries_whatever_its_size(
    inbox: SimpleNamespace, migrator: DB
) -> None:
    conv = rule(migrator, inbox.a, CONV)
    ana = signed(inbox.ana)
    runs(migrator, inbox.a, conv, inbox.convs["ana"], count=3)
    with CaptureQueriesContext(connection) as few:
        assert len(ana.get(RUNS).json()["results"]) == 3
    runs(migrator, inbox.a, conv, inbox.convs["vera"], count=60)
    with CaptureQueriesContext(connection) as page:
        found = ana.get(RUNS).json()
    assert len(found["results"]) == 50 and found["next"] is not None
    assert len(page.captured_queries) == len(few.captured_queries)
    [sql] = [q["sql"] for q in page.captured_queries if 'FROM "automation_runs"' in q["sql"]]
    assert '"automation_runs"."organization_id" =' in sql  # `TenantManager`, además de RLS
    ids = [r["id"] for r in found["results"]]
    assert ids == sorted(ids, reverse=True)  # lo más reciente primero (UUIDv7)

"""F11-05a (#1180; ADR-049): el barrido en `SIMULATE` con PostgreSQL, `crm_app`, hilos y
conexiones reales; el reloj de la pasada se mueve (`sweep.clock`). Datos ficticios."""

import datetime as dt
import threading
from collections import Counter
from importlib import import_module
from itertools import count
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import psycopg
import pytest
from django.conf import settings
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.automations import sweep, tasks
from apps.automations.params import defaults
from apps.contacts.models import Contact
from apps.conversations import commands, delivery
from apps.conversations.models import ChannelAccount, Conversation
from core.tenancy.context import TenantContext
from core.tenancy.scope import tenant_scope
from tests.factories import make_user
from tests.logs import log_text
from tests.test_authorization import give
from tests.test_contacts_api import crm
from tests.test_conversation_commands import inbound, outbound, run
from tests.test_conversations_awaiting_reply import app_conninfo, move, since, status
from tests.test_memberships import ctx, join

__all__ = ["crm"]
pytestmark = pytest.mark.usefixtures("tenant_db")
type DB = psycopg.Connection[Any]
type NS = SimpleNamespace
type MP = pytest.MonkeyPatch
type LOGS = pytest.LogCaptureFixture
BASE = dt.datetime(2030, 1, 7, 17, 0, tzinfo=dt.UTC)  # lunes, 12:00 en Lima
SECRET = "Mi DNI es 12345678"  # noqa: S105 — texto de prueba: nunca en ejecuciones ni logs
RUNS = (
    "SELECT subject_id, episode_at, mode, status, reason, recipients, attempts, task_ids"
    " FROM automation_runs ORDER BY created_at, id"
)
STATE = (  # mensajes, eventos (`message.*` o cualquiera), conversaciones, cotizaciones, ventas
    "SELECT (SELECT count(*) FROM messages), (SELECT count(*) FROM outbox_events), (SELECT"
    " array_agg((status, version, updated_at, awaiting_reply_since)::text) FROM conversations),"
    " (SELECT array_agg((status, version)::text) FROM quotes),"
    " (SELECT array_agg((stage_id, version)::text) FROM opportunities)"
)
_numbers = count(1)
OPEN_UQ = import_module("apps.automations.migrations.0002_automation_runs_open_uq")


def rule(migrator: DB, org: UUID) -> UUID:
    found = migrator.execute(
        "INSERT INTO automation_rules (organization_id, key, mode, params, version,"
        " simulate_since) VALUES (%s, %s, 'SIMULATE', '{}', 1, now()) RETURNING id",
        [org, sweep.KEY],
    ).fetchone()
    return found[0]  # type: ignore[index,no-any-return]


def events(caplog: pytest.LogCaptureFixture, name: str) -> list[dict[str, Any]]:
    return [r.msg for r in caplog.records if isinstance(r.msg, dict) and r.msg["event"] == name]


@pytest.fixture
def w(crm: SimpleNamespace, migrator: DB, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """`crm` (ana Owner, vera Vendedora y sam Supervisor, en «ventas»), cuentas y la regla en A."""
    accounts = {}
    for org in (crm.a, crm.b):
        with tenant_scope(ctx(org)):
            accounts[org] = ChannelAccount.objects.create(
                platform="SANDBOX", name="Sandbox", external_account_id=f"s-{org}"
            )
    clock = [BASE]
    monkeypatch.setattr(sweep, "clock", lambda _: clock[0])
    team = migrator.execute("SELECT id FROM teams WHERE slug = 'ventas'").fetchone()[0]  # type: ignore[index]
    return SimpleNamespace(
        **vars(crm), accounts=accounts, account_a=accounts[crm.a], clock=clock, team=team,
        contact_a=SimpleNamespace(pk=crm.ids["plain"]), rule=rule(migrator, crm.a),
    )  # fmt: skip


def waiting(w: SimpleNamespace, org: UUID | None = None, **over: Any) -> UUID:
    org = org or w.a
    with tenant_scope(ctx(org)):
        contact = Contact.objects.create(display_name="Cliente ficticio")
        made = Conversation.objects.create(
            number=f"CONV-{next(_numbers):05d}", contact_id=contact.pk,
            channel_account_id=w.accounts[org].pk, last_message_at=timezone.now(),
            last_message_direction="INBOUND", last_message_preview=SECRET[:20], **over,
        )  # fmt: skip
    return made.pk  # type: ignore[no-any-return]


def tick(w: SimpleNamespace, org: UUID | None = None, **step: int) -> Counter[str]:
    w.clock[0] += dt.timedelta(**(step or {"days": 7}))  # por omisión, el lunes siguiente
    return sweep.evaluate(TenantContext(org or w.a, "celery"))


def runs(migrator: DB) -> list[tuple[Any, ...]]:
    return migrator.execute(RUNS).fetchall()


def test_switched_off_the_beat_and_the_task_touch_no_database(w: NS, monkeypatch: MP) -> None:
    monkeypatch.setattr(settings, "AUTOMATIONS_ENABLED", False)
    with CaptureQueriesContext(connection) as queries:  # los cuerpos: ninguna consulta
        assert tasks.sweep.run.__wrapped__() == {"organizations": 0, "enqueued": 0, "failed": 0}
        assert tasks.evaluate.run.__wrapped__(str(w.a)) == {}
    assert len(queries.captured_queries) == 0
    with CaptureQueriesContext(connection) as queries:  # como tarea: solo la guarda de tenancy
        tasks.sweep()
        tasks.evaluate(str(w.a))
    assert [q["sql"].startswith("SELECT current_setting(") for q in queries] == [True, True]
    monkeypatch.setattr(settings, "AUTOMATIONS_ENABLED", True)
    sent: list[dict[str, Any]] = []
    monkeypatch.setattr(tasks.evaluate, "apply_async", lambda **kw: sent.append(kw))
    assert tasks.sweep()["enqueued"] == 2
    assert sorted(kw["kwargs"]["organization_id"] for kw in sent) == sorted([str(w.a), str(w.b)])
    assert {kw["expires"] for kw in sent} == {60}
    assert settings.CELERY_BEAT_SCHEDULE["automations.sweep"] == {
        "task": "automations.sweep", "schedule": 60.0, "options": {"expires": 60},
    }  # fmt: skip


def test_simulate_writes_a_run_and_no_task_message_or_state(
    w: NS, migrator: DB, caplog: LOGS
) -> None:
    mine = waiting(w, assigned_user_id=w.vera.pk)
    before = migrator.execute(STATE).fetchone()
    assert tick(w) == Counter(simulated=1)
    [(subject, episode, mode, state, reason, who, attempts, made)] = runs(migrator)
    assert (subject, mode, state, reason, who, attempts, made) == (
        mine, "SIMULATE", "DONE", None, [w.vera.pk], 0, [],
    )  # fmt: skip
    assert episode == since(migrator, SimpleNamespace(pk=mine))
    assert tick(w) == Counter() and len(runs(migrator)) == 1  # repetir no crea otra
    assert migrator.execute(STATE).fetchone() == before  # ni mensajes, ni eventos, ni estados
    assert migrator.execute("SELECT count(*) FROM tasks").fetchone() == (0,)
    seen = log_text(caplog) + str(runs(migrator))
    assert SECRET[:8] not in seen and str(mine) not in log_text(caplog)


def test_paused_off_or_on_write_nothing_and_a_pause_stops_the_pass(
    w: NS, migrator: DB, monkeypatch: MP
) -> None:
    waiting(w, assigned_user_id=w.vera.pk)
    migrator.execute(
        "INSERT INTO automation_switches (organization_id, paused, changed_at)"
        " VALUES (%s, true, now())", [w.a],
    )  # fmt: skip
    assert tick(w) == Counter()
    migrator.execute("UPDATE automation_switches SET paused = false")
    monkeypatch.setattr(settings, "AUTOMATIONS_ENABLED", False)
    assert tick(w) == Counter()
    monkeypatch.setattr(settings, "AUTOMATIONS_ENABLED", True)
    migrator.execute("UPDATE automation_rules SET mode = 'ON', simulate_since = NULL")
    assert tick(w) == Counter(on_pending=1)  # crear tareas en `ON` es de F11-05b
    migrator.execute("UPDATE automation_rules SET mode = 'OFF'")
    assert tick(w) == Counter() and runs(migrator) == []
    migrator.execute("UPDATE automation_rules SET mode = 'SIMULATE', simulate_since = now()")
    waiting(w, assigned_user_id=w.vera.pk)
    real = sweep.recipients  # pausar a mitad de pasada: se detiene en el siguiente sujeto

    def pause_after(*args: Any) -> list[UUID]:
        migrator.execute("UPDATE automation_switches SET paused = true")  # otra conexión
        return real(*args)

    monkeypatch.setattr(sweep, "recipients", pause_after)
    assert tick(w) == Counter(simulated=1, stopped=1)
    assert len(runs(migrator)) == 1


@pytest.mark.parametrize(("utc", "zone", "inside"), [
    ("2030-01-12T23:59", "America/Lima", True),  # sábado 18:59
    ("2030-01-13T00:00", "America/Lima", False),  # sábado 19:00: cierra
    ("2030-01-13T17:00", "America/Lima", False),  # domingo
    ("2030-03-09T14:30", "America/New_York", True),  # sábado 09:30 EST
    ("2030-03-11T13:30", "America/New_York", True),  # lunes 09:30 EDT: la misma hora UTC
    ("2030-11-02T22:30", "America/New_York", True),  # sábado 18:30 EDT
    ("2030-11-04T23:30", "America/New_York", True),  # lunes 18:30 EST
    ("2030-11-01T23:30", "America/New_York", False),  # viernes 19:30 EDT
    ("2030-01-07T17:00", "Lima/Nowhere", False),  # zona desconocida: no abre
])  # fmt: skip
def test_working_hours_follow_the_zone_and_its_dst(utc: str, zone: str, inside: bool) -> None:
    now = dt.datetime.fromisoformat(utc).replace(tzinfo=dt.UTC)
    assert sweep.in_hours(now, zone, defaults(sweep.KEY)) is inside


def test_outside_hours_nothing_new_but_an_answered_one_still_closes(w: NS, migrator: DB) -> None:
    first = waiting(w, assigned_user_id=w.vera.pk)
    assert tick(w) == Counter(simulated=1)
    with tenant_scope(ctx(w.a)):
        Conversation.objects.filter(pk=first).update(last_message_direction="OUTBOUND")
    waiting(w, assigned_user_id=w.vera.pk)
    assert tick(w, days=6) == Counter(closed=1)  # domingo: ningún aviso nuevo
    assert [r[3] for r in runs(migrator)] == ["CLOSED"]
    assert tick(w, days=1) == Counter(simulated=1)  # lunes: avisa


def test_recipients_chain_only_picks_who_sees_it(w: NS, migrator: DB) -> None:
    other = make_user(email="otra@example.com")
    give(w.a, join(w.a, other).pk, {"conversations.view": "OWN"})
    give(w.a, w.m["vera"], {"conversations.assign": None})  # vendedora OWN que puede asignar

    def chosen(convo: UUID, limit: int = 3) -> list[UUID]:
        with tenant_scope(ctx(w.a)) as system:
            return sweep.recipients(system, Conversation.objects.get(pk=convo), limit)

    hers = waiting(w, assigned_user_id=w.vera.pk, assigned_team_id=w.team)
    assert chosen(hers) == [w.vera.pk]
    migrator.execute("UPDATE organization_memberships SET status = 'SUSPENDED' WHERE id = %s",
                     [w.m["vera"]])  # fmt: skip
    assigners = sorted([w.ana.pk, w.sam.pk])  # vera, suspendida, ya no; sam ve las del equipo
    assert chosen(hers) == assigners and chosen(hers, 1) == assigners[:1]
    migrator.execute("UPDATE team_members SET team_role = 'SUPERVISOR' WHERE membership_id = %s",
                     [w.m["sam"]])  # fmt: skip
    assert chosen(hers) == [w.sam.pk]  # el supervisor del equipo, antes que los que asignan
    migrator.execute("UPDATE organization_memberships SET status = 'ACTIVE'")
    theirs = waiting(w, assigned_user_id=other.pk)
    migrator.execute("UPDATE users SET is_active = false WHERE id = %s", [other.pk])
    assert chosen(theirs) == [w.ana.pk]  # ni vera (OWN, no es suya) ni sam (no es del equipo)
    migrator.execute("UPDATE users SET is_active = true WHERE id = %s", [other.pk])
    assert chosen(theirs) == [other.pk]


def test_the_per_user_cap_skips_and_retries_on_the_next_pass(w: NS, migrator: DB) -> None:
    migrator.execute("UPDATE automation_rules SET params = '{\"max_open_per_user\": 1}'")
    first, second = (waiting(w, assigned_user_id=w.vera.pk) for _ in range(2))
    assert tick(w) == Counter(simulated=1, capped=1)
    assert [(r[0], r[3], r[4]) for r in runs(migrator)] == [
        (first, "DONE", None), (second, "SKIPPED", "CAPPED")]  # fmt: skip
    with tenant_scope(ctx(w.a)):
        Conversation.objects.filter(pk=first).update(last_message_direction="OUTBOUND")
    assert tick(w) == Counter(closed=1, simulated=1)  # el tope se libera y se reintenta
    assert {r[0]: r[3] for r in runs(migrator)} == {first: "CLOSED", second: "DONE"}


def test_the_pass_cap_and_three_capped_passes_log_a_storm(
    w: NS, migrator: DB, monkeypatch: MP, caplog: LOGS
) -> None:
    monkeypatch.setattr(sweep, "MAX_PER_PASS", 1)
    subjects = [waiting(w, assigned_user_id=w.vera.pk) for _ in range(5)]
    assert tick(w) == Counter(simulated=1, capped_pass=1)
    assert tick(w) == tick(w) == Counter(simulated=1, capped_pass=1)
    storms = events(caplog, "automations.storm")
    assert len(storms) == 1 and storms[0]["runs"] == 3 and storms[0]["rule"] == sweep.KEY
    assert all(str(s) not in log_text(caplog) for s in subjects)
    assert [r[3] for r in runs(migrator)] == ["DONE"] * 3  # la regla sigue: nadie la apaga sola
    assert migrator.execute("SELECT mode FROM automation_rules").fetchone() == ("SIMULATE",)


class Locked(Exception):  # noqa: N818 — como un `lock_timeout` de PostgreSQL
    sqlstate = "55P03"


def test_a_failure_retries_with_backoff_and_dies_at_the_third(
    w: NS, migrator: DB, monkeypatch: MP, caplog: LOGS
) -> None:
    mine = waiting(w, assigned_user_id=w.vera.pk)
    errors: list[Exception] = [Locked(), RuntimeError(SECRET), RuntimeError(SECRET)]

    def broken(*args: Any) -> list[UUID]:
        raise errors.pop(0)

    monkeypatch.setattr(sweep, "recipients", broken)
    assert tick(w) == Counter(failed=1)
    assert [r[3:5] + (r[6],) for r in runs(migrator)] == [("FAILED", "LOCK_TIMEOUT", 1)]
    assert tick(w, minutes=1) == Counter()  # 2^1 minutos
    assert tick(w, minutes=1) == Counter(failed=1)
    assert tick(w, minutes=3) == Counter()  # 2^2 minutos
    assert tick(w, minutes=1) == Counter(dead=1)
    assert [r[3:5] + (r[6],) for r in runs(migrator)] == [("DEAD", "DB_ERROR", 3)]
    assert tick(w) == Counter()  # `DEAD`: no se reintenta ni abre otra
    dead = events(caplog, "automations.run_dead")
    assert len(dead) == 1 and (dead[0]["rule"], dead[0]["reason"]) == (sweep.KEY, "DB_ERROR")
    assert set(dead[0]) <= {"event", "level", "timestamp", "logger", "organization_id", "rule",
                            "reason", "correlation_id", "request_id"}  # solo códigos  # fmt: skip
    assert SECRET[:8] not in log_text(caplog) and str(mine) not in log_text(caplog)
    with tenant_scope(ctx(w.a)):
        Conversation.objects.filter(pk=mine).update(last_message_direction="OUTBOUND")
    assert tick(w) == Counter(closed=1)


def as_app(org: UUID) -> DB:
    app = psycopg.connect(app_conninfo())  # `crm_app`, con el tenant en su transacción
    app.execute("SELECT set_config('app.tenant_id', %s, true)", [str(org)])
    return app


def test_two_claims_at_once_wait_on_either_unique_key(w: NS, migrator: DB) -> None:
    claim = ("INSERT INTO automation_runs (organization_id, rule_id, rule_version, rule_key,"
             " subject_type, subject_id, episode_at, step, mode, status) VALUES (%s, %s, 1, %s,"
             " 'conversation', %s, %s, 1, 'SIMULATE', 'DONE') ON CONFLICT DO NOTHING"
             " RETURNING id")  # fmt: skip
    index = migrator.execute(OPEN_UQ.STATE, [OPEN_UQ.INDEX]).fetchone()
    assert index == (True, OPEN_UQ.DEFINITION)  # único y parcial, como lo crea la migración
    later = BASE + dt.timedelta(days=1)  # otra hora: solo choca con `automation_runs_open_uq`
    for outcome, second_episode, inserted in (("rollback", BASE, True), ("commit", BASE, False),
                                              ("commit", later, False)):  # fmt: skip
        args = [w.a, w.rule, sweep.KEY, waiting(w)]
        first, second, got = as_app(w.a), as_app(w.a), list[Any]()
        assert first.execute(claim, [*args, BASE]).fetchone() is not None
        waiter = threading.Thread(
            target=lambda c=second, a=[*args, second_episode], out=got: out.append(
                c.execute(claim, a).fetchone()
            )
        )
        waiter.start()
        waiter.join(timeout=1)
        assert waiter.is_alive()  # espera a la transacción de la otra, no falla
        getattr(first, outcome)()
        waiter.join(timeout=10)
        second.commit()
        first.close()
        second.close()
        assert (got[0] is not None) is inserted, (outcome, second_episode)


def test_two_sweeps_at_once_leave_one_run_per_conversation(w: NS, migrator: DB) -> None:
    subjects = {waiting(w, assigned_user_id=w.vera.pk) for _ in range(3)}
    w.clock[0] += dt.timedelta(days=7)
    start, results = threading.Barrier(2), list[Any]([None, None])

    def evaluate(index: int) -> None:
        try:
            start.wait(timeout=10)
            results[index] = sweep.evaluate(TenantContext(w.a, "celery"))
        finally:
            connection.close()

    threads = [threading.Thread(target=evaluate, args=(i,)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert sorted(r[0] for r in runs(migrator)) == sorted(subjects)
    assert sum(r["simulated"] for r in results) == 3


def test_cases_a_and_b_open_no_second_run(w: NS, migrator: DB) -> None:
    first, _ = inbound(w, "wamid.a1")  # a) resuelta sin responder, vuelve a escribir, falla
    convo = SimpleNamespace(pk=first.conversation_id)
    status(w, convo, "RESOLVED", resolved_at=timezone.now())
    inbound(w, "wamid.a2")
    assert tick(w) == Counter(simulated=1)
    reply, _ = outbound(w, convo)
    move(w, reply, "FAILED", "PROVIDER_REJECTED")
    assert since(migrator, convo) == first.created_at  # volvió a antes de la resolución
    assert tick(w) == Counter() and len(runs(migrator)) == 1
    status(w, convo, "CLOSED", closed_at=timezone.now())
    assert tick(w) == Counter(closed=1)
    m1, _ = inbound(w, "wamid.b1")  # b) M1, respuesta en cola, M2, falla, falla otra
    other = SimpleNamespace(pk=m1.conversation_id)
    queued, _ = outbound(w, other)
    m2, _ = inbound(w, "wamid.b2")
    move(w, queued, "FAILED", "PROVIDER_REJECTED")
    assert tick(w) == Counter(simulated=1)
    again, _ = outbound(w, other)
    move(w, again, "FAILED", "PROVIDER_REJECTED")
    assert since(migrator, other) == m1.created_at < m2.created_at  # M2 y luego M1
    assert tick(w) == Counter()
    assert [(r[0], r[1], r[3]) for r in runs(migrator)][1:] == [(other.pk, m2.created_at, "DONE")]


def test_one_run_per_episode_and_an_hour_back_counts_from_closing(w: NS, migrator: DB) -> None:
    first, _ = inbound(w, "wamid.1")
    convo = SimpleNamespace(pk=first.conversation_id)
    assert tick(w) == Counter(simulated=1)
    for n in range(2, 4):  # resolver y volver a escribir: un episodio nuevo cada vez
        status(w, convo, "RESOLVED", resolved_at=timezone.now())
        assert tick(w) == Counter(closed=1) and tick(w) == Counter()
        inbound(w, f"wamid.{n}")
        assert tick(w) == Counter(simulated=1) and tick(w) == Counter()
    status(w, convo, "RESOLVED", resolved_at=timezone.now())
    assert tick(w) == Counter(closed=1)
    closed_at = w.clock[0]
    inbound(w, "wamid.4")  # la reabre y la respuesta falla antes de la pasada (caso a)
    reply, _ = outbound(w, convo)
    move(w, reply, "FAILED", "PROVIDER_REJECTED")
    assert since(migrator, convo) == first.created_at  # la hora de un episodio ya cerrado
    assert tick(w, minutes=5) == Counter()  # 15 minutos desde el cierre, no desde esa hora
    assert tick(w) == Counter(simulated=1) and tick(w) == tick(w) == Counter()
    found = runs(migrator)
    assert [r[3] for r in found] == ["CLOSED"] * 3 + ["DONE"] and len({r[1] for r in found}) == 4
    assert found[-1][1] == closed_at


def test_each_subject_reads_its_row_again_before_claiming_or_closing(w: NS, migrator: DB) -> None:
    fresh = waiting(w, assigned_user_id=w.vera.pk)  # espera desde ahora, no desde hace 15 min
    now = migrator.execute("SELECT now()").fetchone()[0]  # type: ignore[index]
    run_ = sweep.Pass(TenantContext(w.a, "celery"), w.rule, "SIMULATE", 1, defaults(sweep.KEY), now)
    assert sweep.claim(run_, fresh, None, Counter()) == "skipped"
    old = waiting(w, assigned_user_id=w.vera.pk)
    assert tick(w) == Counter(simulated=2)
    [(run_id,)] = migrator.execute(
        "SELECT id FROM automation_runs WHERE subject_id = %s", [old]
    ).fetchall()
    assert sweep.close(run_, old, run_id, Counter()) == "skipped"  # el cliente sigue esperando
    assert [r[3] for r in runs(migrator)] == ["DONE", "DONE"]


def test_echo_or_standby_keep_the_wait_an_opt_out_reply_ends_it(w: NS, migrator: DB) -> None:
    first, _ = inbound(w, "wamid.1")
    convo = first.conversation_id
    assert tick(w) == Counter(simulated=1)
    with tenant_scope(ctx(w.a)) as system:  # otra app o la bandeja nativa: no se registra
        assert delivery.social(system, w.account_a, {"message": {"mid": "m.1", "app_id": "77"}})
    inbound(w, "wamid.2", standby=True)
    assert tick(w) == Counter() and [r[3] for r in runs(migrator)] == ["DONE"]
    run(w, commands.queue_confirmation, conversation_id=convo, body="Listo")  # del sistema
    assert tick(w) == Counter(closed=1)


def test_each_organization_only_sees_and_writes_its_own(w: NS, migrator: DB) -> None:
    rule(migrator, w.b)
    give(w.b, w.m_luis, {"conversations.view": "ORGANIZATION"})
    theirs, mine = waiting(w, org=w.b), waiting(w, assigned_user_id=w.vera.pk)
    assert tick(w) == Counter(simulated=1)
    assert [r[0] for r in runs(migrator)] == [mine]
    assert tick(w, org=w.b) == Counter(no_recipient=1)  # luis ve, pero nadie asigna en B
    give(w.b, w.m_luis, {"conversations.assign": None})
    assert tick(w, org=w.b) == Counter(simulated=1)
    [(subject, who)] = migrator.execute(
        "SELECT subject_id, recipients FROM automation_runs WHERE organization_id = %s", [w.b]
    ).fetchall()
    assert (subject, who) == (theirs, [w.luis.pk])  # RLS de la tabla: test_automations_foundation

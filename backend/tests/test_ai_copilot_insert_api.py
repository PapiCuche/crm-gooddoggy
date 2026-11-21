"""F8-08j (#1026; ADR-034 §9.2, §13 D-F8-05): `POST …/copilot/suggestions/{id}/insert/` con el
stack real (middleware, sesión, CSRF, autorización y PostgreSQL con `crm_app`): el texto guardado
y su auditoría en la transacción de la respuesta, el mismo 404, la evidencia caducada, sin modelo,
sin envío y sin tope; y la purga diaria del texto a los 30 días. Datos ficticios."""

import json
import logging
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from celery.schedules import crontab
from django.db import connection, connections
from django.urls import reverse

from apps.ai_agents import drafts, records, tasks
from apps.ai_agents.api import views
from apps.ai_gateway.throttle import COPILOT
from core.tenancy.context import TenantContextError
from core.tenancy.middleware import UNSCOPED
from core.tenancy.scope import require_scope, tenant_scope
from tests.factories import make_user
from tests.test_ai_copilot_context import conversation
from tests.test_ai_copilot_runtime import FIGURES, SEARCH, desk
from tests.test_ai_copilot_suggest_api import COUNTED, SPENT, ask
from tests.test_ai_gateway import world
from tests.test_authorization import give
from tests.test_member_branch import DENIED
from tests.test_memberships import ctx, join
from tests.test_self_context import NOT_FOUND, real_stack
from tests.test_self_context import signed as plain
from tests.test_team_writes import send

__all__ = ["desk", "real_stack", "world"]  # los fixtures, para pytest
pytestmark = pytest.mark.usefixtures("tenant_db", "real_stack")
DB = psycopg.Connection[Any]
URL = "/api/v1/o/{}/conversations/{}/copilot/suggestions/{}/insert/"
DRAFT = "Cuesta [verificar], ver [verificar] y [verificar]."
FOUND = [{"type": "FIGURE", "start": 7, "end": 18}, {"type": "LINK", "start": 23, "end": 34},
         {"type": "FIGURE", "start": 37, "end": 48}]  # fmt: skip
ROW = (
    "INSERT INTO ai_suggestions (organization_id, run_id, conversation_id, requested_by_user_id,"
    " kind, text, findings, evidence_expires_at, created_at) VALUES (%s, uuidv7(), %s, %s, %s, %s,"
    " %s::jsonb, now() + %s * interval '1 second', now() - %s * interval '1 day') RETURNING id"
)
INSERTED = "SELECT count(*) FROM audit_logs WHERE action = 'ai.suggestion.inserted'"
TEXTS = "SELECT text FROM ai_suggestions WHERE id = %s"


def stored(w: SimpleNamespace, migrator: DB, *, kind: str = "SUGGEST", text: str | None = DRAFT,
           expires: int | None = None, days: int = 0, convo: UUID | None = None,
           org: UUID | None = None, user: Any = None) -> UUID:  # fmt: skip
    """Una fila como la escribe `records.save`, con su edad y su caducidad (en segundos)."""
    values = [org or w.a, convo or w.convo, (user or w.ana).pk, kind, text, json.dumps(FOUND),
              expires, days]  # fmt: skip
    made: UUID = migrator.execute(ROW, values).fetchone()[0]  # type: ignore[index]
    return made


def insert(w: SimpleNamespace, suggestion: UUID, who: Any = None, convo: UUID | None = None,
           org: str = "org-a") -> Any:  # fmt: skip
    return send(plain(who or w.ana), "post", URL.format(org, convo or w.convo, suggestion), {})


def scalar(migrator: DB, sql: str, params: list[Any] | None = None) -> Any:
    return migrator.execute(sql, params).fetchone()[0]  # type: ignore[index]


def untouched(w: SimpleNamespace, migrator: DB) -> list[Any]:
    """Reservas, runs y borradores; el tope por persona; mensajes; outbox; llamadas al modelo."""
    return [migrator.execute(SPENT).fetchone(), migrator.execute(COUNTED).fetchall(),
            scalar(migrator, "SELECT count(*) FROM messages"),
            scalar(migrator, "SELECT count(*) FROM outbox_events"), len(w.fake.calls)]  # fmt: skip


def test_insert_returns_the_stored_text_and_audits_only_ids_and_counts(
    desk: SimpleNamespace, migrator: DB
) -> None:
    desk.fake.replies = [SEARCH, FIGURES, FIGURES]
    suggested = ask(desk).json()  # una sugerencia real, con su marca
    before = untouched(desk, migrator)
    answer = insert(desk, UUID(suggested["id"]))
    assert answer.status_code == 200, answer.content
    assert answer.json() == {"text": suggested["text"]}  # el mismo texto, con su `[verificar]`
    assert "[verificar]" in suggested["text"]
    # sin modelo, sin reserva ni run, sin envío, sin outbox y sin gastar el tope por persona
    assert untouched(desk, migrator) == before
    many = stored(desk, migrator)
    assert insert(desk, many).json() == {"text": DRAFT}
    audit = migrator.execute(
        "SELECT actor_type, actor_id, entity_type, entity_id, changes, metadata FROM audit_logs"
        " WHERE action = 'ai.suggestion.inserted' ORDER BY occurred_at, id"
    ).fetchall()
    convo = str(desk.convo)
    assert audit == [
        ("USER", desk.ana.pk, "ai_suggestion", UUID(suggested["id"]), {},
         {"conversation_id": convo, "findings": {"FIGURE": 1}}),
        ("USER", desk.ana.pk, "ai_suggestion", many, {},
         {"conversation_id": convo, "findings": {"FIGURE": 2, "LINK": 1}}),
    ]  # fmt: skip
    assert "verificar" not in json.dumps(
        migrator.execute("SELECT to_jsonb(a) FROM audit_logs a WHERE action LIKE 'ai.%'").fetchall()
    )


def test_the_audit_lives_in_the_response_transaction_and_without_it_no_text_leaves(
    desk: SimpleNamespace, migrator: DB, monkeypatch: pytest.MonkeyPatch
) -> None:
    suggestion, seen = stored(desk, migrator), []

    class Probe(views.CopilotInsertSerializer):
        @property
        def data(self) -> Any:  # la vista ya auditó; la respuesta aún no se confirmó
            with connection.cursor() as cursor:
                cursor.execute(INSERTED)
                seen.append((cursor.fetchone()[0], scalar(migrator, INSERTED)))
            return super().data

    monkeypatch.setattr(views, "CopilotInsertSerializer", Probe)
    assert insert(desk, suggestion).status_code == 200
    assert seen == [(1, 0)] and scalar(migrator, INSERTED) == 1  # se confirma con la respuesta
    kwargs = {"org_slug": "org-a", "conversation_id": desk.convo, "suggestion_id": suggestion}
    route = reverse("copilot-suggestion-insert", kwargs=kwargs)
    assert route == URL.format("org-a", desk.convo, suggestion)  # la ruta de tenant de siempre:
    assert not UNSCOPED.fullmatch(route) and not COPILOT.match(route)  # con su scope, sin tope

    class Broken(views.CopilotInsertSerializer):
        @property
        def data(self) -> Any:
            raise RuntimeError("fallo tras auditar")

    monkeypatch.setattr(views, "CopilotInsertSerializer", Broken)
    with pytest.raises(RuntimeError):
        insert(desk, suggestion)
    assert scalar(migrator, INSERTED) == 1  # deshecha con la respuesta que no salió

    def refuse(*_: Any, **__: Any) -> None:
        raise RuntimeError("auditoría caída")

    monkeypatch.setattr(views, "CopilotInsertSerializer", Probe)
    monkeypatch.setattr(records, "record", refuse)
    with pytest.raises(RuntimeError):  # sin auditoría, ningún texto
        insert(desk, suggestion)
    assert scalar(migrator, INSERTED) == 1 and len(seen) == 1


def test_expired_evidence_is_a_409_without_text_or_audit(
    desk: SimpleNamespace, migrator: DB
) -> None:
    expired, fresh, timeless = (stored(desk, migrator, expires=s) for s in (-1, 600, None))
    answer = insert(desk, expired)
    assert (answer.status_code, answer.json()) == (409, {"code": "AI_EVIDENCE_EXPIRED"})
    assert "verificar" not in answer.text and scalar(migrator, INSERTED) == 0
    assert [insert(desk, s).status_code for s in (fresh, timeless)] == [200, 200]
    assert scalar(migrator, INSERTED) == 2


def test_a_summary_a_purged_draft_or_one_of_elsewhere_is_the_same_404(
    desk: SimpleNamespace, migrator: DB
) -> None:
    bob, leo = make_user(email="bob@example.com"), make_user(email="leo@example.com")
    give(desk.b, join(desk.b, bob).pk, {"ai.use": None, "conversations.view": "ORGANIZATION"})
    give(desk.a, join(desk.a, leo).pk, {"ai.use": None, "conversations.view": "OWN"})
    mine = conversation(desk.a, {"display_name": "Otro"}, assigned_user_id=desk.ana.pk)
    theirs = conversation(desk.b, {"display_name": "De B"})
    good = stored(desk, migrator)
    other = stored(desk, migrator, convo=mine)
    foreign = stored(desk, migrator, convo=theirs, org=desk.b, user=bob)
    answers = [
        insert(desk, stored(desk, migrator, kind="SUMMARY")),  # un resumen no se inserta
        insert(desk, stored(desk, migrator, text=None)),  # ya purgada
        insert(desk, other),  # de otra conversación, por la ruta de esta
        insert(desk, uuid4()),
        insert(desk, foreign, convo=theirs),  # de B, desde A
        insert(desk, good, who=bob, org="org-b"),  # A desde B
        insert(desk, good, who=bob),  # sin membresía en A
        insert(desk, other, who=leo, convo=mine),  # fuera de su alcance (OWN, de ana)
    ]
    assert [(a.status_code, a.content) for a in answers] == [NOT_FOUND] * len(answers)
    assert scalar(migrator, INSERTED) == 0
    assert insert(desk, good, who=leo).status_code == 200  # la suya, sin asignar
    assert insert(desk, foreign, who=bob, convo=theirs, org="org-b").status_code == 200


@pytest.mark.parametrize("grants", [{"conversations.view": "ORGANIZATION"}, {"ai.use": None}])
def test_each_permission_alone_is_the_same_403_and_the_route_never_counts(
    desk: SimpleNamespace, migrator: DB, grants: dict[str, str | None]
) -> None:
    eva = make_user(email="eva@example.com")
    give(desk.a, join(desk.a, eva).pk, grants)
    answers = [insert(desk, stored(desk, migrator), eva) for _ in range(21)]
    assert [(a.status_code, a.content) for a in answers] == [DENIED] * 21  # nunca un 429
    assert scalar(migrator, INSERTED) == 0 and migrator.execute(COUNTED).fetchall() == []


def test_the_daily_purge_nulls_old_text_in_every_organization_and_logs_counts(
    desk: SimpleNamespace, migrator: DB, caplog: pytest.LogCaptureFixture
) -> None:
    bob = make_user(email="bob@example.com")
    join(desk.b, bob)
    theirs = conversation(desk.b, {"display_name": "De B"})
    old, young = stored(desk, migrator, days=31), stored(desk, migrator, days=29)
    summary = stored(desk, migrator, kind="SUMMARY", days=31)
    foreign = stored(desk, migrator, days=40, convo=theirs, org=desk.b, user=bob)
    kept = "SELECT to_jsonb(s) - 'text' FROM ai_suggestions s ORDER BY id"
    before = migrator.execute(kept).fetchall()
    with caplog.at_level(logging.DEBUG):
        done = tasks.purge_suggestion_text()
    assert done == {"organizations": 2, "purged": 3, "failed": 0}
    texts = [scalar(migrator, TEXTS, [s]) for s in (old, summary, foreign, young)]
    assert texts == [None, None, None, DRAFT]
    assert migrator.execute(kept).fetchall() == before  # los hallazgos y los ids quedan
    assert insert(desk, old).content == NOT_FOUND[1] and insert(desk, young).status_code == 200
    [line] = [r.msg for r in caplog.records if isinstance(r.msg, dict)
              and r.msg.get("event") == "ai.suggestion_text_purged"]  # fmt: skip
    assert {k: line[k] for k in done} == done and "verificar" not in caplog.text
    assert tasks.purge_suggestion_text()["purged"] == 0
    with tenant_scope(ctx(desk.a)), pytest.raises(TenantContextError):
        tasks.purge_suggestion_text()  # de plataforma: nunca con un tenant ya puesto


def test_the_purge_goes_in_batches_and_one_failing_organization_stops_no_other(
    desk: SimpleNamespace, migrator: DB, monkeypatch: pytest.MonkeyPatch
) -> None:
    olds = [stored(desk, migrator, days=31) for _ in range(4)]
    tenant = ctx(desk.a)
    assert records.purge_text(tenant, 30, 1) == 1  # un lote es un lote
    with tenant_scope(tenant), connections[require_scope(tenant)].cursor() as cursor:
        cursor.execute("EXPLAIN " + records.PURGE, [30, 1])
        plan = "\n".join(row[0] for row in cursor.fetchall())
    # una sola vez: en `id IN (… LIMIT n)`, un nested loop bajo RLS la recorre por cada fila
    assert "CTE due" in plan and plan.count("LockRows") == 1
    assert drafts.purge(batch=1) == {"organizations": 2, "purged": 3, "failed": 0}
    assert [scalar(migrator, TEXTS, [s]) for s in olds] == [None] * 4
    again, real = stored(desk, migrator, days=31), records.purge_text

    def flaky(tenant: Any, days: int, batch: int) -> int:
        if tenant.organization_id == desk.b:
            raise RuntimeError("B caída")
        return real(tenant, days, batch)

    monkeypatch.setattr(records, "purge_text", flaky)
    assert drafts.purge() == {"organizations": 2, "purged": 1, "failed": 1}
    assert scalar(migrator, TEXTS, [again]) is None


def test_thirty_days_once_a_day_as_a_platform_task(settings: Any) -> None:
    assert settings.AI_SUGGESTION_TEXT_DAYS == 30  # D-F8-05
    beat = settings.CELERY_BEAT_SCHEDULE["ai_agents.purge_suggestion_text"]
    assert beat == {"task": tasks.purge_suggestion_text.name,
                    "schedule": crontab(hour=4, minute=37)}  # fmt: skip
    assert tasks.purge_suggestion_text.tenancy == "platform"

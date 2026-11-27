"""F11-03 (ADR-049 §4, §6): la entrada del sistema a las tareas (`apps.tasks.automation`) y los
enlaces `tasks.conversation_id` y `tasks.quote_id`, con PostgreSQL real y el rol `crm_app`. El
barrido y las reglas llegan con F11-05: aquí, la idempotencia y `SIMULATE` se prueban componiendo
la entrada con la clave única de `automation_runs` (F11-02), como lo hará el barrido."""

import json
import threading
import time
from datetime import UTC, datetime
from itertools import count
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from django.db import IntegrityError, connection
from django.utils import timezone

from apps.automations.params import KEYS
from apps.conversations.models import ChannelAccount, Conversation
from apps.quotes.models import Quote
from apps.tasks import automation, commands
from apps.tasks.commands import BadReference
from apps.tasks.models import Task
from core.tenancy.context import ActorType, TenantContext
from core.tenancy.scope import tenant_scope
from tests import test_opportunity_scopes as scopes
from tests.conftest import migrator_conninfo
from tests.factories import make_user
from tests.test_memberships import ctx, join
from tests.test_opportunities_tables import make
from tests.test_quotes_read_api import make_quote
from tests.test_self_context import signed

crm, pipelines, real_stack = scopes.crm, scopes.pipelines, scopes.real_stack
pytestmark = pytest.mark.usefixtures("tenant_db", "pipelines")
type DB = psycopg.Connection[Any]
DUE = datetime(2026, 11, 27, 15, 0, tzinfo=UTC)
UNANSWERED, QUOTE = "conversation.unanswered", "quote.no_response"
_numbers = count(1)
CLAIM = (  # lo que hará el barrido (ADR-049 §6): reclamar la ejecución antes de crear la tarea
    "INSERT INTO automation_runs (organization_id, rule_id, rule_version, rule_key, subject_type,"
    " subject_id, episode_at, step, mode, status, recipients) VALUES (%s, %s, 1, %s, %s, %s, %s,"
    " 1, %s, 'DONE', %s) ON CONFLICT DO NOTHING"
    " RETURNING id"
)
OLD_INSERT = (  # la forma de `INSERT` de la imagen de `main` (sin las columnas nuevas)
    "INSERT INTO tasks (organization_id, id, type, title, description, assigned_user_id,"
    " contact_id, opportunity_id, due_at, is_all_day, reminder_at, status, priority, completed_at,"
    " completed_by_user_id, cancel_reason, source, created_by_user_id, updated_by_user_id,"
    " created_at, updated_at, deleted_at, deleted_by_user_id, version) VALUES (%s, %s, 'CALL',"
    " 'Llamar', '', %s, NULL, NULL, NULL, false, NULL, 'PENDING', 'MEDIUM', NULL, NULL, NULL,"
    " 'MANUAL', %s, %s, now(), now(), NULL, NULL, 1)"
)


def conversation(org: UUID, contact: UUID, **over: Any) -> Conversation:
    with tenant_scope(ctx(org)):
        account, _ = ChannelAccount.objects.get_or_create(
            platform="SANDBOX", external_account_id=f"s-{org}", defaults={"name": "Sandbox"}
        )
        made: Conversation = Conversation.objects.create(
            number=f"CONV-{next(_numbers):05d}", contact_id=contact, channel_account_id=account.pk,
            last_message_at=timezone.now(), **over,
        )  # fmt: skip
    return made


@pytest.fixture
def w(crm: SimpleNamespace) -> SimpleNamespace:
    """Sobre `crm`: una conversación y una cotización (con oportunidad) de A, y otras de B."""
    contact = crm.ids["plain"]
    deal = make(crm.a, contact_id=contact)
    return SimpleNamespace(
        **vars(crm),
        convo=conversation(crm.a, contact),
        quote=make_quote(crm.a, crm.ana, contact, opportunity_id=deal.pk),
        deal=deal.pk,
        contact=contact,
        foreign_convo=conversation(crm.b, crm.ids["b"]).pk,
        foreign_quote=make_quote(crm.b, crm.luis, crm.ids["b"]),
    )


def create(org: UUID, *, actor: TenantContext | None = None, **over: Any) -> list[Task]:
    fields = {"rule_key": UNANSWERED, "step": 1, "run_id": uuid4(), "due_at": DUE, **over}
    with tenant_scope(actor or ctx(org)) as tenant:
        return automation.create_automation_task(tenant, **fields)


def cancel(org: UUID, task: Any, version: int = 1, **over: Any) -> bool:
    subject = getattr(task, "conversation_id", None) or getattr(task, "quote_id", None) or uuid4()
    fields = {"rule_key": UNANSWERED, "reason": "RESOLVED", "run_id": uuid4(),
              "subject_id": subject, **over}  # fmt: skip
    with tenant_scope(ctx(org)) as tenant:
        return automation.cancel_automation_task(tenant, task_id=task.pk, version=version, **fields)


def count_tasks(migrator: DB) -> int:
    return migrator.execute("SELECT count(*) FROM tasks").fetchone()[0]  # type: ignore[index,no-any-return]


def audit(migrator: DB) -> list[tuple[Any, ...]]:
    return migrator.execute(
        "SELECT action, actor_type, actor_id, changes::text, metadata::text FROM audit_logs"
        " WHERE entity_type = 'task' ORDER BY occurred_at, id"
    ).fetchall()


def test_the_system_creates_the_fixed_task_for_each_recipient_and_audits_ids_only(
    w: SimpleNamespace, migrator: DB
) -> None:
    run = uuid4()
    made = create(w.a, subject_id=w.convo.pk, recipients=[w.vera.pk, w.sam.pk], run_id=run)
    assert {t.assigned_user_id for t in made} == {w.vera.pk, w.sam.pk}
    with tenant_scope(ctx(w.a)):
        made = list(Task.objects.order_by("assigned_user_id"))
    for task in made:
        assert (task.title, task.type, task.priority, task.status, task.source) == (
            f"Responder a la conversación {w.convo.number}", "MESSAGE", "HIGH", "PENDING",
            "AUTOMATION",
        )  # fmt: skip
        assert task.description == automation.TEMPLATES[(UNANSWERED, 1)].description
        assert (task.conversation_id, task.quote_id, task.contact_id) == (
            w.convo.pk,
            None,
            w.contact,
        )
        assert task.created_by_user_id is task.updated_by_user_id is None
        assert (task.due_at, task.version) == (DUE, 1)
    rows = audit(migrator)
    assert [(a, t, who) for a, t, who, _, _ in rows] == [("task.created", "SYSTEM", None)] * 2
    changes, metadata = json.loads(rows[0][3]), json.loads(rows[0][4])
    assert metadata == {"rule": UNANSWERED, "step": 1, "run_id": str(run)}
    assert changes["title"] == [None, "[texto]"] and changes["source"] == [None, "AUTOMATION"]
    assert changes["conversation_id"] == [None, str(w.convo.pk)]
    assert "Sin nadie" not in json.dumps(rows, default=str)  # ni el nombre del cliente
    [quoted] = create(w.a, rule_key=QUOTE, step=2, subject_id=w.quote, recipients=(w.ana.pk,))
    assert (quoted.quote_id, quoted.conversation_id) == (w.quote, None)
    assert (quoted.contact_id, quoted.opportunity_id, quoted.type) == (w.contact, w.deal, "QUOTE")
    with tenant_scope(ctx(w.a)):
        number = Quote.objects.get(pk=w.quote).number
    assert quoted.title == f"La cotización {number} vence pronto"


def test_only_the_system_creates_or_cancels_and_a_person_never_does(w: SimpleNamespace) -> None:
    [task] = create(w.a, subject_id=w.convo.pk, recipients=[w.vera.pk])
    person = ctx(w.a, w.ana)
    posing = TenantContext(w.a, "test", None, ActorType.SYSTEM, w.ana.pk)
    borrowed = TenantContext(
        w.a, "test", w.ana.pk, ActorType.SYSTEM, None
    )  # el sistema con persona
    agent = TenantContext(w.a, "test", None, ActorType.AI_AGENT, None)
    for actor in (person, posing, borrowed, agent):
        with pytest.raises(ValueError, match="solo el sistema"):
            create(w.a, actor=actor, subject_id=w.convo.pk, recipients=[w.vera.pk])
        with tenant_scope(actor) as tenant, pytest.raises(ValueError, match="solo el sistema"):
            automation.cancel_automation_task(tenant, task_id=task.pk, version=1,
                                              rule_key=UNANSWERED, subject_id=w.convo.pk,
                                              reason="RESOLVED", run_id=uuid4())  # fmt: skip
    with tenant_scope(ctx(w.a)):
        assert Task.objects.count() == 1 and Task.objects.get().status == "PENDING"


@pytest.mark.parametrize(
    ("over", "error"),
    [
        ({"recipients": []}, "responsables"),
        ({"recipients": "x"}, "responsables"),
        ({"rule_key": "custom"}, "regla y paso"),
        ({"step": 3}, "regla y paso"),
        ({"step": True}, "regla y paso"),
        ({"due_at": datetime(2026, 11, 27)}, "vencimiento"),  # noqa: DTZ001 — sin zona: se rechaza
    ],
)
def test_bad_input_writes_nothing(
    w: SimpleNamespace, migrator: DB, over: dict[str, Any], error: str
) -> None:
    fields = {"subject_id": w.convo.pk, "recipients": [w.vera.pk], **over}
    with pytest.raises(ValueError, match=error):
        create(w.a, **fields)
    assert count_tasks(migrator) == 0 and audit(migrator) == []


def test_never_more_than_three_distinct_recipients(w: SimpleNamespace, migrator: DB) -> None:
    four = [w.ana.pk, w.vera.pk, w.sam.pk, w.bea.pk]
    for recipients in (four, [w.vera.pk, w.vera.pk], [w.vera.pk, str(w.vera.pk)]):
        with pytest.raises(ValueError, match="de 1 a 3, distintos"):
            create(w.a, subject_id=w.convo.pk, recipients=recipients)
    assert count_tasks(migrator) == 0
    assert len(create(w.a, subject_id=w.convo.pk, recipients=four[:3])) == 3


def test_an_inactive_or_foreign_member_creates_no_task_at_all(
    w: SimpleNamespace, migrator: DB
) -> None:
    suspended = join(w.a, make_user(), "SUSPENDED").user_id
    for stranger in (suspended, w.luis.pk, uuid4()):
        with pytest.raises(BadReference) as caught:
            create(w.a, subject_id=w.convo.pk, recipients=[w.ana.pk, stranger])
        assert caught.value.field == "assigned_user_id"
    assert count_tasks(migrator) == 0 and audit(migrator) == []


def test_a_subject_of_another_organization_is_the_same_bad_reference_as_a_missing_one(
    w: SimpleNamespace, migrator: DB
) -> None:
    for key, subject, field in ((UNANSWERED, w.foreign_convo, "conversation_id"),
                                (UNANSWERED, uuid4(), "conversation_id"),
                                (QUOTE, w.foreign_quote, "quote_id"),
                                (QUOTE, w.convo.pk, "quote_id")):  # fmt: skip
        with pytest.raises(BadReference) as caught:
            create(w.a, rule_key=key, subject_id=subject, recipients=[w.ana.pk])
        assert (caught.value.field, caught.value.args) == (field, (field,))
    assert count_tasks(migrator) == 0
    # La base también lo impide (FKs compuestas), aunque la aplicación se equivoque
    with tenant_scope(ctx(w.a)):
        mine = Task.objects.create(title="Mía")
    for column, foreign, name in (("conversation_id", w.foreign_convo, "tasks_conversation_fk"),
                                  ("quote_id", w.foreign_quote, "tasks_quote_fk")):  # fmt: skip
        with pytest.raises(IntegrityError) as refused, tenant_scope(ctx(w.a)):
            with connection.cursor() as cursor:
                cursor.execute(f"UPDATE tasks SET {column} = %s WHERE id = %s", [foreign, mine.pk])  # noqa: S608
        assert refused.value.__cause__.diag.constraint_name == name  # type: ignore[union-attr]


def test_cancel_only_touches_an_open_automation_task_nobody_changed(
    w: SimpleNamespace, migrator: DB
) -> None:
    reassigned, completed, kept = (
        create(w.a, subject_id=w.convo.pk, recipients=[w.vera.pk])[0] for _ in range(3)
    )
    with tenant_scope(ctx(w.a, w.ana)) as person:  # una persona la reasigna primero
        commands.assign_task(person, task_id=reassigned.pk, version=1, assigned_user_id=w.sam.pk)
        commands.complete_task(person, task_id=completed.pk, version=1)
    assert cancel(w.a, reassigned) is False  # su versión ya no es 1
    assert cancel(w.a, reassigned, version=2) is True  # la de ahora, y sigue abierta: sí
    assert cancel(w.a, completed, version=2) is False  # cerrada por una persona
    run = uuid4()
    assert cancel(w.a, kept, run_id=run) is True
    assert cancel(w.a, kept, version=2) is False  # ya cancelada
    with tenant_scope(ctx(w.a)):
        states = {t.pk: (t.status, t.version, t.assigned_user_id, t.updated_by_user_id)
                  for t in Task.objects.all()}  # fmt: skip
        assert Task.objects.get(pk=kept.pk).cancel_reason == (
            "La conversación ya se respondió o se cerró"
        )
    assert states[completed.pk] == ("DONE", 2, w.vera.pk, w.ana.pk)
    assert states[reassigned.pk] == ("CANCELLED", 3, w.sam.pk, None)
    assert states[kept.pk] == ("CANCELLED", 2, w.vera.pk, None)
    action, actor, who, changes, metadata = audit(migrator)[-1]
    assert (action, actor, who) == ("task.cancelled", "SYSTEM", None)
    assert json.loads(changes) == {
        "status": ["PENDING", "CANCELLED"],
        "cancel_reason": [None, "[texto]"],
    }
    assert json.loads(metadata) == {"rule": UNANSWERED, "run_id": str(run), "reason": "RESOLVED"}
    with pytest.raises(ValueError, match="motivo"):
        cancel(w.a, kept, version=2, reason="Lo digo yo")


def test_cancel_never_reaches_a_manual_deleted_or_foreign_task(w: SimpleNamespace) -> None:
    [gone] = create(w.a, subject_id=w.convo.pk, recipients=[w.ana.pk])
    with tenant_scope(ctx(w.a, w.ana)) as person:
        commands.delete_task(person, task_id=gone.pk)
    with tenant_scope(ctx(w.a)):  # una manual enlazada a la misma conversación
        manual = Task.objects.create(title="Mía", conversation_id=w.convo.pk)
    [foreign] = create(w.b, subject_id=w.foreign_convo, recipients=[w.luis.pk])
    errors = []
    for task in (manual, gone, foreign, SimpleNamespace(pk=uuid4())):
        with pytest.raises(Task.DoesNotExist) as caught:
            cancel(w.a, task)
        errors.append((type(caught.value), caught.value.args))
    assert len(set(errors)) == 1  # el mismo error para todas
    with tenant_scope(ctx(w.a)):
        assert Task.objects.get(pk=manual.pk).status == "PENDING"
    with tenant_scope(ctx(w.b)):
        assert Task.objects.get(pk=foreign.pk).status == "PENDING"


def test_the_previous_image_still_writes_tasks_on_the_new_schema(
    w: SimpleNamespace, migrator: DB
) -> None:
    old = uuid4()
    with tenant_scope(ctx(w.a)), connection.cursor() as cursor:  # como `crm_app`
        cursor.execute(OLD_INSERT, [w.a, old, w.vera.pk, w.ana.pk, w.ana.pk])
        cursor.execute(
            "UPDATE tasks SET title = 'Otra', status = 'DONE', completed_at = now(),"
            " completed_by_user_id = %s, version = version + 1 WHERE id = %s",
            [w.ana.pk, old],
        )
    with tenant_scope(ctx(w.a, w.ana)) as person:  # y sus comandos, sin tocar los enlaces
        task = commands.create_task(person, title="Llamar", contact_id=w.contact)
        commands.update_task(person, task_id=task.pk, version=1, title="Llamar ya")
        commands.cancel_task(person, task_id=task.pk, version=2, reason="No hace falta")
        commands.reopen_task(person, task_id=task.pk, version=3)
        commands.reopen_task(person, task_id=old, version=2)
        commands.delete_task(person, task_id=task.pk)
    links = migrator.execute("SELECT conversation_id, quote_id FROM tasks").fetchall()
    assert links == [(None, None), (None, None)]


def claim_and_create(
    org: UUID,
    rule: UUID,
    subject: UUID,
    recipients: list[UUID],
    mode: str = "ON",
    *,
    record_tasks_in_simulate: bool = False,
) -> list[Task]:
    """La composición que hará el barrido: reclamar la ejecución (clave única) y, solo si es nueva
    y está en `ON`, crear las tareas y anotarlas en ella, todo en una transacción."""
    with tenant_scope(ctx(org)) as system, connection.cursor() as cursor:
        cursor.execute(CLAIM, [org, rule, UNANSWERED, "conversation", subject,
                               "2026-11-27T10:00:00Z", mode, recipients])  # fmt: skip
        claimed = cursor.fetchone()
        if claimed is None or (mode == "SIMULATE" and not record_tasks_in_simulate):
            return []
        made = automation.create_automation_task(
            system, rule_key=UNANSWERED, step=1, run_id=claimed[0], subject_id=subject,
            recipients=recipients, due_at=DUE,
        )  # fmt: skip
        cursor.execute("UPDATE automation_runs SET task_ids = %s WHERE id = %s",
                       [[t.pk for t in made], claimed[0]])  # fmt: skip
        return made


@pytest.fixture
def rule(w: SimpleNamespace, migrator: DB) -> UUID:
    pk = uuid4()
    migrator.execute(
        "INSERT INTO automation_rules (id, organization_id, key, mode, params, version,"
        " simulate_since, created_at, updated_at) VALUES (%s, %s, %s, 'ON', '{}', 1, NULL, now(),"
        " now())",
        [pk, w.a, UNANSWERED],
    )
    return pk


def test_a_retry_or_two_sweeps_at_once_leave_one_task_per_recipient(
    w: SimpleNamespace, rule: UUID, migrator: DB
) -> None:
    who = [w.vera.pk, w.sam.pk]
    assert len(claim_and_create(w.a, rule, w.convo.pk, who)) == 2
    assert claim_and_create(w.a, rule, w.convo.pk, who) == []  # el reintento no crea otra
    other = conversation(w.a, w.ids["sam"]).pk
    start, results = threading.Barrier(2), list[Any]([None, None])

    def sweep(index: int) -> None:
        try:
            start.wait(timeout=10)
            results[index] = len(claim_and_create(w.a, rule, other, who))
        except Exception as error:  # noqa: BLE001 — lo comprueba el test
            results[index] = type(error).__name__
        finally:
            connection.close()

    threads = [threading.Thread(target=sweep, args=(i,)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert sorted(results) == [0, 2]  # dos conexiones reales: uno espera y no crea nada
    assert count_tasks(migrator) == 4
    assert migrator.execute("SELECT count(*) FROM automation_runs").fetchone() == (2,)


def test_simulate_creates_no_task_and_the_database_refuses_tasks_in_a_simulated_run(
    w: SimpleNamespace, rule: UUID, migrator: DB
) -> None:
    assert claim_and_create(w.a, rule, w.convo.pk, [w.vera.pk], "SIMULATE") == []
    assert count_tasks(migrator) == 0
    assert migrator.execute("SELECT recipients, task_ids FROM automation_runs").fetchall() == [
        ([w.vera.pk], [])
    ]
    other = conversation(w.a, w.ids["sam"]).pk  # un barrido que se equivoca: todo vuelve atrás
    with pytest.raises(IntegrityError) as refused:
        claim_and_create(w.a, rule, other, [w.vera.pk], "SIMULATE", record_tasks_in_simulate=True)
    assert refused.value.__cause__.diag.constraint_name == "automation_runs_simulated_ck"  # type: ignore[union-attr]
    assert count_tasks(migrator) == 0 and audit(migrator) == []


@pytest.mark.usefixtures("real_stack")
def test_my_tasks_lists_an_automation_task_with_its_fixed_text(w: SimpleNamespace) -> None:
    [task] = create(w.a, subject_id=w.convo.pk, recipients=[w.vera.pk])
    with tenant_scope(ctx(w.a, w.ana)) as person:
        commands.create_task(person, title="Manual", assigned_user_id=w.vera.pk)
    response = signed(w.vera).get("/api/v1/o/org-a/tasks/", {"mine": "true"})
    assert response.status_code == 200, response.content
    rows = {row["id"]: row for row in response.json()["results"]}
    assert len(rows) == 2
    row = rows[str(task.pk)]
    assert (row["title"], row["source"], row["status"], row["priority"]) == (
        f"Responder a la conversación {w.convo.number}", "AUTOMATION", "PENDING", "HIGH",
    )  # fmt: skip
    assert row["description"] == automation.TEMPLATES[(UNANSWERED, 1)].description
    assert "conversation_id" not in row and "created_by_user_id" not in row  # la API no cambia
    detail = signed(w.vera).get(f"/api/v1/o/org-a/tasks/{task.pk}/")
    assert detail.status_code == 200 and detail.json()["source"] == "AUTOMATION"


def test_templates_cover_the_registered_rules_with_fixed_text_only() -> None:
    assert {key for key, _ in automation.TEMPLATES} == set(KEYS)
    for template in automation.TEMPLATES.values():
        assert template.title.count("{") == 1 and "{number}" in template.title
        assert "{" not in template.description
        assert set(template.closed) == {"RESOLVED", "RULE_CHANGED"}


def test_cancel_needs_the_task_of_the_rule_subject(w: SimpleNamespace) -> None:
    [quoted] = create(w.a, rule_key=QUOTE, subject_id=w.quote, recipients=[w.ana.pk])
    [convo] = create(w.a, subject_id=w.convo.pk, recipients=[w.ana.pk])
    other = conversation(w.a, w.ids["sam"]).pk
    for task, key, subject in ((quoted, UNANSWERED, w.quote),  # otra regla, otro tipo de sujeto
                               (convo, QUOTE, w.convo.pk),
                               (convo, UNANSWERED, other)):  # fmt: skip
        with pytest.raises(Task.DoesNotExist):
            cancel(w.a, task, rule_key=key, subject_id=subject)
    with tenant_scope(ctx(w.a)):
        assert set(Task.objects.values_list("status", flat=True)) == {"PENDING"}
    assert cancel(w.a, quoted, rule_key=QUOTE) is True
    with tenant_scope(ctx(w.a)):
        assert (
            Task.objects.get(pk=quoted.pk).cancel_reason == "La cotización ya no espera respuesta"
        )


def test_cancel_waits_for_a_person_holding_the_task_and_then_leaves_it(w: SimpleNamespace) -> None:
    [task] = create(w.a, subject_id=w.convo.pk, recipients=[w.vera.pk])
    held, release, results = threading.Event(), threading.Event(), list[Any]([None, None])

    def person() -> None:
        try:
            with tenant_scope(ctx(w.a, w.ana)) as tenant:
                commands.assign_task(tenant, task_id=task.pk, version=1, assigned_user_id=w.sam.pk)
                held.set()
                release.wait(timeout=10)  # la persona aún no confirma
        finally:
            held.set()
            connection.close()

    def system() -> None:
        try:
            results[1] = cancel(w.a, task)  # versión 1: la que conoce el barrido
        except Exception as error:  # noqa: BLE001 — lo comprueba el test
            results[1] = type(error).__name__
        finally:
            connection.close()

    first = threading.Thread(target=person)
    first.start()
    assert held.wait(timeout=10)
    second = threading.Thread(target=system)
    second.start()
    second.join(timeout=2)
    results[0] = second.is_alive()  # espera el bloqueo de la fila
    release.set()
    first.join(timeout=10)
    second.join(timeout=10)
    assert results == [True, False]
    with tenant_scope(ctx(w.a)):
        found = Task.objects.get(pk=task.pk)
    assert (found.status, found.assigned_user_id, found.version) == ("PENDING", w.sam.pk, 2)


def test_recipients_are_locked_in_id_order_so_two_sweeps_never_deadlock(
    w: SimpleNamespace, migrator: DB
) -> None:
    low, high = sorted([w.vera.pk, w.sam.pk])
    other = conversation(w.a, w.ids["sam"]).pk  # otro contacto: no se esperan por él
    results = list[Any]([None, None])
    with psycopg.connect(migrator_conninfo()) as holder:
        holder.execute(  # alguien retiene la membresía de menor id
            "SELECT 1 FROM organization_memberships WHERE organization_id = %s AND user_id = %s"
            " FOR NO KEY UPDATE", [w.a, low],
        )  # fmt: skip

        def sweep(index: int, subject: UUID, recipients: list[UUID]) -> None:
            try:
                made = create(w.a, subject_id=subject, recipients=recipients)
                results[index] = [t.assigned_user_id for t in made]
            except Exception as error:  # noqa: BLE001 — lo comprueba el test
                results[index] = type(error).__name__
            finally:
                connection.close()

        threads = [threading.Thread(target=sweep, args=(0, w.convo.pk, [low, high])),
                   threading.Thread(target=sweep, args=(1, other, [high, low]))]  # fmt: skip
        for thread in threads:
            thread.start()
        waiting = "SELECT count(DISTINCT pid) FROM pg_locks WHERE NOT granted"
        for _ in range(200):  # los dos esperan: el orden de quien llama no cuenta
            if migrator.execute(waiting).fetchone() == (2,):
                break
            time.sleep(0.05)
        else:
            pytest.fail("los dos barridos no llegaron a esperar")
        holder.rollback()
    for thread in threads:
        thread.join(timeout=15)
    assert results == [[low, high], [low, high]]

"""F11-04 (#1178): `conversations.awaiting_reply_since`, desde cuándo espera respuesta (ADR-049
§5). La escribe solo el disparador `conversations_reply_wait` (migración 0066) con la regla de
«Sin responder» de #1093: la fija el primer mensaje del cliente de un episodio, la borran una
respuesta que cuenta y el paso a resuelta o cerrada, y una respuesta que falla devuelve la hora
del episodio. Con PostgreSQL, `crm_app`, los comandos de siempre y las migraciones. Datos
ficticios."""

import datetime as dt
import time
from importlib import import_module
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest
from django.conf import settings
from django.core.management import call_command
from django.db import connection
from django.db.models import F
from django.utils import timezone

from apps.conversations import commands
from apps.conversations.models import Conversation, Message
from core.tenancy.scope import require_scope, tenant_scope
from tests import test_conversations_tables as tables
from tests.conftest import migrator_conninfo
from tests.test_conversation_commands import inbound, outbound, resolve, run, world, writer
from tests.test_memberships import ctx
from tests.test_pipelines_tables import as_migrator

__all__ = ["world", "writer"]  # los fixtures, para pytest
pytestmark = pytest.mark.usefixtures("tenant_db")
DB = psycopg.Connection[Any]
WAIT = import_module("apps.conversations.migrations.0066_conversation_awaiting_reply_since")
INDEX = import_module("apps.conversations.migrations.0067_conversations_awaiting_reply_index")
FILL = import_module("apps.conversations.migrations.0068_backfill_awaiting_reply_since")
ROW = "SELECT awaiting_reply_since, last_message_at FROM conversations WHERE id = %s"


def since(migrator: DB, convo: Any) -> Any:
    """`awaiting_reply_since`, como la ve el migrador (sin RLS)."""
    found = migrator.execute(ROW, [convo.pk]).fetchone()
    return found and found[0]


def clock(migrator: DB, convo: Any) -> Any:
    found = migrator.execute(ROW, [convo.pk]).fetchone()
    return found and found[1]


def move(world: SimpleNamespace, message: Message, state: str, why: str | None = None) -> Any:
    return run(world, commands.advance_delivery, message_id=message.pk, status=state, reason=why)


def status(world: SimpleNamespace, convo: Any, new: str, **stamps: Any) -> None:
    with tenant_scope(ctx(world.a)) as tenant:
        row = Conversation.objects.select_for_update().get(pk=convo.pk)
        commands._status(
            tenant, require_scope(tenant), row, new, f"conversation.{new.lower()}", **stamps
        )


def old(world: SimpleNamespace, convo: Any, **fields: Any) -> None:
    """Lo que escribe la imagen anterior (ADR-033 §7): no conoce la columna."""
    with tenant_scope(ctx(world.a)):
        Conversation.objects.filter(pk=convo.pk).update(**fields, updated_at=timezone.now())


def app_conninfo() -> str:
    db = settings.DATABASES["default"]
    return psycopg.conninfo.make_conninfo(
        dbname=db["NAME"], user=db["USER"], password=db["PASSWORD"], host=db["HOST"],
        port=db["PORT"] or None,
    )  # fmt: skip


def test_the_first_customer_message_starts_the_wait_and_the_episode_keeps_it(
    world: SimpleNamespace, migrator: DB
) -> None:
    first, _ = inbound(world)
    convo = SimpleNamespace(pk=first.conversation_id)
    start = since(migrator, convo)
    assert start is not None and start == clock(migrator, convo)  # la llegada del mensaje
    inbound(world, "wamid.2")  # otro mensaje del cliente: el mismo episodio
    assert since(migrator, convo) == start and clock(migrator, convo) > start
    run(world, commands.add_note, conversation_id=convo.pk, body="Llamar luego")
    old(world, convo, assigned_user_id=world.ana.pk, assigned_at=timezone.now(),
        assignment_version=F("assignment_version") + 1, version=F("version") + 1)  # fmt: skip
    assert since(migrator, convo) == start  # ni una nota ni una asignación cuentan


def test_a_reply_ends_the_wait_and_a_failed_one_brings_back_the_start_of_the_episode(
    world: SimpleNamespace, migrator: DB
) -> None:
    first, _ = inbound(world)
    convo = SimpleNamespace(pk=first.conversation_id)
    start = since(migrator, convo)
    inbound(world, "wamid.2")
    reply, _ = outbound(world, convo)
    assert since(migrator, convo) is None  # en cola cuenta (#1093) hasta que falle
    move(world, reply, "FAILED", "PROVIDER_REJECTED")
    assert since(migrator, convo) == first.created_at  # el episodio, no la hora de la respuesta
    assert start <= since(migrator, convo) < clock(migrator, convo)
    inbound(world, "wamid.3")  # el cliente insiste: sigue el mismo
    assert since(migrator, convo) == first.created_at
    again, _ = outbound(world, convo)
    move(world, again, "SENT")
    move(world, again, "DELIVERED")
    assert since(migrator, convo) is None  # la que llegó termina el episodio
    later, _ = inbound(world, "wamid.4")
    assert since(migrator, convo) == clock(migrator, convo)  # uno nuevo
    lost, _ = outbound(world, convo)
    move(world, lost, "FAILED", "PROVIDER_REJECTED")
    assert since(migrator, convo) == later.created_at  # desde la respuesta que sí llegó


def test_a_provider_unknown_reply_counts_until_meta_says_it_failed(
    world: SimpleNamespace, migrator: DB
) -> None:
    first, _ = inbound(world)
    convo = SimpleNamespace(pk=first.conversation_id)
    maybe, _ = outbound(world, convo)
    move(world, maybe, "SENDING")
    run(world, commands.fail_sending, message_id=maybe.pk, reason="PROVIDER_UNKNOWN")
    assert since(migrator, convo) is None  # pudo salir: contestada
    move(world, maybe, "FAILED", "PROVIDER_REJECTED")  # F7b-15: no salió
    assert since(migrator, convo) == first.created_at
    other, _ = outbound(world, convo)
    move(world, other, "SENDING")
    run(world, commands.fail_sending, message_id=other.pk, reason="PROVIDER_UNKNOWN")
    move(world, other, "SENT")  # un estado tardío: salió
    assert since(migrator, convo) is None
    inbound(world, "wamid.2")
    unsure, _ = outbound(world, convo)
    move(world, unsure, "SENDING")
    run(world, commands.fail_sending, message_id=unsure.pk, reason="PROVIDER_UNKNOWN")
    later, _ = inbound(world, "wamid.3")
    lost, _ = outbound(world, convo)
    move(world, lost, "FAILED", "PROVIDER_REJECTED")
    assert since(migrator, convo) == later.created_at  # la que pudo salir cuenta como respuesta


def test_resolving_or_closing_ends_the_wait_and_a_new_message_opens_a_new_one(
    world: SimpleNamespace, migrator: DB
) -> None:
    first, _ = inbound(world)
    convo = SimpleNamespace(pk=first.conversation_id)
    start = since(migrator, convo)
    status(world, convo, "RESOLVED", resolved_at=timezone.now())
    assert since(migrator, convo) is None
    inbound(world, "wamid.2")  # la reabre: otro episodio
    assert reloaded(world, convo).status == "OPEN"
    assert since(migrator, convo) == clock(migrator, convo) > start
    reply, _ = outbound(world, convo)
    status(world, convo, "CLOSED", closed_at=timezone.now())
    move(world, reply, "FAILED", "PROVIDER_REJECTED")  # cerrada: ya no espera aunque falle
    assert since(migrator, convo) is None


def test_a_conversation_resolved_too_long_ago_is_closed_and_the_new_one_waits(
    world: SimpleNamespace, migrator: DB
) -> None:
    first, _ = inbound(world)
    gone = SimpleNamespace(pk=first.conversation_id)
    resolve(world, gone, dt.timedelta(hours=commands.REOPEN_HOURS + 1))
    later, _ = inbound(world, "wamid.2")
    fresh = SimpleNamespace(pk=later.conversation_id)
    assert fresh.pk != gone.pk and since(migrator, gone) is None
    assert since(migrator, fresh) == clock(migrator, fresh)


def reloaded(world: SimpleNamespace, convo: Any) -> Conversation:
    with tenant_scope(ctx(world.a)):
        found: Conversation = Conversation.objects.get(pk=convo.pk)
        return found


def test_what_the_application_or_the_previous_image_writes_never_breaks_it(
    world: SimpleNamespace, migrator: DB
) -> None:
    first, _ = inbound(world)
    convo = SimpleNamespace(pk=first.conversation_id)
    start = since(migrator, convo)
    old(world, convo, awaiting_reply_since=timezone.now())  # la aplicación no la elige
    assert since(migrator, convo) == start
    with tenant_scope(ctx(world.a)):
        row = Conversation.objects.get(pk=convo.pk)
        row.awaiting_reply_since = None
        row.save()  # un `save()` entero con lo que tenga en memoria
    assert since(migrator, convo) == start
    old(world, convo, last_message_at=timezone.now(), last_message_preview="Hola",
        last_message_direction="OUTBOUND")  # su `record_outbound`  # fmt: skip
    assert since(migrator, convo) is None
    old(world, convo, awaiting_reply_since=timezone.now())
    assert since(migrator, convo) is None  # contestada: nada la pone
    old(
        world,
        convo,
        last_message_at=timezone.now(),
        last_message_preview="¿Hola?",
        last_message_direction="INBOUND",
        last_inbound_at=timezone.now(),
    )  # su `record_inbound`
    assert since(migrator, convo) == clock(migrator, convo)
    old(world, convo, status="RESOLVED", resolved_at=timezone.now(), version=F("version") + 1)
    assert since(migrator, convo) is None


def test_with_no_visible_customer_message_it_falls_back_and_never_raises(
    world: SimpleNamespace, migrator: DB
) -> None:
    """Sin el contexto de la organización RLS oculta las filas: `crm_app` no actualiza nada (ni
    error). Sin un mensaje del cliente visible, el paso 4 toma `last_message_at`."""
    convo = tables.conversation(
        world.a, world.contact_a, world.account_a, last_message_preview="Hola",
        last_message_direction="OUTBOUND",
    )  # fmt: skip
    with psycopg.connect(app_conninfo(), autocommit=True) as app:
        done = app.execute(
            "UPDATE conversations SET last_reply_failed = true WHERE id = %s", [convo.pk]
        )
        assert done.rowcount == 0 and since(migrator, convo) is None
    with tenant_scope(ctx(world.a)):
        Conversation.objects.filter(pk=convo.pk).update(last_reply_failed=True)
    assert since(migrator, convo) == clock(migrator, convo)
    migrator.execute("UPDATE conversations SET last_reply_failed = false WHERE id = %s", [convo.pk])
    assert since(migrator, convo) is None


def test_another_organization_neither_sees_nor_changes_it(world: SimpleNamespace) -> None:
    first, _ = inbound(world)
    with tenant_scope(ctx(world.b)), connection.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM conversations WHERE awaiting_reply_since IS NOT NULL")
        assert cursor.fetchone() == (0,)
        cursor.execute(
            "UPDATE conversations SET status = 'RESOLVED', resolved_at = now() WHERE id = %s",
            [first.conversation_id],
        )
        assert cursor.rowcount == 0
    with tenant_scope(ctx(world.a)):
        assert Conversation.objects.filter(awaiting_reply_since__isnull=False).count() == 1


def plan(sql: str, params: list[Any]) -> str:
    with connection.cursor() as cursor:
        cursor.execute("SET LOCAL enable_seqscan = off")
        cursor.execute("EXPLAIN " + sql, params)
        return " ".join(row[0] for row in cursor.fetchall())


def test_the_wait_query_uses_its_partial_index(world: SimpleNamespace) -> None:
    inbound(world)
    with tenant_scope(ctx(world.a)):
        waiting = Conversation.objects.filter(awaiting_reply_since__lte=timezone.now())
        sql, params = waiting.values("id").query.sql_with_params()
        assert INDEX.INDEX in plan(sql, list(params))


def episode_plan(world: SimpleNamespace, conversation_id: Any) -> str:
    """El plan de la consulta del paso 4 del disparador, tal cual, para una conversación."""
    step = WAIT.FUNCTION.split("coalesce((", 1)[1].rsplit("), NEW.last_message_at)", 1)[0]
    step = step.replace("NEW.organization_id", "%s").replace("NEW.id", "%s")
    with tenant_scope(ctx(world.a)), connection.cursor() as cursor:
        cursor.execute("EXPLAIN " + step, [world.a, conversation_id] * 2)
        return " ".join(row[0] for row in cursor.fetchall())


def fictitious(migrator: DB, world: SimpleNamespace, size: int) -> None:
    """`size` conversaciones ficticias de A, de cuatro formas (por `g % 4`): solo el cliente;
    contestada; una respuesta que llegó, dos mensajes y una que falló; resuelta sin contestar."""
    org, account = world.a, world.account_a.pk
    migrator.execute(
        "INSERT INTO contacts (organization_id, display_name, first_name, last_name, source,"
        " created_at, updated_at, version) SELECT %s, 'Ficticio ' || g, '', '', 'MANUAL', now(),"
        " now(), 1 FROM generate_series(1, %s) g",
        [org, size],
    )
    migrator.execute(
        "INSERT INTO conversations (organization_id, number, status, attention_mode,"
        " assignment_version, last_message_at, created_at, updated_at, version,"
        " channel_account_id, contact_id, last_message_preview, last_message_direction,"
        " last_reply_failed, resolved_at) SELECT %s, 'CONV-' || (10000 + g),"
        " CASE WHEN g %% 4 = 3 THEN 'RESOLVED' ELSE 'OPEN' END, 'HUMAN', 1,"
        " now() - interval '1 hour' + g * interval '1 ms', now(), now(), 1, %s, c.id, 'Hola',"
        " CASE WHEN g %% 4 IN (1, 2) THEN 'OUTBOUND' ELSE 'INBOUND' END, g %% 4 = 2,"
        " CASE WHEN g %% 4 = 3 THEN now() END FROM (SELECT id, row_number() OVER (ORDER BY id) g"
        " FROM contacts WHERE display_name LIKE 'Ficticio %%') c",
        [org, account],
    )
    steps = (  # (forma, orden, dirección, estado de entrega, motivo)
        (0, 1, "INBOUND", None, None), (1, 1, "INBOUND", None, None),
        (1, 2, "OUTBOUND", "SENT", None), (2, 1, "INBOUND", None, None),
        (2, 2, "OUTBOUND", "DELIVERED", None), (2, 3, "INBOUND", None, None),
        (2, 4, "INBOUND", None, None), (2, 5, "OUTBOUND", "FAILED", "PROVIDER_REJECTED"),
        (3, 1, "INBOUND", None, None),
    )  # fmt: skip
    for shape, order, direction, state, why in steps:
        migrator.execute(
            "INSERT INTO messages (organization_id, direction, sender_type, message_type, body,"
            " created_at, channel_account_id, conversation_id, delivery_status,"
            " provider_error_code) SELECT %s, %s, CASE WHEN %s = 'INBOUND' THEN 'CONTACT'"
            " ELSE 'SYSTEM' END, 'TEXT', 'Hola', now() - interval '2 hours' + %s * interval"
            " '1 minute', %s, id, %s, %s FROM conversations WHERE number LIKE 'CONV-1____'"
            " AND substr(number, 6)::int %% 4 = %s",
            [org, direction, direction, order, account, state, why, shape],
        )


def test_the_migrations_backfill_in_batches_and_go_back_and_forth(
    world: SimpleNamespace, migrator: DB, monkeypatch: pytest.MonkeyPatch
) -> None:
    with as_migrator():
        call_command("migrate", "conversations", "0065", verbosity=0)
    gone = "SELECT count(*) FROM pg_trigger WHERE tgname = 'conversations_reply_wait'"
    assert migrator.execute(gone).fetchone() == (0,)
    assert migrator.execute(INDEX.STATE, [INDEX.INDEX]).fetchall() == []
    fictitious(migrator, world, 2000)
    monkeypatch.setattr(FILL, "BATCH", 7)  # muchas tandas: la última también se rellena
    stamps: list[float] = []

    def timed(execute: Any, sql: str, *args: Any) -> Any:
        if "ADD COLUMN awaiting_reply_since" in sql or sql.startswith(
            'INSERT INTO "django_migrations"'
        ):
            stamps.append(time.monotonic())  # del bloqueo exclusivo al último paso antes del commit
        return execute(sql, *args)

    with as_migrator():
        with connection.execute_wrapper(timed):
            call_command("migrate", "conversations", "0066", verbosity=0)
        call_command("migrate", "conversations", verbosity=0)
    held = stamps[-1] - stamps[0]
    print(f"0066: ACCESS EXCLUSIVE held about {held * 1000:.1f} ms on {2000} fictitious rows")
    assert len(stamps) >= 2 and held < 1, stamps
    migrator.execute("ANALYZE messages")  # con estadísticas, como en producción
    some = migrator.execute("SELECT id FROM conversations WHERE number = 'CONV-10002'").fetchone()
    found = episode_plan(world, some[0] if some else None)
    assert found.count("messages_org_conversation_idx") == 2, found
    assert migrator.execute(INDEX.STATE, [INDEX.INDEX]).fetchall() == [(True, INDEX.DEFINITION)]
    rows = migrator.execute(
        "SELECT substr(c.number, 6)::int % 4, c.awaiting_reply_since, (SELECT min(m.created_at)"
        " FROM messages m WHERE m.conversation_id = c.id AND m.direction = 'INBOUND' AND"
        " m.created_at > coalesce((SELECT max(o.created_at) FROM messages o WHERE"
        " o.conversation_id = c.id AND o.direction = 'OUTBOUND' AND o.delivery_status <>"
        " 'FAILED'), '-infinity')), (SELECT min(m.created_at) FROM messages m WHERE"
        " m.conversation_id = c.id) FROM conversations c WHERE c.number LIKE 'CONV-1____'"
    ).fetchall()
    assert len(rows) == 2000
    for shape, value, expected, first in rows:
        assert value == (expected if shape in (0, 2) else None), (shape, value, expected)
        assert shape != 2 or value > first  # tras la respuesta que llegó, no el de antes
    with as_migrator():  # repetirla no cambia nada
        FILL.fill(None, SimpleNamespace(connection=connection))
    with pytest.raises(RuntimeError, match="BYPASSRLS"):  # `crm_app` no ve todas las filas
        FILL.fill(None, SimpleNamespace(connection=connection))


def test_a_lock_timeout_leaves_no_half_state_and_the_migration_runs_again(
    world: SimpleNamespace, migrator: DB
) -> None:
    with as_migrator():
        call_command("migrate", "conversations", "0065", verbosity=0)
    column = (
        "SELECT count(*) FROM information_schema.columns WHERE table_name = 'conversations'"
        " AND column_name = 'awaiting_reply_since'"
    )
    with psycopg.connect(migrator_conninfo()) as reader:  # una lectura larga, abierta
        reader.execute("SELECT 1 FROM conversations LIMIT 1")
        with as_migrator(), pytest.raises(Exception, match="lock timeout"):
            call_command("migrate", "conversations", "0066", verbosity=0)
        assert migrator.execute(column).fetchone() == (0,)
        gone = "SELECT count(*) FROM pg_proc WHERE proname = 'conversations_reply_wait'"
        assert migrator.execute(gone).fetchone() == (0,)
    with as_migrator():
        call_command("migrate", "conversations", "0066", verbosity=0)
    for number in ("CONV-00008", "CONV-00009"):  # dos filas: el índice único de abajo falla
        tables.fresh(world.a, world.account_a, number)
    with pytest.raises(  # un `CONCURRENTLY` fallido de antes: `INVALID`, se borra y se rehace
        psycopg.errors.UniqueViolation
    ):
        migrator.execute(f"CREATE UNIQUE INDEX CONCURRENTLY {INDEX.INDEX} ON conversations ((1))")
    with as_migrator():
        call_command("migrate", "conversations", verbosity=0)
    assert migrator.execute(column).fetchone() == (1,)
    assert migrator.execute(INDEX.STATE, [INDEX.INDEX]).fetchall() == [(True, INDEX.DEFINITION)]

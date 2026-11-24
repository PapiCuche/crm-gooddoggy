"""F7b-20: un `failed` del webhook con un efecto en la cuenta (`131048` calidad, `190` token) solo
cambia la cuenta si la credencial con la que salió el mensaje sigue siendo la suya: nunca una cuenta
rotada después del envío ni una desconectada y vuelta a conectar (otra fila). La comprobación de
salud de WhatsApp tampoco pisa una rotación con una lectura anterior. PostgreSQL real con
`crm_app`; el simulador sustituye solo la red."""

import datetime as dt
import re
import threading
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from apps.conversations import accounts, commands, delivery
from apps.conversations.models import ChannelAccount, Message
from core.tenancy.context import ActorType, TenantContext
from core.tenancy.scope import require_scope, tenant_scope
from tests.conftest import migrator_conninfo
from tests.meta_sim import webhooks
from tests.test_channel_health import account as number
from tests.test_conversation_commands import audit, outbound, world, writer
from tests.test_delivery_status import WAMID, fresh_wake, run, sent, stamp
from tests.test_late_refusal import wait_for
from tests.test_memberships import ctx
from tests.test_meta_webhook import channel
from tests.test_rate_limit_5xx import account, demotions, manager, reconnect, rotate
from tests.test_social_subscription import key
from tests.test_whatsapp_outbound import graph, redis, talk

__all__ = ["channel", "fresh_wake", "graph", "manager", "redis", "world", "writer"]  # fixtures
pytestmark = pytest.mark.usefixtures("tenant_db", "channel", "fresh_wake", "manager")
EFFECTS = [(131048, ("DEGRADED", "QUALITY_LOW"), "channel_account.degraded"),
           (190, ("ERROR", "TOKEN_INVALID"), "channel_account.token_invalid")]  # fmt: skip
REASON = {131048: "QUALITY_BLOCKED", 190: "TOKEN_INVALID"}


def failed(code: int) -> dict[str, Any]:
    return webhooks.status(WAMID, "failed", error=(code, "x"))


def events(migrator: Any) -> list[str]:
    """Los avisos de la cuenta en el outbox, salvo los de rotar, conectar o desconectar."""
    rows = migrator.execute(
        "SELECT event_type FROM outbox_events WHERE event_type LIKE 'channel_account.%%' "
        "ORDER BY occurred_at, id"
    ).fetchall()
    kept = ("credentials_rotated", "connected", "disconnected")
    return [t for (t,) in rows if t.split(".", 1)[1] not in kept]


def webhook(world: SimpleNamespace, stale: ChannelAccount, code: int) -> None:
    """`delivery.apply` como lo llama `ingress.route`, con la cuenta que leyó antes."""
    context = TenantContext(world.a, "webhook", actor_type=ActorType.INTEGRATION)
    with tenant_scope(context):
        assert delivery.apply(context, stale, failed(code)) is None


def stale_account(world: SimpleNamespace, pk: Any) -> ChannelAccount:
    with tenant_scope(ctx(world.a)):
        found: ChannelAccount = ChannelAccount.objects.get(pk=pk)
    return found


# --- El webhook tardío ---------------------------------------------------------------------------


@pytest.mark.parametrize(("code", "demoted", "action"), EFFECTS)
@pytest.mark.parametrize("then", ["nothing", "rotate", "reconnect"])
def test_a_late_webhook_failure_follows_the_credential_the_message_was_sent_with(
    world: SimpleNamespace, graph: Any, migrator: Any,
    then: str, code: int, demoted: tuple[str, str], action: str,
) -> None:  # fmt: skip
    message = sent(world, graph)
    old, made = message.channel_account_id, []
    if then == "rotate":
        rotate(world, old)
    elif then == "reconnect":
        made.append(reconnect(world, old))
    outcome = run(failed(code))
    if then == "nothing":  # la misma credencial: una sola degradación, también si se repite
        assert outcome == {"PROCESSED": 1} and account(world, old) == demoted
        webhook(world, stale_account(world, old), code)  # repetido: nada más cambia
        assert demotions(migrator) == [action] and events(migrator) == [action]
        return
    assert demotions(migrator) == [] and events(migrator) == []  # ni auditoría ni evento
    if then == "rotate":  # el mensaje se corrige igual; la cuenta rotada, no
        assert outcome == {"PROCESSED": 1}
        assert stamp(world, message).provider_error_code == REASON[code]
        assert account(world, old) == ("CONNECTED", None)
    else:  # otra fila: el estado no es de sus mensajes y espera (`NotYet`); nada se degrada
        assert outcome == {"FAILED": 1} and stamp(world, message).delivery_status == "SENT"
        assert account(world, old) == ("DISCONNECTED", "DISCONNECTED_BY_ADMIN")
        assert account(world, made[0]) == ("CONNECTED", None)  # sana: su primer webhook


@pytest.mark.parametrize("code", REASON)
@pytest.mark.parametrize("then", ["rotate", "reconnect"])
def test_a_route_that_read_the_account_before_a_rotation_or_a_reconnect_never_demotes_it(
    world: SimpleNamespace, graph: Any, migrator: Any, then: str, code: int
) -> None:
    """La ruta leyó la cuenta (con su credencial de entonces) y el propietario rota o reconecta
    antes de `apply`: lo decide la fila bloqueada, no el objeto leído."""
    message = sent(world, graph)
    pk = message.channel_account_id
    stale = stale_account(world, pk)
    rotate(world, pk) if then == "rotate" else reconnect(world, pk)
    webhook(world, stale, code)
    assert stamp(world, message).provider_error_code == REASON[code]
    assert demotions(migrator) == [] and events(migrator) == []
    assert account(world, pk)[0] == ("CONNECTED" if then == "rotate" else "DISCONNECTED")


@pytest.mark.parametrize("unknown", ["future_credential", "no_claim_time"])
def test_without_knowing_the_credential_of_the_send_the_account_is_left_alone(
    world: SimpleNamespace, graph: Any, migrator: Any, unknown: str
) -> None:
    message = sent(world, graph)
    pk = message.channel_account_id
    if unknown == "future_credential":  # ninguna credencial de la cuenta cubre el reclamo
        migrator.execute(
            "UPDATE credentials SET created_at = now() + interval '1 hour' WHERE owner_id = %s",
            [pk],
        )
    else:
        migrator.execute("UPDATE messages SET sending_at = NULL WHERE id = %s", [message.pk])
    run(failed(131048))
    assert stamp(world, message).provider_error_code == "QUALITY_BLOCKED"  # el mensaje, sí
    assert account(world, pk) == ("CONNECTED", None) and demotions(migrator) == []


def test_the_credential_of_the_send_is_the_one_alive_at_the_claim_to_the_microsecond(
    world: SimpleNamespace, graph: Any, migrator: Any
) -> None:
    message = sent(world, graph)
    pk = message.channel_account_id
    first = stale_account(world, pk).credential_id
    rotate(world, pk)
    second = stale_account(world, pk).credential_id
    edge = dt.datetime(2026, 11, 24, 12, tzinfo=dt.UTC)
    tick = dt.timedelta(microseconds=1)
    migrator.execute("UPDATE credentials SET created_at = %s - interval '1 day', revoked_at = %s "
                     "WHERE id = %s", [edge, edge, first])  # fmt: skip
    migrator.execute("UPDATE credentials SET created_at = %s WHERE id = %s", [edge, second])
    other = number(world.a, "106540352242922")  # otra cuenta viva: su credencial no cuenta
    early = "UPDATE credentials SET created_at = %s - interval '1 day' WHERE owner_id = %s"
    migrator.execute(early, [edge, other])

    def sent_with(at: dt.datetime) -> Any:
        migrator.execute("UPDATE messages SET sending_at = %s WHERE id = %s", [at, message.pk])
        with tenant_scope(ctx(world.a)) as tenant:
            saved = Message.objects.get(pk=message.pk)
            return delivery._sent_with(require_scope(tenant), pk, saved)

    assert sent_with(edge - tick) == first  # revocada justo después del reclamo
    assert sent_with(edge) == second  # creada en el mismo instante del reclamo
    assert sent_with(edge - dt.timedelta(days=2)) is None  # antes de toda credencial
    migrator.execute("UPDATE credentials SET created_at = %s WHERE id = %s", [edge + tick, second])
    assert sent_with(edge) is None  # entre la revocada y la nueva: no se sabe
    migrator.execute("UPDATE credentials SET created_at = %s WHERE id = %s", [edge, second])
    migrator.execute("UPDATE credentials SET revoked_at = %s WHERE id = %s", [edge + tick, first])
    assert sent_with(edge) is None  # dos que se solapan (solo a mano): tampoco


# --- Hilos reales: la rotación y el webhook, la rotación y la comprobación de salud --------------


def race(migrator: Any, pk: Any, first: Any, second: Any) -> None:
    """Los dos esperan la cuenta, bloqueada aparte, en este orden; luego se suelta."""

    def closing(work: Any) -> Any:
        def run_it() -> None:
            try:
                work()
            finally:
                connection.close()

        return run_it

    threads = [threading.Thread(target=closing(work)) for work in (first, second)]
    with psycopg.connect(migrator_conninfo()) as held:
        held.execute("SELECT 1 FROM channel_accounts WHERE id = %s FOR UPDATE", [pk])
        for waiting, thread in enumerate(threads, start=1):
            thread.start()
            wait_for(migrator, waiting)
    for thread in threads:
        thread.join(timeout=10)
    assert not any(thread.is_alive() for thread in threads)


@pytest.mark.parametrize(("code", "demoted", "action"), EFFECTS)
@pytest.mark.parametrize("first", ["rotate", "webhook"])
def test_a_rotation_and_a_late_webhook_at_once_serialise_on_the_account_lock(
    world: SimpleNamespace, graph: Any, migrator: Any,
    first: str, code: int, demoted: tuple[str, str], action: str,
) -> None:  # fmt: skip
    message = sent(world, graph)
    pk = message.channel_account_id
    stale = stale_account(world, pk)
    work = {"rotate": lambda: rotate(world, pk), "webhook": lambda: webhook(world, stale, code)}
    race(migrator, pk, work[first], work["webhook" if first == "rotate" else "rotate"])
    applies = first == "webhook"  # con la credencial aún vigente, el fallo vale
    assert demotions(migrator) == ([action] if applies else [])
    assert events(migrator) == ([action] if applies else [])
    assert account(world, pk) == ("CONNECTED", None)  # la rotación posterior la recupera
    assert stamp(world, message).provider_error_code == REASON[code]


@pytest.mark.parametrize("first", ["rotate", "health"])
def test_a_rotation_and_an_older_red_health_reading_at_once_never_lose_the_rotation(
    world: SimpleNamespace, graph: Any, migrator: Any, first: str
) -> None:
    pk = talk(world).channel_account_id
    used, done = key(world.a, pk), []  # la que leyó la comprobación

    def health() -> None:
        with tenant_scope(ctx(world.a)) as tenant:
            done.append(accounts.apply_health(
                tenant, channel_account_id=pk, credential_id=used, quality="RED", tier="TIER_1K",
            ))  # fmt: skip

    work = {"rotate": lambda: rotate(world, pk), "health": health}
    race(migrator, pk, work[first], work["health" if first == "rotate" else "rotate"])
    mine = [a for a, *_ in audit(migrator) if a.startswith("channel_account.")]
    if first == "health":  # la lectura vale y la rotación (GREEN) la recupera
        assert done == [True]
        assert mine == ["channel_account.health", "channel_account.credentials_rotated"]
    else:  # la lectura era de la credencial anterior: nada se escribe
        assert done == [False] and mine == ["channel_account.credentials_rotated"]
    saved = stale_account(world, pk)
    assert (saved.status, saved.status_detail, saved.quality_rating) == ("CONNECTED", None, "GREEN")


def test_a_health_reading_of_a_replaced_credential_writes_nothing(
    world: SimpleNamespace, graph: Any, migrator: Any
) -> None:
    pk = talk(world).channel_account_id
    before = key(world.a, pk)
    rotate(world, pk)
    rotated = stale_account(world, pk)
    with tenant_scope(ctx(world.a)) as tenant:
        assert not accounts.apply_health(
            tenant, channel_account_id=pk, credential_id=before, quality="RED",
            tier="TIER_1K",
        )  # fmt: skip
    after = stale_account(world, pk)
    assert (after.version, after.last_healthcheck_at, after.status) == (
        rotated.version, rotated.last_healthcheck_at, "CONNECTED"
    )  # fmt: skip


# --- El orden de los bloqueos no cambia -----------------------------------------------------------


def locks(captured: CaptureQueriesContext) -> list[tuple[str, str]]:
    """(tabla, modo) de cada bloqueo de fila, en orden."""
    found = []
    for query in captured.captured_queries:
        sql = query["sql"]
        mode = re.search(r"FOR (NO KEY UPDATE|UPDATE|SHARE|KEY SHARE)", sql)
        table = re.search(r'FROM "?(\w+)"?', sql)
        if mode and table:
            found.append((table.group(1), mode.group(1)))
    return found


def test_the_claim_and_the_webhook_lock_the_account_before_the_conversation(
    world: SimpleNamespace, graph: Any
) -> None:
    """El reclamo (sin cambios, `claim_outbound`) toma la cuenta `FOR SHARE` antes del contacto, la
    conversación y el mensaje; el webhook, el mensaje y después la cuenta, antes de la
    conversación. `rotate_token`, `disconnect`, `_demote` y `apply_health` solo bloquean la
    cuenta: no hay ciclo."""
    message, _ = outbound(world, talk(world))
    with tenant_scope(ctx(world.a)) as tenant, CaptureQueriesContext(connection) as claim:
        assert commands.claim_outbound(tenant, message_id=message.pk) is not None
    assert locks(claim)[:4] == [
        ("channel_accounts", "SHARE"),
        ("contacts", "SHARE"),
        ("conversations", "NO KEY UPDATE"),
        ("messages", "NO KEY UPDATE"),
    ]
    with tenant_scope(ctx(world.a)):  # ya salió
        Message.objects.filter(pk=message.pk).update(external_message_id=WAMID)
    context = TenantContext(world.a, "webhook", actor_type=ActorType.INTEGRATION)
    stale = stale_account(world, message.channel_account_id)
    with tenant_scope(context), CaptureQueriesContext(connection) as hook:
        delivery.apply(context, stale, failed(131048))
    order = [table for table, _ in locks(hook)]  # el último, el de `_demote`: ya es suyo
    assert order == [
        "messages",
        "channel_accounts",
        "messages",
        "conversations",
        "channel_accounts",
    ]

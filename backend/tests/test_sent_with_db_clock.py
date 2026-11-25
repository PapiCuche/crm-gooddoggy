"""F7b-21: la credencial con la que salió un mensaje (`delivery._sent_with`) no depende del reloj de
ningún proceso. `sending_at`, `revoked_at` y `created_at` los pone PostgreSQL (`clock_timestamp()`,
disparadores de la `0064`) después de los bloqueos, también con las escrituras de la imagen
anterior; un efecto de cuenta que se omite por credencial desconocida deja un rastro solo con ids.
PostgreSQL real con `crm_app` e hilos reales; el simulador sustituye solo la red."""

import datetime as dt
import threading
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.conversations import accounts, commands, credentials, delivery
from apps.conversations.channels import SendResult
from apps.conversations.models import TOKEN_PURPOSE, Credential, Message
from core import sealed
from core.tenancy.scope import require_scope, tenant_scope
from tests.meta_sim import webhooks
from tests.meta_sim.graph import TEST_TOKEN
from tests.test_conversation_commands import outbound, world, writer
from tests.test_delivery_status import WAMID, fresh_wake, run
from tests.test_memberships import ctx
from tests.test_meta_webhook import channel
from tests.test_rate_limit_5xx import account, demotions, manager, rotate
from tests.test_social_subscription import key
from tests.test_webhook_late_demotion import locks, race
from tests.test_whatsapp_outbound import graph, redis, talk

__all__ = ["channel", "fresh_wake", "graph", "manager", "redis", "world", "writer"]  # fixtures
pytestmark = pytest.mark.usefixtures("tenant_db", "channel", "fresh_wake", "manager")
SKEW = dt.timedelta(hours=1)
META_TEXT = "TITULO-PRIVADO-DE-META"


def stamps(migrator: Any, message: Any, old: Any, new: Any) -> tuple[Any, ...]:
    return migrator.execute(  # type: ignore[no-any-return]
        "SELECT (SELECT sending_at FROM messages WHERE id = %s),"
        " (SELECT revoked_at FROM credentials WHERE id = %s),"
        " (SELECT created_at FROM credentials WHERE id = %s)",
        [message.pk, old, new],
    ).fetchone()


@pytest.mark.parametrize("first", ["claim", "rotate"])
@pytest.mark.parametrize("skewed", ["claim", "rotate"])
@pytest.mark.parametrize("sign", [1, -1], ids=["ahead", "behind"])
def test_the_claim_and_a_rotation_at_once_are_ordered_by_the_lock_not_by_any_app_clock(
    world: SimpleNamespace, graph: Any, migrator: Any, monkeypatch: pytest.MonkeyPatch,
    first: str, skewed: str, sign: int,
) -> None:  # fmt: skip
    """Hilos reales que esperan la cuenta en este orden; el reloj de uno de los dos procesos va una
    hora adelantado o atrasado. Las horas siguen el orden de los bloqueos y el `190` posterior
    solo degrada la cuenta si el mensaje salió con la credencial que tiene ahora."""
    message, _ = outbound(world, talk(world))
    pk, real, local = message.channel_account_id, timezone.now, threading.local()
    old = key(world.a, pk)
    monkeypatch.setattr(timezone, "now", lambda: real() + getattr(local, "skew", dt.timedelta()))

    def claim() -> None:
        local.skew = SKEW * sign if skewed == "claim" else dt.timedelta()
        with tenant_scope(ctx(world.a)) as tenant:
            assert commands.claim_outbound(tenant, message_id=message.pk) is not None

    def rotation() -> None:
        local.skew = SKEW * sign if skewed == "rotate" else dt.timedelta()
        rotate(world, pk)

    work = {"claim": claim, "rotate": rotation}
    race(migrator, pk, work[first], work["rotate" if first == "claim" else "claim"])
    new = key(world.a, pk)
    sending, revoked, created = stamps(migrator, message, old, new)
    if first == "claim":  # la rotación esperó el `FOR SHARE` del reclamo
        assert sending < revoked <= created
    else:  # el reclamo esperó la rotación
        assert revoked <= created < sending
    with tenant_scope(ctx(world.a)) as tenant:
        saved = Message.objects.get(pk=message.pk)
        assert delivery._sent_with(require_scope(tenant), pk, saved) == (
            old if first == "claim" else new
        )
        commands.settle_outbound(tenant, message_id=message.pk, result=SendResult(True, WAMID))
    run(webhooks.status(WAMID, "failed", error=(190, META_TEXT)))
    if first == "claim":  # salió con el token viejo: la cuenta rotada no se toca
        assert account(world, pk) == ("CONNECTED", None) and demotions(migrator) == []
    else:
        assert account(world, pk) == ("ERROR", "TOKEN_INVALID")
        assert demotions(migrator) == ["channel_account.token_invalid"]


def test_the_old_image_writes_are_accepted_and_take_the_database_clock(
    world: SimpleNamespace, graph: Any, migrator: Any
) -> None:
    """ADR-033 §7: lo que escribe la imagen de `main` (UPDATE del reclamo, de `revoke` e INSERT de
    `store`, con la hora de su proceso adelantada o atrasada un día), tal cual y como `crm_app`."""
    message, _ = outbound(world, talk(world))
    pk = message.channel_account_id
    old = key(world.a, pk)
    box = sealed.seal(TOKEN_PURPOSE, credentials.owner(pk), b"EAAtoken-nuevo-0000")
    day = dt.timedelta(days=1)
    with tenant_scope(ctx(world.a)), connection.cursor() as cursor:  # como crm_app
        cursor.execute("SELECT clock_timestamp()")
        start = cursor.fetchone()[0]
        cursor.execute(
            "UPDATE messages SET delivery_status = 'SENDING', sending_at = %s,"
            " provider_error_code = NULL, provider_error_detail = NULL WHERE id = %s"
            " AND delivery_status = 'QUEUED' AND provider_error_code IS NULL",
            [timezone.now() + day, message.pk],
        )
        assert cursor.rowcount == 1
        cursor.execute(
            "UPDATE credentials SET ciphertext = NULL, revoked_at = %s WHERE id = %s",
            [timezone.now() - day, old],
        )
        cursor.execute(
            "INSERT INTO credentials (id, organization_id, owner_type, owner_id, ciphertext,"
            " key_version, last_four, expires_at, expiry_checked_at, rotated_at, revoked_at,"
            " created_by_user_id, created_at) VALUES (gen_random_uuid(), %s, 'CHANNEL_ACCOUNT',"
            " %s, %s, %s, '0000', NULL, NULL, %s, NULL, NULL, %s) RETURNING id",
            [world.a, pk, box, int.from_bytes(box[1:3], "big"), timezone.now(),
             timezone.now() + day],
        )  # fmt: skip
        new = cursor.fetchone()[0]
        cursor.execute("SELECT clock_timestamp()")
        end = cursor.fetchone()[0]
    sending, revoked, created = stamps(migrator, message, old, new)
    assert start < sending < revoked < created < end  # la base, en el orden de las sentencias
    later = start - day  # después, un arreglo de datos de un `SENDING` o de una revocada: tal cual
    fix = "UPDATE messages SET delivery_status = 'SENDING', sending_at = %s WHERE id = %s"
    migrator.execute(fix, [later, message.pk])  # sigue `SENDING`: no es un reclamo
    migrator.execute("UPDATE credentials SET revoked_at = %s WHERE id = %s", [later, old])
    assert stamps(migrator, message, old, new)[:2] == (later, later)


def test_the_stamp_trigger_runs_last_and_never_changes_what_the_state_machine_decides(
    world: SimpleNamespace, graph: Any, migrator: Any
) -> None:
    """Los `BEFORE UPDATE` de `messages` corren por orden alfabético; el del reclamo es el último,
    solo escribe `sending_at` (que los otros no leen) y no hace consultas."""
    order = migrator.execute(
        "SELECT tgname FROM pg_trigger WHERE tgrelid = 'public.messages'::regclass"
        " AND NOT tgisinternal ORDER BY tgname"
    ).fetchall()
    assert [name for (name,) in order] == [
        "messages_delivery_advance", "messages_failure_settled", "messages_sending_stamped",
    ]  # fmt: skip
    bodies = migrator.execute(
        "SELECT prosrc FROM pg_proc WHERE proname IN"
        " ('messages_sending_stamped', 'credentials_stamped')"
    ).fetchall()
    assert len(bodies) == 2
    assert not any(w in b.upper() for (b,) in bodies for w in ("SELECT", "PERFORM", " FOR "))
    message, _ = outbound(world, talk(world))
    with tenant_scope(ctx(world.a)) as tenant:
        commands.claim_outbound(tenant, message_id=message.pk)
        commands.settle_outbound(tenant, message_id=message.pk, result=SendResult(True, WAMID))
    sent = stamps(migrator, message, None, None)[0]
    with (
        tenant_scope(ctx(world.a)),
        connection.cursor() as cursor,
        pytest.raises(Exception) as caught,  # noqa: PT011 — se mira el código de abajo
    ):
        back = "UPDATE messages SET delivery_status = 'SENDING' WHERE id = %s"
        cursor.execute(back, [message.pk])
    cause = caught.value.__cause__
    assert isinstance(cause, psycopg.errors.CheckViolation)  # atrasado: lo rechaza igual
    assert stamps(migrator, message, None, None)[0] == sent  # y nada cambió


def test_rotate_disconnect_and_health_lock_only_the_account(
    world: SimpleNamespace, graph: Any
) -> None:
    pk = talk(world).channel_account_id
    with tenant_scope(ctx(world.a, world.ana)) as tenant:
        with CaptureQueriesContext(connection) as rotated:
            _, used = accounts.rotate_token(tenant, channel_account_id=pk, access_token=TEST_TOKEN)
        assert used.created_at == Credential.objects.get(pk=used.pk).created_at  # la de la base
        with CaptureQueriesContext(connection) as health:
            assert accounts.apply_health(
                tenant, channel_account_id=pk, credential_id=used.pk, quality="GREEN",
                tier="TIER_1K",
            )  # fmt: skip
        with CaptureQueriesContext(connection) as gone:
            accounts.disconnect(tenant, channel_account_id=pk)
    only = [("channel_accounts", "NO KEY UPDATE")]
    assert (locks(rotated), locks(health), locks(gone)) == (only, only, only)


@pytest.mark.parametrize("unknown", ["no_claim_time", "no_covering_credential"])
def test_an_account_effect_skipped_for_an_unknown_credential_leaves_an_ids_only_trace(
    world: SimpleNamespace, graph: Any, migrator: Any, caplog: pytest.LogCaptureFixture,
    unknown: str,
) -> None:  # fmt: skip
    message, _ = outbound(world, talk(world))
    pk = message.channel_account_id
    with tenant_scope(ctx(world.a)) as tenant:
        commands.claim_outbound(tenant, message_id=message.pk)
        commands.settle_outbound(tenant, message_id=message.pk, result=SendResult(True, WAMID))
    if unknown == "no_claim_time":
        migrator.execute("UPDATE messages SET sending_at = NULL WHERE id = %s", [message.pk])
    else:
        migrator.execute("UPDATE credentials SET created_at = now() + interval '1 hour'")
    caplog.clear()
    assert run(webhooks.status(WAMID, "failed", error=(190, META_TEXT))) == {"PROCESSED": 1}
    assert account(world, pk) == ("CONNECTED", None) and demotions(migrator) == []
    traces = [r for r in caplog.records if "meta.webhook.account_effect_skipped" in r.getMessage()]
    assert len(traces) == 1
    text = traces[0].getMessage()
    assert str(pk) in text and str(message.pk) in text and "TOKEN_INVALID" in text
    for leaked in (META_TEXT, webhooks.CUSTOMER, WAMID, str(world.contact_a.pk)):
        assert leaked not in caplog.text


def test_a_known_credential_leaves_no_skip_trace(
    world: SimpleNamespace, graph: Any, migrator: Any, caplog: pytest.LogCaptureFixture
) -> None:
    message, _ = outbound(world, talk(world))
    with tenant_scope(ctx(world.a)) as tenant:
        commands.claim_outbound(tenant, message_id=message.pk)
        commands.settle_outbound(tenant, message_id=message.pk, result=SendResult(True, WAMID))
    run(webhooks.status(WAMID, "failed", error=(131048, META_TEXT)))
    assert demotions(migrator) == ["channel_account.degraded"]
    assert "account_effect_skipped" not in caplog.text

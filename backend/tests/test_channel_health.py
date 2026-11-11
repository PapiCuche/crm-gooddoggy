"""F7-06b: la comprobación de salud de las cuentas de WhatsApp (ADR-029 §12): calidad y nivel de
Meta, `PENDING` a `CONNECTED`, `DEGRADED` y `ERROR`, con la Graph API falsa servida por `core.http`.
PostgreSQL real con el rol `crm_app`."""

import datetime as dt
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest
from django.conf import settings
from django.utils import timezone

from apps.conversations import accounts, credentials, tasks
from apps.conversations.models import ChannelAccount, Credential
from core import http
from core.ids import new_id
from core.tenancy.scope import tenant_scope
from tests.meta_sim.graph import TEST_TOKEN, FakeGraph
from tests.meta_sim.webhooks import PHONE_NUMBER_ID, WABA_ID
from tests.test_conversation_commands import audit, world, writer
from tests.test_memberships import ctx
from tests.test_whatsapp_outbound import graph, redis

__all__ = ["graph", "redis", "world", "writer"]  # los fixtures, para pytest
pytestmark = pytest.mark.usefixtures("tenant_db")
OLD = timezone.now() - dt.timedelta(hours=1)  # una comprobación de hace una hora: toca otra


def account(org: UUID, number: str = PHONE_NUMBER_ID, **over: Any) -> UUID:
    """Una cuenta de WhatsApp con su token cifrado y una comprobación de hace una hora."""
    mine = new_id()
    fields: dict[str, Any] = {
        "status": "CONNECTED", "quality_rating": "GREEN", "messaging_limit_tier": "TIER_1K",
        "last_healthcheck_at": OLD,
    }  # fmt: skip
    with tenant_scope(ctx(org)) as tenant:
        stored = credentials.store(tenant, mine, TEST_TOKEN)
        ChannelAccount.objects.create(
            id=mine, platform="WHATSAPP", name="WA", credential_id=stored.pk,
            external_account_id=number, external_parent_id=WABA_ID, **fields | over,
        )  # fmt: skip
    return mine


def read(org: UUID, pk: UUID) -> ChannelAccount:
    with tenant_scope(ctx(org)):
        found: ChannelAccount = ChannelAccount.objects.get(pk=pk)
    return found


def test_a_check_refreshes_quality_and_tier_and_audits_only_ids(
    world: SimpleNamespace, graph: FakeGraph, migrator: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    mine = account(world.a)
    graph.quality, graph.tier = "UNKNOWN", "TIER_10K"
    opened: list[int] = []
    real = credentials.open_token

    def spy(credential: Any) -> str:
        opened.append(1)
        return real(credential)

    monkeypatch.setattr(credentials, "open_token", spy)
    assert tasks.healthcheck_accounts() == {"OK": 1}
    saved = read(world.a, mine)
    assert (saved.quality_rating, saved.messaging_limit_tier) == ("UNKNOWN", "TIER_10K")
    assert saved.last_healthcheck_at is not None and saved.last_healthcheck_at > OLD
    assert len(graph.calls) == 1 and opened == [1]  # una llamada y un solo descifrado
    call = graph.calls[0]
    assert call["headers"]["Authorization"] == f"Bearer {TEST_TOKEN}"  # en la cabecera
    logged = [a for a, *_ in audit(migrator) if a == "channel_account.health"]
    assert logged == ["channel_account.health"]  # cambió el nivel: se audita
    assert TEST_TOKEN not in str(audit(migrator)) + call["path"]
    assert tasks.healthcheck_accounts() == {}  # recién comprobada: no toca otra vez


def test_an_unchanged_check_is_not_audited_and_does_not_bump_the_version(
    world: SimpleNamespace, graph: FakeGraph, migrator: Any
) -> None:
    mine = account(world.a)
    before = read(world.a, mine).version
    graph.tier = "TIER_1K"
    assert tasks.healthcheck_accounts() == {"OK": 1}
    assert read(world.a, mine).version == before
    assert [a for a, *_ in audit(migrator) if a == "channel_account.health"] == []


def test_only_live_whatsapp_accounts_due_are_checked_oldest_first_and_with_a_cap(
    world: SimpleNamespace, graph: FakeGraph, migrator: Any
) -> None:
    graph.numbers.update({"100000000000011": WABA_ID, "100000000000012": WABA_ID})
    due = account(world.a)
    fresh = account(world.a, "100000000000011", last_healthcheck_at=timezone.now())
    never = account(world.a, "100000000000012", last_healthcheck_at=None, status="PENDING",
                    status_detail="WEBHOOK_NOT_SEEN")  # fmt: skip
    closed = []
    for number, over in (("100000000000013", {"status": "ERROR", "status_detail": "TOKEN_INVALID"}),
                         ("100000000000014", {"status": "DISCONNECTED"}),
                         ("100000000000015", {"deleted_at": timezone.now()})):  # fmt: skip
        closed.append(account(world.a, number, **over))
    before = timezone.now() - dt.timedelta(minutes=settings.CHANNEL_HEALTH_INTERVAL_MINUTES)
    with tenant_scope(ctx(world.a)):
        found = tasks.health_due(before, 10)
    assert found == [(world.a, never), (world.a, due)]  # la que nunca se comprobó, primero
    assert fresh not in [account_id for _, account_id in found]
    with tenant_scope(ctx(world.a)) as tenant:  # y tampoco se le lee la credencial a las cerradas
        for pk in closed:
            assert accounts.health_target(tenant, channel_account_id=pk) is None
    assert len(tasks.health_due(before, 1)) == 1 and tasks.health_due(before, 0) == []
    sig = "public.channel_accounts_health_due(timestamptz, integer)"
    row = migrator.execute(
        f"SELECT has_function_privilege('crm_app', '{sig}', 'EXECUTE'), "  # noqa: S608
        f"has_function_privilege('public', '{sig}', 'EXECUTE')"
    ).fetchone()
    assert row == (True, False)


def test_a_pending_account_with_the_webhook_seen_becomes_connected_and_one_without_it_does_not(
    world: SimpleNamespace, graph: FakeGraph
) -> None:
    graph.numbers["100000000000011"] = WABA_ID
    seen = account(world.a, status="PENDING", status_detail=None)
    waiting = account(
        world.a, "100000000000011", status="PENDING", status_detail="WEBHOOK_NOT_SEEN"
    )
    assert tasks.healthcheck_accounts() == {"OK": 2}
    assert read(world.a, seen).status == "CONNECTED"
    assert (read(world.a, waiting).status, read(world.a, waiting).status_detail) == (
        "PENDING",
        "WEBHOOK_NOT_SEEN",
    )  # sigue esperando el primer evento firmado


@pytest.mark.parametrize("quality", ["YELLOW", "RED"])
def test_a_low_quality_degrades_and_a_recovered_one_reconnects_only_what_quality_degraded(
    world: SimpleNamespace, graph: FakeGraph, migrator: Any, quality: str
) -> None:
    graph.numbers["100000000000011"] = WABA_ID
    mine = account(world.a)
    limited = account(world.a, "100000000000011", status="DEGRADED", status_detail="LIMIT_REACHED")
    graph.quality = quality
    tasks.healthcheck_accounts()
    assert (read(world.a, mine).status, read(world.a, mine).status_detail) == (
        "DEGRADED",
        "QUALITY_LOW",
    )
    migrator.execute(
        "UPDATE channel_accounts SET last_healthcheck_at = %s WHERE platform = 'WHATSAPP'", [OLD]
    )
    graph.quality = "GREEN"
    tasks.healthcheck_accounts()
    assert (read(world.a, mine).status, read(world.a, mine).status_detail) == ("CONNECTED", None)
    assert read(world.a, limited).status_detail == "LIMIT_REACHED"  # otro motivo: no se toca
    changes = [c for a, c, _ in audit(migrator) if a == "channel_account.health"]
    assert any(c.get("status") == ["CONNECTED", "DEGRADED"] for c in changes)


def test_a_token_meta_rejects_puts_the_account_in_error_and_it_is_not_checked_again(
    world: SimpleNamespace, graph: FakeGraph, migrator: Any
) -> None:
    mine = account(world.a)
    graph.token = "otro-token-que-no-es-el-guardado"  # noqa: S105 — el de Meta ya no es este
    assert tasks.healthcheck_accounts() == {"REJECTED": 1}
    saved = read(world.a, mine)
    assert (saved.status, saved.status_detail) == ("ERROR", "TOKEN_INVALID")
    assert tasks.healthcheck_accounts() == {}  # en `ERROR` ya no se comprueba
    assert [a for a, *_ in audit(migrator) if a.endswith("token_invalid")] == [
        "channel_account.token_invalid"
    ]


def test_an_unreadable_credential_is_a_rejected_token_without_calling_meta(
    world: SimpleNamespace, graph: FakeGraph
) -> None:
    mine = account(world.a)
    with tenant_scope(ctx(world.a)) as tenant:  # un cifrado que es de otra cuenta no se abre
        other = credentials.store(tenant, new_id(), TEST_TOKEN)
        ChannelAccount.objects.filter(pk=mine).update(credential_id=other.pk)
    assert tasks.healthcheck_accounts() == {"SKIPPED": 1}  # la credencial no es de esta cuenta
    assert graph.requests == [] and read(world.a, mine).status == "CONNECTED"


def test_a_credential_that_does_not_open_is_a_rejected_token_without_calling_meta(
    world: SimpleNamespace, graph: FakeGraph
) -> None:
    mine = account(world.a)
    with tenant_scope(ctx(world.a)):
        Credential.objects.filter(owner_id=mine).update(ciphertext=b"\x01" + bytes(40))
    assert tasks.healthcheck_accounts() == {"REJECTED": 1} and graph.requests == []
    assert read(world.a, mine).status == "ERROR"


def test_meta_down_changes_nothing_logs_no_secret_and_off_means_no_calls(
    world: SimpleNamespace, graph: FakeGraph, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:  # fmt: skip
    mine = account(world.a)

    def down(*args: Any, **kwargs: Any) -> Any:
        raise http.HTTPError(f"https://graph.facebook.com/?access_token={TEST_TOKEN}")

    monkeypatch.setattr(http, "request", down)
    assert tasks.healthcheck_accounts() == {"UNAVAILABLE": 1}
    saved = read(world.a, mine)
    assert (saved.status, saved.last_healthcheck_at) == ("CONNECTED", OLD)  # se reintenta luego
    assert TEST_TOKEN not in caplog.text
    monkeypatch.undo()
    settings.META_ENABLED = False
    try:
        assert tasks.healthcheck_accounts() == {} and graph.requests == []
    finally:
        settings.META_ENABLED = True


def test_one_failing_account_does_not_stop_the_others_and_each_runs_in_its_own_organization(
    world: SimpleNamespace, graph: FakeGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    graph.numbers["100000000000011"] = WABA_ID
    first, second = account(world.a), account(world.b, "100000000000011")
    real = tasks.check_account
    calls: list[UUID] = []

    def flaky(org: UUID, pk: UUID) -> str:
        calls.append(org)
        if org == world.a:
            raise RuntimeError("fallo propio de la cuenta")
        return real(org, pk)

    monkeypatch.setattr(tasks, "check_account", flaky)
    assert tasks.healthcheck_accounts() == {"ERROR": 1, "OK": 1} and len(calls) == 2
    assert read(world.b, second).last_healthcheck_at > OLD  # type: ignore[operator]
    assert read(world.a, first).last_healthcheck_at == OLD
    assert settings.CELERY_BEAT_SCHEDULE["conversations.healthcheck_accounts"]["schedule"] == 300.0

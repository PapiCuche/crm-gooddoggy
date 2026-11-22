"""F7b-06g: «página sin suscribir» en Messenger e Instagram (ADR-035 §3 y §10). La comprobación de
salud lee `GET /<PAGE_ID>/subscribed_apps` con la credencial de la cuenta y guarda
`PAGE_NOT_SUBSCRIBED` si la app del CRM (`META_APP_ID`) no está; la cuenta se recupera al volver a
suscribirla. Contra la Graph API de la página simulada (ADR-041); PostgreSQL real con `crm_app`."""

import datetime as dt
import json
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import psycopg
import pytest
from django.conf import settings as django_settings
from django.db import IntegrityError, connection
from django.utils import timezone

from apps.conversations import accounts, credentials, graph, tasks
from apps.conversations.models import ChannelAccount
from core.tenancy.scope import tenant_scope
from tests.meta_sim.graph import TEST_TOKEN
from tests.meta_sim.page_graph import FakePageGraph
from tests.meta_sim.social import APP_ID, IG_ACCOUNT_ID, OTHER_APP_ID, PAGE_ID
from tests.test_channel_accounts import run
from tests.test_channel_health import account as whatsapp_account
from tests.test_conversation_commands import audit, world, writer
from tests.test_memberships import ctx
from tests.test_meta_webhook import channel
from tests.test_social_accounts import connect, crm, meta, real_stack
from tests.test_social_consent import event, say
from tests.test_social_outbound import graph as page_graph
from tests.test_social_schema import account
from tests.test_whatsapp_outbound import redis

__all__ = ["channel", "crm", "meta", "page_graph", "real_stack", "redis", "world", "writer"]
pytestmark = pytest.mark.usefixtures("tenant_db")
DB = psycopg.Connection[Any]
OLD = timezone.now() - dt.timedelta(hours=1)
PLATFORMS = pytest.mark.parametrize("platform", ["MESSENGER", "INSTAGRAM"])
B_PAGE = "100000000000777"  # la página de la organización B
FIELDS = ["messages"]


def social(org: UUID, platform: str = "MESSENGER", page: str = PAGE_ID, **over: Any) -> UUID:
    """Una cuenta social con su token cifrado, `CONNECTED` y comprobada hace una hora."""
    instagram = platform == "INSTAGRAM"
    made = account(org, platform, IG_ACCOUNT_ID if instagram else page,
                   external_parent_id=page if instagram else None)  # fmt: skip
    with tenant_scope(ctx(org)) as tenant:
        stored = credentials.store(tenant, made.pk, TEST_TOKEN)
        fields = {"status": "CONNECTED", "status_detail": None, "credential_id": stored.pk,
                  "last_healthcheck_at": OLD} | over  # fmt: skip
        ChannelAccount.objects.filter(pk=made.pk).update(**fields)
    return made.pk  # type: ignore[no-any-return]


def read(org: UUID, pk: UUID) -> ChannelAccount:
    with tenant_scope(ctx(org)):
        return ChannelAccount.objects.get(pk=pk)  # type: ignore[no-any-return]


def key(org: UUID, pk: UUID) -> UUID:
    found = read(org, pk).credential_id
    assert found is not None
    return found


def state(org: UUID, pk: UUID) -> tuple[str, str | None]:
    found = read(org, pk)
    return found.status, found.status_detail


def again(org: UUID, pk: UUID) -> None:
    """Que a la cuenta le toque otra comprobación."""
    with tenant_scope(ctx(org)):
        ChannelAccount.objects.filter(pk=pk).update(last_healthcheck_at=OLD)


@pytest.fixture
def ours(page_graph: FakePageGraph, settings: Any) -> FakePageGraph:
    settings.META_APP_ID = APP_ID
    page_graph.subscribed[PAGE_ID] = FIELDS
    return page_graph


@PLATFORMS
def test_an_unsubscribed_page_degrades_the_account_and_resubscribing_recovers_it(
    world: SimpleNamespace, ours: FakePageGraph, migrator: DB, platform: str
) -> None:
    mine = social(world.a, platform)
    del ours.subscribed[PAGE_ID]
    assert tasks.healthcheck_accounts() == {"OK": 1}
    assert state(world.a, mine) == ("DEGRADED", "PAGE_NOT_SUBSCRIBED")
    assert [(c["method"], c["path"]) for c in ours.calls] == [
        ("GET", f"/v23.0/{PAGE_ID}/subscribed_apps")  # en Instagram, por su página
    ]
    logged = [(a, c) for a, c, _ in audit(migrator) if a == "channel_account.health"]
    assert logged == [("channel_account.health", {
        "status": ["CONNECTED", "DEGRADED"], "status_detail": [None, "PAGE_NOT_SUBSCRIBED"],
    })]  # fmt: skip
    assert TEST_TOKEN not in str(audit(migrator))
    version = read(world.a, mine).version
    again(world.a, mine)
    assert tasks.healthcheck_accounts() == {"OK": 1}  # sigue sin suscribir: nada cambia
    assert (state(world.a, mine), read(world.a, mine).version) == (
        ("DEGRADED", "PAGE_NOT_SUBSCRIBED"),
        version,
    )
    ours.subscribed[PAGE_ID] = FIELDS
    again(world.a, mine)
    assert tasks.healthcheck_accounts() == {"OK": 1}
    assert state(world.a, mine) == ("CONNECTED", None)


def test_only_our_app_counts_and_another_app_on_the_page_is_not_a_subscription(
    world: SimpleNamespace, ours: FakePageGraph, settings: Any
) -> None:
    mine = social(world.a)
    settings.META_APP_ID = OTHER_APP_ID  # la página tiene una app suscrita, pero no la nuestra
    tasks.healthcheck_accounts()
    assert state(world.a, mine) == ("DEGRADED", "PAGE_NOT_SUBSCRIBED")
    assert graph.ours([]) is False and graph.ours([OTHER_APP_ID, APP_ID]) is True


@pytest.mark.parametrize("app", ["", "abc", "12 34", None])
def test_without_a_valid_app_id_nothing_is_guessed_but_the_check_is_recorded(
    world: SimpleNamespace, ours: FakePageGraph, settings: Any, app: Any
) -> None:
    mine = social(world.a)
    del ours.subscribed[PAGE_ID]
    settings.META_APP_ID = app
    assert tasks.healthcheck_accounts() == {"OK": 1}
    found = read(world.a, mine)
    assert (found.status, found.status_detail, found.version) == ("CONNECTED", None, 1)
    assert found.last_healthcheck_at is not None and found.last_healthcheck_at > OLD


def test_a_pending_page_says_so_and_a_signed_event_connects_it_without_coming_back(
    world: SimpleNamespace, ours: FakePageGraph
) -> None:
    mine = social(world.a, status="PENDING", status_detail="WEBHOOK_NOT_SEEN")
    del ours.subscribed[PAGE_ID]
    tasks.healthcheck_accounts()
    assert state(world.a, mine) == ("PENDING", "PAGE_NOT_SUBSCRIBED")
    ours.subscribed[PAGE_ID] = FIELDS  # se volvió a suscribir antes de la próxima comprobación
    assert say("MESSENGER", event("MESSENGER", "Hola", "m_SIMIN000001")) == {"PROCESSED": 1}
    assert state(world.a, mine) == ("CONNECTED", None)
    again(world.a, mine)
    tasks.healthcheck_accounts()
    assert state(world.a, mine) == ("CONNECTED", None)  # no reaparece


def test_a_pending_page_that_is_subscribed_again_goes_back_to_waiting_for_meta(
    world: SimpleNamespace, ours: FakePageGraph
) -> None:
    mine = social(world.a, status="PENDING", status_detail="PAGE_NOT_SUBSCRIBED")
    tasks.healthcheck_accounts()
    assert state(world.a, mine) == ("PENDING", "WEBHOOK_NOT_SEEN")


@pytest.mark.parametrize("over", [
    {"status": "ERROR", "status_detail": "TOKEN_INVALID"},
    {"status": "ERROR", "status_detail": "MESSAGES_ACCESS_OFF"},
    {"status": "DISCONNECTED", "status_detail": "DISCONNECTED_BY_ADMIN"},
    {"deleted_at": OLD},
])  # fmt: skip
def test_an_account_in_error_or_gone_is_never_asked_nor_changed(
    world: SimpleNamespace, ours: FakePageGraph, over: dict[str, Any]
) -> None:
    mine = social(world.a, **over)
    del ours.subscribed[PAGE_ID]
    before = read(world.a, mine)
    assert tasks.healthcheck_accounts() == {} and ours.calls == []
    with tenant_scope(ctx(world.a)) as tenant:
        assert not accounts.apply_subscription(
            tenant, channel_account_id=mine, credential_id=key(world.a, mine), subscribed=False
        )
    after = read(world.a, mine)
    assert (after.status, after.status_detail, after.version) == (
        before.status, before.status_detail, before.version
    )  # fmt: skip


def test_another_organization_a_whatsapp_account_and_a_rotated_credential_are_not_written(
    world: SimpleNamespace, ours: FakePageGraph
) -> None:
    theirs, mine, whatsapp = (
        social(world.b, page=B_PAGE),
        social(world.a),
        whatsapp_account(world.a),
    )
    stale, b_key, wa_key = key(world.a, mine), key(world.b, theirs), key(world.a, whatsapp)
    with tenant_scope(ctx(world.a)) as tenant:
        assert not accounts.apply_subscription(  # la de B no existe para A (RLS)
            tenant, channel_account_id=theirs, credential_id=b_key, subscribed=False
        )
        assert not accounts.apply_subscription(  # nunca una cuenta de WhatsApp
            tenant, channel_account_id=whatsapp, credential_id=wa_key, subscribed=False
        )
        credentials.revoke(tenant, stale)
        fresh = credentials.store(tenant, mine, TEST_TOKEN, rotated=True)
        ChannelAccount.objects.filter(pk=mine).update(credential_id=fresh.pk)
        assert not accounts.apply_subscription(  # el resultado era de la credencial anterior
            tenant, channel_account_id=mine, credential_id=stale, subscribed=False
        )
    assert state(world.a, mine) == ("CONNECTED", None)
    assert state(world.b, theirs) == ("CONNECTED", None)


def test_each_organization_is_checked_in_its_own_scope_and_meta_is_asked_outside_a_transaction(
    world: SimpleNamespace, ours: FakePageGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    ours.pages[B_PAGE] = None
    mine, theirs = social(world.a), social(world.b, page=B_PAGE)
    ours.subscribed[B_PAGE] = FIELDS
    del ours.subscribed[PAGE_ID]
    inside: list[bool] = []
    real = graph.subscribed_apps

    def spy(token: str, page: str) -> tuple[list[str], bool]:
        inside.append(connection.in_atomic_block)
        return real(token, page)

    monkeypatch.setattr(graph, "subscribed_apps", spy)
    assert tasks.healthcheck_accounts() == {"OK": 2}
    assert inside == [False, False]
    assert state(world.a, mine) == ("DEGRADED", "PAGE_NOT_SUBSCRIBED")
    assert state(world.b, theirs) == ("CONNECTED", None)


@pytest.mark.parametrize("code", [4, 17, 32, 613, 80001, 80002, 80006])
def test_a_throttle_is_unavailable_and_changes_nothing(
    world: SimpleNamespace, ours: FakePageGraph, monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    mine = social(world.a)
    monkeypatch.setattr(ours, "handle", lambda *a: (400, {}, b'{"error": {"code": %d}}' % code))
    assert tasks.healthcheck_accounts() == {"UNAVAILABLE": 1}
    assert read(world.a, mine).last_healthcheck_at == OLD and state(world.a, mine) == (
        "CONNECTED", None
    )  # fmt: skip


def test_more_pages_of_apps_without_ours_in_the_first_is_unknown_not_unsubscribed(
    world: SimpleNamespace, ours: FakePageGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    mine, other = social(world.a), {"id": OTHER_APP_ID}
    rest = {"next": f"https://graph.facebook.com/v23.0/{PAGE_ID}/subscribed_apps?after=X"}
    pages = {"paging": rest}

    def reply(*_: Any) -> tuple[int, dict[str, str], bytes]:
        return 200, {}, json.dumps({"data": [other], **pages}).encode()

    monkeypatch.setattr(ours, "handle", reply)
    assert tasks.healthcheck_accounts() == {"OK": 1}
    found = read(world.a, mine)  # solo se lee la primera página: la nuestra puede ir después
    assert (found.status, found.status_detail, found.version) == ("CONNECTED", None, 1)
    assert found.last_healthcheck_at is not None and found.last_healthcheck_at > OLD
    pages.clear()  # sin más páginas, la lista está entera: no está suscrita
    again(world.a, mine)
    tasks.healthcheck_accounts()
    assert state(world.a, mine) == ("DEGRADED", "PAGE_NOT_SUBSCRIBED")
    other["id"], pages["paging"] = APP_ID, rest  # la nuestra en la primera página sí cuenta
    again(world.a, mine)
    tasks.healthcheck_accounts()
    assert state(world.a, mine) == ("CONNECTED", None)


def test_without_an_app_id_an_unsubscribed_page_never_recovers_by_guessing(
    crm: SimpleNamespace, meta: FakePageGraph, settings: Any
) -> None:
    settings.META_APP_ID = ""  # la página tiene la app suscrita, pero no se sabe cuál es la nuestra
    made = connect(crm, "MESSENGER")[0]
    with tenant_scope(ctx(crm.a)):
        ChannelAccount.objects.filter(pk=made.pk).update(
            status="DEGRADED", status_detail="PAGE_NOT_SUBSCRIBED", last_healthcheck_at=OLD
        )
    assert tasks.healthcheck_accounts() == {"OK": 1}
    assert state(crm.a, made.pk) == ("DEGRADED", "PAGE_NOT_SUBSCRIBED")
    rotated, _ = run(crm.a, crm.ana, accounts.rotate_token, channel_account_id=made.pk,
                     access_token=TEST_TOKEN, platform="MESSENGER")  # fmt: skip
    assert (rotated.status, rotated.status_detail) == ("DEGRADED", "PAGE_NOT_SUBSCRIBED")


def test_a_190_is_a_rejected_token_and_no_role_on_the_page_keeps_the_state(
    world: SimpleNamespace, ours: FakePageGraph, migrator: DB, caplog: pytest.LogCaptureFixture
) -> None:
    mine = social(world.a)
    ours.no_role = {PAGE_ID}
    assert tasks.healthcheck_accounts() == {"KEPT": 1}
    found = read(world.a, mine)
    assert (found.status, found.status_detail, found.version) == ("CONNECTED", None, 1)
    assert found.last_healthcheck_at is not None and found.last_healthcheck_at > OLD
    ours.no_role, ours.token = set(), "otro-token"  # noqa: S105 — Meta ya no acepta el guardado
    again(world.a, mine)
    assert tasks.healthcheck_accounts() == {"REJECTED": 1}
    assert state(world.a, mine) == ("ERROR", "TOKEN_INVALID")
    assert read(world.a, mine).health_attempt_at is None  # F7b-06h: el rechazo no es un intento
    assert TEST_TOKEN not in caplog.text + str(audit(migrator))


@pytest.mark.parametrize("revive", [False, True])
def test_rotating_the_token_records_a_page_that_lost_the_subscription(
    crm: SimpleNamespace, meta: FakePageGraph, migrator: DB, settings: Any, revive: bool
) -> None:
    settings.META_APP_ID = APP_ID
    made = connect(crm, "MESSENGER")[0]
    with tenant_scope(ctx(crm.a)):
        ChannelAccount.objects.filter(pk=made.pk).update(status="CONNECTED", status_detail=None)
    if revive:
        run(crm.a, crm.ana, accounts.mark_token_invalid, channel_account_id=made.pk)
    meta.subscribed.clear()
    rotated, _ = run(crm.a, crm.ana, accounts.rotate_token, channel_account_id=made.pk,
                     access_token=TEST_TOKEN, platform="MESSENGER")  # fmt: skip
    assert (rotated.status, rotated.status_detail) == ("DEGRADED", "PAGE_NOT_SUBSCRIBED")
    [(_, changes, _)] = [
        r for r in audit(migrator) if r[0] == "channel_account.credentials_rotated"
    ]
    was = "ERROR" if revive else "CONNECTED"
    assert changes["status"] == [was, "DEGRADED"]
    assert changes["status_detail"] == ["TOKEN_INVALID" if revive else None, "PAGE_NOT_SUBSCRIBED"]
    meta.subscribed[PAGE_ID] = FIELDS
    rotated, _ = run(crm.a, crm.ana, accounts.rotate_token, channel_account_id=made.pk,
                     access_token=TEST_TOKEN, platform="MESSENGER")  # fmt: skip
    assert (rotated.status, rotated.status_detail) == ("CONNECTED", None)
    last = [c for a, c, _ in audit(migrator) if a == "channel_account.credentials_rotated"][-1]
    assert last["status_detail"] == ["PAGE_NOT_SUBSCRIBED", None]  # F7b-06h: también al limpiarlo


def test_the_database_keeps_the_closed_set_and_the_due_function_returns_only_ids(
    world: SimpleNamespace, ours: FakePageGraph
) -> None:
    mine, igs = social(world.a), social(world.a, "INSTAGRAM", page=B_PAGE)
    update = "UPDATE channel_accounts SET status = 'DEGRADED', status_detail = %s WHERE id = %s"
    with tenant_scope(ctx(world.a)), connection.cursor() as cursor:  # como crm_app
        cursor.execute(update, ["PAGE_NOT_SUBSCRIBED", mine])
    with pytest.raises(IntegrityError) as caught, tenant_scope(ctx(world.a)):
        with connection.cursor() as cursor:
            cursor.execute(update, ["PAGE_MESSAGING_OFF", igs])  # sin productor de Meta: no existe
    assert caught.value.__cause__.diag.constraint_name == "channel_accounts_detail_ck"  # type: ignore[union-attr]
    before = timezone.now() - dt.timedelta(minutes=django_settings.CHANNEL_HEALTH_INTERVAL_MINUTES)
    with connection.cursor() as cursor:  # fuera de todo scope: solo ids de cuentas vivas
        cursor.execute("SELECT * FROM public.channel_accounts_health_due(%s, %s)", [before, 10])
        assert {tuple(map(str, row)) for row in cursor.fetchall()} == {
            (str(world.a), str(mine)), (str(world.a), str(igs))
        }  # fmt: skip
        assert [c.name for c in cursor.description or []] == ["org", "account"]

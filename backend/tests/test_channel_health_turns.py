"""F7b-06h: lo que la revisión de F7b-06g dejó pendiente (ADR-035 §3 y §10). La pantalla solo da
por suscrita una página si Meta lo dijo en la última respuesta (`page_subscription`); los límites
de uso del negocio de Meta no son un token rechazado; una cuenta que no se pudo comprobar espera su
turno y no deja sin él a las demás. Graph API simulada (ADR-041); PostgreSQL real con `crm_app`."""

import datetime as dt
import threading
import time
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import psycopg
import pytest
from django.db import IntegrityError, connection, connections
from django.utils import timezone

from apps.conversations import accounts, graph, tasks
from apps.conversations.models import ChannelAccount
from core.tenancy.scope import tenant_scope
from tests.meta_sim.graph import TEST_TOKEN
from tests.meta_sim.page_graph import FakePageGraph
from tests.meta_sim.social import APP_ID, PAGE_ID
from tests.test_channel_accounts import run
from tests.test_channel_health import account as whatsapp_account
from tests.test_memberships import ctx
from tests.test_social_accounts import connect, crm, meta
from tests.test_social_page_owner import app
from tests.test_social_standby import API, reader
from tests.test_social_subscription import (
    OLD,
    PLATFORMS,
    again,
    channel,
    key,
    ours,
    page_graph,
    read,
    real_stack,
    redis,
    social,
    state,
    world,
    writer,
)

__all__ = ["channel", "crm", "meta", "ours", "page_graph", "real_stack", "redis", "world", "writer"]
pytestmark = pytest.mark.usefixtures("tenant_db")
DB = psycopg.Connection[Any]
OLDER = OLD - dt.timedelta(hours=1)
PAGES = ("100000000000501", "100000000000502", "100000000000503")
BUSINESS_LIMITS = pytest.mark.parametrize("code", [80001, 80002, 80006])


def shown(org: UUID, pk: UUID) -> str | None:
    return read(org, pk).page_subscription


@PLATFORMS
def test_the_screen_says_subscribed_only_when_meta_said_so_and_the_account_is_still_checked(
    world: SimpleNamespace, ours: FakePageGraph, settings: Any, platform: str
) -> None:
    mine = social(world.a, platform)
    assert shown(world.a, mine) == "UNKNOWN"  # conectada, pero nadie preguntó a Meta todavía
    assert tasks.healthcheck_accounts() == {"OK": 1}
    assert shown(world.a, mine) == "SUBSCRIBED"
    ours.no_role = {PAGE_ID}  # un rechazo que no cambia nada: ya no se sabe
    again(world.a, mine)
    assert (tasks.healthcheck_accounts(), shown(world.a, mine)) == ({"KEPT": 1}, "UNKNOWN")
    ours.no_role, settings.META_APP_ID = set(), ""  # sin el id de la app: no se sabe
    again(world.a, mine)
    assert (tasks.healthcheck_accounts(), shown(world.a, mine)) == ({"OK": 1}, "UNKNOWN")
    settings.META_APP_ID = APP_ID
    del ours.subscribed[PAGE_ID]
    again(world.a, mine)
    tasks.healthcheck_accounts()
    assert shown(world.a, mine) == "NOT_SUBSCRIBED"
    ours.subscribed[PAGE_ID] = ["messages"]
    again(world.a, mine)
    tasks.healthcheck_accounts()
    assert (state(world.a, mine), shown(world.a, mine)) == (("CONNECTED", None), "SUBSCRIBED")
    with tenant_scope(ctx(world.a)) as tenant:  # en `ERROR` ya no se comprueba: no se sabe
        accounts.mark_token_invalid(tenant, channel_account_id=mine)
    assert shown(world.a, mine) == "UNKNOWN"


def test_the_api_shows_only_the_closed_code_and_the_database_keeps_it_off_whatsapp(
    world: SimpleNamespace, ours: FakePageGraph
) -> None:
    mine, whatsapp = social(world.a), whatsapp_account(world.a)
    with tenant_scope(ctx(world.a)):
        ChannelAccount.objects.filter(pk=mine).update(  # F7b-06i: una respuesta reciente
            page_subscribed=True, last_healthcheck_at=timezone.now()
        )
    rows = reader(world).get(API.format("org-a") + "channels/").json()["results"]
    found = {row["id"]: row["page_subscription"] for row in rows if row["platform"] != "SANDBOX"}
    assert found == {str(mine): "SUBSCRIBED", str(whatsapp): None}
    assert all("page_subscribed" not in row and "health_attempt_at" not in row for row in rows)
    with pytest.raises(IntegrityError) as caught, tenant_scope(ctx(world.a)):
        with connection.cursor() as cursor:  # como crm_app
            cursor.execute(
                "UPDATE channel_accounts SET page_subscribed = true WHERE id = %s", [whatsapp]
            )
    assert caught.value.__cause__.diag.constraint_name == "channel_accounts_page_subscribed_ck"  # type: ignore[union-attr]


def test_rotating_writes_what_meta_says_now_and_never_keeps_an_older_answer(
    crm: SimpleNamespace, meta: FakePageGraph, settings: Any
) -> None:
    settings.META_APP_ID = APP_ID
    made = connect(crm, "MESSENGER")[0]
    assert made.page_subscription == "UNKNOWN"  # conectar suscribe, pero no lee la lista

    def rotate() -> ChannelAccount:
        rotated: ChannelAccount = run(crm.a, crm.ana, accounts.rotate_token,
                                      channel_account_id=made.pk, access_token=TEST_TOKEN,
                                      platform="MESSENGER")[0]  # fmt: skip
        return rotated

    assert rotate().page_subscription == "SUBSCRIBED"
    settings.META_APP_ID = ""
    assert (rotate().page_subscription, read(crm.a, made.pk).page_subscribed) == ("UNKNOWN", None)


@BUSINESS_LIMITS
def test_a_business_rate_limit_is_not_a_rejected_token_when_connecting_or_rotating(
    crm: SimpleNamespace, meta: FakePageGraph, monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    made = connect(crm, "MESSENGER")[0]
    monkeypatch.setattr(meta, "handle", lambda *a: (400, {}, b'{"error": {"code": %d}}' % code))
    with pytest.raises(graph.GraphUnavailable):
        connect(crm, "INSTAGRAM")
    with pytest.raises(graph.GraphUnavailable):
        run(crm.a, crm.ana, accounts.rotate_token, channel_account_id=made.pk,
            access_token=TEST_TOKEN, platform="MESSENGER")  # fmt: skip
    assert state(crm.a, made.pk) == ("PENDING", "WEBHOOK_NOT_SEEN")


def test_accounts_that_cannot_be_checked_wait_their_turn_and_never_starve_the_others(
    world: SimpleNamespace, ours: FakePageGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    stuck = [social(world.a, page=page, last_healthcheck_at=OLDER) for page in PAGES]
    fine = social(world.a)  # su última comprobación es más reciente: va detrás
    real = tasks.check_account

    def flaky(org: UUID, pk: UUID) -> str:
        if pk == stuck[2]:
            raise RuntimeError("fallo propio de la cuenta")
        return {stuck[0]: "UNAVAILABLE", stuck[1]: "SKIPPED"}.get(pk) or real(org, pk)

    monkeypatch.setattr(tasks, "check_account", flaky)
    assert tasks.healthcheck_accounts(batch_size=3) == {"UNAVAILABLE": 1, "SKIPPED": 1, "ERROR": 1}
    assert tasks.healthcheck_accounts(batch_size=3) == {"OK": 1}  # ahora le toca a la que espera
    assert read(world.a, fine).last_healthcheck_at > OLD  # type: ignore[operator]
    for pk in stuck:  # el intento no dice nada de la cuenta: ni estado, ni versión, ni la hora
        found = read(world.a, pk)
        assert (found.status, found.status_detail, found.version) == ("CONNECTED", None, 1)
        assert found.last_healthcheck_at == OLDER and found.health_attempt_at > OLD  # type: ignore[operator]
    assert tasks.healthcheck_accounts(batch_size=3) == {}  # un intento por intervalo, como todas


def test_the_queue_goes_by_the_oldest_turn_and_a_future_attempt_never_hides_an_account(
    world: SimpleNamespace, ours: FakePageGraph
) -> None:
    tried, waiting = social(world.a, page=PAGES[0], last_healthcheck_at=OLDER), social(world.a)
    now = timezone.now()
    before = now - dt.timedelta(minutes=15)

    def attempted(at: dt.datetime) -> list[tuple[UUID, UUID]]:
        with tenant_scope(ctx(world.a)):  # como crm_app
            ChannelAccount.objects.filter(pk=tried).update(health_attempt_at=at)
        return tasks.health_due(before, 5)

    assert attempted(now - dt.timedelta(minutes=30)) == [(world.a, waiting), (world.a, tried)]
    assert attempted(now - dt.timedelta(minutes=5)) == [(world.a, waiting)]  # aún no le toca
    assert attempted(now + dt.timedelta(days=365)) == [(world.a, tried), (world.a, waiting)]


def test_health_target_never_returns_a_deleted_account(
    world: SimpleNamespace, ours: FakePageGraph
) -> None:
    gone, live = social(world.a, deleted_at=OLD), social(world.a, page=PAGES[0])
    with tenant_scope(ctx(world.a)) as tenant:
        assert accounts.health_target(tenant, channel_account_id=gone) is None
        assert accounts.health_target(tenant, channel_account_id=live) is not None


def test_a_check_waits_for_a_concurrent_write_and_never_overwrites_its_error(
    world: SimpleNamespace, ours: FakePageGraph
) -> None:
    """Otra transacción tiene la fila (p. ej. un envío que la pone en `ERROR`). La comprobación
    espera el bloqueo y vuelve a mirar la fila: ya no se comprueba, así que no la toca. Sin el
    `select_for_update` leería la fila vieja y la pisaría con `DEGRADED`."""
    mine = social(world.a)
    credential, outcome = key(world.a, mine), []

    def check() -> None:
        try:
            with tenant_scope(ctx(world.a)) as tenant:
                outcome.append(accounts.apply_subscription(
                    tenant, channel_account_id=mine, credential_id=credential, subscribed=False
                ))  # fmt: skip
        finally:
            connections.close_all()

    with app() as holder, app() as watcher:
        watcher.autocommit = True
        holder.execute("SELECT set_config('app.tenant_id', %s, true)", [str(world.a)])
        holder.execute("SELECT 1 FROM channel_accounts WHERE id = %s FOR NO KEY UPDATE", [mine])
        thread = threading.Thread(target=check)
        thread.start()
        waiting = "SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock'"
        for _ in range(200):
            if watcher.execute(waiting).fetchone() != (0,):
                break
            time.sleep(0.05)
        assert watcher.execute(waiting).fetchone() == (1,)
        holder.execute("UPDATE channel_accounts SET status = 'ERROR', status_detail = "
                       "'TOKEN_INVALID', version = version + 1 WHERE id = %s", [mine])  # fmt: skip
        holder.commit()
        thread.join(20)
    assert not thread.is_alive() and outcome == [False]
    assert state(world.a, mine) == ("ERROR", "TOKEN_INVALID")

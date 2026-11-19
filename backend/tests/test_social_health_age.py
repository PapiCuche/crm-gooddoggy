"""F7b-06i (ADR-035 §10): la salud de una cuenta de Messenger o Instagram no se queda con datos
viejos. Un «página suscrita» de Meta vale cuatro intervalos de salud: pasada esa edad, o con una
hora futura, la pantalla dice «sin comprobar». Y una última comprobación con fecha futura ya no
saca a la cuenta de la cola de salud (la `0052`). Graph API simulada (ADR-041); PostgreSQL real con
`crm_app`."""

import datetime as dt
import importlib
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import psycopg
import pytest
from django.core.management import call_command
from django.db import connection
from django.utils import timezone

from apps.conversations import models, tasks
from apps.conversations.models import ChannelAccount
from core.tenancy.scope import tenant_scope
from tests.meta_sim.page_graph import FakePageGraph
from tests.test_channel_health_turns import PAGES
from tests.test_memberships import ctx
from tests.test_pipelines_tables import as_migrator
from tests.test_social_standby import API, reader
from tests.test_social_subscription import (
    channel,
    ours,
    page_graph,
    read,
    real_stack,
    redis,
    social,
    world,
    writer,
)

__all__ = ["channel", "ours", "page_graph", "real_stack", "redis", "world", "writer"]
pytestmark = pytest.mark.usefixtures("tenant_db")
DB = psycopg.Connection[Any]
AT = dt.datetime(2026, 11, 18, 12, tzinfo=dt.UTC)
SECOND = dt.timedelta(seconds=1)


def seen(
    at: dt.datetime | None, now: dt.datetime, monkeypatch: pytest.MonkeyPatch, **over: Any
) -> Any:
    """Lo que la pantalla diría a las `now` de una cuenta que Meta dio por suscrita a las `at`."""
    monkeypatch.setattr(models, "timezone", SimpleNamespace(now=lambda: now))
    fields = {"platform": "MESSENGER", "status": "CONNECTED", "page_subscribed": True} | over
    return ChannelAccount(last_healthcheck_at=at, **fields).page_subscription


def test_the_last_answer_is_worth_four_health_intervals_and_not_one_second_more(
    monkeypatch: pytest.MonkeyPatch, settings: Any
) -> None:
    age, skew = models.subscription_max_age(), models.HEALTH_CLOCK_SKEW
    assert (age, skew) == (dt.timedelta(hours=1), dt.timedelta(minutes=5))  # 4 intervalos de 15
    assert seen(AT, AT, monkeypatch) == seen(AT, AT + age, monkeypatch) == "SUBSCRIBED"
    assert seen(AT, AT + age + SECOND, monkeypatch) == "UNKNOWN"
    assert seen(None, AT, monkeypatch) == "UNKNOWN"
    settings.CHANNEL_HEALTH_INTERVAL_MINUTES = 5  # la edad sigue a la cadencia
    assert models.subscription_max_age() == dt.timedelta(minutes=20)
    assert seen(AT, AT + dt.timedelta(minutes=20, seconds=1), monkeypatch) == "UNKNOWN"


def test_a_future_answer_is_only_tolerated_within_the_clock_skew(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """El worker escribe la hora con su reloj y la web la lee con el suyo: unos segundos de desfase
    no ocultan una respuesta recién llegada; una hora más allá del margen no se afirma."""
    skew = models.HEALTH_CLOCK_SKEW
    assert seen(AT + skew, AT, monkeypatch) == "SUBSCRIBED"
    assert seen(AT + skew + SECOND, AT, monkeypatch) == "UNKNOWN"
    assert seen(AT + dt.timedelta(days=365), AT, monkeypatch) == "UNKNOWN"


def test_age_never_hides_a_lost_subscription_nor_shows_whatsapp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old = AT + dt.timedelta(days=30)
    lost = {"status": "DEGRADED", "status_detail": "PAGE_NOT_SUBSCRIBED", "page_subscribed": False}
    assert seen(AT, old, monkeypatch, **lost) == "NOT_SUBSCRIBED"  # un estado, no una respuesta
    assert seen(AT, AT, monkeypatch, platform="WHATSAPP", page_subscribed=None) is None
    assert seen(AT, AT, monkeypatch, status="ERROR") == "UNKNOWN"


def test_while_meta_throttles_the_check_the_screen_stops_claiming_an_old_subscription(
    world: SimpleNamespace, ours: FakePageGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    mine = social(world.a, page_subscribed=True, last_healthcheck_at=timezone.now())

    def shown() -> Any:
        rows = reader(world).get(API.format("org-a") + "channels/").json()["results"]
        return next(row["page_subscription"] for row in rows if row["id"] == str(mine))

    assert shown() == "SUBSCRIBED"
    stale = timezone.now() - models.subscription_max_age() - dt.timedelta(minutes=1)
    with tenant_scope(ctx(world.a)):
        ChannelAccount.objects.filter(pk=mine).update(last_healthcheck_at=stale)
    monkeypatch.setattr(ours, "handle", lambda *a: (400, {}, b'{"error": {"code": 80006}}'))
    assert tasks.healthcheck_accounts() == {"UNAVAILABLE": 1}  # Meta no contesta: nada cambia
    found = read(world.a, mine)
    assert (found.page_subscribed, found.last_healthcheck_at) == (True, stale)
    assert shown() == "UNKNOWN"  # pero la pantalla ya no afirma una respuesta de hace una hora


def due(before: dt.datetime, limit: int = 10) -> list[tuple[UUID, UUID]]:
    return tasks.health_due(before, limit)


def ahead_by(world: SimpleNamespace, pk: UUID, interval: str, attempt: str | None = None) -> None:
    """Pone la última comprobación (y el intento) relativos al reloj de la base, como `crm_app`."""
    with tenant_scope(ctx(world.a)), connection.cursor() as cursor:
        cursor.execute(
            "UPDATE channel_accounts SET last_healthcheck_at = now() + %s::interval, "
            "health_attempt_at = now() + %s::interval WHERE id = %s", [interval, attempt, pk]
        )  # fmt: skip


def test_a_future_check_never_hides_an_account_from_the_health_queue(
    world: SimpleNamespace, ours: FakePageGraph
) -> None:
    ahead, recent = social(world.a), social(world.a, page=PAGES[0])
    now = timezone.now()
    before = now - dt.timedelta(minutes=15)
    ahead_by(world, recent, "0 seconds")
    ahead_by(world, ahead, "1 year")
    assert due(before) == [(world.a, ahead)]  # como una nunca comprobada: la primera
    assert tasks.healthcheck_accounts() == {"OK": 1}  # la comprobación corrige la hora
    fixed = read(world.a, ahead).last_healthcheck_at
    assert fixed is not None and now <= fixed <= timezone.now()
    assert due(before) == []  # con el reloj del worker: dentro del margen, no vuelve a la cola


def test_the_queue_tolerates_the_same_clock_skew_as_the_screen(
    world: SimpleNamespace, ours: FakePageGraph
) -> None:
    mine = social(world.a)
    before = timezone.now() - dt.timedelta(minutes=15)
    skew = int(models.HEALTH_CLOCK_SKEW.total_seconds())
    ahead_by(world, mine, f"{skew - 2} seconds")  # el margen menos lo que tarde la prueba
    assert due(before) == []  # un worker algo adelantado: comprobada hace nada
    ahead_by(world, mine, f"{skew + 1} seconds")
    assert due(before) == [(world.a, mine)]  # más allá del margen: futura, cuenta como ninguna


def test_a_future_check_gets_one_turn_per_interval_and_never_floods_the_queue(
    world: SimpleNamespace, ours: FakePageGraph
) -> None:
    ahead, waiting = social(world.a, page=PAGES[0]), social(world.a, page=PAGES[1])
    before = timezone.now() - dt.timedelta(minutes=15)

    def queued(attempt: str | None) -> list[tuple[UUID, UUID]]:
        ahead_by(world, ahead, "1 year", attempt)
        return due(before)

    assert queued(None) == [(world.a, ahead), (world.a, waiting)]
    assert queued("-5 minutes") == [(world.a, waiting)]  # ya lo intentó: espera su turno
    assert queued("-30 minutes") == [(world.a, waiting), (world.a, ahead)]  # su turno, detrás
    assert queued("1 day") == [(world.a, ahead), (world.a, waiting)]  # intento futuro: ninguno
    assert [pk for _, pk in due(before, 1)] == [ahead] and due(before, 0) == []


def test_the_migration_goes_back_to_the_exact_0051_body_and_forward(migrator: DB) -> None:
    body = "SELECT md5(prosrc), prosecdef, proconfig FROM pg_proc WHERE oid = %s::regprocedure"
    sig = ["public.channel_accounts_health_due(timestamptz, integer)"]
    acl = "SELECT has_function_privilege(%s, %s, 'EXECUTE')"
    old = importlib.import_module("apps.conversations.migrations.0051_channel_health_followups")
    new = importlib.import_module("apps.conversations.migrations.0052_channel_health_future_check")

    def state() -> tuple[Any, ...]:
        found = migrator.execute(body, sig).fetchone()
        assert found is not None
        public = migrator.execute(acl, ["public", sig[0]]).fetchone()
        app = migrator.execute(acl, ["crm_app", sig[0]]).fetchone()
        return (*found, public, app)

    skew = f"interval '{models.HEALTH_CLOCK_SKEW.total_seconds() / 60:.0f} minutes'"
    assert f"a.last_healthcheck_at <= now() + {skew} " in new.BODY  # el mismo margen que la web
    assert "a.health_attempt_at <= now() THEN" in new.BODY  # la base lo escribe: sin margen
    md5 = "SELECT md5(%s)"
    fixed = ["search_path=pg_catalog, public, pg_temp"]
    assert state() == (migrator.execute(md5, [f" {new.BODY} "]).fetchone()[0], True, fixed,  # type: ignore[index]
                       (False,), (True,))  # fmt: skip
    try:
        with as_migrator():
            call_command("migrate", "conversations", "0051", verbosity=0)
        assert state() == (migrator.execute(md5, [f" {old.BODY} "]).fetchone()[0], True, fixed,  # type: ignore[index]
                           (False,), (True,))  # fmt: skip
    finally:
        with as_migrator():
            call_command("migrate", verbosity=0)
    assert state()[0] == migrator.execute(md5, [f" {new.BODY} "]).fetchone()[0]  # type: ignore[index]

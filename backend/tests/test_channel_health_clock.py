"""F7b-06j (ADR-035 §10): la cola de salud de los canales no depende del reloj de los workers.
`last_healthcheck_at` lo escribe la hora de PostgreSQL en sus cinco escritores, y la pasada decide
qué cuentas tocan con la misma hora; el intervalo es de al menos un minuto y el tope de la función
de descubrimiento, 500. Graph API simulada; PostgreSQL real con `crm_app`."""

import datetime as dt
import re
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest
from django.conf import settings
from django.db import connection, connections
from django.utils import timezone

from apps.conversations import accounts, meta_page, tasks
from apps.conversations import graph as graph_api
from apps.conversations.models import ChannelAccount
from core.tenancy.scope import tenant_scope
from tests.meta_sim.graph import FakeGraph
from tests.meta_sim.page_graph import FakePageGraph
from tests.test_channel_accounts import NEW_TOKEN, run
from tests.test_channel_accounts import connect as connect_number
from tests.test_channel_accounts import meta as number_meta
from tests.test_channel_health import account as number
from tests.test_channel_health import graph
from tests.test_channel_health_turns import PAGES
from tests.test_memberships import ctx
from tests.test_social_accounts import connect as connect_page
from tests.test_social_accounts import meta as page_meta
from tests.test_social_page_owner import app
from tests.test_social_subscription import (
    OLD,
    crm,
    key,
    ours,
    page_graph,
    read,
    real_stack,
    redis,
    social,
    world,
    writer,
)

__all__ = ["crm", "graph", "number_meta", "ours", "page_graph", "page_meta", "real_stack",
           "redis", "world", "writer"]  # fmt: skip
pytestmark = pytest.mark.usefixtures("tenant_db")
DAY = dt.timedelta(days=1)  # mucho más que el margen de 5 minutos y que el intervalo
PHASE_7 = Path(__file__).resolve().parents[2] / "docs" / "phases" / "phase-7.md"


def db_now() -> dt.datetime:
    with connection.cursor() as cursor:
        cursor.execute("SELECT clock_timestamp()")
        found: dt.datetime = cursor.fetchone()[0]
    return found


@pytest.fixture(params=[DAY, -DAY], ids=["ahead", "behind"])
def skewed(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> dt.timedelta:
    """El reloj del worker (y de la web) desfasado un día respecto al de la base."""
    real, skew = timezone.now, request.param
    monkeypatch.setattr(timezone, "now", lambda: real() + skew)
    return skew  # type: ignore[no-any-return]


def stored(org: UUID, pk: UUID) -> dt.datetime:
    found = read(org, pk).last_healthcheck_at
    assert found is not None
    return found


def test_connecting_a_number_or_rotating_its_token_writes_the_database_time(
    crm: SimpleNamespace, number_meta: FakeGraph, skewed: dt.timedelta
) -> None:
    before = db_now()
    made, _ = connect_number(crm)
    after = db_now()
    assert before <= stored(crm.a, made.pk) <= after
    assert made.last_healthcheck_at == stored(crm.a, made.pk)  # lo que devuelve, sin expresiones
    number_meta.token, before = NEW_TOKEN, db_now()
    rotated, _ = run(crm.a, crm.ana, accounts.rotate_token, channel_account_id=made.pk,
                     access_token=NEW_TOKEN)  # fmt: skip
    assert before <= stored(crm.a, made.pk) <= db_now()
    assert rotated.last_healthcheck_at == stored(crm.a, made.pk)


@pytest.mark.parametrize("platform", ["MESSENGER", "INSTAGRAM"])
def test_connecting_a_page_writes_the_database_time(
    crm: SimpleNamespace, page_meta: FakePageGraph, skewed: dt.timedelta, platform: str
) -> None:
    before = db_now()
    made, _ = connect_page(crm, platform)
    assert before <= stored(crm.a, made.pk) <= db_now()
    assert made.last_healthcheck_at == stored(crm.a, made.pk)


def test_a_number_check_writes_the_database_time(
    world: SimpleNamespace, graph: FakeGraph, skewed: dt.timedelta
) -> None:
    mine = number(world.a)
    before = db_now()
    with tenant_scope(ctx(world.a)) as tenant:
        done = accounts.apply_health(
            tenant, channel_account_id=mine, credential_id=key(world.a, mine), quality="GREEN",
            tier="TIER_1K",
        )  # fmt: skip
    assert done and before <= stored(world.a, mine) <= db_now()


def test_a_page_check_writes_the_database_time(
    world: SimpleNamespace, ours: FakePageGraph, skewed: dt.timedelta
) -> None:
    mine = social(world.a)
    before = db_now()
    with tenant_scope(ctx(world.a)) as tenant:
        done = accounts.apply_subscription(
            tenant, channel_account_id=mine, credential_id=key(world.a, mine), subscribed=True
        )
    assert done and before <= stored(world.a, mine) <= db_now()


def test_which_accounts_are_due_is_decided_with_the_database_time(
    world: SimpleNamespace, ours: FakePageGraph, skewed: dt.timedelta
) -> None:
    """Un worker adelantado más que el intervalo no comprueba en cada pasada la cuenta recién
    comprobada; uno atrasado no deja sin comprobar la que ya le toca."""
    fresh = social(world.a, last_healthcheck_at=db_now())
    assert tasks.healthcheck_accounts() == {}  # recién comprobada: no le toca
    with tenant_scope(ctx(world.a)):
        ChannelAccount.objects.filter(pk=fresh).update(last_healthcheck_at=OLD)
    assert tasks.healthcheck_accounts() == {"OK": 1}  # hace una hora: le toca
    assert tasks.healthcheck_accounts() == {}  # y no otra vez en la siguiente pasada


def test_the_pass_takes_the_accounts_checked_exactly_one_interval_ago_or_earlier(
    world: SimpleNamespace, ours: FakePageGraph
) -> None:
    """El corte de la pasada es el intervalo exacto, contado con la hora de la base: un minuto
    antes no le toca, un minuto después sí."""
    minutes = settings.CHANNEL_HEALTH_INTERVAL_MINUTES
    early, late = social(world.a, page=PAGES[0]), social(world.a)
    with tenant_scope(ctx(world.a)), connection.cursor() as cursor:
        for pk, ago in ((early, minutes - 1), (late, minutes + 1)):
            cursor.execute(
                "UPDATE channel_accounts SET last_healthcheck_at = now() - "
                "make_interval(mins => %s) WHERE id = %s", [ago, pk]
            )  # fmt: skip
    kept = stored(world.a, early)
    assert tasks.healthcheck_accounts() == {"OK": 1}
    assert stored(world.a, early) == kept and stored(world.a, late) > kept


def test_a_number_check_waits_for_the_row_lock_and_reads_the_row_again(
    world: SimpleNamespace, graph: FakeGraph
) -> None:
    """Otra transacción tiene la fila y la pone en `ERROR`: la comprobación espera el bloqueo y ya
    no la toca. Sin el `select_for_update` la pisaría con `DEGRADED`."""
    mine, outcome = number(world.a), []
    used = key(world.a, mine)

    def check() -> None:
        try:
            with tenant_scope(ctx(world.a)) as tenant:
                outcome.append(accounts.apply_health(
                    tenant, channel_account_id=mine, credential_id=used, quality="RED",
                    tier="TIER_1K",
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
    found = read(world.a, mine)
    assert not thread.is_alive() and outcome == [False]
    assert (found.status, found.status_detail) == ("ERROR", "TOKEN_INVALID")


def test_the_discovery_returns_at_most_500_accounts(world: SimpleNamespace) -> None:
    """El tope va dentro de la función SECURITY DEFINER (`0017`…`0052`): ni más ni menos."""
    with tenant_scope(ctx(world.a)):
        ChannelAccount.objects.bulk_create(
            ChannelAccount(
                organization_id=world.a,
                platform="MESSENGER",
                name="Página",
                status="PENDING",
                external_account_id=str(100000000100000 + n),
            )  # fmt: skip
            for n in range(501)
        )
    before = db_now() - dt.timedelta(minutes=settings.CHANNEL_HEALTH_INTERVAL_MINUTES)
    assert len(tasks.health_due(before, 10**6)) == 500
    assert len(tasks.health_due(before, 499)) == 499


def test_the_documented_meta_limits_are_the_ones_retried() -> None:
    """La lista de `docs/phases/phase-7.md` (F7b-06j, la reconfirmación que prevé ADR-035 §6) es
    la que reintentan los adaptadores de Messenger e Instagram, y la comprobación de salud y la
    conexión las tratan como «Meta no disponible»."""
    entry = re.search(r"Límites de Meta \(ADR-035 §6\): ([^.]+)\.", PHASE_7.read_text())
    assert entry is not None
    codes = {int(code) for code in re.findall(r"`(\d+)`", entry.group(1))}
    assert codes == set(meta_page.TOO_FAST) and codes <= set(graph_api.THROTTLED)

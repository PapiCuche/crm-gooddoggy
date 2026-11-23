"""F7b-12 (#1077): la caducidad del token de una página de Messenger o Instagram (`debug_token`
contra la Graph API simulada, con la política real de `core.http`) y sus avisos a 10 y 3 días.
PostgreSQL real con `crm_app`; las garantías de la base, con SQL directo."""

import datetime as dt
import logging
import threading
import time
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import psycopg
import pytest
import urllib3
from django.core import mail as outbox
from django.db import connection, connections

from apps.access.selectors import holders
from apps.conversations import accounts, expiry, tasks
from apps.conversations.models import Credential
from core import http
from core.observability import reporting
from core.tenancy.scope import tenant_scope
from tests.meta_sim.graph import TEST_TOKEN, Cut
from tests.meta_sim.page_graph import APP_TOKEN, FakePageGraph
from tests.meta_sim.social import APP_ID, OTHER_APP_ID
from tests.meta_sim.webhooks import TEST_APP_SECRET
from tests.test_channel_accounts import everything, run
from tests.test_contacts_api import crm, real_stack
from tests.test_memberships import ctx
from tests.test_meta_alerts import Spy
from tests.test_social_accounts import connect
from tests.test_social_accounts import meta as page_meta
from tests.test_social_page_owner import app

__all__ = ["crm", "page_meta", "real_stack"]
pytestmark = pytest.mark.usefixtures("tenant_db")
DB = psycopg.Connection[Any]
SECRETS = (TEST_TOKEN, TEST_APP_SECRET, APP_TOKEN)


@pytest.fixture
def meta(page_meta: FakePageGraph, settings: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    settings.META_APP_ID = APP_ID  # el vigente primero; el anterior no abre `debug_token`
    settings.META_APP_SECRETS = [TEST_APP_SECRET, "secreto-anterior-no-real"]
    settings.APP_ORIGIN = "https://crm.example"
    spy = Spy()
    monkeypatch.setattr(reporting, "reporter", lambda: spy)
    return SimpleNamespace(graph=page_meta, spy=spy)


def at(days: float) -> int:
    return int(time.time() + days * 86400)


def cred(org: UUID, pk: Any) -> Credential:
    with tenant_scope(ctx(org)):
        found: Credential = Credential.objects.get(pk=pk)
        return found


def cut() -> None:
    raise Cut


def leaks(*texts: str) -> list[str]:
    return [s for s in SECRETS for text in texts if s in text]


def shift(migrator: DB, pk: Any, days: float) -> None:
    """El paso del tiempo: la caducidad leída no se reescribe (el disparador), salvo aquí."""
    migrator.execute("ALTER TABLE credentials DISABLE TRIGGER credentials_expiry_guard")
    try:
        migrator.execute(
            "UPDATE credentials SET expires_at = now() + make_interval(secs => %s) WHERE id = %s",
            [days * 86400, pk],
        )
    finally:
        migrator.execute("ALTER TABLE credentials ENABLE TRIGGER credentials_expiry_guard")


@pytest.mark.parametrize(
    "platform, debug, days",
    [
        ("MESSENGER", {"expires_at": 0, "data_access_expires_at": 0}, None),
        ("INSTAGRAM", {"expires_at": 0}, None),
        ("MESSENGER", {"expires_at": at(60), "data_access_expires_at": at(90)}, 60),
        ("INSTAGRAM", {"expires_at": at(60), "data_access_expires_at": at(90)}, 60),
        ("INSTAGRAM", {"expires_at": 0, "data_access_expires_at": at(40)}, 40),  # Instagram sí
        ("MESSENGER", {"expires_at": 0, "data_access_expires_at": at(40)}, None),  # Messenger no
    ],
)
def test_connecting_reads_the_expiry_that_counts(
    crm: SimpleNamespace, meta: Any, platform: str, debug: dict[str, int], days: int | None
) -> None:
    meta.graph.debug = debug
    found = cred(crm.a, connect(crm, platform)[1].pk)
    assert found.expiry_checked_at is not None
    if days is None:
        assert found.expires_at is None
    else:
        assert found.expires_at is not None
        assert abs((found.expires_at - dt.datetime.now(dt.UTC)).days - days) <= 1
    calls = [c for c in meta.graph.calls if c["path"].endswith("/debug_token")]
    assert len(calls) == 1 and calls[0]["query"] == {"input_token": [TEST_TOKEN]}


@pytest.mark.parametrize(
    "case, reason",
    [("no_app", "no_app"), ("no_secret", "no_app"), ("wrong_secret", "app_token_rejected"),
     ("old_first", "app_token_rejected"), ("meta_100", "app_token_rejected"),
     ("down", "unavailable"), ("invalid", "invalid_token"), ("other_app", "other_app"),
     ("negative", "malformed"), ("bool", "malformed"), ("text", "malformed"),
     ("huge", "malformed")],
)  # fmt: skip
def test_an_unreadable_expiry_is_unknown_logs_why_and_never_blocks_the_connection(
    crm: SimpleNamespace, meta: Any, settings: Any, caplog: pytest.LogCaptureFixture, case: str,
    reason: str,
) -> None:  # fmt: skip
    cases: dict[str, dict[str, Any]] = {
        "invalid": {"is_valid": False, "expires_at": at(5)},
        "other_app": {"app_id": OTHER_APP_ID, "expires_at": at(5)},
        "negative": {"expires_at": -1}, "bool": {"expires_at": True},
        "text": {"expires_at": str(at(5))}, "huge": {"expires_at": 10**15},
    }  # fmt: skip
    meta.graph.debug = None if case == "meta_100" else cases.get(case, {"expires_at": at(5)})
    if case == "no_app":
        settings.META_APP_ID = ""
    secrets = {"no_secret": [], "wrong_secret": ["otro"], "old_first": ["otro", TEST_APP_SECRET]}
    settings.META_APP_SECRETS = secrets.get(case, settings.META_APP_SECRETS)
    if case == "down":
        real = meta.graph.handle
        meta.graph.handle = lambda m, t, *a: cut() if "debug_token" in t else real(m, t, *a)
    with caplog.at_level(logging.INFO):
        account, made = connect(crm, "INSTAGRAM")
    found = cred(crm.a, made.pk)
    assert account.status == "PENDING" and (found.expiry_checked_at, found.expires_at) == (
        None,
        None,
    )
    logged = [r for r in caplog.records if "channel.token_expiry_unknown" in r.getMessage()]
    assert len(logged) == 1 and f"'reason': '{reason}'" in logged[0].getMessage()
    loud = reason in ("no_app", "app_token_rejected", "malformed")
    assert logged[0].levelno == (logging.WARNING if loud else logging.INFO)


@pytest.mark.parametrize("error", ["max_retry", "unexpected"])
def test_a_network_error_carrying_the_url_leaks_no_token(
    crm: SimpleNamespace, meta: Any, migrator: DB, caplog: pytest.LogCaptureFixture, error: str
) -> None:
    """urllib3 pone la ruta con la query en el texto del error: ni ese texto ni el token llegan a un
    registro, a Sentry, a la base o a la respuesta."""
    real = meta.graph.handle

    def handle(method: str, target: str, *args: Any) -> Any:
        if "debug_token" in target:
            url = f"https://graph.facebook.com{target}"
            if error == "unexpected":
                raise RuntimeError(f"boom {url} {args[0]}")
            reason = urllib3.exceptions.NewConnectionError(None, f"failed {url}")  # type: ignore[arg-type]
            raise urllib3.exceptions.MaxRetryError(None, url, reason)  # type: ignore[arg-type]
        return real(method, target, *args)

    meta.graph.handle = handle
    with caplog.at_level(logging.DEBUG):
        made = connect(crm, "MESSENGER")[1]
        meta.graph.debug = {"expires_at": at(1)}
        done = tasks.token_expiry(backfill=True)  # el aviso de un token aún sin caducidad: nada
    assert cred(crm.a, made.pk).expiry_checked_at is None and done == {"UNKNOWN": 1}
    assert leaks(caplog.text, str(meta.spy.messages), everything(migrator)) == []


def test_debug_token_never_retries_and_both_passes_are_scheduled(
    crm: SimpleNamespace, meta: Any, settings: Any, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:  # fmt: skip
    """`core.http` reintentaría con un aviso que lleva la URL (y el token): `retry=False`."""
    real, asked = http.request, []

    def spy(method: str, url: str, **kwargs: Any) -> Any:
        asked.append((url.split("?")[0].rsplit("/", 1)[-1], kwargs.get("retry", True)))
        return real(method, url, **kwargs)

    monkeypatch.setattr(http, "request", spy)
    settings.META_APP_ID = ""
    connect(crm, "MESSENGER")
    settings.META_APP_ID = APP_ID
    with caplog.at_level(logging.INFO):
        assert tasks.token_expiry(backfill=True) == {"READ": 1}
    assert ("debug_token", False) in asked and all(r for name, r in asked if name != "debug_token")
    assert "channel.token_expiry_swept" in caplog.text
    beat = settings.CELERY_BEAT_SCHEDULE
    hourly, daily = beat["conversations.token_expiry"], beat["conversations.token_expiry_backfill"]
    assert hourly["task"] == daily["task"] == tasks.token_expiry.name
    assert (hourly.get("kwargs"), daily["kwargs"]) == (None, {"backfill": True})
    assert (hourly["schedule"].minute, daily["schedule"].hour) == ({23}, {5})
    assert tasks.token_expiry.tenancy == "platform"


def test_rotating_reads_the_new_expiry_and_starts_the_warnings_again(
    crm: SimpleNamespace, meta: Any, migrator: DB
) -> None:
    meta.graph.debug = {"expires_at": at(5)}
    account, old = connect(crm, "MESSENGER")
    assert tasks.token_expiry() == {"WARNED": 1}
    meta.graph.debug = {"expires_at": at(8)}
    _, new = run(crm.a, crm.ana, accounts.rotate_token, channel_account_id=account.pk,
                 access_token=TEST_TOKEN, platform="MESSENGER")  # fmt: skip
    fresh = cred(crm.a, new.pk)
    assert fresh.expiry_warned_days is None and fresh.expires_at is not None
    assert cred(crm.a, old.pk).revoked_at is not None
    assert tasks.token_expiry() == {"WARNED": 1}  # el token nuevo, su propio aviso de 10 días
    assert cred(crm.a, new.pk).expiry_warned_days == 10


def test_a_token_warns_once_at_10_and_once_at_3_days_without_the_token_anywhere(
    crm: SimpleNamespace, meta: Any, migrator: DB, caplog: pytest.LogCaptureFixture
) -> None:
    meta.graph.debug = {"expires_at": at(9)}
    account, made = connect(crm, "INSTAGRAM")
    with tenant_scope(ctx(crm.a)) as tenant:
        managers = sorted(email for _, email in holders(tenant, "channels.manage"))
    assert managers
    with caplog.at_level(logging.DEBUG):
        assert tasks.token_expiry() == {"WARNED": 1}
        assert tasks.token_expiry() == {}  # una vez por umbral
        shift(migrator, made.pk, 2)
        assert tasks.token_expiry() == {"WARNED": 1}
        assert tasks.token_expiry() == {}
    assert cred(crm.a, made.pk).expiry_warned_days == 3
    assert sorted(m.to[0] for m in outbox.outbox) == sorted(managers * 2)
    subjects = {m.subject for m in outbox.outbox}
    assert subjects == {expiry.SUBJECT.format(days=10), expiry.SUBJECT.format(days=3)}
    assert all("https://crm.example/o/" in m.body and "Tienda" in m.body for m in outbox.outbox)
    rows = migrator.execute(
        "SELECT entity_id, metadata->>'days', actor_type FROM audit_logs WHERE action = %s",
        [expiry.ACTION],
    ).fetchall()
    assert sorted(rows) == [(account.pk, "10", "SYSTEM"), (account.pk, "3", "SYSTEM")]
    alerts = [c for name, c, _ in meta.spy.messages if name == expiry.EVENT]
    assert [a["days"] for a in alerts] == [10, 3] and alerts[0]["platform"] == "INSTAGRAM"
    mails = " ".join(f"{m.subject} {m.body}" for m in outbox.outbox)
    assert leaks(caplog.text, str(meta.spy.messages), everything(migrator), mails) == []
    assert made.last_four not in mails


@pytest.mark.parametrize("days, warned, expired", [(2, 3, False), (-1, 3, True)])
def test_a_late_token_gets_only_the_most_urgent_warning(
    crm: SimpleNamespace, meta: Any, days: float, warned: int, expired: bool
) -> None:
    meta.graph.debug = {"expires_at": at(days)}
    made = connect(crm, "MESSENGER")[1]
    assert tasks.token_expiry() == {"WARNED": 1}
    assert tasks.token_expiry() == {}
    assert cred(crm.a, made.pk).expiry_warned_days == warned
    alerts = [c for name, c, _ in meta.spy.messages if name == expiry.EVENT]
    assert [(a["days"], a["expired"]) for a in alerts] == [(warned, expired)]
    subject = expiry.EXPIRED if expired else expiry.SUBJECT.format(days=3)
    assert {m.subject for m in outbox.outbox} == {subject}


@pytest.mark.parametrize("debug", [{"expires_at": 0}, {"expires_at": at(11)}, None])
def test_never_far_or_unknown_never_warns(
    crm: SimpleNamespace, meta: Any, debug: dict[str, int] | None
) -> None:
    meta.graph.debug = debug
    connect(crm, "MESSENGER")
    assert tasks.token_expiry() == {} and outbox.outbox == [] and meta.spy.messages == []


@pytest.mark.parametrize(
    "change", ["disconnected", "error", "unlinked", "revoked", "suspended", "disabled"]
)
def test_only_live_checked_accounts_warn(
    crm: SimpleNamespace, meta: Any, migrator: DB, settings: Any, change: str
) -> None:
    meta.graph.debug = {"expires_at": at(2)}
    account, made = connect(crm, "MESSENGER")
    if change == "disconnected":
        run(
            crm.a, crm.ana, accounts.disconnect, channel_account_id=account.pk, platform="MESSENGER"
        )
    elif change == "error":
        with tenant_scope(ctx(crm.a)) as tenant:
            accounts.mark_token_invalid(tenant, channel_account_id=account.pk)
    elif change == "unlinked":  # una credencial que ya no es la de la cuenta
        migrator.execute("UPDATE channel_accounts SET credential_id = NULL, status = 'PENDING' "
                         "WHERE id = %s", [account.pk])  # fmt: skip
    elif change == "revoked":  # invalidada y la cuenta aún apunta a ella
        migrator.execute("UPDATE credentials SET revoked_at = now(), ciphertext = NULL "
                         "WHERE id = %s", [made.pk])  # fmt: skip
    elif change == "suspended":
        migrator.execute("UPDATE organizations SET status = 'SUSPENDED' WHERE id = %s", [crm.a])
    else:
        settings.META_ENABLED = False
    if change != "disabled":
        assert expiry._warn(crm.a) == []  # también sin pasar por el descubrimiento
    assert tasks.token_expiry() == {} and outbox.outbox == []
    assert cred(crm.a, made.pk).expiry_warned_days is None


def test_each_organization_warns_only_its_own_managers(crm: SimpleNamespace, meta: Any) -> None:
    meta.graph.debug = {"expires_at": at(2)}
    connect(crm, "MESSENGER")
    meta.graph.pages = {**meta.graph.pages, "100000000000777": None}
    made_b = connect(crm, "MESSENGER", org="b", page_id="100000000000777")[1]
    assert expiry._warn(crm.a) and cred(crm.b, made_b.pk).expiry_warned_days is None
    assert outbox.outbox == []  # `_warn` no envía: lo hace `sweep` tras confirmar
    assert tasks.token_expiry() == {"WARNED": 1}  # solo B queda pendiente
    assert [m.to for m in [*outbox.outbox]] == [[crm.luis.email]]
    with tenant_scope(ctx(crm.a)):
        assert not Credential.objects.filter(pk=made_b.pk).exists()  # RLS: B no se ve desde A


def test_a_concurrent_sweep_waits_for_the_claim_and_does_not_warn_twice(
    crm: SimpleNamespace, meta: Any, migrator: DB
) -> None:
    """Otro barrido reclamó el umbral y no confirmó: este espera el bloqueo de la fila, la vuelve a
    leer y no avisa. Sin el `UPDATE` condicional avisaría dos veces."""
    meta.graph.debug = {"expires_at": at(9)}
    made = connect(crm, "MESSENGER")[1]
    outcome: list[Any] = []

    def sweep() -> None:
        try:
            outcome.append(expiry._warn(crm.a))
        finally:
            connections.close_all()

    with app() as holder, app() as watcher:
        watcher.autocommit = True
        holder.execute("SELECT set_config('app.tenant_id', %s, true)", [str(crm.a)])
        holder.execute("UPDATE credentials SET expiry_warned_days = 10 WHERE id = %s", [made.pk])
        thread = threading.Thread(target=sweep)
        thread.start()
        waiting = "SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock'"
        for _ in range(200):
            if watcher.execute(waiting).fetchone() != (0,):
                break
            time.sleep(0.05)
        assert watcher.execute(waiting).fetchone() == (1,)
        holder.commit()
        thread.join(20)
    assert not thread.is_alive() and outcome == [[]]
    assert migrator.execute("SELECT count(*) FROM audit_logs WHERE action = %s",
                            [expiry.ACTION]).fetchone() == (0,)  # fmt: skip


def test_the_daily_pass_reads_an_unknown_expiry_once_and_the_hourly_one_never_asks(
    crm: SimpleNamespace, meta: Any, settings: Any
) -> None:
    settings.META_APP_ID = ""  # conectada sin app configurada: no se sabe
    made = connect(crm, "INSTAGRAM")[1]
    settings.META_APP_ID = APP_ID
    meta.graph.debug, before = {"expires_at": at(2)}, len(meta.graph.calls)
    assert tasks.token_expiry() == {}  # la de cada hora no pregunta a Meta
    assert len(meta.graph.calls) == before
    meta.graph.debug = None  # Meta no contesta: sigue sin saberse, una llamada por pasada
    assert tasks.token_expiry(backfill=True) == {"UNKNOWN": 1}
    assert len(meta.graph.calls) == before + 1
    meta.graph.debug = {"expires_at": at(2)}
    assert tasks.token_expiry(backfill=True) == {"READ": 1, "WARNED": 1}
    assert tasks.token_expiry(backfill=True) == {}  # leída: no se vuelve a preguntar
    found = cred(crm.a, made.pk)
    assert found.expiry_checked_at is not None and found.expiry_warned_days == 3


def test_the_hourly_pass_never_asks_meta_even_with_a_warning_due(
    crm: SimpleNamespace, meta: Any, settings: Any
) -> None:
    meta.graph.debug = {"expires_at": at(2)}
    connect(crm, "MESSENGER")
    settings.META_APP_ID = ""
    connect(crm, "INSTAGRAM")  # la misma página: su caducidad no se sabe
    settings.META_APP_ID, before = APP_ID, len(meta.graph.calls)
    assert tasks.token_expiry() == {"WARNED": 1} and len(meta.graph.calls) == before


def test_a_read_that_lost_the_race_keeps_the_first_one(
    crm: SimpleNamespace, meta: Any, settings: Any, migrator: DB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Otro worker guardó la caducidad mientras esta pasada preguntaba a Meta: no se reescribe."""
    settings.META_APP_ID = ""
    made = connect(crm, "MESSENGER")[1]
    settings.META_APP_ID, real = APP_ID, expiry.read

    def read(platform: str, token: str) -> Any:
        migrator.execute(
            "UPDATE credentials SET expiry_checked_at = now(), expires_at = NULL WHERE id = %s",
            [made.pk],
        )
        return real(platform, token)

    monkeypatch.setattr(expiry, "read", read)
    meta.graph.debug = {"expires_at": at(2)}
    assert tasks.token_expiry(backfill=True) == {"READ": 0}
    assert cred(crm.a, made.pk).expires_at is None


@pytest.mark.parametrize(
    "sql, constraint",
    [
        ("expires_at = now()", "credentials_expiry_guard"),
        ("expiry_checked_at = NULL, expires_at = NULL", "credentials_expiry_guard"),
        ("expiry_warned_days = 10", "credentials_expiry_guard"),  # de 3 a 10: hacia atrás
        ("expiry_warned_days = NULL", "credentials_expiry_guard"),
        ("expiry_warned_days = 1", "credentials_expiry_warned_ck"),  # solo 10 o 3
    ],
)
def test_postgres_keeps_a_read_expiry_and_never_moves_a_warning_back(
    crm: SimpleNamespace, meta: Any, sql: str, constraint: str
) -> None:
    meta.graph.debug = {"expires_at": at(2)}
    made = connect(crm, "MESSENGER")[1]
    tasks.token_expiry()
    with pytest.raises(Exception) as caught, tenant_scope(ctx(crm.a)):
        with connection.cursor() as cursor:  # como crm_app
            cursor.execute(f"UPDATE credentials SET {sql} WHERE id = %s", [made.pk])  # noqa: S608
    assert caught.value.__cause__.diag.constraint_name == constraint  # type: ignore[union-attr]


@pytest.mark.parametrize(
    "sql, error",
    [
        ("expires_at = now()", "credentials_expiry_ck"),  # una fecha sin leerla
        ("expiry_warned_days = 10", "credentials_expiry_warned_ck"),  # un aviso sin fecha
        ("last_four = 'ZZZZ'", "permission"),  # solo las columnas concedidas
        ("owner_id = gen_random_uuid()", "permission"),
    ],
)
def test_postgres_refuses_an_expiry_or_warning_without_its_reading(
    crm: SimpleNamespace, meta: Any, settings: Any, sql: str, error: str
) -> None:
    settings.META_APP_ID = ""
    made = connect(crm, "MESSENGER")[1]
    with pytest.raises(Exception) as caught, tenant_scope(ctx(crm.a)):
        with connection.cursor() as cursor:
            cursor.execute(f"UPDATE credentials SET {sql} WHERE id = %s", [made.pk])  # noqa: S608
    cause = caught.value.__cause__
    if error == "permission":
        assert isinstance(cause, psycopg.errors.InsufficientPrivilege)
    else:
        assert cause.diag.constraint_name == error  # type: ignore[union-attr]


def test_the_discovery_returns_only_organizations_with_something_due(
    crm: SimpleNamespace, meta: Any, settings: Any
) -> None:
    def due(unknown: bool) -> list[UUID]:
        with connection.cursor() as cursor:  # sin tenant: solo ids de organización
            cursor.execute("SELECT public.credentials_expiry_due(%s, 50)", [unknown])
            return [UUID(str(row[0])) for row in cursor.fetchall()]

    meta.graph.debug = {"expires_at": at(11)}
    connect(crm, "MESSENGER")
    assert due(True) == [] and due(False) == []
    settings.META_APP_ID = ""
    meta.graph.pages = {**meta.graph.pages, "100000000000777": None}
    connect(crm, "MESSENGER", org="b", page_id="100000000000777")
    assert due(True) == [crm.b] and due(False) == []
    with connection.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM credentials")
        assert cursor.fetchone() == (0,)  # crm_app sin tenant no ve credenciales

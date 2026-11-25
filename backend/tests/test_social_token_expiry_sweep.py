"""F7b-12c (#1103): la pasada de caducidad sin inanición, con espera por credencial y con tope de
tiempo. Cada filtro de `credentials_expiry_due` (SECURITY DEFINER, como `crm_app` sin tenant) y de
`expiry._targets` (en el scope de la organización) por separado. Meta, simulada; nunca la real."""

import re
from collections import Counter
from time import monotonic
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest
from django.db import connection

from apps.conversations import expiry, tasks
from apps.conversations.models import WARN_DAYS, Credential
from core.tenancy.resolution import ACCESSIBLE_STATUSES
from core.tenancy.scope import tenant_scope
from tests.meta_sim.social import APP_ID
from tests.test_memberships import ctx
from tests.test_social_accounts import connect
from tests.test_social_token_expiry import DB, at, cred, crm, cut, meta, page_meta, real_stack

__all__ = ["crm", "meta", "page_meta", "real_stack"]
pytestmark = pytest.mark.usefixtures("tenant_db")
SIGNATURE = "public.credentials_expiry_due(boolean, integer)"


def due(unknown: bool) -> list[UUID]:
    with connection.cursor() as cursor:  # sin tenant: solo ids de organización
        cursor.execute("SELECT public.credentials_expiry_due(%s, 50)", [unknown])
        return [UUID(str(row[0])) for row in cursor.fetchall()]


def targets(org: UUID, unknown: bool) -> list[Any]:
    with tenant_scope(ctx(org)) as tenant:
        wanted = expiry.askable() if unknown else expiry.warning_due()
        return [c.pk for _, c in expiry._targets(tenant, wanted, "created_at")]


def page(crm: SimpleNamespace, n: int, org: str = "a", known: bool = True) -> Credential:
    """Otra página de Messenger conectada; `known=False`, sin app: la caducidad no se sabe."""
    pid = f"10000000000{n:04d}"
    crm.meta.graph.pages = {**crm.meta.graph.pages, pid: None}
    crm.settings.META_APP_ID = APP_ID if known else ""
    made = connect(crm, "MESSENGER", org=org, page_id=pid, name=f"Tienda {n}")[1]
    crm.settings.META_APP_ID = APP_ID
    return made


@pytest.fixture
def world(crm: SimpleNamespace, meta: Any, settings: Any) -> SimpleNamespace:
    crm.meta, crm.settings = meta, settings
    return crm


def asked(world: SimpleNamespace) -> int:
    return sum(c["path"].endswith("/debug_token") for c in world.meta.graph.calls)


CASES = {  # caso: (caducidad leída en días o None, SQL del migrador, con desconocidas, ¿toca?)
    "first_warning": (9, [], False, True),
    "far": (11, [], False, False),
    "warned_10_not_yet_3": (9, ["UPDATE credentials SET expiry_warned_days = 10"], False, False),
    "warned_10_at_3": (2, ["UPDATE credentials SET expiry_warned_days = 10"], False, True),
    "warned_3": (2, ["UPDATE credentials SET expiry_warned_days = 3"], False, False),
    "unknown_hourly": (None, [], False, False),
    "unknown_daily": (None, [], True, True),
    "known_daily": (40, [], True, False),
    "tried_an_hour_ago": (None, ["UPDATE credentials SET expiry_attempt_at = "
                                 "now() - interval '1 hour'"], True, False),
    "tried_a_day_ago": (None, ["UPDATE credentials SET expiry_attempt_at = "
                               "now() - interval '23 hours 1 minute'"], True, True),
    "tried_in_the_future": (None, ["UPDATE credentials SET expiry_attempt_at = "
                                   "now() + interval '1 hour'"], True, True),
    "revoked": (2, ["UPDATE credentials SET revoked_at = now(), ciphertext = NULL"], False, False),
    "deleted": (2, ["UPDATE channel_accounts SET deleted_at = now(), status = 'DISCONNECTED'"],
                False, False),
    "error": (2, ["UPDATE channel_accounts SET status = 'ERROR'"], False, False),
    "unlinked": (2, ["UPDATE channel_accounts SET credential_id = NULL"], False, False),
    "suspended": (2, ["UPDATE organizations SET status = 'SUSPENDED'"], False, False),
    "trial": (2, ["UPDATE organizations SET status = 'TRIAL'"], False, True),
}  # fmt: skip


@pytest.mark.parametrize("case", CASES)
def test_each_filter_of_the_discovery_and_of_the_targets(
    world: SimpleNamespace, migrator: DB, case: str
) -> None:
    days, sql, unknown, wanted = CASES[case]
    world.meta.graph.debug = {"expires_at": at(days or 0)}
    made = page(world, 1, known=days is not None)
    for statement in sql:
        table = "organizations" if "organizations" in statement else statement.split()[1]
        key = {"credentials": "id", "channel_accounts": "credential_id"}.get(table, "id")
        value = world.a if table == "organizations" else made.pk
        migrator.execute(f"{statement} WHERE {key} = %s", [value])  # noqa: S608
    assert due(unknown) == ([world.a] if wanted else [])
    assert targets(world.a, unknown) == ([made.pk] if wanted else [])


def test_the_discovery_keeps_its_guarantees_and_matches_the_python_rules(migrator: DB) -> None:
    """Propietario, `search_path`, permisos, solo ids; y los estados, la espera y los días que la
    función escribe en SQL son los de Python (no pueden divergir)."""
    row = migrator.execute(
        "SELECT pg_get_userbyid(p.proowner), p.prosecdef, p.proconfig, p.proacl::text[], "
        "pg_get_functiondef(p.oid), pg_get_function_result(p.oid) FROM pg_proc p "
        "WHERE p.oid = %s::regprocedure",
        [SIGNATURE],
    ).fetchone()
    assert row is not None
    owner, definer, config, acl, body, result = row
    assert (owner, definer, result) == ("crm_migrator", True, "SETOF uuid")
    assert config == ["search_path=pg_catalog, public, pg_temp"]
    assert sorted(a.split("=")[0] for a in acl) == ["crm_app", "crm_migrator"]

    def listed(column: str) -> set[str]:
        found = re.search(column + r"\.status IN \(([^)]*)\)", body)
        return set(re.findall(r"'(\w+)'", found[1] if found else ""))

    assert listed("o") == ACCESSIBLE_STATUSES and listed("a") == set(expiry.CHECKED)
    assert f"interval '{expiry.WAIT.seconds // 3600} hours'" in body
    assert {int(d) for d in re.findall(r"interval '(\d+) days'", body)} == set(WARN_DAYS)
    assert "LIMIT greatest(0, least(coalesce(p_limit, 0), 500))" in body


def test_a_warned_credential_never_starves_a_pending_one(
    world: SimpleNamespace, migrator: DB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Antes, una credencial avisada a 10 días (más urgente) ocupaba el lote y su organización el
    tope: la pendiente de otra cuenta u organización no tenía turno."""
    monkeypatch.setattr(expiry, "BATCH", 1)
    monkeypatch.setattr(expiry, "MAX_ORGS", 1)
    world.meta.graph.debug = {"expires_at": at(5)}
    warned_a = page(world, 1)
    migrator.execute("UPDATE credentials SET expiry_warned_days = 10 WHERE id = %s", [warned_a.pk])
    world.meta.graph.debug = {"expires_at": at(9)}
    fresh_a = page(world, 2)
    fresh_b = page(world, 3, org="b")
    assert tasks.token_expiry() == {"WARNED": 1}
    assert tasks.token_expiry() == {"WARNED": 1}
    assert tasks.token_expiry() == {}
    assert cred(world.a, fresh_a.pk).expiry_warned_days == 10
    assert cred(world.b, fresh_b.pk).expiry_warned_days == 10
    assert cred(world.a, warned_a.pk).expiry_warned_days == 10  # su aviso de 3 días, a su hora


def test_an_unanswered_read_is_asked_once_a_day_and_gives_its_turn_to_others(
    world: SimpleNamespace, migrator: DB, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(expiry, "MAX_ORGS", 1)
    made = {world.a: page(world, 1, known=False), world.b: page(world, 2, org="b", known=False)}
    world.meta.graph.debug = None  # Meta no la contesta
    assert tasks.token_expiry(backfill=True) == {"UNKNOWN": 1}
    tried = [org for org, c in made.items() if cred(org, c.pk).expiry_attempt_at is not None]
    assert len(tried) == 1
    (other,) = set(made) - set(tried)
    world.meta.graph.debug = {"expires_at": at(40)}
    assert tasks.token_expiry(backfill=True) == {"READ": 1}  # ahora la otra organización
    assert cred(other, made[other].pk).expiry_checked_at is not None
    before = asked(world)
    assert tasks.token_expiry(backfill=True) == {} and asked(world) == before  # espera un día
    migrator.execute("UPDATE credentials SET expiry_attempt_at = expiry_attempt_at - "
                     "interval '23 hours' WHERE id = %s", [made[tried[0]].pk])  # fmt: skip
    assert tasks.token_expiry(backfill=True) == {"READ": 1} and asked(world) == before + 1


def test_the_oldest_turn_goes_first(
    world: SimpleNamespace, migrator: DB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sin intento primero, después el intento más viejo: entre organizaciones (la función) y
    entre credenciales de una organización (`_backfill`)."""
    made = {world.a: page(world, 1, known=False), world.b: page(world, 2, org="b", known=False)}
    low, high = sorted(made)

    def tried(org: UUID, ago: str | None) -> None:
        sql = "UPDATE credentials SET expiry_attempt_at = now() - %s::interval WHERE id = %s"
        migrator.execute(sql, [ago, made[org].pk])

    tried(low, "2 days")
    assert due(True) == [high, low]  # sin intento, aunque su id vaya después
    tried(high, "3 days")
    assert due(True) == [high, low]  # el intento más viejo
    tried(high, "25 hours")
    assert due(True) == [low, high]
    monkeypatch.setattr(expiry, "BATCH", 1)
    newer = page(world, 3, known=False)  # sin intento: antes que la 1, más vieja e intentada
    tried(world.a, "25 hours")
    tried(world.b, "2 days")
    assert due(True) == [world.a, world.b]  # una sin preguntar adelanta a su organización
    world.meta.graph.debug = {"expires_at": at(40)}
    expiry._backfill(world.a, Counter(), monotonic() + 60)
    assert cred(world.a, newer.pk).expiry_checked_at is not None
    assert cred(world.a, made[world.a].pk).expiry_checked_at is None


@pytest.mark.parametrize("failure", ["meta_down", "worker_error"])
def test_a_failed_read_keeps_its_attempt_and_is_not_retried_until_the_wait_ends(
    world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    """La pregunta se reclama antes de la llamada: si Meta cae o el worker falla, una caída de
    Meta cuesta un día por credencial, nunca una pregunta por pasada."""
    made = page(world, 1, known=False)
    if failure == "meta_down":
        real = world.meta.graph.handle
        world.meta.graph.handle = lambda m, t, *a: cut() if "debug_token" in t else real(m, t, *a)
    else:
        monkeypatch.setattr(expiry, "read", lambda *a, **k: 1 / 0)
    outcome = {"meta_down": {"UNKNOWN": 1}, "worker_error": {"ERROR": 1}}[failure]
    assert tasks.token_expiry(backfill=True) == outcome
    attempt = cred(world.a, made.pk).expiry_attempt_at
    assert attempt is not None
    before = asked(world)
    assert tasks.token_expiry(backfill=True) == {} and tasks.token_expiry() == {}
    assert asked(world) == before and cred(world.a, made.pk).expiry_attempt_at == attempt


def test_a_read_claimed_by_another_pass_is_not_asked_again(
    world: SimpleNamespace, migrator: DB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dos pasadas a la vez: las dos la eligen; la que llega tarde al `UPDATE` condicional no
    pregunta (el mismo `UPDATE` espera el bloqueo de la fila y vuelve a evaluar la espera)."""
    made, real = page(world, 1, known=False), expiry._targets

    def raced(*args: Any) -> Any:
        found = real(*args)
        migrator.execute("UPDATE credentials SET expiry_attempt_at = now() WHERE id = %s",
                         [made.pk])  # fmt: skip
        return found

    monkeypatch.setattr(expiry, "_targets", raced)
    before = asked(world)
    assert tasks.token_expiry(backfill=True) == {} and asked(world) == before


def test_the_daily_pass_stops_at_its_time_budget_and_keeps_the_turn(
    world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    made = [page(world, n, known=False) for n in (1, 2, 3)]
    clock, real = [0.0], expiry.read

    def slow(*args: Any, **ids: str) -> Any:
        clock[0] += 70  # cada pregunta a Meta, 70 s
        return real(*args, **ids)

    monkeypatch.setattr(expiry, "read", slow)
    monkeypatch.setattr(expiry, "monotonic", lambda: clock[0])
    world.meta.graph.debug = {"expires_at": at(40)}
    assert tasks.token_expiry(backfill=True) == {"READ": 2, "OUT_OF_TIME": 1}
    last = cred(world.a, made[2].pk)
    assert (last.expiry_checked_at, last.expiry_attempt_at) == (None, None)  # ni preguntada
    assert tasks.token_expiry(backfill=True) == {"READ": 1}


def test_the_hourly_pass_stops_between_organizations_never_between_claim_and_send(
    world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.meta.graph.debug = {"expires_at": at(2)}
    page(world, 1)
    page(world, 2, org="b")
    clock, real = [0.0], expiry._send

    def slow(*args: Any) -> None:
        clock[0] += 130
        real(*args)

    monkeypatch.setattr(expiry, "_send", slow)
    monkeypatch.setattr(expiry, "monotonic", lambda: clock[0])
    assert tasks.token_expiry() == {"WARNED": 1, "OUT_OF_TIME": 1}
    assert tasks.token_expiry() == {"WARNED": 1}


def test_targets_see_only_their_own_organization(world: SimpleNamespace) -> None:
    world.meta.graph.debug = {"expires_at": at(2)}
    made_b = page(world, 1, org="b")
    assert due(False) == [world.b]
    assert targets(world.a, False) == [] and targets(world.b, False) == [made_b.pk]


def test_the_daily_log_carries_the_organization_and_the_account(
    world: SimpleNamespace, caplog: pytest.LogCaptureFixture
) -> None:
    made = page(world, 1, known=False)
    world.meta.graph.debug = None
    caplog.clear()  # el de la conexión, sin app
    with caplog.at_level("INFO"):
        assert tasks.token_expiry(backfill=True) == {"UNKNOWN": 1}
    logged = [r.getMessage() for r in caplog.records if "token_expiry_unknown" in r.getMessage()]
    assert len(logged) == 1 and f"'organization_id': '{world.a}'" in logged[0]
    assert f"'channel_account_id': '{made.owner_id}'" in logged[0]

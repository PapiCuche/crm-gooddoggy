"""F8-03b: los comandos de la cuenta del proveedor de IA (`apps.ai_gateway.accounts`) y la prueba
de conexión (`apps.ai_gateway.probe`) contra la API de Anthropic falsa (`tests.ai_sim`), sin red:
`ai.manage` antes de nada, auditoría sin la clave, ningún evento de outbox, el código cerrado de la
prueba y nunca el cuerpo del proveedor. PostgreSQL real con `crm_app`. Claves ficticias."""

import logging
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import psycopg
import pytest
from django.test import override_settings
from urllib3.exceptions import ProtocolError

from apps.access.selectors import AccessDenied
from apps.ai_gateway import accounts, keys
from apps.ai_gateway.probe import Probe
from core import sealed
from core.tenancy.scope import tenant_scope
from tests import ai_sim
from tests.ai_sim import BODY_MARK, TEST_KEY, FakeAnthropic
from tests.test_authorization import give
from tests.test_contacts_api import crm, real_stack
from tests.test_memberships import ctx

__all__ = ["crm", "real_stack"]  # los fixtures, para pytest
pytestmark = pytest.mark.usefixtures("tenant_db")
DB = psycopg.Connection[Any]
OTHER_KEY = "fake-anthropic-key-not-real-0002"  # ficticia
ANTHROPIC = "ANTHROPIC"


@pytest.fixture
def fake(crm: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> FakeAnthropic:
    """La API falsa servida por `core.http` y `ai.manage` para luis (de B); ana es Owner de A."""
    give(crm.b, crm.m_luis, {"ai.manage": None})
    return ai_sim.install(monkeypatch, FakeAnthropic())


def run(org: UUID, user: Any, command: Any, **fields: Any) -> Any:
    with tenant_scope(ctx(org, user)) as tenant:
        return command(tenant, provider=ANTHROPIC, **fields)


def audit(migrator: DB) -> list[tuple[Any, ...]]:
    return migrator.execute(
        "SELECT action, result, changes, metadata FROM audit_logs"
        " WHERE entity_type = 'ai_provider_account' ORDER BY occurred_at, id"
    ).fetchall()


def everything(migrator: DB) -> str:
    """La auditoría, el outbox y las tablas de la IA, en texto."""
    tables = ("audit_logs", "outbox_events", "ai_provider_accounts", "ai_provider_keys")
    return str([migrator.execute(f"SELECT t::text FROM {t} t").fetchall() for t in tables])  # noqa: S608


def test_create_check_rotate_and_disable_are_audited_without_the_key(
    crm: SimpleNamespace, fake: FakeAnthropic, migrator: DB, caplog: pytest.LogCaptureFixture
) -> None:
    outbox = migrator.execute("SELECT count(*) FROM outbox_events").fetchone()
    with caplog.at_level(logging.DEBUG):
        made = run(crm.a, crm.ana, accounts.create, api_key=TEST_KEY)
        assert (made.status, made.key_last_four) == ("ACTIVE", TEST_KEY[-4:])
        assert run(crm.a, crm.ana, accounts.check) == Probe.OK
        [call] = fake.calls  # la llamada más barata, al host fijo, con la clave en la cabecera
        assert (call["method"], call["path"], call["query"]) == (
            "GET",
            "/v1/models",
            {"limit": ["1"]},
        )
        assert call["headers"] == {"x-api-key": TEST_KEY, "anthropic-version": "2026-10-21"}
        assert (fake.retries[0].total, fake.retries[0].redirect) == (0, False)  # sin reintentos
        assert run(crm.a, crm.ana, accounts.rotate, api_key=OTHER_KEY).pk == made.pk
        assert run(crm.a, crm.ana, accounts.check) == Probe.CREDENTIALS_REJECTED  # la nueva
        fake.key = OTHER_KEY
        assert run(crm.a, crm.ana, accounts.check) == Probe.OK
        assert run(crm.a, crm.ana, accounts.disable).status == "DISABLED"
        with pytest.raises(accounts.AccountNotFound):
            run(crm.a, crm.ana, accounts.check)  # desactivada: nada que probar
        assert len(fake.calls) == 3
        again = run(crm.a, crm.ana, accounts.create, api_key=TEST_KEY)  # reactiva la misma
    assert again.pk == made.pk and again.status == "ACTIVE"
    rows = audit(migrator)
    assert [(action, result) for action, result, *_ in rows] == [
        ("ai.account.created", "SUCCESS"), ("ai.account.tested", "SUCCESS"),
        ("ai.account.key_rotated", "SUCCESS"), ("ai.account.tested", "FAILED"),
        ("ai.account.tested", "SUCCESS"), ("ai.account.disabled", "SUCCESS"),
        ("ai.account.created", "SUCCESS"),
    ]  # fmt: skip
    assert rows[0][2:] == ({"status": [None, "ACTIVE"]}, {"provider": ANTHROPIC})
    assert rows[3][3] == {"provider": ANTHROPIC, "result": "CREDENTIALS_REJECTED"}
    assert rows[5][2] == {"status": ["ACTIVE", "DISABLED"]}
    assert rows[6][2] == {"status": ["DISABLED", "ACTIVE"]}
    assert migrator.execute("SELECT count(*) FROM outbox_events").fetchone() == outbox
    seen = caplog.text + everything(migrator)
    assert TEST_KEY not in seen and OTHER_KEY not in seen and BODY_MARK not in seen


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        ("unauthorized", Probe.CREDENTIALS_REJECTED),
        ("forbidden", Probe.CREDENTIALS_REJECTED),
        ("billing", Probe.PROVIDER_BILLING),
        ("spend_limit", Probe.PROVIDER_BILLING),
        ("tier_limit", Probe.PROVIDER_BILLING),
        ("rate_limit", Probe.PROVIDER_UNAVAILABLE),
        ("server", Probe.PROVIDER_UNAVAILABLE),
        ("overloaded", Probe.PROVIDER_UNAVAILABLE),
        ("bad_request", Probe.PROVIDER_UNAVAILABLE),  # un fallo nuestro: el log tiene el estado
        ("not_found", Probe.PROVIDER_UNAVAILABLE),
        ("garbage", Probe.PROVIDER_UNAVAILABLE),
        ("cut", Probe.PROVIDER_UNAVAILABLE),
        ("timeout", Probe.PROVIDER_UNAVAILABLE),
    ],
)
def test_every_provider_answer_becomes_a_closed_code_and_its_body_never_leaves(
    crm: SimpleNamespace,
    fake: FakeAnthropic,
    migrator: DB,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
    expected: Probe,
) -> None:
    run(crm.a, crm.ana, accounts.create, api_key=TEST_KEY)
    fake.outcome = outcome
    handle = fake.handle

    def noisy(method: str, url: str, headers: dict[str, str]) -> tuple[int, bytes]:
        try:
            return handle(method, url, headers)
        except ProtocolError:  # un error de red que, además, lleva las cabeceras
            raise ProtocolError(f"aborted {headers} {BODY_MARK}") from None

    monkeypatch.setattr(fake, "handle", noisy)
    with caplog.at_level(logging.DEBUG):
        assert run(crm.a, crm.ana, accounts.check) == expected
    assert [retry.total for retry in fake.retries] == [0]  # una llamada, sin esperar `retry-after`
    assert audit(migrator)[-1][:2] == ("ai.account.tested", "FAILED")
    assert audit(migrator)[-1][3] == {"provider": ANTHROPIC, "result": str(expected)}
    seen = caplog.text + everything(migrator)
    assert BODY_MARK not in seen and TEST_KEY not in seen


def test_without_ai_manage_nothing_is_read_or_written_and_the_provider_is_never_asked(
    crm: SimpleNamespace, fake: FakeAnthropic, migrator: DB
) -> None:
    run(crm.a, crm.ana, accounts.create, api_key=TEST_KEY)
    before = audit(migrator)
    for command, fields in (
        (accounts.create, {"api_key": OTHER_KEY}),
        (accounts.rotate, {"api_key": "x"}),  # el permiso va antes que el formato
        (accounts.disable, {}),
        (accounts.check, {}),
    ):
        with pytest.raises(AccessDenied):
            run(crm.a, crm.vera, command, **fields)  # vendedora: sin `ai.manage`
    assert fake.calls == [] and audit(migrator) == before
    with tenant_scope(ctx(crm.a)) as tenant:
        assert keys.open_key(tenant, ANTHROPIC) == TEST_KEY  # nada cambió


def test_each_command_refuses_the_wrong_state_and_stays_inside_its_organization(
    crm: SimpleNamespace, fake: FakeAnthropic, migrator: DB, caplog: pytest.LogCaptureFixture
) -> None:
    with pytest.raises(accounts.AccountNotFound):
        run(crm.a, crm.ana, accounts.rotate, api_key=TEST_KEY)
    with pytest.raises(accounts.AccountNotFound):
        run(crm.a, crm.ana, accounts.disable)
    bad = "fake anthropic key not real 0003"  # ficticia y mal formada
    with caplog.at_level(logging.DEBUG), pytest.raises(keys.MalformedKey) as malformed:
        run(crm.a, crm.ana, accounts.create, api_key=bad)
    assert bad not in str(malformed.value) + repr(malformed.value.args) + caplog.text
    run(crm.a, crm.ana, accounts.create, api_key=TEST_KEY)
    with pytest.raises(accounts.AccountExists):
        run(crm.a, crm.ana, accounts.create, api_key=OTHER_KEY)  # se rota, no se crea otra
    for command, fields in ((accounts.rotate, {"api_key": OTHER_KEY}), (accounts.disable, {})):
        with pytest.raises(accounts.AccountNotFound):
            run(crm.b, crm.luis, command, **fields)  # la cuenta de A no existe para B
    with pytest.raises(accounts.AccountNotFound):
        run(crm.b, crm.luis, accounts.check)
    assert fake.calls == []
    assert [row[0] for row in audit(migrator)] == ["ai.account.created"]
    with tenant_scope(ctx(crm.a)) as tenant:
        assert keys.open_key(tenant, ANTHROPIC) == TEST_KEY
    assert bad not in everything(migrator) and OTHER_KEY not in everything(migrator)


def test_a_concurrent_first_store_is_a_domain_error_without_the_key(
    crm: SimpleNamespace, fake: FakeAnthropic, migrator: DB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dos altas a la vez: la segunda no vio la cuenta de la primera y choca con la única por
    proveedor. Se simula haciendo que no vea la que ya existe."""
    run(crm.a, crm.ana, accounts.create, api_key=TEST_KEY)
    run(crm.a, crm.ana, accounts.disable)
    monkeypatch.setattr(keys, "_locked", lambda alias, provider: None)
    with pytest.raises(keys.AccountConflict) as conflict:
        run(crm.a, crm.ana, accounts.create, api_key=OTHER_KEY)
    assert OTHER_KEY not in str(conflict.value) + repr(conflict.value.__context__)
    assert [row[0] for row in audit(migrator)] == ["ai.account.created", "ai.account.disabled"]
    with tenant_scope(ctx(crm.a)) as tenant, pytest.raises(sealed.SealError):
        keys.open_key(tenant, ANTHROPIC)  # nada se guardó
    assert OTHER_KEY not in everything(migrator)


def test_the_provider_host_is_fixed_and_allowlisted_and_nothing_else_is_reached(
    crm: SimpleNamespace, fake: FakeAnthropic, settings: Any
) -> None:
    assert settings.AI_PROVIDER_HOSTS == {ANTHROPIC: ai_sim.HOST}
    assert ai_sim.HOST in settings.HTTP_ALLOWED_HOSTS
    run(crm.a, crm.ana, accounts.create, api_key=TEST_KEY)
    with override_settings(HTTP_ALLOWED_HOSTS=[]):  # fuera de la allowlist: no sale nada
        assert run(crm.a, crm.ana, accounts.check) == Probe.PROVIDER_UNAVAILABLE
    assert fake.calls == []

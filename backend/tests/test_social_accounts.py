"""F7b-04a: conectar una página de Messenger o una cuenta de Instagram (`accounts.connect_social`,
ADR-035 §3) contra la Graph API de la página simulada (`tests/meta_sim/page_graph.py`), servida
por `core.http` con su política real. PostgreSQL real con `crm_app`."""

import logging
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import psycopg
import pytest

from apps.access.selectors import AccessDenied
from apps.conversations import accounts, credentials, graph
from apps.conversations.models import ChannelAccount, Credential
from core.tenancy.scope import tenant_scope
from tests.meta_sim import transport
from tests.meta_sim.graph import TEST_TOKEN, Cut
from tests.meta_sim.page_graph import FakePageGraph
from tests.meta_sim.social import IG_ACCOUNT_ID, PAGE_ID
from tests.test_authorization import give
from tests.test_channel_accounts import everything
from tests.test_contacts_api import crm, real_stack
from tests.test_memberships import ctx

__all__ = ["crm", "real_stack"]  # los fixtures, para pytest
pytestmark = pytest.mark.usefixtures("tenant_db")
DB = psycopg.Connection[Any]
OTHER = "100000000000999"


class Down(FakePageGraph):
    def handle(self, *args: Any) -> Any:
        raise Cut


class Elsewhere(FakePageGraph):
    """Contesta por otra página que la pedida: lo que diga la respuesta no basta."""

    def handle(self, method: str, target: str, *args: Any) -> Any:
        status, headers, body = super().handle(method, target, *args)
        return status, headers, body.replace(PAGE_ID.encode(), OTHER.encode())


@pytest.fixture
def meta(crm: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, settings: Any) -> FakePageGraph:
    settings.META_ENABLED, settings.HTTP_ALLOWED_HOSTS = True, [transport.GRAPH_HOST]
    fake = FakePageGraph()
    transport.install(monkeypatch, fake)
    give(crm.b, crm.m_luis, {"channels.manage": None})
    return fake


def connect(
    crm: SimpleNamespace, platform: str = "MESSENGER", org: str = "a", **over: Any
) -> tuple[ChannelAccount, Credential]:
    who: tuple[UUID, Any] = {"a": (crm.a, crm.ana), "b": (crm.b, crm.luis)}[org]
    wanted: dict[str, Any] = {"name": "Tienda", "page_id": PAGE_ID, "access_token": TEST_TOKEN}
    if platform == "INSTAGRAM":
        wanted["instagram_account_id"] = IG_ACCOUNT_ID
    with tenant_scope(ctx(*who)) as tenant:
        made: tuple[ChannelAccount, Credential] = accounts.connect_social(
            tenant, platform=platform, **wanted | over
        )
        return made


def stored(migrator: DB) -> tuple[Any, ...]:
    found = migrator.execute(
        "SELECT (SELECT count(*) FROM credentials), (SELECT count(*) FROM channel_accounts"
        " WHERE platform IN ('MESSENGER', 'INSTAGRAM'))"
    ).fetchone()
    assert found is not None
    return tuple(found)


@pytest.mark.parametrize("platform", ["MESSENGER", "INSTAGRAM"])
def test_connecting_checks_the_page_subscribes_it_and_stores_a_pending_account(
    crm: SimpleNamespace, meta: FakePageGraph, migrator: DB, caplog: pytest.LogCaptureFixture,
    platform: str,
) -> None:  # fmt: skip
    with caplog.at_level(logging.DEBUG):
        account, secret = connect(crm, platform)
    instagram = platform == "INSTAGRAM"
    assert (account.platform, account.status, account.status_detail) == (
        platform, "PENDING", "WEBHOOK_NOT_SEEN",
    )  # fmt: skip
    assert account.external_account_id == (IG_ACCOUNT_ID if instagram else PAGE_ID)
    assert account.external_parent_id == (PAGE_ID if instagram else None)
    assert account.display_identifier == ("" if instagram else "Página de prueba")
    assert account.credential_id == secret.pk and secret.owner_id == account.pk
    with tenant_scope(ctx(crm.a)):
        assert credentials.open_token(Credential.objects.get(pk=secret.pk)) == TEST_TOKEN
    assert [(c["method"], c["path"]) for c in meta.calls] == [
        ("GET", f"/v23.0/{PAGE_ID}"), ("POST", f"/v23.0/{PAGE_ID}/subscribed_apps"),
    ]  # fmt: skip
    assert "instagram_business_account" in meta.calls[0]["query"]["fields"][0]
    assert meta.subscribed[PAGE_ID] == graph.SUBSCRIBED_FIELDS.split(",")
    assert TEST_TOKEN not in everything(migrator) + caplog.text
    audited = "SELECT metadata->>'platform' FROM audit_logs WHERE action = %s"
    assert migrator.execute(audited, ["channel_account.connected"]).fetchall() == [(platform,)]


@pytest.mark.parametrize(
    "why", ["not_linked", "no_instagram", "token", "page", "elsewhere", "down", "off"]
)
def test_a_refused_connection_leaves_nothing_behind(
    crm: SimpleNamespace, meta: FakePageGraph, monkeypatch: pytest.MonkeyPatch, settings: Any,
    migrator: DB, why: str,
) -> None:  # fmt: skip
    over: dict[str, Any] = {}
    expected: type[Exception] = graph.GraphRejected
    if why == "not_linked":
        over["instagram_account_id"] = OTHER  # la página tiene otra cuenta de Instagram
    elif why == "no_instagram":
        meta.pages[PAGE_ID] = None
    elif why == "token":
        meta.token = "otro-token-de-la-api-falsa-0000"  # noqa: S105
    elif why == "page":
        over["page_id"] = OTHER  # una página que el token no gestiona
    elif why == "elsewhere":
        transport.install(monkeypatch, Elsewhere())
    elif why == "down":
        transport.install(monkeypatch, Down())
        expected = graph.GraphUnavailable
    else:
        settings.META_ENABLED, expected = False, graph.ChannelNotEnabled
    with pytest.raises(expected):
        connect(crm, "INSTAGRAM", **over)
    assert stored(migrator) == (0, 0) and meta.subscribed == {}


def test_a_failed_subscription_stores_nothing(
    crm: SimpleNamespace, meta: FakePageGraph, monkeypatch: pytest.MonkeyPatch, migrator: DB
) -> None:
    def refuse(token: str, page_id: str) -> None:
        raise graph.GraphUnavailable

    monkeypatch.setattr(graph, "subscribe", refuse)
    with pytest.raises(graph.GraphUnavailable):
        connect(crm)
    assert stored(migrator) == (0, 0)


def test_the_account_is_refused_with_one_error_whether_malformed_or_taken(
    crm: SimpleNamespace, meta: FakePageGraph, migrator: DB
) -> None:
    connect(crm)
    connect(crm, "INSTAGRAM")
    before = stored(migrator)
    cases = [("MESSENGER", "a", {"page_id": "no-es-un-id"}), ("MESSENGER", "a", {}),
             ("MESSENGER", "b", {}), ("INSTAGRAM", "b", {}),
             ("INSTAGRAM", "a", {"instagram_account_id": "x"})]  # fmt: skip
    for platform, org, over in cases:
        with pytest.raises(accounts.BadNumber) as caught:
            connect(crm, platform, org, **over)
        assert str(caught.value) == accounts.ACCOUNT_REFUSED, (platform, org, over)
        error = caught.value
        assert error.__cause__ is None and (error.__suppress_context__ or error.__context__ is None)
    assert stored(migrator) == before  # ninguna credencial de más


def test_without_channels_manage_meta_is_never_asked(
    crm: SimpleNamespace, meta: FakePageGraph, migrator: DB
) -> None:
    for user in (crm.vera, crm.sam, crm.bea):
        with tenant_scope(ctx(crm.a, user)) as tenant, pytest.raises(AccessDenied):
            accounts.connect_social(
                tenant, platform="MESSENGER", name="x", page_id=PAGE_ID, access_token=TEST_TOKEN
            )
    assert meta.calls == [] and stored(migrator) == (0, 0)


@pytest.mark.parametrize(
    "over",
    [
        {"platform": "WHATSAPP"}, {"platform": "SANDBOX"},
        {"platform": "MESSENGER", "instagram_account_id": IG_ACCOUNT_ID},
        {"platform": "INSTAGRAM", "instagram_account_id": None},
        {"access_token": "corto"}, {"name": " "}, {"name": "dos\nlíneas"},
    ],
)  # fmt: skip
def test_bad_arguments_never_reach_meta(
    crm: SimpleNamespace, meta: FakePageGraph, over: dict[str, Any]
) -> None:
    platform = over.pop("platform", "MESSENGER")
    with pytest.raises(ValueError):  # `BadNumber` es un `ValueError`
        connect(crm, platform, **over)
    assert meta.calls == []

"""F7-02c: conectar, rotar el token y desconectar una cuenta de WhatsApp
(`apps.conversations.accounts`, ADR-029 §2) y el cliente de la Graph API (`graph`). PostgreSQL real
con `crm_app`, la Graph API de Meta simulada (`tests/meta_sim`) servida por `core.http` con su
política real."""

import json
import logging
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from django.core.exceptions import ObjectDoesNotExist
from django.db import IntegrityError

from apps.access.selectors import AccessDenied
from apps.conversations import accounts, commands, credentials, graph, tasks
from apps.conversations.models import ChannelAccount, Credential
from core import http, sealed
from core.tenancy.scope import tenant_scope
from tests.meta_sim import transport
from tests.meta_sim.graph import TEST_TOKEN, Cut, FakeGraph
from tests.meta_sim.webhooks import PHONE_NUMBER_ID, WABA_ID
from tests.test_authorization import give
from tests.test_contacts_api import crm, real_stack
from tests.test_memberships import ctx

__all__ = ["crm", "real_stack"]  # los fixtures, para pytest
pytestmark = pytest.mark.usefixtures("tenant_db")
DB = psycopg.Connection[Any]
NEW_TOKEN = "meta-sim-rotated-access-token-not-real-0000"  # noqa: S105 — ficticio
SECOND = "100000000000003"


class Down(FakeGraph):
    """Meta no responde: la conexión se corta."""

    def handle(self, *args: Any) -> Any:
        raise Cut


@pytest.fixture
def meta(crm: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, settings: Any) -> FakeGraph:
    """El canal encendido, la Graph API falsa servida por `core.http` y `channels.manage` para
    ana (Owner de A) y para luis (de B)."""
    settings.META_ENABLED, settings.HTTP_ALLOWED_HOSTS = True, [transport.GRAPH_HOST]
    fake = FakeGraph(numbers={PHONE_NUMBER_ID: WABA_ID, SECOND: WABA_ID})
    transport.install(monkeypatch, fake)
    give(crm.b, crm.m_luis, {"channels.manage": None})
    return fake


def run(org: UUID, user: Any, command: Any, **fields: Any) -> Any:
    with tenant_scope(ctx(org, user)) as tenant:
        return command(tenant, **fields)


def connect(crm: SimpleNamespace, org: str = "a", **over: Any) -> tuple[ChannelAccount, Credential]:
    who: tuple[UUID, Any] = {"a": (crm.a, crm.ana), "b": (crm.b, crm.luis)}[org]
    wanted = {"name": "Ventas", "phone_number_id": PHONE_NUMBER_ID, "waba_id": WABA_ID}
    wanted |= {"access_token": TEST_TOKEN} | over
    made: tuple[ChannelAccount, Credential] = run(
        who[0], who[1], accounts.connect_whatsapp, **wanted
    )
    return made


def reload(org: UUID, model: Any, pk: UUID) -> Any:
    with tenant_scope(ctx(org)):
        return model.objects.get(pk=pk)


def everything(migrator: DB) -> str:
    """Lo que la base guarda de la conexión, salvo el cifrado: la auditoría y el outbox enteros y
    las filas de cuentas y credenciales."""
    queries = ("audit_logs", "outbox_events", "credentials", "channel_accounts")
    return "\n".join(
        str(migrator.execute(f"SELECT t::text FROM {table} t").fetchall())  # noqa: S608
        for table in queries
    )


def test_connecting_stores_a_pending_account_and_the_token_only_sealed(
    crm: SimpleNamespace, meta: FakeGraph, migrator: DB, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG):
        account, secret = connect(crm)
    assert (account.platform, account.status, account.status_detail) == (
        "WHATSAPP", "PENDING", "WEBHOOK_NOT_SEEN"  # nunca nace CONNECTED
    )  # fmt: skip
    assert (account.quality_rating, account.messaging_limit_tier) == ("GREEN", "TIER_1K")
    assert account.credential_id == secret.pk and account.last_healthcheck_at is not None
    assert (secret.last_four, secret.rotated_at, secret.revoked_at) == (TEST_TOKEN[-4:], None, None)
    assert secret.key_version == sealed.current() and secret.owner_id == account.pk
    box = bytes(reload(crm.a, Credential, secret.pk).ciphertext)
    assert TEST_TOKEN.encode() not in box and not sealed.stale(box)
    assert credentials.open_token(reload(crm.a, Credential, secret.pk)) == TEST_TOKEN
    assert [c["path"] for c in meta.calls] == [f"/v23.0/{WABA_ID}/phone_numbers"]
    seen = everything(migrator) + caplog.text
    assert TEST_TOKEN not in seen and secret.last_four in seen
    assert "channel_account.connected" in seen  # y sí quedó el hecho, en la auditoría y el outbox


def test_the_number_is_refused_with_one_error_whether_malformed_or_taken(
    crm: SimpleNamespace, meta: FakeGraph, migrator: DB
) -> None:
    connect(crm)
    before = everything(migrator).count("credentials")
    outcomes = []
    for org, number in (("a", "no-es-un-numero"), ("a", PHONE_NUMBER_ID), ("b", PHONE_NUMBER_ID)):
        with pytest.raises(accounts.BadNumber) as caught:
            connect(crm, org, phone_number_id=number)
        outcomes.append(
            (
                str(caught.value),
                caught.value.__cause__,
                caught.value.__suppress_context__ or caught.value.__context__ is None,
            )
        )
    assert len({o[0] for o in outcomes}) == 1 and outcomes[0][0] == accounts.NUMBER_REFUSED
    assert all(o[1] is None and o[2] for o in outcomes)  # sin el IntegrityError a la vista
    assert "channel_accounts_external_uq" not in outcomes[0][0] and "Key (" not in outcomes[0][0]
    assert everything(migrator).count("credentials") == before  # y la credencial no quedó


@pytest.mark.parametrize("why", ["token", "waba", "down", "off"])
def test_a_refused_connection_leaves_nothing_behind(
    crm: SimpleNamespace, meta: FakeGraph, monkeypatch: pytest.MonkeyPatch, settings: Any,
    migrator: DB, why: str,
) -> None:  # fmt: skip
    over: dict[str, Any] = {}
    expected: type[Exception] = graph.GraphRejected
    if why == "token":
        meta.token = "otro-token-de-la-api-falsa-0000"  # noqa: S105
    elif why == "waba":
        over["waba_id"] = "100000000000099"  # el número no es de ese WABA
    elif why == "down":
        transport.install(monkeypatch, Down())
        expected = graph.GraphUnavailable
    else:
        settings.META_ENABLED, expected = False, graph.ChannelNotEnabled
    with pytest.raises(expected):
        connect(crm, **over)
    for table in ("credentials", "channel_accounts WHERE platform = 'WHATSAPP'"):
        assert migrator.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,)  # noqa: S608


def test_without_channels_manage_nothing_is_read_and_meta_is_never_asked(
    crm: SimpleNamespace, meta: FakeGraph
) -> None:
    account, _ = connect(crm)
    meta.requests.clear()
    for user in (crm.vera, crm.sam, crm.bea):
        with pytest.raises(AccessDenied):
            run(crm.a, user, accounts.connect_whatsapp, name="x", phone_number_id=SECOND,
                waba_id=WABA_ID, access_token=NEW_TOKEN)  # fmt: skip
        with pytest.raises(AccessDenied):
            run(crm.a, user, accounts.rotate_token, channel_account_id=account.pk,
                access_token=NEW_TOKEN)  # fmt: skip
        with pytest.raises(AccessDenied):
            run(crm.a, user, accounts.disconnect, channel_account_id=account.pk)
    assert meta.requests == [] and reload(crm.a, ChannelAccount, account.pk).deleted_at is None


def test_rotating_replaces_the_token_and_wipes_the_old_one(
    crm: SimpleNamespace, meta: FakeGraph, migrator: DB
) -> None:
    account, old = connect(crm)
    with tenant_scope(ctx(crm.a)):
        ChannelAccount.objects.filter(pk=account.pk).update(
            status_detail="TOKEN_INVALID", status="ERROR"
        )
    meta.token = NEW_TOKEN
    rotated, new = run(crm.a, crm.ana, accounts.rotate_token, channel_account_id=account.pk,
                       access_token=NEW_TOKEN)  # fmt: skip
    assert rotated.credential_id == new.pk != old.pk and new.rotated_at is not None
    assert (rotated.status, rotated.status_detail, rotated.version) == ("CONNECTED", None, 2)
    gone = reload(crm.a, Credential, old.pk)
    assert (
        gone.ciphertext is None and gone.revoked_at is not None and gone.last_four == old.last_four
    )
    assert credentials.open_token(reload(crm.a, Credential, new.pk)) == NEW_TOKEN
    with pytest.raises(sealed.SealError):
        credentials.open_token(gone)  # el anterior ya no se puede abrir
    assert TEST_TOKEN not in everything(migrator) and NEW_TOKEN not in everything(migrator)
    meta.token = "tercer-token-de-la-api-falsa-000"  # noqa: S105  Meta rechaza el que llega
    with pytest.raises(graph.GraphRejected):
        run(crm.a, crm.ana, accounts.rotate_token, channel_account_id=account.pk,
            access_token=TEST_TOKEN)  # fmt: skip
    assert reload(crm.a, ChannelAccount, account.pk).credential_id == new.pk  # nada cambió


def test_rotating_an_account_in_error_audits_the_way_back_and_low_quality_degrades_it(
    crm: SimpleNamespace, meta: FakeGraph, migrator: DB
) -> None:
    """`ERROR` no se comprueba nunca: el token nuevo, ya verificado por Meta, saca a la cuenta."""
    account, _ = connect(crm)
    meta.token = NEW_TOKEN

    def broken(status: str = "ERROR", detail: str | None = "TOKEN_INVALID") -> None:
        with tenant_scope(ctx(crm.a)):
            ChannelAccount.objects.filter(pk=account.pk).update(status=status, status_detail=detail)

    def rotate() -> ChannelAccount:
        found = run(crm.a, crm.ana, accounts.rotate_token, channel_account_id=account.pk,
                    access_token=NEW_TOKEN)  # fmt: skip
        done: ChannelAccount = found[0]
        return done

    broken()
    assert (rotate().status, tasks.healthcheck_accounts()) == ("CONNECTED", {})  # reciente
    changes = migrator.execute(
        "SELECT changes::text FROM audit_logs WHERE action = 'channel_account.credentials_rotated'"
    ).fetchall()
    assert json.loads(changes[0][0])["status"] == ["ERROR", "CONNECTED"]
    meta.quality = "RED"
    broken()
    degraded = rotate()
    assert (degraded.status, degraded.status_detail) == ("DEGRADED", "QUALITY_LOW")
    broken("ERROR", None)  # un `ERROR` que no es de este token no se toca
    still = rotate()
    assert (still.status, still.status_detail) == ("ERROR", None)
    broken("CONNECTED", None)
    assert rotate().status == "CONNECTED"  # lo sano sigue igual


def test_an_account_that_is_not_a_live_whatsapp_account_of_the_organization_does_not_exist(
    crm: SimpleNamespace, meta: FakeGraph
) -> None:
    other, _ = connect(crm, "b", phone_number_id=SECOND)
    gone, _ = connect(crm)
    run(crm.a, crm.ana, accounts.disconnect, channel_account_id=gone.pk)
    sandbox = run(crm.a, crm.ana, commands.ensure_sandbox_account)
    meta.requests.clear()
    for target in (other.pk, gone.pk, sandbox.pk, uuid4()):  # ajena, desconectada, Sandbox, ninguna
        for command, extra in ((accounts.rotate_token, {"access_token": TEST_TOKEN}),
                               (accounts.disconnect, {})):  # fmt: skip
            with pytest.raises(ObjectDoesNotExist):
                run(crm.a, crm.ana, command, channel_account_id=target, **extra)
    assert meta.requests == []  # Meta no se entera de cuentas que no existen
    assert reload(crm.b, ChannelAccount, other.pk).deleted_at is None


def test_disconnecting_frees_the_number_and_reconnecting_is_a_new_account(
    crm: SimpleNamespace, meta: FakeGraph, migrator: DB
) -> None:
    account, secret = connect(crm)
    run(crm.a, crm.ana, accounts.disconnect, channel_account_id=account.pk)
    done = reload(crm.a, ChannelAccount, account.pk)
    assert (done.status, done.status_detail) == ("DISCONNECTED", "DISCONNECTED_BY_ADMIN")
    assert done.deleted_at is not None and done.deleted_by_user_id == crm.ana.pk
    assert reload(crm.a, Credential, secret.pk).ciphertext is None  # el token ya no está
    again, fresh = connect(crm, "b")  # el número se libera: otra organización lo registra
    assert again.pk != account.pk and fresh.pk != secret.pk and again.status == "PENDING"
    assert TEST_TOKEN not in everything(migrator)


def test_the_token_travels_only_in_the_authorization_header(
    crm: SimpleNamespace, meta: FakeGraph
) -> None:
    """Ni en la URL (que otros registran) ni en otra cabecera; y la URL lleva solo ids validados."""
    connect(crm)
    [call] = meta.calls
    assert call["path"] == f"/v23.0/{WABA_ID}/phone_numbers" and call["method"] == "GET"
    assert set(call["query"]) == {"fields", "limit"} and TEST_TOKEN not in str(call["query"])
    carrying = [name for name, value in call["headers"].items() if TEST_TOKEN in value]
    assert carrying == ["Authorization"]
    assert call["headers"]["Authorization"] == f"Bearer {TEST_TOKEN}"


def test_a_number_that_is_not_in_the_waba_of_the_token_is_rejected(
    crm: SimpleNamespace, meta: FakeGraph, migrator: DB
) -> None:
    """El WABA tiene números, pero no ese: nunca se acepta el primero de la lista."""
    with pytest.raises(graph.GraphRejected):
        connect(crm, phone_number_id="100000000000777")
    assert migrator.execute("SELECT count(*) FROM credentials").fetchone() == (0,)
    assert connect(crm, phone_number_id=SECOND)[0].external_account_id == SECOND


@pytest.mark.parametrize(
    ("status", "error"),
    [(400, graph.GraphRejected), (401, graph.GraphRejected), (403, graph.GraphRejected),
     (404, graph.GraphRejected), (429, graph.GraphUnavailable), (500, graph.GraphUnavailable),
     (503, graph.GraphUnavailable), (302, graph.GraphUnavailable)],
)  # fmt: skip
def test_what_meta_answers_is_rejected_only_when_the_token_or_the_number_is_wrong(
    meta: FakeGraph, monkeypatch: pytest.MonkeyPatch, status: int, error: type[Exception]
) -> None:
    import io

    import urllib3

    def reply(method: str, url: str, **kwargs: Any) -> Any:
        return urllib3.HTTPResponse(body=io.BytesIO(b"{}"), status=status, preload_content=True)

    monkeypatch.setattr(http, "request", reply)
    with pytest.raises(error):
        graph.phone_number(TEST_TOKEN, WABA_ID, PHONE_NUMBER_ID)


@pytest.mark.parametrize(
    ("waba", "number"), [("../1", PHONE_NUMBER_ID), (WABA_ID, "1/2"), ("", PHONE_NUMBER_ID),
                         (WABA_ID, "12 34 5")],
)  # fmt: skip
def test_the_graph_client_validates_the_ids_before_building_the_url(
    meta: FakeGraph, waba: str, number: str
) -> None:
    with pytest.raises(ValueError, match="identificador"):
        graph.phone_number(TEST_TOKEN, waba, number)
    assert meta.calls == []


def test_an_integrity_error_that_is_not_the_number_index_is_not_the_number_error(
    crm: SimpleNamespace, meta: FakeGraph, monkeypatch: pytest.MonkeyPatch, migrator: DB
) -> None:
    def boom(*args: Any, **kwargs: Any) -> None:
        raise IntegrityError("otra restricción")

    monkeypatch.setattr(accounts, "emit", boom)
    with pytest.raises(IntegrityError, match="otra restricción"):
        connect(crm)
    for table in ("credentials", "channel_accounts WHERE platform = 'WHATSAPP'"):
        assert migrator.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,)  # noqa: S608


def answer(monkeypatch: pytest.MonkeyPatch, body: bytes, status: int = 200) -> None:
    """Lo que `core.http` devolvería: una respuesta de Meta con ese estado y ese cuerpo."""
    import io

    import urllib3

    def reply(*args: Any, **kwargs: Any) -> Any:
        return urllib3.HTTPResponse(body=io.BytesIO(body), status=status, preload_content=True)

    monkeypatch.setattr(http, "request", reply)


@pytest.mark.parametrize(
    "body",
    [
        b"no es json",
        b"",
        b"\xff\xfe",
        b'{"data": null}',
        b'{"data": {"a": 1}}',
        b"[]",
        b"[" * 100_000,
    ],
)
def test_a_reply_that_is_not_what_meta_sends_is_unavailable_never_a_rejected_token(
    meta: FakeGraph, monkeypatch: pytest.MonkeyPatch, body: bytes
) -> None:
    answer(monkeypatch, body)
    with pytest.raises(graph.GraphUnavailable) as caught:
        graph.phone_number(TEST_TOKEN, WABA_ID, PHONE_NUMBER_ID)
    assert caught.value.__cause__ is None and TEST_TOKEN not in repr(caught.value)


@pytest.mark.parametrize(
    "body",
    [b'{"data": []}', b'{"data": [{"id": "100000000000009"}]}', b'{"data": [1, "x", null]}'],
)
def test_a_number_missing_from_the_reply_is_rejected(
    meta: FakeGraph, monkeypatch: pytest.MonkeyPatch, body: bytes
) -> None:
    answer(monkeypatch, body)
    with pytest.raises(graph.GraphRejected):
        graph.phone_number(TEST_TOKEN, WABA_ID, PHONE_NUMBER_ID)


def test_an_oversized_reply_and_a_blocked_destination_are_unavailable(
    crm: SimpleNamespace, meta: FakeGraph, monkeypatch: pytest.MonkeyPatch, settings: Any
) -> None:
    answer(monkeypatch, b'{"data": []}' + b" " * graph.BODY_MAX)
    with pytest.raises(graph.GraphUnavailable):
        graph.phone_number(TEST_TOKEN, WABA_ID, PHONE_NUMBER_ID)
    monkeypatch.undo()
    settings.META_ENABLED, settings.HTTP_ALLOWED_HOSTS = True, []  # nada permitido
    with pytest.raises(graph.GraphUnavailable):
        graph.phone_number(TEST_TOKEN, WABA_ID, PHONE_NUMBER_ID)


@pytest.mark.parametrize("token", ["corto", "con espacios " * 3, "a" * 20 + "\r\nX: y", "a" * 513])
def test_a_token_that_is_not_shaped_like_one_is_refused_before_meta(
    crm: SimpleNamespace, meta: FakeGraph, token: str
) -> None:
    with pytest.raises(ValueError, match="token") as caught:
        connect(crm, access_token=token)
    assert token not in str(caught.value)
    account, _ = connect(crm)
    meta.requests.clear()
    with pytest.raises(ValueError, match="token"):
        run(crm.a, crm.ana, accounts.rotate_token, channel_account_id=account.pk,
            access_token=token)  # fmt: skip
    assert meta.requests == []


def test_a_rotation_that_fails_after_meta_accepted_leaves_the_old_token_intact(
    crm: SimpleNamespace, meta: FakeGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    account, old = connect(crm)
    meta.token = NEW_TOKEN

    def boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("la base falló")

    monkeypatch.setattr(credentials, "store", boom)  # después de invalidar la anterior
    with pytest.raises(RuntimeError):
        run(crm.a, crm.ana, accounts.rotate_token, channel_account_id=account.pk,
            access_token=NEW_TOKEN)  # fmt: skip
    kept = reload(crm.a, Credential, old.pk)
    assert kept.revoked_at is None and kept.ciphertext is not None
    assert reload(crm.a, ChannelAccount, account.pk).credential_id == old.pk
    meta.token = TEST_TOKEN
    assert credentials.open_token(kept) == TEST_TOKEN

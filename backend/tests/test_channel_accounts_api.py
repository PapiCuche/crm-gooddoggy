"""F7-02b: `POST …/channels/whatsapp/`, `…/{id}/token/` y `DELETE …/{id}/` (ADR-029 §2). Middleware,
sesión, CSRF, motor de autorización y PostgreSQL con `crm_app`, la Graph API de Meta simulada; la
auditoría y el outbox, leídos con el rol dueño. Ninguna salida lleva el token."""

import logging
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from django.test import Client

from tests.meta_sim import transport
from tests.meta_sim.graph import TEST_TOKEN, FakeGraph
from tests.meta_sim.webhooks import PHONE_NUMBER_ID, WABA_ID
from tests.test_channel_credentials import (
    NEW_TOKEN,
    SECOND,
    Down,
    connect,
    crm,
    everything,
    meta,
    real_stack,
)
from tests.test_conversations_api import UNAUTHENTICATED
from tests.test_member_branch import DENIED
from tests.test_self_context import NOT_FOUND, reply, signed
from tests.test_team_writes import send

__all__ = ["crm", "meta", "real_stack"]  # los fixtures, para pytest
pytestmark = pytest.mark.usefixtures("tenant_db")
DB = psycopg.Connection[Any]
BASE = "/api/v1/o/org-a/channels/whatsapp/"
BODY = {"name": "Ventas", "phone_number_id": PHONE_NUMBER_ID, "waba_id": WABA_ID}
SHOWN = {
    "id", "name", "display_identifier", "phone_number_id", "waba_id", "status", "status_detail",
    "quality_rating", "messaging_limit_tier", "last_healthcheck_at", "token_last_four",
    "token_rotated_at", "token_expires_at", "version", "created_at",
}  # fmt: skip
TOKEN = {"access_token": TEST_TOKEN}


def post(client: Client, path: str = BASE, **over: Any) -> Any:
    body = {"access_token": TEST_TOKEN} | (BODY if path == BASE else {}) | over
    return send(client, "post", path, body)


def test_connecting_returns_the_pending_account_and_no_output_carries_the_token(
    crm: SimpleNamespace, meta: FakeGraph, migrator: DB, caplog: pytest.LogCaptureFixture
) -> None:
    ana = signed(crm.ana)
    with caplog.at_level(logging.DEBUG):
        made = post(ana)
        account = made.json()
        meta.token = NEW_TOKEN
        rotated = post(ana, f"{BASE}{account['id']}/token/", access_token=NEW_TOKEN)
        gone = send(ana, "delete", f"{BASE}{account['id']}/", None)
    assert made.status_code == 201 and set(account) == SHOWN
    assert (account["status"], account["status_detail"]) == ("PENDING", "WEBHOOK_NOT_SEEN")
    assert (account["phone_number_id"], account["waba_id"]) == (PHONE_NUMBER_ID, WABA_ID)
    assert account["token_last_four"] == TEST_TOKEN[-4:] and account["token_rotated_at"] is None
    assert rotated.status_code == 200 and rotated.json()["token_last_four"] == NEW_TOKEN[-4:]
    assert rotated.json()["token_rotated_at"] is not None and gone.status_code == 204
    outputs = b"".join(r.content for r in (made, rotated, gone)).decode() + caplog.text
    for token in (TEST_TOKEN, NEW_TOKEN):
        assert token not in outputs + everything(migrator)
    audited = migrator.execute("SELECT action FROM audit_logs ORDER BY occurred_at").fetchall()
    assert [a for (a,) in audited if a.startswith("channel_account")] == [
        "channel_account.connected", "channel_account.credentials_rotated",
        "channel_account.disconnected",
    ]  # fmt: skip


def test_the_permission_is_checked_before_the_body_the_lookup_and_meta(
    crm: SimpleNamespace, meta: FakeGraph
) -> None:
    account, _ = connect(crm)
    meta.requests.clear()
    paths = (
        (BASE, "post"),
        (f"{BASE}{account.pk}/token/", "post"),
        (f"{BASE}{account.pk}/", "delete"),
    )
    for who in (
        crm.vera,
        crm.sam,
        crm.bea,
    ):  # vendedora, supervisor y sin rol: sin `channels.manage`
        for path, method in paths:
            assert reply(send(signed(who), method, path, {"basura": 1})) == DENIED, (who, path)
    assert reply(send(signed(crm.vera), "post", f"{BASE}{uuid4()}/token/", {})) == DENIED
    for path, method in paths:
        assert reply(send(Client(enforce_csrf_checks=True), method, path, BODY)) == UNAUTHENTICATED
        other = path.replace("org-a", "org-b")  # ana no es miembro de B
        assert reply(send(signed(crm.ana), method, other, BODY)) == NOT_FOUND
    assert meta.requests == []  # a Meta nunca se le preguntó nada


def test_a_write_without_the_csrf_token_is_refused(crm: SimpleNamespace, meta: FakeGraph) -> None:
    ana = signed(crm.ana)
    response = ana.post(BASE, BODY | {"access_token": TEST_TOKEN}, "application/json")
    assert reply(response) == (403, b'{"code":"CSRF_FAILED"}') and meta.requests == []


@pytest.mark.parametrize(
    ("body", "fields"),
    [
        ({}, {"access_token", "name", "phone_number_id", "waba_id"}),
        (BODY | {"access_token": "corto"}, {"access_token"}),
        (BODY | {"access_token": 123456789012345678901234}, {"access_token"}),
        (BODY | {"access_token": TEST_TOKEN, "platform": "SANDBOX"}, {"platform"}),
        (BODY | {"access_token": TEST_TOKEN, "waba_id": "abc"}, {"waba_id"}),
        (BODY | {"access_token": TEST_TOKEN, "name": " " * 3}, {"name"}),
        (BODY | {"access_token": TEST_TOKEN, "name": "x" * 101}, {"name"}),
    ],
)
def test_the_body_is_closed_and_strict_and_an_error_never_repeats_the_token(
    crm: SimpleNamespace, meta: FakeGraph, body: dict[str, Any], fields: set[str]
) -> None:
    response = send(signed(crm.ana), "post", BASE, body)
    assert response.status_code == 400 and set(response.json()["fields"]) == fields
    assert TEST_TOKEN not in response.content.decode() and meta.requests == []


def test_a_malformed_taken_and_foreign_number_get_the_very_same_answer(
    crm: SimpleNamespace, meta: FakeGraph
) -> None:
    connect(crm)  # ya es de A
    ana, luis = signed(crm.ana), signed(crm.luis)
    answers = {
        "malformed": reply(post(ana, phone_number_id="no-es-un-numero")),
        "short": reply(post(ana, phone_number_id="123")),
        "mine": reply(post(ana)),
        "theirs": reply(send(luis, "post", BASE.replace("org-a", "org-b"), BODY | TOKEN)),
    }
    assert len(set(answers.values())) == 1, answers  # la misma respuesta, byte a byte
    status, content = answers["mine"]
    assert status == 400 and b"No se puede registrar ese n" in content
    assert b"external_uq" not in content and b"Key (" not in content and b"already" not in content


@pytest.mark.parametrize(
    "path", ["{other}/token/", "{sandbox}/token/", "{gone}/token/", "{none}/token/"]
)
def test_an_account_that_is_not_yours_is_not_found_the_same_way_whichever_it_is(
    crm: SimpleNamespace, meta: FakeGraph, path: str
) -> None:
    other, _ = connect(crm, "b", phone_number_id=SECOND)
    gone, _ = connect(crm)
    ana = signed(crm.ana)
    assert send(ana, "delete", f"{BASE}{gone.pk}/", None).status_code == 204
    from apps.conversations import commands
    from tests.test_channel_credentials import run

    sandbox = run(crm.a, crm.ana, commands.ensure_sandbox_account)
    ids = {"other": other.pk, "sandbox": sandbox.pk, "gone": gone.pk, "none": uuid4()}
    target = path.format(**ids)
    for method, tail in (("post", target), ("delete", target.replace("/token/", "/"))):
        response = send(
            ana, method, f"{BASE}{tail}", {"access_token": TEST_TOKEN} if method == "post" else None
        )
        assert reply(response) == NOT_FOUND, (method, tail)
    bad = send(ana, "post", f"{BASE}{ids['none']}/token/", {"access_token": "corto"})
    assert bad.status_code == 400  # el cuerpo se valida antes de buscar la cuenta


def test_what_meta_says_becomes_a_clear_error_and_nothing_is_stored(
    crm: SimpleNamespace, meta: FakeGraph, monkeypatch: pytest.MonkeyPatch, settings: Any,
    migrator: DB,
) -> None:  # fmt: skip
    ana = signed(crm.ana)
    meta.token = "otro-token-de-la-api-falsa-0000"  # noqa: S105
    rejected = post(ana)
    assert (rejected.status_code, rejected.json()["code"]) == (400, "CHANNEL_CREDENTIALS_REJECTED")
    transport.install(monkeypatch, Down())
    assert reply(post(ana))[0] == 503 and post(ana).json()["code"] == "CHANNEL_PROVIDER_UNAVAILABLE"
    settings.META_ENABLED = False
    off = post(ana)
    assert (off.status_code, off.json()["code"]) == (409, "CHANNEL_NOT_ENABLED")
    assert migrator.execute("SELECT count(*) FROM credentials").fetchone() == (0,)
    assert TEST_TOKEN not in b"".join(r.content for r in (rejected, off)).decode()

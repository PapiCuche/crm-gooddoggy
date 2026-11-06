"""F7-07a: `GET …/channels/` y `GET …/channels/{id}/` (ADR-029 §2, ADR-016). Middleware, sesión,
motor de autorización y PostgreSQL con `crm_app`; la Graph API de Meta, simulada. Solo lectura:
`channels.manage` antes de consultar, 404 único y ninguna salida con el token, su cifrado ni el id
de la credencial."""

import base64
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

from apps.conversations import accounts, commands, credentials, selectors
from apps.conversations.models import ChannelAccount
from core.tenancy.scope import tenant_scope
from tests.meta_sim.graph import TEST_TOKEN, FakeGraph
from tests.meta_sim.webhooks import WABA_ID
from tests.test_authorization import give
from tests.test_channel_accounts import NEW_TOKEN, SECOND, connect, crm, meta, real_stack, run
from tests.test_channel_accounts_api import signed
from tests.test_conversations_api import UNAUTHENTICATED
from tests.test_member_branch import DENIED
from tests.test_memberships import ctx
from tests.test_self_context import NOT_FOUND, reply
from tests.test_team_writes import send

__all__ = ["crm", "meta", "real_stack"]  # los fixtures, para pytest
pytestmark = pytest.mark.usefixtures("tenant_db")
DB = psycopg.Connection[Any]
LIST = "/api/v1/o/org-a/channels/"
SHOWN = {
    "id", "platform", "name", "status", "status_detail", "quality_rating", "messaging_limit_tier",
    "last_healthcheck_at", "external_account_id", "external_parent_id", "token_last_four",
    "version", "created_at", "updated_at",
}  # fmt: skip
FORBIDDEN = ("credential", "cipher", "fingerprint", "key_version", "revoked", "access_token")


def get(client: Client, path: str) -> Any:
    return client.get(path)


def make(org: UUID, count: int, *, first: int = 0) -> list[UUID]:
    """`count` cuentas de WhatsApp `PENDING` con su credencial viva, sin pasar por Meta."""
    made: list[UUID] = []
    for n in range(first, first + count):
        with tenant_scope(ctx(org)) as tenant:
            account = ChannelAccount.objects.create(
                platform="WHATSAPP", name=f"Cuenta {n}", status="PENDING",
                external_account_id=f"2000000000{n:04d}", external_parent_id=WABA_ID,
            )  # fmt: skip
            token = TEST_TOKEN[:-4] + f"{n:04d}"
            credential = credentials.store(tenant, account.pk, token)
            ChannelAccount.objects.filter(pk=account.pk).update(credential_id=credential.pk)
            made.append(account.pk)
    return made


def queries(client: Client, path: str) -> tuple[Any, list[str]]:
    with CaptureQueriesContext(connection) as captured:
        response = client.get(path)
    return response, [q["sql"] for q in captured]


def test_channels_manage_is_required_even_to_list_and_the_templates_decide(
    crm: SimpleNamespace, meta: FakeGraph
) -> None:
    account, _ = connect(crm)
    detail = f"{LIST}{account.pk}/"
    for who in (crm.vera, crm.sam, crm.bea):  # Vendedora, Supervisor y sin rol
        client = signed(who)
        for path in (LIST, detail, f"{LIST}{uuid4()}/"):  # exista o no, el mismo 403
            assert reply(get(client, path)) == DENIED, (who, path)
    assert get(signed(crm.ana), LIST).status_code == 200  # el Owner
    assert get(signed(crm.ana), detail).status_code == 200
    give(crm.a, crm.m["vera"], {"channels.manage": None})  # el permiso, no el rol, decide
    assert get(signed(crm.vera), LIST).status_code == 200
    assert reply(get(Client(), LIST)) == UNAUTHENTICATED
    assert reply(get(Client(), detail)) == UNAUTHENTICATED
    for path in (LIST, detail):  # ana no es miembro de B
        assert reply(get(signed(crm.ana), path.replace("org-a", "org-b"))) == NOT_FOUND


def test_nothing_is_read_before_the_permission_is_checked(
    crm: SimpleNamespace, meta: FakeGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    account, _ = connect(crm)

    def boom() -> Any:
        raise AssertionError("se consultó antes de comprobar el permiso")

    monkeypatch.setattr(selectors, "channel_accounts", boom)
    for path in (LIST, f"{LIST}{account.pk}/"):
        assert reply(get(signed(crm.vera), path)) == DENIED


def test_the_list_and_the_detail_show_exactly_the_public_fields_and_the_sandbox_account(
    crm: SimpleNamespace, meta: FakeGraph
) -> None:
    whatsapp, credential = connect(crm)
    sandbox = run(crm.a, crm.ana, commands.ensure_sandbox_account)
    ana = signed(crm.ana)
    page = get(ana, LIST).json()
    rows = {row["id"]: row for row in page["results"]}
    assert page["next"] is None and set(rows) == {str(whatsapp.pk), str(sandbox.pk)}
    assert all(set(row) == SHOWN for row in rows.values())
    wa, sb = rows[str(whatsapp.pk)], rows[str(sandbox.pk)]
    assert (wa["platform"], wa["name"], wa["status"], wa["version"]) == (
        "WHATSAPP",
        "Ventas",
        "PENDING",
        1,
    )
    assert wa["status_detail"] == "WEBHOOK_NOT_SEEN" and wa["last_healthcheck_at"] is not None
    assert (wa["external_account_id"], wa["external_parent_id"]) == (
        whatsapp.external_account_id,
        WABA_ID,
    )
    assert wa["token_last_four"] == TEST_TOKEN[-4:] and wa["quality_rating"] == "GREEN"
    assert wa["messaging_limit_tier"] == "TIER_1K" and wa["created_at"] and wa["updated_at"]
    assert (sb["platform"], sb["name"], sb["external_parent_id"]) == ("SANDBOX", "Sandbox", None)
    assert sb["token_last_four"] is None and sb["status_detail"] is None
    for pk, row in rows.items():  # el detalle es la fila de la lista
        assert get(ana, f"{LIST}{pk}/").json() == row
    assert str(credential.pk) not in get(ana, LIST).content.decode()


def test_an_unknown_foreign_or_disconnected_account_is_the_same_404(
    crm: SimpleNamespace, meta: FakeGraph
) -> None:
    other, _ = connect(crm, "b", phone_number_id=SECOND)
    gone, _ = connect(crm)
    ana = signed(crm.ana)
    run(crm.a, crm.ana, accounts.disconnect, channel_account_id=gone.pk)
    answers = {name: reply(get(ana, f"{LIST}{pk}/")) for name, pk in (
        ("other", other.pk), ("gone", gone.pk), ("none", uuid4()),
    )}  # fmt: skip
    assert set(answers.values()) == {NOT_FOUND}, answers
    assert get(ana, LIST).json()["results"] == []  # la desconectada no se lista


def test_an_account_of_another_organization_is_never_listed_nor_retrievable(
    crm: SimpleNamespace, meta: FakeGraph
) -> None:
    mine, _ = connect(crm)
    theirs, _ = connect(crm, "b", phone_number_id=SECOND)
    ana, luis = signed(crm.ana), signed(crm.luis)
    seen_a = [row["id"] for row in get(ana, LIST).json()["results"]]
    seen_b = [row["id"] for row in get(luis, LIST.replace("org-a", "org-b")).json()["results"]]
    assert (seen_a, seen_b) == ([str(mine.pk)], [str(theirs.pk)])
    assert reply(get(ana, f"{LIST}{theirs.pk}/")) == NOT_FOUND
    assert reply(get(luis, f"{LIST.replace('org-a', 'org-b')}{mine.pk}/")) == NOT_FOUND
    # el cursor de A, usado en B, no trae filas de A
    ids = {str(pk) for pk in make(crm.a, 3)}
    cursor = get(ana, f"{LIST}?limit=1").json()["next"]
    foreign = get(luis, f"{LIST.replace('org-a', 'org-b')}?cursor={cursor}")
    assert foreign.status_code == 200 and not ids & {r["id"] for r in foreign.json()["results"]}


def test_no_response_carries_the_token_its_ciphertext_or_the_credential(
    crm: SimpleNamespace, meta: FakeGraph, migrator: DB
) -> None:
    account, first = connect(crm)
    old = bytes(migrator.execute("SELECT ciphertext FROM credentials").fetchone()[0])  # type: ignore[index]
    meta.token = NEW_TOKEN
    run(
        crm.a, crm.ana, accounts.rotate_token, channel_account_id=account.pk, access_token=NEW_TOKEN
    )
    rows = migrator.execute(
        "SELECT id, ciphertext FROM credentials WHERE ciphertext IS NOT NULL"
    ).fetchall()
    assert len(rows) == 1
    live_id, live = rows[0][0], bytes(rows[0][1])
    ana = signed(crm.ana)
    bodies = [get(ana, path) for path in (LIST, f"{LIST}{account.pk}/", f"{LIST}?limit=1")]
    assert all(r.status_code == 200 for r in bodies)
    raw = b"".join(r.content for r in bodies)
    secrets = [TEST_TOKEN, NEW_TOKEN, str(live_id), str(first.pk), str(account.credential_id)]
    for blob in (old, live):
        secrets += [
            blob.hex(),
            base64.b64encode(blob).decode(),
            base64.urlsafe_b64encode(blob).decode(),
        ]
        assert blob not in raw
    for secret in secrets:
        assert secret and secret not in raw.decode(), secret
    assert not [w for w in FORBIDDEN if w in raw.decode().lower()]
    assert bodies[1].json()["token_last_four"] == NEW_TOKEN[-4:]  # el final de la vigente


def test_a_missing_or_revoked_credential_shows_no_token(
    crm: SimpleNamespace, meta: FakeGraph, migrator: DB
) -> None:
    account, _ = connect(crm)
    ana = signed(crm.ana)
    migrator.execute("UPDATE credentials SET revoked_at = now(), ciphertext = NULL")
    assert get(ana, f"{LIST}{account.pk}/").json()["token_last_four"] is None
    migrator.execute("UPDATE channel_accounts SET credential_id = NULL")
    assert get(ana, LIST).json()["results"][0]["token_last_four"] is None


def test_the_list_pages_by_creation_with_the_cursor_of_adr_016(
    crm: SimpleNamespace, meta: FakeGraph
) -> None:
    ids = [str(pk) for pk in make(crm.a, 5)]
    ana, collected, path = signed(crm.ana), [], f"{LIST}?limit=2"
    sizes = []
    while path:
        page = get(ana, path).json()
        sizes.append(len(page["results"]))
        collected += [row["id"] for row in page["results"]]
        path = f"{LIST}?limit=2&cursor={page['next']}" if page["next"] else ""
    assert sizes == [2, 2, 1] and collected == ids  # por creación, sin repetir ni saltar
    assert set(get(ana, LIST).json()) == {"results", "next"}
    for bad in ("limit=0", "limit=201", "limit=x", "cursor=basura"):
        response = get(ana, f"{LIST}?{bad}")
        assert response.status_code == 400 and set(response.json()["fields"]) <= {"limit", "cursor"}


def test_the_number_of_queries_does_not_grow_with_the_accounts(
    crm: SimpleNamespace, meta: FakeGraph
) -> None:
    make(crm.a, 1)
    ana = signed(crm.ana)
    get(ana, LIST)  # calienta lo que se carga una vez
    _, few = queries(ana, LIST)
    make(crm.a, 8, first=1)
    response, many = queries(ana, LIST)
    assert len(response.json()["results"]) == 9 and len(many) == len(few)
    assert len([q for q in many if 'FROM "channel_accounts"' in q]) == 1  # una sola consulta
    assert [q for q in many if "credentials" in q and "channel_accounts" not in q] == []
    one = response.json()["results"][0]["id"]
    assert len(queries(ana, f"{LIST}{one}/")[1]) <= len(few)


@pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
def test_only_reading_is_allowed_on_both_routes(
    crm: SimpleNamespace, meta: FakeGraph, migrator: DB, method: str
) -> None:
    account, _ = connect(crm)
    before = migrator.execute("SELECT t::text FROM channel_accounts t").fetchall()
    for who in (crm.ana, crm.vera):  # sin método declarado, ni el Owner escribe aquí
        for path in (LIST, f"{LIST}{account.pk}/"):
            body = {"name": "Otro"} if method != "delete" else None
            assert reply(send(signed(who), method, path, body)) == DENIED, (who, path)
    assert migrator.execute("SELECT t::text FROM channel_accounts t").fetchall() == before


def test_reading_writes_nothing_and_does_not_spend_the_limit_of_the_write_routes(
    crm: SimpleNamespace, meta: FakeGraph, migrator: DB
) -> None:
    connect(crm)
    audit = "SELECT count(*) FROM audit_logs"
    before = migrator.execute(audit).fetchone()
    ana = signed(crm.ana)
    assert [get(ana, LIST).status_code for _ in range(8)] == [200] * 8
    assert migrator.execute(audit).fetchone() == before
    assert migrator.execute(
        "SELECT count(*) FROM login_throttles WHERE key LIKE 'chn:%'"
    ).fetchone() == (0,)


def test_the_selector_is_the_only_source_of_the_rows(crm: SimpleNamespace, meta: FakeGraph) -> None:
    gone, _ = connect(crm)
    run(crm.a, crm.ana, accounts.disconnect, channel_account_id=gone.pk)
    sandbox = run(crm.a, crm.ana, commands.ensure_sandbox_account)
    probe: Callable[[], list[UUID]] = lambda: [a.pk for a in selectors.channel_accounts()]  # noqa: E731
    with tenant_scope(ctx(crm.a)):
        assert probe() == [sandbox.pk]  # viva, y nada más
    with tenant_scope(ctx(crm.b)):
        assert probe() == []

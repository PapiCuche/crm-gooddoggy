"""F7b-12b (#1098): la caducidad del token en `GET …/channels/` y `GET …/channels/{id}/`
(`token_expires_at` y `token_expiry`), anotada desde la credencial viva de cada cuenta. PostgreSQL
real con `crm_app`; nada del token más que la fecha y el estado."""

import base64
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import psycopg
import pytest
from django.utils import timezone

from apps.conversations import commands, credentials
from apps.conversations.models import ChannelAccount, Credential
from core.tenancy.scope import tenant_scope
from tests.meta_sim.graph import TEST_TOKEN, FakeGraph
from tests.test_channel_accounts import SECOND, connect, crm, meta, real_stack, run
from tests.test_channel_accounts_api import signed
from tests.test_channel_accounts_read_api import LIST, get, queries
from tests.test_memberships import ctx
from tests.test_self_context import NOT_FOUND, reply

__all__ = ["crm", "meta", "real_stack"]  # los fixtures, para pytest
pytestmark = pytest.mark.usefixtures("tenant_db")
DB = psycopg.Connection[Any]
KNOWN = object()  # leída en Meta y sin fecha: no caduca
B_LIST = LIST.replace("org-a", "org-b")


def social(
    org: UUID, n: int, expiry: Any = None, *, platform: str = "MESSENGER"
) -> tuple[ChannelAccount, Credential]:
    """Una cuenta social viva con su credencial: `expiry` `None` (no se sabe), `KNOWN` (no
    caduca) o la fecha leída. Sin pasar por Meta."""
    page = f"7{n:014d}"
    parent = {"external_parent_id": f"8{n:014d}"} if platform == "INSTAGRAM" else {}
    with tenant_scope(ctx(org)) as tenant:
        account = ChannelAccount.objects.create(
            platform=platform, name=f"Página {n}", status="PENDING",
            external_account_id=page, **parent,
        )  # fmt: skip
        read = None if expiry is None else ((None,) if expiry is KNOWN else (expiry,))
        made = credentials.store(tenant, account.pk, TEST_TOKEN[:-4] + f"{n:04d}", expiry=read)
        ChannelAccount.objects.filter(pk=account.pk).update(credential_id=made.pk)
        account.credential_id = made.pk
        return account, made


def rows(client: Any, path: str = LIST) -> dict[str, dict[str, Any]]:
    return {row["id"]: row for row in get(client, path).json()["results"]}


def shown(row: dict[str, Any]) -> tuple[Any, datetime | None]:
    at = row["token_expires_at"]
    return row["token_expiry"], None if at is None else datetime.fromisoformat(at)


def test_each_account_shows_the_expiry_of_its_live_credential_and_the_detail_agrees(
    crm: SimpleNamespace, meta: FakeGraph, migrator: DB
) -> None:
    soon = timezone.now().replace(microsecond=0) + timedelta(days=5)
    dated, _ = social(crm.a, 1, soon)
    never, _ = social(crm.a, 2, KNOWN, platform="INSTAGRAM")
    unknown, _ = social(crm.a, 3)
    whatsapp, _ = connect(crm)  # WhatsApp no la lee: sigue sin saberse
    sandbox = run(crm.a, crm.ana, commands.ensure_sandbox_account)
    gone, revoked = social(crm.a, 4, soon + timedelta(days=1))
    migrator.execute(
        "UPDATE credentials SET revoked_at = now(), ciphertext = NULL WHERE id = %s", [revoked.pk]
    )
    ana = signed(crm.ana)
    found = rows(ana)
    assert {pk: shown(found[str(pk)]) for pk in (dated.pk, never.pk, unknown.pk)} == {
        dated.pk: ("AT", soon), never.pk: ("NEVER", None), unknown.pk: ("UNKNOWN", None),
    }  # fmt: skip
    assert shown(found[str(whatsapp.pk)]) == ("UNKNOWN", None)
    assert shown(found[str(sandbox.pk)]) == shown(found[str(gone.pk)]) == (None, None)
    for pk, row in found.items():  # el detalle es la fila de la lista
        assert get(ana, f"{LIST}{pk}/").json() == row


def test_another_organization_never_sees_the_date(crm: SimpleNamespace, meta: FakeGraph) -> None:
    mine_at = timezone.now().replace(microsecond=0) + timedelta(days=7, hours=3)
    theirs_at = mine_at + timedelta(days=20)
    mine, _ = social(crm.a, 1, mine_at)
    theirs, _ = social(crm.b, 2, theirs_at)
    ana, luis = signed(crm.ana), signed(crm.luis)
    seen_b = get(luis, B_LIST)
    assert [shown(r) for r in seen_b.json()["results"]] == [("AT", theirs_at)]
    assert reply(get(luis, f"{B_LIST}{mine.pk}/")) == NOT_FOUND
    assert reply(get(ana, f"{LIST}{theirs.pk}/")) == NOT_FOUND
    assert [shown(r) for r in get(ana, LIST).json()["results"]] == [("AT", mine_at)]
    assert mine_at.isoformat()[:16] not in seen_b.content.decode()  # ni en el texto crudo


def test_no_response_carries_anything_of_the_token_but_its_date_and_state(
    crm: SimpleNamespace, meta: FakeGraph, migrator: DB
) -> None:
    account, credential = social(crm.a, 1, timezone.now() + timedelta(days=3))
    blob, key, checked = migrator.execute(
        "SELECT ciphertext, key_version, expiry_checked_at FROM credentials WHERE id = %s",
        [credential.pk],
    ).fetchone()  # type: ignore[misc]
    blob = bytes(blob)
    ana = signed(crm.ana)
    bodies = [get(ana, path) for path in (LIST, f"{LIST}{account.pk}/", f"{LIST}?limit=1")]
    assert all(r.status_code == 200 for r in bodies)
    raw = b"".join(r.content for r in bodies)
    nonce = blob[3:15]  # `core.sealed`: formato, versión de la clave y el nonce de 12 bytes
    assert blob not in raw and nonce not in raw
    text = raw.decode()
    secrets = [TEST_TOKEN[:-4], str(credential.pk), blob.hex(), nonce.hex()]
    secrets += [base64.b64encode(blob).decode(), base64.urlsafe_b64encode(blob).decode()]
    secrets += [checked.strftime("%Y-%m-%dT%H:%M:%S.%f")]  # ni cuándo se leyó en Meta
    assert [s for s in secrets if s in text] == []
    forbidden = ("cipher", "key_version", "credential", "checked_at", "warned", "nonce")
    assert [w for w in forbidden if w in text.lower()] == [] and key is not None


def test_a_credential_of_another_account_shows_no_expiry(
    crm: SimpleNamespace, meta: FakeGraph, migrator: DB
) -> None:
    mine, _ = social(crm.a, 1)
    other, theirs = social(crm.a, 2, timezone.now() + timedelta(days=2))
    migrator.execute(
        "UPDATE channel_accounts SET credential_id = %s WHERE id = %s", [theirs.pk, mine.pk]
    )
    found = rows(signed(crm.ana))
    assert shown(found[str(mine.pk)]) == (None, None)
    assert shown(found[str(other.pk)])[0] == "AT"


def test_the_expiry_costs_no_query_per_account(crm: SimpleNamespace, meta: FakeGraph) -> None:
    social(crm.a, 1, timezone.now() + timedelta(days=1))
    ana = signed(crm.ana)
    get(ana, LIST)  # calienta lo que se carga una vez
    _, few = queries(ana, LIST)
    for n in range(2, 10):
        social(crm.a, n, (None, KNOWN, timezone.now() + timedelta(days=n))[n % 3])
    connect(crm, phone_number_id=SECOND)
    response, many = queries(ana, LIST)
    states = {row["token_expiry"] for row in response.json()["results"]}
    assert len(response.json()["results"]) == 10 and states == {"AT", "NEVER", "UNKNOWN"}
    assert len(many) == len(few)
    assert len([q for q in many if 'FROM "channel_accounts"' in q]) == 1
    assert [q for q in many if "credentials" in q and "channel_accounts" not in q] == []

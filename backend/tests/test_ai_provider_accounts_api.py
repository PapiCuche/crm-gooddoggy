"""F8-03c: `GET`/`POST …/ai/provider-accounts/`, `POST …/{id}/key/` y `DELETE …/{id}/` (ADR-034 §4).
Middleware, sesión, CSRF, motor de autorización y PostgreSQL con `crm_app`; la API de Anthropic,
simulada (ninguna ruta la llama). Primero `ai.manage`, después el segundo factor reciente (D-F7-11),
un solo 404 para lo que no es de la organización y ninguna salida con la clave. Claves ficticias."""

import logging
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from django.test import Client

from apps.ai_gateway import keys
from core.api.step_up import STEP_UP_REQUIRED
from tests.ai_sim import TEST_KEY
from tests.test_ai_provider_accounts import OTHER_KEY, audit, crm, everything, real_stack
from tests.test_authorization import give
from tests.test_channel_accounts_api import signed
from tests.test_conversations_api import UNAUTHENTICATED
from tests.test_member_branch import DENIED
from tests.test_self_context import NOT_FOUND, reply
from tests.test_self_context import signed as plain
from tests.test_team_writes import send

__all__ = ["crm", "real_stack"]  # los fixtures, para pytest
pytestmark = pytest.mark.usefixtures("tenant_db")
DB = psycopg.Connection[Any]
BASE = "/api/v1/o/org-a/ai/provider-accounts/"
SHOWN = {
    "id", "provider", "status", "key_last_four", "configured_at", "rotated_at", "revoked_at",
    "updated_at",
}  # fmt: skip


def create(client: Any, key: str = TEST_KEY, base: str = BASE, **over: Any) -> Any:
    return send(client, "post", base, {"provider": "ANTHROPIC", "api_key": key} | over)


def rotate(client: Any, account_id: Any, key: str = OTHER_KEY, base: str = BASE) -> Any:
    return send(client, "post", f"{base}{account_id}/key/", {"api_key": key})


def test_create_list_rotate_and_delete_and_no_response_carries_the_key(
    crm: SimpleNamespace, migrator: DB, caplog: pytest.LogCaptureFixture
) -> None:
    ana = signed(crm.ana)
    with caplog.at_level(logging.DEBUG):
        made = create(ana)
        account = made.json()
        listed = ana.get(BASE)
        rotated = rotate(ana, account["id"])
        gone = send(ana, "delete", f"{BASE}{account['id']}/", None)
        after = ana.get(BASE)
        again = create(ana)  # una clave nueva reactiva la misma cuenta
    assert made.status_code == 201 and set(account) == SHOWN
    assert (account["provider"], account["status"]) == ("ANTHROPIC", "ACTIVE")
    assert account["key_last_four"] == TEST_KEY[-4:] and account["rotated_at"] is None
    assert listed.status_code == 200 and listed.json() == [account]
    assert rotated.status_code == 200 and rotated.json()["key_last_four"] == OTHER_KEY[-4:]
    assert rotated.json()["rotated_at"] is not None and gone.status_code == 204
    [disabled] = after.json()
    assert (disabled["status"], disabled["revoked_at"] is not None) == ("DISABLED", True)
    assert again.status_code == 201 and again.json()["id"] == account["id"]
    actions = [row[0] for row in audit(migrator)]
    assert actions == [
        "ai.account.created", "ai.account.key_rotated", "ai.account.disabled",
        "ai.account.created",
    ]  # fmt: skip
    outputs = b"".join(r.content for r in (made, listed, rotated, gone, after, again)).decode()
    seen = outputs + caplog.text + everything(migrator)
    assert TEST_KEY not in seen and OTHER_KEY not in seen


def test_the_permission_goes_before_the_second_factor_and_nothing_changes_without_both(
    crm: SimpleNamespace, migrator: DB
) -> None:
    made = create(signed(crm.ana)).json()
    paths = (
        ("get", BASE, None), ("post", BASE, {"provider": "ANTHROPIC", "api_key": OTHER_KEY}),
        ("post", f"{BASE}{made['id']}/key/", {"api_key": OTHER_KEY}),
        ("delete", f"{BASE}{made['id']}/", None),
    )  # fmt: skip
    for method, path, body in paths:
        for vera in (plain(crm.vera), signed(crm.vera)):  # vendedora: sin `ai.manage`
            assert reply(send(vera, method, path, body)) == DENIED, (method, path)
        late = send(plain(crm.ana), method, path, body)  # Owner sin segundo factor reciente
        assert late.status_code == 403 and late.json()["code"] == STEP_UP_REQUIRED, (method, path)
        anonymous = send(Client(), method, path, body)
        assert reply(anonymous) == UNAUTHENTICATED
    assert [row[0] for row in audit(migrator)] == ["ai.account.created"]
    assert signed(crm.ana).get(BASE).json()[0]["key_last_four"] == TEST_KEY[-4:]  # sin cambios
    assert OTHER_KEY not in everything(migrator)


def test_another_organizations_account_is_a_single_404_and_the_body_goes_first(
    crm: SimpleNamespace, migrator: DB
) -> None:
    give(crm.b, crm.m_luis, {"ai.manage": None})
    mine = create(signed(crm.ana)).json()
    luis, b_base = signed(crm.luis), "/api/v1/o/org-b/ai/provider-accounts/"
    assert luis.get(b_base).json() == []  # la cuenta de A no existe para B
    for target in (mine["id"], uuid4()):
        assert reply(rotate(luis, target, base=b_base)) == NOT_FOUND
        assert reply(send(luis, "delete", f"{b_base}{target}/", None)) == NOT_FOUND
    assert rotate(luis, uuid4(), key="", base=b_base).status_code == 400  # el cuerpo, antes
    theirs = create(luis, OTHER_KEY, base=b_base).json()  # B ya tiene la suya
    for target in (mine["id"], uuid4()):  # el id de A nunca llega a la cuenta de B
        assert reply(rotate(luis, target, TEST_KEY, base=b_base)) == NOT_FOUND
        assert reply(send(luis, "delete", f"{b_base}{target}/", None)) == NOT_FOUND
    assert luis.get(b_base).json() == [theirs]  # sin cambios
    ana = signed(crm.ana)
    assert send(ana, "delete", f"{BASE}{mine['id']}/", None).status_code == 204
    assert reply(rotate(ana, mine["id"])) == NOT_FOUND  # desactivada: el mismo 404
    assert reply(send(ana, "delete", f"{BASE}{mine['id']}/", None)) == NOT_FOUND
    actions = [row[0] for row in audit(migrator)]
    assert actions == ["ai.account.created", "ai.account.created", "ai.account.disabled"]
    assert OTHER_KEY not in everything(migrator)


def test_conflicts_and_bad_bodies_never_echo_the_key(
    crm: SimpleNamespace, migrator: DB, monkeypatch: pytest.MonkeyPatch
) -> None:
    ana = signed(crm.ana)
    bad = "fake anthropic key not real 0004"  # ficticia y mal formada
    for wrong in (bad, "k" * 257, TEST_KEY + "\n"):
        refused = create(ana, wrong)
        assert refused.status_code == 400 and set(refused.json()["fields"]) == {"api_key"}
        assert wrong.strip() not in refused.content.decode()
    assert create(ana, provider="OPENAI").status_code == 400
    assert create(ana).status_code == 201
    exists = create(ana, OTHER_KEY)
    assert (exists.status_code, exists.json()["code"]) == (409, "AI_ACCOUNT_EXISTS")
    account_id = ana.get(BASE).json()[0]["id"]
    assert send(ana, "delete", f"{BASE}{account_id}/", None).status_code == 204
    monkeypatch.setattr(keys, "_locked", lambda alias, provider: None)  # dos altas a la vez
    raced = create(ana, OTHER_KEY)
    assert (raced.status_code, raced.json()["code"]) == (409, "AI_ACCOUNT_CONFLICT")
    outputs = exists.content.decode() + raced.content.decode() + everything(migrator)
    assert OTHER_KEY not in outputs and bad not in everything(migrator)

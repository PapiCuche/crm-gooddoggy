"""F1-14: con `SANDBOX_INBOUND_ENABLED` apagado (production), la entrada Sandbox no existe."""

from types import SimpleNamespace
from typing import Any

import psycopg
import pytest
from django.test import Client
from django.urls import Resolver404, resolve

from apps.contacts.models import Contact
from apps.conversations import commands
from core.tenancy.scope import tenant_scope
from tests.test_conversations_api import crm, inbox, real_stack
from tests.test_conversations_messages_api import INBOUND, simulated
from tests.test_memberships import ctx
from tests.test_self_context import signed
from tests.test_team_writes import send

__all__ = ["crm", "inbox", "real_stack"]
UNKNOWN = "/api/v1/o/org-a/channels/sandbox/elsewhere/"
TABLES = ("channel_accounts", "conversations", "messages", "audit_logs", "outbox_events")


def counts(migrator: psycopg.Connection[Any]) -> list[Any]:
    return [migrator.execute(f"SELECT count(*) FROM {t}").fetchone() for t in TABLES]  # noqa: S608


def answer(client: Client, path: str, body: Any, csrf: bool) -> tuple[int, bytes, str]:
    sent = send(client, "post", path, body) if csrf else client.post(path, body, "application/json")
    return sent.status_code, sent.content, sent.get("Content-Type", "")


def test_off_the_route_is_the_same_404_as_an_unknown_path_and_writes_nothing(
    inbox: SimpleNamespace, migrator: psycopg.Connection[Any], settings: Any
) -> None:
    with tenant_scope(ctx(inbox.a)):
        who = Contact.objects.create(display_name="x").pk
    settings.SANDBOX_INBOUND_ENABLED = False
    before = counts(migrator)
    clients = {"anon": Client(), "owner": signed(inbox.ana), "seller": signed(inbox.vera)}
    clients["other-org"] = signed(inbox.luis)
    for name, client in clients.items():
        for body in (simulated(who), "basura", {}):
            for csrf in (True, False):
                gated = answer(client, INBOUND, body, csrf)
                if csrf:
                    assert gated[0] == (401 if name == "anon" else 404), (name, gated)
                assert gated == answer(client, UNKNOWN, body, csrf), (name, body, csrf)
    assert counts(migrator) == before
    settings.SANDBOX_INBOUND_ENABLED = True  # se decide en cada petición, no al importar
    assert resolve(INBOUND).url_name == "sandbox-inbound"
    settings.SANDBOX_INBOUND_ENABLED = False
    with pytest.raises(Resolver404):
        resolve(INBOUND)


def test_off_the_public_schema_does_not_announce_the_route(settings: Any) -> None:
    settings.SANDBOX_INBOUND_ENABLED = False
    hidden = Client().get("/api/schema/")
    assert hidden.status_code == 200 and b"/conversations/" in hidden.content
    assert b"sandbox/inbound" not in hidden.content and b"channels_sandbox" not in hidden.content
    settings.SANDBOX_INBOUND_ENABLED = True
    assert b"channels_sandbox_inbound" in Client().get("/api/schema/").content


def test_off_the_sandbox_account_cannot_be_created(
    inbox: SimpleNamespace, migrator: psycopg.Connection[Any], settings: Any
) -> None:
    settings.SANDBOX_INBOUND_ENABLED = False
    before = counts(migrator)
    with tenant_scope(ctx(inbox.a, inbox.ana)) as tenant, pytest.raises(commands.SandboxDisabled):
        commands.ensure_sandbox_account(tenant)
    assert counts(migrator) == before

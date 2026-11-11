"""F7-05a: `GET …/conversations/{id}/messaging-policy/` y los 409 del envío por la política.
Middleware, sesión, motor de autorización y PostgreSQL con el rol `crm_app`."""

import datetime as dt
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from django.test import Client
from django.utils import timezone

from apps.contacts.models import Contact
from apps.conversations.models import ChannelAccount, Conversation
from core.tenancy.scope import tenant_scope
from tests import test_conversations_tables as tables
from tests.factories import make_user
from tests.test_authorization import give
from tests.test_conversations_api import BASE, UNAUTHENTICATED, crm, inbox, real_stack
from tests.test_conversations_send_api import counts, post
from tests.test_member_branch import DENIED
from tests.test_memberships import ctx, join
from tests.test_self_context import NOT_FOUND, reply, signed

__all__ = ["crm", "inbox", "real_stack"]  # los fixtures, para pytest
pytestmark = pytest.mark.usefixtures("tenant_db")
FIELDS = {"allowed", "mode", "reason", "window_expires_at", "window_hours", "evaluated_at"}


def url(convo: Any) -> str:
    return f"{BASE}{convo}/messaging-policy/"


def whatsapp_convo(inbox: SimpleNamespace, ago: dt.timedelta | None, **over: Any) -> Conversation:
    with tenant_scope(ctx(inbox.a)):
        account = ChannelAccount.objects.create(
            platform="WHATSAPP", name="WA", status="DEGRADED", external_account_id="15550007002",
            external_parent_id="1234567890",
        )  # fmt: skip
    stamp = None if ago is None else timezone.now() - ago
    convo: Conversation = tables.fresh(
        inbox.a, account, "CONV-07002", last_inbound_at=stamp, **over
    )
    return convo


def test_the_policy_of_a_sandbox_conversation_is_open_with_no_window(
    inbox: SimpleNamespace,
) -> None:
    found = signed(inbox.ana).get(url(inbox.convs["nobody"]))
    assert found.status_code == 200, found.content
    body = found.json()
    assert set(body) == FIELDS
    assert (body["allowed"], body["mode"], body["reason"]) == (True, "FREE_FORM", None)
    assert (body["window_hours"], body["window_expires_at"]) == (None, None)


@pytest.mark.parametrize(
    ("ago", "blocked", "reason"),
    [(dt.timedelta(hours=24), False, "WINDOW_CLOSED"), (None, False, "WINDOW_CLOSED"),
     (dt.timedelta(hours=1), True, "CONTACT_BLOCKED")],
)  # fmt: skip
def test_the_policy_says_why_a_reply_cannot_go_out(
    inbox: SimpleNamespace, ago: dt.timedelta | None, blocked: bool, reason: str
) -> None:
    convo = whatsapp_convo(inbox, ago)
    with tenant_scope(ctx(inbox.a)):
        Contact.objects.filter(pk=convo.contact_id).update(is_blocked=blocked)
    body = signed(inbox.ana).get(url(convo.pk)).json()
    assert (body["allowed"], body["mode"], body["reason"]) == (False, "BLOCKED", reason)


def test_the_route_needs_both_permissions_and_the_send_scope_and_hides_other_tenants(
    inbox: SimpleNamespace,
) -> None:
    convo = inbox.convs["vera"]
    assert reply(signed(inbox.bea).get(url(convo))) == DENIED  # sin rol
    assert reply(Client().get(url(convo))) == UNAUTHENTICATED
    assert reply(signed(inbox.luis).get(url(convo))) == NOT_FOUND  # de B, en la ruta de A
    only: tuple[dict[str, str | None], ...] = (
        {"messages.send": "ORGANIZATION"}, {"conversations.view": "ORGANIZATION"},
    )  # fmt: skip
    for index, grants in enumerate(only):
        user = make_user(email=f"solo{index}@example.com")
        give(inbox.a, join(inbox.a, user).pk, grants)
        assert reply(signed(user).get(url(convo))) == DENIED  # cada permiso solo no basta
    missing = reply(signed(inbox.ana).get(url(uuid4())))
    assert missing == NOT_FOUND
    vera = signed(inbox.vera)
    assert vera.get(url(convo)).status_code == 200
    for key in ("sam", "ana", "team", "b"):  # ajenas, del equipo (a ella no) o de B: la misma
        assert reply(vera.get(url(inbox.convs[key]))) == missing, key
    wide = make_user(email="ancho@example.com")  # ve toda la organización, envía solo lo suyo
    give(inbox.a, join(inbox.a, wide).pk,
         {"messages.send": "OWN", "conversations.view": "ORGANIZATION"})  # fmt: skip
    assert reply(signed(wide).get(url(inbox.convs["sam"]))) == missing  # lo que no envía, no existe


def test_sending_outside_the_window_or_to_a_blocked_contact_is_a_clear_409_and_writes_nothing(
    inbox: SimpleNamespace, migrator: Any
) -> None:
    ana, late = signed(inbox.ana), whatsapp_convo(inbox, dt.timedelta(hours=24))
    refused = post(ana, late.pk)
    assert (refused.status_code, refused.json()["code"]) == (409, "WINDOW_CLOSED")
    assert "ventana" in refused.json()["message"]
    with tenant_scope(ctx(inbox.a)):
        Conversation.objects.filter(pk=late.pk).update(last_inbound_at=timezone.now())
        Contact.objects.filter(pk=late.contact_id).update(is_blocked=True)
    blocked = post(ana, late.pk)
    assert (blocked.status_code, blocked.json()["code"]) == (409, "CONTACT_BLOCKED")
    assert counts(migrator) == (0, 0, 0)
    with tenant_scope(ctx(inbox.a)):
        Contact.objects.filter(pk=late.contact_id).update(is_blocked=False)
    sent = post(ana, late.pk, body="Dentro de la ventana")
    assert sent.status_code == 202 and counts(migrator) == (1, 1, 1)
    saved = migrator.execute("SELECT policy_decision->>'allowed' FROM messages").fetchone()
    assert saved == ("true",)  # la decisión viaja con el mensaje

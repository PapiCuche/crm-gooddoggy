"""F6-08a2: buscar respuestas rápidas desde una conversación y resolver su texto sin enviar
(`GET …/conversations/{id}/quick-replies/`, `POST …/quick-replies/{qr}/render/`). Middleware,
sesión, motor de autorización y PostgreSQL con el rol `crm_app`."""

from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from django.test import Client

from apps.contacts.models import Contact
from apps.conversations import quick_replies as qr
from apps.conversations.models import Conversation, QuickReplyCategory
from apps.conversations.seed import ensure_quick_reply_categories
from core.tenancy.scope import tenant_scope
from tests.factories import make_user
from tests.test_authorization import give
from tests.test_conversations_api import BASE, UNAUTHENTICATED, crm, inbox, real_stack
from tests.test_member_branch import DENIED
from tests.test_memberships import ctx, join
from tests.test_products import send
from tests.test_self_context import NOT_FOUND, reply, signed

__all__ = ["crm", "inbox", "real_stack"]  # los fixtures, para pytest
pytestmark = pytest.mark.usefixtures("tenant_db")
BODY = "Hola {{contact.first_name}}, te escribe {{ org.name }}. Envío hoy."


@pytest.fixture
def quick(inbox: SimpleNamespace) -> SimpleNamespace:
    """Sobre `inbox`: categorías en A y B, `delivery` en Delivery de A, y el contacto de la
    conversación de vera es suyo (lo ve) y se llama Rosa."""
    made = {}
    for org, user in (("a", inbox.ana), ("b", None)):
        with tenant_scope(ctx(getattr(inbox, org), user)) as tenant:
            ensure_quick_reply_categories(tenant)
            delivery = QuickReplyCategory.objects.get(name="Delivery").pk
            if user is not None:
                made["delivery"] = qr.create_reply(
                    tenant,
                    shortcut="delivery",
                    title="A domicilio",
                    category_id=delivery,
                    body=BODY,
                ).pk
    with tenant_scope(ctx(inbox.a)):
        convo = Conversation.objects.get(pk=inbox.convs["vera"])
        Contact.objects.filter(pk=convo.contact_id).update(
            owner_user_id=inbox.vera.pk, first_name="Rosa"
        )
    return SimpleNamespace(**vars(inbox), qr=made, contact=convo.contact_id)


def listing(convo: Any, org: str = "org-a") -> str:
    return f"/api/v1/o/{org}/conversations/{convo}/quick-replies/"


def render(client: Client, convo: Any, reply_id: Any, org: str = "org-a") -> Any:
    return send(client, "post", f"{listing(convo, org)}{reply_id}/render/")


def found(client: Client, convo: Any, q: str | None = None) -> list[str]:
    response = client.get(listing(convo), {} if q is None else {"q": q})
    assert response.status_code == 200, response.content
    return [row["shortcut"] for row in response.json()["results"]]


def written(migrator: psycopg.Connection[Any]) -> tuple[Any, ...]:
    row = migrator.execute(
        "SELECT (SELECT count(*) FROM messages), (SELECT count(*) FROM outbox_events), "
        "(SELECT count(*) FROM audit_logs WHERE action NOT LIKE 'quick_reply%'), "
        "(SELECT max(usage_count) FROM quick_replies), (SELECT max(version) FROM quick_replies)"
    ).fetchone()
    assert row is not None
    return tuple(row)


def test_a_seller_finds_delivery_by_deli_and_renders_it_without_sending(
    quick: SimpleNamespace, migrator: psycopg.Connection[Any]
) -> None:
    vera, convo = signed(quick.vera), quick.convs["vera"]
    rows = vera.get(listing(convo), {"q": "deli"}).json()["results"]
    assert [(r["shortcut"], r["title"], r["body"]) for r in rows] == [
        ("delivery", "A domicilio", BODY)
    ]
    assert set(rows[0]["category"]) == {"id", "name", "sort_order"}
    assert (rows[0]["category"]["name"], rows[0]["category"]["sort_order"]) == ("Delivery", 3)
    for q in ("/DELI", "Delivery", " envío ", "DOMICILIO", None):  # atajo, texto, título; nada
        assert found(vera, convo, q) == ["delivery"], q
    assert found(vera, convo, "garantía") == []
    before = written(migrator)
    done = render(vera, convo, quick.qr["delivery"])
    assert done.status_code == 200, done.content
    assert done.json() == {
        "id": str(quick.qr["delivery"]), "shortcut": "delivery",
        "text": "Hola Rosa, te escribe Org A. Envío hoy.", "missing": [],
    }  # fmt: skip
    assert render(vera, convo, quick.qr["delivery"]).status_code == 200
    assert written(migrator) == (*before[:3], 2, 1)  # solo suma usos: ni mensaje, ni outbox


def test_contact_variables_need_contacts_view_over_that_contact(quick: SimpleNamespace) -> None:
    convo = quick.convs["vera"]
    with tenant_scope(ctx(quick.a)):  # ya no es suyo: vera no lo ve
        Contact.objects.filter(pk=quick.contact).update(owner_user_id=quick.ana.pk)
    hidden = render(signed(quick.vera), convo, quick.qr["delivery"]).json()
    assert (hidden["text"], hidden["missing"]) == (
        "Hola , te escribe Org A. Envío hoy.", ["contact.first_name"],
    )  # fmt: skip
    seen = render(signed(quick.ana), convo, quick.qr["delivery"]).json()  # la Owner lo ve todo
    assert seen["text"] == "Hola Rosa, te escribe Org A. Envío hoy."
    with tenant_scope(ctx(quick.a, quick.ana)) as tenant:  # un nombre nunca se interpreta
        Contact.objects.filter(pk=quick.contact).update(display_name="{{org.name}}")
        named = qr.create_reply(
            tenant, shortcut="nombre", title="Nombre", body="A: {{contact.name}}",
            category_id=QuickReplyCategory.objects.get(name="Ventas").pk,
        )  # fmt: skip
    assert render(signed(quick.ana), convo, named.pk).json()["text"] == "A: {{org.name}}"


def test_both_permissions_are_needed(
    quick: SimpleNamespace, migrator: psycopg.Connection[Any]
) -> None:
    convo, used = quick.convs["vera"], quick.qr["delivery"]
    for client in (signed(quick.bea), Client()):  # sin rol; sin sesión
        expected = DENIED if client.cookies else UNAUTHENTICATED
        assert reply(client.get(listing(convo))) == expected
        assert reply(render(client, convo, used)) == expected
    only: tuple[dict[str, str | None], ...] = (
        {"messages.send": "ORGANIZATION"}, {"conversations.view": "ORGANIZATION"},
    )  # fmt: skip
    for index, grants in enumerate(only):
        user = make_user(email=f"solo{index}@example.com")
        give(quick.a, join(quick.a, user).pk, grants)
        assert reply(signed(user).get(listing(convo))) == DENIED  # uno solo no basta
        assert reply(render(signed(user), convo, used)) == DENIED
    assert written(migrator)[3] == 0


def test_what_is_not_reachable_or_not_usable_is_the_same_404(
    quick: SimpleNamespace, migrator: psycopg.Connection[Any]
) -> None:
    vera, ana, used = signed(quick.vera), signed(quick.ana), quick.qr["delivery"]
    for convo in (quick.convs["sam"], quick.convs["b"], uuid4()):  # ajena, de B, inexistente
        assert reply(vera.get(listing(convo))) == NOT_FOUND
        assert reply(render(vera, convo, used)) == NOT_FOUND
    wide = make_user(email="ancho@example.com")  # ve toda la organización, envía solo lo suyo
    give(quick.a, join(quick.a, wide).pk,
         {"messages.send": "OWN", "conversations.view": "ORGANIZATION"})  # fmt: skip
    assert reply(signed(wide).get(listing(quick.convs["sam"]))) == NOT_FOUND
    give(quick.b, quick.m_luis, {"messages.send": "ORGANIZATION",
                                 "conversations.view": "ORGANIZATION"})  # fmt: skip
    luis = signed(quick.luis)  # en B no existe la respuesta de A
    assert reply(render(luis, quick.convs["b"], used, "org-b")) == NOT_FOUND
    assert reply(luis.get(listing(quick.convs["vera"], "org-b"))) == NOT_FOUND
    with tenant_scope(ctx(quick.a, quick.ana)) as tenant:
        qr.update_reply(tenant, quick_reply_id=used, version=1, is_active=False)
    assert reply(render(ana, quick.convs["vera"], used)) == NOT_FOUND  # inactiva
    assert reply(render(ana, quick.convs["vera"], uuid4())) == NOT_FOUND
    with tenant_scope(ctx(quick.a, quick.ana)) as tenant:
        qr.update_reply(tenant, quick_reply_id=used, version=2, is_active=True)
        QuickReplyCategory.objects.filter(name="Delivery").update(is_active=False)
    assert found(ana, quick.convs["vera"]) == []  # su categoría está inactiva
    assert reply(render(ana, quick.convs["vera"], used)) == NOT_FOUND
    assert written(migrator)[3] == 0


@pytest.mark.parametrize("q", ["x" * 101, "a\x00b", "a\nb"])
def test_a_bad_search_is_a_400_on_q(quick: SimpleNamespace, q: str) -> None:
    response = signed(quick.vera).get(listing(quick.convs["vera"]), {"q": q})
    assert response.status_code == 400 and list(response.json()["fields"]) == ["q"]


def test_the_routes_are_in_the_conversation_and_the_list_is_paginated(
    quick: SimpleNamespace,
) -> None:
    assert listing(quick.convs["vera"]).startswith(BASE)
    page = signed(quick.ana).get(listing(quick.convs["vera"]), {"limit": 1}).json()
    assert set(page) == {"results", "next"} and len(page["results"]) == 1
    assert UUID(page["results"][0]["id"]) == quick.qr["delivery"]

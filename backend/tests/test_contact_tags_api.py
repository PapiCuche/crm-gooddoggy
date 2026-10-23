"""F4-11: etiquetar contactos (`PUT`/`DELETE …/contacts/{id}/tags/{tag_id}/`), sus etiquetas en
la respuesta y el filtro `?tag=`. Middleware, sesión, motor de autorización y PostgreSQL con el
rol `crm_app`."""

from types import SimpleNamespace
from typing import Any
from urllib.parse import urlencode
from uuid import UUID, uuid4

import psycopg
import pytest
from django.db import IntegrityError, connection, transaction
from django.test import Client
from django.test.utils import CaptureQueriesContext

from apps.contacts.models import ContactTag
from core.tenancy.scope import tenant_scope
from tests import test_contacts_api, test_self_context
from tests.test_anti_escalation import audit
from tests.test_authorization import give
from tests.test_contacts_api import CONTACTS
from tests.test_contacts_write_api import ROW, held, patch
from tests.test_member_branch import DENIED
from tests.test_memberships import ctx
from tests.test_self_context import NOT_FOUND, reply, signed
from tests.test_tags_api import make_tag
from tests.test_team_writes import send

crm, real_stack = test_contacts_api.crm, test_self_context.real_stack
pytestmark = pytest.mark.usefixtures("tenant_db")
LINKS = "SELECT contact_id, tag_id, added_by_user_id FROM contact_tags ORDER BY created_at, id"


def tag(client: Client, contact_id: Any, tag_id: Any, method: str = "put") -> Any:
    return send(client, method, f"{CONTACTS}{contact_id}/tags/{tag_id}/", None)


def untag(client: Client, contact_id: Any, tag_id: Any) -> Any:
    return tag(client, contact_id, tag_id, "delete")


def links(migrator: psycopg.Connection[Any]) -> list[tuple[Any, ...]]:
    return migrator.execute(LINKS).fetchall()


def tags_of(client: Client, contact_id: Any) -> list[str]:
    response = client.get(f"{CONTACTS}{contact_id}/")
    assert response.status_code == 200, response.content
    return [row["name"] for row in response.json()["tags"]]


def found(client: Client, **params: str) -> set[UUID]:
    response = client.get(f"{CONTACTS}?{urlencode(params)}")
    assert response.status_code == 200, response.content
    return {UUID(row["id"]) for row in response.json()["results"]}


def test_a_seller_tags_their_contact_once_and_untags_it_with_audit(
    crm: SimpleNamespace, migrator: psycopg.Connection[Any]
) -> None:
    vip, owned, vera = make_tag(crm.a, "VIP"), crm.ids["owned"], signed(crm.vera)
    before = migrator.execute(f"{ROW} WHERE id = %s", [owned]).fetchone()
    for _ in range(2):  # idempotente: la segunda no escribe ni audita
        assert reply(tag(vera, owned, vip)) == (204, b"")
    assert links(migrator) == [(owned, vip, crm.vera.pk)]
    shown = vera.get(f"{CONTACTS}{owned}/").json()["tags"]
    assert shown == [{"id": str(vip), "name": "VIP", "color": "#1f9d55", "is_active": True}]
    assert migrator.execute(f"{ROW} WHERE id = %s", [owned]).fetchone() == before  # ni versión
    for _ in range(2):
        assert reply(untag(vera, owned, vip)) == (204, b"")
    assert links(migrator) == [] and tags_of(vera, owned) == []
    metadata = {"tag": "VIP"}
    assert audit(migrator) == [
        ("contact.tagged", "contact", owned, {"tag_id": [None, str(vip)]}, metadata, crm.vera.pk),
        ("contact.untagged", "contact", owned, {"tag_id": [str(vip), None]}, metadata, crm.vera.pk),
    ]


def test_out_of_scope_foreign_or_unknown_is_404_and_without_update_403(
    crm: SimpleNamespace, migrator: psycopg.Connection[Any]
) -> None:
    vip, foreign = make_tag(crm.a, "VIP"), make_tag(crm.b, "De B")
    vera, sam, bea = signed(crm.vera), signed(crm.sam), signed(crm.bea)
    for key in ("lima", "sam", "plain", "deleted", "b"):  # fuera del alcance de vera
        assert reply(tag(vera, crm.ids[key], vip)) == NOT_FOUND, key
        assert reply(untag(vera, crm.ids[key], vip)) == NOT_FOUND, key
    for tag_id in (foreign, uuid4()):  # la etiqueta de B no existe en A
        assert reply(tag(vera, crm.ids["owned"], tag_id)) == NOT_FOUND
        assert reply(untag(vera, crm.ids["owned"], tag_id)) == NOT_FOUND
    for client in (sam, bea):  # el Supervisor solo ve; bea, nada
        assert reply(tag(client, crm.ids["sam"], vip)) == DENIED
        assert reply(untag(client, crm.ids["sam"], vip)) == DENIED
    give(crm.a, crm.m["bea"], {"contacts.update": "ORGANIZATION"})  # editar sin ver, tampoco
    assert reply(tag(bea, crm.ids["plain"], vip)) == DENIED
    assert reply(untag(bea, crm.ids["plain"], vip)) == DENIED
    assert links(migrator) == [] and audit(migrator) == []
    give(crm.a, crm.m["sam"], {"contacts.update": "OWN"})
    assert reply(tag(sam, crm.ids["sam"], vip)) == (204, b"")  # lo suyo, sí


def test_an_inactive_tag_stays_where_it_is_but_is_not_attached_again(
    crm: SimpleNamespace, migrator: psycopg.Connection[Any]
) -> None:
    ana, old = signed(crm.ana), make_tag(crm.a, "Antiguo")
    assert tag(ana, crm.ids["plain"], old).status_code == 204
    assert tag(ana, crm.ids["owned"], old).status_code == 204
    send(ana, "patch", f"/api/v1/o/org-a/tags/{old}/", {"is_active": False})
    refused = tag(ana, crm.ids["lima"], old)
    assert refused.status_code == 409 and refused.json()["code"] == "TAG_INACTIVE"
    assert reply(tag(ana, crm.ids["plain"], old)) == (204, b"")  # ya la llevaba: nada
    shown = ana.get(f"{CONTACTS}{crm.ids['plain']}/").json()["tags"]
    assert [(row["name"], row["is_active"]) for row in shown] == [("Antiguo", False)]
    assert found(ana, tag=str(old)) == {crm.ids["plain"], crm.ids["owned"]}  # también la mira
    assert untag(ana, crm.ids["plain"], old).status_code == 204  # quitarla, sí
    assert links(migrator) == [(crm.ids["owned"], old, crm.ana.pk)]  # solo de ese contacto


def test_tags_come_by_name_with_every_contact_and_writes_reply_with_them(
    crm: SimpleNamespace,
) -> None:
    ana, plain = signed(crm.ana), crm.ids["plain"]
    for name in ("vip", "Mayorista", "alfa"):
        assert tag(ana, plain, make_tag(crm.a, name)).status_code == 204
    by_name = ["alfa", "Mayorista", "vip"]  # sin distinguir mayúsculas
    assert tags_of(ana, plain) == by_name
    rows = {row["id"]: row["tags"] for row in ana.get(CONTACTS).json()["results"]}
    assert [t["name"] for t in rows[str(plain)]] == by_name
    assert rows[str(crm.ids["owned"])] == []
    edited = patch(ana, plain, {"first_name": "Ana", "version": 1})
    assert [t["name"] for t in edited.json()["tags"]] == by_name


def test_the_list_filters_by_tag_inside_the_scope_and_with_q(crm: SimpleNamespace) -> None:
    ana, vera = signed(crm.ana), signed(crm.vera)
    vip, other = make_tag(crm.a, "VIP"), make_tag(crm.a, "Otro")
    for key in ("owned", "lima", "plain"):
        assert tag(ana, crm.ids[key], vip).status_code == 204
    assert tag(ana, crm.ids["created"], other).status_code == 204
    assert found(ana, tag=str(vip)) == {crm.ids[k] for k in ("owned", "lima", "plain")}
    assert found(vera, tag=str(vip)) == {crm.ids["owned"]}  # lo demás no es suyo
    assert found(ana, tag=str(vip), q="lima") == {crm.ids["lima"]}
    assert found(ana, tag=str(vip), q="Creado") == set()
    assert found(ana, tag=str(make_tag(crm.b, "De B"))) == set()  # de otra organización
    assert found(ana, tag=str(uuid4())) == set()
    for query in (f"tag={vip}&tag={other}", "tag=x", "tag="):
        response = ana.get(f"{CONTACTS}?{query}")
        assert response.status_code == 400, query
        assert set(response.json()["fields"]) == {"tag"}, query


def test_the_list_costs_the_same_queries_with_tags_on_one_row_or_many(
    crm: SimpleNamespace,
) -> None:
    ana = signed(crm.ana)
    for name in ("A", "B"):
        tag_id = make_tag(crm.a, name)
        for key in ("owned", "created", "lima", "cusco", "sam", "plain"):
            assert tag(ana, crm.ids[key], tag_id).status_code == 204
    with CaptureQueriesContext(connection) as one:
        assert len(ana.get(f"{CONTACTS}?limit=1").json()["results"][0]["tags"]) == 2
    with CaptureQueriesContext(connection) as many:
        assert len(ana.get(f"{CONTACTS}?limit=6").json()["results"]) == 6
    assert len(one) == len(many)


def test_the_database_refuses_a_tag_or_contact_of_another_organization(
    crm: SimpleNamespace,
) -> None:
    mine, foreign = make_tag(crm.a, "VIP"), make_tag(crm.b, "De B")
    with tenant_scope(ctx(crm.a)):
        for contact_id, tag_id in ((crm.ids["plain"], foreign), (crm.ids["b"], mine)):
            with (
                pytest.raises(IntegrityError, match="contact_tags_(tag|contact)_fk"),
                transaction.atomic(),
            ):
                ContactTag.objects.create(contact_id=contact_id, tag_id=tag_id)
        with pytest.raises(IntegrityError, match="contact_tags_added_by_fk"), transaction.atomic():
            ContactTag.objects.create(
                contact_id=crm.ids["plain"], tag_id=mine, added_by_user_id=crm.luis.pk
            )


def test_two_tabs_tagging_at_once_wait_for_the_contact_and_one_writes(
    crm: SimpleNamespace, migrator: psycopg.Connection[Any]
) -> None:
    """Las dos esperan la fila del contacto: al soltarla, una la pone y la otra ya la ve. Si
    mientras tanto el contacto deja de ser suyo, 404 sin escribir."""
    vip, owned, outcome = make_tag(crm.a, "VIP"), crm.ids["owned"], dict[str, Any]()

    def put(name: str) -> Any:
        return lambda: outcome.update({name: reply(tag(signed(crm.vera), owned, vip))})

    held(migrator, owned, [put("uno"), put("dos")], "")
    assert outcome == {"uno": (204, b""), "dos": (204, b"")}
    assert links(migrator) == [(owned, vip, crm.vera.pk)] and len(audit(migrator)) == 1
    lost = "UPDATE contacts SET owner_user_id = NULL WHERE id = %s"
    held(migrator, owned, [put("tarde")], lost)
    assert outcome["tarde"] == NOT_FOUND and len(audit(migrator)) == 1

"""F4-11: el catálogo de etiquetas de contactos (`…/tags/`) y `settings.manage`. Middleware,
sesión, motor de autorización y PostgreSQL con el rol `crm_app`."""

from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from django.test import Client

from apps.access.catalog import BY_CODE, ROLE_TEMPLATES
from apps.contacts.models import Tag
from core.tenancy.scope import tenant_scope
from tests import test_contacts_api, test_self_context
from tests.test_anti_escalation import audit
from tests.test_authorization import give
from tests.test_member_branch import DENIED
from tests.test_memberships import ctx
from tests.test_self_context import NOT_FOUND, reply, signed
from tests.test_team_writes import send

crm, real_stack = test_contacts_api.crm, test_self_context.real_stack
pytestmark = pytest.mark.usefixtures("tenant_db")
TAGS = "/api/v1/o/org-a/tags/"
TAG_FIELDS = {"id", "name", "color", "applies_to", "is_active"}


def make_tag(org: UUID, name: str, color: str = "#1f9d55", **fields: Any) -> UUID:
    with tenant_scope(ctx(org)):
        made: Tag = Tag.objects.create(
            name=name, color=color, applies_to=Tag.Target.CONTACT, **fields
        )
    return made.pk


def create(client: Client, body: Any, org: str = "org-a") -> Any:
    return send(client, "post", f"/api/v1/o/{org}/tags/", body)


def patch(client: Client, tag_id: Any, body: Any) -> Any:
    return send(client, "patch", f"{TAGS}{tag_id}/", body)


def listed(client: Client) -> dict[str, dict[str, Any]]:
    response = client.get(TAGS)
    assert response.status_code == 200, response.content
    return {row["name"]: row for row in response.json()["results"]}


def test_settings_manage_is_an_unscoped_organization_permission_of_owner_and_admin() -> None:
    permission = BY_CODE["settings.manage"]
    assert (permission.module, permission.is_sensitive, permission.supports_scope) == (
        "organization",
        False,
        False,
    )
    holders = {t.code for t in ROLE_TEMPLATES if "settings.manage" in t.grants}
    assert holders == {"owner", "admin"}


def test_create_rename_recolor_and_deactivate_with_audit(
    crm: SimpleNamespace, migrator: psycopg.Connection[Any]
) -> None:
    ana = signed(crm.ana)
    made = create(ana, {"name": "  VIP  ", "color": "#1F9D55"})
    assert made.status_code == 201, made.content
    body = made.json()
    assert set(body) == TAG_FIELDS
    assert (body["name"], body["color"], body["applies_to"], body["is_active"]) == (
        "VIP",
        "#1f9d55",  # en minúsculas
        "CONTACT",
        True,
    )
    tag = UUID(body["id"])
    changed = patch(ana, tag, {"name": "Cliente VIP", "color": "#AA0000", "is_active": False})
    assert changed.status_code == 200, changed.content
    assert changed.json() == {**body, "name": "Cliente VIP", "color": "#aa0000", "is_active": False}
    assert patch(ana, tag, {"name": "Cliente VIP", "is_active": False}).status_code == 200
    assert audit(migrator) == [  # lo que no cambió no se escribe ni se audita
        (
            "tag.created",
            "tag",
            tag,
            {"name": [None, "VIP"], "color": [None, "#1f9d55"]},
            {},
            crm.ana.pk,
        ),
        (
            "tag.updated",
            "tag",
            tag,
            {
                "name": ["VIP", "Cliente VIP"],
                "color": ["#1f9d55", "#aa0000"],
                "is_active": [True, False],
            },
            {},
            crm.ana.pk,
        ),
    ]
    assert listed(ana)["Cliente VIP"]["is_active"] is False  # sigue en el catálogo
    assert patch(ana, tag, {"is_active": True}).json()["is_active"] is True


def test_names_are_unique_without_case_also_against_inactive_tags(
    crm: SimpleNamespace, migrator: psycopg.Connection[Any]
) -> None:
    ana = signed(crm.ana)
    make_tag(crm.a, "Mayorista", is_active=False)
    other = make_tag(crm.a, "Minorista")
    for name in ("mayorista", "MAYORISTA", " Mayorista "):
        taken = create(ana, {"name": name, "color": "#000000"})
        assert taken.status_code == 409 and taken.json()["code"] == "TAG_NAME_TAKEN", name
    renamed = patch(ana, other, {"name": "mayoRISTA"})
    assert renamed.status_code == 409 and renamed.json()["code"] == "TAG_NAME_TAKEN"
    assert patch(ana, other, {"name": "minorista"}).json()["name"] == "minorista"  # la propia
    give(crm.b, crm.m_luis, {"settings.manage": None})
    assert (
        create(signed(crm.luis), {"name": "Mayorista", "color": "#000000"}, "org-b").status_code
        == 201
    )
    assert [row[0] for row in audit(migrator)] == ["tag.updated", "tag.created"]


def test_name_and_color_are_validated_without_echoing_them(crm: SimpleNamespace) -> None:
    ana = signed(crm.ana)
    for body, field in (
        ({"name": "   ", "color": "#000000"}, "name"),
        ({"name": "x" * 51, "color": "#000000"}, "name"),
        ({"name": "a\nb", "color": "#000000"}, "name"),
        ({"name": "Rojo", "color": "red"}, "color"),
        ({"name": "Rojo", "color": "#ff00001"}, "color"),
        ({"name": "Rojo", "color": "#ff000"}, "color"),
        ({"name": "Rojo"}, "color"),
    ):
        response = create(ana, body)
        assert response.status_code == 400, body
        assert set(response.json()["fields"]) == {field}, body
    assert listed(ana) == {}


def test_anyone_who_sees_contacts_lists_tags_only_settings_manage_changes_them(
    crm: SimpleNamespace, migrator: psycopg.Connection[Any]
) -> None:
    vip = make_tag(crm.a, "VIP")
    make_tag(crm.a, "Antiguo", is_active=False)
    make_tag(crm.b, "De B")
    for user in (crm.vera, crm.sam):  # `OWN` y `TEAM`: el catálogo entero, nada de B
        assert set(listed(signed(user))) == {"VIP", "Antiguo"}
    vera, bea = signed(crm.vera), signed(crm.bea)
    assert reply(bea.get(TAGS)) == DENIED  # sin `contacts.view`
    for client in (vera, bea):
        assert reply(create(client, {"name": "Nuevo", "color": "#000000"})) == DENIED
        for tag in (vip, uuid4()):
            assert reply(patch(client, tag, {"name": "Otro"})) == DENIED
    give(crm.a, crm.m["bea"], {"settings.manage": None})  # cambiarlas no da leerlas
    assert reply(bea.get(TAGS)) == DENIED
    assert create(bea, {"name": "Nuevo", "color": "#000000"}).status_code == 201
    assert not [row for row in audit(migrator) if row[0] != "tag.created"]


def test_a_tag_of_another_organization_does_not_exist(crm: SimpleNamespace) -> None:
    foreign, ana = make_tag(crm.b, "De B"), signed(crm.ana)
    assert reply(patch(ana, foreign, {"name": "Mía"})) == NOT_FOUND
    assert reply(patch(ana, uuid4(), {"name": "Mía"})) == NOT_FOUND
    with tenant_scope(ctx(crm.b)):
        assert Tag.objects.get(pk=foreign).name == "De B"

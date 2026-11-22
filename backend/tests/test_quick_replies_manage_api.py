"""F6-08a3: gestionar respuestas rápidas por HTTP (`…/quick-replies/`). Middleware, sesión, motor
de autorización y PostgreSQL con el rol `crm_app`; los comandos se prueban en
`test_quick_replies`."""

from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from apps.conversations import quick_replies as qr
from apps.conversations.models import QuickReplyCategory
from apps.conversations.seed import ensure_quick_reply_categories
from core.tenancy.scope import tenant_scope
from tests import test_contacts_api, test_self_context
from tests.test_authorization import give
from tests.test_member_branch import DENIED
from tests.test_memberships import ctx
from tests.test_pipelines_write_api import CSRF_FAILED, administrator
from tests.test_products import send
from tests.test_self_context import NOT_FOUND, reply, signed

crm, real_stack = test_contacts_api.crm, test_self_context.real_stack
pytestmark = pytest.mark.usefixtures("tenant_db")
DB = psycopg.Connection[Any]
LIST, B_LIST = "/api/v1/o/org-a/quick-replies/", "/api/v1/o/org-b/quick-replies/"
FIELDS = {
    "id", "shortcut", "title", "category_id", "body", "is_active", "usage_count", "version",
    "created_at", "updated_at",
}  # fmt: skip
# Con un salto final, que no se recorta.
TEXT = "Hola {{contact.first_name}}, enviamos a {{ contact.name }} de {{org.name}}.\n"


@pytest.fixture
def world(crm: SimpleNamespace) -> SimpleNamespace:
    for org in (crm.a, crm.b):
        with tenant_scope(ctx(org)) as tenant:
            ensure_quick_reply_categories(tenant)
    return crm


def category(world: SimpleNamespace, name: str = "Delivery", org: str = "a") -> str:
    with tenant_scope(ctx(getattr(world, org))):
        return str(QuickReplyCategory.objects.get(name=name).pk)


def at(reply_id: Any, org: str = "org-a") -> str:
    return f"/api/v1/o/{org}/quick-replies/{reply_id}/"


def create(world: SimpleNamespace, client: Any, url: str = LIST, **fields: Any) -> Any:
    wanted = {"shortcut": "delivery", "title": "Delivery", "body": TEXT, **fields}
    wanted.setdefault("category_id", category(world))
    return send(client, "post", url, wanted)


def change(client: Any, reply_id: Any, version: Any = 1, **fields: Any) -> Any:
    return send(client, "patch", at(reply_id), {"version": version, **fields})


def listed(client: Any, url: str = LIST) -> list[dict[str, Any]]:
    response = client.get(url)
    assert response.status_code == 200, response.content
    found: list[dict[str, Any]] = response.json()["results"]
    return found


def stored(migrator: DB) -> tuple[Any, ...]:
    found = migrator.execute(
        "SELECT (SELECT count(*) FROM quick_replies), (SELECT max(version) FROM quick_replies), "
        "(SELECT count(*) FROM audit_logs WHERE entity_type LIKE 'quick_reply%')"
    ).fetchone()
    return tuple(found or ())


def test_the_owner_creates_delivery_in_delivery_changes_lists_and_deletes_it(
    world: SimpleNamespace, migrator: DB
) -> None:
    ana = signed(world.ana)
    made = create(world, ana, shortcut=" DeLivery ")
    assert made.status_code == 201, made.content
    new = made.json()
    assert set(new) == FIELDS
    assert [new[k] for k in ("shortcut", "category_id", "body", "is_active", "version")] == [
        "delivery", category(world), TEXT, True, 1,
    ]  # fmt: skip
    done = change(ana, new["id"], title=" Envío  hoy ", is_active=False)
    assert done.status_code == 200, done.content
    got = done.json()
    assert (got["title"], got["is_active"], got["version"]) == ("Envío hoy", False, 2)
    assert listed(ana) == [got]  # la inactiva también se lista
    before = stored(migrator)
    stale = change(ana, new["id"], version=1, title="Pisada")
    assert (stale.status_code, stale.json()["code"]) == (409, "STALE_VERSION")
    for bad in ({"title": "x"}, {"version": "2"}, {"version": 0}, {"version": True}):
        assert list(send(ana, "patch", at(new["id"]), bad).json()["fields"]) == ["version"], bad
    same = change(ana, new["id"], version=2, title="Envío hoy")  # sin cambios: nada escrito
    assert (same.status_code, same.json()["version"], stored(migrator)) == (200, 2, before)
    assert reply(send(ana, "delete", at(new["id"]))) == (204, b"")
    assert listed(ana) == []
    assert reply(send(ana, "delete", at(new["id"]))) == NOT_FOUND  # ya borrada
    assert reply(change(ana, new["id"], version=3, title="X")) == NOT_FOUND
    rows = migrator.execute(
        "SELECT action, actor_id, changes FROM audit_logs WHERE entity_type = 'quick_reply' "
        "ORDER BY occurred_at, id"
    ).fetchall()
    assert [(action, actor) for action, actor, _ in rows] == [
        ("quick_reply.created", world.ana.pk),
        ("quick_reply.updated", world.ana.pk),
        ("quick_reply.deleted", world.ana.pk),
    ]
    assert rows[0][2]["body_fingerprint"] == [None, qr.fingerprint(TEXT)]
    assert rows[1][2]["is_active"] == [True, False] and "deleted_at" in rows[2][2]
    assert "enviamos" not in str(rows)  # el texto nunca va a la auditoría


def test_only_quick_replies_manage_reads_and_writes(world: SimpleNamespace, migrator: DB) -> None:
    mine = create(world, signed(world.ana)).json()["id"]
    give(world.a, world.m["bea"], {"quick_replies.manage": None})  # solo gestionar
    for user, shortcut in ((administrator(world), "admin"), (world.bea, "bea")):
        assert create(world, signed(user), shortcut=shortcut).status_code == 201, shortcut
    assert len(listed(signed(world.bea))) == 3
    assert change(signed(world.bea), mine, title="Bea").status_code == 200
    before = stored(migrator)
    with CaptureQueriesContext(connection) as captured:
        for user in (world.vera, world.sam):  # Vendedor y Supervisor: usar no es gestionar
            client = signed(user)
            for denied in (
                client.get(LIST),
                send(client, "post", LIST, {"shortcut": 5, "organization_id": str(world.b)}),
                change(client, mine, version=2, title="No"),
                send(client, "delete", at(mine)),
            ):
                assert reply(denied) == DENIED, user.email
    assert not [q for q in captured.captured_queries if "quick_repl" in q["sql"]]  # ni leyó
    assert stored(migrator) == before  # lo denegado no escribió ni auditó


def test_another_organization_anonymous_csrf_and_what_does_not_exist(
    world: SimpleNamespace, migrator: DB
) -> None:
    mine = create(world, signed(world.ana)).json()["id"]
    gone = create(world, signed(world.ana), shortcut="gone").json()["id"]
    assert send(signed(world.ana), "delete", at(gone)).status_code == 204
    give(world.b, world.m_luis, {"quick_replies.manage": None})
    luis = signed(world.luis)
    theirs = create(world, luis, B_LIST, category_id=category(world, org="b")).json()["id"]
    assert [r["id"] for r in listed(luis, B_LIST)] == [theirs]
    assert [r["id"] for r in listed(signed(world.ana))] == [mine]  # RLS: ni la de B ni la borrada
    before = stored(migrator)
    for missing in (
        send(luis, "patch", at(mine, "org-b"), {"version": 1, "title": "Mío"}),
        send(luis, "delete", at(mine, "org-b")),
        change(signed(world.ana), theirs, title="Mío"),
        send(signed(world.ana), "delete", at(theirs)),
        change(signed(world.ana), uuid4()),
        luis.get(LIST),  # luis no es miembro de A
    ):
        assert reply(missing) == NOT_FOUND
    elsewhere = create(world, luis, B_LIST, shortcut="x")
    assert elsewhere.status_code == 400  # la categoría de A no existe para B
    assert list(elsewhere.json()["fields"]) == ["category_id"]
    anonymous = signed(world.ana)
    anonymous.cookies.clear()
    assert reply(anonymous.get(LIST)) == (401, b'{"code":"NOT_AUTHENTICATED"}')
    ana = signed(world.ana)  # con sesión, sin la cabecera CSRF
    assert reply(ana.post(LIST, {}, "application/json")) == CSRF_FAILED
    assert reply(ana.delete(at(mine))) == CSRF_FAILED
    assert stored(migrator) == before


def test_the_shortcut_is_unique_among_the_active(world: SimpleNamespace) -> None:
    ana = signed(world.ana)
    first = create(world, ana).json()
    taken = create(world, ana, shortcut="DELIVERY")
    assert (taken.status_code, taken.json()["code"]) == (409, "SHORTCUT_TAKEN")
    other = create(world, ana, shortcut="otro").json()
    assert change(ana, other["id"], shortcut="delivery").json()["code"] == "SHORTCUT_TAKEN"
    assert change(ana, first["id"], is_active=False).status_code == 200
    second = create(world, ana)  # desactivada, deja el atajo libre
    assert second.status_code == 201, second.content
    back = change(ana, first["id"], version=2, is_active=True)  # reactivarla choca
    assert (back.status_code, back.json()["code"]) == (409, "SHORTCUT_TAKEN")


BAD_BODIES = [
    "{{contact.phone}}", "Hola {{nombre}}", "{{ contact.name", "solo }}", "{{}}",
    "{{{contact.name}}}", "{{{", "}}}", "a {{org.name}}}", "{{contact. name}}",
]  # fmt: skip


@pytest.mark.parametrize("body", BAD_BODIES)
def test_a_body_with_a_bad_variable_is_400_on_body_and_writes_nothing(
    world: SimpleNamespace, migrator: DB, body: str
) -> None:
    ana = signed(world.ana)
    mine = create(world, ana).json()["id"]
    before = stored(migrator)
    for refused in (create(world, ana, shortcut="otra", body=body), change(ana, mine, body=body)):
        assert refused.status_code == 400, refused.content
        assert list(refused.json()["fields"]) == ["body"]
        assert body not in refused.content.decode()  # el error no repite lo recibido
    assert stored(migrator) == before


BAD = [  # (lo que se cambia del cuerpo válido, el campo del 400)
    ({"shortcut": "/delivery"}, "shortcut"), ({"shortcut": "deli very"}, "shortcut"),
    ({"shortcut": "ñandú"}, "shortcut"), ({"shortcut": 5}, "shortcut"),
    ({"title": "!!!"}, "title"), ({"title": None}, "title"), ({"category_id": 5}, "category_id"),
    ({"category_id": "no"}, "category_id"), ({"category_id": str(uuid4())}, "category_id"),
    ({"body": " \u200b "}, "body"), ({"body": "a\u202eb"}, "body"), ({"body": "x" * 4097}, "body"),
    ({"organization_id": "x"}, "organization_id"), ({"usage_count": 9}, "usage_count"),
]  # fmt: skip


@pytest.mark.parametrize(("bad", "field"), BAD)
def test_a_bad_field_is_400_on_that_field(
    world: SimpleNamespace, migrator: DB, bad: dict[str, Any], field: str
) -> None:
    before = stored(migrator)
    refused = create(world, signed(world.ana), **bad)
    assert refused.status_code == 400, refused.content
    assert list(refused.json()["fields"]) == [field]
    assert stored(migrator) == before


def test_an_active_reply_never_lands_in_an_inactive_category(
    world: SimpleNamespace, migrator: DB
) -> None:
    ana = signed(world.ana)
    active = create(world, ana).json()
    asleep = create(world, ana, shortcut="dormida").json()
    elsewhere = create(world, ana, shortcut="otra", category_id=category(world, "Pagos")).json()
    assert change(ana, asleep["id"], is_active=False).status_code == 200
    delivery = UUID(category(world))
    with tenant_scope(ctx(world.a)):  # sin el comando: desde F6-08a4 no se desactiva con activas
        QuickReplyCategory.objects.filter(pk=delivery).update(is_active=False)
    before = stored(migrator)
    for refused, field in (
        (create(world, ana, shortcut="nueva"), "category_id"),
        (change(ana, asleep["id"], version=2, is_active=True), "is_active"),
        (change(ana, elsewhere["id"], category_id=category(world)), "category_id"),
    ):
        assert refused.status_code == 400, refused.content
        assert list(refused.json()["fields"]) == [field]
        assert "desactivada" in refused.content.decode()
    assert stored(migrator) == before
    edited = change(ana, active["id"], body="Nuevo texto")  # editarla donde está, sí
    assert edited.status_code == 200, edited.content
    moved = change(ana, asleep["id"], version=2, category_id=category(world, "Pagos"))
    assert moved.status_code == 200  # inactiva, se mueve; y en una activa se reactiva
    assert change(ana, asleep["id"], version=3, is_active=True).json()["is_active"] is True

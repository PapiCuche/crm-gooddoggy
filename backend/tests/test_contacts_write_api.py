"""F4-03: `POST …/contacts/`, `PATCH` y `DELETE …/contacts/{id}/`, con sus permisos y su alcance.
Middleware, sesión, motor de autorización y PostgreSQL con el rol `crm_app`."""

import threading
import time
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from django.db import connection
from django.test import Client

from apps.contacts.models import DocumentType
from apps.organizations.models import OrganizationMembership
from core.tenancy.scope import tenant_scope
from tests import test_contacts_api, test_self_context
from tests.conftest import migrator_conninfo
from tests.test_anti_escalation import audit
from tests.test_authorization import give
from tests.test_contacts_api import CONTACTS, FIELDS, contact
from tests.test_member_branch import DENIED
from tests.test_memberships import ctx
from tests.test_self_context import NOT_FOUND, reply, signed
from tests.test_team_writes import send

crm, real_stack = test_contacts_api.crm, test_self_context.real_stack
pytestmark = pytest.mark.usefixtures("tenant_db")
ROW = "SELECT version, xmin::text, updated_by_user_id, deleted_at, deleted_by_user_id FROM contacts"
NOT_OURS = {  # nada de esto se toma del cliente
    "organization_id": str(uuid4()),
    "owner_user_id": str(uuid4()),
    "branch_id": str(uuid4()),
    "created_by_user_id": str(uuid4()),
}


def create(client: Client, body: Any) -> Any:
    return send(client, "post", CONTACTS, body)


def patch(client: Client, contact_id: Any, body: Any) -> Any:
    return send(client, "patch", f"{CONTACTS}{contact_id}/", body)


def delete(client: Client, contact_id: Any) -> Any:
    return send(client, "delete", f"{CONTACTS}{contact_id}/", None)


def row(migrator: psycopg.Connection[Any], contact_id: Any) -> Any:
    return migrator.execute(f"{ROW} WHERE id = %s", [contact_id]).fetchone()


def in_lima(crm: SimpleNamespace, name: str) -> None:
    with tenant_scope(ctx(crm.a)):
        OrganizationMembership.objects.filter(pk=crm.m[name]).update(default_branch=crm.lima)


def test_the_creator_owns_the_contact_in_their_branch_and_organization(
    crm: SimpleNamespace, migrator: psycopg.Connection[Any]
) -> None:
    vera = signed(crm.vera)
    sent = {
        "first_name": " María ",
        "last_name": "Quispe",
        "primary_phone_e164": "0051 987 654 321",
        "primary_email": " maria@example.com ",
        "document_type": "DNI",
        "document_number": "12345678",
        "source": "WALK_IN",
        "version": 7,
        **NOT_OURS,
    }
    made = create(vera, sent)
    assert made.status_code == 201, made.content
    body = made.json()
    assert set(body) == FIELDS
    mine = str(crm.vera.pk)
    assert {k: body[k] for k in FIELDS - {"id", "created_at", "updated_at"}} == {
        "display_name": "María Quispe",
        "first_name": "María",
        "last_name": "Quispe",
        "primary_phone_e164": "+51987654321",
        "primary_email": "maria@example.com",
        "document_type": "DNI",
        "document_number": "12345678",
        "owner_user_id": mine,
        "branch_id": None,  # vera no tiene sucursal
        "source": "WALK_IN",
        "created_by_user_id": mine,
        "version": 1,
        "tags": [],  # F4-11
    }
    found = "SELECT organization_id FROM contacts WHERE id = %s"
    assert migrator.execute(found, [body["id"]]).fetchone() == (crm.a,)
    changes = {k: [None, body[k]] for k in ("display_name", "first_name", "last_name")}
    changes |= {k: [None, body[k]] for k in ("primary_phone_e164", "primary_email")}
    changes |= {"document_type": [None, "DNI"], "document_number": [None, "*****678"]}
    changes |= {"source": [None, "WALK_IN"], "owner_user_id": [None, mine]}
    made_id = UUID(body["id"])
    assert audit(migrator)[-1] == ("contact.created", "contact", made_id, changes, {}, crm.vera.pk)
    assert vera.get(f"{CONTACTS}{body['id']}/").json() == body  # lo ve: es suyo
    in_lima(crm, "vera")
    lima = create(vera, {"display_name": "En Lima"}).json()
    assert (lima["branch_id"], lima["source"]) == (str(crm.lima.pk), "MANUAL")


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({"primary_phone_e164": "987654321"}, "primary_phone_e164"),
        ({"primary_phone_e164": "+５１987654321"}, "primary_phone_e164"),
        ({"primary_email": "ana@"}, "primary_email"),
        ({"document_type": "DNI", "document_number": "1234"}, "document_number"),
        ({"document_number": "12345678"}, "document_number"),
        ({"document_type": "RUC"}, "document_type"),
        ({"document_type": "12345678"}, "document_type"),
        ({"source": "+51987654321"}, "source"),
        ({"display_name": " - "}, "display_name"),
        ({"display_name": "", "first_name": "Ana\nMaría"}, "first_name"),
        ({"display_name": "", "first_name": " ", "last_name": ""}, "display_name"),
    ],
)
def test_a_bad_field_is_a_400_that_names_it_without_echoing_it(
    crm: SimpleNamespace, migrator: psycopg.Connection[Any], body: dict[str, Any], field: str
) -> None:
    answer = create(signed(crm.vera), {"display_name": "Ana", **body})
    assert answer.status_code == 400
    assert (answer.json()["code"], set(answer.json()["fields"])) == ("VALIDATION_ERROR", {field})
    for key in ("primary_phone_e164", "primary_email", "document_number", "document_type"):
        value = body.get(key)  # un tipo válido («DNI») sí puede salir en el mensaje
        assert not value or value in DocumentType.values or value not in answer.content.decode()
    assert "+51987654321" not in answer.content.decode()  # tampoco como `source`
    assert migrator.execute("SELECT count(*) FROM contacts").fetchone() == (8,)


def test_each_write_needs_its_permission_and_editing_needs_seeing(crm: SimpleNamespace) -> None:
    bea, vera = signed(crm.bea), signed(crm.vera)
    for nobody in (bea, signed(crm.sam)):  # el Supervisor ve, pero no crea
        assert reply(create(nobody, {"display_name": "Ana"})) == DENIED
    for contact_id in (crm.ids["plain"], crm.ids["b"], uuid4()):
        assert reply(patch(bea, contact_id, {"version": 1})) == DENIED
        assert reply(delete(bea, contact_id)) == DENIED
    assert reply(delete(vera, crm.ids["owned"])) == DENIED  # la Vendedora no borra
    give(crm.a, crm.m["bea"], {"contacts.update": "ORGANIZATION", "contacts.delete": "OWN"})
    for contact_id in (crm.ids["plain"], uuid4()):  # sin `contacts.view`, ni edita ni borra
        assert reply(patch(bea, contact_id, {"version": 1, "display_name": "X"})) == DENIED
        assert reply(delete(bea, contact_id)) == DENIED


def test_a_seller_edits_their_own_and_gets_404_on_the_others(
    crm: SimpleNamespace, migrator: psycopg.Connection[Any]
) -> None:
    vera = signed(crm.vera)
    for key in ("owned", "created"):
        edited = patch(vera, crm.ids[key], {"version": 1, "last_name": "Suyo"}).json()
        assert (edited["last_name"], edited["version"]) == ("Suyo", 2)
    before = migrator.execute(ROW).fetchall()
    for contact_id in (*(crm.ids[k] for k in ("lima", "sam", "plain", "deleted", "b")), uuid4()):
        for version in (1, 9):  # una versión vieja tampoco delata que existe
            assert (
                reply(patch(vera, contact_id, {"version": version, "last_name": "X"})) == NOT_FOUND
            )
    assert migrator.execute(ROW).fetchall() == before


def test_the_scopes_of_view_and_update_intersect(crm: SimpleNamespace) -> None:
    """Ver un contacto no deja editarlo: vera ve Lima (`BRANCH`) pero edita con `OWN`; bea edita
    con `ORGANIZATION` pero ve con `OWN`. sam, `BRANCH` en los dos: su sucursal y lo suyo."""
    give(crm.a, crm.m["vera"], {"contacts.view": "BRANCH"})
    in_lima(crm, "vera")
    vera = signed(crm.vera)
    assert vera.get(f"{CONTACTS}{crm.ids['lima']}/").status_code == 200
    assert reply(patch(vera, crm.ids["lima"], {"version": 1})) == NOT_FOUND
    give(crm.a, crm.m["bea"], {"contacts.view": "OWN", "contacts.update": "ORGANIZATION"})
    bea, mine = signed(crm.bea), contact(crm.a, "De bea", owner_user_id=crm.bea.pk)
    assert reply(patch(bea, crm.ids["plain"], {"version": 1})) == NOT_FOUND
    assert patch(bea, mine, {"version": 1, "display_name": "Mío"}).status_code == 200
    give(crm.a, crm.m["sam"], {"contacts.view": "BRANCH", "contacts.update": "BRANCH"})
    in_lima(crm, "sam")
    sam = signed(crm.sam)
    for key in ("lima", "sam"):
        assert patch(sam, crm.ids[key], {"version": 1, "first_name": "L"}).status_code == 200
    assert reply(patch(sam, crm.ids["cusco"], {"version": 1})) == NOT_FOUND
    ana = signed(crm.ana)
    for key in ("cusco", "plain"):
        assert patch(ana, crm.ids[key], {"version": 1, "first_name": "O"}).status_code == 200
    assert reply(patch(ana, crm.ids["b"], {"version": 1})) == NOT_FOUND


def test_an_edit_audits_the_diff_masks_the_document_and_ignores_what_is_not_its_own(
    crm: SimpleNamespace, migrator: psycopg.Connection[Any]
) -> None:
    vera, owned = signed(crm.vera), crm.ids["owned"]
    body = {"version": 1, "display_name": "Vera Cliente", "primary_phone_e164": "+51 911 222 333"}
    body |= {"document_type": "RUC", "document_number": "20123456789", **NOT_OURS}
    edited = patch(vera, owned, body).json()
    assert (edited["owner_user_id"], edited["branch_id"]) == (str(crm.vera.pk), None)
    assert (edited["created_by_user_id"], edited["version"]) == (str(crm.ana.pk), 2)
    changes = {
        "display_name": ["De vera", "Vera Cliente"],
        "primary_phone_e164": [None, "+51911222333"],
        "document_type": [None, "RUC"],
        "document_number": [None, "********789"],
    }
    assert audit(migrator)[-1] == ("contact.updated", "contact", owned, changes, {}, crm.vera.pk)
    assert row(migrator, owned)[2] == crm.vera.pk  # quién lo cambió


def test_a_stale_version_is_a_409_and_no_change_writes_nothing(
    crm: SimpleNamespace, migrator: psycopg.Connection[Any]
) -> None:
    vera, owned = signed(crm.vera), crm.ids["owned"]
    before, logged = row(migrator, owned), len(audit(migrator))
    for same in ({"version": 1}, {"version": 1, "display_name": " De  vera "}):
        assert patch(vera, owned, same).json()["version"] == 1
    assert (row(migrator, owned), len(audit(migrator))) == (before, logged)  # ni el xmin
    assert patch(vera, owned, {"version": 1, "first_name": "Uno"}).status_code == 200
    before, logged = row(migrator, owned), len(audit(migrator))
    assert patch(vera, owned, {"version": 1}).status_code == 409  # también sin cambios
    for version in (1, 3):
        answer = patch(vera, owned, {"version": version, "first_name": "Pisado"})
        assert (answer.status_code, answer.json()["code"]) == (409, "CONTACT_VERSION_CONFLICT")
    unversioned = patch(vera, owned, {"first_name": "Sin versión"}).json()["fields"]
    assert [error["code"] for error in unversioned["version"]] == ["required"]
    assert (row(migrator, owned), len(audit(migrator))) == (before, logged)


def test_a_document_that_does_not_match_the_stored_one_names_the_field(
    crm: SimpleNamespace, migrator: psycopg.Connection[Any]
) -> None:
    vera, owned = signed(crm.vera), crm.ids["owned"]
    alone = patch(vera, owned, {"version": 1, "document_number": "12345678"})
    assert alone.status_code == 400 and set(alone.json()["fields"]) == {"document_number"}
    assert "12345678" not in alone.content.decode()
    kind = {"version": 1, "document_type": "DNI", "document_number": "12345678"}
    assert patch(vera, owned, kind).status_code == 200
    other = patch(vera, owned, {"version": 2, "document_type": "RUC"})  # el número es de DNI
    assert other.status_code == 400 and set(other.json()["fields"]) == {"document_type"}
    phone = patch(vera, owned, {"version": 2, "primary_phone_e164": "987654321"})
    assert set(phone.json()["fields"]) == {"primary_phone_e164"}
    assert "987654321" not in phone.content.decode() and row(migrator, owned)[0] == 2


def test_delete_is_logical_scoped_audited_and_once(
    crm: SimpleNamespace, migrator: psycopg.Connection[Any]
) -> None:
    ana, plain = signed(crm.ana), crm.ids["plain"]
    assert delete(ana, plain).status_code == 204
    assert reply(delete(ana, plain)) == NOT_FOUND  # ya borrado
    assert reply(ana.get(f"{CONTACTS}{plain}/")) == NOT_FOUND
    assert reply(patch(ana, plain, {"version": 2})) == NOT_FOUND
    version, _, _, deleted_at, deleted_by = row(migrator, plain)
    assert (version, deleted_at is not None, deleted_by) == (2, True, crm.ana.pk)
    assert audit(migrator)[-1] == ("contact.deleted", "contact", plain, {}, {}, crm.ana.pk)
    assert reply(delete(ana, crm.ids["b"])) == NOT_FOUND
    give(crm.a, crm.m["bea"], {"contacts.view": "OWN", "contacts.delete": "OWN"})
    bea = signed(crm.bea)
    for key in ("lima", "owned", "sam"):
        assert reply(delete(bea, crm.ids[key])) == NOT_FOUND
        assert row(migrator, crm.ids[key])[3] is None
    assert delete(bea, contact(crm.a, "De bea", created_by_user_id=crm.bea.pk)).status_code == 204


def test_another_organization_never_reaches_the_contacts_of_a(crm: SimpleNamespace) -> None:
    every = ("contacts.view", "contacts.update", "contacts.delete")
    give(crm.b, crm.m_luis, {**dict.fromkeys(every, "ORGANIZATION"), "contacts.create": None})
    luis, theirs, plain = signed(crm.luis), "/api/v1/o/org-b/contacts/", crm.ids["plain"]
    for answer in (
        send(luis, "patch", f"{theirs}{plain}/", {"version": 1}),
        send(luis, "delete", f"{theirs}{plain}/", None),
        patch(luis, plain, {"version": 1}),  # en A no es miembro
        delete(luis, plain),
        create(luis, {"display_name": "Intruso"}),
    ):
        assert reply(answer) == NOT_FOUND
    assert send(luis, "post", theirs, {"display_name": "De B"}).status_code == 201
    assert send(luis, "patch", f"{theirs}{crm.ids['b']}/", {"version": 1}).status_code == 200


def held(
    migrator: psycopg.Connection[Any], contact_id: UUID, steps: list[Any], change: str
) -> None:
    """El migrador retiene la fila; cada paso, en su hilo, la espera; al soltarla ejecuta
    `change` (vacío: nada) y confirma."""

    def waiting(count: int) -> None:
        for _ in range(100):
            found = migrator.execute("SELECT count(DISTINCT pid) FROM pg_locks WHERE NOT granted")
            if found.fetchone() == (count,):
                return
            time.sleep(0.05)
        raise AssertionError("no esperó")

    def run(step: Any) -> None:
        try:
            step()
        finally:
            connection.close()

    threads = [threading.Thread(target=run, args=(step,)) for step in steps]
    with psycopg.connect(migrator_conninfo()) as holder:
        holder.execute("SELECT 1 FROM contacts WHERE id = %s FOR UPDATE", [contact_id])
        for count, thread in enumerate(threads, 1):
            thread.start()
            waiting(count)
        if change:
            holder.execute(change, [contact_id])
        holder.commit()
    for thread in threads:
        thread.join(timeout=15)


def test_the_scope_is_decided_on_the_locked_row(
    crm: SimpleNamespace, migrator: psycopg.Connection[Any]
) -> None:
    """Mientras vera edita y borra, el contacto pasa a otro responsable (F4-08): lo que esperaba
    la fila ya no lo alcanza y es 404, sin escribir."""
    give(crm.a, crm.m["vera"], {"contacts.delete": "OWN"})
    owned, outcome, vera = crm.ids["owned"], dict[str, Any](), signed(crm.vera)

    def edit() -> None:
        outcome["edit"] = reply(patch(vera, owned, {"version": 1, "first_name": "Tarde"}))

    def remove() -> None:
        outcome["delete"] = reply(delete(signed(crm.vera), owned))

    reassign = "UPDATE contacts SET owner_user_id = created_by_user_id WHERE id = %s"
    held(migrator, owned, [edit, remove], reassign)
    assert outcome == {"edit": NOT_FOUND, "delete": NOT_FOUND}
    assert row(migrator, owned)[0] == 1 and row(migrator, owned)[3] is None
    assert not audit(migrator)


def test_two_edits_of_the_same_version_one_wins_and_the_other_is_a_409(
    crm: SimpleNamespace, migrator: psycopg.Connection[Any]
) -> None:
    """Dos pestañas guardan la versión 1 a la vez y esperan la fila: al soltarla, una escribe y
    la otra encuentra la versión 2."""
    owned, outcome = crm.ids["owned"], dict[str, Any]()
    clients = {name: signed(crm.vera) for name in ("uno", "dos")}

    def edit(name: str) -> Any:
        def run() -> None:
            answer = patch(clients[name], owned, {"version": 1, "first_name": name})
            outcome[name] = (answer.status_code, answer.json().get("code"))

        return run

    held(migrator, owned, [edit(name) for name in clients], "")
    assert sorted(outcome.values(), key=str) == [(200, None), (409, "CONTACT_VERSION_CONFLICT")]
    winner = next(name for name, done in outcome.items() if done[0] == 200)
    updates = [entry[3] for entry in audit(migrator) if entry[0] == "contact.updated"]
    assert updates == [{"first_name": ["", winner]}] and row(migrator, owned)[0] == 2

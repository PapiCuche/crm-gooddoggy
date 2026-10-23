"""F4-08: `PUT …/contacts/{id}/assignment/` (`contacts.assign`): el responsable y la sucursal.
Middleware, sesión, motor de autorización y PostgreSQL con el rol `crm_app`; las carreras, con
hilos reales."""

import threading
import time
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from django.db import connection
from django.test import Client

from tests import test_contacts_api, test_self_context
from tests.conftest import migrator_conninfo
from tests.factories import make_user
from tests.test_anti_escalation import audit
from tests.test_authorization import give
from tests.test_branches import branch
from tests.test_contacts_api import CONTACTS
from tests.test_contacts_write_api import ROW, held, patch, row
from tests.test_member_branch import DENIED
from tests.test_member_status import status_of, switch
from tests.test_memberships import join
from tests.test_self_context import NOT_FOUND, reply, signed
from tests.test_team_writes import send

crm, real_stack = test_contacts_api.crm, test_self_context.real_stack
pytestmark = pytest.mark.usefixtures("tenant_db")
PLACE = "SELECT owner_user_id, branch_id, version, updated_by_user_id FROM contacts WHERE id = %s"


def assign(client: Client, contact_id: Any, body: Any, org: str = "org-a") -> Any:
    path = f"/api/v1/o/{org}/contacts/{contact_id}/assignment/"
    return send(client, "put", path, body)


def body(owner: Any, branch_id: Any, version: int = 1) -> dict[str, Any]:
    return {
        "owner_user_id": owner and str(owner),
        "branch_id": branch_id and str(branch_id),
        "version": version,
    }


def place(migrator: psycopg.Connection[Any], contact_id: UUID) -> Any:
    return migrator.execute(PLACE, [contact_id]).fetchone()


def test_assigning_changes_who_sees_the_contact_and_audits_only_what_changed(
    crm: SimpleNamespace, migrator: psycopg.Connection[Any]
) -> None:
    ana, vera, plain = signed(crm.ana), signed(crm.vera), crm.ids["plain"]
    assert reply(vera.get(f"{CONTACTS}{plain}/")) == NOT_FOUND
    done = assign(ana, plain, body(crm.vera.pk, crm.lima.pk))
    assert done.status_code == 200, done.content
    got = done.json()
    assert (got["owner_user_id"], got["branch_id"]) == (str(crm.vera.pk), str(crm.lima.pk))
    assert got["version"] == 2 and got["display_name"] == "Sin nadie"
    assert place(migrator, plain) == (crm.vera.pk, crm.lima.pk, 2, crm.ana.pk)
    changes = {"owner_user_id": [None, str(crm.vera.pk)], "branch_id": [None, str(crm.lima.pk)]}
    assert audit(migrator) == [("contact.assigned", "contact", plain, changes, {}, crm.ana.pk)]
    assert vera.get(f"{CONTACTS}{plain}/").json() == got  # ahora es suyo (OWN)
    again = assign(ana, plain, body(None, crm.lima.pk, 2))
    assert again.json()["owner_user_id"] is None and again.json()["version"] == 3
    unassigned = {"owner_user_id": [str(crm.vera.pk), None]}  # la sucursal no cambió
    assert audit(migrator)[-1] == ("contact.assigned", "contact", plain, unassigned, {}, crm.ana.pk)
    assert reply(vera.get(f"{CONTACTS}{plain}/")) == NOT_FOUND  # ya no es suyo


def test_without_contacts_assign_it_is_403_and_nothing_changes(
    crm: SimpleNamespace, migrator: psycopg.Connection[Any]
) -> None:
    before, wanted = migrator.execute(ROW).fetchall(), body(crm.vera.pk, None)
    assert assign(Client(), crm.ids["owned"], wanted).status_code == 401
    vera, bea = signed(crm.vera), signed(crm.bea)
    for contact_id in (crm.ids["owned"], crm.ids["plain"], crm.ids["b"], uuid4()):
        assert reply(assign(vera, contact_id, wanted)) == DENIED  # la Vendedora ve y edita
        assert reply(assign(bea, contact_id, wanted)) == DENIED  # sin rol
    give(crm.a, crm.m["bea"], {"contacts.assign": None, "contacts.update": "ORGANIZATION"})
    for contact_id in (crm.ids["plain"], uuid4()):  # asignar sin ver, tampoco
        assert reply(assign(bea, contact_id, wanted)) == DENIED
    assert migrator.execute(ROW).fetchall() == before and not audit(migrator)


def test_out_of_the_view_scope_deleted_or_foreign_is_the_same_404(
    crm: SimpleNamespace, migrator: psycopg.Connection[Any]
) -> None:
    give(crm.a, crm.m["vera"], {"contacts.assign": None})
    vera, before = signed(crm.vera), migrator.execute(ROW).fetchall()
    for key in ("lima", "sam", "plain", "deleted", "b"):
        for version in (1, 9):
            assert reply(assign(vera, crm.ids[key], body(crm.vera.pk, None, version))) == NOT_FOUND
    assert reply(assign(vera, uuid4(), body(crm.vera.pk, None))) == NOT_FOUND
    assert migrator.execute(ROW).fetchall() == before
    moved = assign(vera, crm.ids["created"], body(crm.sam.pk, crm.lima.pk))  # lo creó ella
    assert moved.status_code == 200 and moved.json()["owner_user_id"] == str(crm.sam.pk)
    every = ("contacts.view", "contacts.update", "contacts.delete")
    give(crm.b, crm.m_luis, {**dict.fromkeys(every, "ORGANIZATION"), "contacts.assign": None})
    luis = signed(crm.luis)
    assert reply(assign(luis, crm.ids["plain"], body(None, None))) == NOT_FOUND  # en A, nadie
    assert reply(assign(luis, crm.ids["plain"], body(None, None), "org-b")) == NOT_FOUND
    mine = assign(luis, crm.ids["b"], body(None, None), "org-b")
    assert mine.status_code == 200 and place(migrator, crm.ids["plain"])[2] == 1


def test_the_owner_must_be_an_active_member_and_the_branch_of_the_organization(
    crm: SimpleNamespace, migrator: psycopg.Connection[Any]
) -> None:
    ana, plain = signed(crm.ana), crm.ids["plain"]
    suspended = make_user(email="sus@example.com")
    join(crm.a, suspended, status="SUSPENDED")
    inactive = make_user(email="off@example.com", is_active=False)
    join(crm.a, inactive)
    foreign_branch = branch(crm.b, "LIM")
    bad = [
        (body(suspended.pk, None), "owner_user_id"),
        (body(inactive.pk, None), "owner_user_id"),
        (body(crm.luis.pk, None), "owner_user_id"),  # de B
        (body(uuid4(), None), "owner_user_id"),
        (body(crm.ana.pk, foreign_branch.pk), "branch_id"),
        (body(crm.ana.pk, uuid4()), "branch_id"),
        ({**body(None, None), "owner_user_id": "ana@example.com"}, "owner_user_id"),
        ({"branch_id": None, "version": 1}, "owner_user_id"),
        ({"owner_user_id": None, "version": 1}, "branch_id"),
        (body(None, None, 0), "version"),
    ]
    for sent, field in bad:
        answer = assign(ana, plain, sent)
        assert answer.status_code == 400, sent
        assert (answer.json()["code"], set(answer.json()["fields"])) == (
            "VALIDATION_ERROR",
            {field},
        )
        for value in sent.values():
            assert not isinstance(value, str) or value not in answer.content.decode()
    assert place(migrator, plain) == (None, None, 1, None) and not audit(migrator)


def test_only_what_changes_is_validated(
    crm: SimpleNamespace, migrator: psycopg.Connection[Any]
) -> None:
    """Un responsable que se suspendió después sigue siéndolo al cambiar solo la sucursal."""
    noa = make_user(email="noa@example.com")
    member = join(crm.a, noa).pk
    ana, plain = signed(crm.ana), crm.ids["plain"]
    assert assign(ana, plain, body(noa.pk, None)).status_code == 200
    assert switch(ana, member).status_code == 200
    moved = assign(ana, plain, body(noa.pk, crm.lima.pk, 2))
    assert moved.status_code == 200 and place(migrator, plain)[:3] == (noa.pk, crm.lima.pk, 3)
    assert audit(migrator)[-1][3] == {"branch_id": [None, str(crm.lima.pk)]}
    refused = assign(ana, plain, body(uuid4(), None, 3))
    assert set(refused.json()["fields"]) == {"owner_user_id"}


def test_a_stale_version_is_a_409_and_no_change_writes_nothing(
    crm: SimpleNamespace, migrator: psycopg.Connection[Any]
) -> None:
    ana, owned = signed(crm.ana), crm.ids["owned"]
    before, logged = row(migrator, owned), len(audit(migrator))
    same = assign(ana, owned, body(crm.vera.pk, None))
    assert same.status_code == 200 and same.json()["version"] == 1
    assert (row(migrator, owned), len(audit(migrator))) == (before, logged)  # ni el xmin
    assert patch(ana, owned, {"version": 1, "first_name": "Uno"}).status_code == 200
    before = row(migrator, owned)
    for version in (1, 3):
        answer = assign(ana, owned, body(crm.sam.pk, None, version))
        assert (answer.status_code, answer.json()["code"]) == (409, "CONTACT_VERSION_CONFLICT")
    assert reply(assign(ana, owned, body(crm.vera.pk, None))) == reply(answer)  # sin cambios
    assert row(migrator, owned) == before and len(audit(migrator)) == logged + 1


def test_an_edit_and_an_assignment_of_the_same_version_one_wins(
    crm: SimpleNamespace, migrator: psycopg.Connection[Any]
) -> None:
    """Las dos esperan la fila del contacto: al soltarla, una escribe y la otra es un 409."""
    owned, outcome = crm.ids["owned"], dict[str, Any]()

    def edit() -> None:
        answer = patch(signed(crm.ana), owned, {"version": 1, "first_name": "Editado"})
        outcome["edit"] = (answer.status_code, answer.json().get("code"))

    def move() -> None:
        answer = assign(signed(crm.ana), owned, body(crm.sam.pk, crm.lima.pk))
        outcome["assign"] = (answer.status_code, answer.json().get("code"))

    held(migrator, owned, [edit, move], "")
    assert sorted(outcome.values(), key=str) == [(200, None), (409, "CONTACT_VERSION_CONFLICT")]
    assert place(migrator, owned)[2] == 2 and len(audit(migrator)) == 1


def holding(lock: str, target: UUID, steps: list[Callable[[], None]], change: str = "") -> None:
    """Como `held`, sobre cualquier fila: el migrador la retiene con `lock`, cada paso la espera
    en su hilo (o espera a otro que la espera), y al soltarla ejecuta `change` y confirma."""

    def waiting(count: int) -> None:
        with psycopg.connect(migrator_conninfo(), autocommit=True) as watcher:
            for _ in range(100):
                found = watcher.execute(
                    "SELECT count(DISTINCT pid) FROM pg_locks WHERE NOT granted"
                )
                if found.fetchone() == (count,):
                    return
                time.sleep(0.05)
        raise AssertionError("no esperó")

    def run(step: Callable[[], None]) -> None:
        try:
            step()
        finally:
            connection.close()

    threads = [threading.Thread(target=run, args=(step,)) for step in steps]
    with psycopg.connect(migrator_conninfo()) as holder:
        holder.execute(lock, [target])
        for count, thread in enumerate(threads, 1):
            thread.start()
            waiting(count)
        if change:
            holder.execute(change, [target])
        holder.commit()
    for thread in threads:
        thread.join(timeout=15)
        assert not thread.is_alive()


def test_a_suspension_confirmed_while_waiting_is_seen(
    crm: SimpleNamespace, migrator: psycopg.Connection[Any]
) -> None:
    """La membresía de noa está bloqueada por su suspensión: la asignación la espera, con el
    contacto ya bloqueado, y al confirmarse la suspensión ve que noa ya no está activa."""
    noa = make_user(email="noa@example.com")
    member, plain, outcome = join(crm.a, noa).pk, crm.ids["plain"], dict[str, Any]()

    def move() -> None:
        answer = assign(signed(crm.ana), plain, body(noa.pk, None))
        outcome["assign"] = (answer.status_code, set(answer.json().get("fields", ())))

    lock = "SELECT 1 FROM organization_memberships WHERE id = %s FOR NO KEY UPDATE"
    suspend = "UPDATE organization_memberships SET status = 'SUSPENDED' WHERE id = %s"
    holding(lock, member, [move], suspend)
    assert outcome == {"assign": (400, {"owner_user_id"})}
    assert place(migrator, plain) == (None, None, 1, None) and not audit(migrator)


def test_a_suspension_waits_for_an_assignment_in_progress_without_deadlock(
    crm: SimpleNamespace, migrator: psycopg.Connection[Any]
) -> None:
    """La asignación ya bloqueó el contacto y la membresía de noa, y espera la sucursal (su FK);
    la suspensión de noa, con el bloqueo de RBAC, espera la membresía. Al soltar la sucursal
    terminan las dos: noa queda como responsable (estaba activa) y suspendida después."""
    noa = make_user(email="noa@example.com")
    member, plain, outcome = join(crm.a, noa).pk, crm.ids["plain"], dict[str, Any]()

    def move() -> None:
        outcome["assign"] = assign(signed(crm.ana), plain, body(noa.pk, crm.lima.pk)).status_code

    def suspend() -> None:
        outcome["suspend"] = switch(signed(crm.ana), member).status_code

    lock = "SELECT 1 FROM branches WHERE id = %s FOR UPDATE"
    holding(lock, crm.lima.pk, [move, suspend])
    assert outcome == {"assign": 200, "suspend": 200}
    assert place(migrator, plain)[:3] == (noa.pk, crm.lima.pk, 2)
    assert status_of(migrator, member) == "SUSPENDED"
    actions = [entry[0] for entry in audit(migrator)]
    assert actions == ["contact.assigned", "membership.suspended"]


def test_a_custom_role_can_be_given_it_and_it_still_needs_to_see(
    crm: SimpleNamespace,
) -> None:
    """sam (Supervisor, `contacts.view` `TEAM`, es decir `OWN`) con `contacts.assign` concedido:
    reasigna lo que creó, y lo de vera sigue siendo 404 aunque estén en el mismo equipo."""
    give(crm.a, crm.m["sam"], {"contacts.assign": None})
    sam = signed(crm.sam)
    moved = assign(sam, crm.ids["sam"], body(crm.vera.pk, None))  # lo creó sam: lo ve
    assert moved.status_code == 200
    assert reply(assign(sam, crm.ids["owned"], body(crm.sam.pk, None))) == NOT_FOUND


def test_the_view_scope_is_decided_on_the_locked_row(
    crm: SimpleNamespace, migrator: psycopg.Connection[Any]
) -> None:
    """Mientras vera reasigna lo suyo, el contacto pasa a otro responsable: al soltar la fila ya
    no lo ve, y es 404 sin escribir."""
    give(crm.a, crm.m["vera"], {"contacts.assign": None})
    owned, outcome = crm.ids["owned"], dict[str, Any]()

    def move() -> None:
        outcome["assign"] = reply(assign(signed(crm.vera), owned, body(crm.sam.pk, None)))

    held(migrator, owned, [move], "UPDATE contacts SET owner_user_id = NULL WHERE id = %s")
    assert outcome == {"assign": NOT_FOUND} and not audit(migrator)
    assert place(migrator, owned) == (None, None, 1, None)

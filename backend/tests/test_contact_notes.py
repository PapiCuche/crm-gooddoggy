"""F4-09: las notas de un cliente (`…/contacts/{id}/notes/`), con el alcance del contacto, la
regla del autor y la auditoría sin el cuerpo. PostgreSQL real con el rol `crm_app`."""

from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from django.db import IntegrityError

from apps.contacts.models import Note
from core.tenancy.scope import tenant_scope
from tests import test_contacts_api, test_self_context
from tests.test_anti_escalation import audit
from tests.test_authorization import give
from tests.test_contacts_write_api import held
from tests.test_member_branch import DENIED
from tests.test_memberships import ctx
from tests.test_self_context import NOT_FOUND, reply, signed
from tests.test_team_writes import send

crm, real_stack = test_contacts_api.crm, test_self_context.real_stack
pytestmark = pytest.mark.usefixtures("tenant_db")
type NS = SimpleNamespace
type DB = psycopg.Connection[Any]
ROW = "SELECT body, version, is_pinned, updated_by_user_id, deleted_by_user_id FROM notes"
PERSONAL = "DNI 12345678 de la clienta"
E_ACUTE = "e\N{COMBINING ACUTE ACCENT}"  # dos caracteres; en NFC, «\xe9»


def url(contact_id: Any, note_id: Any = None, org: str = "org-a") -> str:
    return f"/api/v1/o/{org}/contacts/{contact_id}/notes/" + (f"{note_id}/" if note_id else "")


def add(client: Any, contact_id: Any, body: Any) -> Any:
    return send(client, "post", url(contact_id), body)


def edit(client: Any, contact_id: Any, note_id: Any, body: Any) -> Any:
    return send(client, "patch", url(contact_id, note_id), body)


def drop(client: Any, contact_id: Any, note_id: Any) -> Any:
    return send(client, "delete", url(contact_id, note_id), None)


def note(crm: NS, key: str, who: Any, body: str = "Llamar") -> Any:
    with tenant_scope(ctx(crm.a)):
        return Note.objects.create(contact_id=crm.ids[key], author_user_id=who.pk, body=body).pk


def row(migrator: DB, note_id: Any) -> Any:
    return migrator.execute(f"{ROW} WHERE id = %s", [note_id]).fetchone()


def test_a_seller_notes_their_own_contact_and_the_audit_omits_it(crm: NS, migrator: DB) -> None:
    vera, owned, mine = signed(crm.vera), crm.ids["owned"], str(crm.vera.pk)
    sent = f"  {PERSONAL}\r\n\tCaf{E_ACUTE}\rx\N{LINE SEPARATOR}y "
    saved = f"{PERSONAL}\n\tCaf\xe9\nx\ny"
    body = add(vera, owned, {"body": sent, "author_user_id": str(uuid4())}).json()
    assert [body[k] for k in ("body", "is_pinned", "version")] == [saved, False, 1]
    assert (body["author_user_id"], body["contact_id"]) == (mine, str(owned))
    assert vera.get(url(owned)).json() == {"results": [body], "next": None}
    edited = edit(vera, owned, body["id"], {"version": 1, "body": "Otra", "is_pinned": True}).json()
    assert (edited["body"], edited["version"], edited["updated_by_user_id"]) == ("Otra", 2, mine)
    note_id, about = UUID(body["id"]), dict[str, Any](contact_id=str(owned))
    created = ("note.created", "note", note_id, {"is_pinned": [None, False]}, about, crm.vera.pk)
    changes, about = {"is_pinned": [False, True]}, {**about, "body_changed": True}
    updated = ("note.updated", "note", note_id, changes, about, crm.vera.pk)
    assert audit(migrator) == [created, updated]
    logged = migrator.execute("SELECT changes::text || metadata::text FROM audit_logs").fetchall()
    assert not any("12345678" in text or "Otra" in text for (text,) in logged)
    long = add(vera, owned, {"body": E_ACUTE * 5000, "is_pinned": True}).json()
    assert (long["body"], long["is_pinned"]) == ("\xe9" * 5000, True)
    bad = {"version": 2, "body": "a\N{RIGHT-TO-LEFT OVERRIDE}b", "is_pinned": "x"}
    assert set(edit(vera, owned, note_id, bad).json()["fields"]) == {"body", "is_pinned"}


def test_out_of_scope_missing_deleted_or_foreign_are_the_same_404(crm: NS, migrator: DB) -> None:
    vera = signed(crm.vera)
    for key in ("lima", "plain", "deleted", "b"):
        theirs, contact_id = (note(crm, key, crm.ana) if key != "b" else uuid4()), crm.ids[key]
        for answer in (
            vera.get(url(contact_id)),
            add(vera, contact_id, {"body": "x"}),
            edit(vera, contact_id, theirs, {"version": 1, "body": "x"}),
            drop(vera, contact_id, theirs),
        ):
            assert reply(answer) == NOT_FOUND
    assert reply(vera.get(url(uuid4()))) == NOT_FOUND
    mine = note(crm, "owned", crm.vera)
    for wrong in ("plain", "created"):  # la nota es de otro contacto, aunque se vea
        assert reply(drop(vera, crm.ids[wrong], mine)) == NOT_FOUND
    live = migrator.execute("SELECT count(*) FROM notes WHERE deleted_at IS NULL").fetchone()
    assert live == (4,) and not audit(migrator)


def test_reading_needs_view_and_writing_also_update(crm: NS) -> None:
    sam = signed(crm.sam)  # Supervisor: ve con `TEAM`, no edita
    assert sam.get(url(crm.ids["sam"])).status_code == 200
    assert reply(add(sam, crm.ids["sam"], {"body": "x"})) == DENIED
    give(crm.a, crm.m["bea"], {"contacts.update": "ORGANIZATION"})  # sin ver, ni lee ni escribe
    bea, plain, ghost = signed(crm.bea), crm.ids["plain"], note(crm, "plain", crm.bea)
    for answer in (bea.get(url(plain)), add(bea, plain, {"body": "x"}), drop(bea, plain, ghost)):
        assert reply(answer) == DENIED
    assert reply(edit(bea, plain, ghost, {"version": 1})) == DENIED


def test_only_the_author_or_organization_wide_update_changes_a_note(crm: NS, migrator: DB) -> None:
    vera, ana, owned = signed(crm.vera), signed(crm.ana), crm.ids["owned"]
    by_ana = note(crm, "owned", crm.ana)
    assert len(vera.get(url(owned)).json()["results"]) == 1  # la ve, pero no es suya
    give(crm.a, crm.m["vera"], {"contacts.view": "ORGANIZATION", "contacts.update": "BRANCH"})
    vera = signed(crm.vera)
    assert reply(edit(vera, owned, by_ana, {"version": 1, "is_pinned": True})) == NOT_FOUND
    assert reply(drop(vera, owned, by_ana)) == NOT_FOUND
    assert row(migrator, by_ana)[1:] == (1, False, None, None)
    by_vera = UUID(add(vera, owned, {"body": "Mía"}).json()["id"])
    now = edit(ana, owned, by_vera, {"version": 1, "is_pinned": True}).json()
    assert [now["author_user_id"], now["updated_by_user_id"]] == [str(crm.vera.pk), str(crm.ana.pk)]
    assert drop(ana, owned, by_vera).status_code == 204
    assert row(migrator, by_vera)[3:] == (crm.ana.pk, crm.ana.pk)


@pytest.mark.parametrize("text", ["", " \n\t ", "a\x00b", "a\x1bb", "x" * 5001, E_ACUTE * 5001])
def test_a_bad_body_is_a_400_that_never_echoes_it(crm: NS, migrator: DB, text: str) -> None:
    sent = f"{PERSONAL} {text}" if text.strip() and len(text) < 10 else text
    answer = add(signed(crm.vera), crm.ids["owned"], {"body": sent})
    assert (answer.status_code, set(answer.json()["fields"])) == (400, {"body"})
    assert PERSONAL not in answer.content.decode() and "x" * 50 not in answer.content.decode()
    assert migrator.execute("SELECT count(*) FROM notes").fetchone() == (0,)


def test_delete_is_logical_and_once_and_a_stale_version_is_a_409(crm: NS, migrator: DB) -> None:
    vera, owned = signed(crm.vera), crm.ids["owned"]
    first, second = note(crm, "owned", crm.vera), note(crm, "owned", crm.vera)
    assert edit(vera, owned, first, {"version": 1, "body": " Llamar "}).json()["version"] == 1
    assert edit(vera, owned, first, {"version": 1, "is_pinned": True}).status_code == 200
    for version in (1, 3):
        stale = edit(vera, owned, first, {"version": version, "body": "Pisado"})
        assert (stale.status_code, stale.json()["code"]) == (409, "NOTE_VERSION_CONFLICT")
    assert set(edit(vera, owned, first, {"body": "Sin"}).json()["fields"]) == {"version"}
    assert row(migrator, first)[:3] == ("Llamar", 2, True)
    assert drop(vera, owned, first).status_code == 204
    assert reply(drop(vera, owned, first)) == NOT_FOUND
    assert reply(edit(vera, owned, first, {"version": 3})) == NOT_FOUND
    assert [r["id"] for r in vera.get(url(owned)).json()["results"]] == [str(second)]
    assert row(migrator, first)[1:] == (3, True, crm.vera.pk, crm.vera.pk)
    about = {"contact_id": str(owned)}
    assert audit(migrator)[-1] == ("note.deleted", "note", first, {}, about, crm.vera.pk)


def test_the_list_is_newest_first_by_cursor(crm: NS) -> None:
    vera, owned = signed(crm.vera), crm.ids["owned"]
    made = [str(note(crm, "owned", crm.vera, f"n{i}")) for i in range(3)]
    page = vera.get(url(owned), {"limit": 2}).json()
    assert [r["id"] for r in page["results"]] == made[:0:-1]
    rest = vera.get(url(owned), {"limit": 2, "cursor": page["next"]}).json()
    assert ([r["id"] for r in rest["results"]], rest["next"]) == ([made[0]], None)


def test_another_organization_never_reaches_the_notes_of_a(crm: NS) -> None:
    give(crm.b, crm.m_luis, dict.fromkeys(("contacts.view", "contacts.update"), "ORGANIZATION"))
    luis, mine, owned = signed(crm.luis), note(crm, "owned", crm.vera), crm.ids["owned"]
    for answer in (
        luis.get(url(owned, org="org-b")),
        send(luis, "post", url(owned, org="org-b"), {"body": "x"}),
        send(luis, "delete", url(owned, mine, org="org-b"), None),
        luis.get(url(owned)),  # en A no es miembro
    ):
        assert reply(answer) == NOT_FOUND
    assert send(luis, "post", url(crm.ids["b"], org="org-b"), {"body": "B"}).status_code == 201
    with pytest.raises(IntegrityError), tenant_scope(ctx(crm.a)):  # FK compuesta al contacto
        Note.objects.create(contact_id=crm.ids["b"], author_user_id=crm.ana.pk, body="x")
    with pytest.raises(IntegrityError), tenant_scope(ctx(crm.a)):  # y a la membresía
        Note.objects.create(contact_id=owned, author_user_id=crm.luis.pk, body="x")


def test_a_note_never_lands_on_a_contact_deleted_meanwhile(crm: NS, migrator: DB) -> None:
    """El contacto se borra mientras vera añade y edita: los dos esperaban su fila y son 404."""
    owned, outcome, mine = crm.ids["owned"], dict[str, Any](), note(crm, "owned", crm.vera)

    def adding() -> None:
        outcome["add"] = reply(add(signed(crm.vera), owned, {"body": "Tarde"}))

    def editing() -> None:
        outcome["edit"] = reply(edit(signed(crm.vera), owned, mine, {"version": 1, "body": "T"}))

    held(migrator, owned, [adding, editing], "UPDATE contacts SET deleted_at = now() WHERE id = %s")
    assert outcome == {"add": NOT_FOUND, "edit": NOT_FOUND}
    assert migrator.execute("SELECT body FROM notes").fetchall() == [("Llamar",)]

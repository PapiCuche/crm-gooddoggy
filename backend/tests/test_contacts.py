"""F4-01: la tabla `contacts` y sus comandos (crear, editar, borrar), con PostgreSQL real y el
rol `crm_app`. Sin API: llega con F4-02 y F4-03."""

import json
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from django.db import connection

from apps.contacts.commands import (
    BadReference,
    StaleContact,
    create_contact,
    delete_contact,
    document,
    email,
    masked,
    phone,
    update_contact,
)
from apps.contacts.models import Contact
from apps.organizations.models import Branch
from core.tenancy.scope import tenant_scope
from tests.factories import make_user
from tests.test_memberships import ctx, join

pytestmark = pytest.mark.usefixtures("tenant_db")


@pytest.fixture
def world(orgs: dict[str, Any]) -> SimpleNamespace:
    """`ana` es miembro de A, `luis` solo de B; cada organización tiene una sucursal."""
    ana, luis = make_user(), make_user()
    join(orgs["A"], ana)
    join(orgs["B"], luis)
    branches = {}
    for org in ("A", "B"):
        with tenant_scope(ctx(orgs[org])):
            branches[org] = Branch.objects.create(code=f"LIMA-{org}", name="Lima").pk
    return SimpleNamespace(a=orgs["A"], b=orgs["B"], ana=ana, luis=luis, branches=branches)


def audit(migrator: psycopg.Connection[Any]) -> list[tuple[Any, ...]]:
    return migrator.execute(
        "SELECT action, entity_id, entity_label, changes::text FROM audit_logs "
        "WHERE entity_type = 'contact' ORDER BY occurred_at, id"
    ).fetchall()


def test_create_cleans_what_it_stores_and_audits_without_the_document(
    world: SimpleNamespace, migrator: psycopg.Connection[Any]
) -> None:
    with tenant_scope(ctx(world.a, world.ana)):
        contact = create_contact(
            ctx(world.a, world.ana),
            first_name="  María  José ",
            last_name="Quispe",
            primary_phone_e164="+51 987-654-321",
            primary_email="  Maria@Example.com ",
            document_type="DNI",
            document_number="4567 8912",
            owner_user_id=world.ana.pk,
            branch_id=world.branches["A"],
            source=Contact.Source.WALK_IN,
        )
    row = migrator.execute(
        "SELECT organization_id, display_name, primary_phone_e164, primary_email, "
        "document_number, created_by_user_id, version FROM contacts WHERE id = %s",
        [contact.pk],
    ).fetchone()
    assert row == (
        world.a,
        "María José Quispe",  # sin nombre visible, se forma con el nombre y el apellido
        "+51987654321",
        "Maria@Example.com",
        "45678912",
        world.ana.pk,  # quién lo creó, del contexto
        1,
    )
    [(action, entity, label, changes)] = audit(migrator)
    assert (action, entity, label) == ("contact.created", contact.pk, None)  # sin nombre
    stored = json.loads(changes)
    assert stored["document_number"] == [None, "*****912"]  # 04 §N.1: enmascarado
    assert "45678912" not in changes
    assert stored["owner_user_id"] == [None, str(world.ana.pk)]
    assert stored["source"] == [None, "WALK_IN"]


def test_a_contact_of_a_never_exists_for_b(world: SimpleNamespace) -> None:
    with tenant_scope(ctx(world.a)):
        contact = create_contact(ctx(world.a), display_name="Cliente de A")
        assert contact.created_by_user_id is None  # sin persona: el actor es el sistema
    with tenant_scope(ctx(world.b)):
        assert not Contact.objects.exists()
        with connection.cursor() as cursor:  # RLS, no solo el manager
            cursor.execute("SELECT count(*) FROM contacts")
            assert cursor.fetchone() == (0,)
        with pytest.raises(Contact.DoesNotExist):
            update_contact(ctx(world.b), contact_id=contact.pk, version=1, display_name="Mío")
        with pytest.raises(Contact.DoesNotExist):
            delete_contact(ctx(world.b), contact_id=contact.pk)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("owner_user_id", "luis"),  # miembro de otra organización
        ("owner_user_id", "nobody"),  # no es miembro de ninguna
        ("branch_id", "branch_b"),  # sucursal de otra organización
        ("branch_id", "missing"),
    ],
)
def test_owner_and_branch_belong_to_the_organization(
    world: SimpleNamespace, migrator: psycopg.Connection[Any], field: str, value: str
) -> None:
    ids = {
        "luis": world.luis.pk,
        "nobody": make_user().pk,
        "branch_b": world.branches["B"],
        "missing": uuid4(),
    }
    with tenant_scope(ctx(world.a, world.ana)), pytest.raises(BadReference) as error:
        create_contact(ctx(world.a, world.ana), display_name="X", **{field: ids[value]})
    assert error.value.field == field
    assert migrator.execute("SELECT count(*) FROM contacts").fetchone() == (0,)
    assert audit(migrator) == []


def test_update_checks_the_version_and_audits_the_diff(
    world: SimpleNamespace, migrator: psycopg.Connection[Any]
) -> None:
    me = ctx(world.a, world.ana)
    with tenant_scope(me):
        made = create_contact(
            me, display_name="Ana", document_type="DNI", document_number="12345678"
        )
        same = update_contact(me, contact_id=made.pk, version=1, display_name=" Ana ")
        assert same.version == 1  # nada cambia: ni escribe ni audita
        changed = update_contact(
            me, contact_id=made.pk, version=1, display_name="Ana Pérez", document_number="87654321"
        )
        assert changed.version == 2 and changed.document_type == "DNI"
        with pytest.raises(StaleContact):  # otra pestaña editaba la versión 1
            update_contact(me, contact_id=made.pk, version=1, display_name="Pisado")
        cleared = update_contact(
            me, contact_id=made.pk, version=2, document_type=None, document_number=""
        )
        assert (cleared.document_type, cleared.document_number, cleared.version) == (None, None, 3)
    row = migrator.execute(
        "SELECT display_name, updated_by_user_id, version FROM contacts WHERE id = %s", [made.pk]
    ).fetchone()
    assert row == ("Ana Pérez", world.ana.pk, 3)
    rows = audit(migrator)
    assert [r[0] for r in rows] == ["contact.created", "contact.updated", "contact.updated"]
    assert json.loads(rows[0][3]) == {  # lo vacío no se audita
        "display_name": [None, "Ana"],
        "document_type": [None, "DNI"],
        "document_number": [None, "*****678"],
        "source": [None, "MANUAL"],
    }
    assert json.loads(rows[1][3]) == {
        "display_name": ["Ana", "Ana Pérez"],
        "document_number": ["*****678", "*****321"],
    }
    assert json.loads(rows[2][3]) == {
        "document_type": ["DNI", None],
        "document_number": ["*****321", None],
    }


def test_update_refuses_what_it_does_not_own(world: SimpleNamespace) -> None:
    me = ctx(world.a, world.ana)
    with tenant_scope(me):
        made = create_contact(me, display_name="Ana")
        for field in ("owner_user_id", "branch_id", "organization_id"):  # F4-08
            with pytest.raises(ValueError, match="campos no admitidos"):
                update_contact(me, contact_id=made.pk, version=1, **{field: uuid4()})
        with pytest.raises(ValueError, match="van juntos"):  # un número sin tipo
            update_contact(me, contact_id=made.pk, version=1, document_number="12345678")


def test_delete_is_logical_and_audited(
    world: SimpleNamespace, migrator: psycopg.Connection[Any]
) -> None:
    me = ctx(world.a, world.ana)
    with tenant_scope(me):
        made = create_contact(me, display_name="Ana")
        delete_contact(me, contact_id=made.pk)
        with pytest.raises(Contact.DoesNotExist):  # ya borrado
            delete_contact(me, contact_id=made.pk)
        with pytest.raises(Contact.DoesNotExist):
            update_contact(me, contact_id=made.pk, version=2, display_name="Otra")
    row = migrator.execute(
        "SELECT deleted_by_user_id, deleted_at IS NOT NULL, version FROM contacts WHERE id = %s",
        [made.pk],
    ).fetchone()
    assert row == (world.ana.pk, True, 2)
    assert [r[0] for r in audit(migrator)] == ["contact.created", "contact.deleted"]


def test_duplicates_are_allowed(world: SimpleNamespace) -> None:
    """§E.5: familias que comparten número; nunca se fusiona por coincidencias (ADR-028)."""
    me = ctx(world.a)
    with tenant_scope(me):
        for name in ("Padre", "Hija"):
            create_contact(
                me,
                display_name=name,
                primary_phone_e164="+51987654321",
                document_type="RUC",
                document_number="20123456789",
            )
        assert Contact.objects.filter(primary_phone_e164="+51987654321").count() == 2


@pytest.mark.parametrize(
    "raw", ["987654321", "+51", "+0987654321", "+51 98765432101234", "+５１987654321", "abc"]
)
def test_phone_must_be_e164_and_the_error_does_not_echo_it(raw: str) -> None:
    with pytest.raises(ValueError) as error:
        phone(raw)
    assert raw not in str(error.value)


def test_phone_email_and_document_rules() -> None:
    assert phone("0051 (987) 654.321") == "+51987654321"
    assert phone("  ") is None and phone(None) is None
    assert email(" ") is None
    for bad in ("ana@", "ana example.com", f"{'a' * 250}@x.pe"):
        with pytest.raises(ValueError) as error:
            email(bad)
        assert bad not in str(error.value)
    assert document("CE", "ab-123 456") == ("CE", "AB123456")
    assert document("PASSPORT", "x1234567") == ("PASSPORT", "X1234567")
    assert document(None, " ") == (None, None)
    for kind, number in [
        ("DNI", "1234567"),
        ("DNI", "１２３４５６７８"),  # cifras de ancho completo
        ("RUC", "2012345678"),
        ("CE", "1234567"),
        ("PASSPORT", "12345"),
        ("PASSPORT", "ﬁ123456"),  # la ligadura sería «FI» en mayúsculas
        ("LICENCIA", "12345678"),
        ("DNI", None),
    ]:
        with pytest.raises(ValueError):
            document(kind, number)
    assert masked("12") == "12" and masked(None) is None


def test_names_are_validated(world: SimpleNamespace) -> None:
    me = ctx(world.a)
    with tenant_scope(me):
        cases: list[dict[str, Any]] = [{}, {"display_name": " - "}, {"first_name": "Ana\nPérez"}]
        for bad in cases:
            with pytest.raises(ValueError):
                create_contact(me, **bad)
        with pytest.raises(ValueError, match="origen"):
            create_contact(me, display_name="Ana", source="TIKTOK")
        assert not Contact.objects.exists()


def test_the_database_refuses_what_the_commands_never_write(world: SimpleNamespace) -> None:
    """Las mismas reglas en la tabla, por si otro camino escribe: teléfono E.164, documento con
    su tipo, origen conocido."""
    cases: list[dict[str, Any]] = [
        {"primary_phone_e164": "987654321"},
        {"document_number": "12345678"},
        {"document_type": "DNI"},
        {"source": "TIKTOK"},
        {"version": 0},
    ]
    for bad in cases:
        with tenant_scope(ctx(world.a)), pytest.raises(Exception, match="contacts_"):
            Contact.objects.create(display_name="X", **bad)

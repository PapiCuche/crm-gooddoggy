"""F6-08a1: tablas, siembra de categorías, permiso y comandos de las respuestas rápidas, con
PostgreSQL real y el rol `crm_app`. Sin rutas: llegan con F6-08a2 a F6-08a4."""

import json
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from django.core.management import call_command
from django.db import DatabaseError, IntegrityError, connection

from apps.access.catalog import BY_CODE, ROLE_TEMPLATES
from apps.conversations import quick_replies as qr
from apps.conversations.models import QuickReply, QuickReplyCategory
from apps.conversations.seed import CATEGORIES, ensure_quick_reply_categories
from apps.provisioning import seeds
from core.tenancy.scope import tenant_scope
from tests.factories import make_user
from tests.test_bootstrap import bootstrap
from tests.test_memberships import ctx, join
from tests.test_pipelines_tables import as_migrator

pytestmark = pytest.mark.usefixtures("tenant_db")
DB = psycopg.Connection[Any]


@pytest.fixture
def world(orgs: dict[str, Any]) -> SimpleNamespace:
    """`ana` es miembro de A y `luis` de B; las dos organizaciones, con sus categorías."""
    ana, luis = make_user(), make_user()
    join(orgs["A"], ana)
    join(orgs["B"], luis)
    found = SimpleNamespace(a=orgs["A"], b=orgs["B"], ana=ana, luis=luis)
    for org in ("a", "b"):
        with tenant_scope(ctx(getattr(found, org))) as tenant:
            assert ensure_quick_reply_categories(tenant) is True
    return found


def run(world: SimpleNamespace, command: Callable[..., Any], org: str = "a", **fields: Any) -> Any:
    me = ctx(world.a, world.ana) if org == "a" else ctx(world.b, world.luis)
    with tenant_scope(me) as tenant:
        return command(tenant, **fields)


def category(world: SimpleNamespace, name: str = "Delivery", org: str = "a") -> UUID:
    with tenant_scope(ctx(getattr(world, org))):
        found: UUID = QuickReplyCategory.objects.get(name=name).pk
    return found


def reply(world: SimpleNamespace, shortcut: str = "delivery", org: str = "a", **fields: Any) -> Any:
    wanted = {"title": "Delivery", "body": "Hola {{contact.first_name}}, enviamos hoy.", **fields}
    wanted.setdefault("category_id", category(world, org=org))
    return run(world, qr.create_reply, org=org, shortcut=shortcut, **wanted)


def audit(migrator: DB) -> list[tuple[str, str, Any, dict[str, Any]]]:
    found = migrator.execute(
        "SELECT action, entity_label, actor_id, changes::text FROM audit_logs "
        "WHERE entity_type LIKE 'quick_reply%' ORDER BY occurred_at, id"
    ).fetchall()
    return [(action, label, actor, json.loads(changes)) for action, label, actor, changes in found]


def count(migrator: DB) -> tuple[Any, ...]:
    found = migrator.execute(
        "SELECT (SELECT count(*) FROM quick_replies), (SELECT count(*) FROM quick_reply_categories)"
    ).fetchone()
    assert found is not None
    return tuple(found)


def test_delivery_is_created_in_delivery_and_the_other_organization_never_sees_it(
    world: SimpleNamespace, migrator: DB
) -> None:
    made = reply(world, " DeLivery ")
    assert (made.shortcut, made.category_id, made.is_active, made.usage_count) == (
        "delivery", category(world), True, 0,
    )  # fmt: skip
    assert (made.organization_id, made.created_by_user_id, made.version) == (
        world.a,
        world.ana.pk,
        1,
    )
    with tenant_scope(ctx(world.b)), connection.cursor() as cursor:
        assert not QuickReply.objects.exists()
        cursor.execute("SELECT count(*) FROM quick_replies")  # RLS, no solo el manager
        assert cursor.fetchone() == (0,)
    with pytest.raises(QuickReply.DoesNotExist):
        run(world, qr.update_reply, org="b", quick_reply_id=made.pk, version=1, title="Mío")
    with pytest.raises(QuickReply.DoesNotExist):
        run(world, qr.delete_reply, org="b", quick_reply_id=made.pk)
    with pytest.raises(qr.Invalid) as error:  # la categoría de A no existe para B
        reply(world, "otro", org="b", category_id=category(world))
    assert error.value.field == "category_id"
    assert reply(world, org="b").shortcut == "delivery"  # el atajo es único por organización
    with pytest.raises(DatabaseError), tenant_scope(ctx(world.b)), connection.cursor() as cursor:
        cursor.execute(  # WITH CHECK: B no escribe una fila de A
            "INSERT INTO quick_reply_categories (organization_id, name, sort_order, is_active, "
            "created_at, updated_at, version) VALUES (%s, 'X', 0, true, now(), now(), 1)",
            [world.a],
        )
    assert count(migrator) == (2, 14)


@pytest.mark.parametrize("bad", ["/delivery", "deli very", "ñandú", "x" * 33, "", "  ", 5, None])
def test_a_bad_shortcut_is_invalid_and_writes_nothing(
    world: SimpleNamespace, migrator: DB, bad: Any
) -> None:
    with pytest.raises(qr.Invalid) as error:
        reply(world, bad)
    assert error.value.field == "shortcut"
    assert (count(migrator), audit(migrator)) == ((0, 14), [])


def test_the_shortcut_is_unique_among_the_active_and_live(world: SimpleNamespace) -> None:
    first = reply(world)
    with pytest.raises(qr.Taken):
        reply(world, "DELIVERY")
    run(world, qr.update_reply, quick_reply_id=first.pk, version=1, is_active=False)
    second = reply(world)  # desactivada, deja el atajo libre
    with pytest.raises(qr.Taken):  # y reactivarla choca con la nueva
        run(world, qr.update_reply, quick_reply_id=first.pk, version=2, is_active=True)
    run(world, qr.delete_reply, quick_reply_id=second.pk)
    again = run(world, qr.update_reply, quick_reply_id=first.pk, version=2, is_active=True)
    assert (again.is_active, again.version) == (True, 3)
    with pytest.raises(QuickReply.DoesNotExist):  # la borrada ya no se cambia
        run(world, qr.update_reply, quick_reply_id=second.pk, version=2, is_active=True)


@pytest.mark.parametrize(
    ("constraint", "fields"),
    [
        ("quick_replies_shortcut_ck", {"shortcut": "Con Espacio"}),
        ("quick_replies_body_ck", {"body": "   "}),
        ("quick_replies_deleted_ck", {"deleted_at": "2026-01-01T00:00:00Z"}),
        ("quick_replies_category_fk", {"category_id": uuid4()}),
    ],
)
def test_the_database_refuses_what_the_commands_would_refuse(
    world: SimpleNamespace, constraint: str, fields: dict[str, Any]
) -> None:
    wanted = {"shortcut": "ok", "title": "T", "body": "Hola", "category_id": category(world)}
    with tenant_scope(ctx(world.a)), pytest.raises(IntegrityError, match=constraint):
        QuickReply.objects.create(**{**wanted, **fields})


@pytest.mark.parametrize(
    "body",
    [
        "Hola {{contact.phone}}", "{{contact.first_name | upper}}", "Hola {{org.name}",
        "Hola }} y {{", "{{}}", "{{ contact.Name }}", "Total {{1+1}}", "x" * 4097, "\x00",
        "{{contact name}}", "{{" + " " * 4000,
    ],
)  # fmt: skip
def test_a_body_with_a_variable_outside_the_list_is_invalid(
    world: SimpleNamespace, migrator: DB, body: str
) -> None:
    with pytest.raises(qr.Invalid) as error:
        reply(world, body=body)
    assert error.value.field == "body"
    made = reply(world, body="{{ org.name }}: {{contact.name}} / {{contact.first_name}}")
    with pytest.raises(qr.Invalid):
        run(world, qr.update_reply, quick_reply_id=made.pk, version=1, body=body)
    assert run(world, qr.update_reply, quick_reply_id=made.pk, version=1).version == 1
    assert [a for a, *_ in audit(migrator)] == ["quick_reply.created"]


def test_render_resolves_the_closed_list_once_and_reports_what_is_missing() -> None:
    body = "Hola {{contact.first_name}}, soy de {{ org.name }}. {{contact.first_name}}!"
    values = {"contact.first_name": "Ana", "org.name": "Good Doggy"}
    assert qr.render(body, values) == ("Hola Ana, soy de Good Doggy. Ana!", [])
    assert qr.render(body, {"org.name": "GD", "contact.first_name": ""}) == (
        "Hola , soy de GD. !", ["contact.first_name"],
    )  # fmt: skip
    evil = {"contact.name": "{{org.name}}", "org.name": "Secreto"}  # nunca se reinterpreta
    assert qr.render("A: {{contact.name}}", evil) == ("A: {{org.name}}", [])
    assert qr.render("{{contact.phone}}", {"contact.phone": "999"}) == ("", ["contact.phone"])
    assert qr.render("Sin variables", {}) == ("Sin variables", [])
    assert qr.variables("{{contact.name}} {{org.name}}") == ["contact.name", "org.name"]


def test_every_change_is_audited_with_the_body_as_a_fingerprint(
    world: SimpleNamespace, migrator: DB
) -> None:
    made = reply(world, body="Secreto: llámanos")
    run(world, qr.update_reply, quick_reply_id=made.pk, version=1, title=" Envío  hoy ")
    run(world, qr.update_reply, quick_reply_id=made.pk, version=2, title="Envío hoy")  # igual
    run(world, qr.update_reply, quick_reply_id=made.pk, version=2, body="Otro texto")
    run(world, qr.delete_reply, quick_reply_id=made.pk)
    rows = audit(migrator)
    assert [(a, label, actor) for a, label, actor, _ in rows] == [
        ("quick_reply.created", "delivery", world.ana.pk),
        ("quick_reply.updated", "delivery", world.ana.pk),
        ("quick_reply.updated", "delivery", world.ana.pk),
        ("quick_reply.deleted", "delivery", world.ana.pk),
    ]
    assert rows[1][3] == {"title": ["Delivery", "Envío hoy"]}
    hashed = rows[2][3]["body_fingerprint"]
    assert hashed == [qr.fingerprint("Secreto: llámanos"), qr.fingerprint("Otro texto")]
    assert rows[0][3]["body_fingerprint"] == [None, hashed[0]] and hashed[0] != hashed[1]
    assert "Secreto" not in str(rows) and "Otro texto" not in str(rows)
    assert rows[3][3]["is_active"] == [True, False] and "deleted_at" in rows[3][3]


def test_categories_are_written_audited_and_not_deleted_while_in_use(
    world: SimpleNamespace, migrator: DB
) -> None:
    mine = run(world, qr.create_category, name="  Reclamos ")
    assert (mine.name, mine.sort_order, mine.is_active) == ("Reclamos", len(CATEGORIES), True)
    with pytest.raises(qr.Taken):  # sin distinguir mayúsculas, entre las vivas
        run(world, qr.create_category, name="DELIVERY")
    used = reply(world, category_id=mine.pk)
    with pytest.raises(qr.CategoryInUse):
        run(world, qr.delete_category, category_id=mine.pk)
    run(world, qr.update_reply, quick_reply_id=used.pk, version=1, category_id=category(world))
    with pytest.raises(qr.StaleVersion):
        run(world, qr.update_category, category_id=mine.pk, version=2, name="Pisado")
    with pytest.raises(ValueError, match="no admitidos"):  # nunca del cuerpo
        run(world, qr.update_category, category_id=mine.pk, version=1, organization_id=world.b)
    run(world, qr.update_category, category_id=mine.pk, version=1, is_active=False, sort_order=-1)
    run(world, qr.delete_category, category_id=mine.pk)
    with pytest.raises(qr.Invalid):  # una respuesta no va a una categoría borrada
        run(world, qr.update_reply, quick_reply_id=used.pk, version=2, category_id=mine.pk)
    assert run(world, qr.create_category, name="Reclamos").pk != mine.pk  # el nombre queda libre
    assert [(a, label) for a, label, *_ in audit(migrator) if "category" in a] == [
        ("quick_reply_category.created", "Reclamos"),
        ("quick_reply_category.updated", "Reclamos"),
        ("quick_reply_category.deleted", "Reclamos"),
        ("quick_reply_category.created", "Reclamos"),
    ]


def seeded(migrator: DB, org: UUID) -> list[tuple[str, int, bool]]:
    found = migrator.execute(
        "SELECT name, sort_order, is_active FROM quick_reply_categories "
        "WHERE organization_id = %s ORDER BY sort_order",
        [org],
    ).fetchall()
    return [(name, order, active) for name, order, active in found]


EXPECTED = [(name, order, True) for order, name in enumerate(CATEGORIES)]


def test_new_organizations_are_born_with_the_seven_categories(migrator: DB) -> None:
    assert ensure_quick_reply_categories in seeds._SEEDS
    made = bootstrap()  # el alta de F2-06
    assert seeded(migrator, made.organization_id) == EXPECTED
    assert [name for name, *_ in EXPECTED] == [
        "Ventas", "Precios", "Stock", "Delivery", "Garantías", "Pagos", "Atención general",
    ]  # fmt: skip
    with tenant_scope(ctx(made.organization_id)) as tenant:
        assert ensure_quick_reply_categories(tenant) is False  # idempotente


def test_the_data_migration_seeds_the_organizations_that_already_existed(
    orgs: dict[str, Any], migrator: DB
) -> None:
    """`orgs` creó A y B sin categorías, como antes de F6-08a1; B ya tenía una, borrada."""
    migrator.execute(
        "INSERT INTO quick_reply_categories (organization_id, name, sort_order, is_active, "
        "created_at, updated_at, deleted_at, version) "
        "VALUES (%s, 'Vieja', 0, false, now(), now(), now(), 1)",
        [orgs["B"]],
    )
    for _ in range(2):  # dos veces: sin duplicar
        with as_migrator():
            call_command("migrate", "conversations", "0037", verbosity=0)
            call_command("migrate", "conversations", verbosity=0)
        assert seeded(migrator, orgs["A"]) == EXPECTED
        assert seeded(migrator, orgs["B"]) == [("Vieja", 0, False)]  # no se toca


def test_the_permission_is_in_the_catalog_and_only_the_owner_and_admin_templates() -> None:
    permission = BY_CODE["quick_replies.manage"]
    assert (permission.module, permission.supports_scope, permission.is_sensitive) == (
        "conversations", False, False,
    )  # fmt: skip
    held = {t.code for t in ROLE_TEMPLATES if "quick_replies.manage" in t.grants}
    assert held == {"owner", "admin"}

"""F2-04: modelo RBAC. Catálogo global de solo lectura, roles por tenant e integridad en BD."""

from types import SimpleNamespace
from typing import Any
from uuid import UUID

import psycopg
import pytest
from django.core.management import call_command
from django.db import IntegrityError, ProgrammingError, connection
from django.db.models import ProtectedError
from psycopg.errors import CheckViolation, ForeignKeyViolation, UniqueViolation

from apps.access import catalog, services
from apps.access.apps import extend_owner_roles
from apps.access.catalog import (
    BY_CODE,
    PERMISSIONS,
    ROLE_TEMPLATES,
    PermissionDef,
    RoleTemplate,
    Scope,
)
from apps.access.models import MembershipRole, Permission, Role, RolePermission
from apps.access.services import clone_role_templates
from apps.accounts.models import User
from core.tenancy.context import TenantContextError
from core.tenancy.scope import tenant_scope
from tests.conftest import migrator_settings
from tests.factories import TEST_PASSWORD, make_user
from tests.test_memberships import ctx, join, raw
from tests.test_tenancy import raw_count

pytestmark = pytest.mark.usefixtures("tenant_db")
NEW_PERMISSION = (
    "INSERT INTO permissions (code, module, is_sensitive, supports_scope) "
    "VALUES (%s, 'x', false, %s)"
)
GRANT = (
    "INSERT INTO role_permissions (organization_id, role_id, permission_code, supports_scope, "
    "scope) VALUES (%s, %s, %s, %s, %s)"
)
ASSIGN = (
    "INSERT INTO membership_roles (organization_id, membership_id, role_id) VALUES (%s, %s, %s)"
)


@pytest.fixture
def rbac(orgs: dict[str, UUID]) -> dict[str, Any]:
    """En A y en B: una membresía y los roles plantilla clonados (sin asignar a nadie)."""
    data = {}
    for key in ("A", "B"):
        membership = join(orgs[key], make_user())
        with tenant_scope(ctx(orgs[key])) as scope:
            roles = {role.code: role for role in clone_role_templates(scope)}
        data[key] = SimpleNamespace(org=orgs[key], membership=membership.pk, roles=roles)
    return data


def catalog_rows() -> set[tuple[Any, ...]]:
    fields = ("code", "module", "is_sensitive", "supports_scope")
    return set(Permission.objects.values_list(*fields))  # global: legible sin tenant


EXPECTED = {(p.code, p.module, p.is_sensitive, p.supports_scope) for p in PERMISSIONS}


def test_catalog_is_synced_and_scope_is_never_part_of_a_code(
    migrator: psycopg.Connection[Any],
) -> None:
    assert catalog_rows() == EXPECTED
    sensitive = {"organization.manage", "users.manage", "roles.manage", "audit.view"}
    assert {p.code for p in PERMISSIONS if p.is_sensitive} == sensitive
    owners = [template for template in ROLE_TEMPLATES if template.is_owner_role]
    assert len(owners) == 1 and set(owners[0].grants) == set(BY_CODE)
    for template in ROLE_TEMPLATES:  # plantillas coherentes con el catálogo
        for code, scope in template.grants.items():
            assert (scope is not None) == BY_CODE[code].supports_scope, (template.code, code)
    with pytest.raises(CheckViolation):
        migrator.execute(NEW_PERMISSION, ["contacts.view.own", True])  # alcance en el código


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO permissions (code, module, is_sensitive, supports_scope) "
        "VALUES ('x.y', 'x', false, false)",
        "UPDATE permissions SET is_sensitive = false",
        "DELETE FROM permissions",
    ],
)
def test_permissions_is_read_only_for_the_runtime_role(sql: str) -> None:
    with pytest.raises(ProgrammingError, match="permission denied"):
        raw(sql)
    assert catalog_rows() == EXPECTED


def test_post_migrate_resyncs_the_catalog(
    rbac: dict[str, Any], migrator: psycopg.Connection[Any]
) -> None:
    a = rbac["A"]
    for code in ("old.unused", "old.granted"):
        migrator.execute(NEW_PERMISSION, [code, False])
    migrator.execute(GRANT, [a.org, a.roles["seller"].pk, "old.granted", False, None])
    migrator.execute("UPDATE permissions SET is_sensitive = false WHERE code = 'audit.view'")
    runtime = dict(connection.settings_dict)  # el job de migraciones: migrate como crm_migrator
    connection.close()
    connection.settings_dict.update(migrator_settings())
    try:
        call_command("migrate", verbosity=0)
    finally:
        connection.close()
        connection.settings_dict.clear()
        connection.settings_dict.update(runtime)
    try:  # el concedido se conserva (no rompe el despliegue); el resto vuelve al catálogo
        assert catalog_rows() == EXPECTED | {("old.granted", "x", False, False)}
    finally:
        migrator.execute("DELETE FROM role_permissions WHERE permission_code = 'old.granted'")
        migrator.execute("DELETE FROM permissions WHERE code = 'old.granted'")


def as_migrator(command: Any) -> Any:
    """Lo que hace el job de migraciones: con la conexión de Django como `crm_migrator`."""
    runtime = dict(connection.settings_dict)
    connection.close()
    connection.settings_dict.update(migrator_settings())
    try:
        return command()
    finally:
        connection.close()
        connection.settings_dict.clear()
        connection.settings_dict.update(runtime)


def test_the_owner_role_follows_the_catalog_and_no_other_role_does(
    rbac: dict[str, Any], migrator: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR-018: un permiso nuevo llega, tras el `migrate`, al rol Owner de cada organización."""
    new = (PermissionDef("teams.manage", "users"), PermissionDef("leads.view", "x", False, True))
    grants = "SELECT role_id, permission_code, scope FROM role_permissions"
    audited = (
        "SELECT actor_type, actor_id, metadata FROM platform_audit_logs "
        "WHERE action = 'access.owner_roles.extended'"
    )
    a, b = rbac["A"], rbac["B"]
    # El rol Owner de A, con otro código y otro nombre; un rol que solo se llama Owner y lleva su
    # código; y un rol Owner, el de B, que ya tiene uno de los dos permisos con menos alcance.
    migrator.execute(
        "UPDATE roles SET code = 'fundador', name = 'Otro' WHERE id = %s", [a.roles["owner"].pk]
    )
    migrator.execute(
        "INSERT INTO roles (id, organization_id, code, name, description, is_system, "
        "is_owner_role, created_at, updated_at) "
        "VALUES (uuidv7(), %s, 'owner', 'Owner', '', true, false, now(), now())",
        [a.org],
    )
    migrator.execute(NEW_PERMISSION, ["leads.view", True])
    migrator.execute(GRANT, [b.org, b.roles["owner"].pk, "leads.view", True, "TEAM"])
    before = set(migrator.execute(grants).fetchall())
    monkeypatch.setattr(catalog, "PERMISSIONS", (*PERMISSIONS, *new))
    try:
        assert extend_owner_roles("default") == 0  # el runtime no ve otra organización: nada
        assert set(migrator.execute(grants).fetchall()) == before
        as_migrator(lambda: call_command("migrate", verbosity=0))
        added = set(migrator.execute(grants).fetchall()) - before
        assert added == {  # solo los roles Owner, y solo lo que les faltaba
            (a.roles["owner"].pk, "teams.manage", None),
            (a.roles["owner"].pk, "leads.view", "ORGANIZATION"),  # todo el alcance
            (b.roles["owner"].pk, "teams.manage", None),  # el `TEAM` que ya tenía no se toca
        }
        assert before <= set(migrator.execute(grants).fetchall())  # nada se quita ni cambia
        assert migrator.execute(audited).fetchall() == [
            (
                "SYSTEM",
                None,
                {"grants": 3, "permissions": ["leads.view", "teams.manage"], "roles": 2},
            )
        ]
        as_migrator(lambda: call_command("migrate", verbosity=0))  # otro despliegue: nada
        assert set(migrator.execute(grants).fetchall()) == before | added
        assert len(migrator.execute(audited).fetchall()) == 1
        migrator.execute(  # si a un rol Owner le falta una, la función dice cuántas añadió
            "DELETE FROM role_permissions WHERE role_id = %s AND permission_code = 'teams.manage'",
            [a.roles["owner"].pk],
        )
        assert as_migrator(lambda: extend_owner_roles("default")) == 1
        assert set(migrator.execute(grants).fetchall()) == before | added
        assert len(migrator.execute(audited).fetchall()) == 2
    finally:
        for code in ("teams.manage", "leads.view"):
            migrator.execute("DELETE FROM role_permissions WHERE permission_code = %s", [code])
            migrator.execute("DELETE FROM permissions WHERE code = %s", [code])


def test_scope_is_null_exactly_when_the_permission_has_no_scope(
    rbac: dict[str, Any], migrator: psycopg.Connection[Any]
) -> None:
    a, role = rbac["A"], rbac["A"].roles["seller"].pk
    with migrator.transaction(force_rollback=True):  # el permiso de prueba nunca se confirma
        migrator.execute(NEW_PERMISSION, ["tests.scoped", True])
        rejected = [
            ("users.view", False, "OWN", CheckViolation),  # no admite alcance
            ("tests.scoped", True, None, CheckViolation),  # lo admite y no lo trae
            ("tests.scoped", True, "EVERYTHING", CheckViolation),  # alcance inexistente
            ("users.view", True, "OWN", ForeignKeyViolation),  # miente sobre supports_scope
            ("no.existe", False, None, ForeignKeyViolation),  # permiso fuera del catálogo
        ]
        for code, flag, scope, error in rejected:
            with pytest.raises(error), migrator.transaction():
                migrator.execute(GRANT, [a.org, role, code, flag, scope])
        migrator.execute(GRANT, [a.org, role, "users.view", False, None])
        migrator.execute(GRANT, [a.org, role, "tests.scoped", True, "TEAM"])
        with pytest.raises(UniqueViolation), migrator.transaction():
            migrator.execute(GRANT, [a.org, role, "tests.scoped", True, "BRANCH"])


def test_role_code_is_unique_per_organization_and_owner_is_single(rbac: dict[str, Any]) -> None:
    a, b = rbac["A"], rbac["B"]
    for fields in ({"code": "seller"}, {"code": "otro", "is_owner_role": True}):
        with pytest.raises(IntegrityError), tenant_scope(ctx(a.org)):
            Role.objects.create(name="Duplicado", **fields)
    with tenant_scope(ctx(a.org)):
        Role.objects.create(code="support", name="Atención al cliente")  # rol personalizado
        assert Role.objects.count() == 5
    with tenant_scope(ctx(b.org)):
        assert Role.objects.count() == 4  # los mismos códigos conviven en otra organización


def test_cross_tenant_links_are_impossible_even_with_raw_sql(
    rbac: dict[str, Any], migrator: psycopg.Connection[Any]
) -> None:
    a, b = rbac["A"], rbac["B"]
    seller_a, seller_b = a.roles["seller"].pk, b.roles["seller"].pk
    mixed = [(a.org, a.membership, seller_b), (a.org, b.membership, seller_a)]
    for row in [*mixed, (b.org, a.membership, seller_a)]:
        with pytest.raises(ForeignKeyViolation):  # ni el migrador (BYPASSRLS) puede
            migrator.execute(ASSIGN, list(row))
    with pytest.raises(ForeignKeyViolation):
        migrator.execute(GRANT, [a.org, seller_b, "users.view", False, None])
    migrator.execute(ASSIGN, [a.org, a.membership, seller_a])
    with pytest.raises(UniqueViolation):
        migrator.execute(ASSIGN, [a.org, a.membership, seller_a])
    with pytest.raises(ProgrammingError, match="row-level security"), tenant_scope(ctx(a.org)):
        raw(ASSIGN, [b.org, b.membership, seller_b])  # runtime: fila de otro tenant


def test_rbac_tables_are_isolated_by_tenant(
    rbac: dict[str, Any], migrator: psycopg.Connection[Any]
) -> None:
    a, b = rbac["A"], rbac["B"]
    for side in (a, b):
        migrator.execute(ASSIGN, [side.org, side.membership, side.roles["seller"].pk])
    tables = ("roles", "role_permissions", "membership_roles")
    for table in tables:
        assert raw_count(table) == 0, table  # T1: sin tenant no se ve nada, aunque haya filas
    role = "INSERT INTO roles (organization_id, code, name, description, is_system, is_owner_role, "
    role += "created_at, updated_at) VALUES (%s, 'intruso', 'x', '', false, false, now(), now())"
    for sql, params in (
        (role, [b.org]),
        (GRANT, [b.org, b.roles["seller"].pk, "users.view", False, None]),
    ):
        with pytest.raises(ProgrammingError, match="row-level security"), tenant_scope(ctx(a.org)):
            raw(sql, params)  # T3: desde A no se escribe en B, tampoco con SQL directo
    with tenant_scope(ctx(a.org)):
        for table in tables:
            rows = raw(f"SELECT DISTINCT organization_id FROM {table}")  # noqa: S608 — fijos
            assert rows == [(a.org,)], table
        assert raw("UPDATE roles SET name = 'cambiado'") == [(4,)]
        assert not Role.objects.filter(pk=b.roles["owner"].pk).exists()
        assert MembershipRole.objects.count() == 1  # solo la asignación de A
    untouched = "SELECT count(*) FROM roles WHERE organization_id = %s AND name <> 'cambiado'"
    assert migrator.execute(untouched, [b.org]).fetchone() == (4,)


def test_clone_is_idempotent_and_preserves_the_organizations_edits(rbac: dict[str, Any]) -> None:
    a = rbac["A"]
    flags = {(role.code, role.is_system, role.is_owner_role) for role in a.roles.values()}
    assert flags == {(t.code, True, t.is_owner_role) for t in ROLE_TEMPLATES} and len(flags) == 4
    with tenant_scope(ctx(a.org)) as scope:
        admin = Role.objects.get(code="admin")
        admin.name = "Gerencia"
        admin.save()
        RolePermission.objects.filter(role=admin, permission_id="users.manage").delete()
        Role.objects.create(code="warehouse", name="Encargado de almacén")
        assert clone_role_templates(scope) == []  # nada que crear, nada que pisar
        assert Role.objects.get(code="admin").name == "Gerencia" and Role.objects.count() == 5
        granted = set(admin.grants.values_list("permission_id", flat=True))
        assert granted == set(ROLE_TEMPLATES[1].grants) - {"users.manage"}
        owner = Role.objects.get(is_owner_role=True)
        template = next(t for t in ROLE_TEMPLATES if t.is_owner_role)
        assert set(owner.grants.values_list("permission_id", "scope")) == set(
            template.grants.items()
        )
        owner.code = "duenos"  # el Owner se reconoce por su flag: renombrarlo no lo duplica
        owner.save()
        Role.objects.get(code="supervisor").delete()
        assert [role.code for role in clone_role_templates(scope)] == [
            "supervisor"
        ]  # solo el que falta
        assert Role.objects.get(code="admin").name == "Gerencia" and Role.objects.count() == 5
        assert MembershipRole.objects.count() == 0  # clonar no asigna roles a nadie
    with pytest.raises(TenantContextError):
        clone_role_templates(ctx(a.org))  # fuera de su tenant_scope


def test_roles_in_use_cannot_be_deleted(
    rbac: dict[str, Any], migrator: psycopg.Connection[Any]
) -> None:
    a = rbac["A"]
    migrator.execute(ASSIGN, [a.org, a.membership, a.roles["seller"].pk])
    with pytest.raises(ProtectedError), tenant_scope(ctx(a.org)):
        Role.objects.get(code="seller").delete()
    for sql, value in (
        ("DELETE FROM roles WHERE id = %s", a.roles["admin"].pk),  # tiene concesiones
        ("DELETE FROM organization_memberships WHERE id = %s", a.membership),  # tiene un rol
        ("DELETE FROM permissions WHERE code = %s", "users.view"),  # está concedido
    ):
        with pytest.raises(ForeignKeyViolation):
            migrator.execute(sql, [value])
    with tenant_scope(ctx(a.org)):
        custom = Role.objects.create(code="support", name="Atención al cliente")
        RolePermission.objects.create(role=custom, permission_id="users.view", supports_scope=False)
        custom.delete()  # rol propio sin asignar: se va con sus concesiones
        assert not RolePermission.objects.filter(role_id=custom.pk).exists()


def test_roles_hang_from_the_membership_and_staff_gets_none(
    rbac: dict[str, Any], migrator: psycopg.Connection[Any]
) -> None:
    fields = {field.name for field in MembershipRole._meta.get_fields()}
    assert fields == {"id", "organization_id", "membership", "role", "created_at"}  # sin usuario
    assert not any(field.is_relation for field in User._meta.get_fields())  # sin rol en User
    staff = User.objects.create_superuser("ops@example.com", TEST_PASSWORD)
    owned = "SELECT count(*) FROM organization_memberships WHERE user_id = %s"
    assert migrator.execute(owned, [staff.pk]).fetchone() == (0,)  # ni membresía implícita
    assert migrator.execute("SELECT count(*) FROM membership_roles").fetchone() == (0,)  # ni rol


def test_clone_copies_the_scope_of_each_grant(
    orgs: dict[str, UUID], migrator: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """El alcance viaja con la concesión: plantilla con un permiso que sí admite alcance."""
    scoped = PERMISSIONS[0].__class__("tests.scoped", "x", supports_scope=True)
    template = RoleTemplate("scoped", "Con alcance", {scoped.code: Scope.TEAM, "users.view": None})
    monkeypatch.setitem(BY_CODE, scoped.code, scoped)
    monkeypatch.setattr(services, "ROLE_TEMPLATES", (template,))
    migrator.execute(NEW_PERMISSION, [scoped.code, True])
    try:
        with tenant_scope(ctx(orgs["A"])) as scope:
            (role,) = clone_role_templates(scope)
            fields = ("permission_id", "supports_scope", "scope")
            grants: set[tuple[Any, ...]] = set(role.grants.values_list(*fields))
        assert grants == {(scoped.code, True, "TEAM"), ("users.view", False, None)}
    finally:
        migrator.execute("DELETE FROM role_permissions WHERE permission_code = %s", [scoped.code])
        migrator.execute("DELETE FROM permissions WHERE code = %s", [scoped.code])

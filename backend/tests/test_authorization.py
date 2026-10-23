"""F2-05A: motor de autorización. Decide por permisos y alcances; ante la duda, deniega."""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from itertools import count
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from django.core.exceptions import ImproperlyConfigured
from django.db import connection
from django.test.utils import CaptureQueriesContext

from apps.access import scopes, selectors
from apps.access.catalog import BY_CODE, PermissionDef, Scope
from apps.access.models import MembershipRole, Role, RolePermission
from apps.access.scopes import FieldScopes, ScopePolicyMissing, register
from apps.access.selectors import (
    AccessDenied,
    Denied,
    ExecutionContext,
    UnknownPermission,
    can,
    execution_context,
    has_permission,
    require,
    scoped,
)
from apps.access.services import clone_role_templates
from apps.accounts.models import User
from apps.organizations.models import Organization, OrganizationMembership
from core.tenancy.context import ActorType, TenantContext, TenantContextError
from core.tenancy.scope import tenant_scope
from tests.factories import TEST_PASSWORD, make_user
from tests.tenancy_app.models import Widget, WidgetPart
from tests.test_access import NEW_PERMISSION
from tests.test_memberships import ctx, join

pytestmark = pytest.mark.usefixtures("tenant_db")
VIEW = "widgets.view"  # permiso de prueba que sí admite alcance
_codes = count(1)


@pytest.fixture
def world(
    orgs: dict[str, UUID], migrator: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> Iterator[Any]:
    """Permiso con alcance, política de `Widget` y una persona (`ana`) con membresía en A."""

    def drop() -> None:
        migrator.execute("DELETE FROM role_permissions WHERE permission_code = %s", [VIEW])
        migrator.execute("DELETE FROM permissions WHERE code = %s", [VIEW])

    drop()  # por si una ejecución anterior se interrumpió
    try:
        migrator.execute(NEW_PERMISSION, [VIEW, True])
        monkeypatch.setitem(BY_CODE, VIEW, PermissionDef(VIEW, "x", supports_scope=True))
        policy = FieldScopes(("assigned_user_id", "created_by_user_id"), "team_id", "branch_id")
        registry: dict[Any, Any] = scopes._REGISTRY
        monkeypatch.setitem(registry, Widget, policy)
        ana = make_user(email="ana@example.com")
        membership = join(orgs["A"], ana).pk
        yield SimpleNamespace(a=orgs["A"], b=orgs["B"], ana=ana, orgs=orgs, membership=membership)
    finally:
        drop()


def give(org: UUID, membership: UUID, grants: dict[str, str | None], code: str = "") -> Role:
    """Rol propio de la organización con esas concesiones, asignado a la membresía."""
    with tenant_scope(ctx(org)):
        role: Role = Role.objects.create(code=code or f"rol{next(_codes)}", name="Rol propio")
        RolePermission.objects.bulk_create(
            RolePermission(
                organization_id=org,
                role=role,
                permission_id=permission,
                supports_scope=scope is not None,
                scope=scope,
            )
            for permission, scope in grants.items()
        )
        MembershipRole.objects.create(membership_id=membership, role=role)
    return role


def widest(codes: list[str]) -> dict[str, str | None]:
    """Cada permiso con su alcance más amplio: `ORGANIZATION` si lo admite (F4-02)."""
    return {code: "ORGANIZATION" if BY_CODE[code].supports_scope else None for code in codes}


@contextmanager
def acting(org: UUID, user: User, **changes: Any) -> Iterator[ExecutionContext]:
    tenant = ctx(org, user)
    with tenant_scope(tenant):
        yield replace(execution_context(tenant), **changes)


def denied(org: UUID, user: User, tenant: TenantContext | None = None) -> Denied:
    tenant = tenant or ctx(org, user)
    with pytest.raises(AccessDenied) as error, tenant_scope(tenant):
        execution_context(tenant)
    return error.value.reason


def test_member_without_roles_or_grant_is_denied_and_exact_grant_allows(world: Any) -> None:
    with acting(world.a, world.ana) as ectx:
        assert ectx.permissions == {} and ectx.membership_id == world.membership
        assert not has_permission(ectx, "users.view") and not can(ectx, "users.view")
        with pytest.raises(AccessDenied) as error:
            require(ectx, "users.view")
        assert error.value.reason is Denied.PERMISSION  # ser miembro no da ningún permiso
    give(world.a, world.membership, {"users.view": None})
    with acting(world.a, world.ana) as ectx:
        assert can(ectx, "users.view") and not can(ectx, "users.manage")  # solo lo concedido
        require(ectx, "users.view")


def test_roles_union_scopes_and_custom_roles_work(world: Any) -> None:
    give(world.a, world.membership, {VIEW: "OWN", "users.view": None}, code="warehouse_manager")
    give(world.a, world.membership, {VIEW: "TEAM"})
    with acting(world.a, world.ana) as ectx:
        assert ectx.permissions[VIEW] == {Scope.OWN, Scope.TEAM}  # unión, no "el mayor"
        assert ectx.permissions["users.view"] == {None}
        assert can(ectx, "users.view")  # un rol propio autoriza sin tocar el motor


def test_grants_of_another_membership_or_organization_never_leak(world: Any) -> None:
    luis = make_user(email="luis@example.com")
    give(world.a, join(world.a, luis).pk, {"users.view": None})
    give(world.b, join(world.b, world.ana).pk, {"users.manage": None})
    with acting(world.a, world.ana) as ectx:  # ni lo de luis en A ni lo suyo en B
        assert ectx.permissions == {} and ectx.membership_id == world.membership
    with acting(world.a, luis) as ectx:
        assert set(ectx.permissions) == {"users.view"}
    with acting(world.b, world.ana) as ectx:
        assert set(ectx.permissions) == {"users.manage"}


def test_owner_is_authorized_by_grants_not_by_code_or_flag(world: Any) -> None:
    with tenant_scope(ctx(world.a)) as scope:
        owner = next(role for role in clone_role_templates(scope) if role.is_owner_role)
        MembershipRole.objects.create(membership_id=world.membership, role=owner)
    with acting(world.a, world.ana) as ectx:
        assert all(can(ectx, code) for code in BY_CODE if not BY_CODE[code].supports_scope)
    with tenant_scope(ctx(world.a)):
        Role.objects.filter(pk=owner.pk).update(code="fundador", name="Otro nombre")
    with acting(world.a, world.ana) as ectx:
        assert can(ectx, "roles.manage")  # renombrarlo no cambia nada
    with tenant_scope(ctx(world.a)):  # Owner por código, nombre y marca, con una sola concesión
        Role.objects.filter(pk=owner.pk).update(code="owner", name="Owner")
        RolePermission.objects.filter(role=owner).exclude(permission_id="users.view").delete()
    with acting(world.a, world.ana) as ectx:
        assert set(ectx.permissions) == {"users.view"} and not can(ectx, "roles.manage")


def test_platform_staff_gets_nothing(world: Any) -> None:
    staff = User.objects.create_superuser("ops@example.com", TEST_PASSWORD)
    assert denied(world.a, staff) is Denied.MEMBERSHIP  # sin membresía
    join(world.a, staff)
    with acting(world.a, staff) as ectx:  # con membresía y sin roles: nada
        assert ectx.permissions == {} and not can(ectx, "organization.view")


@pytest.mark.parametrize("status", ["INVITED", "SUSPENDED", "DEACTIVATED"])
def test_membership_that_is_not_active_is_denied(world: Any, status: str) -> None:
    give(world.a, world.membership, {"users.view": None})
    with tenant_scope(ctx(world.a)):
        OrganizationMembership.objects.update(status=status)
    assert denied(world.a, world.ana) is Denied.MEMBERSHIP


def test_inactive_user_or_non_human_actor_is_denied(world: Any) -> None:
    give(world.a, world.membership, {"users.view": None})
    for tenant in (
        TenantContext(world.a, "test"),  # SYSTEM, sin usuario
        TenantContext(world.a, "test", world.ana.pk, ActorType.AI_AGENT, uuid4()),
        TenantContext(world.a, "test", None, ActorType.USER),
    ):
        assert denied(world.a, world.ana, tenant) is Denied.MEMBERSHIP
    User.objects.filter(pk=world.ana.pk).update(is_active=False)
    assert denied(world.a, world.ana) is Denied.MEMBERSHIP


def test_unknown_permission_raises_and_stale_grant_never_allows(
    world: Any, migrator: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    give(world.a, world.membership, {"users.manage": None, VIEW: "ORGANIZATION"})
    with acting(world.a, world.ana) as ectx:
        for check in (has_permission, can, require):
            with pytest.raises(UnknownPermission):
                check(ectx, "users.mange")  # una errata nunca concede
        with pytest.raises(UnknownPermission):
            scoped(ectx, "users.mange", Widget.objects.all())
    monkeypatch.delitem(BY_CODE, "users.manage")  # código retirado del catálogo, aún concedido
    with acting(world.a, world.ana) as ectx:
        assert "users.manage" not in ectx.permissions
    monkeypatch.setitem(BY_CODE, VIEW, PermissionDef(VIEW, "x"))  # el catálogo ya no admite alcance
    with acting(world.a, world.ana) as ectx:
        assert VIEW not in ectx.permissions  # concesión incoherente con el catálogo: se ignora
    scoped_now = PermissionDef("users.manage", "users", supports_scope=True)
    monkeypatch.setitem(BY_CODE, "users.manage", scoped_now)  # y al revés: ahora lo admite
    with acting(world.a, world.ana) as ectx:
        assert not ectx.permissions and not has_permission(ectx, "users.manage")


def test_other_tenant_is_denied(world: Any) -> None:
    give(world.a, world.membership, {"users.view": None, VIEW: "ORGANIZATION"})
    assert denied(world.b, world.ana) is Denied.MEMBERSHIP  # sin membresía en B
    foreign = Widget(id=world.orgs["widget_B"], organization_id=world.b, name="de B")
    with acting(world.a, world.ana) as ectx:
        assert not can(ectx, VIEW, foreign) and not can(ectx, "users.view", foreign)
        assert not can(ectx, "users.view", Organization(slug="x"))  # objeto sin organización
        with pytest.raises(AccessDenied) as error:
            require(ectx, VIEW, foreign)
        assert error.value.reason is Denied.SCOPE
        with pytest.raises(AccessDenied) as error:
            require(ectx, "users.view", foreign)  # tampoco con un permiso sin alcance
        assert error.value.reason is Denied.SCOPE


def test_context_requires_its_own_active_tenant_scope(world: Any) -> None:
    give(world.a, world.membership, {VIEW: "ORGANIZATION"})
    tenant = ctx(world.a, world.ana)
    with pytest.raises(TenantContextError):
        execution_context(tenant)  # fuera de tenant_scope
    with tenant_scope(ctx(world.b)), pytest.raises(TenantContextError):
        execution_context(tenant)  # dentro del scope de otra organización
    with acting(world.a, world.ana) as ectx:
        widgets = Widget.objects.all()  # perezoso: se construye dentro y se usa fuera
    checks = (
        lambda: has_permission(ectx, VIEW),
        lambda: can(ectx, VIEW),
        lambda: scoped(ectx, VIEW, widgets),
    )
    for check in checks:
        with pytest.raises(TenantContextError):
            check()  # fuera de toda transacción
        with tenant_scope(ctx(world.b)), pytest.raises(TenantContextError):
            check()  # dentro del scope de otra organización
        with tenant_scope(tenant), pytest.raises(TenantContextError, match="recalcularlo"):
            check()  # mismo contexto, otra transacción: la foto ya no vale


def test_scope_object_check_and_list_filter_agree(
    world: Any, migrator: psycopg.Connection[Any]
) -> None:
    me, other, team, branch = world.ana.pk, uuid4(), uuid4(), uuid4()
    rows = {
        "assigned": (me, None, None, None),
        "created": (None, me, None, None),
        "someone": (other, other, None, None),
        "team": (other, None, team, None),
        "branch": (None, other, None, branch),
        "nobody": (None, None, None, None),
        "elsewhere": (other, None, uuid4(), uuid4()),  # otro equipo y otra sucursal
    }
    insert = "INSERT INTO tenancy_app_widget (id, organization_id, name, assigned_user_id, "
    insert += "created_by_user_id, team_id, branch_id) VALUES (%s, %s, %s, %s, %s, %s, %s)"
    for name, columns in rows.items():
        migrator.execute(insert, [uuid4(), world.a, name, *columns])
    own = {"assigned", "created"}
    everything = {*rows, "widget A"}
    cases: list[tuple[set[str], dict[str, Any], set[str]]] = [
        ({"OWN"}, {}, own),
        ({"TEAM"}, {}, own),  # sin datos de equipo, TEAM equivale a OWN
        ({"BRANCH"}, {}, own),
        ({"TEAM"}, {"team_ids": frozenset({None})}, own),  # un None nunca casa con NULL
        ({"BRANCH"}, {"branch_ids": frozenset({None})}, own),
        ({"TEAM"}, {"team_ids": frozenset({team})}, own | {"team"}),
        ({"BRANCH"}, {"team_ids": frozenset({team})}, own),  # BRANCH no ve filas de equipo
        ({"TEAM"}, {"branch_ids": frozenset({branch})}, own),  # ni TEAM las de sucursal
        ({"BRANCH"}, {"branch_ids": frozenset({branch})}, own | {"branch"}),
        ({"TEAM", "BRANCH"}, {"team_ids": frozenset({team}), "branch_ids": frozenset({branch})},
         own | {"team", "branch"}),
        ({"ORGANIZATION"}, {}, everything),
        ({"OWN", "ORGANIZATION"}, {}, everything),
    ]  # fmt: skip
    for granted, extra, expected in cases:
        permissions = {VIEW: frozenset(Scope(scope) for scope in granted)}
        with acting(world.a, world.ana, permissions=permissions, **extra) as ectx:
            listed = set(scoped(ectx, VIEW, Widget.objects.all()).values_list("name", flat=True))
            allowed = {widget.name for widget in Widget.objects.all() if can(ectx, VIEW, widget)}
            assert listed == allowed == expected, granted


def test_unscoped_permission_ignores_ownership_and_scoped_needs_an_object(world: Any) -> None:
    give(world.a, world.membership, {"users.view": None, VIEW: "OWN"})
    with acting(world.a, world.ana) as ectx:
        widget = Widget.objects.get()
        assert can(ectx, "users.view", widget)  # sin alcance: cualquier objeto del tenant
        assert set(scoped(ectx, "users.view", Widget.objects.all())) == {widget}
        assert has_permission(ectx, VIEW) and not can(ectx, VIEW, widget)  # no es suyo
        for check in (can, require):
            with pytest.raises(TypeError):
                check(ectx, VIEW)  # con alcance hay que pasar el objeto
        with pytest.raises(AccessDenied) as error:
            require(ectx, VIEW, widget)
        assert error.value.reason is Denied.SCOPE
        assert not scoped(ectx, "users.manage", Widget.objects.all()).exists()  # sin permiso


def test_missing_or_invalid_scope_policy_fails_closed(
    world: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    give(world.a, world.membership, {VIEW: "ORGANIZATION"})
    with acting(world.a, world.ana) as ectx:
        part = WidgetPart(organization_id=world.a)  # modelo sin política declarada
        with pytest.raises(ScopePolicyMissing):
            can(ectx, VIEW, part)  # ni con ORGANIZATION
        with pytest.raises(ScopePolicyMissing):
            scoped(ectx, VIEW, WidgetPart.objects.all())
        with pytest.raises(TypeError):
            scoped(ectx, VIEW, Organization.objects.all())  # no es tenant-owned
    monkeypatch.setattr(scopes, "_REGISTRY", {})
    for model, policy in (
        (Organization, FieldScopes(("id",))),
        (Widget, FieldScopes(())),
        (Widget, FieldScopes(("no_existe",))),
        (Widget, FieldScopes(("assigned_user_id",), team="no_existe")),
        (WidgetPart, FieldScopes(("widget",))),  # FK por su nombre, no por su columna
        (Widget, FieldScopes(("widgetpart",))),  # relación inversa
    ):
        with pytest.raises(ImproperlyConfigured):
            register(model, policy)
    register(Widget, FieldScopes(("assigned_user_id",)))
    register(Widget, FieldScopes(("assigned_user_id",)))  # idéntica: no falla
    with pytest.raises(ImproperlyConfigured):
        register(Widget, FieldScopes(("created_by_user_id",)))  # otra distinta: sí


def test_rls_still_isolates_under_organization_scope(world: Any) -> None:
    give(world.a, world.membership, {VIEW: "ORGANIZATION", "users.view": None})
    with acting(world.a, world.ana) as ectx:
        ids = set(scoped(ectx, VIEW, Widget.objects.all()).values_list("id", flat=True))
        assert ids == {world.orgs["widget_A"]}  # ORGANIZATION es esta organización, no todas
        unmanaged = Widget._base_manager.all()  # sin TenantManager: el filtro lo pone el motor
        for code in (VIEW, "users.view"):
            assert str(world.a) in str(scoped(ectx, code, unmanaged).query), code
        with connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM tenancy_app_widget")
            assert cursor.fetchone() == (1,)  # RLS: segunda barrera, también con SQL directo


def test_context_costs_two_queries_and_decisions_none(world: Any) -> None:
    for _ in range(3):
        give(world.a, world.membership, {VIEW: "OWN", "users.view": None})
    tenant = ctx(world.a, world.ana)
    with tenant_scope(tenant):
        with CaptureQueriesContext(connection) as queries:
            ectx = execution_context(tenant)
        assert len(queries) == 2  # constante, con uno o con tres roles
        widget = Widget.objects.get()
        with CaptureQueriesContext(connection) as queries:
            can(ectx, VIEW, widget), has_permission(ectx, VIEW)
            scoped(ectx, VIEW, Widget.objects.all())
        assert len(queries) == 0


def test_decision_code_never_reads_role_names_owner_flag_or_staff() -> None:
    for module in (selectors, scopes):
        source = Path(str(module.__file__)).read_text()
        code = "\n".join(line.split("#")[0] for line in source.splitlines() if '"""' not in line)
        words = ("is_owner_role", "is_platform_staff", "is_system", ".code ==", ".name ==")
        for forbidden in (*words, "role__code", "role__name", "code=", "name="):
            assert forbidden not in code, (module.__name__, forbidden)
    assert selectors.ACTIVE == OrganizationMembership.Status.ACTIVE
    related = MembershipRole._meta.get_field("membership").related_model
    assert related is OrganizationMembership  # el mismo modelo que resuelve get_model()

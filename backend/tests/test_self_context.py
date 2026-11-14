"""F2-11: `GET /api/v1/o/{slug}/me/`, el contexto autorizado del usuario en una organización.
Middleware, sesión y motor de autorización reales."""

from typing import Any
from uuid import UUID

import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

from apps.access.catalog import BY_CODE
from apps.access.models import MembershipRole, Role
from apps.access.selectors import organization_of, role_names
from apps.access.services import clone_role_templates
from apps.accounts.models import User
from apps.organizations.models import OrganizationMembership
from config.settings import base
from core.tenancy.context import TenantContextError
from core.tenancy.scope import tenant_scope
from tests import test_authorization
from tests.factories import TEST_PASSWORD, make_user, sign_in
from tests.test_authorization import VIEW, acting, give
from tests.test_memberships import ctx, join

world = test_authorization.world
pytestmark = pytest.mark.usefixtures("tenant_db")
NOT_FOUND = (404, b'{"code":"NOT_FOUND"}')


@pytest.fixture(autouse=True)
def real_stack(settings: Any) -> None:
    settings.ROOT_URLCONF = "config.urls"
    settings.MIDDLEWARE = base.MIDDLEWARE
    settings.TENANCY_MEMBERSHIP_RESOLVER = base.TENANCY_MEMBERSHIP_RESOLVER


def me(client: Client, org: str = "org-a") -> Any:
    return client.get(f"/api/v1/o/{org}/me/")


def reply(response: Any) -> tuple[int, bytes]:
    return response.status_code, response.content


def signed(user: User) -> Client:
    client = Client(enforce_csrf_checks=True)
    sign_in(client, user)
    return client


def test_a_member_without_roles_gets_an_empty_context(world: Any) -> None:
    User.objects.filter(pk=world.ana.pk).update(first_name="Ana", last_name="López")
    assert me(signed(world.ana)).json() == {
        "user": {
            "id": str(world.ana.pk),
            "email": "ana@example.com",
            "first_name": "Ana",
            "last_name": "López",
        },
        "organization": {"id": str(world.a), "slug": "org-a", "name": "Org A"},
        "membership_id": str(world.membership),
        "roles": [],
        "permissions": [],  # ser miembro no da ningún permiso
        "mfa": {"required": False, "missing": False},  # F2-115: sin roles no lo exige
    }


def test_permissions_come_with_their_scopes_and_roles_are_only_names(world: Any) -> None:
    held = (("c", "Alfa", "TEAM"), ("a", "Zeta", "OWN"), ("b", "Alfa", "ORGANIZATION"))
    for code, name, scope in held:
        role = give(world.a, world.membership, {VIEW: scope, "users.view": None}, code=code)
        with tenant_scope(ctx(world.a)):
            Role.objects.filter(pk=role.pk).update(name=name)
    give(world.a, world.membership, {VIEW: "BRANCH"}, code="d")
    client = signed(world.ana)
    with CaptureQueriesContext(connection) as queries:
        context = me(client).json()
    assert context["permissions"] == [
        {"code": "users.view", "scopes": []},  # un permiso sin alcance
        {"code": VIEW, "scopes": ["BRANCH", "ORGANIZATION", "OWN", "TEAM"]},  # la unión, ordenada
    ]
    assert context["roles"] == [  # por nombre y, a igual nombre, por código
        {"code": "b", "name": "Alfa"},
        {"code": "c", "name": "Alfa"},
        {"code": "d", "name": "Rol propio"},
        {"code": "a", "name": "Zeta"},
    ]
    assert len(queries) <= 18  # no crece con el número de roles ni de permisos (+2: `mfa`)


def test_the_owner_sees_the_whole_catalog_and_a_change_shows_on_the_next_request(
    world: Any,
) -> None:
    client = signed(world.ana)
    with tenant_scope(scope := ctx(world.a)):
        owner = next(role for role in clone_role_templates(scope) if role.is_owner_role)
        assignment = MembershipRole.objects.create(membership_id=world.membership, role=owner)
    context = me(client).json()
    assert {grant["code"] for grant in context["permissions"]} >= set(BY_CODE) - {VIEW}
    assert context["roles"] == [{"code": "owner", "name": "Owner"}]
    assert "is_owner_role" not in str(context)  # la marca de Owner no sale: no autoriza
    with tenant_scope(ctx(world.a)):
        assignment.delete()
    after = me(client).json()  # sin caché: la siguiente petición ya lo ve
    assert after["permissions"] == [] and after["roles"] == []


def test_nothing_of_another_organization_or_member_leaks(world: Any) -> None:
    luis = make_user(email="luis@example.com")
    give(world.a, join(world.a, luis).pk, {"users.manage": None})
    give(world.b, join(world.b, world.ana).pk, {"roles.manage": None}, code="en-b")
    give(world.a, world.membership, {"users.view": None}, code="en-a")
    in_a, in_b = me(signed(world.ana)).json(), me(signed(world.ana), "org-b").json()
    assert [grant["code"] for grant in in_a["permissions"]] == ["users.view"]
    assert [role["code"] for role in in_a["roles"]] == ["en-a"]
    assert [grant["code"] for grant in in_b["permissions"]] == ["roles.manage"]
    assert (
        in_b["organization"]["slug"] == "org-b" and in_b["membership_id"] != in_a["membership_id"]
    )
    assert "luis" not in str(in_a) and "users.manage" not in str(in_a)


def test_only_an_active_member_reads_it_and_nobody_writes(world: Any) -> None:
    anonymous = me(Client())
    assert reply(anonymous) == (401, b'{"code":"NOT_AUTHENTICATED"}')
    client = signed(world.ana)
    assert reply(me(client, "org-b")) == NOT_FOUND  # sin membresía en B
    assert reply(me(client, "no-existe")) == NOT_FOUND  # el mismo 404
    staff = User.objects.create_superuser("ops@example.com", TEST_PASSWORD)
    assert reply(me(signed(staff))) == NOT_FOUND  # el staff de plataforma no es miembro
    join(world.a, staff)
    as_member = me(signed(staff)).json()  # y si lo es, tampoco recibe permisos por ser staff
    assert as_member["permissions"] == [] and as_member["roles"] == []
    assert client.head("/api/v1/o/org-a/me/").status_code == 200
    client.cookies["csrftoken"] = token = "t" * 32
    for method in ("options", "post", "put", "patch", "delete"):
        written = getattr(client, method)("/api/v1/o/org-a/me/", headers={"X-CSRFToken": token})
        assert reply(written) == (403, b'{"code":"PERMISSION_DENIED"}'), method
    with tenant_scope(ctx(world.a)):
        OrganizationMembership.objects.filter(pk=world.membership).update(status="SUSPENDED")
    assert reply(me(client)) == NOT_FOUND


def anyone(user: Any, organization_id: UUID) -> UUID:
    """Resolvedor demasiado permisivo: todo usuario pasa el middleware."""
    return user.pk  # type: ignore[no-any-return]


def test_is_member_denies_what_a_lax_resolver_lets_through(world: Any, settings: Any) -> None:
    settings.TENANCY_MEMBERSHIP_RESOLVER = f"{__name__}.anyone"
    client = signed(world.ana)
    client.cookies["csrftoken"] = token = "t" * 32
    for method in ("GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH", "DELETE"):
        statuses = {
            client.generic(
                method, f"/api/v1/o/{org}/me/", headers={"X-CSRFToken": token}
            ).status_code
            for org in ("org-b", "no-existe")
        }
        assert statuses == {404}, method  # sin membresía: 404 también al escribir, y no 403


def test_a_stale_context_fails_in_both_selectors(world: Any) -> None:
    tenant = ctx(world.a, world.ana)
    with acting(world.a, world.ana) as ectx:
        assert role_names(ectx) == [] and organization_of(ectx)["slug"] == "org-a"
    for selector in (role_names, organization_of):
        with pytest.raises(TenantContextError):
            selector(ectx)  # fuera de toda transacción
        with tenant_scope(ctx(world.b)), pytest.raises(TenantContextError):
            selector(ectx)  # scope de otra organización
        with tenant_scope(tenant), pytest.raises(TenantContextError, match="recalcularlo"):
            selector(ectx)  # mismo contexto, otra transacción

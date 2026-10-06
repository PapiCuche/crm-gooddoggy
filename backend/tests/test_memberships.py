"""F2-02: membresías usuario ↔ organización, RLS condicional (ADR-002 §3.2) y resolvedor real."""

from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from django.contrib.auth.models import AnonymousUser
from django.db import IntegrityError, ProgrammingError, connection
from django.test import Client, override_settings

from apps.accounts.models import User
from apps.organizations.models import OrganizationMembership as Membership
from apps.organizations.selectors import active_membership, organizations_for_user
from config.settings import base
from core.tenancy.context import ActorType, TenantContext, TenantContextError
from core.tenancy.scope import tenant_scope, user_scope
from tests.factories import TEST_PASSWORD, make_user, sign_in

pytestmark = pytest.mark.usefixtures("tenant_db")
REAL = override_settings(
    MIDDLEWARE=base.MIDDLEWARE, TENANCY_MEMBERSHIP_RESOLVER=base.TENANCY_MEMBERSHIP_RESOLVER
)
RAW = "SELECT organization_id, user_id FROM organization_memberships"


def ctx(org: UUID, user: User | None = None) -> TenantContext:
    if user is None:
        return TenantContext(org, "test")
    return TenantContext(org, "test", user.pk, ActorType.USER, user.pk)


def join(org: UUID, user: User, status: str = "ACTIVE") -> Membership:
    with tenant_scope(ctx(org)):
        membership: Membership = Membership.objects.create(user=user, status=status)
    return membership


def raw(sql: str = RAW, params: list[Any] | None = None) -> list[tuple[Any, ...]]:
    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        return cursor.fetchall() if cursor.description else [(cursor.rowcount,)]


@pytest.fixture
def ana(orgs: dict[str, UUID]) -> User:
    """Miembro de A y de B; `luis` solo de B (para comprobar que no se filtran filas ajenas)."""
    user = make_user(email="ana@example.com")
    join(orgs["A"], user)
    join(orgs["B"], user)
    join(orgs["B"], make_user(email="luis@example.com"))
    return user


def test_t14_with_tenant_only_that_organization_is_visible(
    orgs: dict[str, UUID], ana: User
) -> None:
    with tenant_scope(ctx(orgs["A"], ana)):  # app.user_id fijado: no amplía la visibilidad
        assert raw() == [(orgs["A"], ana.pk)]
        assert list(Membership.objects.values_list("user_id", flat=True)) == [ana.pk]
    with tenant_scope(ctx(orgs["B"], ana)):
        assert {user for _, user in raw()} == {ana.pk, User.objects.get(email__startswith="l").pk}


def test_t15_without_tenant_the_user_sees_only_their_memberships_and_cannot_write(
    orgs: dict[str, UUID], ana: User
) -> None:
    assert raw() == []  # sin contexto: nada
    with user_scope(ana.pk):
        assert sorted(raw()) == sorted([(orgs["A"], ana.pk), (orgs["B"], ana.pk)])
        assert raw("UPDATE organization_memberships SET status = 'SUSPENDED'") == [(0,)]
        assert raw("DELETE FROM organization_memberships") == [(0,)]
    insert = (
        "INSERT INTO organization_memberships (id, organization_id, user_id, status, "
        "created_at, updated_at) VALUES (uuidv7(), %s, %s, 'ACTIVE', now(), now())"
    )
    other = make_user()
    with pytest.raises(ProgrammingError, match="row-level security"), user_scope(other.pk):
        raw(insert, [orgs["A"], other.pk])
    with user_scope(ana.pk):
        assert len(raw()) == 2  # nada cambió


def test_tenant_cannot_write_memberships_of_another_organization(
    orgs: dict[str, UUID], ana: User
) -> None:
    other = make_user()
    with pytest.raises(TenantContextError), tenant_scope(ctx(orgs["A"])):
        Membership(organization_id=orgs["B"], user=other).save()  # el modelo lo impide
    with pytest.raises(ProgrammingError, match="row-level security"), tenant_scope(ctx(orgs["A"])):
        raw(  # y, por SQL directo, también RLS
            "INSERT INTO organization_memberships (organization_id, user_id, status, created_at,"
            " updated_at) VALUES (%s, %s, 'ACTIVE', now(), now())",
            [orgs["B"], other.pk],
        )
    with tenant_scope(ctx(orgs["A"])):
        assert raw("UPDATE organization_memberships SET status = 'SUSPENDED'") == [(1,)]
    with user_scope(ana.pk):
        assert Membership.for_user.filter(status="ACTIVE").count() == 1  # B intacta


def test_database_rejects_duplicates_bad_status_and_dangling_references(
    orgs: dict[str, UUID], ana: User, migrator: psycopg.Connection[Any]
) -> None:
    with pytest.raises(IntegrityError), tenant_scope(ctx(orgs["A"])):
        Membership.objects.create(user=ana)  # UNIQUE (organization_id, user_id)
    insert = (
        "INSERT INTO organization_memberships (organization_id, user_id, status, created_at, "
        "updated_at) VALUES (%s, %s, %s, now(), now())"
    )
    user = make_user()
    with pytest.raises(psycopg.errors.CheckViolation):
        migrator.execute(insert, [orgs["A"], user.pk, "OWNER"])  # un rol no es un estado
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        migrator.execute(insert, [orgs["A"], uuid4(), "ACTIVE"])  # usuario inexistente
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        migrator.execute(insert, [uuid4(), user.pk, "ACTIVE"])  # organización inexistente


def test_membership_has_no_role_and_user_stays_global() -> None:
    """Los roles irán en `membership_roles` (F2-04): ni la membresía ni el usuario llevan uno."""
    fields = {field.name for field in Membership._meta.get_fields()}
    assert fields == {
        "id",
        "organization_id",
        "user",
        "status",
        "default_branch",  # su sucursal (F2-68): un dato de la membresía, no un rol
        "created_at",
        "updated_at",
    }
    assert not any(field.is_relation for field in User._meta.get_fields())


def test_resolver_grants_only_active_members_of_that_organization(
    orgs: dict[str, UUID], ana: User
) -> None:
    assert active_membership(ana, orgs["A"]) == ana.pk
    assert active_membership(ana, uuid4()) is None
    luis = User.objects.get(email="luis@example.com")
    assert active_membership(luis, orgs["A"]) is None  # miembro solo de B
    assert active_membership(AnonymousUser(), orgs["A"]) is None
    assert active_membership(None, orgs["A"]) is None


@pytest.mark.parametrize("status", ["INVITED", "SUSPENDED", "DEACTIVATED"])
def test_resolver_denies_memberships_that_are_not_active(
    orgs: dict[str, UUID], status: str
) -> None:
    user = make_user()
    join(orgs["A"], user, status)
    assert active_membership(user, orgs["A"]) is None
    assert organizations_for_user(user) == []


def test_resolver_denies_inactive_users_and_platform_staff_without_membership(
    orgs: dict[str, UUID], ana: User
) -> None:
    staff = User.objects.create_superuser("ops@example.com", TEST_PASSWORD)
    assert active_membership(staff, orgs["A"]) is None  # sin membresía implícita
    assert organizations_for_user(staff) == []
    ana.is_active = False
    assert active_membership(ana, orgs["A"]) is None
    assert organizations_for_user(ana) == []


def test_organizations_for_user_lists_accessible_organizations(
    orgs: dict[str, UUID], ana: User, migrator: psycopg.Connection[Any]
) -> None:
    assert [(o.id, o.slug) for o in organizations_for_user(ana)] == [
        (orgs["A"], "org-a"),
        (orgs["B"], "org-b"),
    ]
    migrator.execute("UPDATE organizations SET status = 'SUSPENDED' WHERE slug = 'org-b'")
    assert [o.slug for o in organizations_for_user(ana)] == ["org-a"]
    assert organizations_for_user(AnonymousUser()) == []


@REAL
def test_http_member_reaches_only_their_tenant(
    orgs: dict[str, UUID], migrator: psycopg.Connection[Any]
) -> None:
    """Middleware y resolvedor reales: la URL selecciona, la membresía autoriza."""
    user = make_user()
    join(orgs["A"], user)
    client = Client()
    sign_in(client, user)
    assert client.get("/api/v1/o/org-a/widgets/").json() == {"ids": [str(orgs["widget_A"])]}
    not_found = (404, {"code": "NOT_FOUND"})
    for url in ("/api/v1/o/org-b/widgets/", "/api/v1/o/no-existe/widgets/"):
        response = client.get(url)  # otra organización e inexistente: indistinguibles
        assert (response.status_code, response.json()) == not_found
    response = client.get(f"/api/v1/o/org-a/widgets/{orgs['widget_B']}/")  # id de otro tenant
    assert (response.status_code, response.json()) == not_found

    migrator.execute("UPDATE organizations SET status = 'SUSPENDED'")
    assert client.get("/api/v1/o/org-a/widgets/").json() == {"code": "ORG_SUSPENDED"}
    assert client.get("/api/v1/o/org-b/widgets/").status_code == 404  # no miembro: no aprende
    migrator.execute("UPDATE organizations SET status = 'ACTIVE'")

    migrator.execute("UPDATE organization_memberships SET status = 'DEACTIVATED'")
    assert client.get("/api/v1/o/org-a/widgets/").status_code == 404
    migrator.execute("UPDATE organization_memberships SET status = 'ACTIVE'")
    User.objects.filter(pk=user.pk).update(is_active=False)
    assert client.get("/api/v1/o/org-a/widgets/").status_code == 401  # sesión ya no válida

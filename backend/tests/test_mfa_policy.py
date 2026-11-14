"""F2-115: la política de segundo factor por membresía (ADR-023 §9, ADR-038), con `MFA_ENFORCEMENT`
en `warn` y en `enforce`. Middleware, sesión, roles y PostgreSQL reales."""

from types import SimpleNamespace
from typing import Any
from uuid import UUID

import psycopg
import pytest
from django.core.exceptions import ImproperlyConfigured
from django.test import Client

from apps.access.catalog import ADMIN_TEMPLATE
from apps.access.models import MembershipRole, Role, RolePermission
from apps.access.second_factor import required as second_factor_required
from core.tenancy.context import TenantContext
from core.tenancy.scope import tenant_scope
from core.tenancy.second_factor import MESSAGE, SecondFactor, evaluate
from tests import test_contacts_api
from tests.factories import make_user
from tests.test_authorization import give
from tests.test_memberships import ctx, join
from tests.test_mfa_second_factor import enrolled
from tests.test_self_context import reply, signed

crm, real_stack = test_contacts_api.crm, test_contacts_api.real_stack
pytestmark = pytest.mark.usefixtures("tenant_db", "real_stack")
NOT_FOUND = (404, b'{"code":"NOT_FOUND"}')
REFUSED = {"code": "MFA_REQUIRED", "message": MESSAGE}
WARNED, CLEAR = {"required": True, "missing": True}, {"required": False, "missing": False}


@pytest.fixture
def team(crm: SimpleNamespace) -> SimpleNamespace:
    """`crm` (ana Owner, vera Vendedora, sam Supervisor, bea sin rol; luis solo de B) y ada, con
    el Administrador de plantilla. Ana es además miembro de B, sin roles."""
    crm.ada = make_user(email="ada@example.com")
    crm.m_ada = join(crm.a, crm.ada).pk
    with tenant_scope(ctx(crm.a)):
        admin = Role.objects.get(is_system=True, code=ADMIN_TEMPLATE)
        MembershipRole.objects.create(membership_id=crm.m_ada, role=admin)
    crm.admin = admin.pk
    join(crm.b, crm.ana)
    return crm


def required(org: UUID, user: Any) -> bool:
    tenant = ctx(org, user)
    with tenant_scope(tenant):
        return second_factor_required(tenant)


def get(user: Any, org: str = "org-a", route: str = "me/") -> Any:
    return signed(user).get(f"/api/v1/o/{org}/{route}")


def test_the_owner_the_template_admin_or_a_sensitive_permission_require_it(team: Any) -> None:
    assert required(team.a, team.ana) and required(team.a, team.ada)  # Owner, Administrador
    assert not any(required(team.a, who) for who in (team.vera, team.sam, team.bea))
    assert not required(team.b, team.ana)  # por membresía: en B no tiene roles
    give(team.a, team.m["bea"], {"audit.view": None})  # un rol propio con un permiso sensible
    assert required(team.a, team.bea)
    give(team.a, team.m["sam"], {"users.view": None}, code="admin-propio")
    assert not required(team.a, team.sam)  # un rol propio, sin permiso sensible, no lo exige


def test_stripping_the_template_admin_or_renaming_it_does_not_free_it(team: Any) -> None:
    with tenant_scope(ctx(team.a)):
        gone = RolePermission.objects.filter(role_id=team.admin, permission__is_sensitive=True)
        assert "users.manage" in {g.permission_id for g in gone} and gone.delete()[0] > 1
        Role.objects.filter(pk=team.admin).update(name="Gerente")
    assert required(team.a, team.ada)  # por ser de plantilla y su código, nunca por el nombre
    with tenant_scope(ctx(team.b)):  # en B no hay plantillas: un rol propio con ese código
        own = Role.objects.create(code=ADMIN_TEMPLATE, name="Administrador")
        luis = MembershipRole.objects.create(membership_id=team.m_luis, role=own)
    assert not required(team.b, team.luis)  # no es de plantilla: no lo exige
    with tenant_scope(ctx(team.b)):
        luis.delete()
        own.delete()


def test_warn_opens_everything_and_me_says_it_is_required_and_missing(team: Any) -> None:
    assert get(team.ana).json()["mfa"] == WARNED
    assert get(team.ana, route="roles/").status_code == 200
    assert get(team.ana, "org-b").json()["mfa"] == CLEAR
    assert get(team.vera).json()["mfa"] == CLEAR
    enrolled(team.ana)
    assert get(team.ana).json()["mfa"] == {"required": True, "missing": False}


def test_enforce_closes_every_route_of_that_organization_until_a_device_is_confirmed(
    team: Any, settings: Any, migrator: psycopg.Connection[Any]
) -> None:
    settings.MFA_ENFORCEMENT = "enforce"
    for who in (team.ana, team.ada):
        for route in ("me/", "roles/", "members/", "contacts/"):
            answer = get(who, route=route)
            assert (answer.status_code, answer.json()) == (403, REFUSED), (who.email, route)
    assert get(team.ana, "org-b").json()["mfa"] == CLEAR  # su otra organización no lo exige
    assert get(team.vera).json()["mfa"] == CLEAR and get(team.bea).status_code == 200
    assert reply(get(team.luis)) == NOT_FOUND  # quien no es miembro no aprende nada
    assert reply(get(team.ana, "no-existe")) == NOT_FOUND
    enrolled(team.ada, confirmed=False)  # un alta a medias no cuenta
    assert get(team.ada).status_code == 403
    enrolled(team.ana)
    assert get(team.ana).json()["mfa"] == {"required": True, "missing": False}
    assert get(team.ana, route="roles/").status_code == 200
    migrator.execute("UPDATE organizations SET status = 'SUSPENDED' WHERE id = %s", [team.a])
    assert get(team.ada).json() == {"code": "ORG_SUSPENDED"}  # la organización, antes


def answer_true(ctx: TenantContext) -> object:
    return True  # una política que no devuelve un `SecondFactor`


def test_a_policy_or_a_mode_that_does_not_answer_fails_closed(team: Any, settings: Any) -> None:
    settings.MFA_ENFORCEMENT = "enforce"
    settings.TENANCY_SECOND_FACTOR_POLICY = f"{__name__}.answer_true"
    client = signed(team.vera)
    with pytest.raises(TypeError):
        client.get("/api/v1/o/org-a/me/")
    settings.MFA_ENFORCEMENT = "strict"
    with pytest.raises(ImproperlyConfigured):
        client.get("/api/v1/o/org-a/me/")
    with pytest.raises(Exception, match="tenant_scope"):
        evaluate(ctx(team.a, team.vera))  # fuera de su scope no responde


def test_the_answer_says_missing_only_when_required() -> None:
    assert SecondFactor(True, False).missing
    assert not SecondFactor(True, True).missing and not SecondFactor(False, False).missing


def test_without_a_session_it_is_still_401(team: Any, settings: Any) -> None:
    settings.MFA_ENFORCEMENT = "enforce"
    assert Client().get("/api/v1/o/org-a/me/").status_code == 401

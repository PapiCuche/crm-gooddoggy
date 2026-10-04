"""F2-19: `PUT /api/v1/o/{slug}/members/{id}/status/` suspende y reactiva a un miembro (ADR-017).
Middleware, sesión, motor de autorización y PostgreSQL con el rol `crm_app`."""

from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from django.core.exceptions import ObjectDoesNotExist
from django.test import Client

from apps.access.catalog import BY_CODE
from apps.access.models import MembershipRole, RolePermission
from apps.access.selectors import AccessDenied, Denied
from apps.access.services import assign_role, ensure_can_manage_member
from apps.accounts.models import User
from apps.members.api import views
from apps.members.services import set_member_status
from apps.organizations import services as organizations
from apps.organizations.models import OrganizationMembership
from apps.organizations.services import InvalidTransition, set_membership_status
from core.tenancy.context import TenantContextError
from core.tenancy.scope import tenant_scope
from tests import test_anti_escalation, test_authorization, test_self_context
from tests.factories import make_user
from tests.test_anti_escalation import EDIT, acting, audit, race, state
from tests.test_authorization import VIEW, give
from tests.test_memberships import ctx, join
from tests.test_self_context import NOT_FOUND, me, reply, signed

world, rbac = test_authorization.world, test_anti_escalation.rbac
real_stack = test_self_context.real_stack
pytestmark = pytest.mark.usefixtures("tenant_db")
DENIED = (403, b'{"code":"PERMISSION_DENIED"}')
STATUS = "SELECT status FROM organization_memberships WHERE id = %s"


def switch(client: Client, member: Any, status: Any = "SUSPENDED", org: str = "org-a") -> Any:
    client.cookies["csrftoken"] = token = "t" * 32
    path, body = f"/api/v1/o/{org}/members/{member}/status/", {"status": status}
    return client.put(path, body, "application/json", headers={"X-CSRFToken": token})


def denied(org: UUID, user: User, member: Any, status: str = "SUSPENDED") -> Denied:
    with pytest.raises(AccessDenied) as error, acting(org, user) as tenant:
        set_member_status(tenant, membership_id=member, status=status)
    return error.value.reason


def status_of(migrator: psycopg.Connection[Any], member: UUID) -> Any:
    return migrator.execute(STATUS, [member]).fetchone()[0]  # type: ignore[index]


def test_suspending_closes_the_organization_at_once_and_reactivating_restores_it(
    rbac: Any, migrator: psycopg.Connection[Any]
) -> None:
    give(rbac.a, rbac.m_eva, {"users.view": None}, code="lectora")
    join(rbac.b, rbac.eva)  # su otra organización no se toca
    ana, eva = signed(rbac.ana), signed(rbac.eva)
    before, roles = state(migrator), me(eva).json()["roles"]
    done = switch(ana, rbac.m_eva)
    assert reply(done) == (200, b'{"id":"%s","status":"SUSPENDED"}' % str(rbac.m_eva).encode())
    assert reply(me(eva)) == NOT_FOUND  # desde su siguiente petición, sin cerrar su sesión
    listed = [row["slug"] for row in eva.get("/api/v1/me/organizations/").json()]
    assert listed == ["org-b"] and me(eva, "org-b").status_code == 200
    assert switch(ana, rbac.m_eva).status_code == 200  # repetirlo no cambia nada ni audita
    assert state(migrator) == (before[0], before[1], before[2] + 1)
    assert switch(ana, rbac.m_eva, "ACTIVE").json() == {"id": str(rbac.m_eva), "status": "ACTIVE"}
    assert switch(ana, rbac.m_eva, "ACTIVE").status_code == 200
    assert me(eva).json()["roles"] == roles == [{"code": "lectora", "name": "Rol propio"}]
    assert audit(migrator)[-2:] == [
        (action, "membership", rbac.m_eva, {"status": change}, {}, rbac.ana.pk)
        for action, change in (
            ("membership.suspended", ["ACTIVE", "SUSPENDED"]),
            ("membership.reactivated", ["SUSPENDED", "ACTIVE"]),
        )
    ]
    assert state(migrator) == (before[0], before[1], before[2] + 2)


def test_without_session_permission_or_membership_it_changes_and_reveals_nothing(
    rbac: Any, migrator: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    other = join(rbac.b, make_user(email="otra@example.com")).pk
    rest = [code for code in BY_CODE if code not in ("users.manage", VIEW, EDIT)]
    give(rbac.a, rbac.m_eva, dict.fromkeys(rest))  # eva: todo el catálogo menos `users.manage`
    before, eva = state(migrator), signed(rbac.eva)
    assert switch(Client(), rbac.m_luis).status_code == 401
    for member in (rbac.m_luis, other, uuid4()):  # sin el permiso no se aprende quién existe
        assert reply(switch(eva, member)) == DENIED
    assert reply(switch(eva, rbac.m_luis, "X")) == DENIED  # ni se valida el cuerpo
    assert denied(rbac.a, rbac.eva, rbac.m_luis) is Denied.PERMISSION  # el servicio lo relee
    ana = signed(rbac.ana)
    for member in (other, uuid4()):  # de otra organización: no existe
        assert reply(switch(ana, member)) == NOT_FOUND
    assert reply(switch(ana, other, org="org-b")) == NOT_FOUND  # ana no es de B
    assert reply(switch(ana, "no-es-uuid")) == NOT_FOUND
    assert reply(switch(ana, rbac.m_eva, org="no-existe")) == NOT_FOUND
    url = f"/api/v1/o/org-a/members/{rbac.m_eva}/status/"
    no_token = Client(enforce_csrf_checks=True)
    no_token.force_login(rbac.ana)
    refused = no_token.put(url, {"status": "SUSPENDED"}, "application/json")
    assert refused.json()["code"] == "CSRF_FAILED"
    for bad in ("DEACTIVATED", "INVITED", "active", "", None, 1, ["SUSPENDED"]):
        answer = switch(ana, rbac.m_eva, bad)
        assert (answer.status_code, answer.json()["code"]) == (400, "VALIDATION_ERROR"), bad
        assert list(answer.json()["fields"]) == ["status"]
    token = {"X-CSRFToken": ana.cookies["csrftoken"].value}
    assert ana.put(url, {}, "application/json", headers=token).status_code == 400
    for other_method in (ana.get, ana.post, ana.patch, ana.delete):  # solo PUT
        assert reply(other_method(url, headers=token)) == DENIED
    assert state(migrator) == before and status_of(migrator, rbac.m_eva) == "ACTIVE"
    with acting(rbac.a, rbac.ana) as tenant, pytest.raises(ObjectDoesNotExist):
        set_member_status(tenant, membership_id=other, status="SUSPENDED")
    assert status_of(migrator, other) == "ACTIVE"

    def gone(*args: Any, **kwargs: Any) -> None:
        raise AccessDenied(Denied.MEMBERSHIP)

    monkeypatch.setattr(views, "set_member_status", gone)  # suspendido mientras esperaba
    assert reply(switch(ana, rbac.m_eva)) == NOT_FOUND  # como cualquiera que no es miembro


def test_nobody_changes_themselves_or_a_member_they_could_not_assign_roles_to(
    rbac: Any, migrator: psycopg.Connection[Any]
) -> None:
    members = {
        name: join(rbac.a, make_user(email=f"{name}@example.com")).pk
        for name in ("plain", "narrow", "wide", "admin", "mixed")
    }
    give(rbac.a, members["narrow"], {VIEW: "OWN"})  # luis lo tiene con TEAM: lo cubre
    give(rbac.a, members["wide"], {VIEW: "ORGANIZATION"})  # más alcance que luis
    give(rbac.a, members["admin"], {"users.manage": None})  # sensible: solo un Owner
    give(rbac.a, members["mixed"], {VIEW: "OWN"})
    give(rbac.a, members["mixed"], {"users.view": None})  # un rol cubierto y otro no
    expected = {
        rbac.m_luis: Denied.SELF,
        rbac.membership: Denied.ESCALATION,  # la Owner: tiene lo que luis no
        members["wide"]: Denied.ESCALATION,
        members["mixed"]: Denied.ESCALATION,
        members["admin"]: Denied.SENSITIVE,
    }
    before = state(migrator)
    for own in (str(rbac.m_luis), str(rbac.m_luis).upper(), rbac.m_luis.hex):  # también en texto
        assert denied(rbac.a, rbac.luis, own) is Denied.SELF
    for status in ("SUSPENDED", "ACTIVE"):  # reactivar exige lo mismo que suspender
        for member, reason in expected.items():
            assert denied(rbac.a, rbac.luis, member, status) is reason
        with tenant_scope(ctx(rbac.a)):  # suspendidos por otra vía: reactivarlos también se niega
            OrganizationMembership.objects.exclude(pk__in=[rbac.m_luis, rbac.membership]).update(
                status="SUSPENDED"
            )
    assert state(migrator) == before
    luis, ana = signed(rbac.luis), signed(rbac.ana)
    for member in expected:
        assert reply(switch(luis, member, "ACTIVE")) == DENIED  # sin decir cuál de las reglas
    for name in ("plain", "narrow"):  # sin roles, o con roles que luis cubre
        assert switch(luis, members[name], "ACTIVE").status_code == 200
        assert switch(luis, members[name]).status_code == 200
    assert reply(switch(ana, rbac.membership)) == DENIED  # tampoco una Owner a sí misma
    assert switch(ana, members["admin"], "ACTIVE").status_code == 200  # la Owner sí lo cubre
    assert state(migrator) == (before[0], before[1], before[2] + 5)


def test_an_active_owner_always_remains(rbac: Any, migrator: psycopg.Connection[Any]) -> None:
    owner = rbac.roles["owner"]
    with tenant_scope(ctx(rbac.a)):
        MembershipRole.objects.create(membership_id=rbac.m_eva, role=owner)  # segunda Owner
    assert denied(rbac.a, rbac.luis, rbac.m_eva) is Denied.ESCALATION  # solo otro Owner
    assert denied(rbac.a, rbac.ana, str(rbac.membership)) is Denied.SELF  # ni con otra Owner

    def suspending(target: UUID) -> Any:
        return lambda tenant: set_member_status(tenant, membership_id=target, status="SUSPENDED")

    changes = {
        "ana": (rbac.ana, suspending(rbac.m_eva)),
        "eva": (rbac.eva, suspending(rbac.membership)),
    }
    outcome = race(rbac.a, changes, hold="ana")  # eva espera el bloqueo y relee: ya no es miembro
    assert outcome == {"ana": "ok", "eva": Denied.MEMBERSHIP}
    assert (status_of(migrator, rbac.membership), status_of(migrator, rbac.m_eva)) == (
        "ACTIVE",
        "SUSPENDED",
    )
    with acting(rbac.a, rbac.ana) as tenant:
        set_member_status(tenant, membership_id=rbac.m_eva, status="ACTIVE")
    outcome = race(rbac.a, changes, hold=None)  # a la vez: gana cualquiera, y solo una
    assert sorted(map(str, outcome.values())) == ["membership", "ok"]
    statuses = {status_of(migrator, rbac.membership), status_of(migrator, rbac.m_eva)}
    assert statuses == {"ACTIVE", "SUSPENDED"}
    # Un rol Owner sin permisos sensibles lo cubre quien no es Owner: decide el recuento.
    with tenant_scope(ctx(rbac.a)):
        OrganizationMembership.objects.update(status="ACTIVE")
        MembershipRole.objects.filter(membership_id=rbac.m_eva, role=owner).delete()
        RolePermission.objects.filter(role=owner).exclude(permission_id="users.view").delete()
    give(rbac.a, rbac.m_luis, {"users.view": None})
    before = state(migrator)
    assert denied(rbac.a, rbac.luis, rbac.membership) is Denied.LAST_OWNER
    last = switch(signed(rbac.luis), rbac.membership)
    assert (last.status_code, last.json()["code"]) == (409, "LAST_OWNER")
    assert set(last.json()) == {"code", "message"}
    assert state(migrator) == before and status_of(migrator, rbac.membership) == "ACTIVE"
    with acting(rbac.a, rbac.luis) as tenant:  # reactivar no cuenta Owners
        assert not set_member_status(tenant, membership_id=rbac.membership, status="ACTIVE")
    with tenant_scope(ctx(rbac.a)):  # ya suspendida: repetirlo no es dejar de ser Owner
        OrganizationMembership.objects.filter(pk=rbac.membership).update(status="SUSPENDED")
    assert switch(signed(rbac.luis), rbac.membership).status_code == 200
    with tenant_scope(ctx(rbac.a)):
        OrganizationMembership.objects.filter(pk=rbac.membership).update(status="ACTIVE")
    assert state(migrator) == before
    with tenant_scope(ctx(rbac.a)):
        MembershipRole.objects.create(membership_id=rbac.m_eva, role=owner)
    assert switch(signed(rbac.luis), rbac.membership).status_code == 200  # queda eva


def test_an_invited_or_deactivated_membership_is_not_switched(
    rbac: Any, migrator: psycopg.Connection[Any]
) -> None:
    ana = signed(rbac.ana)
    for status in ("INVITED", "DEACTIVATED"):
        member = join(rbac.a, make_user(), status).pk
        before = state(migrator)
        for wanted in ("ACTIVE", "SUSPENDED"):
            answer = switch(ana, member, wanted)
            assert (answer.status_code, answer.json()["code"]) == (409, "INVALID_TRANSITION")
            assert set(answer.json()) == {"code", "message"}
        assert state(migrator) == before and status_of(migrator, member) == status
        with acting(rbac.a, rbac.ana) as tenant, pytest.raises(InvalidTransition):
            set_member_status(tenant, membership_id=member, status="ACTIVE")


def test_the_change_and_its_audit_are_atomic_and_hold_the_rbac_lock(
    rbac: Any, migrator: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("auditoría caída")

    before = state(migrator)
    with acting(rbac.a, rbac.ana) as tenant, monkeypatch.context() as patch:
        patch.setattr(organizations, "record", broken)
        with pytest.raises(RuntimeError):
            set_member_status(tenant, membership_id=rbac.m_eva, status="SUSPENDED")
    assert state(migrator) == before and status_of(migrator, rbac.m_eva) == "ACTIVE"
    link = {"membership_id": rbac.m_eva, "role_id": rbac.target.pk}
    changes = {
        "ana": (
            rbac.ana,
            lambda t: set_member_status(t, membership_id=rbac.m_eva, status="SUSPENDED"),
        ),
        "luis": (rbac.luis, lambda t: assign_role(t, **link)),
    }  # mientras ana no confirma, otro cambio de RBAC de la organización espera
    assert race(rbac.a, changes, hold="ana") == {"ana": "ok", "luis": "ok"}


def test_the_services_need_the_active_scope_and_a_known_status(rbac: Any) -> None:
    for service in (set_member_status, set_membership_status):
        tenant = ctx(rbac.a, rbac.ana)
        with pytest.raises(TenantContextError):
            service(tenant, membership_id=rbac.m_eva, status="SUSPENDED")  # sin tenant_scope
        with tenant_scope(ctx(rbac.b, rbac.ana)), pytest.raises(TenantContextError):
            service(tenant, membership_id=rbac.m_eva, status="SUSPENDED")  # scope de otra
        for unknown in ("DEACTIVATED", "INVITED", "suspended", ""):
            with acting(rbac.a, rbac.ana) as scope, pytest.raises(ValueError, match="estado"):
                service(scope, membership_id=rbac.m_eva, status=unknown)
    other, eva = join(rbac.b, make_user()).pk, {"membership_id": rbac.m_eva, "status": "SUSPENDED"}
    with acting(rbac.a, rbac.ana) as scope:
        with pytest.raises(ObjectDoesNotExist):  # las reglas solas ya no la encuentran
            ensure_can_manage_member(scope, membership_id=other, leaving=True)
        stamp = OrganizationMembership.objects.get(pk=rbac.m_eva).updated_at
        assert set_member_status(scope, **eva) is True  # cambió
        assert set_member_status(scope, **eva) is False  # ya lo estaba
        assert OrganizationMembership.objects.get(pk=rbac.m_eva).updated_at > stamp

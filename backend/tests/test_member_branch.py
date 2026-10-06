"""F2-69: `PUT /api/v1/o/{slug}/members/{id}/branch/` asigna a un miembro su sucursal (ADR-017).
Middleware, sesión, motor de autorización y PostgreSQL con el rol `crm_app`."""

from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from django.core.exceptions import ObjectDoesNotExist
from django.db import IntegrityError
from django.test import Client

from apps.access.catalog import BY_CODE
from apps.access.selectors import AccessDenied, Denied
from apps.members.api import views
from apps.members.services import set_member_branch
from apps.organizations import services as organizations
from apps.organizations.services import UnknownBranch, set_membership_branch
from core.tenancy.context import TenantContextError
from core.tenancy.scope import tenant_scope
from tests import test_anti_escalation, test_authorization, test_self_context
from tests.factories import make_user
from tests.test_anti_escalation import EDIT, acting, audit, race, state
from tests.test_anti_escalation import denied as refused
from tests.test_authorization import VIEW, give
from tests.test_branches import branch
from tests.test_membership_branch import own_branches
from tests.test_memberships import ctx, join
from tests.test_self_context import NOT_FOUND, reply, signed

world, rbac = test_authorization.world, test_anti_escalation.rbac
real_stack = test_self_context.real_stack
pytestmark = pytest.mark.usefixtures("tenant_db")
DENIED = (403, b'{"code":"PERMISSION_DENIED"}')
BRANCH = "SELECT default_branch_id FROM organization_memberships WHERE id = %s"


def place(client: Client, member: Any, branch_id: Any, org: str = "org-a") -> Any:
    client.cookies["csrftoken"] = token = "t" * 32
    path = f"/api/v1/o/{org}/members/{member}/branch/"
    body = {"branch_id": str(branch_id) if isinstance(branch_id, UUID) else branch_id}
    return client.put(path, body, "application/json", headers={"X-CSRFToken": token})


def branch_of(migrator: psycopg.Connection[Any], member: UUID) -> Any:
    return migrator.execute(BRANCH, [member]).fetchone()[0]  # type: ignore[index]


def test_it_assigns_changes_and_clears_the_branch_and_audits_each_change(
    rbac: Any, migrator: psycopg.Connection[Any]
) -> None:
    lima = branch(rbac.a, "LIM", name="Centro de Lima")
    cusco = branch(rbac.a, "CUZ", name="Cusco", is_active=False)  # inactiva: también vale
    ana, before = signed(rbac.ana), state(migrator)
    assert own_branches(rbac.a, rbac.eva) == frozenset()
    done = place(ana, rbac.m_eva, lima.pk)
    assert (done.status_code, done.json()) == (
        200,
        {
            "id": str(rbac.m_eva),
            "default_branch": {"id": str(lima.pk), "code": "LIM", "name": "Centro de Lima"},
        },
    )
    assert own_branches(rbac.a, rbac.eva) == {lima.pk}  # el motor ya la cuenta como suya
    assert place(ana, rbac.m_eva, lima.pk).json() == done.json()  # repetirlo responde lo mismo
    assert state(migrator) == (before[0], before[1], before[2] + 1)  # y no escribe ni audita
    moved = place(ana, rbac.m_eva, cusco.pk).json()
    assert moved["default_branch"] == {"id": str(cusco.pk), "code": "CUZ", "name": "Cusco"}
    assert own_branches(rbac.a, rbac.eva) == {cusco.pk}  # una sola: la anterior deja de serlo
    cleared = place(ana, rbac.m_eva, None)
    assert (cleared.status_code, cleared.json()) == (
        200,
        {"id": str(rbac.m_eva), "default_branch": None},
    )
    assert place(ana, rbac.m_eva, None).json() == cleared.json()  # ya estaba sin sucursal
    assert own_branches(rbac.a, rbac.eva) == frozenset()
    assert audit(migrator)[-3:] == [
        (
            "membership.branch_changed",
            "membership",
            rbac.m_eva,
            {"default_branch": change},
            named,
            rbac.ana.pk,
        )
        for change, named in (
            ([None, str(lima.pk)], {"branch": "LIM"}),
            ([str(lima.pk), str(cusco.pk)], {"branch": "CUZ"}),
            ([str(cusco.pk), None], {}),
        )
    ]
    assert state(migrator) == (before[0], before[1], before[2] + 3)
    assert branch_of(migrator, rbac.m_luis) is None  # los demás, intactos


def test_without_session_permission_or_membership_it_changes_and_reveals_nothing(
    rbac: Any, migrator: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    mine, theirs = branch(rbac.a, "LIM"), branch(rbac.b, "LIM")
    other = join(rbac.b, make_user(email="otra@example.com")).pk
    rest = [code for code in BY_CODE if code not in ("users.manage", VIEW, EDIT)]
    give(rbac.a, rbac.m_eva, dict.fromkeys(rest))  # eva: todo el catálogo menos `users.manage`
    before, eva = state(migrator), signed(rbac.eva)
    assert place(Client(), rbac.m_luis, mine.pk).status_code == 401
    for member in (rbac.m_luis, other, uuid4()):  # sin el permiso no se aprende quién existe
        for branch_id in (mine.pk, theirs.pk, "no-es-uuid"):  # ni qué sucursales, ni se valida
            assert reply(place(eva, member, branch_id)) == DENIED
    ana = signed(rbac.ana)
    for member in (other, uuid4()):  # de otra organización: no existe, tampoco con otra sucursal
        wanted: tuple[Any, ...] = (mine.pk, theirs.pk, uuid4(), None)
        for branch_id in wanted:
            assert reply(place(ana, member, branch_id)) == NOT_FOUND
    assert reply(place(ana, other, theirs.pk, org="org-b")) == NOT_FOUND  # ana no es de B
    assert reply(place(ana, rbac.m_eva, mine.pk, org="no-existe")) == NOT_FOUND
    for unknown in (theirs.pk, uuid4()):  # la sucursal de otra organización, como una que no hay
        answer = place(ana, rbac.m_eva, unknown)
        assert (answer.status_code, answer.json()["code"]) == (400, "VALIDATION_ERROR")
        assert answer.json()["fields"] == {
            "branch_id": [
                {"code": "invalid", "message": "No es una sucursal de esta organización."}
            ]
        }
    for bad in ("no-es-uuid", "", 1, True, [str(mine.pk)], {"id": str(mine.pk)}):
        answer = place(ana, rbac.m_eva, bad)
        assert (answer.status_code, answer.json()["code"]) == (400, "VALIDATION_ERROR"), bad
        assert list(answer.json()["fields"]) == ["branch_id"]
    url = f"/api/v1/o/org-a/members/{rbac.m_eva}/branch/"
    token = {"X-CSRFToken": ana.cookies["csrftoken"].value}
    for body in ({}, {"branch": str(mine.pk)}, [str(mine.pk)]):  # sin `branch_id` no se adivina
        assert ana.put(url, body, "application/json", headers=token).status_code == 400
    no_token = Client(enforce_csrf_checks=True)
    no_token.force_login(rbac.ana)
    refused_ = no_token.put(url, {"branch_id": str(mine.pk)}, "application/json")
    assert refused_.json()["code"] == "CSRF_FAILED"
    for other_method in (ana.get, ana.post, ana.patch, ana.delete):  # solo PUT
        assert reply(other_method(url, headers=token)) == DENIED
    assert state(migrator) == before and branch_of(migrator, rbac.m_eva) is None

    def lost(reason: Denied) -> Any:  # lo que el actor perdió mientras esperaba el bloqueo
        def refuse(*args: Any, **kwargs: Any) -> None:
            raise AccessDenied(reason)

        return refuse

    monkeypatch.setattr(views, "set_member_branch", lost(Denied.MEMBERSHIP))
    assert reply(place(ana, rbac.m_eva, mine.pk)) == NOT_FOUND  # como quien no es miembro
    monkeypatch.setattr(views, "set_member_branch", lost(Denied.PERMISSION))
    assert reply(place(ana, rbac.m_eva, mine.pk)) == DENIED


def test_nobody_changes_their_own_branch_or_that_of_a_member_they_do_not_cover(
    rbac: Any, migrator: psycopg.Connection[Any]
) -> None:
    lima = branch(rbac.a, "LIM")
    members = {
        name: join(rbac.a, make_user(email=f"{name}@example.com")).pk
        for name in ("plain", "narrow", "wide", "admin")
    }
    give(rbac.a, members["narrow"], {VIEW: "OWN"})  # luis lo tiene con TEAM: lo cubre
    give(rbac.a, members["wide"], {VIEW: "ORGANIZATION"})  # más alcance que luis
    give(rbac.a, members["admin"], {"users.manage": None})  # sensible: solo un Owner
    expected = {
        rbac.m_luis: Denied.SELF,
        rbac.membership: Denied.ESCALATION,  # la Owner: tiene lo que luis no
        members["wide"]: Denied.ESCALATION,
        members["admin"]: Denied.SENSITIVE,
    }
    before, luis = state(migrator), signed(rbac.luis)
    for member, reason in expected.items():
        for branch_id in (lima.pk, None):  # quitarla exige lo mismo que ponerla
            wanted = {"membership_id": member, "branch_id": branch_id}
            assert refused(rbac.a, rbac.luis, set_member_branch, **wanted) is reason
            assert reply(place(luis, member, branch_id)) == DENIED  # por HTTP, sin el motivo
    assert reply(place(signed(rbac.ana), rbac.membership, lima.pk)) == DENIED  # ni la Owner la suya
    assert state(migrator) == before
    for name in ("plain", "narrow"):  # a quien cubre, sí: no hace falta ser Owner
        assert place(luis, members[name], lima.pk).status_code == 200
        assert branch_of(migrator, members[name]) == lima.pk
    assert own_branches(rbac.a, rbac.luis) == frozenset()  # y la suya sigue sin cambiar


def test_two_changes_at_once_queue_and_each_audits_what_it_found(
    rbac: Any, migrator: psycopg.Connection[Any]
) -> None:
    lima, cusco = branch(rbac.a, "LIM"), branch(rbac.a, "CUZ")

    def placing(branch_id: UUID) -> Any:
        return lambda t: set_member_branch(t, membership_id=rbac.m_eva, branch_id=branch_id)

    changes = {"ana": (rbac.ana, placing(lima.pk)), "luis": (rbac.luis, placing(cusco.pk))}
    assert race(rbac.a, changes, hold="ana") == {"ana": "ok", "luis": "ok"}  # luis espera
    assert branch_of(migrator, rbac.m_eva) == cusco.pk
    assert [row[3] for row in audit(migrator)[-2:]] == [
        {"default_branch": [None, str(lima.pk)]},
        {"default_branch": [str(lima.pk), str(cusco.pk)]},  # su «antes» es lo que dejó la otra
    ]


def test_the_services_check_what_they_store_without_http(
    rbac: Any, migrator: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    mine, theirs = branch(rbac.a, "LIM"), branch(rbac.b, "LIM")
    other, before = join(rbac.b, make_user()).pk, state(migrator)
    with acting(rbac.a, rbac.ana) as tenant:
        for unknown in (theirs.pk, uuid4()):
            with pytest.raises(UnknownBranch):  # antes de escribir: no llega a la FK
                set_member_branch(tenant, membership_id=rbac.m_eva, branch_id=unknown)
            with pytest.raises(UnknownBranch):
                set_membership_branch(tenant, membership_id=rbac.m_eva, branch_id=unknown)
        for service in (set_member_branch, set_membership_branch):
            with pytest.raises(ObjectDoesNotExist):  # la membresía de otra organización
                service(tenant, membership_id=other, branch_id=mine.pk)
    for service in (set_member_branch, set_membership_branch):
        with pytest.raises(TenantContextError):  # fuera de un `tenant_scope` no escriben
            service(ctx(rbac.a, rbac.ana), membership_id=rbac.m_eva, branch_id=mine.pk)

    def unaudited(*args: Any, **kwargs: Any) -> None:
        raise IntegrityError("sin auditoría")

    with monkeypatch.context() as patch, acting(rbac.a, rbac.ana) as tenant:
        patch.setattr(organizations, "record", unaudited)
        with pytest.raises(IntegrityError, match="sin auditoría"):
            set_member_branch(tenant, membership_id=rbac.m_eva, branch_id=mine.pk)
    assert state(migrator) == before and branch_of(migrator, rbac.m_eva) is None
    with tenant_scope(ctx(rbac.a)) as system:  # sin actor (el sistema): el servicio del módulo
        left = set_membership_branch(system, membership_id=rbac.m_eva, branch_id=mine.pk)
    assert left == (mine.pk, "LIM", "LIM") and branch_of(migrator, rbac.m_eva) == mine.pk

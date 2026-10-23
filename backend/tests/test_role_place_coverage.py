"""D-F2-16 (F2-124): asignar o quitar un rol, y conceder, cambiar o retirar un permiso de un rol,
cubren por lugar las concesiones `TEAM` o `BRANCH` que ganan o pierden sus miembros, como D-F2-13
al poner a alguien en un equipo. Ningún permiso del catálogo admite aún alcance: se prueba con los
de prueba de `test_authorization`, contra PostgreSQL real."""

from itertools import count
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import psycopg
import pytest

from apps.access.models import MembershipRole, Role, RolePermission
from apps.access.selectors import AccessDenied, Denied
from apps.access.services import (
    assign_role,
    grant_permission,
    remove_role,
    revoke_permission,
)
from apps.members.services import (
    put_team_member,
    remove_team_member,
    set_member_branch,
    set_member_status,
)
from core.tenancy.scope import tenant_scope
from tests import test_anti_escalation, test_authorization, test_scoped_coverage
from tests.factories import make_user
from tests.test_anti_escalation import EDIT, acting, race
from tests.test_authorization import VIEW, give
from tests.test_branches import branch
from tests.test_memberships import ctx, join
from tests.test_scoped_coverage import SET_BRANCH
from tests.test_team_members import add

world, rbac = test_authorization.world, test_anti_escalation.rbac
scoped = test_scoped_coverage.scoped
pytestmark = pytest.mark.usefixtures("tenant_db")
_codes = count()
WRITES = (
    "SELECT (SELECT count(*) FROM membership_roles), (SELECT count(*) FROM role_permissions), "
    "(SELECT count(*) FROM audit_logs)"
)


def role(org: UUID, grants: dict[str, str | None]) -> Role:
    """Rol propio con esas concesiones, sin asignar."""
    with tenant_scope(ctx(org)):
        made: Role = Role.objects.create(code=f"lugar{next(_codes)}", name="Lugar")
        RolePermission.objects.bulk_create(
            RolePermission(
                organization_id=org,
                role=made,
                permission_id=code,
                supports_scope=scope is not None,
                scope=scope,
            )
            for code, scope in grants.items()
        )
    return made


def attempt(s: Any, actor: Any, service: Any, **kwargs: Any) -> Denied | None:
    try:
        with acting(s.a, actor) as tenant:
            service(tenant, **kwargs)
    except AccessDenied as error:
        return error.reason
    return None


def writes(migrator: psycopg.Connection[Any]) -> Any:
    return migrator.execute(WRITES).fetchone()


@pytest.fixture
def placed(scoped: Any) -> Any:
    """eva es del equipo soporte; luis (`VIEW` `TEAM`, `users.manage`, sin ser Owner) de
    ninguno; un rol sin asignar con `VIEW` `TEAM`."""
    add(scoped.a, scoped.support, scoped.m_eva)
    return SimpleNamespace(**vars(scoped), team_role=role(scoped.a, {VIEW: "TEAM"}).pk)


def test_assigning_a_team_grant_needs_it_in_each_team_of_the_member(
    placed: Any, migrator: psycopg.Connection[Any]
) -> None:
    wanted = {"membership_id": placed.m_eva, "role_id": placed.team_role}
    before = writes(migrator)
    assert attempt(placed, placed.luis, assign_role, **wanted) is Denied.ESCALATION
    assert writes(migrator) == before  # nada escrito, ni auditado
    add(placed.a, placed.sales, placed.m_eva)  # en dos equipos: falta cubrir los dos
    add(placed.a, placed.support, placed.m_luis)
    assert attempt(placed, placed.luis, assign_role, **wanted) is Denied.ESCALATION
    add(placed.a, placed.sales, placed.m_luis)
    assert attempt(placed, placed.luis, assign_role, **wanted) is None
    assert writes(migrator) == (before[0] + 1, before[1], before[2] + 1)


def test_removing_it_needs_the_same(placed: Any) -> None:
    give(placed.a, placed.m_eva, {VIEW: "TEAM"})
    held = give(placed.a, placed.m_eva, {VIEW: "TEAM"}).pk
    wanted = {"membership_id": placed.m_eva, "role_id": held}
    assert attempt(placed, placed.luis, remove_role, **wanted) is Denied.ESCALATION
    give(placed.a, placed.m_luis, {VIEW: "ORGANIZATION"})  # con `ORGANIZATION`, cualquier lugar
    assert attempt(placed, placed.luis, remove_role, **wanted) is None


def test_suspending_a_member_covers_their_team_grants_where_they_are(placed: Any) -> None:
    """Suspender quita todo lo que el miembro alcanza, como quitarle sus roles (PO-1): sus
    concesiones `TEAM` se cubren en sus equipos."""
    give(placed.a, placed.m_eva, {VIEW: "TEAM"})
    wanted = {"membership_id": placed.m_eva, "status": "SUSPENDED"}
    assert attempt(placed, placed.luis, set_member_status, **wanted) is Denied.ESCALATION
    add(placed.a, placed.support, placed.m_luis)
    assert attempt(placed, placed.luis, set_member_status, **wanted) is None


def test_a_member_without_a_team_or_branch_has_no_place_to_cover(placed: Any) -> None:
    """Sin equipo ni sucursal, `TEAM` y `BRANCH` valen como `OWN`: basta tenerlos con ese
    alcance, como antes de D-F2-16."""
    give(placed.a, placed.m_luis, {EDIT: "BRANCH"})
    nobody = join(placed.a, make_user(email="nadie@example.com")).pk
    wanted = {"membership_id": nobody, "role_id": role(placed.a, {VIEW: "TEAM", EDIT: "BRANCH"}).pk}
    assert attempt(placed, placed.luis, assign_role, **wanted) is None


def test_a_branch_grant_is_covered_in_the_member_branch(
    placed: Any, migrator: psycopg.Connection[Any]
) -> None:
    lima, cusco = branch(placed.a, "LIM"), branch(placed.a, "CUZ")
    give(placed.a, placed.m_luis, {EDIT: "BRANCH"})
    migrator.execute(SET_BRANCH, [cusco.pk, placed.m_luis])
    migrator.execute(SET_BRANCH, [lima.pk, placed.m_eva])
    wanted = {"membership_id": placed.m_eva, "role_id": role(placed.a, {EDIT: "BRANCH"}).pk}
    assert attempt(placed, placed.luis, assign_role, **wanted) is Denied.ESCALATION
    migrator.execute(SET_BRANCH, [lima.pk, placed.m_luis])
    assert attempt(placed, placed.luis, assign_role, **wanted) is None


def test_granting_or_revoking_covers_every_holder_of_the_role(
    placed: Any, migrator: psycopg.Connection[Any]
) -> None:
    """Cambiar una concesión de un rol la cambia para cada titular: se cubre en el lugar de
    todos. luis gestiona roles; el rol lo tiene eva (soporte) y nadie más."""
    give(placed.a, placed.m_luis, {"roles.manage": None})
    shared = role(placed.a, {})
    with tenant_scope(ctx(placed.a)):
        MembershipRole.objects.create(membership_id=placed.m_eva, role=shared)
    grant = {"role_id": shared.pk, "code": VIEW, "scope": "TEAM"}
    before = writes(migrator)
    assert attempt(placed, placed.luis, grant_permission, **grant) is Denied.ESCALATION
    assert writes(migrator) == before
    add(placed.a, placed.support, placed.m_luis)
    assert attempt(placed, placed.luis, grant_permission, **grant) is None
    add(placed.a, placed.sales, placed.m_eva)  # ahora eva está también donde luis no está
    revoke = {"role_id": shared.pk, "code": VIEW}
    assert attempt(placed, placed.luis, revoke_permission, **revoke) is Denied.ESCALATION
    assert attempt(placed, placed.luis, grant_permission, **{**grant, "scope": "OWN"}) is (
        Denied.ESCALATION  # reducir el alcance también retira lo que tenía en ventas
    )
    add(placed.a, placed.sales, placed.m_luis)
    assert attempt(placed, placed.luis, revoke_permission, **revoke) is None


def test_today_nothing_changes(placed: Any) -> None:
    """Sin alcance, `OWN` u `ORGANIZATION`: el lugar no cuenta. Con el catálogo de hoy, ninguna
    concesión admite alcance, así que ninguna respuesta cambia."""
    give(placed.a, placed.m_luis, {VIEW: "ORGANIZATION"})
    plain = role(placed.a, {"users.view": None, VIEW: "OWN"}).pk
    assert (
        attempt(placed, placed.luis, assign_role, membership_id=placed.m_eva, role_id=plain) is None
    )
    wide = role(placed.a, {VIEW: "ORGANIZATION"}).pk
    assert (
        attempt(placed, placed.luis, assign_role, membership_id=placed.m_eva, role_id=wide) is None
    )


def test_leaving_the_team_while_waiting_stops_the_assignment(
    placed: Any, migrator: psycopg.Connection[Any]
) -> None:
    """Los equipos se leen bajo el bloqueo de RBAC: si ana saca a luis del equipo de eva
    mientras luis espera, luis ya no cubre ese equipo."""
    add(placed.a, placed.support, placed.m_luis)
    give(placed.a, placed.membership, {VIEW: "ORGANIZATION"})
    changes = {
        "ana": (
            placed.ana,
            lambda t: remove_team_member(t, team_id=placed.support, membership_id=placed.m_luis),
        ),
        "luis": (
            placed.luis,
            lambda t: assign_role(t, membership_id=placed.m_eva, role_id=placed.team_role),
        ),
    }
    before = writes(migrator)
    assert race(placed.a, changes, hold="ana") == {"ana": "ok", "luis": Denied.ESCALATION}
    assert writes(migrator) == (before[0], before[1], before[2] + 1)  # solo lo de ana


def holders(s: Any, grants: dict[str, str | None]) -> UUID:
    """Rol que tienen eva (soporte) y, después, zoe (ventas); luis gestiona roles y es de
    soporte, pero no de ventas."""
    give(s.a, s.m_luis, {"roles.manage": None})
    add(s.a, s.support, s.m_luis)
    zoe = join(s.a, make_user(email="zoe@example.com")).pk
    add(s.a, s.sales, zoe)
    shared = role(s.a, grants)
    with tenant_scope(ctx(s.a)):
        for member in (s.m_eva, zoe):
            MembershipRole.objects.create(membership_id=member, role=shared)
    return shared.pk


def test_every_holder_counts_not_just_the_first(placed: Any) -> None:
    grant = {"role_id": holders(placed, {}), "code": VIEW, "scope": "TEAM"}
    assert attempt(placed, placed.luis, grant_permission, **grant) is Denied.ESCALATION
    revoke = {"role_id": holders_again(placed), "code": VIEW}
    assert attempt(placed, placed.luis, revoke_permission, **revoke) is Denied.ESCALATION


def holders_again(s: Any) -> UUID:
    """Otro rol, ya con `VIEW` `TEAM`, con los mismos dos titulares."""
    shared = role(s.a, {VIEW: "TEAM"})
    zoe = join(s.a, make_user(email="zoe2@example.com")).pk
    add(s.a, s.sales, zoe)
    with tenant_scope(ctx(s.a)):
        for member in (s.m_eva, zoe):
            MembershipRole.objects.create(membership_id=member, role=shared)
    return shared.pk


def test_an_inactive_team_membership_still_counts(placed: Any) -> None:
    """Como en `execution_context`: una pertenencia inactiva también cuenta."""
    add(placed.a, placed.support, placed.m_luis)
    add(placed.a, placed.sales, placed.m_eva, is_active=False)
    wanted = {"membership_id": placed.m_eva, "role_id": placed.team_role}
    assert attempt(placed, placed.luis, assign_role, **wanted) is Denied.ESCALATION


def test_the_member_places_are_read_under_the_lock(
    placed: Any, migrator: psycopg.Connection[Any]
) -> None:
    """Lado del miembro de la carrera: ana pone a eva en ventas (o la cambia de sucursal) y
    retiene su transacción; luis, que espera, ya ve el lugar nuevo."""
    add(placed.a, placed.support, placed.m_luis)
    joining = {
        "ana": (
            placed.ana,
            lambda t: put_team_member(t, team_id=placed.sales, membership_id=placed.m_eva),
        ),
        "luis": (
            placed.luis,
            lambda t: assign_role(t, membership_id=placed.m_eva, role_id=placed.team_role),
        ),
    }
    assert race(placed.a, joining, hold="ana") == {"ana": "ok", "luis": Denied.ESCALATION}
    lima, cusco = branch(placed.a, "LIM"), branch(placed.a, "CUZ")
    give(placed.a, placed.m_luis, {EDIT: "BRANCH"})
    migrator.execute(SET_BRANCH, [lima.pk, placed.m_luis])
    migrator.execute(SET_BRANCH, [lima.pk, placed.m_eva])
    edit_role = role(placed.a, {EDIT: "BRANCH"}).pk
    moving = {
        "ana": (
            placed.ana,
            lambda t: set_member_branch(t, membership_id=placed.m_eva, branch_id=cusco.pk),
        ),
        "luis": (
            placed.luis,
            lambda t: assign_role(t, membership_id=placed.m_eva, role_id=edit_role),
        ),
    }
    assert race(placed.a, moving, hold="ana") == {"ana": "ok", "luis": Denied.ESCALATION}


def test_suspending_or_moving_covers_every_place_of_the_member(
    placed: Any, migrator: psycopg.Connection[Any]
) -> None:
    """Suspender o mover exige cubrir todo lo que tiene el miembro, como para quitarle sus
    roles: sus concesiones `BRANCH` en su sucursal y también las `TEAM` en sus equipos, aunque
    la sucursal no cambie sus equipos."""
    lima = branch(placed.a, "LIM")
    give(placed.a, placed.m_eva, {EDIT: "BRANCH"})
    give(placed.a, placed.m_luis, {EDIT: "BRANCH"})
    migrator.execute(SET_BRANCH, [lima.pk, placed.m_eva])
    suspend = {"membership_id": placed.m_eva, "status": "SUSPENDED"}
    assert attempt(placed, placed.luis, set_member_status, **suspend) is Denied.ESCALATION
    migrator.execute(SET_BRANCH, [lima.pk, placed.m_luis])
    give(placed.a, placed.m_eva, {VIEW: "TEAM"})  # eva es de soporte; luis, no
    move = {"membership_id": placed.m_eva, "branch_id": None}
    assert attempt(placed, placed.luis, set_member_branch, **move) is Denied.ESCALATION
    add(placed.a, placed.support, placed.m_luis)
    assert attempt(placed, placed.luis, set_member_branch, **move) is None

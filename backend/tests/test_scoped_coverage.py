"""D-F2-13 (F2-123): quien pone a otro en un equipo, lo quita o le cambia la sucursal cubre lo
que ese miembro gana o pierde, bajo el bloqueo de RBAC. Se prueba con los permisos de prueba de
`test_authorization`, contra PostgreSQL real."""

from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import psycopg
import pytest

from apps.access.catalog import BY_CODE, PermissionDef
from apps.access.selectors import AccessDenied, Denied
from apps.access.services import remove_role
from apps.members.services import put_team_member, remove_team_member, set_member_branch
from tests import test_anti_escalation, test_authorization, test_self_context
from tests.factories import make_user
from tests.test_anti_escalation import EDIT, acting, denied, race
from tests.test_authorization import VIEW, give
from tests.test_branches import branch
from tests.test_member_branch import DENIED, place
from tests.test_memberships import join
from tests.test_self_context import reply, signed
from tests.test_team_member_delete import remove
from tests.test_team_member_put import put
from tests.test_team_members import add
from tests.test_teams import team

world, rbac = test_authorization.world, test_anti_escalation.rbac
real_stack = test_self_context.real_stack
pytestmark = pytest.mark.usefixtures("tenant_db")
SENSITIVE = "widgets.audit"  # permiso de prueba sensible y con alcance
TEAMS: dict[str, str | None] = {"teams.manage": None, "users.view": None}
WRITES = (
    "SELECT (SELECT count(*) FROM team_members), (SELECT count(*) FROM audit_logs), "
    "(SELECT count(*) FROM organization_memberships WHERE default_branch_id IS NOT NULL)"
)
SET_BRANCH = "UPDATE organization_memberships SET default_branch_id = %s WHERE id = %s"


@pytest.fixture
def scoped(rbac: Any, migrator: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch) -> Any:
    """`rbac` (ana Owner; luis con `VIEW` `TEAM` y `users.manage`, sin ser Owner; eva sin rol),
    luis además gestiona equipos, un permiso sensible con alcance y dos equipos sin nadie."""

    def drop() -> None:
        migrator.execute("DELETE FROM role_permissions WHERE permission_code = %s", [SENSITIVE])
        migrator.execute("DELETE FROM permissions WHERE code = %s", [SENSITIVE])

    drop()
    insert = "INSERT INTO permissions (code, module, is_sensitive, supports_scope) VALUES "
    migrator.execute(insert + "(%s, 'x', true, true)", [SENSITIVE])
    monkeypatch.setitem(BY_CODE, SENSITIVE, PermissionDef(SENSITIVE, "x", True, True))
    teams = give(rbac.a, rbac.m_luis, TEAMS)
    sales, support = team(rbac.a, "ventas"), team(rbac.a, "soporte")
    try:
        yield SimpleNamespace(**vars(rbac), teams=teams, sales=sales.pk, support=support.pk)
    finally:
        drop()


def writes(migrator: psycopg.Connection[Any]) -> Any:
    return migrator.execute(WRITES).fetchone()


def joins(s: Any, actor: Any, team_id: UUID, member: UUID) -> Denied | None:
    """Pone a `member` en el equipo y lo vuelve a quitar; o el motivo por el que no puede."""
    for service in (put_team_member, remove_team_member):
        with acting(s.a, actor) as tenant:
            try:
                service(tenant, team_id=team_id, membership_id=member)
            except AccessDenied as error:
                return error.reason
    return None


def test_joining_or_leaving_a_team_needs_each_team_grant_over_that_team(
    scoped: Any, migrator: psycopg.Connection[Any]
) -> None:
    give(scoped.a, scoped.m_eva, {VIEW: "TEAM"})
    before, luis = writes(migrator), signed(scoped.luis)
    for service in (put_team_member, remove_team_member):  # luis lo tiene, pero no es del equipo
        wanted = {"team_id": scoped.sales, "membership_id": scoped.m_eva}
        assert denied(scoped.a, scoped.luis, service, **wanted) is Denied.ESCALATION
    assert reply(put(luis, scoped.sales, scoped.m_eva, {})) == DENIED
    assert reply(remove(luis, scoped.sales, scoped.m_eva)) == DENIED
    assert writes(migrator) == before  # nada escrito, ni auditado
    add(scoped.a, scoped.sales, scoped.m_luis)
    assert put(luis, scoped.sales, scoped.m_eva, {}).status_code == 201  # siendo del equipo, sí
    assert remove(luis, scoped.sales, scoped.m_eva).status_code == 204
    assert joins(scoped, scoped.luis, scoped.support, scoped.m_eva) is Denied.ESCALATION  # otro
    assert joins(scoped, scoped.ana, scoped.support, scoped.m_eva) is Denied.ESCALATION  # Owner,
    give(scoped.a, scoped.membership, {VIEW: "ORGANIZATION"})  # pero sin el permiso; ya con él,
    assert joins(scoped, scoped.ana, scoped.support, scoped.m_eva) is None  # cualquier equipo
    assert writes(migrator) == (before[0] + 1, before[1] + 4, before[2])


def test_a_branch_scope_never_covers_a_team(scoped: Any, migrator: psycopg.Connection[Any]) -> None:
    """`TEAM` y `BRANCH` no se anidan (ADR-003 §5): tampoco siendo del equipo y de la sucursal."""
    lima = branch(scoped.a, "LIM")
    marta = make_user(email="marta@example.com")
    m_marta = join(scoped.a, marta).pk
    give(scoped.a, m_marta, {**TEAMS, VIEW: "BRANCH"})
    migrator.execute(SET_BRANCH, [lima.pk, m_marta])
    add(scoped.a, scoped.sales, m_marta)
    for member in (scoped.m_eva, scoped.m_luis):  # eva con `VIEW` `TEAM`; luis, también
        give(scoped.a, member, {VIEW: "TEAM"})
        assert joins(scoped, marta, scoped.sales, member) is Denied.ESCALATION
    give(scoped.a, m_marta, {VIEW: "ORGANIZATION"})
    assert joins(scoped, marta, scoped.sales, scoped.m_eva) is None


def test_only_team_grants_count_so_today_nothing_changes(
    scoped: Any, migrator: psycopg.Connection[Any]
) -> None:
    """Las concesiones sin alcance, `OWN` o `BRANCH` no cambian con el equipo: no se miran."""
    give(scoped.a, scoped.m_eva, {"audit.view": None, VIEW: "OWN", EDIT: "BRANCH"})
    assert joins(scoped, scoped.luis, scoped.sales, scoped.m_eva) is None  # sin ser Owner


def test_a_sensitive_team_grant_is_covered_only_by_an_owner(scoped: Any) -> None:
    give(scoped.a, scoped.m_eva, {SENSITIVE: "TEAM"})
    give(scoped.a, scoped.m_luis, {SENSITIVE: "ORGANIZATION"})
    assert joins(scoped, scoped.luis, scoped.sales, scoped.m_eva) is Denied.SENSITIVE
    give(scoped.a, scoped.membership, {SENSITIVE: "TEAM"})
    add(scoped.a, scoped.sales, scoped.membership)
    assert joins(scoped, scoped.ana, scoped.sales, scoped.m_eva) is None


def test_moving_a_member_covers_each_branch_grant_in_both_branches(
    scoped: Any, migrator: psycopg.Connection[Any]
) -> None:
    lima, cusco = branch(scoped.a, "LIM"), branch(scoped.a, "CUZ")
    give(scoped.a, scoped.m_eva, {VIEW: "BRANCH"})
    give(scoped.a, scoped.m_luis, {VIEW: "BRANCH"})  # en lima
    migrator.execute(SET_BRANCH, [lima.pk, scoped.m_luis])
    luis = signed(scoped.luis)

    def moves(actor: Any, origin: UUID | None, to: UUID | None) -> Denied | None:
        migrator.execute(SET_BRANCH, [origin, scoped.m_eva])
        wanted = {"membership_id": scoped.m_eva, "branch_id": to}
        try:
            with acting(scoped.a, actor) as tenant:
                set_member_branch(tenant, **wanted)
        except AccessDenied as error:
            return error.reason
        return None

    assert moves(scoped.luis, None, lima.pk) is None
    assert moves(scoped.luis, lima.pk, None) is None  # sin sucursal, `BRANCH` es `OWN`
    for origin, to in ((lima.pk, cusco.pk), (cusco.pk, lima.pk), (cusco.pk, None)):
        assert moves(scoped.luis, origin, to) is Denied.ESCALATION  # destino, origen, origen
    before = writes(migrator)
    assert reply(place(luis, scoped.m_eva, lima.pk)) == DENIED
    assert writes(migrator) == before
    migrator.execute(SET_BRANCH, [None, scoped.m_luis])  # el permiso, sin sucursal
    assert moves(scoped.luis, None, lima.pk) is Denied.ESCALATION
    give(scoped.a, scoped.membership, {VIEW: "ORGANIZATION"})
    assert moves(scoped.ana, cusco.pk, lima.pk) is None


@pytest.fixture
def widened(scoped: Any) -> Iterator[Any]:
    """luis cubre a eva por un rol con `VIEW` `ORGANIZATION`; ana puede quitárselo."""
    give(scoped.a, scoped.m_eva, {VIEW: "TEAM"})
    give(scoped.a, scoped.membership, {VIEW: "ORGANIZATION"})
    yield give(scoped.a, scoped.m_luis, {VIEW: "ORGANIZATION"})


def test_a_revocation_committed_while_waiting_stops_the_write(
    scoped: Any, widened: Any, migrator: psycopg.Connection[Any]
) -> None:
    """Los permisos del actor se releen bajo el bloqueo de RBAC: lo que otro le quitó mientras
    esperaba ya no cuenta, ni el permiso con alcance ni `teams.manage`."""

    def revoking(role: Any) -> dict[str, tuple[Any, Any]]:
        return {
            "ana": (
                scoped.ana,
                lambda t: remove_role(t, membership_id=scoped.m_luis, role_id=role),
            ),
            "luis": (
                scoped.luis,
                lambda t: put_team_member(t, team_id=scoped.sales, membership_id=scoped.m_eva),
            ),
        }

    for role, reason in ((widened, Denied.ESCALATION), (scoped.teams, Denied.PERMISSION)):
        before = writes(migrator)
        assert race(scoped.a, revoking(role.pk), hold="ana") == {"ana": "ok", "luis": reason}
        assert writes(migrator) == (before[0], before[1] + 1, before[2])  # solo lo de ana

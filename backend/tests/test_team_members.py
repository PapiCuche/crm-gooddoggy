"""F2-52: la tabla `team_members` y los equipos propios en el motor de autorización
(`ExecutionContext.team_ids`). PostgreSQL con el rol `crm_app`."""

from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from django.db import IntegrityError, ProgrammingError, connection
from django.db.models import ProtectedError
from django.test.utils import CaptureQueriesContext

from apps.access.selectors import execution_context, scoped
from apps.organizations.models import OrganizationMembership, Team, TeamMember
from core.tenancy.context import TenantContextError, TenantContextMissing
from core.tenancy.scope import tenant_scope
from tests import test_authorization
from tests.factories import make_user
from tests.tenancy_app.models import Widget
from tests.test_authorization import VIEW, give
from tests.test_memberships import ctx, join, raw
from tests.test_teams import team

world = test_authorization.world
pytestmark = pytest.mark.usefixtures("tenant_db")
INSERT = (
    "INSERT INTO team_members (organization_id, team_id, membership_id, team_role, is_active,"
    " created_at, updated_at) VALUES (%s, %s, %s, %s, true, now(), now())"
)


def add(org: UUID, team_id: UUID, membership_id: UUID, **fields: Any) -> TeamMember:
    with tenant_scope(ctx(org)):
        made: TeamMember = TeamMember.objects.create(
            team_id=team_id, membership_id=membership_id, **fields
        )
    return made


def own_teams(org: UUID, user: Any) -> frozenset[UUID]:
    tenant = ctx(org, user)
    with tenant_scope(tenant):
        return execution_context(tenant).team_ids


def test_the_context_carries_exactly_the_teams_of_the_membership(world: Any) -> None:
    sales, support, empty = (team(world.a, slug) for slug in ("ventas", "soporte", "vacio"))
    assert own_teams(world.a, world.ana) == frozenset()  # sin equipos: vacío, no un error
    add(world.a, sales.pk, world.membership)
    assert own_teams(world.a, world.ana) == {sales.pk}
    add(world.a, support.pk, world.membership, team_role="SUPERVISOR", is_active=False)
    with tenant_scope(ctx(world.a)):
        Team.objects.filter(pk=support.pk).update(is_active=False)
    # Cuentan todos: también un equipo inactivo y una pertenencia que no recibe asignaciones.
    assert own_teams(world.a, world.ana) == {sales.pk, support.pk}
    luis = make_user(email="luis@example.com")
    add(world.a, empty.pk, join(world.a, luis).pk)  # el equipo de otro miembro no es mío
    other = team(world.b, "ventas")
    add(world.b, other.pk, join(world.b, world.ana).pk)  # ni mi equipo en otra organización
    assert own_teams(world.a, world.ana) == {sales.pk, support.pk}
    assert own_teams(world.a, luis) == {empty.pk}
    assert own_teams(world.b, world.ana) == {other.pk}


def test_the_teams_come_in_the_same_two_queries(world: Any) -> None:
    for slug in ("uno", "dos", "tres"):
        add(world.a, team(world.a, slug).pk, world.membership)
    give(world.a, world.membership, {VIEW: "TEAM", "users.view": None})
    tenant = ctx(world.a, world.ana)
    with tenant_scope(tenant), CaptureQueriesContext(connection) as queries:
        ectx = execution_context(tenant)
    assert len(queries) == 2 and len(ectx.team_ids) == 3  # con tres equipos como con ninguno
    assert "team_members" in queries[0]["sql"]


def test_team_scope_now_reaches_the_resources_of_my_teams(
    world: Any, migrator: psycopg.Connection[Any]
) -> None:
    mine, theirs = team(world.a, "ventas"), team(world.a, "soporte")
    add(world.a, mine.pk, world.membership)
    insert = "INSERT INTO tenancy_app_widget (id, organization_id, name, team_id) "
    insert += "VALUES (%s, %s, %s, %s)"
    for name, team_id in (("de mi equipo", mine.pk), ("de otro equipo", theirs.pk)):
        migrator.execute(insert, [uuid4(), world.a, name, team_id])
    give(world.a, world.membership, {VIEW: "TEAM"})
    tenant = ctx(world.a, world.ana)
    with tenant_scope(tenant):
        seen = scoped(execution_context(tenant), VIEW, Widget.objects.all())
        assert set(seen.values_list("name", flat=True)) == {"de mi equipo"}
    with tenant_scope(ctx(world.a)):
        TeamMember.objects.all().delete()  # al salir del equipo, deja de verlos
    with tenant_scope(tenant):
        seen = scoped(execution_context(tenant), VIEW, Widget.objects.all())
        assert not seen.exists()


def test_rls_hides_and_refuses_the_rows_of_another_organization(world: Any) -> None:
    luis = make_user(email="luis@example.com")
    mine = add(world.a, team(world.a, "a-1").pk, world.membership)
    assert (mine.team_role, mine.is_active) == ("MEMBER", True)  # lo que pone el modelo
    theirs = add(world.b, team(world.b, "b-1").pk, join(world.b, luis).pk)
    assert raw("SELECT count(*) FROM team_members") == [(0,)]  # sin tenant, nada
    with pytest.raises(TenantContextMissing):
        TeamMember.objects.count()
    with tenant_scope(ctx(world.a)):
        assert raw("SELECT id FROM team_members") == [(mine.pk,)]  # RLS, no solo el manager
        assert not TeamMember._base_manager.filter(pk=theirs.pk).exists()
        assert raw("UPDATE team_members SET is_active = false") == [(1,)]
        assert raw("DELETE FROM team_members WHERE id = %s", [theirs.pk]) == [(0,)]
    with tenant_scope(ctx(world.b)):
        assert TeamMember.objects.get().is_active is True  # intacta
    with pytest.raises(ProgrammingError, match="row-level security"), tenant_scope(ctx(world.a)):
        raw(INSERT, [world.b, theirs.team_id, theirs.membership_id, "MEMBER"])
    with pytest.raises(TenantContextError), tenant_scope(ctx(world.a)):
        TeamMember(
            organization_id=world.b, team_id=theirs.team_id, membership_id=theirs.membership_id
        ).save()  # y el modelo, antes


def test_a_team_and_a_membership_of_different_organizations_cannot_be_linked(
    world: Any, migrator: psycopg.Connection[Any]
) -> None:
    team_a, team_b = team(world.a, "ventas").pk, team(world.b, "ventas").pk
    member_b = join(world.b, make_user()).pk
    mixed = [  # (organización de la fila, equipo, membresía)
        (world.a, team_b, world.membership),
        (world.a, team_a, member_b),
        (world.b, team_a, member_b),
        (world.b, team_b, world.membership),
    ]
    for org, team_id, membership_id in mixed:
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            migrator.execute(INSERT, [org, team_id, membership_id, "MEMBER"])  # ni el dueño
    with pytest.raises(IntegrityError), tenant_scope(ctx(world.a)):
        TeamMember.objects.create(team_id=team_b, membership_id=world.membership)
    migrator.execute(INSERT, [world.a, team_a, world.membership, "MEMBER"])  # la buena, sí


def test_the_database_rejects_a_repeated_member_an_unknown_role_and_dangling_references(
    world: Any, migrator: psycopg.Connection[Any]
) -> None:
    sales = team(world.a, "ventas").pk
    add(world.a, sales, world.membership)
    with pytest.raises(IntegrityError), tenant_scope(ctx(world.a)):
        TeamMember.objects.create(team_id=sales, membership_id=world.membership)
    other = join(world.a, make_user()).pk
    for role in ("member", "OWNER", ""):
        with pytest.raises(psycopg.errors.CheckViolation):
            migrator.execute(INSERT, [world.a, sales, other, role])
    migrator.execute(INSERT, [world.a, sales, other, "SUPERVISOR"])
    for team_id, membership_id in ((uuid4(), other), (sales, uuid4())):
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            migrator.execute(INSERT, [world.a, team_id, membership_id, "MEMBER"])
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        migrator.execute(INSERT, [uuid4(), sales, other, "MEMBER"])
    with pytest.raises(psycopg.errors.ForeignKeyViolation):  # con equipos, no se borra sin más
        migrator.execute("DELETE FROM organization_memberships WHERE id = %s", [other])
    with pytest.raises(psycopg.errors.ForeignKeyViolation):  # ni un equipo con integrantes
        migrator.execute("DELETE FROM teams WHERE id = %s", [sales])
    for model, pk in ((Team, sales), (OrganizationMembership, other)):  # ni con el ORM
        with pytest.raises(ProtectedError), tenant_scope(ctx(world.a)):
            model.objects.get(pk=pk).delete()
    found = migrator.execute(
        "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint"
        " WHERE conrelid = 'team_members'::regclass AND contype IN ('u', 'f')"
    ).fetchall()
    assert sorted(found) == [
        (
            "team_members_membership_org_fk",
            "FOREIGN KEY (organization_id, membership_id)"
            " REFERENCES organization_memberships(organization_id, id)",
        ),
        (
            "team_members_organization_fk",
            "FOREIGN KEY (organization_id) REFERENCES organizations(id)",
        ),
        ("team_members_team_membership_uq", "UNIQUE (organization_id, team_id, membership_id)"),
        (
            "team_members_team_org_fk",
            "FOREIGN KEY (organization_id, team_id) REFERENCES teams(organization_id, id)",
        ),
    ]
    index = migrator.execute(  # el que usa `execution_context` en cada petición
        "SELECT indexdef FROM pg_indexes WHERE indexname = 'team_members_org_member_idx'"
    ).fetchone()
    assert index and index[0].endswith("USING btree (organization_id, membership_id)")

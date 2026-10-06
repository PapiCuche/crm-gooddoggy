"""F2-68: la sucursal propia de una membresía (`organization_memberships.default_branch_id`) y
`ExecutionContext.branch_ids`. PostgreSQL con el rol `crm_app`."""

from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from django.db import IntegrityError, connection
from django.db.models import ProtectedError
from django.test.utils import CaptureQueriesContext

from apps.access.selectors import can, execution_context, scoped
from apps.organizations.models import Branch, OrganizationMembership
from core.tenancy.scope import tenant_scope
from tests import test_authorization
from tests.factories import make_user
from tests.tenancy_app.models import Widget
from tests.test_authorization import VIEW, give
from tests.test_branches import branch
from tests.test_memberships import ctx, join, raw
from tests.test_team_members import add, own_teams
from tests.test_teams import team

world = test_authorization.world
pytestmark = pytest.mark.usefixtures("tenant_db")
SET = "UPDATE organization_memberships SET default_branch_id = %s WHERE id = %s"


def place(org: UUID, membership_id: UUID, branch_id: UUID | None) -> None:
    with tenant_scope(ctx(org)):
        OrganizationMembership.objects.filter(pk=membership_id).update(default_branch_id=branch_id)


def own_branches(org: UUID, user: Any) -> frozenset[UUID]:
    tenant = ctx(org, user)
    with tenant_scope(tenant):
        return execution_context(tenant).branch_ids


def test_the_context_carries_the_branch_of_the_membership(world: Any) -> None:
    lima, cusco = branch(world.a, "LIM"), branch(world.a, "CUZ")
    assert own_branches(world.a, world.ana) == frozenset()  # sin sucursal: vacío, no un error
    place(world.a, world.membership, lima.pk)
    assert own_branches(world.a, world.ana) == {lima.pk}
    luis = make_user(email="luis@example.com")
    place(world.a, join(world.a, luis).pk, cusco.pk)  # la de otro miembro no es mía
    other = branch(world.b, "LIM")
    place(world.b, join(world.b, world.ana).pk, other.pk)  # ni la mía en otra organización
    assert own_branches(world.a, world.ana) == {lima.pk}
    assert own_branches(world.a, luis) == {cusco.pk}
    assert own_branches(world.b, world.ana) == {other.pk}
    with tenant_scope(ctx(world.a)):
        Branch.objects.filter(pk=lima.pk).update(is_active=False)
    assert own_branches(world.a, world.ana) == {lima.pk}  # cuenta también inactiva
    assert own_teams(world.a, world.ana) == frozenset()  # y la sucursal no cuenta como equipo
    place(world.a, world.membership, None)
    assert own_branches(world.a, world.ana) == frozenset()


def test_the_branch_comes_in_the_same_two_queries(world: Any) -> None:
    place(world.a, world.membership, branch(world.a, "LIM").pk)
    add(world.a, team(world.a, "ventas").pk, world.membership)
    give(world.a, world.membership, {VIEW: "BRANCH", "users.view": None})
    tenant = ctx(world.a, world.ana)
    with tenant_scope(tenant), CaptureQueriesContext(connection) as queries:
        ectx = execution_context(tenant)
    assert len(queries) == 2 and len(ectx.branch_ids) == 1 and len(ectx.team_ids) == 1
    assert "default_branch_id" in queries[0]["sql"]  # con la membresía, no en otra consulta
    assert "branches" not in queries[0]["sql"]  # y sin leer la tabla de sucursales


def test_branch_scope_now_reaches_the_resources_of_my_branch(
    world: Any, migrator: psycopg.Connection[Any]
) -> None:
    mine, theirs = branch(world.a, "LIM"), branch(world.a, "CUZ")
    insert = "INSERT INTO tenancy_app_widget (id, organization_id, name, branch_id, team_id) "
    insert += "VALUES (%s, %s, %s, %s, %s)"
    sales = team(world.a, "ventas")
    rows = (("de mi sucursal", mine.pk, None), ("de otra", theirs.pk, None))
    for name, branch_id, team_id in (*rows, ("de un equipo", None, sales.pk)):
        migrator.execute(insert, [uuid4(), world.a, name, branch_id, team_id])
    give(world.a, world.membership, {VIEW: "BRANCH"})
    tenant = ctx(world.a, world.ana)

    def seen() -> set[str]:
        with tenant_scope(tenant):
            found = scoped(execution_context(tenant), VIEW, Widget.objects.all())
            return set(found.values_list("name", flat=True))

    assert seen() == set()  # sin sucursal, `BRANCH` sigue sin dar nada ajeno
    place(world.a, world.membership, mine.pk)
    assert seen() == {"de mi sucursal"}
    add(world.a, sales.pk, world.membership)
    assert seen() == {"de mi sucursal"}  # estar en un equipo no amplía `BRANCH`
    place(world.a, world.membership, theirs.pk)
    assert seen() == {"de otra"}  # una sola sucursal: al cambiar, deja de ver la anterior
    place(world.a, world.membership, None)
    assert seen() == set()


def test_only_a_branch_grant_reaches_the_branch_and_only_a_team_grant_the_team(
    world: Any, migrator: psycopg.Connection[Any]
) -> None:
    """Con sucursal y equipo reales en el contexto, `OWN` sigue dando solo lo propio."""
    mine, sales = branch(world.a, "LIM"), team(world.a, "ventas")
    insert = "INSERT INTO tenancy_app_widget (id, organization_id, name, assigned_user_id, "
    insert += "branch_id, team_id) VALUES (%s, %s, %s, %s, %s, %s)"
    rows = (("propio", world.ana.pk, None, None), ("de mi sucursal", None, mine.pk, None))
    for name, user_id, branch_id, team_id in (*rows, ("de mi equipo", None, None, sales.pk)):
        migrator.execute(insert, [uuid4(), world.a, name, user_id, branch_id, team_id])
    place(world.a, world.membership, mine.pk)
    add(world.a, sales.pk, world.membership)
    tenant = ctx(world.a, world.ana)

    def seen() -> set[str]:
        with tenant_scope(tenant):
            ectx = execution_context(tenant)
            found = set(scoped(ectx, VIEW, Widget.objects.all()).values_list("name", flat=True))
            assert found == {w.name for w in Widget.objects.all() if can(ectx, VIEW, w)}
            return found

    give(world.a, world.membership, {VIEW: "OWN"})
    assert seen() == {"propio"}  # ni su sucursal ni su equipo: eso lo dan `BRANCH` y `TEAM`
    give(world.a, world.membership, {VIEW: "TEAM"})
    assert seen() == {"propio", "de mi equipo"}  # `TEAM` no da la sucursal
    give(world.a, world.membership, {VIEW: "BRANCH"})
    assert seen() == {"propio", "de mi equipo", "de mi sucursal"}  # unión de los tres roles


def test_rls_and_the_composite_key_keep_the_branch_inside_the_organization(
    world: Any, migrator: psycopg.Connection[Any]
) -> None:
    mine, theirs = branch(world.a, "LIM"), branch(world.b, "LIM")
    member_b = join(world.b, make_user()).pk
    for membership_id, branch_id in ((world.membership, theirs.pk), (member_b, mine.pk)):
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            migrator.execute(SET, [branch_id, membership_id])  # ni el dueño de las tablas
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        migrator.execute(SET, [uuid4(), world.membership])  # ni una sucursal que no existe
    with pytest.raises(IntegrityError), tenant_scope(ctx(world.a)):
        raw(SET, [theirs.pk, world.membership])  # ni la aplicación, dentro de su tenant
    with tenant_scope(ctx(world.a)):
        assert raw(SET, [mine.pk, member_b]) == [(0,)]  # RLS: la membresía de B no existe aquí
        assert raw(SET, [mine.pk, world.membership]) == [(1,)]  # la buena, sí
    assert own_branches(world.a, world.ana) == {mine.pk}
    with tenant_scope(ctx(world.b)):
        assert OrganizationMembership.objects.get(pk=member_b).default_branch_id is None


def test_a_branch_with_members_is_not_deleted_and_the_schema_says_why(
    world: Any, migrator: psycopg.Connection[Any]
) -> None:
    lima = branch(world.a, "LIM")
    place(world.a, world.membership, lima.pk)
    with pytest.raises(psycopg.errors.ForeignKeyViolation):  # con SQL directo
        migrator.execute("DELETE FROM branches WHERE id = %s", [lima.pk])
    with pytest.raises(ProtectedError), tenant_scope(ctx(world.a)):  # ni con el ORM
        Branch.objects.get(pk=lima.pk).delete()
    place(world.a, world.membership, None)
    migrator.execute("DELETE FROM branches WHERE id = %s", [lima.pk])  # sin miembros, sí
    column = migrator.execute(
        "SELECT is_nullable, column_default FROM information_schema.columns"
        " WHERE table_name = 'organization_memberships' AND column_name = 'default_branch_id'"
    ).fetchone()
    assert column == ("YES", None)  # opcional: una membresía nace sin sucursal
    found = migrator.execute(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint"
        " WHERE conname = 'organization_memberships_branch_org_fk'"
    ).fetchone()
    assert found == (
        "FOREIGN KEY (organization_id, default_branch_id) REFERENCES branches(organization_id, id)",
    )
    index = migrator.execute(
        "SELECT indexdef FROM pg_indexes WHERE indexname = 'org_memberships_branch_idx'"
    ).fetchone()
    assert index and index[0].endswith("USING btree (organization_id, default_branch_id)")
    with tenant_scope(ctx(world.a)):
        assert join(world.a, make_user()).default_branch_id is None

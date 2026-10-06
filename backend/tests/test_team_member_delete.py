"""F2-59: `DELETE /api/v1/o/{slug}/teams/{id}/members/{membership_id}/`, quitar a un miembro de
un equipo. Middleware, sesión, motor de autorización y PostgreSQL con el rol `crm_app`."""

from typing import Any
from uuid import uuid4

import psycopg
import pytest
from django.db import IntegrityError
from django.test import Client

from apps.organizations import teams
from apps.organizations.models import OrganizationMembership, Team, TeamMember
from apps.organizations.teams import OwnTeamMembership, put_team_member, remove_team_member
from core.tenancy.context import ActorType, TenantContext, TenantContextError
from core.tenancy.scope import tenant_scope
from tests import test_authorization, test_self_context, test_team_member_put
from tests.factories import make_user
from tests.test_anti_escalation import audit, race
from tests.test_authorization import give
from tests.test_memberships import ctx, join
from tests.test_self_context import NOT_FOUND, reply, signed
from tests.test_team_member_put import MANAGE, ROWS, put, stored
from tests.test_team_members import add, own_teams
from tests.test_team_writes import DENIED, send
from tests.test_teams import team

world, real_stack = test_authorization.world, test_self_context.real_stack
ana, luis = test_team_member_put.ana, test_team_member_put.luis
pytestmark = pytest.mark.usefixtures("tenant_db")


def remove(client: Client, team_id: Any, membership_id: Any, org: str = "org-a") -> Any:
    path = f"/api/v1/o/{org}/teams/{team_id}/members/{membership_id}/"
    return send(client, "delete", path, {"team_role": "MEMBER"})  # el cuerpo no cuenta


def test_it_removes_the_member_and_audits_what_they_had(
    world: Any, ana: Client, luis: OrganizationMembership, migrator: psycopg.Connection[Any]
) -> None:
    sales, support = team(world.a, "ventas"), team(world.a, "soporte")
    marta = join(world.a, make_user(email="marta@example.com"))
    add(world.a, sales.pk, luis.pk, team_role="SUPERVISOR", is_active=False)
    add(world.a, sales.pk, marta.pk)  # ni otro integrante ni su otro equipo cambian
    add(world.a, support.pk, luis.pk)
    assert own_teams(world.a, luis.user) == {sales.pk, support.pk}
    removed = remove(ana, sales.pk, luis.pk)
    assert (removed.status_code, removed.content) == (204, b"")
    left = [(sales.pk, marta.pk, "MEMBER", True), (support.pk, luis.pk, "MEMBER", True)]
    assert stored(world.a) == left
    changes = {"team_role": ["SUPERVISOR", None], "is_active": [False, None]}  # lo que tenía
    who = {"membership_id": str(luis.pk)}
    assert audit(migrator) == [
        ("team.member_removed", "team", sales.pk, changes, who, world.ana.pk)
    ]
    label = "SELECT entity_label FROM audit_logs"
    assert migrator.execute(label).fetchall() == [("ventas",)]
    assert own_teams(world.a, luis.user) == {support.pk}  # el motor deja de contarlo
    before = migrator.execute(ROWS).fetchone()
    assert reply(remove(ana, sales.pk, luis.pk)) == NOT_FOUND  # repetir: ya no está
    assert migrator.execute(ROWS).fetchone() == before and stored(world.a) == left
    with tenant_scope(ctx(world.a)):  # la membresía y el equipo siguen ahí
        assert OrganizationMembership.objects.get(pk=luis.pk).status == "ACTIVE"
        assert Team.objects.get(pk=sales.pk).is_active
    back = put(ana, sales.pk, luis.pk, {})  # y puede volver: entra como nuevo
    assert (back.status_code, back.json()["team_role"], back.json()["is_active"]) == (
        201,
        "MEMBER",
        True,
    )


@pytest.mark.parametrize("status", ["INVITED", "SUSPENDED", "DEACTIVATED"])
def test_any_membership_leaves_also_an_inactive_team(world: Any, ana: Client, status: str) -> None:
    sales = team(world.a, "ventas")
    member = join(world.a, make_user(), status)
    add(world.a, sales.pk, member.pk)
    with tenant_scope(ctx(world.a)):
        Team.objects.filter(pk=sales.pk).update(is_active=False)
    assert remove(ana, sales.pk, member.pk).status_code == 204
    assert stored(world.a) == []


def test_nobody_removes_themselves_from_a_team(
    world: Any, ana: Client, migrator: psycopg.Connection[Any]
) -> None:
    joined = team(world.a, "soporte")
    add(world.a, joined.pk, world.membership)
    for team_id in (joined.pk, uuid4()):  # antes de mirar el equipo
        assert reply(remove(ana, team_id, world.membership)) == DENIED
    as_task = TenantContext(world.a, "test", None, ActorType.AI_AGENT, world.ana.pk)
    text: Any = str(world.ana.pk).upper()  # un contexto mal construido no se salta la regla
    for same_person in (ctx(world.a, world.ana), as_task, TenantContext(world.a, "test", text)):
        with tenant_scope(same_person) as tenant, pytest.raises(OwnTeamMembership):
            remove_team_member(tenant, team_id=joined.pk, membership_id=world.membership)
    assert stored(world.a) == [(joined.pk, world.membership, "MEMBER", True)]
    assert migrator.execute(ROWS).fetchone() == (1, 0)
    with tenant_scope(ctx(world.a)) as tenant:  # sin actor (el sistema) no hay «uno mismo»
        remove_team_member(tenant, team_id=joined.pk, membership_id=world.membership)
    assert stored(world.a) == [] and own_teams(world.a, world.ana) == frozenset()


def test_only_who_manages_teams_and_sees_members_removes_and_only_in_their_organization(
    world: Any, luis: OrganizationMembership, migrator: psycopg.Connection[Any]
) -> None:
    mine, other, theirs = team(world.a, "ventas"), team(world.a, "soporte"), team(world.b, "x")
    foreign = join(world.b, make_user(email="solo-en-b@example.com"))
    also_b = join(world.b, luis.user)  # la misma persona, con otra membresía en B
    for org, team_id, member in (
        (world.a, mine.pk, luis.pk),
        (world.b, theirs.pk, foreign.pk),
        (world.b, theirs.pk, also_b.pk),
    ):
        add(org, team_id, member)
    assert remove(Client(), mine.pk, luis.pk).status_code == 401
    client = signed(world.ana)
    assert reply(remove(client, mine.pk, luis.pk)) == DENIED  # miembro sin roles
    give(world.a, world.membership, {"teams.view": None, "users.view": None})
    marta = make_user(email="marta@example.com")
    give(world.a, join(world.a, marta).pk, {"teams.manage": None, "teams.view": None})
    for blind in (client, signed(marta)):  # ni verlos ni administrarlos sin ver a las personas
        for unknown in ((mine.pk, luis.pk), (uuid4(), luis.pk), (mine.pk, uuid4())):
            assert reply(remove(blind, *unknown)) == DENIED  # sin permiso no se sabe qué existe
    give(world.a, world.membership, MANAGE)
    for missing in (
        (theirs.pk, foreign.pk),  # ni el equipo ni la membresía de otra organización existen
        (theirs.pk, luis.pk),
        (mine.pk, foreign.pk),
        (mine.pk, also_b.pk),
        (uuid4(), luis.pk),
        (mine.pk, uuid4()),
        (mine.pk, luis.user_id),  # la membresía, no el usuario
        (other.pk, luis.pk),  # es de la organización, pero no está en ese equipo
    ):
        assert reply(remove(client, *missing)) == NOT_FOUND
    for org in ("org-b", "no-existe"):  # sin membresía, la organización no existe
        assert reply(remove(client, theirs.pk, foreign.pk, org)) == NOT_FOUND
        assert reply(remove(client, mine.pk, luis.pk, org)) == NOT_FOUND
    url = f"/api/v1/o/org-a/teams/{mine.pk}/members/{luis.pk}/"
    assert client.delete(url).status_code == 403  # sin token CSRF
    assert migrator.execute(ROWS).fetchone() == (3, 0)
    assert len(stored(world.a)) == 1 and len(stored(world.b)) == 2
    assert remove(client, mine.pk, luis.pk).status_code == 204  # con todo en regla, sí
    assert stored(world.a) == [] and len(stored(world.b)) == 2


def test_removing_and_adding_at_once_queue_and_each_audits_what_it_found(
    world: Any, luis: OrganizationMembership, migrator: psycopg.Connection[Any]
) -> None:
    sales = team(world.a, "ventas")
    gone: list[str] = []

    def adding(**fields: Any) -> tuple[Any, Any]:
        def change(tenant: Any) -> Any:
            return put_team_member(tenant, team_id=sales.pk, membership_id=luis.pk, **fields)

        return world.ana, change

    def removing(name: str) -> tuple[Any, Any]:
        def change(tenant: Any) -> None:
            try:
                remove_team_member(tenant, team_id=sales.pk, membership_id=luis.pk)
            except TeamMember.DoesNotExist:
                gone.append(name)

        return world.ana, change

    # Quien quita espera el bloqueo del equipo: ve la incorporación que estaba en curso.
    race(world.a, {"first": adding(team_role="SUPERVISOR"), "second": removing("b")}, hold="first")
    assert stored(world.a) == [] and gone == []
    # Y quien incorpora espera a quien quita: lo encuentra fuera y lo incorpora de nuevo.
    add(world.a, sales.pk, luis.pk, is_active=False)
    race(world.a, {"first": removing("a"), "second": adding()}, hold="first")
    assert stored(world.a) == [(sales.pk, luis.pk, "MEMBER", True)] and gone == []
    assert [(row[0], row[3]) for row in audit(migrator)] == [
        ("team.member_added", {"team_role": [None, "SUPERVISOR"], "is_active": [None, True]}),
        ("team.member_removed", {"team_role": ["SUPERVISOR", None], "is_active": [True, None]}),
        ("team.member_removed", {"team_role": ["MEMBER", None], "is_active": [False, None]}),
        ("team.member_added", {"team_role": [None, "MEMBER"], "is_active": [None, True]}),
    ]
    race(world.a, {"a": removing("a"), "b": removing("b")}, hold=None)  # dos a la vez: una quita
    assert stored(world.a) == [] and len(gone) == 1
    assert migrator.execute(ROWS).fetchone() == (0, 5)


def test_the_removal_and_its_audit_row_go_together_or_not_at_all(
    world: Any, luis: OrganizationMembership, monkeypatch: pytest.MonkeyPatch
) -> None:
    sales = team(world.a, "ventas")
    add(world.a, sales.pk, luis.pk)

    def unaudited(*args: Any, **kwargs: Any) -> None:
        raise IntegrityError("sin auditoría")

    monkeypatch.setattr(teams, "record", unaudited)
    with tenant_scope(ctx(world.a, world.ana)) as tenant:
        with pytest.raises(IntegrityError, match="sin auditoría"):
            remove_team_member(tenant, team_id=sales.pk, membership_id=luis.pk)
        assert TeamMember.objects.count() == 1  # el savepoint deshizo el borrado


def test_the_command_finds_nothing_to_remove_outside_its_organization_or_scope(
    world: Any, luis: OrganizationMembership, migrator: psycopg.Connection[Any]
) -> None:
    sales, other = team(world.a, "ventas"), team(world.a, "soporte")
    add(world.a, sales.pk, luis.pk)
    with tenant_scope(ctx(world.a, world.ana)) as tenant:
        for missing, error in (
            ((uuid4(), luis.pk), Team.DoesNotExist),
            ((sales.pk, uuid4()), OrganizationMembership.DoesNotExist),
            ((other.pk, luis.pk), TeamMember.DoesNotExist),
        ):
            with pytest.raises(error):
                remove_team_member(tenant, team_id=missing[0], membership_id=missing[1])
    with tenant_scope(ctx(world.b, world.ana)) as tenant:  # desde otra organización no existen
        with pytest.raises(OrganizationMembership.DoesNotExist):
            remove_team_member(tenant, team_id=sales.pk, membership_id=luis.pk)
    with pytest.raises(TenantContextError):  # fuera de un `tenant_scope` no escribe
        remove_team_member(ctx(world.a), team_id=sales.pk, membership_id=luis.pk)
    assert migrator.execute(ROWS).fetchone() == (1, 0)

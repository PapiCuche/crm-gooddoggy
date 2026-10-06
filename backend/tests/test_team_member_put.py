"""F2-58: `PUT /api/v1/o/{slug}/teams/{id}/members/{membership_id}/`, poner a un miembro en un
equipo. Middleware, sesión, motor de autorización y PostgreSQL con el rol `crm_app`."""

from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from django.test import Client

from apps.organizations.models import OrganizationMembership, Team, TeamMember
from apps.organizations.teams import OwnTeamMembership, put_team_member
from core.tenancy.context import TenantContextError
from core.tenancy.scope import tenant_scope
from tests import test_authorization, test_self_context
from tests.factories import make_user
from tests.test_anti_escalation import audit, race
from tests.test_authorization import give
from tests.test_memberships import ctx, join
from tests.test_self_context import NOT_FOUND, reply, signed
from tests.test_team_members import add, own_teams
from tests.test_team_writes import DENIED, send
from tests.test_teams import team

world, real_stack = test_authorization.world, test_self_context.real_stack
pytestmark = pytest.mark.usefixtures("tenant_db")
MANAGE: dict[str, str | None] = {"teams.manage": None, "users.view": None}  # los dos (F2-56)
ROWS = "SELECT (SELECT count(*) FROM team_members), (SELECT count(*) FROM audit_logs)"
STAMPS = "SELECT id, xmin::text, updated_at FROM team_members ORDER BY id"  # cambian al escribir


def put(client: Client, team_id: Any, membership_id: Any, body: Any, org: str = "org-a") -> Any:
    return send(client, "put", f"/api/v1/o/{org}/teams/{team_id}/members/{membership_id}/", body)


@pytest.fixture
def ana(world: Any) -> Client:
    give(world.a, world.membership, MANAGE)
    return signed(world.ana)


@pytest.fixture
def luis(world: Any) -> OrganizationMembership:
    user = make_user(email="luis@example.com", first_name="Luis", last_name="Paz")
    return join(world.a, user)


def stored(org: UUID) -> list[tuple[Any, ...]]:
    with tenant_scope(ctx(org)):
        rows = TeamMember.objects.order_by("id")
        return list(rows.values_list("team_id", "membership_id", "team_role", "is_active"))


def test_it_adds_a_member_and_audits_it(
    world: Any, ana: Client, luis: OrganizationMembership, migrator: psycopg.Connection[Any]
) -> None:
    sales, support = team(world.a, "ventas"), team(world.a, "soporte")
    assert own_teams(world.a, luis.user) == frozenset()
    body = {"organization_id": str(world.b), "team_id": str(support.pk), "status": "SUSPENDED"}
    added = put(ana, sales.pk, luis.pk, body)  # nada de eso se elige aquí
    expected = {
        "id": str(luis.pk),  # el de la membresía, como en el listado
        "status": "ACTIVE",
        "team_role": "MEMBER",  # lo que no se envía al incorporar
        "is_active": True,
        "user": {
            "id": str(luis.user_id),
            "email": "luis@example.com",
            "first_name": "Luis",
            "last_name": "Paz",
        },
    }
    assert (added.status_code, added.json()) == (201, expected)
    assert stored(world.a) == [(sales.pk, luis.pk, "MEMBER", True)]
    changes = {"team_role": [None, "MEMBER"], "is_active": [None, True]}
    who = {"membership_id": str(luis.pk)}
    assert audit(migrator) == [("team.member_added", "team", sales.pk, changes, who, world.ana.pk)]
    label = "SELECT entity_label FROM audit_logs"
    assert migrator.execute(label).fetchall() == [("ventas",)]
    assert own_teams(world.a, luis.user) == {sales.pk}  # el motor ya lo cuenta como suyo
    give(world.a, world.membership, {"teams.view": None})  # leerlos pide su propio permiso
    listed = ana.get(f"/api/v1/o/org-a/teams/{sales.pk}/members/").json()
    assert listed == {"results": [expected], "next": None}  # la misma forma que el listado
    with tenant_scope(ctx(world.a)):
        assert luis.status == OrganizationMembership.objects.get(pk=luis.pk).status


def test_what_is_sent_when_adding_is_what_is_stored(
    world: Any, ana: Client, luis: OrganizationMembership, migrator: psycopg.Connection[Any]
) -> None:
    sales = team(world.a, "ventas")
    added = put(ana, sales.pk, luis.pk, {"team_role": "SUPERVISOR", "is_active": False})
    assert added.status_code == 201
    assert (added.json()["team_role"], added.json()["is_active"]) == ("SUPERVISOR", False)
    assert stored(world.a) == [(sales.pk, luis.pk, "SUPERVISOR", False)]
    assert audit(migrator)[-1][3] == {"team_role": [None, "SUPERVISOR"], "is_active": [None, False]}


def test_on_a_member_it_changes_only_what_is_sent_and_repeating_writes_nothing(
    world: Any, ana: Client, luis: OrganizationMembership, migrator: psycopg.Connection[Any]
) -> None:
    sales, support = team(world.a, "ventas"), team(world.a, "soporte")
    marta = join(world.a, make_user(email="marta@example.com"))
    # Ni otro integrante ni su otro equipo cambian.
    others = [add(world.a, sales.pk, marta.pk), add(world.a, support.pk, luis.pk)]
    first = put(ana, sales.pk, luis.pk, {})
    assert first.status_code == 201
    before, stamps = migrator.execute(ROWS).fetchone(), migrator.execute(STAMPS).fetchall()
    for same in ({}, {"team_role": "MEMBER"}, {"is_active": True, "team_role": "MEMBER"}):
        again = put(ana, sales.pk, luis.pk, same)
        assert (again.status_code, again.json()) == (200, first.json())  # ya estaba: 200
    assert migrator.execute(ROWS).fetchone() == before  # sin cambios, ni escribe ni audita
    assert migrator.execute(STAMPS).fetchall() == stamps  # ni un `UPDATE`: las mismas filas
    promoted = put(ana, sales.pk, luis.pk, {"team_role": "SUPERVISOR"})
    assert (promoted.status_code, promoted.json()["team_role"]) == (200, "SUPERVISOR")
    assert promoted.json()["is_active"] is True  # lo que no se envía se queda como está
    who = {"membership_id": str(luis.pk)}
    assert audit(migrator)[-1] == (
        "team.member_updated",
        "team",
        sales.pk,
        {"team_role": ["MEMBER", "SUPERVISOR"]},
        who,
        world.ana.pk,
    )
    paused = put(ana, sales.pk, luis.pk, {"is_active": False}).json()
    assert (paused["team_role"], paused["is_active"]) == ("SUPERVISOR", False)
    assert audit(migrator)[-1][3] == {"is_active": [True, False]}
    assert sorted(stored(world.a)) == sorted(
        [
            (sales.pk, marta.pk, "MEMBER", True),
            (support.pk, luis.pk, "MEMBER", True),
            (sales.pk, luis.pk, "SUPERVISOR", False),
        ]
    )
    with tenant_scope(ctx(world.a)):
        for other in others:
            assert TeamMember.objects.get(pk=other.pk).updated_at == other.updated_at


@pytest.mark.parametrize("status", ["INVITED", "SUSPENDED", "DEACTIVATED"])
def test_any_membership_of_the_organization_fits_also_in_an_inactive_team(
    world: Any, ana: Client, status: str
) -> None:
    sales = team(world.a, "ventas")
    with tenant_scope(ctx(world.a)):
        Team.objects.filter(pk=sales.pk).update(is_active=False)
    member = join(world.a, make_user(), status)
    added = put(ana, sales.pk, member.pk, {})
    assert (added.status_code, added.json()["status"]) == (201, status)  # el estado no cambia


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("team_role", ""),
        ("team_role", "member"),
        ("team_role", "OWNER"),
        ("team_role", None),
        ("team_role", ["MEMBER"]),
        ("is_active", "quizá"),
        ("is_active", None),
        ("is_active", "false"),  # solo los booleanos de JSON, como el comando
        ("is_active", 0),
    ],
)
def test_a_field_that_does_not_fit_is_a_400_that_names_it(
    world: Any,
    ana: Client,
    luis: OrganizationMembership,
    migrator: psycopg.Connection[Any],
    field: str,
    value: Any,
) -> None:
    sales = team(world.a, "ventas")
    for member in (luis.pk, world.membership, uuid4()):  # antes de mirar a quién se pone
        refused = put(ana, sales.pk, member, {"team_role": "SUPERVISOR", field: value})
        assert refused.status_code == 400 and refused.json()["code"] == "VALIDATION_ERROR"
        assert list(refused.json()["fields"]) == [field]
    assert put(ana, sales.pk, luis.pk, ["MEMBER"]).status_code == 400  # ni lo que no es objeto
    assert migrator.execute(ROWS).fetchone() == (0, 0)  # tampoco el campo que sí servía


def test_nobody_changes_their_own_place_in_a_team(
    world: Any, ana: Client, migrator: psycopg.Connection[Any]
) -> None:
    sales, joined = team(world.a, "ventas"), team(world.a, "soporte")
    add(world.a, joined.pk, world.membership)
    for team_id in (sales.pk, joined.pk):  # ni entrar ni cambiar su papel donde ya está
        assert reply(put(ana, team_id, world.membership, {"team_role": "SUPERVISOR"})) == DENIED
    assert stored(world.a) == [(joined.pk, world.membership, "MEMBER", True)]
    assert migrator.execute(ROWS).fetchone() == (1, 0)
    assert own_teams(world.a, world.ana) == {joined.pk}
    with tenant_scope(ctx(world.a, world.ana)) as tenant, pytest.raises(OwnTeamMembership):
        put_team_member(tenant, team_id=sales.pk, membership_id=world.membership)
    with tenant_scope(
        ctx(world.a)
    ) as tenant:  # sin actor (una tarea del sistema) no hay «uno mismo»
        _, new = put_team_member(tenant, team_id=sales.pk, membership_id=world.membership)
    assert new and own_teams(world.a, world.ana) == {sales.pk, joined.pk}


def test_only_who_manages_teams_and_sees_members_writes_and_only_in_their_organization(
    world: Any, luis: OrganizationMembership, migrator: psycopg.Connection[Any]
) -> None:
    mine, theirs = team(world.a, "ventas"), team(world.b, "soporte")
    foreign = join(world.b, make_user(email="solo-en-b@example.com"))
    also_b = join(world.b, luis.user)  # la misma persona, con otra membresía en B
    assert put(Client(), mine.pk, luis.pk, {}).status_code == 401
    client = signed(world.ana)
    assert reply(put(client, mine.pk, luis.pk, {})) == DENIED  # miembro sin roles
    give(world.a, world.membership, {"teams.view": None, "users.view": None})
    assert reply(put(client, mine.pk, luis.pk, {})) == DENIED  # verlos no es administrarlos
    marta = make_user(email="marta@example.com")
    give(world.a, join(world.a, marta).pk, {"teams.manage": None, "teams.view": None})
    for blind in (client, signed(marta)):  # ni administrarlos sin ver a las personas
        for unknown in ((mine.pk, luis.pk), (uuid4(), luis.pk), (mine.pk, uuid4())):
            assert reply(put(blind, *unknown, {})) == DENIED  # sin permiso no se sabe qué existe
    give(world.a, world.membership, MANAGE)
    for missing in (
        (theirs.pk, luis.pk),  # el equipo de otra organización no existe
        (uuid4(), luis.pk),
        (mine.pk, foreign.pk),  # ni la membresía de otra organización
        (mine.pk, also_b.pk),
        (mine.pk, uuid4()),
        (mine.pk, luis.user_id),  # la membresía, no el usuario
        (theirs.pk, foreign.pk),
    ):
        assert reply(put(client, *missing, {})) == NOT_FOUND
    for org in ("org-b", "no-existe"):  # sin membresía, la organización no existe
        assert reply(put(client, theirs.pk, foreign.pk, {}, org)) == NOT_FOUND
        assert reply(put(client, mine.pk, luis.pk, {}, org)) == NOT_FOUND
    url = f"/api/v1/o/org-a/teams/{mine.pk}/members/{luis.pk}/"
    for method in ("post", "patch", "delete", "get"):  # sobre un integrante solo hay `PUT`
        assert reply(send(client, method, url, {})) == DENIED
    assert client.put(url, {}, "application/json").status_code == 403  # sin token CSRF
    assert migrator.execute(ROWS).fetchone() == (0, 0)
    assert stored(world.a) == [] and stored(world.b) == []
    assert put(client, mine.pk, luis.pk, {}).status_code == 201  # con todo en regla, sí


def test_two_writes_at_once_leave_one_row_and_each_audits_what_it_found(
    world: Any, luis: OrganizationMembership, migrator: psycopg.Connection[Any]
) -> None:
    sales = team(world.a, "ventas")

    def write(**fields: Any) -> tuple[Any, Any]:
        def change(tenant: Any) -> Any:
            return put_team_member(tenant, team_id=sales.pk, membership_id=luis.pk, **fields)

        return world.ana, change

    changes = {"first": write(), "second": write(team_role="SUPERVISOR")}
    assert race(world.a, changes, hold="first") == {"first": "ok", "second": "ok"}
    assert stored(world.a) == [(sales.pk, luis.pk, "SUPERVISOR", True)]  # una fila, no dos
    assert [(row[0], row[3]) for row in audit(migrator)] == [
        ("team.member_added", {"team_role": [None, "MEMBER"], "is_active": [None, True]}),
        ("team.member_updated", {"team_role": ["MEMBER", "SUPERVISOR"]}),  # la encontró puesta
    ]
    race(world.a, {"first": write(), "second": write()}, hold=None)  # a la vez, sin cambios
    assert migrator.execute(ROWS).fetchone() == (1, 2)


def test_the_command_checks_what_it_stores_without_http(
    world: Any, luis: OrganizationMembership, migrator: psycopg.Connection[Any]
) -> None:
    sales = team(world.a, "ventas")
    bad: list[dict[str, Any]] = [
        {"team_role": "OWNER"},
        {"team_role": None},
        {"team_role": 1},
        {"is_active": "true"},
        {"is_active": 1},
        {"name": "Ventas"},  # un campo del equipo no es de un integrante
        {"membership": luis.pk},
        {"organization_id": world.b},
    ]
    with tenant_scope(ctx(world.a, world.ana)) as tenant:
        for fields in bad:
            with pytest.raises(ValueError):
                put_team_member(tenant, team_id=sales.pk, membership_id=luis.pk, **fields)
        for missing in ((uuid4(), luis.pk), (sales.pk, uuid4())):
            with pytest.raises((Team.DoesNotExist, OrganizationMembership.DoesNotExist)):
                put_team_member(tenant, team_id=missing[0], membership_id=missing[1])
    with pytest.raises(TenantContextError):  # fuera de un `tenant_scope` no escribe
        put_team_member(ctx(world.a), team_id=sales.pk, membership_id=luis.pk)
    assert migrator.execute(ROWS).fetchone() == (0, 0)
    with tenant_scope(ctx(world.a, world.ana)) as tenant:
        member, new = put_team_member(tenant, team_id=sales.pk, membership_id=luis.pk)
        assert new and member.membership.user.email == "luis@example.com"  # ya cargados
        assert not put_team_member(tenant, team_id=sales.pk, membership_id=luis.pk)[1]

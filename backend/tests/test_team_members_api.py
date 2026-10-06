"""F2-55: `GET /api/v1/o/{slug}/teams/{id}/members/`, quién pertenece a un equipo. Middleware,
sesión, motor de autorización y PostgreSQL con el rol `crm_app`."""

from typing import Any
from uuid import uuid4

import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

from apps.accounts.models import User
from apps.organizations.models import Team
from apps.organizations.selectors import team_members as stored
from core.tenancy.scope import tenant_scope
from tests import test_authorization, test_self_context
from tests.factories import make_user
from tests.test_authorization import give
from tests.test_memberships import ctx, join
from tests.test_self_context import NOT_FOUND, reply, signed
from tests.test_team_members import add
from tests.test_teams import team

world, real_stack = test_authorization.world, test_self_context.real_stack
pytestmark = pytest.mark.usefixtures("tenant_db")
VIEW: dict[str, str | None] = {"teams.view": None, "users.view": None}  # los dos (F2-56)
DENIED = (403, b'{"code":"PERMISSION_DENIED"}')


def members(client: Client, team_id: Any, org: str = "org-a", **query: Any) -> Any:
    return client.get(f"/api/v1/o/{org}/teams/{team_id}/members/", query)


def test_it_lists_who_belongs_to_the_team_with_their_role_in_it(world: Any) -> None:
    give(world.a, world.membership, VIEW)
    User.objects.filter(pk=world.ana.pk).update(first_name="Ana", last_name="López")
    sales, support = team(world.a, "ventas"), team(world.a, "soporte")
    luis, marta = make_user(email="luis@example.com"), make_user(email="marta@example.com")
    suspended, invited = join(world.a, luis, "SUSPENDED"), join(world.a, marta, "INVITED")
    gone = join(world.a, make_user(email="baja@example.com", is_active=False))
    add(world.a, sales.pk, world.membership, team_role="SUPERVISOR")
    add(world.a, sales.pk, suspended.pk, is_active=False)
    add(world.a, sales.pk, gone.pk)  # con la cuenta desactivada: sigue en el equipo
    add(world.a, support.pk, invited.pk)  # de otro equipo: no aparece
    other = team(world.b, "ventas")
    foreign = join(world.b, make_user(email="solo-en-b@example.com"))
    add(world.b, other.pk, foreign.pk)  # ni el equipo homónimo de otra organización
    body = members(signed(world.ana), sales.pk).json()
    assert body == {
        "results": [
            {
                "id": str(world.membership),  # el de la membresía, como en el directorio
                "status": "ACTIVE",
                "team_role": "SUPERVISOR",
                "is_active": True,
                "user": {
                    "id": str(world.ana.pk),
                    "email": "ana@example.com",
                    "first_name": "Ana",
                    "last_name": "López",
                },
            },
            {
                "id": str(suspended.pk),
                "status": "SUSPENDED",  # el estado en la organización, no en el equipo
                "team_role": "MEMBER",
                "is_active": False,
                "user": {
                    "id": str(luis.pk),
                    "email": "luis@example.com",
                    "first_name": "",
                    "last_name": "",
                },
            },
            {
                "id": str(gone.pk),
                "status": "ACTIVE",
                "team_role": "MEMBER",
                "is_active": True,
                "user": {
                    "id": str(gone.user_id),
                    "email": "baja@example.com",
                    "first_name": "",
                    "last_name": "",
                },
            },
        ],
        "next": None,
    }
    for alien in ("marta@example.com", "solo-en-b@example.com", str(foreign.pk), str(world.b)):
        assert alien not in str(body)
    for hidden in ("password", "is_staff", "is_platform_staff", "roles", "permissions"):
        assert hidden not in str(body)  # ni contraseña, ni marcas de plataforma, ni roles
    with tenant_scope(ctx(world.a)):  # un equipo inactivo conserva a sus integrantes
        Team.objects.filter(pk=support.pk).update(is_active=False)
    assert members(signed(world.ana), support.pk).json()["results"][0]["id"] == str(invited.pk)


def test_only_a_member_with_the_permission_reads_them_and_nobody_writes(world: Any) -> None:
    mine, theirs = team(world.a, "ventas"), team(world.b, "soporte")
    add(world.a, mine.pk, world.membership)
    add(world.b, theirs.pk, join(world.b, make_user()).pk)
    assert members(Client(), mine.pk).status_code == 401
    client = signed(world.ana)
    assert reply(members(client, mine.pk)) == DENIED  # miembro sin roles
    give(world.a, world.membership, {"users.view": None, "organization.view": None})
    assert reply(members(client, mine.pk)) == DENIED  # ver miembros no es ver equipos
    assert reply(members(client, uuid4())) == DENIED  # sin permiso no se sabe si existe
    luis = make_user(email="luis@example.com")
    give(world.a, join(world.a, luis).pk, {"teams.view": None, "organization.view": None})
    for team_id in (mine.pk, uuid4()):  # ni ver equipos es ver a las personas que los forman
        assert reply(members(signed(luis), team_id)) == DENIED
    give(world.a, world.membership, {"teams.view": None})  # con los dos, sí
    assert members(client, mine.pk).status_code == 200
    for missing in (theirs.pk, uuid4()):  # el equipo de otra organización no existe
        assert reply(members(client, missing)) == NOT_FOUND
    for org in ("org-b", "no-existe"):  # sin membresía, la organización no existe
        assert reply(members(client, mine.pk, org)) == NOT_FOUND
        assert reply(members(client, theirs.pk, org)) == NOT_FOUND
    client.cookies["csrftoken"] = token = "t" * 32  # con token responde la vista, no el CSRF
    for write in (client.post, client.put, client.patch, client.delete):  # solo lectura
        url = f"/api/v1/o/org-a/teams/{mine.pk}/members/"
        assert reply(write(url, headers={"X-CSRFToken": token})) == DENIED
    with tenant_scope(ctx(world.a)):
        assert stored(mine.pk).count() == 1 and stored(theirs.pk).count() == 0


def test_an_empty_team_is_an_empty_list_and_it_paginates_by_joining_order(world: Any) -> None:
    give(world.a, world.membership, VIEW)
    sales = team(world.a, "ventas")
    client = signed(world.ana)
    assert members(client, sales.pk).json() == {"results": [], "next": None}
    made = [join(world.a, make_user()).pk for _ in range(5)]
    for membership_id in reversed(made):  # el orden es el de incorporación al equipo
        add(world.a, sales.pk, membership_id)
    first = members(client, sales.pk, limit=2).json()
    rest = members(client, sales.pk, limit=200, cursor=first["next"]).json()
    listed = [row["id"] for row in first["results"] + rest["results"]]
    assert listed == [str(pk) for pk in reversed(made)]
    assert first["next"] and rest["next"] is None
    assert members(client, sales.pk, limit=201).json()["code"] == "VALIDATION_ERROR"


def test_the_queries_do_not_grow_with_the_members(world: Any) -> None:
    give(world.a, world.membership, VIEW)
    sales = team(world.a, "ventas")
    client = signed(world.ana)
    counts = []
    for size in (1, 6):
        while True:
            with tenant_scope(ctx(world.a)):
                if stored(sales.pk).count() >= size:
                    break
            add(world.a, sales.pk, join(world.a, make_user()).pk)
        with CaptureQueriesContext(connection) as queries:
            assert len(members(client, sales.pk).json()["results"]) == size
        counts.append(len(queries))
        assert sum("team_members" in query["sql"] for query in queries) == 2  # contexto y lista
    assert counts[0] == counts[1]  # con uno como con seis

"""F2-50: la tabla `teams`, el permiso `teams.view` y `GET /api/v1/o/{slug}/teams/`, los equipos
de una organización. Middleware, sesión, motor de autorización y PostgreSQL con `crm_app`."""

from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from django.db import IntegrityError, ProgrammingError
from django.test import Client

from apps.access.catalog import BY_CODE, ROLE_TEMPLATES
from apps.organizations.models import Team
from apps.organizations.selectors import teams as stored
from core.tenancy.context import TenantContextError, TenantContextMissing
from core.tenancy.scope import tenant_scope
from tests import test_authorization, test_self_context
from tests.factories import make_user
from tests.test_authorization import give
from tests.test_memberships import ctx, join, raw
from tests.test_self_context import NOT_FOUND, reply, signed

world, real_stack = test_authorization.world, test_self_context.real_stack
pytestmark = pytest.mark.usefixtures("tenant_db")
VIEW: dict[str, str | None] = {"teams.view": None}
INSERT = (
    "INSERT INTO teams (organization_id, slug, name, description, assignment_strategy, is_active,"
    " created_at, updated_at) VALUES (%s, %s, 'x', '', %s, true, now(), now())"
)


def teams(client: Client, org: str = "org-a", **query: Any) -> Any:
    return client.get(f"/api/v1/o/{org}/teams/", query)


def team(org: UUID, slug: str, **fields: Any) -> Team:
    with tenant_scope(ctx(org)):
        made: Team = Team.objects.create(slug=slug, name=fields.pop("name", slug), **fields)
    return made


def test_the_catalog_has_the_permission_and_who_gets_it() -> None:
    permission = BY_CODE["teams.view"]
    assert (permission.module, permission.is_sensitive, permission.supports_scope) == (
        "users",
        False,
        False,
    )
    holders = {template.code for template in ROLE_TEMPLATES if "teams.view" in template.grants}
    assert holders == {"owner", "admin", "supervisor"}


def test_it_lists_the_teams_of_the_organization_and_none_of_another(world: Any) -> None:
    give(world.a, world.membership, VIEW)
    sales = team(world.a, "ventas", name="Ventas", description="Atiende a clientes nuevos")
    closed = team(
        world.a, "soporte-2", name="Soporte", assignment_strategy="ROUND_ROBIN", is_active=False
    )
    foreign = team(world.b, "solo-en-b", name="Equipo de B")
    body = teams(signed(world.ana)).json()
    assert body == {
        "results": [
            {
                "id": str(sales.pk),
                "slug": "ventas",
                "name": "Ventas",
                "description": "Atiende a clientes nuevos",
                "assignment_strategy": "MANUAL",  # el valor por defecto
                "is_active": True,
            },
            {  # también el inactivo, con lo opcional vacío
                "id": str(closed.pk),
                "slug": "soporte-2",
                "name": "Soporte",
                "description": "",
                "assignment_strategy": "ROUND_ROBIN",
                "is_active": False,
            },
        ],
        "next": None,
    }
    for alien in ("solo-en-b", "Equipo de B", str(foreign.pk), str(world.b)):
        assert alien not in str(body)  # ningún identificador aleatorio puede contenerlos enteros


def test_only_a_member_with_the_permission_reads_them_and_nobody_writes(world: Any) -> None:
    team(world.a, "ventas")
    assert teams(Client()).status_code == 401
    client = signed(world.ana)
    give(world.a, world.membership, {"organization.view": None, "users.view": None})
    assert reply(teams(client)) == (403, b'{"code":"PERMISSION_DENIED"}')  # ver otra cosa no basta
    give(world.a, world.membership, VIEW)
    assert teams(client).status_code == 200
    for org in ("org-b", "no-existe"):  # sin membresía, la organización no existe
        assert reply(teams(client, org)) == NOT_FOUND
    outsider = make_user()
    join(world.b, outsider)
    assert reply(teams(signed(outsider))) == NOT_FOUND
    client.cookies["csrftoken"] = token = "t" * 32  # con token responde la vista, no el CSRF
    for write in (client.post, client.put, client.patch, client.delete):  # solo lectura
        denied = write("/api/v1/o/org-a/teams/", headers={"X-CSRFToken": token})
        assert reply(denied) == (403, b'{"code":"PERMISSION_DENIED"}')
    with tenant_scope(ctx(world.a)):
        assert stored().count() == 1


def test_it_paginates_by_creation_order(world: Any) -> None:
    give(world.a, world.membership, VIEW)
    made = [team(world.a, f"e-{n}").slug for n in range(5)]
    team(world.b, "e-9")
    client = signed(world.ana)
    first = teams(client, limit=2).json()
    rest = teams(client, limit=200, cursor=first["next"]).json()
    assert [row["slug"] for row in first["results"] + rest["results"]] == made
    assert first["next"] and rest["next"] is None
    assert teams(client, limit=201).json()["code"] == "VALIDATION_ERROR"


def test_rls_hides_and_refuses_the_teams_of_another_organization(world: Any) -> None:
    mine, theirs = team(world.a, "a-1"), team(world.b, "b-1")
    assert raw("SELECT count(*) FROM teams") == [(0,)]  # sin tenant, nada
    with pytest.raises(TenantContextMissing):
        stored().count()
    with tenant_scope(ctx(world.a)):
        assert raw("SELECT id FROM teams") == [(mine.pk,)]  # RLS, no solo el manager
        assert list(stored()) == [mine]
        assert not Team._base_manager.filter(pk=theirs.pk).exists()
        assert raw("UPDATE teams SET name = 'tocado'") == [(1,)]
        assert raw("DELETE FROM teams WHERE id = %s", [theirs.pk]) == [(0,)]
    with tenant_scope(ctx(world.b)):
        assert stored().get().name == "b-1"  # intacto
    with pytest.raises(ProgrammingError, match="row-level security"), tenant_scope(ctx(world.a)):
        raw(INSERT, [world.b, "b-2", "MANUAL"])
    with pytest.raises(TenantContextError), tenant_scope(ctx(world.a)):
        Team(organization_id=world.b, slug="b-3", name="x").save()  # y el modelo, antes


@pytest.mark.parametrize(
    ("slug", "strategy"),
    [
        ("", "MANUAL"),
        ("Ventas", "MANUAL"),
        ("ventas-", "MANUAL"),
        ("-ventas", "MANUAL"),
        ("ventas--1", "MANUAL"),
        ("ventas 1", "MANUAL"),
        ("ventas_1", "MANUAL"),
        ("vénta", "MANUAL"),
        ("ventas\n", "MANUAL"),
        ("ｖｅｎｔａｓ", "MANUAL"),
        ("ventas", "manual"),
        ("ventas", ""),
        ("ventas", "RANDOM"),
    ],
)
def test_the_database_only_takes_lowercase_ascii_slugs_and_known_strategies(
    world: Any, migrator: psycopg.Connection[Any], slug: str, strategy: str
) -> None:
    with pytest.raises(psycopg.errors.CheckViolation):
        migrator.execute(INSERT, [world.a, slug, strategy])


def test_the_database_rejects_a_repeated_slug_and_a_missing_organization(
    world: Any, migrator: psycopg.Connection[Any]
) -> None:
    team(world.a, "ventas")
    team(world.b, "ventas")  # el slug es único por organización, no global
    with pytest.raises(IntegrityError), tenant_scope(ctx(world.a)):
        Team.objects.create(slug="ventas", name="otro")
    with pytest.raises(psycopg.errors.StringDataRightTruncation):
        migrator.execute(INSERT, [world.a, "a" * 51, "MANUAL"])
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        migrator.execute(INSERT, [uuid4(), "ventas", "MANUAL"])
    for strategy in ("MANUAL", "ROUND_ROBIN", "LOAD_BALANCED", "SKILL_BASED", "AI_RULES"):
        migrator.execute(INSERT, [world.a, strategy.lower().replace("_", "-"), strategy])
    unique = migrator.execute(
        "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint"
        " WHERE conrelid = 'teams'::regclass AND contype = 'u'"
    ).fetchall()
    assert sorted(unique) == [  # la primera es el destino de las FK compuestas `team_id`
        ("teams_org_id_uq", "UNIQUE (organization_id, id)"),
        ("teams_org_slug_uq", "UNIQUE (organization_id, slug)"),
    ]

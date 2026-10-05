"""F2-53: `POST …/teams/`, crear un equipo, y el permiso `teams.manage`. Middleware, sesión,
motor de autorización y PostgreSQL con el rol `crm_app`."""

from typing import Any

import psycopg
import pytest
from django.db import IntegrityError
from django.test import Client

from apps.access.catalog import BY_CODE, ROLE_TEMPLATES
from apps.organizations import teams
from apps.organizations.models import Team
from apps.organizations.teams import TeamSlugTaken, create_team
from core.tenancy.scope import tenant_scope
from tests import test_authorization, test_self_context
from tests.test_anti_escalation import audit
from tests.test_authorization import give
from tests.test_memberships import ctx
from tests.test_self_context import NOT_FOUND, reply, signed
from tests.test_teams import team

world, real_stack = test_authorization.world, test_self_context.real_stack
pytestmark = pytest.mark.usefixtures("tenant_db")
DENIED = (403, b'{"code":"PERMISSION_DENIED"}')
URL = "/api/v1/o/{}/teams/"
MANAGE: dict[str, str | None] = {"teams.view": None, "teams.manage": None}
ROWS = "SELECT (SELECT count(*) FROM teams), (SELECT count(*) FROM audit_logs)"


def send(client: Client, method: str, path: str, body: Any) -> Any:
    client.cookies["csrftoken"] = token = "t" * 32
    return getattr(client, method)(path, body, "application/json", headers={"X-CSRFToken": token})


def create(client: Client, body: Any, org: str = "org-a") -> Any:
    return send(client, "post", URL.format(org), body)


@pytest.fixture
def ana(world: Any) -> Client:
    give(world.a, world.membership, MANAGE)
    return signed(world.ana)


def test_the_catalog_has_the_permission_and_who_gets_it() -> None:
    permission = BY_CODE["teams.manage"]
    assert (permission.module, permission.is_sensitive, permission.supports_scope) == (
        "users",
        False,  # 03 §H no lo marca sensible
        False,  # ni con alcance (D-F2-12)
    )
    holders = {template.code for template in ROLE_TEMPLATES if "teams.manage" in template.grants}
    assert holders == {"owner", "admin"}  # «Supervisor» lo ve, no lo administra


def test_it_creates_an_active_team_and_audits_it(
    world: Any, ana: Client, migrator: psycopg.Connection[Any]
) -> None:
    body = {
        "slug": " Ventas-Norte ",
        "name": "  Ventas   Norte ",
        "description": " Café y  clientes nuevos ",
        "assignment_strategy": "ROUND_ROBIN",
        "is_active": False,  # no se elige al crear: nace activo
        "organization_id": str(world.b),  # ni la organización: sale del contexto
    }
    made = create(ana, body)
    with tenant_scope(ctx(world.a)):
        stored = Team.objects.get()
    expected = {
        "id": str(stored.pk),
        "slug": "ventas-norte",
        "name": "Ventas Norte",
        "description": "Café y clientes nuevos",
        "assignment_strategy": "ROUND_ROBIN",
        "is_active": True,
    }
    assert (made.status_code, made.json()) == (201, expected)
    assert stored.organization_id == world.a
    changes = {
        field: [None, value]
        for field, value in expected.items()
        if field not in ("id", "is_active")
    }
    assert audit(migrator)[-1] == ("team.created", "team", stored.pk, changes, {}, world.ana.pk)
    label = "SELECT entity_label FROM audit_logs WHERE action = 'team.created'"
    assert migrator.execute(label).fetchall() == [("ventas-norte",)]
    assert ana.get(URL.format("org-a")).json()["results"] == [made.json()]
    short = create(ana, {"slug": "soporte", "name": "Soporte", "description": ""}).json()
    assert (short["assignment_strategy"], short["description"]) == ("MANUAL", "")
    assert set(audit(migrator)[-1][3]) == {"slug", "name", "assignment_strategy"}  # lo vacío, no


def test_a_repeated_slug_is_refused_and_writes_nothing(
    world: Any, ana: Client, migrator: psycopg.Connection[Any]
) -> None:
    team(world.a, "ventas")
    team(world.b, "soporte")  # el slug de otra organización no estorba
    before = migrator.execute(ROWS).fetchone()
    for slug in ("ventas", "VENTAS", " Ventas "):
        taken = create(ana, {"slug": slug, "name": "Otro"})
        assert (taken.status_code, taken.json()["code"]) == (409, "TEAM_SLUG_TAKEN")
    assert migrator.execute(ROWS).fetchone() == before
    assert create(ana, {"slug": "soporte", "name": "Soporte"}).status_code == 201
    with tenant_scope(ctx(world.a)) as tenant:
        with pytest.raises(TeamSlugTaken):
            create_team(tenant, slug="soporte", name="Otro")
        # el fallo no rompió la transacción de fuera: es esta misma
        assert create_team(tenant, slug="postventa", name="Postventa").slug == "postventa"


def test_without_its_audit_row_there_is_no_team_and_another_error_is_not_a_taken_slug(
    world: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unaudited(*args: Any, **kwargs: Any) -> None:
        raise IntegrityError("otra restricción")

    monkeypatch.setattr(teams, "record", unaudited)
    with tenant_scope(ctx(world.a)) as tenant:
        with pytest.raises(IntegrityError, match="otra"):
            create_team(tenant, slug="ventas", name="Ventas")
        assert Team.objects.count() == 0  # el savepoint se llevó el equipo


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("slug", ""),
        ("slug", "ventas norte"),
        ("slug", "ventas--norte"),
        ("slug", "-ventas"),
        ("slug", "ventas-"),
        ("slug", "ventas_norte"),
        ("slug", "vénta"),
        ("slug", "Kiosco"),  # el signo kelvin: `lower()` lo haría «k»
        ("slug", "a" * 51),
        ("slug", None),
        ("name", ""),
        ("name", "   "),
        ("name", "---"),
        ("name", "Dos\nlíneas"),
        ("name", "Oculto​"),
        ("name", "N" * 101),
        ("name", "שּׁ" * 40),  # 40 letras que en forma NFC son 120
        ("name", None),
        ("description", "Dos\tcolumnas"),
        ("description", "D" * 256),
        ("assignment_strategy", ""),
        ("assignment_strategy", "manual"),
        ("assignment_strategy", "RANDOM"),
        ("assignment_strategy", None),
    ],
)
def test_a_field_that_does_not_fit_is_a_400_that_names_it(
    ana: Client, migrator: psycopg.Connection[Any], field: str, value: Any
) -> None:
    refused = create(ana, {"slug": "ventas", "name": "Ventas", field: value})
    assert refused.status_code == 400 and refused.json()["code"] == "VALIDATION_ERROR"
    assert list(refused.json()["fields"]) == [field]
    assert migrator.execute(ROWS).fetchone() == (0, 0)


def test_only_who_manages_teams_creates_and_only_in_their_organization(
    world: Any, migrator: psycopg.Connection[Any]
) -> None:
    team(world.b, "soporte")
    before = migrator.execute(ROWS).fetchone()
    new = {"slug": "ventas", "name": "Ventas"}
    assert create(Client(), new).status_code == 401
    client = signed(world.ana)
    assert reply(create(client, new)) == DENIED  # miembro sin roles
    give(world.a, world.membership, {"teams.view": None, "organization.view": None})
    assert reply(create(client, new)) == DENIED  # verlos no es administrarlos
    for org in ("org-b", "no-existe"):  # sin membresía, la organización no existe
        assert reply(create(client, new, org)) == NOT_FOUND
    assert migrator.execute(ROWS).fetchone() == before
    give(world.a, world.membership, {"teams.manage": None})
    assert create(client, new).status_code == 201
    assert reply(create(client, new, "org-b")) == NOT_FOUND  # el permiso es de su organización
    for method in ("put", "patch", "delete"):  # sobre la colección, solo leer y crear
        assert reply(send(client, method, URL.format("org-a"), {})) == DENIED
    with tenant_scope(ctx(world.b)):
        assert [row.slug for row in Team.objects.all()] == ["soporte"]


def test_the_command_validates_for_callers_that_do_not_come_by_http(
    world: Any, migrator: psycopg.Connection[Any]
) -> None:
    with tenant_scope(ctx(world.a)) as tenant:
        sales = create_team(tenant, slug=" Ventas ", name=" Café  de  Lima ")
        assert (sales.slug, sales.name, sales.is_active) == ("ventas", "Café de Lima", True)
        assert sales.assignment_strategy == "MANUAL"
        # justo en cada límite, con cifras y varios guiones; en forma NFKC «ﬁ» serían dos letras
        slug, name, about = "a-1-" + "b" * 46, "9" * 100, "ﬁ" * 255
        edge = create_team(tenant, slug=slug, name=name, description=about)
        assert (edge.slug, edge.name, edge.description) == (slug, name, about)
        for bad in (
            {"slug": "soporte"},  # sin nombre
            {"name": "Soporte"},  # sin slug
            {"slug": "s" * 51, "name": "Soporte"},
            {"slug": 7, "name": "Soporte"},  # lo que no es texto tampoco es otro error
            {"slug": "soporte", "name": None},
            {"slug": "soporte", "name": "Soporte", "is_active": False},
            {"slug": "soporte", "name": "Soporte", "organization_id": world.b},
            {"slug": "soporte", "name": "Soporte", "assignment_strategy": "RANDOM"},
            {"slug": "soporte", "name": "Soporte", "assignment_strategy": "manual"},
        ):
            with pytest.raises(ValueError):
                create_team(tenant, **bad)
        assert Team.objects.count() == 2
    assert audit(migrator)[0][3]["name"] == [None, "Café de Lima"]  # lo que se guardó

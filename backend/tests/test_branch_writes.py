"""F2-44: `POST …/branches/`, crear una sucursal, y el permiso `branches.manage`. Middleware,
sesión, motor de autorización y PostgreSQL con el rol `crm_app`."""

from typing import Any

import psycopg
import pytest
from django.test import Client

from apps.access.catalog import BY_CODE, ROLE_TEMPLATES
from apps.organizations.branches import BranchCodeTaken, create_branch
from apps.organizations.models import Branch
from core.tenancy.scope import tenant_scope
from tests import test_authorization, test_self_context
from tests.test_anti_escalation import audit
from tests.test_authorization import give
from tests.test_branches import branch
from tests.test_memberships import ctx
from tests.test_self_context import NOT_FOUND, reply, signed

world, real_stack = test_authorization.world, test_self_context.real_stack
pytestmark = pytest.mark.usefixtures("tenant_db")
DENIED = (403, b'{"code":"PERMISSION_DENIED"}')
URL = "/api/v1/o/{}/branches/"
MANAGE: dict[str, str | None] = {"organization.view": None, "branches.manage": None}
ROWS = "SELECT (SELECT count(*) FROM branches), (SELECT count(*) FROM audit_logs)"


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
    permission = BY_CODE["branches.manage"]
    assert (permission.module, permission.is_sensitive, permission.supports_scope) == (
        "organization",
        False,
        False,
    )
    holders = {template.code for template in ROLE_TEMPLATES if "branches.manage" in template.grants}
    assert holders == {"owner", "admin"}


def test_it_creates_an_active_branch_and_audits_it(
    world: Any, ana: Client, migrator: psycopg.Connection[Any]
) -> None:
    body = {
        "code": " lim-01 ",
        "name": "  Centro   de Lima ",
        "address": "Av. Wilson 1234",
        "district": "Cercado",
        "city": "Lima",
        "phone": "+51 1 555 0100",
        "timezone": "America/Bogota",
        "is_active": False,  # no se elige al crear: nace activa
        "organization_id": str(world.b),  # ni la organización: sale del contexto
    }
    made = create(ana, body)
    with tenant_scope(ctx(world.a)):
        stored = Branch.objects.get()
    expected = {**body, "id": str(stored.pk), "code": "LIM-01", "name": "Centro de Lima"}
    expected.pop("organization_id")
    assert (made.status_code, made.json()) == (201, {**expected, "is_active": True})
    assert stored.organization_id == world.a
    changes = {
        field: [None, value]
        for field, value in expected.items()
        if field not in ("id", "is_active")
    }
    assert audit(migrator)[-1] == (
        "branch.created", "branch", stored.pk, changes, {}, world.ana.pk
    )  # fmt: skip
    label = "SELECT entity_label FROM audit_logs WHERE action = 'branch.created'"
    assert migrator.execute(label).fetchall() == [("LIM-01",)]
    assert ana.get(URL.format("org-a")).json()["results"] == [made.json()]
    short = create(ana, {"code": "AQP", "name": "Arequipa"}).json()
    assert (short["timezone"], short["address"], short["phone"]) == ("America/Lima", "", "")
    assert set(audit(migrator)[-1][3]) == {"code", "name", "timezone"}  # lo vacío no se anota


def test_a_repeated_code_is_refused_and_writes_nothing(
    world: Any, ana: Client, migrator: psycopg.Connection[Any]
) -> None:
    branch(world.a, "LIM")
    branch(world.b, "AQP")  # el código de otra organización no estorba
    before = migrator.execute(ROWS).fetchone()
    for code in ("LIM", "lim", " Lim "):
        taken = create(ana, {"code": code, "name": "Otra"})
        assert (taken.status_code, taken.json()["code"]) == (409, "BRANCH_CODE_TAKEN")
    assert migrator.execute(ROWS).fetchone() == before
    assert create(ana, {"code": "AQP", "name": "Arequipa"}).status_code == 201
    with pytest.raises(BranchCodeTaken), tenant_scope(ctx(world.a)) as tenant:
        create_branch(tenant, code="AQP", name="Otra")
    with tenant_scope(ctx(world.a)) as tenant:  # el fallo no rompió la transacción de fuera
        assert create_branch(tenant, code="CUZ", name="Cusco").code == "CUZ"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("code", ""),
        ("code", "LIM 1"),
        ("code", "LIM--1"),
        ("code", "-LIM"),
        ("code", "LÍM"),
        ("code", "l\u0131m"),  # una «ı» sin punto: `upper()` la haría «I»
        ("code", "stra\u00dfe"),  # «ß»: `upper()` la haría «SS»
        ("code", "A" * 21),
        ("code", None),
        ("name", ""),
        ("name", "   "),
        ("name", "---"),
        ("name", "Dos\nlíneas"),
        ("name", "Oculto\u200b"),
        ("name", "N" * 101),
        ("name", None),
        ("address", "Calle\t1"),
        ("address", "A" * 256),
        ("district", "D" * 101),
        ("city", "Lima\x00"),
        ("phone", "5" * 33),
        ("timezone", ""),
        ("timezone", "Lima"),
        ("timezone", "america/lima"),
        ("timezone", "../../etc/passwd"),
        ("timezone", "America/ Lima"),
    ],
)
def test_a_field_that_does_not_fit_is_a_400_that_names_it(
    ana: Client, migrator: psycopg.Connection[Any], field: str, value: Any
) -> None:
    refused = create(ana, {"code": "LIM", "name": "Lima", field: value})
    assert refused.status_code == 400 and refused.json()["code"] == "VALIDATION_ERROR"
    assert list(refused.json()["fields"]) == [field]
    assert migrator.execute(ROWS).fetchone() == (0, 0)


def test_only_who_manages_branches_creates_and_only_in_their_organization(
    world: Any, migrator: psycopg.Connection[Any]
) -> None:
    branch(world.b, "AQP")
    before = migrator.execute(ROWS).fetchone()
    new = {"code": "CUZ", "name": "Cusco"}
    assert create(Client(), new).status_code == 401
    client = signed(world.ana)
    assert reply(create(client, new)) == DENIED  # miembro sin roles
    give(world.a, world.membership, {"organization.view": None})  # leer no es administrar
    assert reply(create(client, new)) == DENIED
    for org in ("org-b", "no-existe"):  # sin membresía, la organización no existe
        assert reply(create(client, new, org)) == NOT_FOUND
    assert migrator.execute(ROWS).fetchone() == before
    give(world.a, world.membership, {"branches.manage": None})
    assert create(client, new).status_code == 201
    assert reply(create(client, new, "org-b")) == NOT_FOUND  # el permiso es de su organización
    for method in ("put", "patch", "delete"):  # sobre la colección, solo leer y crear
        assert reply(send(client, method, URL.format("org-a"), {})) == DENIED
    with tenant_scope(ctx(world.b)):
        assert [row.code for row in Branch.objects.all()] == ["AQP"]


def test_the_command_validates_for_callers_that_do_not_come_by_http(world: Any) -> None:
    with tenant_scope(ctx(world.a)) as tenant:
        lima = create_branch(tenant, code="lim", name=" Lima ")
        assert (lima.code, lima.name, lima.is_active) == ("LIM", "Lima", True)
        for bad in (
            {"code": "CUZ"},  # sin nombre
            {"name": "Cusco"},  # sin código
            {"code": "CUZ", "name": "Cusco", "is_active": False},
            {"code": "CUZ", "name": "Cusco", "organization_id": world.b},
            {"code": "CUZ", "name": "Cusco", "timezone": "Lima"},
        ):
            with pytest.raises(ValueError):
                create_branch(tenant, **bad)
        assert Branch.objects.count() == 1

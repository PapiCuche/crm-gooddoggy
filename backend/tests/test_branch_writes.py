"""F2-44: `POST …/branches/` y `PATCH …/branches/{id}/`, crear y editar una sucursal, y el
permiso `branches.manage`. Middleware, sesión, motor de autorización y PostgreSQL con `crm_app`."""

from typing import Any
from uuid import uuid4

import psycopg
import pytest
from django.test import Client

from apps.access.catalog import BY_CODE, ROLE_TEMPLATES
from apps.organizations.branches import BranchCodeTaken, create_branch, update_branch
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


def update(client: Client, branch_id: Any, body: Any, org: str = "org-a") -> Any:
    return send(client, "patch", f"{URL.format(org)}{branch_id}/", body)


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


def test_it_changes_only_what_is_sent_and_audits_the_difference(
    world: Any, ana: Client, migrator: psycopg.Connection[Any]
) -> None:
    lima = branch(world.a, "LIM", name="Lima", city="Lima", phone="555")
    edited = update(ana, lima.pk, {"name": " Lima  Centro ", "phone": "", "city": "Lima"})
    assert edited.status_code == 200
    assert edited.json() == {
        "id": str(lima.pk),
        "code": "LIM",
        "name": "Lima Centro",
        "address": "",
        "district": "",
        "city": "Lima",
        "phone": "",
        "timezone": "America/Lima",
        "is_active": True,
    }
    changes = {"name": ["Lima", "Lima Centro"], "phone": ["555", ""]}  # `city` no cambió
    assert audit(migrator)[-1] == ("branch.updated", "branch", lima.pk, changes, {}, world.ana.pk)
    before = migrator.execute(ROWS).fetchone()
    for same in ({}, {"name": "Lima Centro"}, {"code": "OTRO", "organization_id": str(world.b)}):
        assert update(ana, lima.pk, same).json() == edited.json()  # el código no se cambia
    assert migrator.execute(ROWS).fetchone() == before  # sin cambios, ni escribe ni audita
    off = update(ana, lima.pk, {"is_active": False, "timezone": "UTC"}).json()
    assert (off["is_active"], off["timezone"]) == (False, "UTC")
    assert audit(migrator)[-1][3] == {
        "is_active": [True, False],
        "timezone": ["America/Lima", "UTC"],
    }
    assert update(ana, lima.pk, {"is_active": True}).json()["is_active"] is True
    for bad in ({"name": ""}, {"timezone": "Marte/Olympus"}, {"is_active": "quizá"}):
        refused = update(ana, lima.pk, bad)
        assert (refused.status_code, list(refused.json()["fields"])) == (400, list(bad))


def test_only_who_manages_branches_writes_and_only_in_their_organization(
    world: Any, migrator: psycopg.Connection[Any]
) -> None:
    mine, theirs = branch(world.a, "LIM"), branch(world.b, "AQP")
    before = migrator.execute(ROWS).fetchone()
    new, change = {"code": "CUZ", "name": "Cusco"}, {"name": "Tocada"}
    assert create(Client(), new).status_code == update(Client(), mine.pk, change).status_code == 401
    client = signed(world.ana)
    give(world.a, world.membership, {"organization.view": None})  # leer no es administrar
    assert reply(create(client, new)) == reply(update(client, mine.pk, change)) == DENIED
    assert reply(update(client, uuid4(), change)) == DENIED  # sin permiso no se sabe si existe
    give(world.a, world.membership, {"branches.manage": None})
    for missing in (theirs.pk, uuid4()):  # la de otra organización no existe
        assert reply(update(client, missing, change)) == NOT_FOUND
    for org in ("org-b", "no-existe"):
        assert reply(create(client, new, org)) == NOT_FOUND
        assert reply(update(client, theirs.pk, change, org)) == NOT_FOUND
    for method in ("put", "delete", "post"):  # sobre una sucursal solo hay `PATCH`
        assert reply(send(client, method, f"{URL.format('org-a')}{mine.pk}/", {})) == DENIED
    assert migrator.execute(ROWS).fetchone() == before
    with tenant_scope(ctx(world.b)):
        assert Branch.objects.get().name == "AQP"


def test_the_commands_validate_for_callers_that_do_not_come_by_http(world: Any) -> None:
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
        for bad in ({"code": "OTRO"}, {"is_active": 1}, {"name": "\n"}, {"id": uuid4()}):
            with pytest.raises(ValueError, match=next(iter(bad))):
                update_branch(tenant, branch_id=lima.pk, **bad)
        assert Branch.objects.count() == 1
    with pytest.raises(Branch.DoesNotExist), tenant_scope(ctx(world.b)) as other:
        update_branch(other, branch_id=lima.pk, name="Ajena")

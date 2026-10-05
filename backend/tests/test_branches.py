"""F2-43: la tabla `branches` y `GET /api/v1/o/{slug}/branches/`, las sucursales de una
organización. Middleware, sesión, motor de autorización y PostgreSQL con el rol `crm_app`."""

from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from django.db import IntegrityError, ProgrammingError
from django.test import Client

from apps.organizations.models import Branch
from apps.organizations.selectors import branches as stored
from core.tenancy.context import TenantContextError, TenantContextMissing
from core.tenancy.scope import tenant_scope
from tests import test_authorization, test_self_context
from tests.factories import make_user
from tests.test_authorization import give
from tests.test_memberships import ctx, join, raw
from tests.test_self_context import NOT_FOUND, reply, signed

world, real_stack = test_authorization.world, test_self_context.real_stack
pytestmark = pytest.mark.usefixtures("tenant_db")
VIEW: dict[str, str | None] = {"organization.view": None}
INSERT = (
    "INSERT INTO branches (organization_id, code, name, address, district, city, phone, timezone,"
    " is_active, created_at, updated_at) VALUES (%s, %s, 'x', '', '', '', '', 'UTC', true, now(),"
    " now())"
)


def branches(client: Client, org: str = "org-a", **query: Any) -> Any:
    return client.get(f"/api/v1/o/{org}/branches/", query)


def branch(org: UUID, code: str, **fields: Any) -> Branch:
    with tenant_scope(ctx(org)):
        made: Branch = Branch.objects.create(code=code, name=fields.pop("name", code), **fields)
    return made


def test_it_lists_the_branches_of_the_organization_and_none_of_another(world: Any) -> None:
    give(world.a, world.membership, VIEW)
    centro = branch(
        world.a,
        "LIM-01",
        name="Centro de Lima",
        address="Av. Wilson 1234",
        district="Cercado",
        city="Lima",
        phone="+51 1 555 0100",
    )
    cerrada = branch(world.a, "AQP", name="Arequipa", timezone="UTC", is_active=False)
    foreign = branch(world.b, "SOLO-EN-B", name="Sede de B")
    body = branches(signed(world.ana)).json()
    assert body == {
        "results": [
            {
                "id": str(centro.pk),
                "code": "LIM-01",
                "name": "Centro de Lima",
                "address": "Av. Wilson 1234",
                "district": "Cercado",
                "city": "Lima",
                "phone": "+51 1 555 0100",
                "timezone": "America/Lima",  # el valor por defecto
                "is_active": True,
            },
            {  # también la inactiva, con lo opcional vacío
                "id": str(cerrada.pk),
                "code": "AQP",
                "name": "Arequipa",
                "address": "",
                "district": "",
                "city": "",
                "phone": "",
                "timezone": "UTC",
                "is_active": False,
            },
        ],
        "next": None,
    }
    for alien in ("SOLO-EN-B", "Sede de B", str(foreign.pk), str(world.b)):
        assert alien not in str(body)  # ningún identificador aleatorio puede contenerlos enteros


def test_only_a_member_with_the_permission_reads_them_and_nobody_writes(world: Any) -> None:
    branch(world.a, "LIM")
    assert branches(Client()).status_code == 401
    client = signed(world.ana)
    assert reply(branches(client)) == (403, b'{"code":"PERMISSION_DENIED"}')  # sin el permiso
    give(world.a, world.membership, VIEW)
    assert branches(client).status_code == 200
    for org in ("org-b", "no-existe"):  # sin membresía, la organización no existe
        assert reply(branches(client, org)) == NOT_FOUND
    outsider = make_user()
    join(world.b, outsider)
    assert reply(branches(signed(outsider))) == NOT_FOUND
    client.cookies["csrftoken"] = token = "t" * 32  # con token responde la vista, no el CSRF
    for write in (client.post, client.put, client.patch, client.delete):  # solo lectura
        denied = write("/api/v1/o/org-a/branches/", headers={"X-CSRFToken": token})
        assert reply(denied) == (403, b'{"code":"PERMISSION_DENIED"}')
    with tenant_scope(ctx(world.a)):
        assert stored().count() == 1


def test_it_paginates_by_creation_order(world: Any) -> None:
    give(world.a, world.membership, VIEW)
    made = [branch(world.a, f"S-{n}").code for n in range(5)]
    branch(world.b, "S-9")
    client = signed(world.ana)
    first = branches(client, limit=2).json()
    rest = branches(client, limit=200, cursor=first["next"]).json()
    assert [row["code"] for row in first["results"] + rest["results"]] == made
    assert first["next"] and rest["next"] is None
    assert branches(client, limit=201).json()["code"] == "VALIDATION_ERROR"


def test_rls_hides_and_refuses_the_branches_of_another_organization(world: Any) -> None:
    mine, theirs = branch(world.a, "A-1"), branch(world.b, "B-1")
    assert raw("SELECT count(*) FROM branches") == [(0,)]  # sin tenant, nada
    with pytest.raises(TenantContextMissing):
        stored().count()
    with tenant_scope(ctx(world.a)):
        assert raw("SELECT id FROM branches") == [(mine.pk,)]  # RLS, no solo el manager
        assert list(stored()) == [mine]
        assert not Branch._base_manager.filter(pk=theirs.pk).exists()
        assert raw("UPDATE branches SET name = 'tocada'") == [(1,)]
        assert raw("DELETE FROM branches WHERE id = %s", [theirs.pk]) == [(0,)]
    with tenant_scope(ctx(world.b)):
        assert stored().get().name == "B-1"  # intacta
    with pytest.raises(ProgrammingError, match="row-level security"), tenant_scope(ctx(world.a)):
        raw(INSERT, [world.b, "B-2"])
    with pytest.raises(TenantContextError), tenant_scope(ctx(world.a)):
        Branch(organization_id=world.b, code="B-3", name="x").save()  # y el modelo, antes


@pytest.mark.parametrize(
    "code", ["", "lim", "LIM-", "-LIM", "LIM--1", "LIM 1", "LÍM", "LIM\n", "LIM_1", "ＬＩＭ"]
)
def test_the_database_only_takes_codes_that_cannot_read_alike(
    world: Any, migrator: psycopg.Connection[Any], code: str
) -> None:
    with pytest.raises(psycopg.errors.CheckViolation):
        migrator.execute(INSERT, [world.a, code])


def test_the_database_rejects_a_repeated_code_and_a_missing_organization(
    world: Any, migrator: psycopg.Connection[Any]
) -> None:
    branch(world.a, "LIM")
    branch(world.b, "LIM")  # el código es único por organización, no global
    with pytest.raises(IntegrityError), tenant_scope(ctx(world.a)):
        Branch.objects.create(code="LIM", name="otra")
    with pytest.raises(psycopg.errors.StringDataRightTruncation):
        migrator.execute(INSERT, [world.a, "A" * 21])
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        migrator.execute(INSERT, [uuid4(), "LIM"])

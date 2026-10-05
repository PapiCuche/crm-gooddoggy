"""F2-45: `PATCH /api/v1/o/{slug}/branches/{id}/`, editar y desactivar una sucursal.
Middleware, sesión, motor de autorización y PostgreSQL con el rol `crm_app`."""

from typing import Any
from uuid import uuid4

import psycopg
import pytest
from django.db import IntegrityError
from django.test import Client

from apps.organizations import branches
from apps.organizations.branches import update_branch
from apps.organizations.models import Branch
from core.tenancy.scope import tenant_scope
from tests import test_authorization, test_branch_writes, test_self_context
from tests.test_anti_escalation import audit
from tests.test_authorization import give
from tests.test_branch_writes import DENIED, ROWS, URL, send
from tests.test_branches import branch
from tests.test_memberships import ctx
from tests.test_self_context import NOT_FOUND, reply, signed

world, real_stack = test_authorization.world, test_self_context.real_stack
ana = test_branch_writes.ana
pytestmark = pytest.mark.usefixtures("tenant_db")


def update(client: Client, branch_id: Any, body: Any, org: str = "org-a") -> Any:
    return send(client, "patch", f"{URL.format(org)}{branch_id}/", body)


def test_it_changes_only_what_is_sent_and_audits_the_difference(
    world: Any, ana: Client, migrator: psycopg.Connection[Any]
) -> None:
    lima = branch(world.a, "LIM", name="Lima", city="Lima", phone="555")
    other = branch(world.a, "AQP", name="Arequipa")
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
    label = "SELECT entity_label FROM audit_logs WHERE action = 'branch.updated'"
    assert migrator.execute(label).fetchall() == [("LIM",)]
    before = migrator.execute(ROWS).fetchone()
    for same in ({}, {"name": "Lima Centro"}, {"code": "OTRO", "organization_id": str(world.b)}):
        assert update(ana, lima.pk, same).json() == edited.json()  # el código no se cambia
    assert migrator.execute(ROWS).fetchone() == before  # sin cambios, ni escribe ni audita
    listed = {row["code"]: row for row in ana.get(URL.format("org-a")).json()["results"]}
    assert listed["LIM"] == edited.json() and listed["AQP"]["name"] == "Arequipa"
    with tenant_scope(ctx(world.a)):
        assert Branch.objects.get(pk=other.pk).updated_at == other.updated_at  # la otra, intacta
        assert Branch.objects.get(pk=lima.pk).updated_at > lima.updated_at


def test_it_deactivates_and_reactivates(
    world: Any, ana: Client, migrator: psycopg.Connection[Any]
) -> None:
    lima = branch(world.a, "LIM")
    off = update(ana, lima.pk, {"is_active": False, "timezone": "UTC"}).json()
    assert (off["is_active"], off["timezone"]) == (False, "UTC")
    assert audit(migrator)[-1][3] == {
        "is_active": [True, False],
        "timezone": ["America/Lima", "UTC"],
    }
    assert update(ana, lima.pk, {"is_active": False}).json() == off
    assert len(audit(migrator)) == 1  # ya estaba inactiva
    assert update(ana, lima.pk, {"is_active": True}).json()["is_active"] is True
    assert audit(migrator)[-1][3] == {"is_active": [False, True]}


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("name", ""),
        ("name", "---"),
        ("name", "Dos\nlíneas"),
        ("name", "N" * 101),
        ("name", None),
        ("address", "A" * 256),
        ("city", "Li\tma"),
        ("phone", "5" * 33),
        ("timezone", ""),
        ("timezone", "Marte/Olympus"),
        ("timezone", "localtime"),
        ("is_active", "quizá"),
        ("is_active", None),
    ],
)
def test_a_field_that_does_not_fit_is_a_400_that_names_it(
    world: Any, ana: Client, migrator: psycopg.Connection[Any], field: str, value: Any
) -> None:
    lima = branch(world.a, "LIM", name="Lima", city="Lima")
    refused = update(ana, lima.pk, {"district": "Cercado", field: value})
    assert refused.status_code == 400 and refused.json()["code"] == "VALIDATION_ERROR"
    assert list(refused.json()["fields"]) == [field]
    assert migrator.execute(ROWS).fetchone() == (1, 0)  # tampoco el campo que sí servía
    with tenant_scope(ctx(world.a)):
        assert Branch.objects.get().district == ""


def test_only_who_manages_branches_edits_and_only_in_their_organization(
    world: Any, migrator: psycopg.Connection[Any]
) -> None:
    mine, theirs = branch(world.a, "LIM"), branch(world.b, "AQP")
    before, change = migrator.execute(ROWS).fetchone(), {"name": "Tocada", "is_active": False}
    assert update(Client(), mine.pk, change).status_code == 401
    client = signed(world.ana)
    assert reply(update(client, mine.pk, change)) == DENIED  # miembro sin roles
    give(world.a, world.membership, {"organization.view": None})  # leer no es administrar
    assert reply(update(client, mine.pk, change)) == DENIED
    assert reply(update(client, uuid4(), change)) == DENIED  # sin permiso no se sabe si existe
    give(world.a, world.membership, {"branches.manage": None})
    for missing in (theirs.pk, uuid4()):  # la de otra organización no existe
        assert reply(update(client, missing, change)) == NOT_FOUND
    for org in ("org-b", "no-existe"):
        assert reply(update(client, theirs.pk, change, org)) == NOT_FOUND
        assert reply(update(client, mine.pk, change, org)) == NOT_FOUND
    for method in ("put", "delete", "post", "get"):  # sobre una sucursal solo hay `PATCH`
        assert reply(send(client, method, f"{URL.format('org-a')}{mine.pk}/", {})) == DENIED
    assert migrator.execute(ROWS).fetchone() == before
    with tenant_scope(ctx(world.b)):
        assert (Branch.objects.get().name, Branch.objects.get().is_active) == ("AQP", True)
    assert update(client, mine.pk, change).json()["name"] == "Tocada"


def test_the_command_validates_and_writes_all_or_nothing(
    world: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    lima = branch(world.a, "LIM", name="Lima")
    with tenant_scope(ctx(world.a)) as tenant:
        for bad in (
            {"code": "OTRO"},
            {"is_active": 1},
            {"is_active": "false"},
            {"name": "\n"},
            {"name": 7},
            {"id": uuid4()},
            {"organization_id": world.b},
        ):
            with pytest.raises(ValueError, match=next(iter(bad))):
                update_branch(tenant, branch_id=lima.pk, district="No llega", **bad)
        assert (Branch.objects.get().name, Branch.objects.get().district) == ("Lima", "")
        cleaned = update_branch(tenant, branch_id=lima.pk, name=" Cafe\u0301\u00a0 de  Lima ")
        assert cleaned.name == "Caf\u00e9 de Lima"

        def unaudited(*args: Any, **kwargs: Any) -> None:
            raise IntegrityError("sin auditoría")

        monkeypatch.setattr(branches, "record", unaudited)
        with pytest.raises(IntegrityError, match="sin auditoría"):
            update_branch(tenant, branch_id=lima.pk, name="Sin rastro")
        assert Branch.objects.get().name == "Caf\u00e9 de Lima"  # el savepoint deshizo el cambio
    with pytest.raises(Branch.DoesNotExist), tenant_scope(ctx(world.b)) as other:
        update_branch(other, branch_id=lima.pk, name="Ajena")

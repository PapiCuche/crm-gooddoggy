"""F3-24: revertir una importación de stock (`apps.inventory.imports.revert` y `…/revert/`),
contra PostgreSQL real con el rol `crm_app`, también con dos transacciones a la vez."""

from typing import Any

import psycopg
import pytest
from django.test import Client

from apps.imports.models import DataImport
from apps.inventory import imports
from apps.inventory.models import InventoryLevel
from core.tenancy.scope import tenant_scope
from tests import test_authorization, test_self_context
from tests.factories import make_user
from tests.test_authorization import give
from tests.test_inventory_stock import cell, level, move, race, wanted
from tests.test_memberships import ctx, join
from tests.test_products import send
from tests.test_self_context import signed
from tests.test_stock_import import ADJUST, IMPORTS
from tests.test_stock_import_apply import HEADER, _held, apply, ledger
from tests.test_stock_import_preview import preview, rows

world, real_stack = test_authorization.world, test_self_context.real_stack
pytestmark = pytest.mark.usefixtures("tenant_db")
AUDIT = "SELECT entity_id, metadata FROM audit_logs WHERE action = 'inventory.import_reverted'"


@pytest.fixture
def ana(world: Any) -> Client:
    give(world.a, world.membership, ADJUST)
    return signed(world.ana)


def revert(client: Client, job: DataImport, body: Any = None) -> Any:
    return send(client, "post", f"{IMPORTS}{job.pk}/revert/", body or {"confirm": True})


def undo(job: DataImport) -> Any:
    return lambda tenant: imports.revert(tenant, import_id=job.pk)


def test_revert_puts_back_the_untouched_cells_and_keeps_both_movements(
    ana: Client, world: Any, migrator: psycopg.Connection[Any]
) -> None:
    counted, fresh, moved = cell(world.a, "A1"), cell(world.a, "B2"), cell(world.a, "C3")
    same = cell(world.a, "D4")
    move(world.a, world.ana, wanted(counted, "RECEIPT", 2))
    job = preview(world.a, world.ana, HEADER + "A1,ALM,5\nB2,ALM,3\nC3,ALM,4\nD4,ALM,0\nX,A,1\n")
    assert apply(ana, job).json()["result"]["applied"] == 3
    move(world.a, world.ana, wanted(moved, "ISSUE", 1))
    response = revert(ana, job)
    assert response.status_code == 200, response.content
    body = response.json()
    assert (body["status"], body["revert"]) == (
        "REVERTED",
        {"reverted": 2, "stale": 1, "failed": 0},
    )
    assert body["result"] == {"applied": 3, "unchanged": 1, "stale": 0, "failed": 0}
    assert (level(world.a, counted), level(world.a, fresh), level(world.a, moved)) == (
        (2, 0, 2),
        (0, 0, 0),
        (3, 0, 3),
    )
    with tenant_scope(ctx(world.a)):
        assert not InventoryLevel.objects.filter(variant_id=same[0]).exists()
    key, found = f"import:{job.pk}", ledger(world.a)
    assert found[1:4] + found[5:] == [  # nada se edita: el ajuste y, después, su contrario
        (3, "IMPORT", job.pk, f"{key}:2"),
        (3, "IMPORT", job.pk, f"{key}:3"),
        (4, "IMPORT", job.pk, f"{key}:4"),
        (-3, "IMPORT", job.pk, f"{key}:2:revert"),
        (-3, "IMPORT", job.pk, f"{key}:3:revert"),
    ]
    done = rows(world.a, job)
    actions = [done[n].action for n in range(2, 7)]
    assert actions == ["UPDATE", "CREATE", "STALE", "UNCHANGED", "ERROR"]
    assert done[4].errors == ["El saldo cambió después de la importación: era 4, ahora 3."]
    assert migrator.execute(AUDIT).fetchall() == [(job.pk, body["revert"])]


def test_reverting_again_creates_no_movement(
    ana: Client, world: Any, migrator: psycopg.Connection[Any]
) -> None:
    here = cell(world.a)
    job = preview(world.a, world.ana, HEADER + "IP16,ALM,4\n")
    apply(ana, job)
    first = revert(ana, job).json()
    assert revert(ana, job).json() == first and len(ledger(world.a)) == 2
    # aunque se perdiera el estado, la clave de la fila no deja repetir el ajuste contrario
    migrator.execute("UPDATE data_imports SET status = 'APPLIED'")
    assert revert(ana, job).json()["revert"] == {"reverted": 1, "stale": 0, "failed": 0}
    assert len(ledger(world.a)) == 2 and level(world.a, here) == (0, 0, 0)
    assert len(migrator.execute(AUDIT).fetchall()) == 2


def test_only_an_applied_import_is_reverted_and_only_when_confirmed(
    ana: Client, world: Any
) -> None:
    cell(world.a)
    job = preview(world.a, world.ana, HEADER + "IP16,ALM,4\n")
    refused = revert(ana, job)
    assert (refused.status_code, refused.json()["code"]) == (409, "IMPORT_NOT_APPLIED")
    apply(ana, job)
    for body in ({"confirm": False}, {"nope": 1}):
        assert "confirm" in revert(ana, job, body).json()["fields"]
    assert len(ledger(world.a)) == 1
    with tenant_scope(ctx(world.a)):
        assert DataImport.objects.get().status == "APPLIED"


def test_what_cannot_go_back_fails_on_its_row(ana: Client, world: Any) -> None:
    held, big = cell(world.a, "A1"), cell(world.a, "B2")
    move(world.a, world.ana, wanted(held, "RECEIPT", 1))
    for _ in range(2):
        move(world.a, world.ana, wanted(big, "RECEIPT", 1_000_000))
    job = preview(world.a, world.ana, HEADER + "A1,ALM,5\nB2,ALM,7\n")
    apply(ana, job)
    with tenant_scope(ctx(world.a)):
        InventoryLevel.objects.filter(variant_id=held[0]).update(reserved=3)
    assert revert(ana, job).json()["revert"] == {"reverted": 0, "stale": 0, "failed": 2}
    assert [r.errors for r in rows(world.a, job).values()] == [
        ["Hay más reservadas que el saldo de antes."],
        ["El saldo de antes pasa de 1000000, el tope de un ajuste: corrígelo a mano."],
    ]
    assert (level(world.a, held), level(world.a, big)) == ((5, 3, 2), (7, 0, 7))


def test_it_needs_inventory_adjust_and_stays_in_its_organization(world: Any) -> None:
    bo = make_user(email="bo@example.com")
    join(world.b, bo)
    cell(world.b, "B1")
    theirs = preview(world.b, bo, HEADER + "B1,ALM,1\n")
    with tenant_scope(ctx(world.b, bo)) as tenant:
        imports.apply(tenant, import_id=theirs.pk)
    give(world.a, world.membership, {"inventory.view": None})
    viewer = signed(world.ana)
    assert revert(viewer, theirs).status_code == 403
    give(world.a, world.membership, ADJUST)
    assert revert(viewer, theirs).status_code == 404
    with tenant_scope(ctx(world.b)):
        assert DataImport.objects.get().status == "APPLIED"


def test_a_movement_during_the_revert_leaves_the_row_stale(world: Any) -> None:
    here = cell(world.a)
    job = preview(world.a, world.ana, HEADER + "IP16,ALM,7\n")
    with tenant_scope(ctx(world.a, world.ana)) as tenant:
        imports.apply(tenant, import_id=job.pk)
    first, second = race(world.a, world.ana, [wanted(here, "ISSUE", 2), undo(job)])
    assert first[1] is True and second.summary["revert"] == {"reverted": 0, "stale": 1, "failed": 0}
    assert rows(world.a, job)[2].action == "STALE" and level(world.a, here) == (5, 0, 5)


def test_two_reverts_at_once_go_one_after_the_other(
    world: Any, migrator: psycopg.Connection[Any]
) -> None:
    here = cell(world.a)
    job = preview(world.a, world.ana, HEADER + "IP16,ALM,7\n")
    with tenant_scope(ctx(world.a, world.ana)) as tenant:
        imports.apply(tenant, import_id=job.pk)
    first, second = race(world.a, world.ana, [undo(job), undo(job)])
    assert (first.status, second.status, level(world.a, here)) == (
        "REVERTED",
        "REVERTED",
        (0, 0, 0),
    )
    assert len(migrator.execute(AUDIT).fetchall()) == 1 and len(ledger(world.a)) == 2


def test_a_revert_and_another_import_of_the_same_cells_do_not_deadlock(
    world: Any, migrator: psycopg.Connection[Any]
) -> None:
    """La reversión va de A a B; la otra importación (B, A en el archivo) también empieza por A:
    con la fila del saldo de B retenida, las dos esperan sin bloquear nada que la otra necesite."""
    low, here = cell(world.a, "A1")
    high = cell(world.a, "B2")[0]
    for variant in (low, high):
        move(world.a, world.ana, wanted((variant, here), "RECEIPT", 1))
    one = preview(world.a, world.ana, HEADER + "A1,ALM,5\nB2,ALM,5\n")
    with tenant_scope(ctx(world.a, world.ana)) as tenant:
        imports.apply(tenant, import_id=one.pk)
    two = preview(world.a, world.ana, HEADER + "B2,ALM,7\nA1,ALM,7\n")
    other = lambda tenant: imports.apply(tenant, import_id=two.pk)  # noqa: E731
    outcome = _held(migrator, world, (undo(one), other), high)
    assert not any(isinstance(done, str) for done in outcome), outcome
    assert outcome[0].summary["revert"]["reverted"] == 2
    assert outcome[1].summary["result"]["stale"] == 2
    assert (level(world.a, (low, here)), level(world.a, (high, here))) == ((1, 0, 1), (1, 0, 1))

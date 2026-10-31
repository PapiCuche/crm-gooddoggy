"""F5-03b: crear, cambiar, asignar y borrar oportunidades, con PostgreSQL real y el rol `crm_app`,
también con dos transacciones a la vez. Sin API: llega con F5-04."""

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from functools import partial
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from django.utils import timezone

from apps.deals import commands, opportunities
from apps.deals.models import Opportunity, OpportunityItem, Pipeline
from apps.deals.opportunities import BadReference, OpportunityClosed
from core.tenancy.scope import tenant_scope
from tests import test_opportunities_tables as tables
from tests.factories import make_user
from tests.test_inventory_stock import race
from tests.test_lost_reasons import count, run
from tests.test_memberships import ctx, join
from tests.test_opportunities_tables import contact, stage, variant
from tests.test_pipeline_commands import new as new_pipeline

world = tables.world
pytestmark = pytest.mark.usefixtures("tenant_db")
type DB = psycopg.Connection[Any]
NAIVE, FAR = datetime(2026, 11, 1, 9, 0), datetime(3000, 1, 1, tzinfo=UTC)


def create(world: SimpleNamespace, **fields: Any) -> Opportunity:
    wanted = {"contact_id": contact(world.a).pk, "title": "iPhone 16", **fields}
    made: Opportunity = run(world, opportunities.create_opportunity, **wanted)
    return made


def reload(world: SimpleNamespace, pk: Any, org: str = "a") -> Opportunity:
    with tenant_scope(ctx(getattr(world, org))):
        return Opportunity.objects.get(pk=pk)  # type: ignore[no-any-return]


def audit(migrator: DB) -> list[tuple[str, str, dict[str, Any]]]:
    found = migrator.execute(
        "SELECT action, entity_label, changes::text FROM audit_logs "
        "WHERE entity_type = 'opportunity' ORDER BY occurred_at, id"
    ).fetchall()
    return [(action, label, json.loads(changes)) for action, label, changes in found]


def history(migrator: DB) -> list[tuple[Any, ...]]:
    return migrator.execute(
        "SELECT from_stage_id, to_stage_id, changed_by_user_id, seconds_in_previous_stage "
        "FROM opportunity_stage_history ORDER BY changed_at, id"
    ).fetchall()


def test_create_opens_the_opportunity_in_the_first_open_stage_and_audits(
    world: SimpleNamespace, migrator: DB
) -> None:
    sku = variant(world.a).pk
    note = "Pide factura con RUC 20123456789"
    made = create(
        world, amount="1500.5", priority="HIGH", next_action="Llamar",
        expected_close_date=date(2026, 12, 1),
        items=[{"variant_id": sku, "quantity": 2, "estimated_unit_price": Decimal("750.25"),
                "notes": note}],
    )  # fmt: skip
    nuevo = stage(world.a)
    assert (made.number, made.status, made.probability, made.currency) == (
        "OPP-000001", "OPEN", 10, "PEN",
    )  # fmt: skip
    assert (made.stage_id, made.pipeline_id, made.version) == (nuevo.pk, nuevo.pipeline_id, 1)
    assert (made.assigned_user_id, made.created_by_user_id) == (world.ana.pk, world.ana.pk)
    assert made.amount == Decimal("1500.50") and made.stage_entered_at == made.last_activity_at
    with tenant_scope(ctx(world.a)):
        assert OpportunityItem.objects.values_list("quantity", "notes").get() == (2, note)
    assert history(migrator) == [(None, nuevo.pk, world.ana.pk, None)]
    [(action, label, changes)] = audit(migrator)
    assert (action, label) == ("opportunity.created", "OPP-000001")
    assert changes["title"] == [None, "iPhone 16"] and changes["next_action"] == [None, "[texto]"]
    assert changes["items"][1] == [
        {"variant_id": str(sku), "quantity": 2, "estimated_unit_price": "750.25", "has_notes": True}
    ]
    assert note not in json.dumps(changes) and "Llamar" not in json.dumps(changes)
    assert create(world).number == "OPP-000002"  # la numeración sigue
    assert tables.make(world.b).number == "OPP-000001"  # y es de cada organización


def test_create_in_a_chosen_pipeline_for_another_member(world: SimpleNamespace) -> None:
    other, pedro = new_pipeline(world, "mayoristas"), make_user()
    join(world.a, pedro)
    made = create(world, pipeline_id=other.pk, assigned_user_id=pedro.pk)
    assert (made.pipeline_id, made.assigned_user_id) == (other.pk, pedro.pk)
    assert made.stage_id != stage(world.a).pk and made.created_by_user_id == world.ana.pk


@pytest.mark.parametrize(
    ("field", "make_bad"),
    [
        ("contact_id", lambda w: contact(w.b).pk),  # de otra organización
        ("contact_id", lambda w: uuid4()),
        ("contact_id", lambda w: deleted_contact(w)),
        ("pipeline_id", lambda w: stage(w.b).pipeline_id),
        ("pipeline_id", lambda w: uuid4()),
        ("pipeline_id", lambda w: inactive_pipeline(w)),
        ("assigned_user_id", lambda w: w.luis.pk),  # no es miembro de A
        ("assigned_user_id", lambda w: suspended(w)),
        ("items", lambda w: [{"variant_id": variant(w.b).pk, "quantity": 1}]),
        ("items", lambda w: [{"variant_id": uuid4(), "quantity": 1}]),
    ],
)
def test_create_rejects_a_reference_that_does_not_serve_and_writes_nothing(
    world: SimpleNamespace, migrator: DB, field: str, make_bad: Any
) -> None:
    bad = make_bad(world)
    mine = contact(world.a).pk
    fields = {"contact_id": mine, "title": "x", field: bad}
    with pytest.raises(BadReference) as caught:
        run(world, opportunities.create_opportunity, **fields)
    assert caught.value.field == ("variant_id" if field == "items" else field)
    for table in ("opportunities", "opportunity_items", "opportunity_stage_history"):
        assert count(migrator, table) == 0
    assert audit(migrator) == []
    assert create(world).number == "OPP-000001"  # el número no se gastó


def deleted_contact(world: SimpleNamespace) -> Any:
    made = contact(world.a)
    with tenant_scope(ctx(world.a)):
        type(made).objects.filter(pk=made.pk).update(deleted_at=timezone.now())
    return made.pk


def inactive_pipeline(world: SimpleNamespace) -> Any:
    other = new_pipeline(world, "inactivo")
    with tenant_scope(ctx(world.a)):
        Pipeline.objects.filter(pk=other.pk).update(is_active=False)
    return other.pk


def suspended(world: SimpleNamespace) -> Any:
    sleeper = make_user()
    join(world.a, sleeper, "SUSPENDED")
    return sleeper.pk


HUGE = "9" * 100_000
LINE = {"variant_id": str(uuid4()), "quantity": 1}
QUANTITIES: tuple[Any, ...] = (0, -1, True, 1.5, "1", None, 10**9, [])
PRICES: tuple[Any, ...] = (None, 1.5, True, "-1", "1.001")
NOTES: tuple[Any, ...] = (None, 5, [], "x" * 1001, "a\nb")
BAD: dict[str, list[Any]] = {
    "title": [None, 5, True, [], "", "   ", "!!!", "x" * 201, "a\nb", HUGE],
    "amount": [None, 1.5, True, [], "abc", "-1", "1e3", "1.001", "9" * 13, "", HUGE,
               Decimal("NaN"), Decimal("Infinity"), Decimal("1E+999999"), Decimal("1.100")],
    "priority": [None, 1, True, [], ["LOW"], "urgent", "ASAP", HUGE],
    "source": [None, 1, True, [], "RADIO", "manual", HUGE],
    "next_action": [5, True, [], "", "   ", "x" * 201, "a\nb", HUGE],
    "next_action_at": [5, True, [], "2026-11-01T09:00:00Z", NAIVE, FAR, date(2026, 11, 1)],
    "expected_close_date": [5, True, [], "2026-12-01", datetime(2026, 12, 1, tzinfo=UTC),
                            date(3000, 1, 1)],
    "items": [None, "x", 5, {}, [None], [{}], [5], [{"variant_id": None, "quantity": 1}],
              [{"variant_id": "x", "quantity": 1}], [{"variant_id": 5, "quantity": 1}],
              *([{**LINE, "quantity": q}] for q in QUANTITIES),
              *([{**LINE, "estimated_unit_price": p}] for p in PRICES),
              *([{**LINE, "notes": n}] for n in NOTES),
              [{**LINE, "bogus": 1}], [LINE, LINE], [{"quantity": 1}],
              [{"variant_id": LINE["variant_id"]}],
              [{"variant_id": str(uuid4()), "quantity": 1} for _ in range(51)]],
    "bogus": [1],
    "organization_id": [str(uuid4())],  # nunca del payload
}  # fmt: skip


def rejects(call: Any, label: str) -> None:
    try:
        call()
    except ValueError as error:
        assert not isinstance(error, BadReference), label
    except Exception as error:  # noqa: BLE001 — cualquier otro tipo es el fallo que se busca
        pytest.fail(f"{label}: {type(error).__name__} en vez de ValueError")
    else:
        pytest.fail(f"{label}: se aceptó")


def reject_all(world: SimpleNamespace, command: Any, base: dict[str, Any], cases: Any) -> None:
    """Cada valor malo de cada campo, sobre `base`, es un `ValueError`."""
    for field, values in cases.items():
        for bad in values:
            call = partial(run, world, command, **{**base, field: bad})
            rejects(call, f"{field}={str(bad)[:30]!r}")


def test_create_rejects_every_bad_type_with_value_error_and_writes_nothing(
    world: SimpleNamespace, migrator: DB
) -> None:
    base = {"contact_id": contact(world.a).pk, "title": "x"}
    refs = {f: [True, 5, [], {}, "x", HUGE] for f in ("pipeline_id", "assigned_user_id")}
    reject_all(world, opportunities.create_opportunity, base, BAD | refs)
    reject_all(
        world, opportunities.create_opportunity, base, {"contact_id": [None, True, 5, [], "x"]}
    )
    assert count(migrator, "opportunities") == 0 and audit(migrator) == []


def test_two_creations_at_once_get_distinct_consecutive_numbers(world: SimpleNamespace) -> None:
    mine = contact(world.a).pk

    def one(tenant: Any) -> str:
        return opportunities.create_opportunity(tenant, contact_id=mine, title="x").number

    first, second = race(world.a, world.ana, [one, one])  # el mismo contacto: el segundo espera
    assert (first, second) == ("OPP-000001", "OPP-000002")


def test_update_changes_what_is_sent_and_audits_before_and_after(
    world: SimpleNamespace, migrator: DB
) -> None:
    made, sku, spare = create(world), variant(world.a).pk, variant(world.a, "B").pk
    then = made.last_activity_at
    got = run(
        world, opportunities.update_opportunity, opportunity_id=made.pk, version=1,
        title=" Galaxy  S25 ",
        amount="1500.5", priority="HIGH", source="WALK_IN", next_action="Llamar", probability=55,
        next_action_at=datetime(2026, 11, 2, 15, tzinfo=UTC), expected_close_date=date(2026, 12, 1),
        items=[{"variant_id": sku, "quantity": 1}],
    )  # fmt: skip
    row = reload(world, made.pk)
    assert (got.version, row.version, row.title, row.amount) == (
        2,
        2,
        "Galaxy S25",
        Decimal("1500.50"),
    )
    assert (row.priority, row.source, row.probability) == ("HIGH", "WALK_IN", 55)
    assert row.last_activity_at > then and row.updated_by_user_id == world.ana.pk
    [_, (action, label, changes)] = audit(migrator)
    assert (action, label) == ("opportunity.updated", made.number)
    assert changes["amount"] == ["0.00", "1500.50"] and changes["probability"] == [10, 55]
    assert changes["next_action"] == [None, "[texto]"] and "Llamar" not in json.dumps(changes)
    assert changes["items"][0] == [] and len(changes["items"][1]) == 1
    run(  # reemplazar: cambia la cantidad de una, se quita otra y entra una nueva
        world, opportunities.update_opportunity, opportunity_id=made.pk, version=2,
        items=[{"variant_id": spare, "quantity": 3, "notes": "gris"}],
    )  # fmt: skip
    with tenant_scope(ctx(world.a)):
        assert [(i.variant_id, i.quantity, i.notes) for i in OpportunityItem.objects.all()] == [
            (spare, 3, "gris")
        ]
    run(world, opportunities.update_opportunity, opportunity_id=made.pk, version=3, items=[])
    assert count(migrator, "opportunity_items") == 0
    run(
        world, opportunities.update_opportunity, opportunity_id=made.pk, version=4, next_action=None
    )
    assert reload(world, made.pk).next_action is None


def test_update_with_nothing_new_neither_writes_nor_audits(
    world: SimpleNamespace, migrator: DB
) -> None:
    sku = variant(world.a).pk
    made = create(world, items=[{"variant_id": sku, "quantity": 1}], priority="LOW")
    before = reload(world, made.pk)
    same = [{"variant_id": str(sku), "quantity": 1, "estimated_unit_price": "0.00", "notes": ""}]
    for fields in ({}, {"priority": "LOW", "title": "iPhone 16"}, {"items": same}):
        run(world, opportunities.update_opportunity, opportunity_id=made.pk, version=1, **fields)
    after = reload(world, made.pk)
    assert (after.version, after.updated_at, after.last_activity_at) == (
        1, before.updated_at, before.last_activity_at,
    )  # fmt: skip
    assert [a for a, _, _ in audit(migrator)] == ["opportunity.created"]


def test_update_rejections_write_nothing(world: SimpleNamespace, migrator: DB) -> None:
    made, other = create(world), tables.make(world.b)
    done = tables.make(world.a, status="WON", stage_id=stage(world.a, "ganado").pk, probability=100,
                       won_at=timezone.now())  # fmt: skip
    update = opportunities.update_opportunity
    with pytest.raises(commands.StaleVersion):
        run(world, update, opportunity_id=made.pk, version=2, title="x")
    with pytest.raises(OpportunityClosed):
        run(world, update, opportunity_id=done.pk, version=1, title="x")
    with pytest.raises(Opportunity.DoesNotExist):  # la de otra organización
        run(world, update, opportunity_id=other.pk, version=1, title="x")
    with pytest.raises(BadReference):
        run(
            world,
            update,
            opportunity_id=made.pk,
            version=1,
            items=[{"variant_id": uuid4(), "quantity": 1}],
        )
    assert reload(world, made.pk).version == 1 and reload(world, done.pk).title == "iPhone 16"
    assert [a for a, _, _ in audit(migrator)] == ["opportunity.created"]


def test_update_rejects_every_bad_type_with_value_error(world: SimpleNamespace) -> None:
    made = create(world)
    cases = {k: v for k, v in BAD.items() if k != "title"} | {
        "title": BAD["title"], "probability": [None, True, 1.5, "5", -1, 101, [], 10**20],
        "opportunity_id": [None, True, 5, [], "x", HUGE],
        "version": [None, True, 0, "1", 1.5, [], 2**40],
    }  # fmt: skip
    base = {"opportunity_id": made.pk, "version": 1, "title": "otro"}
    reject_all(world, opportunities.update_opportunity, base, cases)
    assert reload(world, made.pk).version == 1


def test_two_updates_from_the_same_version_leave_one_and_a_stale_version(
    world: SimpleNamespace,
) -> None:
    made = create(world)

    def edit(title: str) -> Any:
        return lambda tenant: opportunities.update_opportunity(
            tenant, opportunity_id=made.pk, version=1, title=title
        )

    first, second = race(world.a, world.ana, [edit("Uno"), edit("Dos")])
    assert first.title == "Uno" and second == "StaleVersion"
    assert reload(world, made.pk).title == "Uno" and reload(world, made.pk).version == 2


def test_assign_changes_the_owner_only_to_an_active_member(
    world: SimpleNamespace, migrator: DB
) -> None:
    made, pedro = create(world), make_user()
    join(world.a, pedro)
    assign = opportunities.assign_opportunity
    run(world, assign, opportunity_id=made.pk, version=1, assigned_user_id=world.ana.pk)  # ya lo es
    assert reload(world, made.pk).version == 1 and len(audit(migrator)) == 1
    run(world, assign, opportunity_id=made.pk, version=1, assigned_user_id=str(pedro.pk))
    assert reload(world, made.pk).assigned_user_id == pedro.pk
    [_, (action, _, changes)] = audit(migrator)
    assert action == "opportunity.assigned"
    assert changes == {"assigned_user_id": [str(world.ana.pk), str(pedro.pk)]}
    for bad in (world.luis.pk, suspended(world), uuid4()):  # otra organización, suspendido, nadie
        with pytest.raises(BadReference):
            run(world, assign, opportunity_id=made.pk, version=2, assigned_user_id=bad)
    with pytest.raises(commands.StaleVersion):
        run(world, assign, opportunity_id=made.pk, version=1, assigned_user_id=world.ana.pk)
    assert reload(world, made.pk).assigned_user_id == pedro.pk and len(audit(migrator)) == 2
    base = {"opportunity_id": made.pk, "version": 2, "assigned_user_id": world.ana.pk}
    cases = {
        "assigned_user_id": [None, True, "x", 5, []],
        "opportunity_id": [None, 5, "x"],
        "version": [None, True, "2", 0, 1.5],
    }
    reject_all(world, assign, base, cases)


def test_delete_marks_the_opportunity_deleted_and_audits(
    world: SimpleNamespace, migrator: DB
) -> None:
    made, other = create(world), tables.make(world.b)
    run(world, opportunities.delete_opportunity, opportunity_id=made.pk)
    row = reload(world, made.pk)
    assert (row.deleted_at is not None, row.deleted_by_user_id, row.version) == (
        True,
        world.ana.pk,
        2,
    )
    assert [(a, label) for a, label, _ in audit(migrator)] == [
        ("opportunity.created", made.number), ("opportunity.deleted", made.number),
    ]  # fmt: skip
    for call, kwargs in (
        (opportunities.delete_opportunity, {}),
        (opportunities.update_opportunity, {"version": 2, "title": "x"}),
        (opportunities.assign_opportunity, {"version": 2, "assigned_user_id": world.ana.pk}),
    ):
        with pytest.raises(Opportunity.DoesNotExist):  # borrada: ya no se toca
            run(world, call, opportunity_id=made.pk, **kwargs)
    with pytest.raises(Opportunity.DoesNotExist):
        run(world, opportunities.delete_opportunity, opportunity_id=other.pk)
    reject_all(
        world, opportunities.delete_opportunity, {}, {"opportunity_id": [None, True, 5, "x", []]}
    )

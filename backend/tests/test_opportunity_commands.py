"""F5-03b: crear, cambiar, asignar y borrar oportunidades, con PostgreSQL real y el rol `crm_app`,
también con dos transacciones a la vez. Sin API: llega con F5-04."""

import json
from datetime import UTC, date, datetime, timedelta
from datetime import timezone as tz
from decimal import Decimal
from functools import partial
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.catalog.models import ProductVariant
from apps.contacts.commands import delete_contact
from apps.contacts.models import Contact
from apps.deals import commands, opportunities
from apps.deals.models import Opportunity, OpportunityItem, Pipeline, PipelineStage
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
NAIVE, FAR, NOW = datetime(2026, 11, 1, 9, 0), datetime(3000, 1, 1, tzinfo=UTC), timezone.now()


def create(world: SimpleNamespace, **fields: Any) -> Opportunity:
    wanted = {"contact_id": contact(world.a).pk, "title": "iPhone 16", **fields}
    made: Opportunity = run(world, opportunities.create_opportunity, **wanted)
    return made


def update(world: SimpleNamespace, opp: Any, version: int = 1, **fields: Any) -> Opportunity:
    changed: Opportunity = run(
        world, opportunities.update_opportunity, opportunity_id=opp.pk, version=version, **fields
    )
    return changed


def reload(world: SimpleNamespace, pk: Any, org: str = "a") -> Opportunity:
    with tenant_scope(ctx(getattr(world, org))):
        return Opportunity.objects.get(pk=pk)  # type: ignore[no-any-return]


def item(sku: Any, quantity: int = 1, **extra: Any) -> dict[str, Any]:
    return {"variant_id": sku, "quantity": quantity, **extra}


def lines(world: SimpleNamespace, *columns: str) -> list[Any]:
    with tenant_scope(ctx(world.a)):
        return list(OpportunityItem.objects.values_list(*columns))


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


def test_create_opens_in_the_first_open_stage(world: SimpleNamespace, migrator: DB) -> None:
    sku, note = variant(world.a).pk, "Pide factura con RUC 20123456789"
    made = create(
        world, title="Juan Perez DNI 12345678", amount="1500.5", priority="HIGH",
        next_action="Llamar", expected_close_date=date(2026, 12, 1),
        items=[item(sku, 2, estimated_unit_price=Decimal("750.25"), notes=note)],
    )  # fmt: skip
    nuevo = stage(world.a)
    assert (made.number, made.status, made.probability, made.currency, made.version) == (
        "OPP-000001", "OPEN", 10, "PEN", 1,
    )  # fmt: skip
    assert (made.stage_id, made.pipeline_id) == (nuevo.pk, nuevo.pipeline_id)
    assert made.assigned_user_id == made.created_by_user_id == world.ana.pk
    assert made.amount == Decimal("1500.50") and made.stage_entered_at == made.last_activity_at
    assert lines(world, "quantity", "notes") == [(2, note)]
    assert history(migrator) == [(None, nuevo.pk, world.ana.pk, None)]
    [(action, label, changes)] = audit(migrator)
    assert (action, label) == ("opportunity.created", "OPP-000001")
    assert changes["title"] == changes["next_action"] == [None, "[texto]"]
    assert [(i["variant_id"], i["has_notes"]) for i in changes["items"][1]] == [(str(sku), True)]
    for text in (note, "Llamar", "Juan", "12345678"):  # texto libre: ni título ni notas
        assert text not in json.dumps(changes)
    assert create(world).number == "OPP-000002"  # la numeración sigue
    assert tables.make(world.b).number == "OPP-000001"  # y es de cada organización
    other, pedro = new_pipeline(world, "mayoristas"), make_user()
    join(world.a, pedro)  # otro pipeline y otro responsable
    chosen = create(world, pipeline_id=other.pk, assigned_user_id=pedro.pk)
    assert (chosen.pipeline_id, chosen.assigned_user_id) == (other.pk, pedro.pk)
    assert chosen.stage_id != nuevo.pk and chosen.created_by_user_id == world.ana.pk
    change(world, PipelineStage, nuevo.pk, is_active=False)  # la primera por orden que sirve
    change(world, PipelineStage, stage(world.a, "perdido").pk, sort_order=-5)  # primera, pero LOST
    assert create(world).stage_id == stage(world.a, "contactado").pk


@pytest.mark.parametrize(
    ("field", "make_bad"),
    [
        ("contact_id", lambda w: contact(w.b).pk),  # de otra organización
        ("contact_id", lambda w: change(w, Contact, contact(w.a).pk, deleted_at=NOW)),
        ("pipeline_id", lambda w: stage(w.b).pipeline_id),
        ("pipeline_id", lambda w: change(w, Pipeline, new_pipeline(w, "x").pk, is_active=False)),
        ("pipeline_id", lambda w: change(w, Pipeline, new_pipeline(w, "y").pk, deleted_at=NOW)),
        ("assigned_user_id", lambda w: w.luis.pk),  # no es miembro de A
        ("assigned_user_id", lambda w: join(w.a, make_user(), "SUSPENDED").user_id),
        ("items", lambda w: [{"variant_id": variant(w.b).pk, "quantity": 1}]),
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


def change(world: SimpleNamespace, model: Any, pk: Any, **fields: Any) -> Any:
    with tenant_scope(ctx(world.a)):
        model.objects.filter(pk=pk).update(**fields)
    return pk


HUGE = "9" * 100_000
LINE = {"variant_id": str(uuid4()), "quantity": 1}
CAP = "999999999999.99"  # lo que cabe en NUMERIC(14,2)
PENNY = item(str(uuid4()), estimated_unit_price="0.01")
BAD_LINES: dict[str, list[Any]] = {
    "quantity": [0, -1, True, 1.5, "1", None, 10**9, []],
    "estimated_unit_price": [None, 1.5, True, "-1", "1.001"],
    "notes": [None, 5, [], "x" * 1001, "a\nb"],
}
BAD: dict[str, list[Any]] = {
    "title": [None, 5, True, [], "", "   ", "!!!", "x" * 201, "a\nb", HUGE],
    "amount": [None, 1.5, True, [], "abc", "-1", "1e3", "1.001", "9" * 13, "", HUGE,
               Decimal("NaN"), Decimal("Infinity"), Decimal("1E+999999999"), Decimal("1.100")],
    "priority": [None, 1, True, [], ["LOW"], "urgent", "ASAP", HUGE],
    "source": [None, 1, True, [], "RADIO", "manual", HUGE],
    "next_action": [5, True, [], "", "   ", "x" * 201, "a\nb", HUGE],
    "next_action_at": [5, True, [], "2026-11-01T09:00:00Z", NAIVE, FAR, date(2026, 11, 1)],
    "expected_close_date": [5, True, [], "2026-12-01", datetime(2026, 12, 1, tzinfo=UTC),
                            date(3000, 1, 1)],
    "items": [None, "x", 5, {}, [None], [{}], [5], [{"variant_id": None, "quantity": 1}],
              [{"variant_id": "x", "quantity": 1}], [{"variant_id": 5, "quantity": 1}],
              *([{**LINE, f: v}] for f, vs in BAD_LINES.items() for v in vs),
              [{**LINE, "bogus": 1}], [LINE, LINE], [{"quantity": 1}],
              [{"variant_id": LINE["variant_id"]}],
              [{"variant_id": str(uuid4()), "quantity": 1} for _ in range(51)],
              [{**LINE, "quantity": 10**6, "estimated_unit_price": CAP}],  # una línea que excede
              [{**LINE, "estimated_unit_price": CAP}, PENNY],
              [{**LINE, "quantity": 3, "estimated_unit_price": "333333333333.34"}]],  # +0.03
    "bogus": [1],
    "organization_id": [str(uuid4())],  # nunca del payload
}  # fmt: skip


def rejects(call: Any, label: str) -> None:
    with pytest.raises(ValueError) as caught:  # un `TypeError` o `IntegrityError` propaga: fallo
        call()
    assert not isinstance(caught.value, BadReference), label


def reject_all(world: SimpleNamespace, command: Any, base: dict[str, Any], cases: Any) -> None:
    """Cada valor malo de cada campo, sobre `base`, es un `ValueError`."""
    for field, values in cases.items():
        for bad in values:
            call = partial(run, world, command, **{**base, field: bad})
            rejects(call, f"{field}={str(bad)[:30]!r}")


def test_create_rejects_every_bad_type(world: SimpleNamespace, migrator: DB) -> None:
    base = {"contact_id": contact(world.a).pk, "title": "x"}
    refs = {f: [True, 5, [], {}, "x", HUGE] for f in ("pipeline_id", "assigned_user_id")}
    cases = BAD | refs | {"contact_id": [None, True, 5, [], "x"]}
    reject_all(world, opportunities.create_opportunity, base, cases)
    assert count(migrator, "opportunities") == 0 and audit(migrator) == []


def test_update_changes_and_audits_before_and_after(world: SimpleNamespace, migrator: DB) -> None:
    made, sku, spare = create(world), variant(world.a).pk, variant(world.a, "B").pk
    then = made.last_activity_at
    got = update(
        world, made, title=" Galaxy  S25 ", amount="1500.5", priority="HIGH", source="WALK_IN",
        next_action="Llamar", probability=55, next_action_at=datetime(2026, 11, 2, 15, tzinfo=UTC),
        expected_close_date=date(2026, 12, 1), items=[item(sku)],
    )  # fmt: skip
    row = reload(world, made.pk)
    assert (got.version, row.version, row.title) == (2, 2, "Galaxy S25")
    assert (row.amount, row.priority, row.source) == (Decimal("1500.50"), "HIGH", "WALK_IN")
    assert row.probability == 55
    assert row.last_activity_at > then and row.updated_by_user_id == world.ana.pk
    [_, (action, label, changes)] = audit(migrator)
    assert (action, label) == ("opportunity.updated", made.number)
    assert changes["amount"] == ["0.00", "1500.50"] and changes["probability"] == [10, 55]
    assert changes["title"] == ["[texto]"] * 2 and changes["next_action"] == [None, "[texto]"]
    assert "Galaxy" not in json.dumps(changes) and "Llamar" not in json.dumps(changes)
    assert changes["items"][0] == [] and len(changes["items"][1]) == 1
    update(world, made, 2, items=[item(sku, 5, estimated_unit_price="9.5")])
    assert lines(world, "quantity", "estimated_unit_price") == [(5, Decimal("9.50"))]  # en sitio
    update(world, made, 3, items=[item(spare, 3, notes="gris")])
    assert lines(world, "variant_id", "quantity", "notes") == [(spare, 3, "gris")]
    update(world, made, 4, next_action=None)
    assert reload(world, made.pk).next_action is None


@pytest.mark.parametrize(
    "off", [{"deleted_at": NOW}, {"status": "INACTIVE"}, {"status": "DISCONTINUED"}]
)
def test_a_line_needs_a_live_variant(
    world: SimpleNamespace, migrator: DB, off: dict[str, Any]
) -> None:
    keep, gone = variant(world.a, "KEEP").pk, variant(world.a, "GONE").pk
    made = create(world, items=[item(keep)])
    for sku in (keep, gone):
        change(world, ProductVariant, sku, **off)
    fresh, line = variant(world.a, "FRESH").pk, [item(gone)]
    with pytest.raises(BadReference) as caught:
        create(world, items=[item(fresh), *line])  # una buena y una mala: no basta una
    assert caught.value.field == "variant_id" and count(migrator, "opportunities") == 1
    with pytest.raises(BadReference):
        update(world, made, items=[item(keep), item(fresh), *line])
    update(world, made, items=[item(keep, 2)])  # la ya presente no se revisa
    assert lines(world, "quantity") == [(2,)]


def test_limits_offsets_and_variant_lock_order(world: SimpleNamespace) -> None:
    far = datetime(2026, 1, 1, tzinfo=tz(timedelta(hours=23, minutes=59)))
    full = item(variant(world.a).pk, 3, estimated_unit_price="333333333333.33")  # justo el tope
    with CaptureQueriesContext(connection) as queries:
        made = create(world, next_action_at=far, items=[full, item(variant(world.a, "B").pk)])
    assert made.next_action_at == far and str(made.next_action_at).endswith("+00:00")
    locks = [q["sql"] for q in queries if q["sql"].startswith("SELECT id FROM public.product_var")]
    assert len(locks) == 1 and "ORDER BY id FOR SHARE" in locks[0]


def test_update_rejections_and_no_ops_write_nothing(world: SimpleNamespace, migrator: DB) -> None:
    sku = variant(world.a).pk
    made, other = create(world, items=[item(sku)], priority="LOW"), tables.make(world.b)
    same = [item(str(sku), estimated_unit_price="0.00", notes="")]
    for fields in ({}, {"priority": "LOW", "title": "iPhone 16"}, {"items": same}):
        update(world, made, **fields)  # nada nuevo: ni escribe ni audita
    done = tables.make(world.a, status="WON", stage_id=stage(world.a, "ganado").pk, probability=100,
                       won_at=timezone.now())  # fmt: skip
    wrong: list[tuple[type[Exception], Any, int, dict[str, Any]]] = [
        (commands.StaleVersion, made, 2, {"title": "x"}),
        (OpportunityClosed, done, 1, {"title": "x"}),
        (Opportunity.DoesNotExist, other, 1, {"title": "x"}),  # de otra organización
        (BadReference, made, 1, {"items": [item(uuid4())]}),
    ]
    for error, target, version, fields in wrong:
        with pytest.raises(error):
            update(world, target, version, **fields)
    assert reload(world, made.pk).version == 1 and reload(world, done.pk).title == "iPhone 16"
    assert [a for a, _, _ in audit(migrator)] == ["opportunity.created"]


def test_update_rejects_every_bad_type_with_value_error(world: SimpleNamespace) -> None:
    made = create(world)
    cases = BAD | {
        "probability": [None, True, 1.5, "5", -1, 101, [], 10**20],
        "opportunity_id": [None, True, 5, [], "x", HUGE],
        "version": [None, True, 0, "1", 1.5, [], 2**40],
    }  # fmt: skip
    base = {"opportunity_id": made.pk, "version": 1, "title": "otro"}
    reject_all(world, opportunities.update_opportunity, base, cases)
    assert reload(world, made.pk).version == 1


def test_two_updates_from_one_version_leave_one(
    world: SimpleNamespace,
) -> None:
    made = create(world)

    def edit(title: str) -> Any:
        return lambda tenant: opportunities.update_opportunity(
            tenant, opportunity_id=made.pk, version=1, title=title
        )

    first, second = race(world.a, world.ana, [edit("Uno"), edit("Dos")])
    assert first.title == "Uno" and second == "StaleVersion"
    assert (reload(world, made.pk).title, reload(world, made.pk).version) == ("Uno", 2)


def test_assign_needs_an_active_member(world: SimpleNamespace, migrator: DB) -> None:
    made, pedro = create(world), make_user()
    join(world.a, pedro)
    base = {"opportunity_id": made.pk, "version": 1, "assigned_user_id": world.ana.pk}
    run(world, opportunities.assign_opportunity, **base)  # ya lo es: ni escribe ni audita
    assert reload(world, made.pk).version == 1 and len(audit(migrator)) == 1
    run(world, opportunities.assign_opportunity, **{**base, "assigned_user_id": str(pedro.pk)})
    assert reload(world, made.pk).assigned_user_id == pedro.pk
    [_, (action, _, changes)] = audit(migrator)
    assert action == "opportunity.assigned"
    assert changes == {"assigned_user_id": [str(world.ana.pk), str(pedro.pk)]}
    base["version"] = 2
    sleeper = join(world.a, make_user(), "SUSPENDED").user_id
    for bad in (world.luis.pk, sleeper, uuid4()):  # otra organización, suspendido, nadie
        with pytest.raises(BadReference):
            run(world, opportunities.assign_opportunity, **{**base, "assigned_user_id": bad})
    with pytest.raises(commands.StaleVersion):
        run(world, opportunities.assign_opportunity, **{**base, "version": 1})
    assert reload(world, made.pk).assigned_user_id == pedro.pk and len(audit(migrator)) == 2
    cases = {"assigned_user_id": [None, True, "x", 5, []], "opportunity_id": [None, 5, "x"],
             "version": [None, True, "2", 0, 1.5]}  # fmt: skip
    reject_all(world, opportunities.assign_opportunity, base, cases)


def test_delete_is_logical_and_audited(world: SimpleNamespace, migrator: DB) -> None:
    made, other = create(world), tables.make(world.b)
    run(world, opportunities.delete_opportunity, opportunity_id=made.pk)
    row = reload(world, made.pk)
    assert row.deleted_at and (row.deleted_by_user_id, row.version) == (world.ana.pk, 2)
    assert [a for a, _, _ in audit(migrator)] == ["opportunity.created", "opportunity.deleted"]
    for call, more in (
        (opportunities.delete_opportunity, {}),
        (opportunities.update_opportunity, {"version": 2, "title": "x"}),
        (opportunities.assign_opportunity, {"version": 2, "assigned_user_id": world.ana.pk}),
    ):
        with pytest.raises(Opportunity.DoesNotExist):  # borrada: ya no se toca
            run(world, call, opportunity_id=made.pk, **more)
    with pytest.raises(Opportunity.DoesNotExist):  # la de otra organización
        run(world, opportunities.delete_opportunity, opportunity_id=other.pk)
    reject_all(
        world, opportunities.delete_opportunity, {}, {"opportunity_id": [None, True, 5, "x"]}
    )


def test_a_creation_waits_for_writers_of_its_pipeline_contact_and_variant(
    world: SimpleNamespace,
) -> None:
    other, sku = new_pipeline(world, "mayoristas"), variant(world.a).pk
    mine = [contact(world.a).pk for _ in range(3)]

    def create_for(i: int, **extra: Any) -> Any:
        return partial(opportunities.create_opportunity, contact_id=mine[i], title="x", **extra)

    off = partial(commands.update_pipeline, pipeline_id=other.pk, version=1, is_active=False)
    retire = lambda t: ProductVariant.objects.filter(pk=sku).update(deleted_at=NOW)  # noqa: E731
    for first, creation in (  # quien escribe primero y la alta que espera: la ve y la rechaza
        (off, create_for(0, pipeline_id=other.pk)),
        (partial(delete_contact, contact_id=mine[1]), create_for(1)),
        (retire, create_for(2, items=[item(sku)])),
    ):
        assert race(world.a, world.ana, [first, creation])[1] == "BadReference"


def test_updater_is_recorded_and_a_non_member_actor_refused(
    world: SimpleNamespace,
) -> None:
    pedro, made = make_user(), create(world)
    join(world.a, pedro)

    def as_(user: Any, command: Any, **kwargs: Any) -> Any:
        with tenant_scope(ctx(world.a, user)) as tenant:
            return command(tenant, **kwargs)

    as_(pedro, opportunities.update_opportunity, opportunity_id=made.pk, version=1, title="Otro")
    row = reload(world, made.pk)
    assert (row.updated_by_user_id, row.created_by_user_id) == (pedro.pk, world.ana.pk)
    one = {"opportunity_id": made.pk, "version": 2}
    for command, kwargs in (
        (opportunities.create_opportunity, {"contact_id": made.contact_id, "title": "x",
                                            "assigned_user_id": pedro.pk}),
        (opportunities.update_opportunity, {**one, "title": "Z"}),
        (opportunities.assign_opportunity, {**one, "assigned_user_id": pedro.pk}),
        (opportunities.delete_opportunity, {"opportunity_id": made.pk}),
    ):  # fmt: skip
        with pytest.raises(BadReference) as caught:  # `luis` no es miembro de A
            as_(world.luis, command, **kwargs)
        assert caught.value.field.endswith("_by_user_id")

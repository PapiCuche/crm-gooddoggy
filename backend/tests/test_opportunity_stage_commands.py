"""F5-03c: mover, ganar, perder y reabrir oportunidades y el gancho de desactivación de F5-01c
(D-F5-01), con PostgreSQL real y el rol `crm_app`, también con transacciones a la vez."""

import random
import threading
from collections.abc import Callable
from decimal import Decimal
from functools import partial
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from django.db import connection
from django.utils import timezone

from apps.deals import commands, opportunities
from apps.deals import opportunity_stages as stages
from apps.deals.models import LostReason, Opportunity, Pipeline, PipelineStage
from apps.deals.opportunities import BadReference, OpportunityClosed
from core.tenancy.scope import tenant_scope
from tests import test_opportunities_tables as tables
from tests.factories import make_user
from tests.test_inventory_stock import race
from tests.test_lost_reasons import count, run
from tests.test_memberships import ctx, join
from tests.test_opportunities_tables import reason, stage
from tests.test_opportunity_commands import audit, create, history, reject_all, reload
from tests.test_pipeline_commands import new as new_pipeline

world = tables.world
pytestmark = pytest.mark.usefixtures("tenant_db")
type DB = psycopg.Connection[Any]
HUGE = "9" * 100_000
LOST = {"stage_type": "LOST", "default_probability": 0}
WEIRD: list[Any] = [None, True, False, 0, -1, 2**70, 1.5, float("nan"), "", " ", "x", "\x00", HUGE,
                    [], [[1]], {}, (1,), b"1", object()]  # fmt: skip


def step(world: SimpleNamespace, command: Any, opp: Any, version: int = 1, **kw: Any) -> Any:
    return run(world, command, opportunity_id=opp.pk, version=version, **kw)


def move(world: SimpleNamespace, opp: Any, code: str, version: int = 1, **kw: Any) -> Any:
    kw = {"stage_id": stage(world.a, code).pk, **kw}
    return step(world, stages.move_opportunity, opp, version, **kw)


def win(world: SimpleNamespace, opp: Any, version: int = 1, amount: Any = "1200") -> Any:
    return step(world, stages.win_opportunity, opp, version, amount=amount)


def lose(world: SimpleNamespace, opp: Any, version: int = 1, **kw: Any) -> Any:
    kw = {"lost_reason_id": reason(world.a).pk, **kw}
    return step(world, stages.lose_opportunity, opp, version, **kw)


def reopen(world: SimpleNamespace, opp: Any, code: str = "contactado", version: int = 2) -> Any:
    return step(world, stages.reopen_opportunity, opp, version, stage_id=stage(world.a, code).pk)


def untouched(world: SimpleNamespace, migrator: DB, opp: Any, rows: int = 1) -> None:
    """Un comando rechazado no cambia la oportunidad ni deja historial ni auditoría."""
    assert reload(world, opp.pk).version == opp.version and len(history(migrator)) == rows
    assert len(audit(migrator)) == rows


def consistent(migrator: DB) -> None:
    """Historial íntegro: empieza sin etapa, cada paso sale de donde llegó el anterior, todas las
    etapas son del pipeline de la oportunidad, sin segundos negativos y acaba en su etapa; y cada
    versión de la oportunidad tiene una auditoría."""
    rows = migrator.execute(
        "SELECT h.opportunity_id, h.from_stage_id, h.to_stage_id, h.seconds_in_previous_stage, "
        "f.pipeline_id, t.pipeline_id, o.pipeline_id, o.stage_id, o.version FROM "
        "opportunity_stage_history h JOIN opportunities o ON o.id = h.opportunity_id "
        "JOIN pipeline_stages t ON t.id = h.to_stage_id "
        "LEFT JOIN pipeline_stages f ON f.id = h.from_stage_id "
        "ORDER BY h.opportunity_id, h.changed_at, h.id"
    ).fetchall()
    last: dict[Any, Any] = {}
    for opp, origin, target, seconds, from_pipe, to_pipe, pipe, *_ in rows:
        assert origin == last.get(opp) and (origin is None) == (seconds is None)
        assert to_pipe == pipe and from_pipe in (None, pipe) and (seconds or 0) >= 0
        last[opp] = target
    assert all(last[opp] == current for opp, *_, current, _ in rows)
    for opp, version in {r[0]: r[8] for r in rows}.items():
        logged = migrator.execute("SELECT count(*) FROM audit_logs WHERE entity_id = %s", [opp])
        assert logged.fetchone() == (version,)


def test_move_changes_stage_probability_clock_history_and_audit(
    world: SimpleNamespace, migrator: DB
) -> None:
    opp = create(world)
    migrator.execute("UPDATE opportunities SET stage_entered_at = now() - interval '100 seconds'")
    moved = move(world, opp, "cotizado")
    row = reload(world, opp.pk)
    assert (moved.version, row.stage_id, row.probability, row.status) == (
        2, stage(world.a, "cotizado").pk, 40, "OPEN",
    )  # fmt: skip
    assert (
        row.stage_entered_at > opp.stage_entered_at and row.last_activity_at > opp.last_activity_at
    )
    [_, (from_stage, to_stage, who, seconds)] = history(migrator)
    assert (from_stage, to_stage, who) == (stage(world.a).pk, row.stage_id, world.ana.pk)
    assert 100 <= seconds < 120
    [_, (action, label, changes)] = audit(migrator)
    assert (action, label, set(changes)) == (
        "opportunity.moved",
        opp.number,
        {"stage_id", "probability"},
    )
    assert changes["probability"] == [10, 40]
    about = migrator.execute("SELECT metadata FROM audit_logs WHERE action = 'opportunity.moved'")
    assert about.fetchone() == ({"from_stage": "nuevo", "to_stage": "cotizado",
                                 "seconds_in_previous_stage": seconds},)  # fmt: skip
    assert move(world, opp, "nuevo", version=2).probability == 10  # también hacia atrás
    move(world, opp, "nuevo", version=3)  # ya está en ella: ni escribe ni audita
    assert reload(world, opp.pk).version == 3 and len(history(migrator)) == 3
    assert len(audit(migrator)) == 3
    consistent(migrator)


def test_move_rejections_write_nothing(world: SimpleNamespace, migrator: DB) -> None:
    opp, other = create(world), tables.make(world.b)
    elsewhere = new_pipeline(world, "mayoristas")
    with tenant_scope(ctx(world.a)):
        PipelineStage.objects.filter(code="contactado", pipeline__code="retail").update(
            is_active=False
        )
        theirs = PipelineStage.objects.get(pipeline=elsewhere, code="nuevo").pk
    for code, error in (("ganado", stages.ClosingStage), ("perdido", stages.ClosingStage),
                        ("contactado", BadReference)):  # fmt: skip
        with pytest.raises(error):
            move(world, opp, code)
    for bad in (theirs, stage(world.b).pk, uuid4()):  # de otro pipeline, de otra organización
        with pytest.raises(BadReference):
            move(world, opp, "nuevo", stage_id=bad)
    with pytest.raises(commands.StaleVersion):
        move(world, opp, "cotizado", version=2)
    with pytest.raises(Opportunity.DoesNotExist):
        move(world, other, "nuevo")
    untouched(world, migrator, opp)
    done = win(world, opp)
    with pytest.raises(OpportunityClosed):
        move(world, opp, "cotizado", version=done.version)


def test_a_stage_that_requires_fields_waits_for_them(world: SimpleNamespace) -> None:
    opp = create(world)
    with tenant_scope(ctx(world.a)):
        PipelineStage.objects.filter(code="cotizado").update(
            requires_fields={"next_action": True, "otra": True}  # lo que no se conoce, no cuenta
        )
    with pytest.raises(stages.MissingFields) as caught:
        move(world, opp, "cotizado")
    assert caught.value.fields == ["next_action"]
    step(world, opportunities.update_opportunity, opp, next_action="Llamar")
    assert move(world, opp, "cotizado", version=2).version == 3


def test_win_closes_the_opportunity_with_the_final_amount(
    world: SimpleNamespace, migrator: DB
) -> None:
    opp = create(world, amount="1000")
    won = win(world, opp, amount=Decimal("1200.5"))
    row = reload(world, opp.pk)
    assert (won.version, row.status, row.probability) == (2, "WON", 100)
    assert row.amount == Decimal("1200.50") and row.won_at is not None
    assert row.stage_id == stage(world.a, "ganado").pk
    assert row.lost_at is None and row.lost_reason_id is None
    [_, (action, _, changes)] = audit(migrator)
    assert action == "opportunity.won" and changes["status"] == ["OPEN", "WON"]
    assert changes["amount"] == ["1000.00", "1200.50"] and changes["won_at"][0] is None
    for call in (win, lose, lambda w, o, v: move(w, o, "nuevo", v)):
        with pytest.raises(OpportunityClosed):
            call(world, opp, 2)
    with pytest.raises(OpportunityClosed):  # cerrada: tampoco se edita
        step(world, opportunities.update_opportunity, opp, 2, title="x")
    untouched(world, migrator, row, rows=2)
    assert win(world, create(world), amount="999999999999.99").amount == Decimal("999999999999.99")


def test_lose_needs_an_active_reason_and_keeps_the_note_out_of_the_audit(
    world: SimpleNamespace, migrator: DB
) -> None:
    opp = create(world, amount="900")
    with tenant_scope(ctx(world.a)):
        LostReason.objects.filter(code="OTHER").update(is_active=False)
        LostReason.objects.filter(code="COMPETITOR").update(deleted_at=timezone.now())
        inactive = LostReason.objects.get(code="OTHER").pk
        gone = LostReason.objects.get(code="COMPETITOR").pk
    for bad in (tables.reason(world.b).pk, inactive, gone, uuid4()):
        with pytest.raises(BadReference) as caught:
            lose(world, opp, lost_reason_id=bad)
        assert caught.value.field == "lost_reason_id"
    untouched(world, migrator, opp)
    lost = lose(world, opp, note="  Lo compró en Wong, dijo 'más barato' ")
    row = reload(world, opp.pk)
    assert (lost.version, row.status, row.probability) == (2, "LOST", 0)
    assert row.amount == Decimal("900.00") and row.lost_at is not None and row.won_at is None
    assert (row.stage_id, row.lost_reason_id) == (stage(world.a, "perdido").pk, reason(world.a).pk)
    assert row.lost_reason_note == "Lo compró en Wong, dijo 'más barato'"
    [_, (action, _, changes)] = audit(migrator)
    assert action == "opportunity.lost" and changes["lost_reason_note"] == [None, "[texto]"]
    assert "Wong" not in str(changes) and changes["lost_reason_id"][1] == str(reason(world.a).pk)
    assert reload(world, lose(world, create(world), note="   ").pk).lost_reason_note is None


def test_reopen_returns_a_closed_opportunity_to_an_open_stage_and_clears_the_closing(
    world: SimpleNamespace, migrator: DB
) -> None:
    closers: list[tuple[Callable[[Any], Any], str]] = [
        (lambda o: win(world, o), "1200.00"),
        (lambda o: lose(world, o, note="Caro"), "500.00"),
    ]
    for close, amount in closers:
        opp = create(world, amount="500")
        close(opp)
        back = reopen(world, opp)
        row = reload(world, opp.pk)
        assert (back.version, row.status, row.probability) == (3, "OPEN", 20)
        assert row.amount == Decimal(amount)  # el importe de la ganada se queda
        assert row.stage_id == stage(world.a, "contactado").pk
        assert (row.won_at, row.lost_at, row.lost_reason_id, row.lost_reason_note) == (None,) * 4
    [*_, (action, _, changes)] = audit(migrator)
    assert action == "opportunity.reopened" and changes["status"] == ["LOST", "OPEN"]
    assert changes["lost_reason_note"] == ["[texto]", None] and "Caro" not in str(changes)
    assert len(history(migrator)) == 6  # alta, cierre y reapertura de cada una
    consistent(migrator)
    again = create(world)
    with pytest.raises(stages.NotClosed):
        reopen(world, again, version=1)
    win(world, again)
    for code in ("ganado", "perdido"):  # solo a una etapa OPEN
        with pytest.raises(BadReference):
            reopen(world, again, code)
    with pytest.raises(BadReference):
        step(world, stages.reopen_opportunity, again, 2, stage_id=uuid4())
    with pytest.raises(commands.StaleVersion):
        reopen(world, again, version=1)
    with tenant_scope(ctx(world.a)):  # su pipeline se desactiva con la oportunidad ya cerrada
        Pipeline.objects.filter(pk=again.pipeline_id).update(is_default=False, is_active=False)
    with pytest.raises(BadReference) as caught:
        reopen(world, again)
    assert caught.value.field == "pipeline_id"


def test_every_hostile_argument_is_a_value_error_and_writes_nothing(
    world: SimpleNamespace, migrator: DB
) -> None:
    opp, base = create(world), {"opportunity_id": create(world).pk, "version": 1}
    sid, ids = stage(world.a).pk, {"opportunity_id": WEIRD, "version": [*WEIRD, 2**31]}
    notes: list[Any] = [5, True, [], {}, b"x", "x" * 1001, "a\nb", "\x00", HUGE, float("nan")]
    cases = (
        (stages.move_opportunity, {"stage_id": sid}, {"stage_id": WEIRD}),
        (stages.win_opportunity, {"amount": "10"}, {"amount": [*WEIRD[:-1], "0.00", "1e3",
                                                              "9" * 13, Decimal("Infinity")]}),
        (stages.lose_opportunity, {"lost_reason_id": reason(world.a).pk},
         {"lost_reason_id": WEIRD, "note": notes}),
        (stages.reopen_opportunity, {"stage_id": sid}, {"stage_id": WEIRD}),
    )  # fmt: skip
    for command, valid, bad in cases:
        reject_all(world, command, {**base, **valid}, {**ids, **bad})
    untouched(world, migrator, opp, rows=2)  # la otra oportunidad de `base` también está intacta


def test_an_actor_who_is_not_a_member_is_refused_by_every_command(
    world: SimpleNamespace, migrator: DB
) -> None:
    open_one, closed = create(world), create(world)
    win(world, closed)
    why, sid = reason(world.a).pk, stage(world.a, "cotizado").pk
    for command, opp, version, kw in (
        (stages.move_opportunity, open_one, 1, {"stage_id": sid}),
        (stages.win_opportunity, open_one, 1, {"amount": "5"}),
        (stages.lose_opportunity, open_one, 1, {"lost_reason_id": why}),
        (stages.reopen_opportunity, closed, 2, {"stage_id": sid}),
    ):
        with (
            tenant_scope(ctx(world.a, world.luis)) as tenant,
            pytest.raises(BadReference) as caught,
        ):
            command(tenant, opportunity_id=opp.pk, version=version, **kw)  # `luis` es de B
        assert caught.value.field.endswith("_by_user_id")
    assert reload(world, open_one.pk).version == 1 and reload(world, closed.pk).version == 2
    consistent(migrator)


def test_the_hook_blocks_deactivating_what_has_open_opportunities(
    world: SimpleNamespace, migrator: DB
) -> None:
    other = new_pipeline(world, "mayoristas")
    opp = create(world, pipeline_id=other.pk)  # en su primera etapa OPEN: `nuevo`
    with tenant_scope(ctx(world.a)):
        mine = PipelineStage.objects.get(pipeline=other, code="nuevo")
        spare = PipelineStage.objects.get(pipeline=other, code="cotizado")
    audited = count(migrator, "audit_logs")
    for call, kw in (
        (commands.update_pipeline, {"pipeline_id": other.pk, "version": 1, "is_active": False}),
        (commands.update_stage, {"stage_id": mine.pk, "is_active": False}),  # sale de OPEN:
        (
            commands.update_stage,
            {"stage_id": mine.pk, **LOST},
        ),
    ):
        with pytest.raises(commands.OpenOpportunities):
            run(world, call, **kw)
    assert count(migrator, "audit_logs") == audited
    with tenant_scope(ctx(world.a)):
        assert (
            Pipeline.objects.get(pk=other.pk).is_active
            and PipelineStage.objects.get(pk=mine.pk).is_active
        )
    run(world, commands.update_stage, stage_id=spare.pk, is_active=False)  # otra etapa, sin nadie
    closed = win(world, opp, amount="10")
    run(world, commands.update_pipeline, pipeline_id=other.pk, version=1, is_active=False)
    assert closed.status == "WON" and not reload(world, opp.pk).deleted_at  # solo ganadas


def test_the_hook_ignores_deleted_opportunities_and_closed_ones_pin_the_stage_type(
    world: SimpleNamespace,
) -> None:
    other = new_pipeline(world, "mayoristas")
    gone = create(world, pipeline_id=other.pk)
    run(world, opportunities.delete_opportunity, opportunity_id=gone.pk)
    with tenant_scope(ctx(world.a)):
        nuevo = PipelineStage.objects.get(pipeline=other, code="nuevo")
    run(world, commands.update_stage, stage_id=nuevo.pk, is_active=False)  # la borrada no cuenta
    run(world, commands.update_stage, stage_id=nuevo.pk, is_active=True)
    with pytest.raises(commands.StageInUse):  # tiene (aunque borrada) una oportunidad
        run(world, commands.update_stage, stage_id=nuevo.pk, **LOST)
    run(world, commands.add_stage, pipeline_id=other.pk, name="Descartado", code="descartado",
        stage_type="LOST", default_probability=0, color="#aa0000", position=2)  # fmt: skip
    kept = create(world, pipeline_id=other.pk)
    lost = lose(world, kept)
    with tenant_scope(ctx(world.a)):
        discarded = PipelineStage.objects.get(pipeline=other, code="descartado")
    assert lost.stage_id == discarded.pk  # la primera LOST por orden
    with pytest.raises(commands.StageInUse):  # LOST → OPEN con una perdida dentro
        run(world, commands.update_stage, stage_id=discarded.pk, stage_type="OPEN",
            default_probability=5)  # fmt: skip


def test_two_moves_or_two_wins_from_the_same_version_leave_one(world: SimpleNamespace) -> None:
    opp = create(world)

    def go(code: str) -> Any:
        target = stage(world.a, code).pk
        return lambda tenant: stages.move_opportunity(
            tenant, opportunity_id=opp.pk, version=1, stage_id=target
        )

    first, second = race(world.a, world.ana, [go("cotizado"), go("negociacion")])
    assert first.stage_id == stage(world.a, "cotizado").pk and second == "StaleVersion"

    def finish(tenant: Any) -> Any:
        return stages.win_opportunity(tenant, opportunity_id=opp.pk, version=2, amount="5")

    won, late = race(world.a, world.ana, [finish, finish])
    assert won.status == "WON" and late == "StaleVersion"


def test_a_move_and_a_stage_deactivation_wait_for_each_other_and_see_each_other(
    world: SimpleNamespace,
) -> None:
    opp, target = create(world), stage(world.a, "contactado").pk

    def deactivate(tenant: Any) -> Any:
        return commands.update_stage(tenant, stage_id=target, is_active=False)

    def enter(tenant: Any) -> Any:
        return stages.move_opportunity(tenant, opportunity_id=opp.pk, version=1, stage_id=target)

    done, refused = race(world.a, world.ana, [deactivate, enter])  # la desactivación, primero
    assert done.is_active is False and refused == "BadReference"
    assert reload(world, opp.pk).stage_id == stage(world.a).pk
    run(world, commands.update_stage, stage_id=target, is_active=True)
    moved, refused = race(world.a, world.ana, [enter, deactivate])  # el movimiento, primero
    assert moved.stage_id == target and refused == "OpenOpportunities"


def test_a_loss_and_a_win_wait_for_the_reason_and_the_opportunity_and_see_the_change(
    world: SimpleNamespace,
) -> None:
    opp, why = create(world), reason(world.a).pk
    off = partial(commands.update_lost_reason, lost_reason_id=why, version=1, is_active=False)
    lose_it = partial(stages.lose_opportunity, opportunity_id=opp.pk, version=1, lost_reason_id=why)
    assert race(world.a, world.ana, [off, lose_it])[1] == "BadReference"  # el motivo, ya inactivo
    drop = partial(opportunities.delete_opportunity, opportunity_id=opp.pk)
    win_it = partial(stages.win_opportunity, opportunity_id=opp.pk, version=1, amount="5")
    assert race(world.a, world.ana, [drop, win_it])[1] == "DoesNotExist"  # la oportunidad, borrada


def test_a_mixed_stress_leaves_only_allowed_errors_and_consistent_records(
    world: SimpleNamespace, migrator: DB
) -> None:
    allowed = {"StaleVersion", "OpportunityClosed", "NotClosed", "BadReference", "ClosingStage",
               "OpenOpportunities", "MissingFields"}  # fmt: skip
    pedro, pks = make_user(), [create(world).pk for _ in range(4)]
    join(world.a, pedro)
    staff, codes = [pedro.pk, world.ana.pk], ("nuevo", "contactado", "cotizado", "negociacion")
    ids, why, seen, lock = (
        {c: stage(world.a, c).pk for c in codes},
        reason(world.a).pk,
        [],
        threading.Lock(),
    )

    def worker(seed: int) -> None:
        rng = random.Random(seed)  # noqa: S311 — semilla fija: reparto reproducible
        try:
            for _ in range(60):
                pk = rng.choice(pks)
                at = {"opportunity_id": pk, "version": reload(world, pk).version}
                target = {"stage_id": ids[rng.choice(codes)]}
                toggle = {"stage_id": ids["negociacion"], "is_active": seed % 2 == 0}
                calls = (
                    (stages.move_opportunity, target),
                    (stages.win_opportunity, {"amount": str(rng.randint(1, 999))}),
                    (stages.lose_opportunity, {"lost_reason_id": why, "note": "no"}),
                    (stages.reopen_opportunity, target),
                    (opportunities.update_opportunity, {"title": f"T{rng.randint(1, 3)}"}),
                    (opportunities.assign_opportunity, {"assigned_user_id": rng.choice(staff)}),
                    (commands.update_stage, toggle),
                )  # fmt: skip
                command, kwargs = rng.choice(calls)
                try:
                    run(
                        world, command, **({} if command is commands.update_stage else at), **kwargs
                    )
                except Exception as error:  # noqa: BLE001 — se comprueba el tipo al final
                    with lock:
                        seen.append(type(error).__name__)
        finally:
            connection.close()

    pool = [threading.Thread(target=worker, args=(n,), daemon=True) for n in range(10)]
    for thread in pool:
        thread.start()
    for thread in pool:
        thread.join(timeout=300)
    assert not any(thread.is_alive() for thread in pool), "hilo bloqueado"
    assert not set(seen) - allowed, sorted(set(seen) - allowed)
    assert sum(reload(world, pk).version for pk in pks) > 4 + 50  # sí se escribió
    consistent(migrator)

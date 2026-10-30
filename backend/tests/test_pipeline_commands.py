"""F5-01: los comandos de pipelines, etapas y motivos de pérdida, con PostgreSQL real y el rol
`crm_app`, también con dos transacciones a la vez. Sin API: llega con F5-02."""

from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import psycopg
import pytest

from apps.deals import commands
from apps.deals.models import Pipeline, PipelineStage
from core.tenancy.scope import tenant_scope
from tests import test_lost_reasons
from tests.test_inventory_stock import race
from tests.test_lost_reasons import audit, count, run
from tests.test_memberships import ctx

world = test_lost_reasons.world  # el fixture de las dos organizaciones
pytestmark = pytest.mark.usefixtures("tenant_db")
BACKEND = Path(__file__).resolve().parent.parent


def spec(code: str, kind: str = "OPEN", **fields: Any) -> dict[str, Any]:
    probability = {"OPEN": 10, "WON": 100, "LOST": 0}[kind]
    return {
        "name": code.title(), "code": code, "stage_type": kind,
        "default_probability": probability, "color": "#112233", **fields,
    }  # fmt: skip


def basic() -> list[dict[str, Any]]:
    return [spec("nuevo"), spec("cotizado", stale_after_days=7), spec("ganado", "WON"),
            spec("perdido", "LOST")]  # fmt: skip


def new(world: SimpleNamespace, code: str = "retail", **fields: Any) -> Pipeline:
    wanted = {"name": code, "code": code, "kind": "RETAIL", "stages": basic(), **fields}
    made: Pipeline = run(world, commands.create_pipeline, **wanted)
    return made


def stage(pipeline: Pipeline, code: str) -> PipelineStage:
    with tenant_scope(ctx(pipeline.organization_id)):
        found: PipelineStage = PipelineStage.objects.get(pipeline_id=pipeline.pk, code=code)
    return found


def order(pipeline: Pipeline) -> list[tuple[str, int]]:
    with tenant_scope(ctx(pipeline.organization_id)):
        found = PipelineStage.objects.filter(pipeline_id=pipeline.pk).order_by("sort_order", "id")
        return list(found.values_list("code", "sort_order"))


def defaults(migrator: psycopg.Connection[Any]) -> list[str]:
    found = migrator.execute("SELECT code FROM pipelines WHERE is_default ORDER BY code")
    return [code for (code,) in found.fetchall()]


def test_create_stores_the_pipeline_with_its_stages_and_audits(
    world: SimpleNamespace, migrator: psycopg.Connection[Any]
) -> None:
    made = new(world, " Retail ", name="  Ventas   minoristas ")
    assert (made.code, made.name, made.version, made.is_default) == (
        "retail", "Ventas minoristas", 1, True,  # el primero de la organización
    )  # fmt: skip
    assert made.created_by_user_id == world.ana.pk
    assert order(made) == [("nuevo", 0), ("cotizado", 1), ("ganado", 2), ("perdido", 3)]
    assert stage(made, "cotizado").stale_after_days == 7
    assert stage(made, "nuevo").requires_fields == {}
    [(action, label, changes)] = audit(migrator)
    assert (action, label) == ("pipeline.created", "retail")
    assert changes["stages"] == [None, ["nuevo", "cotizado", "ganado", "perdido"]]
    assert changes["is_default"] == [None, True] and changes["kind"] == [None, "RETAIL"]


@pytest.mark.parametrize(
    "stages",
    [
        [spec("nuevo"), spec("perdido", "LOST")],  # sin WON
        [spec("nuevo"), spec("a", "WON"), spec("b", "WON"), spec("perdido", "LOST")],
        [spec("nuevo"), spec("ganado", "WON")],  # sin LOST
        [spec("ganado", "WON"), spec("perdido", "LOST")],  # sin OPEN
        [spec("nuevo"), spec("ganado", "WON"), spec("perdido", "LOST", is_active=False)],
        [spec("nuevo", is_active=False), spec("ganado", "WON"), spec("perdido", "LOST")],
        [spec("nuevo"), spec("ganado", "WON", is_active=False), spec("perdido", "LOST")],
        [],
    ],
)
def test_create_enforces_the_stage_invariants(
    world: SimpleNamespace, migrator: psycopg.Connection[Any], stages: Any
) -> None:
    with pytest.raises(commands.StageInvariant):
        new(world, stages=stages)
    assert count(migrator, "pipelines") == count(migrator, "pipeline_stages") == 0


@pytest.mark.parametrize(
    "change",
    [
        {"code": "Con Espacios"},
        {"code": "ñandú"},
        {"kind": "SHOP"},
        {"currency": "USD"},
        {"name": " - "},
        {"stages": [*basic(), spec("nuevo")]},  # código repetido
        {"stages": [*basic(), {**spec("x"), "stage_type": "DONE"}]},
        {
            "stages": [
                *basic()[:2],
                spec("ganado", "WON", default_probability=90),
                spec("p", "LOST"),
            ]
        },
        {"stages": [*basic(), spec("x", default_probability=101)]},
        {"stages": [*basic(), spec("x", default_probability=True)]},
        {"stages": [*basic(), spec("x", stale_after_days=0)]},
        {"stages": [*basic(), spec("x", color="red")]},
        {"stages": [*basic(), spec("x", requires_fields=[])]},
        {"stages": [*basic(), {**spec("x"), "pipeline_id": str(uuid4())}]},
        {"stages": [*basic(), {"name": "X", "code": "x"}]},  # faltan campos
    ],
)
def test_create_rejects_what_does_not_hold_and_writes_nothing(
    world: SimpleNamespace, migrator: psycopg.Connection[Any], change: dict[str, Any]
) -> None:
    with pytest.raises(ValueError):
        new(world, **change)
    assert count(migrator, "pipelines") == count(migrator, "pipeline_stages") == 0
    assert audit(migrator) == []


def test_a_taken_code_writes_nothing(
    world: SimpleNamespace, migrator: psycopg.Connection[Any]
) -> None:
    new(world)
    with pytest.raises(commands.CodeTaken):
        new(world, "retail")
    assert (count(migrator, "pipelines"), count(migrator, "pipeline_stages")) == (1, 4)
    assert len(audit(migrator)) == 1


def test_only_one_default_and_the_swap_is_audited(
    world: SimpleNamespace, migrator: psycopg.Connection[Any]
) -> None:
    first, second = new(world, "uno"), new(world, "dos")
    assert (first.is_default, second.is_default) == (True, False)
    third = new(world, "tres", is_default=True)  # quita la marca al anterior
    assert defaults(migrator) == ["tres"] and third.is_default
    run(world, commands.update_pipeline, pipeline_id=second.pk, version=1, is_default=True)
    assert defaults(migrator) == ["dos"]
    swaps = [(label, c["is_default"]) for a, label, c in audit(migrator) if a == "pipeline.updated"]
    assert swaps == [("uno", [True, False]), ("tres", [True, False]), ("dos", [False, True])]
    with tenant_scope(ctx(world.a)):  # el que perdió la marca subió de versión
        assert Pipeline.objects.get(code="tres").version == 2
    for change in ({"is_default": False}, {"is_active": False}):
        with pytest.raises(commands.PipelineIsDefault):
            run(world, commands.update_pipeline, pipeline_id=second.pk, version=2, **change)
    with pytest.raises(ValueError, match="inactivo"):
        run(world, commands.update_pipeline, pipeline_id=first.pk, version=2, is_active=False,
            is_default=True)  # fmt: skip
    assert defaults(migrator) == ["dos"]


def test_update_checks_the_version_and_audits_the_diff(
    world: SimpleNamespace, migrator: psycopg.Connection[Any]
) -> None:
    new(world, "uno")
    other = new(world, "dos")
    same = run(
        world, commands.update_pipeline, pipeline_id=other.pk, version=1, name="dos", sort_order=0
    )
    assert same.version == 1  # nada cambia: ni escribe ni audita
    edit = {"name": " Mayoristas ", "kind": "WHOLESALE", "sort_order": 3, "is_active": False}
    changed = run(world, commands.update_pipeline, pipeline_id=other.pk, version=1, **edit)
    assert (changed.version, changed.is_active, changed.updated_by_user_id) == (
        2,
        False,
        world.ana.pk,
    )
    with pytest.raises(commands.StaleVersion):  # otra pestaña editaba la versión 1
        run(world, commands.update_pipeline, pipeline_id=other.pk, version=1, name="Pisado")
    for field in ("code", "currency", "organization_id"):
        with pytest.raises(ValueError, match="no admitidos"):
            run(world, commands.update_pipeline, pipeline_id=other.pk, version=2, **{field: "x"})
    rows = audit(migrator)
    assert [a for a, _, _ in rows] == ["pipeline.created", "pipeline.created", "pipeline.updated"]
    assert rows[2][2] == {
        "name": ["dos", "Mayoristas"], "kind": ["RETAIL", "WHOLESALE"], "sort_order": [0, 3],
        "is_active": [True, False],
    }  # fmt: skip


def test_stages_are_added_in_place_and_audited(
    world: SimpleNamespace, migrator: psycopg.Connection[Any]
) -> None:
    made = new(world)

    def add(**fields: Any) -> Any:
        return run(world, commands.add_stage, pipeline_id=made.pk, **fields)

    add(**spec("contactado"))  # una OPEN va tras la última OPEN
    assert [c for c, _ in order(made)] == ["nuevo", "cotizado", "contactado", "ganado", "perdido"]
    add(position=0, **spec("lead"))
    add(**spec("revisada", "LOST"))  # otra LOST, al final
    assert order(made) == [(c, i) for i, c in enumerate(
        ["lead", "nuevo", "cotizado", "contactado", "ganado", "perdido", "revisada"])]  # fmt: skip
    for bad in (spec("segunda", "WON"), spec("lead"), {**spec("x"), "organization_id": 1}):
        with pytest.raises((commands.StageInvariant, commands.CodeTaken, ValueError)):
            add(**bad)
    with pytest.raises(ValueError, match="posición"):
        add(position=99, **spec("x"))
    assert len(order(made)) == 7
    added = [(a, label) for a, label, _ in audit(migrator) if a == "stage.added"]
    assert added == [
        ("stage.added", "contactado"),
        ("stage.added", "lead"),
        ("stage.added", "revisada"),
    ]
    last = audit(migrator)[-1][2]
    assert last["sort_order"] == [None, 6] and last["pipeline_id"] == [None, str(made.pk)]
    with pytest.raises(Pipeline.DoesNotExist):  # un pipeline de otra organización
        run(world, commands.add_stage, org="b", pipeline_id=made.pk, **spec("x"))


def test_update_stage_keeps_the_invariants_after_each_change(
    world: SimpleNamespace, migrator: psycopg.Connection[Any]
) -> None:
    made = new(world)

    def up(code: str, **fields: Any) -> Any:
        return run(world, commands.update_stage, stage_id=stage(made, code).pk, **fields)

    up("nuevo", name="Nuevo", default_probability=10)  # nada cambia: ni escribe ni audita
    assert len(audit(migrator)) == 1
    up("cotizado", name=" Cotización ", default_probability=45, stale_after_days=None,
       color="#AABBCC", requires_fields={"quote": True})  # fmt: skip
    got = stage(made, "cotizado")
    assert (got.name, got.default_probability, got.stale_after_days, got.color) == (
        "Cotización", 45, None, "#aabbcc",
    )  # fmt: skip
    assert audit(migrator)[-1][0:2] == ("stage.updated", "cotizado")
    assert audit(migrator)[-1][2]["stale_after_days"] == [7, None]
    for code, change in (
        ("ganado", {"is_active": False}),  # la WON no se va
        ("ganado", {"stage_type": "OPEN", "default_probability": 10}),  # ni se convierte
        ("perdido", {"is_active": False}),  # ni la última LOST
        ("perdido", {"stage_type": "OPEN", "default_probability": 10}),
        ("nuevo", {"stage_type": "WON", "default_probability": 100}),  # una segunda WON
    ):
        with pytest.raises(commands.StageInvariant):
            up(code, **change)
    up("cotizado", stage_type="LOST", default_probability=0)  # queda otra OPEN (nuevo)
    up("perdido", stage_type="OPEN", default_probability=5)  # y ahora hay otra LOST
    with pytest.raises(ValueError, match="probabilidad 0"):
        up("nuevo", stage_type="LOST")  # una LOST tiene 0 %, no el 10 % de antes
    with pytest.raises(commands.StageInvariant):
        up("cotizado", is_active=False)  # la última LOST
    up("nuevo", is_active=False)
    with pytest.raises(commands.StageInvariant):
        up("perdido", is_active=False)  # la última OPEN activa
    assert [stage(made, c).is_active for c in ("nuevo", "perdido")] == [False, True]


def test_reorder_rewrites_only_what_changes_and_audits(
    world: SimpleNamespace, migrator: psycopg.Connection[Any]
) -> None:
    made = new(world)
    ids = {code: stage(made, code).pk for code, _ in order(made)}
    wanted = [ids[c] for c in ("cotizado", "nuevo", "perdido", "ganado")]
    done = run(world, commands.reorder_stages, pipeline_id=made.pk, stage_ids=wanted)
    assert [s.code for s in done] == ["cotizado", "nuevo", "perdido", "ganado"]
    assert order(made) == [("cotizado", 0), ("nuevo", 1), ("perdido", 2), ("ganado", 3)]
    run(world, commands.reorder_stages, pipeline_id=made.pk, stage_ids=wanted)  # ya está así
    [(_, label, changes)] = [r for r in audit(migrator) if r[0] == "stage.reordered"]
    assert label == "retail"
    assert changes == {"order": [["nuevo", "cotizado", "ganado", "perdido"],
                                 ["cotizado", "nuevo", "perdido", "ganado"]]}  # fmt: skip
    for bad in (wanted[:3], [*wanted, wanted[0]], [*wanted[:3], uuid4()], []):
        with pytest.raises(ValueError, match="todas las etapas"):
            run(world, commands.reorder_stages, pipeline_id=made.pk, stage_ids=bad)
    assert order(made)[0] == ("cotizado", 0) and len(audit(migrator)) == 2


def test_what_belongs_to_a_cannot_be_touched_from_b(world: SimpleNamespace) -> None:
    made = new(world)
    mine = stage(made, "nuevo")
    with pytest.raises(PipelineStage.DoesNotExist):
        run(world, commands.update_stage, org="b", stage_id=mine.pk, name="Mío")
    with pytest.raises(Pipeline.DoesNotExist):
        run(world, commands.update_pipeline, org="b", pipeline_id=made.pk, version=1, name="Mío")
    with pytest.raises(Pipeline.DoesNotExist):
        run(world, commands.reorder_stages, org="b", pipeline_id=made.pk, stage_ids=[mine.pk])
    assert run(world, commands.create_pipeline, org="b", name="B", code="retail", kind="OTHER",
               stages=basic()).is_default  # el código `retail` es libre en B  # fmt: skip


def test_two_defaults_at_once_leave_one(
    world: SimpleNamespace, migrator: psycopg.Connection[Any]
) -> None:
    def create(code: str) -> Callable[[Any], Pipeline]:
        return lambda tenant: commands.create_pipeline(
            tenant, name=code, code=code, kind="RETAIL", stages=basic(), is_default=True
        )

    first, second = race(world.a, world.ana, [create("uno"), create("dos")])
    assert isinstance(first, Pipeline) and isinstance(second, Pipeline)
    assert defaults(migrator) == ["dos"]  # el segundo esperó al primero y le quitó la marca
    assert count(migrator, "pipelines") == 2


def test_two_reorders_at_once_apply_one_after_the_other(world: SimpleNamespace) -> None:
    made = new(world)
    ids = {code: stage(made, code).pk for code, _ in order(made)}

    def reorder(codes: list[str]) -> Callable[[Any], Any]:
        return lambda tenant: commands.reorder_stages(
            tenant, pipeline_id=made.pk, stage_ids=[ids[c] for c in codes]
        )

    first = ["perdido", "ganado", "cotizado", "nuevo"]
    second = ["ganado", "nuevo", "perdido", "cotizado"]
    race(world.a, world.ana, [reorder(first), reorder(second)])
    assert order(made) == [(c, i) for i, c in enumerate(second)]  # no una mezcla de las dos


def test_two_deactivations_at_once_keep_an_open_stage(world: SimpleNamespace) -> None:
    """Sin el bloqueo del pipeline, cada una vería que la otra OPEN sigue activa."""
    made = new(world)
    ids = [stage(made, code).pk for code in ("nuevo", "cotizado")]

    def deactivate(stage_id: Any) -> Callable[[Any], Any]:
        return lambda tenant: commands.update_stage(tenant, stage_id=stage_id, is_active=False)

    first, second = race(world.a, world.ana, [deactivate(ids[0]), deactivate(ids[1])])
    assert isinstance(first, PipelineStage) and second == "StageInvariant"
    assert [stage(made, c).is_active for c in ("nuevo", "cotizado")] == [False, True]

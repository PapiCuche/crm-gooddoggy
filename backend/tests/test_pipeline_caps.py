"""F5-01d: los topes de tamaño de los comandos de pipelines, sin la API delante: `requires_fields`
(4096 bytes serializado, 4 niveles, sin NaN, infinitos ni NUL) y 30 etapas por pipeline. Lo que se
rechaza no escribe ni audita. Con PostgreSQL real y el rol `crm_app`."""

import json
import math
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import psycopg
import pytest

from apps.deals import commands
from apps.deals.api import inputs
from apps.deals.models import PipelineStage
from tests import test_contacts_api, test_lost_reasons, test_self_context
from tests.test_inventory_stock import race
from tests.test_lost_reasons import audit, count, run
from tests.test_pipeline_commands import basic, new, order, spec, stage
from tests.test_pipelines_api import pipeline
from tests.test_pipelines_stages_api import at
from tests.test_pipelines_update_api import snapshot
from tests.test_products import send
from tests.test_self_context import signed

world, crm = test_lost_reasons.world, test_contacts_api.crm
real_stack = test_self_context.real_stack  # la ruta, con el middleware de verdad
pytestmark = pytest.mark.usefixtures("tenant_db")


def circular() -> dict[str, Any]:
    loop: dict[str, Any] = {}
    loop["a"] = loop
    return loop


FITS = [
    {"k": "x" * 4088},  # 4096 bytes exactos: `{"k":"` y `"}` son 8
    {"k": "ñ" * 2044},  # 4096 bytes en 2052 caracteres
    {"a": {"b": [{"c": 1, "d": None, "e": True, "f": 2.5, "g": "ñ"}]}},  # 4 niveles
    {"a": [[[1]]]},  # 4 niveles de listas
    {},
]
TOO_BIG = [
    {"k": "x" * 4089},  # 4097 bytes
    {"k": "ñ" * 2045},  # 4098 bytes en solo 2053 caracteres: se cuentan bytes
    {"k": "x" * 5 * 1024 * 1024},  # 5 MB
    {f"k{i}": i for i in range(1000)},
]
WRONG_SHAPE = [
    {"a": {"b": {"c": {"d": {}}}}},  # 5 niveles
    {"a": [[[[1]]]]},
    {"a": math.nan}, {"a": math.inf}, {"a": [-math.inf]},
    {"a": "x\x00"}, {"a\x00": 1}, {"a": {"b\x00": 1}},  # PostgreSQL no guarda NUL
    {1: "a"}, {"a": (1, 2)}, {"a": {1, 2}}, {"a": b"x"}, {"a": uuid4()},
    circular(),
    [], None, "{}", 5, (),
]  # fmt: skip
SURROGATE = {"a": "\ud800"}  # ni UTF-8 ni `jsonb` lo admiten
HUGE_INT = {"a": 10**4300}  # `json.dumps` no escribe un entero de más de 4300 cifras


def refused(value: Any) -> str:
    """El texto con que el comando rechaza `value` en `requires_fields`."""
    with pytest.raises(commands.Invalid) as caught:
        commands.requires_fields(value)
    assert caught.value.field == "requires_fields"
    return str(caught.value)


def test_requires_fields_is_bounded_in_bytes_depth_and_shape() -> None:
    for value in FITS:
        assert commands.requires_fields(value) is value
        size = len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode())
        assert size <= 4096
    for value in TOO_BIG:
        assert refused(value) == "hasta 4096 bytes serializado"
    for value in WRONG_SHAPE:
        assert refused(value) == "un objeto JSON de hasta 4 niveles, sin NaN, infinitos ni NUL"
    for value in (SURROGATE, HUGE_INT):
        assert refused(value) == "un objeto JSON que se pueda escribir en UTF-8"
    assert refused({"a": [0] * 4095}) == "hasta 4096 bytes serializado"  # 4097 elementos
    assert commands.requires_fields({"a": [0] * 2044}) == {"a": [0] * 2044}  # 4095 bytes
    assert (commands.FIELDS_BYTES, commands.FIELDS_DEPTH, commands.STAGES_MAX) == (4096, 4, 30)
    assert (inputs.FIELDS_BYTES, inputs.FIELDS_DEPTH, inputs.STAGES_MAX) == (4096, 4, 30)


def test_every_stage_command_applies_the_cap_and_writes_nothing(
    world: SimpleNamespace, migrator: psycopg.Connection[Any]
) -> None:
    made = new(world)
    nuevo = stage(made, "nuevo").pk
    before = (count(migrator, "pipelines"), count(migrator, "pipeline_stages"), audit(migrator))
    for value in [*TOO_BIG, *WRONG_SHAPE[:3], SURROGATE, HUGE_INT]:
        attempts: list[tuple[Any, dict[str, Any]]] = [
            (commands.create_pipeline, {
                "name": "Otro", "code": "otro", "kind": "RETAIL",
                "stages": [spec("a", requires_fields=value), *basic()],
            }),
            (commands.add_stage, {"pipeline_id": made.pk, **spec("x", requires_fields=value)}),
            (commands.update_stage, {"stage_id": nuevo, "requires_fields": value}),
        ]  # fmt: skip
        for command, fields in attempts:
            with pytest.raises(commands.Invalid) as caught:
                run(world, command, **fields)
            assert caught.value.field == "requires_fields", command
    after = (count(migrator, "pipelines"), count(migrator, "pipeline_stages"), audit(migrator))
    assert after == before
    edge = {"k": "x" * 4088}
    run(world, commands.update_stage, stage_id=nuevo, requires_fields=edge)
    assert stage(made, "nuevo").requires_fields == edge
    assert audit(migrator)[-1][2]["requires_fields"] == [{}, edge]


@pytest.mark.timeout(10)
def test_a_list_repeated_by_reference_is_refused_without_walking_it() -> None:
    row = [0] * 1000
    shared = {"a": [[row] * 1000] * 1000}  # mil millones de elementos, pocos objetos
    assert refused(shared) == "hasta 4096 bytes serializado"
    leaf = {str(i): 0 for i in range(1000)}
    mid = {str(i): leaf for i in range(1000)}  # también con objetos: un solo presupuesto
    assert refused({"a": {str(i): mid for i in range(1000)}}) == "hasta 4096 bytes serializado"


def thirty(world: SimpleNamespace, code: str = "treinta", inactive: int = 0) -> Any:
    """Un pipeline de exactamente 30 etapas; las `inactive` primeras, inactivas."""
    many = [spec(f"s{i}", is_active=i >= inactive) for i in range(28)]
    return new(world, code, stages=[*many, spec("ganado", "WON"), spec("perdido", "LOST")])


def test_a_pipeline_is_created_with_at_most_thirty_stages(
    world: SimpleNamespace, migrator: psycopg.Connection[Any]
) -> None:
    for stages in (
        [spec(f"s{i}") for i in range(29)] + [spec("ganado", "WON"), spec("perdido", "LOST")],
        [spec(f"s{i}") for i in range(2000)],
        [None] * 31,  # se cuentan antes de mirar cada una
    ):
        with pytest.raises(commands.Invalid) as caught:
            new(world, stages=stages)
        assert (caught.value.field, str(caught.value)) == ("stages", "hasta 30 etapas por pipeline")
    assert count(migrator, "pipelines") == count(migrator, "pipeline_stages") == 0
    assert audit(migrator) == []
    made = thirty(world)
    assert len(order(made)) == 30
    [(_, _, changes)] = audit(migrator)
    assert len(changes["stages"][1]) == 30


def test_a_stage_is_not_added_to_a_pipeline_with_thirty_inactive_ones_included(
    world: SimpleNamespace, migrator: psycopg.Connection[Any]
) -> None:
    roomy = new(world, "holgado")
    for index in range(26):  # el básico ya lleva cuatro
        run(world, commands.add_stage, pipeline_id=roomy.pk, **spec(f"e{index}"))
    assert len(order(roomy)) == 30
    full = thirty(world, inactive=5)
    before = (count(migrator, "pipeline_stages"), audit(migrator))
    for pipeline_id in (roomy.pk, full.pk):
        for extra in (spec("extra"), spec("segunda", "WON"), spec("s1")):  # antes del resto
            with pytest.raises(commands.Invalid) as caught:
                run(world, commands.add_stage, pipeline_id=pipeline_id, **extra)
            assert (caught.value.field, str(caught.value)) == (
                "_",
                "el pipeline ya tiene 30 etapas",
            )
    assert (count(migrator, "pipeline_stages"), audit(migrator)) == before


def test_a_reorder_takes_at_most_thirty_ids_and_its_audit_stays_bounded(
    world: SimpleNamespace, migrator: psycopg.Connection[Any]
) -> None:
    made = thirty(world)
    ids = [stage(made, code).pk for code, _ in order(made)]
    for wanted in ([*ids, uuid4()], ["no-es-un-uuid"] * 31, [uuid4()] * 2000):
        with pytest.raises(commands.Invalid) as caught:
            run(world, commands.reorder_stages, pipeline_id=made.pk, stage_ids=wanted)
        assert (caught.value.field, str(caught.value)) == ("stage_ids", "hasta 30 etapas")
    with pytest.raises(commands.Invalid, match="hasta 30"):  # antes de buscar el pipeline
        run(world, commands.reorder_stages, pipeline_id=uuid4(), stage_ids=ids * 2)
    assert len(audit(migrator)) == 1
    run(world, commands.reorder_stages, pipeline_id=made.pk, stage_ids=ids[::-1])
    assert [code for code, _ in order(made)][0] == "perdido"
    (_, _, changes) = audit(migrator)[-1]
    assert [len(codes) for codes in changes["order"]] == [30, 30]
    assert len(json.dumps(changes)) < 30 * 2 * (commands.CODE_MAX + 4) + 20


def test_the_route_words_a_bad_requires_fields_as_the_command_does(
    crm: SimpleNamespace, migrator: psycopg.Connection[Any]
) -> None:
    ana, made = signed(crm.ana), pipeline(crm)
    path = at(made.pk, f"stages/{stage(made, 'nuevo').pk}/")
    before = snapshot(migrator)
    for value, message in (
        ({"k": "x" * 4089}, "Hasta 4096 bytes serializado."),
        ({"a": [[[[1]]]]}, "Un objeto JSON de hasta 4 niveles, sin NaN, infinitos ni NUL."),
    ):
        response = send(ana, "patch", path, {"requires_fields": value})
        assert response.status_code == 400, response.content
        assert response.json()["fields"] == {
            "requires_fields": [{"code": "invalid", "message": message}]
        }
    assert snapshot(migrator) == before


def test_two_additions_at_once_never_pass_the_command_cap(world: SimpleNamespace) -> None:
    made = new(world, "holgado", stages=[*[spec(f"s{i}") for i in range(27)], *basic()[2:]])
    assert len(order(made)) == 29

    def adding(code: str) -> Any:
        return lambda tenant: commands.add_stage(tenant, pipeline_id=made.pk, **spec(code))

    first, second = race(world.a, world.ana, [adding("a"), adding("b")])
    assert isinstance(first, PipelineStage) and second == "Invalid"
    assert len(order(made)) == 30

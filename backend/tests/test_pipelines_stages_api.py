"""F5-02e: añadir etapas y ordenarlas por HTTP (`POST …/pipelines/{id}/stages/` y
`PUT …/pipelines/{id}/stages/order/`). Middleware, sesión, motor de autorización y PostgreSQL con el
rol `crm_app`; los comandos se prueban en `test_pipeline_commands`."""

from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from apps.deals.api import inputs
from tests import test_contacts_api, test_self_context
from tests.test_authorization import give
from tests.test_member_branch import DENIED
from tests.test_pipeline_commands import basic, spec, stage
from tests.test_pipelines_api import PIPELINE_FIELDS, PIPELINES, pipeline, read
from tests.test_pipelines_update_api import logged, snapshot
from tests.test_pipelines_write_api import CSRF_FAILED, administrator, fail, http_race
from tests.test_products import send
from tests.test_self_context import NOT_FOUND, reply, signed

crm, real_stack = test_contacts_api.crm, test_self_context.real_stack
pytestmark = pytest.mark.usefixtures("tenant_db")


def at(pipeline_id: Any, tail: str = "stages/", org: str = "org-a") -> str:
    return f"/api/v1/o/{org}/pipelines/{pipeline_id}/{tail}"


def add(client: Any, pipeline_id: Any, **fields: Any) -> Any:
    return send(client, "post", at(pipeline_id), spec("propuesta") | fields)


def order(client: Any, pipeline_id: Any, ids: Any) -> Any:
    return send(client, "put", at(pipeline_id, "stages/order/"), {"stage_ids": ids})


def codes(sent: Any) -> list[str]:
    return [s["code"] for s in sent["stages"]]


def test_a_stage_is_added_where_asked_and_the_answer_is_the_pipeline(
    crm: SimpleNamespace, migrator: psycopg.Connection[Any]
) -> None:
    ana, made = signed(crm.ana), pipeline(crm)
    first = add(ana, made.pk, name="  Propuesta  enviada ", color="#ABCDEF", stale_after_days=3)
    assert first.status_code == 201, first.content
    got = first.json()
    assert set(got) == PIPELINE_FIELDS and got["version"] == 1
    assert codes(got) == ["nuevo", "cotizado", "propuesta", "ganado", "perdido"]  # tras la OPEN
    new = got["stages"][2]
    assert [new[k] for k in ("name", "color", "stale_after_days", "sort_order")] == [
        "Propuesta enviada", "#abcdef", 3, 2,
    ]  # fmt: skip
    assert [s["sort_order"] for s in got["stages"]] == [0, 1, 2, 3, 4]  # renumeradas
    assert read(ana, PIPELINES)["results"] == [got]
    placed = add(ana, made.pk, **{"code": "primera", "position": 0}).json()
    assert codes(placed)[0] == "primera"
    closing = add(ana, made.pk, **{"code": "tarde", "stage_type": "LOST", "default_probability": 0})
    assert codes(closing.json())[-1] == "tarde"  # una LOST va al final
    rows = logged(migrator, "stage")
    assert [(action, actor) for action, actor, _ in rows] == [("stage.added", crm.ana.pk)] * 3
    assert rows[0][2]["pipeline_id"][1] == str(made.pk)


def test_who_may_add_and_order_and_what_is_the_other_organization(
    crm: SimpleNamespace, migrator: psycopg.Connection[Any]
) -> None:
    made, theirs = pipeline(crm), pipeline(crm, "b", code="solo-b")
    gone = pipeline(crm, code="borrable", is_default=False)
    migrator.execute("UPDATE pipelines SET deleted_at = now() WHERE id = %s", [gone.pk])
    ids = [str(stage(made, c).pk) for c in ("nuevo", "cotizado", "ganado", "perdido")]
    give(crm.a, crm.m["bea"], {"pipelines.manage": None})  # solo administrar, sin ver
    for who, user in {"ana": crm.ana, "admin": administrator(crm), "bea": crm.bea}.items():
        assert add(signed(user), made.pk, code=who).status_code == 201, who
        assert order(signed(user), made.pk, ids[::-1]).status_code == 400  # ahora hay más
    before = snapshot(migrator)
    with CaptureQueriesContext(connection) as captured:
        for user in (crm.vera, crm.sam):  # ver oportunidades no es administrarlas
            denied = (add(signed(user), made.pk), order(signed(user), made.pk, ids))
            assert [reply(r) for r in denied] == [DENIED, DENIED], user.email
    assert not [q for q in captured.captured_queries if "pipeline" in q["sql"]]  # antes de leer
    anonymous = signed(crm.ana)
    anonymous.cookies.clear()
    expected = (401, b'{"code":"NOT_AUTHENTICATED"}')
    assert reply(add(anonymous, made.pk)) == expected == reply(order(anonymous, made.pk, ids))
    ana = signed(crm.ana)  # con sesión, sin la cabecera CSRF
    assert reply(ana.post(at(made.pk), spec("x"), "application/json")) == CSRF_FAILED
    assert reply(ana.put(at(made.pk, "stages/order/"), {"stage_ids": ids})) == CSRF_FAILED
    give(crm.b, crm.m_luis, {"pipelines.manage": None})
    luis = signed(crm.luis)
    assert reply(add(luis, made.pk)) == reply(order(luis, made.pk, ids)) == NOT_FOUND
    for missing in (made.pk, theirs.pk, gone.pk, uuid4()):  # el de A en B, de B en A, borrado
        for path in ("stages/", "stages/order/"):
            tail = send(ana, "post" if path == "stages/" else "put", at(missing, path, "org-b"), {})
            assert reply(tail) == NOT_FOUND
    for pipeline_id in (theirs.pk, gone.pk, uuid4()):
        assert reply(add(ana, pipeline_id)) == reply(order(ana, pipeline_id, ids)) == NOT_FOUND
    assert snapshot(migrator) == before


def test_the_invariants_the_code_and_the_position_come_back_as_stable_errors(
    crm: SimpleNamespace, migrator: psycopg.Connection[Any]
) -> None:
    ana, made, other = signed(crm.ana), pipeline(crm), pipeline(crm, code="otro", is_default=False)
    before = snapshot(migrator)
    second_won = add(ana, made.pk, code="otra-won", stage_type="WON", default_probability=100)
    assert (second_won.status_code, second_won.json()["code"]) == (409, "PIPELINE_INVARIANT")
    for code in ("nuevo", "NUEVO"):
        taken = add(ana, made.pk, code=code)
        assert (taken.status_code, taken.json()["code"]) == (409, "CODE_TAKEN"), code
    closed = add(ana, made.pk, code="cerrada", stage_type="LOST", default_probability=5)
    assert fail(closed) == (400, "VALIDATION_ERROR", ["default_probability"])  # LOST es 0 %
    beyond = add(ana, made.pk, code="lejos", position=5)  # hay cuatro: de 0 a 4
    assert fail(beyond) == (400, "VALIDATION_ERROR", ["position"])
    assert snapshot(migrator) == before
    assert add(ana, made.pk, code="extra").status_code == 201
    assert add(ana, other.pk, code="extra").status_code == 201  # el código es del pipeline
    assert add(ana, made.pk, code="lejos", position=4).status_code == 201


BAD = [  # (lo que se cambia de la etapa válida, el campo del 400)
    ({"name": 5}, "name"), ({"name": ""}, "name"), ({"name": "a\x00b"}, "name"),
    ({"name": "a‮b"}, "name"), ({"name": "x" * 101}, "name"), ({"code": 7}, "code"),
    ({"code": "A B"}, "code"), ({"code": "x" * 31}, "code"), ({"stage_type": "WIN"}, "stage_type"),
    ({"default_probability": "10"}, "default_probability"),
    ({"default_probability": True}, "default_probability"),
    ({"default_probability": 101}, "default_probability"),
    ({"stale_after_days": 0}, "stale_after_days"), ({"stale_after_days": "7"}, "stale_after_days"),
    ({"color": "red"}, "color"), ({"is_active": "true"}, "is_active"),
    ({"requires_fields": []}, "requires_fields"),
    ({"requires_fields": {"k\x00": 1}}, "requires_fields"),
    ({"requires_fields": {"a": {"b": {"c": {"d": {}}}}}}, "requires_fields"),
    ({"requires_fields": {"k": "x" * 4096}}, "requires_fields"),
    ({"position": "1"}, "position"), ({"position": True}, "position"),
    ({"position": -1}, "position"), ({"position": 31}, "position"), ({"foo": 1}, "foo"),
    ({"sort_order": 1}, "sort_order"), ({"pipeline_id": str(uuid4())}, "pipeline_id"),
]  # fmt: skip


@pytest.mark.parametrize(("changed", "field"), BAD)
def test_a_bad_stage_value_is_a_400_on_its_field_and_writes_nothing(
    crm: SimpleNamespace, migrator: psycopg.Connection[Any], changed: dict[str, Any], field: str
) -> None:
    made = pipeline(crm)
    before = snapshot(migrator)
    response = send(signed(crm.ana), "post", at(made.pk), spec("propuesta") | changed)
    assert fail(response) == (400, "VALIDATION_ERROR", [field]), response.content
    assert snapshot(migrator) == before
    for echoed in ("\x00", "‮"):  # el texto del error no repite lo recibido
        assert echoed not in response.content.decode()
    for missing in ("name", "code", "stage_type", "default_probability", "color"):
        sent = {k: v for k, v in spec("propuesta").items() if k != missing}
        incomplete = send(signed(crm.ana), "post", at(made.pk), sent)
        assert fail(incomplete) == (400, "VALIDATION_ERROR", [missing]), missing


def test_at_most_thirty_stages_and_the_cap_counts_the_inactive_ones(
    crm: SimpleNamespace, migrator: psycopg.Connection[Any]
) -> None:
    ana = signed(crm.ana)
    many = [spec(f"s{i}", is_active=i != 0) for i in range(inputs.STAGES_MAX - 3)]
    full = pipeline(crm, stages=[*many, spec("sin-ganar", "OPEN"), *basic()[2:]])
    assert len(read(ana, PIPELINES)["results"][0]["stages"]) == inputs.STAGES_MAX
    before = snapshot(migrator)
    over = add(ana, full.pk, code="treinta-y-una")
    assert fail(over) == (400, "VALIDATION_ERROR", ["_"]), over.content
    assert snapshot(migrator) == before
    roomy = pipeline(crm, code="holgado", is_default=False)
    for index in range(inputs.STAGES_MAX - 4):  # el básico ya lleva cuatro
        assert add(ana, roomy.pk, code=f"e{index}").status_code == 201, index
    assert add(ana, roomy.pk, code="ultima").status_code == 400  # exactamente 30 caben, no 31


def test_the_order_is_a_full_list_of_this_pipelines_stages(
    crm: SimpleNamespace, migrator: psycopg.Connection[Any]
) -> None:
    ana, made = signed(crm.ana), pipeline(crm)
    other, theirs = pipeline(crm, code="otro", is_default=False), pipeline(crm, "b", code="solo-b")
    ids = {c: str(stage(made, c).pk) for c in ("nuevo", "cotizado", "ganado", "perdido")}
    wanted = [ids[c] for c in ("cotizado", "nuevo", "perdido", "ganado")]
    done = order(ana, made.pk, wanted)
    assert done.status_code == 200, done.content
    got = done.json()
    assert set(got) == PIPELINE_FIELDS and codes(got) == ["cotizado", "nuevo", "perdido", "ganado"]
    assert [s["sort_order"] for s in got["stages"]] == [0, 1, 2, 3]
    assert read(ana, PIPELINES)["results"][0] == got
    [(action, actor, changes)] = logged(migrator, "pipeline")[-1:]
    assert (action, actor) == ("stage.reordered", crm.ana.pk)
    assert changes["order"][1] == ["cotizado", "nuevo", "perdido", "ganado"]
    before = snapshot(migrator)
    assert order(ana, made.pk, wanted).status_code == 200  # ya en ese orden: ni escribe ni audita
    assert snapshot(migrator) == before
    foreign = [str(stage(other, "nuevo").pk), str(stage(theirs, "nuevo").pk)]
    wrong = [
        wanted[:-1], [*wanted, wanted[0]], [*wanted[:-1], wanted[0]], [*wanted[:-1], str(uuid4())],
        [*wanted[:-1], foreign[0]], [*wanted[:-1], foreign[1]], [], ["x"], [1, 2, 3, 4], [None],
        [{"a": 1}], wanted * 8,
    ]  # fmt: skip
    for sent in wrong:
        assert fail(order(ana, made.pk, sent)) == (400, "VALIDATION_ERROR", ["stage_ids"]), sent
    for body in ({}, {"stage_ids": "x"}, {"stage_ids": wanted, "x": 1}, {"ids": wanted}, wanted):
        assert fail(send(ana, "put", at(made.pk, "stages/order/"), body))[1] == "VALIDATION_ERROR"
    assert snapshot(migrator) == before


HOSTILE = [  # cuerpos que ningún serializador espera: 400 o 415, nunca un 500
    "null", "[]", '"x"', "1", "{", "", '{"name": "' + "x" * 200_000 + '"}',
    "[" * 200 + "]" * 200, '{"stage_ids":' + "[" * 120 + "]" * 120 + "}", '{"position":NaN}',
]  # fmt: skip


@pytest.mark.parametrize("raw", HOSTILE)
def test_hostile_bodies_are_never_a_500(crm: SimpleNamespace, raw: str) -> None:
    made, ana = pipeline(crm), signed(crm.ana)
    for method, tail in (("post", "stages/"), ("put", "stages/order/")):
        response = send(ana, method, at(made.pk, tail), raw)
        assert response.status_code in {400, 415}, (method, response.status_code)
        assert response.json()["code"] in {"VALIDATION_ERROR", "PARSE_ERROR"}


def test_two_additions_at_once_never_pass_the_cap_or_repeat_a_code(
    crm: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    ana = signed(crm.ana)
    made = pipeline(crm, stages=[*[spec(f"s{i}") for i in range(26)], *basic()[2:]])  # 28 etapas
    assert add(ana, made.pk, code="s28").status_code == 201  # 29: cabe una más
    paths, sent = [at(made.pk)] * 2, [spec("a"), spec("b")]
    capped = http_race(monkeypatch, sent, "post", paths)
    assert capped == [(201, "retail"), (400, "VALIDATION_ERROR")]
    gap = pipeline(crm, code="otro", is_default=False)
    same = http_race(monkeypatch, [spec("igual")] * 2, "post", [at(gap.pk)] * 2)
    assert same == [(201, "otro"), (409, "CODE_TAKEN")]

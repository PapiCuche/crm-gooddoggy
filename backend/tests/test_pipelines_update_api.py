"""F5-02d: cambiar pipelines y etapas por HTTP (`PATCH …/pipelines/{id}/` y
`PATCH …/pipelines/{id}/stages/{stage_id}/`). Middleware, sesión, motor de autorización y
PostgreSQL con el rol `crm_app`; los comandos se prueban en `test_pipeline_commands`."""

from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from tests import test_contacts_api, test_opportunities_tables, test_self_context
from tests.test_authorization import give
from tests.test_member_branch import DENIED
from tests.test_pipeline_commands import basic, spec, stage
from tests.test_pipelines_api import PIPELINE_FIELDS, PIPELINES, STAGE_FIELDS, pipeline, read
from tests.test_pipelines_write_api import CSRF_FAILED, administrator, fail, http_race
from tests.test_products import send
from tests.test_self_context import NOT_FOUND, reply, signed

crm, real_stack = test_contacts_api.crm, test_self_context.real_stack
pytestmark = pytest.mark.usefixtures("tenant_db")
TWO_OPEN = [spec("nuevo"), spec("contactado"), spec("ganado", "WON"), spec("perdido", "LOST")]


def at(pipeline_id: Any, stage_id: Any = None, org: str = "org-a") -> str:
    tail = "" if stage_id is None else f"stages/{stage_id}/"
    return f"/api/v1/o/{org}/pipelines/{pipeline_id}/{tail}"


def change(client: Any, pipeline_id: Any, version: Any = 1, **fields: Any) -> Any:
    return send(client, "patch", at(pipeline_id), {"version": version, **fields})


def edit(client: Any, made: Any, stage_code: str, /, **fields: Any) -> Any:
    return send(client, "patch", at(made.pk, stage(made, stage_code).pk), fields)


def logged(migrator: psycopg.Connection[Any], kind: str = "pipeline") -> list[Any]:
    found = migrator.execute(
        "SELECT action, actor_id, changes FROM audit_logs WHERE entity_type = %s "
        "ORDER BY occurred_at, id",
        [kind],
    )
    return found.fetchall()


def snapshot(migrator: psycopg.Connection[Any]) -> tuple[Any, ...]:
    pipelines = migrator.execute("SELECT id, version, name, is_default FROM pipelines ORDER BY id")
    stages = migrator.execute("SELECT id, name, color, is_active FROM pipeline_stages ORDER BY id")
    audit = migrator.execute("SELECT count(*) FROM audit_logs")
    return pipelines.fetchall(), stages.fetchall(), audit.fetchone()


def test_patch_changes_what_is_sent_with_the_version_and_audits_it(
    crm: SimpleNamespace, migrator: psycopg.Connection[Any]
) -> None:
    ana = signed(crm.ana)
    pipeline(crm)
    other = pipeline(crm, code="mayoristas", is_default=False).pk
    done = change(ana, other, name=" Otro  nombre ", kind="OTHER", sort_order=-3, is_active=False)
    assert done.status_code == 200, done.content
    got = done.json()
    assert set(got) == PIPELINE_FIELDS and len(got["stages"]) == 4
    assert [got[k] for k in ("name", "kind", "sort_order", "is_active", "version")] == [
        "Otro nombre", "OTHER", -3, False, 2,
    ]  # fmt: skip
    assert (got["code"], got["currency"]) == ("mayoristas", "PEN")
    assert read(ana, PIPELINES)["results"][1] == got
    [(action, actor, changes)] = logged(migrator)[-1:]
    assert (action, actor, changes["kind"]) == ("pipeline.updated", crm.ana.pk, ["RETAIL", "OTHER"])
    before = snapshot(migrator)
    same = change(ana, other, version=2, name="Otro nombre")  # sin cambios: ni versión ni auditoría
    assert (same.status_code, same.json()["version"], snapshot(migrator)) == (200, 2, before)
    promoted = change(ana, other, version=2, is_active=True, is_default=True).json()
    assert (promoted["is_default"], promoted["version"]) == (True, 3)
    assert [p["is_default"] for p in read(ana, PIPELINES)["results"]] == [False, True]


def test_a_stale_or_malformed_version_never_writes(
    crm: SimpleNamespace, migrator: psycopg.Connection[Any]
) -> None:
    made = pipeline(crm).pk
    ana = signed(crm.ana)
    assert change(ana, made, name="Uno").status_code == 200  # ahora va por la 2
    before = snapshot(migrator)
    for version in (1, 3):  # una vieja y una del futuro
        stale = change(ana, made, version=version, name="Dos")
        assert (stale.status_code, stale.json()["code"]) == (409, "STALE_VERSION"), version
    for wrong in (0, -1, "2", True, 2.0, None, 2**31):
        assert fail(change(ana, made, version=wrong)) == (400, "VALIDATION_ERROR", ["version"])
    missing = send(ana, "patch", at(made), {"name": "Tres"})
    assert fail(missing) == (400, "VALIDATION_ERROR", ["version"])
    assert snapshot(migrator) == before


def test_the_default_rules_and_the_fields_a_pipeline_patch_does_not_take(
    crm: SimpleNamespace, migrator: psycopg.Connection[Any]
) -> None:
    ana = signed(crm.ana)
    default = pipeline(crm).pk
    other = pipeline(crm, code="mayoristas", is_default=False).pk
    before = snapshot(migrator)
    attempts: tuple[dict[str, Any], ...] = ({"is_active": False}, {"is_default": False})
    for sent in attempts:  # sin elegir otro
        refused = change(ana, default, **sent)
        assert (refused.status_code, refused.json()["code"]) == (409, "PIPELINE_IS_DEFAULT")
    inactive = change(ana, other, is_active=False, is_default=True)
    assert fail(inactive) == (400, "VALIDATION_ERROR", ["is_default"])  # un inactivo no lo es
    unknown: tuple[dict[str, Any], ...] = (
        {"code": "otro"},
        {"currency": "PEN"},
        {"stages": []},
        {"id": str(uuid4())},
    )
    for sent in unknown:
        assert fail(change(ana, other, **sent)) == (400, "VALIDATION_ERROR", sorted(sent))
    bad: list[dict[str, Any]] = [{"name": 5}, {"kind": "x"}, {"sort_order": "1"}, {"is_active": 1}]
    bad += [
        {"is_default": "x"},
        {"is_default": "false"},
        {"is_default": 0},
        {"name": "a\u202eb"},
        {"sort_order": 32768},
        {"name": "x" * 101},
    ]
    for sent in bad:
        assert fail(change(ana, other, **sent)) == (400, "VALIDATION_ERROR", sorted(sent))
    assert snapshot(migrator) == before
    assert reply(change(ana, uuid4(), name="x")) == NOT_FOUND
    assert reply(send(ana, "patch", PIPELINES + "no-es-uuid/", {"version": 1})) == NOT_FOUND


def test_who_may_patch_and_what_is_the_other_organization(
    crm: SimpleNamespace, migrator: psycopg.Connection[Any]
) -> None:
    made = pipeline(crm)
    theirs = pipeline(crm, "b", code="solo-b")
    gone = pipeline(crm, code="borrable", is_default=False)
    migrator.execute("UPDATE pipelines SET deleted_at = now() WHERE id = %s", [gone.pk])
    before = snapshot(migrator)
    give(crm.a, crm.m["bea"], {"pipelines.manage": None})  # solo administrar, sin ver
    for version, who in enumerate((crm.ana, administrator(crm), crm.bea), 1):
        assert change(signed(who), made.pk, version, name=f"Sí {version}").status_code == 200
    after = snapshot(migrator)
    first = at(made.pk, stage(made, "nuevo").pk)
    with CaptureQueriesContext(connection) as captured:
        for user in (crm.vera, crm.sam):  # ver oportunidades no es administrarlas
            denied = (change(signed(user), made.pk, 4), send(signed(user), "patch", first, {}))
            assert [reply(r) for r in denied] == [DENIED, DENIED], user.email
    assert not [q for q in captured.captured_queries if "pipeline" in q["sql"]]  # antes de leer
    anonymous = signed(crm.ana)
    anonymous.cookies.clear()
    assert reply(change(anonymous, made.pk, 4)) == (401, b'{"code":"NOT_AUTHENTICATED"}')
    ana = signed(crm.ana)  # con sesión, sin la cabecera CSRF
    assert reply(ana.patch(at(made.pk), {"version": 4}, "application/json")) == CSRF_FAILED
    give(crm.b, crm.m_luis, {"pipelines.manage": None})
    luis = signed(crm.luis)
    assert reply(change(luis, made.pk, 4)) == NOT_FOUND  # no es miembro de A
    elsewhere = send(luis, "patch", at(made.pk, org="org-b"), {"version": 4, "name": "Mío"})
    for missing in (change(ana, theirs.pk), change(ana, gone.pk), elsewhere):
        assert reply(missing) == NOT_FOUND  # de B, borrado o ajeno por la ruta de B: igual
    assert snapshot(migrator)[2] != before[2] and snapshot(migrator) == after


def test_a_stage_is_renamed_and_the_answer_is_the_pipeline(
    crm: SimpleNamespace, migrator: psycopg.Connection[Any]
) -> None:
    ana, made = signed(crm.ana), pipeline(crm)
    rules = {"note": {"required": True, "tags": ["a", 1, None]}}
    done = edit(ana, made, "cotizado", name=" Propuesta  enviada ", color="#ABCDEF",
                stale_after_days=None, default_probability=55, requires_fields=rules)  # fmt: skip
    assert done.status_code == 200, done.content
    got = done.json()
    assert set(got) == PIPELINE_FIELDS and got["version"] == 1  # las etapas no tienen versión
    assert all(set(s) == STAGE_FIELDS for s in got["stages"])
    renamed = {s["code"]: s for s in got["stages"]}["cotizado"]
    assert [renamed[k] for k in ("name", "color", "stale_after_days", "default_probability")] == [
        "Propuesta enviada", "#abcdef", None, 55,
    ]  # fmt: skip
    assert renamed["requires_fields"] == rules and renamed["sort_order"] == 1
    assert read(ana, PIPELINES)["results"] == [got]
    [(action, actor, changes)] = logged(migrator, "stage")
    assert (action, actor, changes["name"]) == (
        "stage.updated",
        crm.ana.pk,
        ["Cotizado", got["stages"][1]["name"]],
    )
    before = snapshot(migrator)
    same = edit(
        ana, made, "cotizado", name="Propuesta enviada"
    )  # sin cambios: ni escribe ni audita
    assert (same.status_code, snapshot(migrator)) == (200, before)
    assert edit(ana, made, "nuevo").status_code == 200  # `{}`: nada que cambiar


def test_a_stage_of_another_pipeline_or_organization_does_not_exist_for_the_url(
    crm: SimpleNamespace, migrator: psycopg.Connection[Any]
) -> None:
    ana, mine = signed(crm.ana), pipeline(crm)
    second = pipeline(crm, code="mayoristas", is_default=False)
    theirs = pipeline(crm, "b", code="solo-b")
    gone = pipeline(crm, code="borrable", is_default=False)
    migrator.execute("UPDATE pipelines SET deleted_at = now() WHERE id = %s", [gone.pk])
    before = snapshot(migrator)
    sent = {"name": "Robada"}
    cases = [
        (mine.pk, stage(second, "nuevo").pk),  # de otro pipeline de la misma organización
        (mine.pk, stage(theirs, "nuevo").pk),  # de otra organización
        (second.pk, stage(mine, "nuevo").pk),  # y al revés
        (mine.pk, uuid4()),  # inexistente
        (uuid4(), stage(mine, "nuevo").pk),  # pipeline inexistente
        (theirs.pk, stage(theirs, "nuevo").pk),  # pipeline de B, por la ruta de A
        (gone.pk, stage(gone, "nuevo").pk),  # de un pipeline borrado
    ]
    for pipeline_id, stage_id in cases:
        assert reply(send(ana, "patch", at(pipeline_id, stage_id), sent)) == NOT_FOUND
    assert snapshot(migrator) == before
    assert send(ana, "patch", at(mine.pk, stage(mine, "nuevo").pk), sent).status_code == 200


def test_the_invariants_come_back_as_409_and_a_closed_probability_as_a_400(
    crm: SimpleNamespace, migrator: psycopg.Connection[Any]
) -> None:
    ana = signed(crm.ana)
    made = pipeline(crm, stages=[spec("nuevo"), *basic()[2:]])  # una OPEN, una WON, una LOST
    before = snapshot(migrator)
    refused: list[tuple[str, dict[str, Any]]] = [
        ("nuevo", {"is_active": False}),  # la última OPEN
        ("perdido", {"is_active": False}),  # la última LOST
        ("ganado", {"is_active": False}),  # la WON
        ("ganado", {"stage_type": "OPEN", "default_probability": 10}),  # ya no habría WON
        ("nuevo", {"stage_type": "WON", "default_probability": 100}),  # una segunda WON
    ]
    for code, sent in refused:
        response = edit(ana, made, code, **sent)
        assert (response.status_code, response.json()["code"]) == (409, "PIPELINE_INVARIANT"), code
    closed = edit(ana, made, "perdido", default_probability=5)
    assert fail(closed) == (400, "VALIDATION_ERROR", ["default_probability"])
    assert snapshot(migrator) == before
    assert edit(ana, made, "perdido", name="Se fue").status_code == 200


BAD = [  # (lo que se cambia, el campo del 400)
    ({"name": 5}, "name"), ({"name": ""}, "name"), ({"name": "a\x00b"}, "name"),
    ({"name": "a\u202eb"}, "name"), ({"name": "x" * 101}, "name"),
    ({"code": "otro"}, "code"), ({"id": str(uuid4())}, "id"), ({"sort_order": 2}, "sort_order"),
    ({"stage_type": "WIN"}, "stage_type"), ({"stage_type": 1}, "stage_type"),
    ({"default_probability": "10"}, "default_probability"),
    ({"default_probability": True}, "default_probability"),
    ({"default_probability": 101}, "default_probability"),
    ({"default_probability": None}, "default_probability"),
    ({"stale_after_days": 0}, "stale_after_days"), ({"stale_after_days": "7"}, "stale_after_days"),
    ({"stale_after_days": 32768}, "stale_after_days"), ({"color": "red"}, "color"),
    ({"color": 5}, "color"), ({"is_active": "true"}, "is_active"),
    ({"is_active": None}, "is_active"),
    ({"requires_fields": []}, "requires_fields"), ({"requires_fields": None}, "requires_fields"),
    ({"requires_fields": {"k\x00": 1}}, "requires_fields"),
    ({"requires_fields": {"k": "\x00"}}, "requires_fields"),
    ({"requires_fields": {"a": {"b": {"c": {"d": {}}}}}}, "requires_fields"),
    ({"requires_fields": {"k": "x" * 4096}}, "requires_fields"),
]  # fmt: skip


@pytest.mark.parametrize(("changed", "field"), BAD)
def test_a_bad_stage_value_is_a_400_on_its_field_and_writes_nothing(
    crm: SimpleNamespace, migrator: psycopg.Connection[Any], changed: dict[str, Any], field: str
) -> None:
    made = pipeline(crm)
    before = snapshot(migrator)
    response = edit(signed(crm.ana), made, "nuevo", **changed)
    assert fail(response) == (400, "VALIDATION_ERROR", [field]), response.content
    assert snapshot(migrator) == before
    for echoed in ("\x00", "\u202e"):  # el texto del error no repite lo recibido
        assert echoed not in response.content.decode()


HOSTILE = [  # cuerpos que ningún serializador espera: 400 o 415, nunca un 500
    "null", "[]", '"x"', "1", "{", '{"name": "' + "x" * 200_000 + '"}',
    "[" * 200 + "]" * 200, '{"name":"\\ud800"}', '{"version":NaN}', '{"requires_fields":NaN}',
]  # fmt: skip


@pytest.mark.parametrize("raw", HOSTILE)
def test_hostile_bodies_are_never_a_500(crm: SimpleNamespace, raw: str) -> None:
    made, ana = pipeline(crm), signed(crm.ana)
    for path in (at(made.pk), at(made.pk, stage(made, "nuevo").pk)):
        response = send(ana, "patch", path, raw)
        assert response.status_code in {400, 415}, (path, response.status_code)
        assert response.json()["code"] in {"VALIDATION_ERROR", "PARSE_ERROR"}


def test_concurrent_patches_one_wins_the_version_and_the_invariants_hold(
    crm: SimpleNamespace, migrator: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    made = pipeline(crm, stages=TWO_OPEN)
    twice = http_race(monkeypatch, [{"version": 1, "name": "A"}] * 2, "patch", at(made.pk))
    assert twice == [(200, "retail"), (409, "STALE_VERSION")]
    paths = [at(made.pk, stage(made, code).pk) for code in ("nuevo", "contactado")]
    off = http_race(monkeypatch, [{"is_active": False}] * 2, "patch", paths)  # no quedaría OPEN
    assert off == [(200, "retail"), (409, "PIPELINE_INVARIANT")]
    live = migrator.execute(
        "SELECT count(*) FROM pipeline_stages WHERE stage_type = 'OPEN' AND is_active"
    ).fetchone()
    assert live == (1,)


def test_opportunities_make_a_deactivation_or_a_type_change_a_409(
    crm: SimpleNamespace, migrator: psycopg.Connection[Any]
) -> None:
    ana, made = signed(crm.ana), pipeline(crm)
    other = pipeline(crm, code="mayoristas", is_default=False)
    here, there = stage(made, "nuevo").pk, stage(other, "nuevo").pk
    test_opportunities_tables.make(crm.a, pipeline_id=other.pk, stage_id=there)  # abierta
    test_opportunities_tables.make(crm.a, stage_id=here, deleted_at=timezone.now())  # borrada
    before = snapshot(migrator)
    refused = [
        (change(ana, other.pk, is_active=False), "OPEN_OPPORTUNITIES"),
        (send(ana, "patch", at(other.pk, there), {"is_active": False}), "OPEN_OPPORTUNITIES"),
        (edit(ana, made, "nuevo", stage_type="LOST", default_probability=0), "STAGE_IN_USE"),
    ]
    for response, code in refused:
        assert (response.status_code, response.json()["code"]) == (409, code)
    assert snapshot(migrator) == before

"""F5-02c: crear y cambiar motivos de pérdida por HTTP (`POST …/lost-reasons/`, `PATCH
…/lost-reasons/{id}/`). Middleware, sesión, motor de autorización y PostgreSQL con el rol
`crm_app`; los comandos se prueban en `test_lost_reasons`."""

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
from tests.test_pipelines_api import REASON_FIELDS, REASONS, read, reason
from tests.test_pipelines_write_api import CSRF_FAILED, administrator, fail, http_race
from tests.test_products import send
from tests.test_self_context import NOT_FOUND, reply, signed

crm, real_stack = test_contacts_api.crm, test_self_context.real_stack
pytestmark = pytest.mark.usefixtures("tenant_db")


def body(**fields: Any) -> dict[str, Any]:
    return {"name": "  Precio   alto ", "code": "price", **fields}


def at(reason_id: Any, org: str = "org-a") -> str:
    return f"/api/v1/o/{org}/lost-reasons/{reason_id}/"


def create(client: Any, **fields: Any) -> Any:
    return send(client, "post", REASONS, body(**fields))


def change(client: Any, reason_id: Any, version: Any = 1, **fields: Any) -> Any:
    return send(client, "patch", at(reason_id), {"version": version, **fields})


def stored(migrator: psycopg.Connection[Any]) -> tuple[int, int]:
    rows = migrator.execute("SELECT count(*) FROM lost_reasons").fetchone()
    logged = migrator.execute("SELECT count(*) FROM audit_logs WHERE entity_type = 'lost_reason'")
    return (rows or (0,))[0], (logged.fetchone() or (0,))[0]


def test_the_owner_creates_and_changes_a_reason_and_gets_it_back_with_its_audit(
    crm: SimpleNamespace, migrator: psycopg.Connection[Any]
) -> None:
    ana = signed(crm.ana)
    made = create(ana)
    assert made.status_code == 201, made.content
    new = made.json()
    assert set(new) == REASON_FIELDS
    assert [new[k] for k in ("name", "code", "sort_order", "is_active", "version")] == [
        "Precio alto", "PRICE", 0, True, 1,
    ]  # fmt: skip
    assert create(ana, name="Otro", code="other").json()["sort_order"] == 1  # al final
    done = change(ana, new["id"], name=" Muy  caro ", sort_order=7, is_active=False)
    assert done.status_code == 200, done.content
    got = done.json()
    assert [got[k] for k in ("name", "code", "sort_order", "is_active", "version")] == [
        "Muy caro", "PRICE", 7, False, 2,
    ]  # fmt: skip
    assert read(ana, REASONS)["results"][0] == got
    rows = migrator.execute(
        "SELECT action, actor_id, changes FROM audit_logs WHERE entity_type = 'lost_reason' "
        "ORDER BY occurred_at, id"
    ).fetchall()
    assert [(action, actor) for action, actor, _ in rows] == [
        ("lost_reason.created", crm.ana.pk)
    ] * 2 + [("lost_reason.updated", crm.ana.pk)]
    assert rows[2][2]["is_active"] == [True, False]
    logged = stored(migrator)
    same = change(
        ana, new["id"], version=2, name="Muy caro"
    )  # sin cambios: ni versión ni auditoría
    assert (same.status_code, same.json()["version"], stored(migrator)) == (200, 2, logged)


def test_only_pipelines_manage_writes_and_it_alone_is_enough(
    crm: SimpleNamespace, migrator: psycopg.Connection[Any]
) -> None:
    made = reason(crm, "OLD").pk
    before = stored(migrator)
    give(crm.a, crm.m["bea"], {"pipelines.manage": None})  # solo administrar, sin ver
    for who, user in {"ana": crm.ana, "admin": administrator(crm), "bea": crm.bea}.items():
        assert create(signed(user), code=who).status_code == 201, who
    assert change(signed(crm.bea), made, name="Solo administra").status_code == 200
    assert reply(signed(crm.bea).get(REASONS)) == DENIED  # y sin `opportunities.view` no lee
    after = stored(migrator)
    give(crm.a, crm.m["vera"], {"opportunities.view": "ORGANIZATION"})
    for user in (crm.vera, crm.sam):  # ver oportunidades no es administrarlas
        for denied in (create(signed(user), code="no"), change(signed(user), made, name="No")):
            assert reply(denied) == DENIED, user.email
    assert stored(migrator) == after != before  # lo denegado no escribió ni auditó


def test_the_permission_is_checked_before_anything_about_reasons_is_read(
    crm: SimpleNamespace,
) -> None:
    made = reason(crm, "OLD").pk
    with CaptureQueriesContext(connection) as captured:
        for denied in (create(signed(crm.vera)), change(signed(crm.vera), made)):
            assert reply(denied) == DENIED
    assert not [q for q in captured.captured_queries if "lost_reasons" in q["sql"]]


def test_anonymous_csrf_the_other_organization_and_what_does_not_exist(
    crm: SimpleNamespace, migrator: psycopg.Connection[Any]
) -> None:
    mine, theirs = reason(crm, "MINE").pk, reason(crm, "THEIRS", "b").pk
    gone = reason(crm, "GONE").pk
    migrator.execute("UPDATE lost_reasons SET deleted_at = now() WHERE id = %s", [gone])
    before = stored(migrator)
    anonymous = signed(crm.ana)
    anonymous.cookies.clear()
    expected = (401, b'{"code":"NOT_AUTHENTICATED"}')
    assert reply(create(anonymous)) == expected and reply(change(anonymous, mine)) == expected
    ana = signed(crm.ana)  # con sesión, sin la cabecera CSRF
    assert reply(ana.post(REASONS, body(), "application/json")) == CSRF_FAILED
    assert reply(ana.patch(at(mine), {"version": 1}, "application/json")) == CSRF_FAILED
    give(crm.b, crm.m_luis, {"pipelines.manage": None})
    luis = signed(crm.luis)
    assert reply(create(luis)) == NOT_FOUND and reply(change(luis, mine)) == NOT_FOUND  # no es de A
    elsewhere = send(luis, "patch", at(mine, "org-b"), {"version": 1, "name": "Mío"})
    for missing in (change(ana, theirs), change(ana, uuid4()), change(ana, gone), elsewhere):
        assert reply(missing) == NOT_FOUND  # de otra organización, inexistente o borrado: igual
    assert reply(send(ana, "patch", REASONS + "no-es-uuid/", {"version": 1})) == NOT_FOUND
    assert stored(migrator) == before


BAD = [  # (lo que se cambia del cuerpo válido, el campo del 400)
    ({"name": 5}, "name"), ({"name": None}, "name"), ({"name": ""}, "name"),
    ({"name": "x" * 101}, "name"), ({"name": "a\x00b"}, "name"), ({"name": "a‮b"}, "name"),
    ({"name": "a\nb"}, "name"), ({"name": "!!!"}, "name"), ({"code": 7}, "code"),
    ({"code": "Ñu"}, "code"), ({"code": "1PRICE"}, "code"), ({"code": "A-B"}, "code"),
    ({"code": "X" * 31}, "code"), ({"code": ""}, "code"), ({"sort_order": "1"}, "sort_order"),
    ({"sort_order": 1.0}, "sort_order"), ({"sort_order": True}, "sort_order"),
    ({"sort_order": 32768}, "sort_order"), ({"sort_order": -32769}, "sort_order"),
    ({"sort_order": None}, "sort_order"), ({"is_active": True}, "is_active"),
    ({"organization_id": str(uuid4())}, "organization_id"), ({"version": 1}, "version"),
]  # fmt: skip
BAD_UPDATES = [
    ({"name": 5}, "name"), ({"name": "a‮b"}, "name"), ({"name": "x" * 101}, "name"),
    ({"sort_order": "1"}, "sort_order"), ({"sort_order": True}, "sort_order"),
    ({"sort_order": 32768}, "sort_order"), ({"is_active": 1}, "is_active"),
    ({"is_active": "false"}, "is_active"), ({"is_active": None}, "is_active"),
    ({"code": "OTHER"}, "code"), ({"id": str(uuid4())}, "id"),
    ({"version": 0}, "version"), ({"version": "1"}, "version"), ({"version": True}, "version"),
    ({"version": 1.0}, "version"), ({"version": None}, "version"), ({"version": 2**31}, "version"),
]  # fmt: skip


@pytest.mark.parametrize(("changed", "field"), BAD)
def test_a_bad_value_on_create_is_a_400_on_its_field_and_writes_nothing(
    crm: SimpleNamespace, migrator: psycopg.Connection[Any], changed: dict[str, Any], field: str
) -> None:
    before = stored(migrator)
    response = send(signed(crm.ana), "post", REASONS, {**body(), **changed})
    assert fail(response) == (400, "VALIDATION_ERROR", [field]), response.content
    assert stored(migrator) == before
    for echoed in ("\x00", "‮", "Ñu"):  # el texto del error no repite lo recibido
        assert echoed not in response.content.decode()


@pytest.mark.parametrize(("changed", "field"), BAD_UPDATES)
def test_a_bad_value_on_update_is_a_400_on_its_field_and_writes_nothing(
    crm: SimpleNamespace, migrator: psycopg.Connection[Any], changed: dict[str, Any], field: str
) -> None:
    made = reason(crm, "OLD").pk
    before = stored(migrator)
    sent = {"version": 1, **changed}
    response = send(signed(crm.ana), "patch", at(made), sent)
    assert fail(response) == (400, "VALIDATION_ERROR", [field]), response.content
    assert stored(migrator) == before
    missing = send(signed(crm.ana), "patch", at(made), {"name": "Sin versión"})
    assert fail(missing) == (400, "VALIDATION_ERROR", ["version"])


def test_the_code_is_unique_among_the_live_ones_and_a_stale_version_is_a_409(
    crm: SimpleNamespace, migrator: psycopg.Connection[Any]
) -> None:
    ana = signed(crm.ana)
    made = create(ana).json()["id"]
    before = stored(migrator)
    for code in ("PRICE", "price"):  # se guarda en mayúsculas: la misma clave
        taken = create(ana, code=code)
        assert (taken.status_code, taken.json()["code"]) == (409, "CODE_TAKEN"), code
    assert create(signed(crm.luis), code="price").status_code == 404  # B: ni se entera
    assert change(ana, made, name="Uno").status_code == 200  # ahora va por la 2
    after = stored(migrator)
    for version in (1, 3):  # una vieja y una del futuro
        stale = change(ana, made, version=version, name="Dos")
        assert (stale.status_code, stale.json()["code"]) == (409, "STALE_VERSION"), version
    assert stored(migrator) == (before[0], before[1] + 1) and after == stored(migrator)
    assert read(ana, REASONS)["results"][0]["name"] == "Uno"
    migrator.execute("UPDATE lost_reasons SET deleted_at = now() WHERE id = %s", [made])
    assert create(ana).status_code == 201  # el código de uno borrado se puede volver a usar


def test_the_caps_per_organization_and_the_implicit_order_that_ran_out(
    crm: SimpleNamespace, migrator: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    ana = signed(crm.ana)
    monkeypatch.setattr(inputs, "REASONS_MAX", 3)
    reason(crm, "SOLO_B", "b")  # los de otra organización no cuentan
    for code in ("UNO", "DOS", "TRES"):
        assert create(ana, code=code).status_code == 201  # el tercero cabe
    full = create(ana, code="CUATRO")
    assert fail(full) == (400, "VALIDATION_ERROR", ["_"]), full.content
    migrator.execute("UPDATE lost_reasons SET deleted_at = now() WHERE code = 'TRES'")
    assert create(ana, code="CUATRO", sort_order=32767).status_code == 201  # uno borrado no cuenta
    lockout = create(ana, code="CINCO")  # tras 32767 el orden implícito ya no cabe
    assert fail(lockout) == (400, "VALIDATION_ERROR", ["_"])
    monkeypatch.setattr(inputs, "REASONS_MAX", 9)
    clear = create(ana, code="CINCO")
    assert fail(clear) == (400, "VALIDATION_ERROR", ["sort_order"]), clear.content
    assert "indica uno" in clear.json()["fields"]["sort_order"][0]["message"]
    assert create(ana, code="CINCO", sort_order=0).status_code == 201


HOSTILE = [  # cuerpos que ningún serializador espera: 400 o 415, nunca un 500
    "null", "[]", '"x"', "1", "{", "", "{}", '{"name": "' + "x" * 200_000 + '"}',
    "[" * 200 + "]" * 200, '{"name":"\\ud800"}', '{"version":1e999}', '{"version":NaN}',
]  # fmt: skip


@pytest.mark.parametrize("raw", HOSTILE)
def test_hostile_bodies_are_never_a_500(crm: SimpleNamespace, raw: str) -> None:
    made, ana = reason(crm, "OLD").pk, signed(crm.ana)
    for method, path in (("post", REASONS), ("patch", at(made))):
        response = send(ana, method, path, raw)
        assert response.status_code in {400, 415}, (method, response.status_code)
        assert response.json()["code"] in {"VALIDATION_ERROR", "PARSE_ERROR"}


def test_two_requests_at_once_never_pass_the_cap_or_the_version(
    crm: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    made = reason(crm, "OLD").pk
    twice = http_race(monkeypatch, [{"version": 1, "name": "A"}, {"version": 1, "name": "B"}],
                      "patch", at(made))  # fmt: skip
    assert twice == [(200, "OLD"), (409, "STALE_VERSION")]
    monkeypatch.setattr(inputs, "REASONS_MAX", 2)  # ya hay uno: cabe uno más
    capped = http_race(monkeypatch, [body(code="UNO"), body(code="DOS")], "post", REASONS)
    assert capped == [(201, "UNO"), (400, "VALIDATION_ERROR")]
    monkeypatch.setattr(inputs, "REASONS_MAX", 100)
    same = http_race(monkeypatch, [body(code="SOLO"), body(code="SOLO")], "post", REASONS)
    assert same == [(201, "SOLO"), (409, "CODE_TAKEN")]

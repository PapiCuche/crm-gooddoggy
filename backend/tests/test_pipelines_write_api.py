"""F5-02b: crear y cambiar pipelines por HTTP (`POST …/pipelines/`, `PATCH …/pipelines/{id}/`).
Middleware, sesión, motor de autorización y PostgreSQL con el rol `crm_app`; los comandos se
prueban en `test_pipeline_commands`."""

import threading
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from apps.access.models import MembershipRole, Role
from apps.accounts.models import User
from apps.audit import services as audit_services
from apps.deals import commands
from apps.deals.api import inputs
from apps.deals.models import Pipeline
from core.tenancy.scope import tenant_scope
from core.text import name as clean_name
from tests import test_contacts_api, test_self_context
from tests.factories import make_user
from tests.test_authorization import give
from tests.test_member_branch import DENIED
from tests.test_memberships import ctx, join
from tests.test_pipeline_commands import basic, spec
from tests.test_pipelines_api import PIPELINE_FIELDS, PIPELINES, STAGE_FIELDS, pipeline, read
from tests.test_products import send
from tests.test_self_context import NOT_FOUND, reply, signed

crm, real_stack = test_contacts_api.crm, test_self_context.real_stack
pytestmark = pytest.mark.usefixtures("tenant_db")
CSRF_FAILED = (403, b'{"code":"CSRF_FAILED"}')


def body(**fields: Any) -> dict[str, Any]:
    return {
        "name": "  Ventas   mayoristas ", "code": "mayoristas", "kind": "WHOLESALE",
        "stages": basic(), **fields,
    }  # fmt: skip


def create(client: Any, **fields: Any) -> Any:
    return send(client, "post", PIPELINES, body(**fields))


def fail(response: Any) -> tuple[int, str | None, list[str]]:
    sent = response.json()
    return response.status_code, sent.get("code"), sorted(sent.get("fields", {}))


def audit(migrator: psycopg.Connection[Any]) -> list[tuple[str, Any, Any]]:
    found = migrator.execute(
        "SELECT action, actor_id, changes FROM audit_logs WHERE entity_type = 'pipeline' "
        "ORDER BY occurred_at, id"
    )
    return found.fetchall()


def stored(migrator: psycopg.Connection[Any]) -> tuple[int, int]:
    pipelines = migrator.execute("SELECT count(*) FROM pipelines").fetchone()
    logged = migrator.execute("SELECT count(*) FROM audit_logs WHERE entity_type = 'pipeline'")
    return (pipelines or (0,))[0], (logged.fetchone() or (0,))[0]


def administrator(crm: SimpleNamespace) -> User:
    user = make_user(email="admin@example.com")
    membership = join(crm.a, user).pk
    with tenant_scope(ctx(crm.a)):
        MembershipRole.objects.create(membership_id=membership, role=Role.objects.get(code="admin"))
    return user


def test_the_owner_creates_a_pipeline_and_gets_it_back_like_the_read_route(
    crm: SimpleNamespace, migrator: psycopg.Connection[Any]
) -> None:
    ana = signed(crm.ana)
    first = create(ana)
    assert first.status_code == 201, first.content
    made = first.json()
    assert set(made) == PIPELINE_FIELDS and all(set(s) == STAGE_FIELDS for s in made["stages"])
    assert (made["name"], made["code"], made["kind"], made["currency"]) == (
        "Ventas mayoristas", "mayoristas", "WHOLESALE", "PEN",
    )  # fmt: skip
    assert (made["version"], made["is_default"], made["is_active"]) == (1, True, True)  # el primero
    assert [(s["code"], s["sort_order"]) for s in made["stages"]] == [
        ("nuevo", 0), ("cotizado", 1), ("ganado", 2), ("perdido", 3)
    ]  # fmt: skip
    assert read(ana, PIPELINES)["results"] == [made]
    other = create(ana, code="retail", is_default=True, sort_order=5).json()
    assert (other["is_default"], other["sort_order"]) == (True, 5)  # quita la marca al anterior
    assert [p["is_default"] for p in read(ana, PIPELINES)["results"]] == [False, True]
    actions = [(action, actor) for action, actor, _ in audit(migrator)]
    assert actions == [
        ("pipeline.created", crm.ana.pk), ("pipeline.updated", crm.ana.pk),
        ("pipeline.created", crm.ana.pk),
    ]  # fmt: skip


def test_only_pipelines_manage_writes_and_it_alone_is_enough(
    crm: SimpleNamespace, migrator: psycopg.Connection[Any]
) -> None:
    before = stored(migrator)
    give(crm.a, crm.m["bea"], {"pipelines.manage": None})  # solo administrar, sin ver
    for who, user in {"ana": crm.ana, "admin": administrator(crm), "bea": crm.bea}.items():
        assert create(signed(user), code=who).status_code == 201, who
    assert reply(signed(crm.bea).get(PIPELINES)) == DENIED  # y sin `opportunities.view` no lee
    after = stored(migrator)
    for user in (crm.vera, crm.sam):  # Vendedora y Supervisor ven oportunidades, no administran
        assert reply(create(signed(user), code="no")) == DENIED, user.email
    give(crm.a, crm.m["vera"], {"opportunities.view": "ORGANIZATION"})
    assert reply(create(signed(crm.vera), code="no")) == DENIED  # ver no es administrar
    assert stored(migrator) == after != before  # lo denegado no escribió ni auditó


def test_the_permission_is_checked_before_anything_about_pipelines_is_read(
    crm: SimpleNamespace,
) -> None:
    with CaptureQueriesContext(connection) as captured:
        assert reply(create(signed(crm.vera))) == DENIED
    assert not [q for q in captured.captured_queries if "pipeline" in q["sql"]]


def test_anonymous_csrf_and_the_other_organization(
    crm: SimpleNamespace, migrator: psycopg.Connection[Any]
) -> None:
    before = stored(migrator)
    anonymous = signed(crm.ana)
    anonymous.cookies.clear()  # sin sesión
    assert reply(create(anonymous)) == (401, b'{"code":"NOT_AUTHENTICATED"}')
    ana = signed(crm.ana)  # con sesión, sin la cabecera CSRF
    assert reply(ana.post(PIPELINES, body(), "application/json")) == CSRF_FAILED
    give(crm.b, crm.m_luis, {"pipelines.manage": None})
    luis = signed(crm.luis)
    assert reply(create(luis)) == NOT_FOUND  # no es miembro de A
    assert reply(send(luis, "post", PIPELINES.replace("-a", "-x"), body())) == NOT_FOUND
    assert reply(send(ana, "post", PIPELINES.replace("-a", "-b"), body())) == NOT_FOUND
    assert stored(migrator) == before
    mine = send(luis, "post", PIPELINES.replace("-a", "-b"), body())  # en la suya sí escribe
    assert mine.status_code == 201 and read(ana, PIPELINES)["results"] == []


def without(key: str) -> dict[str, Any]:
    return {k: v for k, v in spec("a").items() if k != key}


BAD = [  # (lo que se cambia del cuerpo válido, el campo del 400)
    ({"name": 5}, "name"), ({"name": None}, "name"), ({"name": ["a"]}, "name"),
    ({"name": ""}, "name"), ({"name": "x" * 101}, "name"), ({"name": "a\x00b"}, "name"),
    ({"name": "a\u202eb"}, "name"), ({"name": "a\nb"}, "name"), ({"name": "!!!"}, "name"),
    ({"code": 7}, "code"), ({"code": "Ñu"}, "code"), ({"code": "a b"}, "code"),
    ({"code": "-a"}, "code"), ({"code": "x" * 31}, "code"), ({"code": ""}, "code"),
    ({"kind": "retail"}, "kind"), ({"kind": 1}, "kind"), ({"kind": ["RETAIL"]}, "kind"),
    ({"kind": {"a": 1}}, "kind"), ({"kind": None}, "kind"), ({"kind": "\u202e"}, "kind"),
    ({"kind": "Ñu"}, "kind"), ({"name": "\u3164"}, "name"), ({"currency": "USD"}, "currency"),
    ({"currency": 1}, "currency"), ({"sort_order": "1"}, "sort_order"),
    ({"sort_order": 1.0}, "sort_order"), ({"sort_order": True}, "sort_order"),
    ({"sort_order": 32768}, "sort_order"), ({"sort_order": -32769}, "sort_order"),
    ({"is_default": "false"}, "is_default"), ({"is_default": 0}, "is_default"),
    ({"is_default": None}, "is_default"), ({"organization_id": str(uuid4())}, "organization_id"),
    ({"version": 1}, "version"), ({"stages": "x"}, "stages"), ({"stages": []}, "stages"),
    ({"stages": None}, "stages"), ({"stages": {}}, "stages"), ({"stages": [1]}, "stages"),
    ({"stages": [spec("a")] * 31}, "stages"), ({"stages": [*basic(), basic()[0]]}, "stages"),
    ({"stages": [{**spec("a"), "foo": 1}, *basic()]}, "stages"),
    ({"stages": [{"name": "a"}, *basic()]}, "stages"),
    ({"stages": [without("color"), *basic()]}, "stages"),
    ({"stages": [without("name"), *basic()]}, "stages"),
    ({"stages": [spec("a", name=5), *basic()]}, "stages"),
    *(({"stages": [spec("a", name=bad), *basic()]}, "stages")
      for bad in ("a\x00b", "a\u202eb", "a\nb", "!!!", "\u3164")),
    ({"stages": [{**spec("a"), "code": "A B"}, *basic()]}, "stages"),
    ({"stages": [spec("a", stage_type="WIN"), *basic()]}, "stages"),
    ({"stages": [spec("a", default_probability="10"), *basic()]}, "stages"),
    ({"stages": [spec("a", default_probability=True), *basic()]}, "stages"),
    ({"stages": [spec("a", default_probability=101), *basic()]}, "stages"),
    ({"stages": [spec("a", default_probability=1.5), *basic()]}, "stages"),
    ({"stages": [spec("a", stale_after_days=0), *basic()]}, "stages"),
    ({"stages": [spec("a", stale_after_days="7"), *basic()]}, "stages"),
    ({"stages": [spec("a", color="red"), *basic()]}, "stages"),
    ({"stages": [spec("a", color=5), *basic()]}, "stages"),
    ({"stages": [spec("a", is_active="true"), *basic()]}, "stages"),
    ({"stages": [spec("a", requires_fields=[]), *basic()]}, "stages"),
    ({"stages": [spec("a", requires_fields={"k\x00": 1}), *basic()]}, "stages"),
    ({"stages": [spec("a", requires_fields={"k": "\x00"}), *basic()]}, "stages"),
    ({"stages": [spec("a", requires_fields={"a": {"b": {"c": {"d": {}}}}}), *basic()]}, "stages"),
    ({"stages": [spec("a", requires_fields={"k": "x" * 4096}), *basic()]}, "stages"),
    ({"stages": [spec("a", default_probability=40, stage_type="WON"), *basic()[:2]]}, "stages"),
]  # fmt: skip


@pytest.mark.parametrize(("changed", "field"), BAD)
def test_a_bad_value_is_a_400_on_its_field_and_writes_nothing(
    crm: SimpleNamespace, migrator: psycopg.Connection[Any], changed: dict[str, Any], field: str
) -> None:
    before = stored(migrator)
    response = send(signed(crm.ana), "post", PIPELINES, {**body(), **changed})
    assert fail(response) == (400, "VALIDATION_ERROR", [field]), response.content
    assert stored(migrator) == before
    for echoed in ("\x00", "\u202e", "Ñu"):  # el texto del error no repite lo recibido
        assert echoed not in response.content.decode()


def test_requires_fields_is_a_bounded_json_object(crm: SimpleNamespace) -> None:
    ana = signed(crm.ana)

    def with_fields(value: Any) -> Any:
        return create(
            ana, code=f"p{uuid4().hex[:8]}", stages=[spec("a", requires_fields=value), *basic()[2:]]
        )

    nested = {"a": {"b": [{"c": 1, "e": None, "f": True, "g": 2.5, "h": "ñ"}]}}  # 4 niveles
    made = with_fields(nested)
    assert made.status_code == 201, made.content
    assert made.json()["stages"][0]["requires_fields"] == nested
    assert with_fields({"k": "x" * 4088}).status_code == 201  # 4096 bytes: `{"k":""}` son 8
    over = with_fields({"k": "x" * 4089})  # 4097 bytes
    assert fail(over) == (400, "VALIDATION_ERROR", ["stages"]), over.content
    assert with_fields({"k": "ñ" * 2049}).status_code == 400  # bytes, no caracteres
    raw = '{"name":"n","code":"n","kind":"RETAIL","stages":[%s]}'
    for hostile in ('{"a":NaN}', '{"a":Infinity}', '{"a":-Infinity}', '{"a":1e999}'):
        stage = '{"name":"a","code":"a","stage_type":"OPEN","default_probability":1,'
        stage += '"color":"#000000","requires_fields":%s}'
        response = send(ana, "post", PIPELINES, raw % (stage % hostile))
        assert response.status_code == 400 and response.json()["code"] in {
            "PARSE_ERROR",
            "VALIDATION_ERROR",
        }


def test_the_caps_on_pipelines_per_organization_and_stages_per_pipeline(
    crm: SimpleNamespace, migrator: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    ana = signed(crm.ana)
    many = [spec(f"s{i}") for i in range(28)]  # 28 OPEN, todas válidas y distintas
    ends = [spec("ganado", "WON"), spec("perdido", "LOST")]
    thirty = create(ana, code="treinta", stages=[*many, *ends])
    assert thirty.status_code == 201 and len(thirty.json()["stages"]) == 30
    over = create(ana, code="treinta-y-uno", stages=[spec("extra"), *many, *ends])
    assert fail(over) == (400, "VALIDATION_ERROR", ["stages"])
    monkeypatch.setattr(inputs, "PIPELINES_MAX", 3)
    pipeline(crm, "b", code="solo-b")  # los de otra organización no cuentan
    assert create(ana, code="dos").status_code == 201  # ya van 2
    assert create(ana, code="tres").status_code == 201  # el tercero cabe
    full = create(ana, code="cuatro")
    assert fail(full) == (400, "VALIDATION_ERROR", ["_"]), full.content
    migrator.execute("UPDATE pipelines SET deleted_at = now() WHERE code = 'tres'")
    assert create(ana, code="cuatro").status_code == 201  # un borrado no cuenta
    assert migrator.execute("SELECT count(*) FROM pipelines WHERE code = 'cuatro'").fetchone() == (
        1,
    )


def test_the_invariants_and_the_code_come_back_as_stable_codes(
    crm: SimpleNamespace, migrator: psycopg.Connection[Any]
) -> None:
    ana = signed(crm.ana)
    assert create(ana).status_code == 201
    before = stored(migrator)
    no_won, no_lost, two_won = (
        basic()[:2] + basic()[3:],
        basic()[:3],
        [*basic(), spec("otra", "WON")],
    )
    for stages in (no_won, no_lost, two_won, [spec("a")]):
        response = create(ana, code="otro", stages=stages)
        assert response.status_code == 409, response.content
        assert (
            response.json()["code"] == "PIPELINE_INVARIANT"
            and "otro" not in response.content.decode()
        )
    for code in ("mayoristas", "MAYORISTAS"):
        taken = create(ana, code=code)
        assert (taken.status_code, taken.json()["code"]) == (409, "CODE_TAKEN"), code
    closed = create(
        ana,
        code="otro",
        stages=[spec("a", "WON", default_probability=50), *basic()[:2], basic()[3]],
    )
    assert fail(closed) == (400, "VALIDATION_ERROR", ["stages"])  # WON es 100 %
    repeated = create(ana, code="otro", stages=[*basic(), spec("nuevo")])
    assert fail(repeated) == (400, "VALIDATION_ERROR", ["stages"])
    assert stored(migrator) == before


HOSTILE = [  # cuerpos que ningún serializador espera: 400 o 415, nunca un 500
    "null", "[]", '"x"', "1", "true", "{", "", "{}", '{"name": "' + "x" * 200_000 + '"}',
    "[" * 200 + "]" * 200, '{"a":' * 150 + "1" + "}" * 150, '{"name":"\\ud800"}',
    '{"stages":' + "[" * 120 + "]" * 120 + "}", '{"version":1e999}', '{"version":NaN}',
]  # fmt: skip


@pytest.mark.parametrize("raw", HOSTILE)
def test_hostile_bodies_are_never_a_500(crm: SimpleNamespace, raw: str) -> None:
    ana = signed(crm.ana)
    response = send(ana, "post", PIPELINES, raw)
    assert response.status_code in {400, 415}, response.status_code
    assert response.json()["code"] in {"VALIDATION_ERROR", "PARSE_ERROR", "UNSUPPORTED_MEDIA_TYPE"}
    form = ana.post(PIPELINES, {"name": "x"}, headers={"X-CSRFToken": "t" * 32})
    assert form.status_code in {403, 415}


def http_race(
    monkeypatch: pytest.MonkeyPatch,
    bodies: list[dict[str, Any]],
    method: str = "post",
    path: str | list[str] = PIPELINES,
) -> list[Any]:
    """La primera petición retiene su transacción (tras auditar) y la segunda espera su bloqueo."""
    wrote, release, outcome = threading.Event(), threading.Event(), list[Any]([None, None])
    real = audit_services.record

    def hold(*args: Any, **kwargs: Any) -> Any:
        done = real(*args, **kwargs)
        if threading.current_thread().name == "first":
            wrote.set()
            release.wait(timeout=10)
        return done

    monkeypatch.setattr(commands, "record", hold, raising=True)
    owner = User.objects.get(email="a@example.com")

    def run(index: int) -> None:
        try:
            response = send(
                signed(owner), method, path if isinstance(path, str) else path[index], bodies[index]
            )
            outcome[index] = (response.status_code, response.json().get("code"))
        except Exception as error:  # noqa: BLE001 — el resultado lo comprueba cada test
            outcome[index] = type(error).__name__
        finally:
            wrote.set()
            connection.close()

    names = ("first", "second")
    first, second = (threading.Thread(target=run, args=(i,), name=n) for i, n in enumerate(names))
    first.start()
    assert wrote.wait(timeout=10)
    second.start()
    second.join(timeout=1)
    waited = second.is_alive()
    release.set()
    for thread in (first, second):
        thread.join(timeout=10)
    assert waited, "la segunda no esperó a la primera"
    return outcome


def test_two_creations_at_once_never_pass_the_cap_or_repeat_a_code(
    crm: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    same = http_race(monkeypatch, [body(), body()])
    assert same == [(201, "mayoristas"), (409, "CODE_TAKEN")]
    monkeypatch.setattr(inputs, "PIPELINES_MAX", 2)  # ya hay uno: cabe uno más
    capped = http_race(monkeypatch, [body(code="uno"), body(code="dos")])
    assert capped == [(201, "uno"), (400, "VALIDATION_ERROR")]
    with tenant_scope(ctx(crm.a)):
        assert Pipeline.objects.count() == 2


def test_fifty_pipelines_fit_and_the_fifty_first_is_a_400(
    crm: SimpleNamespace, migrator: psycopg.Connection[Any]
) -> None:
    ana = signed(crm.ana)
    assert create(ana).status_code == 201
    migrator.execute(  # 48 más, sin etapas: el tope solo cuenta pipelines
        "INSERT INTO pipelines (id, organization_id, name, code, kind, currency, is_default, "
        "sort_order, is_active, created_at, updated_at, version) SELECT gen_random_uuid(), "
        "organization_id, name, code || '-' || n, kind, currency, false, 0, n % 2 = 0, now(), "
        "now(), 1 FROM pipelines, generate_series(1, 48) AS n WHERE code = 'mayoristas'"
    )
    assert create(ana, code="cincuenta").status_code == 201
    assert stored(migrator)[0] == 50  # la mitad de los insertados, inactivos: cuentan igual
    full = create(ana, code="cincuenta-y-uno")
    assert fail(full) == (400, "VALIDATION_ERROR", ["_"]), full.content
    assert full.json()["fields"]["_"][0]["message"] == "La organización ya tiene 50 pipelines."
    assert stored(migrator)[0] == 50


def test_an_inactive_pipeline_still_counts_and_the_api_frees_no_room(
    crm: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    ana = signed(crm.ana)
    monkeypatch.setattr(inputs, "PIPELINES_MAX", 2)
    assert create(ana).status_code == 201
    second = create(ana, code="segundo").json()
    path = f"{PIPELINES}{second['id']}/"
    off = send(ana, "patch", path, {"version": 1, "is_active": False})
    assert (off.status_code, off.json()["is_active"]) == (200, False)
    assert fail(create(ana, code="tercero")) == (400, "VALIDATION_ERROR", ["_"])
    reused = send(ana, "patch", path, {"version": 2, "is_active": True, "name": "Tercero"})
    assert (reused.status_code, reused.json()["name"]) == (200, "Tercero")  # se reutiliza
    assert len(read(ana, PIPELINES)["results"]) == 2


def test_an_unexpected_value_error_is_a_generic_400_that_echoes_nothing(
    crm: SimpleNamespace, migrator: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise ValueError("Ñu \u202e\x00 secreto")

    monkeypatch.setattr(commands, "create_pipeline", refuse)
    before = stored(migrator)
    response = create(signed(crm.ana))
    assert response.status_code == 400, response.content
    assert response.json() == {
        "code": "VALIDATION_ERROR",
        "fields": {"_": [{"code": "invalid", "message": "Dato no admitido."}]},
    }
    assert stored(migrator) == before


def test_an_unknown_key_comes_back_short_and_printable(crm: SimpleNamespace) -> None:
    ana = signed(crm.ana)
    huge = "x" * 100_000 + "\x00\u202e"
    response = send(ana, "post", PIPELINES, {**body(), huge: 1, "Ñu\u202e": 2, "a\x00\x1b": 3})
    assert response.status_code == 400 and len(response.content) < 400, len(response.content)
    assert set(response.json()["fields"]) == {"x" * 64 + "...", "?u?", "a??"}
    for echoed in ("\x00", "\u202e", "Ñ", "\\u0000", "\\u202e", "\\u00d1", "\\u001b", "x" * 65):
        assert echoed not in response.content.decode()


def test_a_name_of_hangul_fillers_only_is_refused() -> None:
    for filler in ("\u3164", "\u115f\u1160", "\uffa0 \u3164"):
        with pytest.raises(ValueError, match="alguna letra o cifra"):
            clean_name(filler, 100)
    assert clean_name("\u3164 Ventas", 100) == "\u3164 Ventas"  # con algo que se ve, vale

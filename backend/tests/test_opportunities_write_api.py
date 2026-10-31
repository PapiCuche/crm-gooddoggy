"""F5-04d: `POST …/opportunities/`, con sus permisos, el alcance del contacto, sus errores de
dominio y los datos hostiles. Middleware, sesión, motor de autorización y
PostgreSQL con el rol `crm_app`."""

from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from django.test import Client
from django.utils import timezone

from apps.deals.models import Pipeline
from core.tenancy.scope import tenant_scope
from tests import test_opportunity_scopes as scopes
from tests.factories import make_user
from tests.test_authorization import give
from tests.test_member_branch import DENIED
from tests.test_memberships import ctx, join
from tests.test_opportunities_api import OPPS
from tests.test_opportunities_tables import variant
from tests.test_opportunity_detail_api import DETAIL_FIELDS
from tests.test_self_context import NOT_FOUND, reply, signed
from tests.test_team_writes import send

crm, sales, pipelines = scopes.crm, scopes.sales, scopes.pipelines
real_stack = scopes.real_stack
pytestmark = pytest.mark.usefixtures("tenant_db")
CSRF_FAILED = (403, b'{"code":"CSRF_FAILED"}')
UNAUTHENTICATED = (401, b'{"code":"NOT_AUTHENTICATED"}')
ROWS = "SELECT (SELECT count(*) FROM opportunities), (SELECT count(*) FROM audit_logs)"
NOT_OURS = {  # nada de esto se toma del cliente
    "organization_id": str(uuid4()),
    "assigned_user_id": str(uuid4()),
    "number": "OPP-999999",
    "status": "WON",
    "stage_id": str(uuid4()),
    "created_by_user_id": str(uuid4()),
    "version": 3,
}


def create(client: Client, body: Any) -> Any:
    return send(client, "post", OPPS, body)


def fail(response: Any) -> tuple[int, str | None, list[str]]:
    sent = response.json()
    return response.status_code, sent.get("code"), sorted(sent.get("fields", {}))


def rows(migrator: psycopg.Connection[Any]) -> Any:
    return migrator.execute(ROWS).fetchone()


def acts(migrator: psycopg.Connection[Any]) -> list[tuple[str, UUID]]:
    found = migrator.execute(
        "SELECT action, actor_id FROM audit_logs WHERE entity_type = 'opportunity' "
        "ORDER BY occurred_at, id"
    )
    return found.fetchall()


def test_the_owner_creates_an_opportunity_and_gets_it_back_like_the_detail(
    crm: SimpleNamespace, migrator: psycopg.Connection[Any]
) -> None:
    sku = variant(crm.a).pk
    made = create(
        signed(crm.ana),
        {
            "contact_id": str(crm.ids["plain"]),
            "title": "  Juan   Perez DNI 12345678 ",
            "amount": "1500.5",
            "priority": "HIGH",
            "source": "WALK_IN",
            "next_action": "Llamar",
            "next_action_at": "2026-11-01T09:00:00-05:00",
            "expected_close_date": "2026-12-01",
            "items": [{"variant_id": str(sku), "quantity": 2, "estimated_unit_price": "750.25"}],
        },
    )
    assert made.status_code == 201, made.content
    body = made.json()
    assert set(body) == DETAIL_FIELDS
    assert (body["number"], body["title"], body["amount"], body["currency"]) == (
        "OPP-000001", "Juan Perez DNI 12345678", "1500.50", "PEN",
    )  # fmt: skip
    assert (body["status"], body["probability"], body["version"], body["priority"]) == (
        "OPEN", 10, 1, "HIGH",
    )  # fmt: skip
    assert body["assigned_user_id"] == str(crm.ana.pk)  # quien crea
    assert body["contact"] == {"id": str(crm.ids["plain"]), "display_name": "Sin nadie"}
    assert body["expected_close_date"] == "2026-12-01" and body["source"] == "WALK_IN"
    assert body["next_action_at"].startswith("2026-11-01T14:00:00")  # en UTC
    assert [(i["quantity"], i["estimated_unit_price"]) for i in body["items"]] == [(2, "750.25")]
    assert [(h["from_stage"], h["to_stage"]["name"]) for h in body["history"]] == [(None, "Nuevo")]
    assert acts(migrator) == [("opportunity.created", crm.ana.pk)]  # por HTTP, con el actor
    assert signed(crm.ana).get(f"{OPPS}{body['id']}/").json() == body  # y el detalle lo confirma


def test_a_minimal_create_takes_the_default_pipeline_and_the_creator(
    crm: SimpleNamespace,
) -> None:
    body = create(signed(crm.vera), {"contact_id": str(crm.ids["owned"]), "title": "iPhone"})
    assert body.status_code == 201 and body.json()["amount"] == "0.00"
    assert body.json()["assigned_user_id"] == str(crm.vera.pk)
    assert signed(crm.vera).get(OPPS).json()["results"][0]["id"] == body.json()["id"]


def test_a_creator_needs_to_see_the_contact_and_every_failure_looks_the_same(
    crm: SimpleNamespace, migrator: psycopg.Connection[Any]
) -> None:
    from apps.contacts.models import Contact

    with tenant_scope(ctx(crm.a)):
        Contact.objects.filter(pk=crm.ids["created"]).update(deleted_at=timezone.now())
    vera, before = signed(crm.vera), rows(migrator)
    bad = [  # fuera de su alcance, inexistente, borrado y de otra organización
        crm.ids["plain"], uuid4(), crm.ids["created"], crm.ids["b"], crm.ids["lima"],
    ]  # fmt: skip
    answers = {create(vera, {"contact_id": str(c), "title": "x"}).content for c in bad}
    assert len(answers) == 1  # el mismo 400 con el mismo cuerpo: no sirve para sondear
    assert fail(create(vera, {"contact_id": str(bad[0]), "title": "x"})) == (
        400, "VALIDATION_ERROR", ["contact_id"],
    )  # fmt: skip
    assert rows(migrator) == before
    assert create(vera, {"contact_id": str(crm.ids["owned"]), "title": "x"}).status_code == 201
    sam = signed(crm.sam)  # `TEAM` de contactos es lo propio (D-F4-01)
    assert create(sam, {"contact_id": str(crm.ids["owned"]), "title": "x"}).status_code == 400
    assert create(sam, {"contact_id": str(crm.ids["sam"]), "title": "x"}).status_code == 201


def test_creating_needs_both_permissions_before_reading_anything(
    crm: SimpleNamespace, migrator: psycopg.Connection[Any]
) -> None:
    body, before = {"contact_id": str(crm.ids["plain"]), "title": "x"}, rows(migrator)
    bea = signed(crm.bea)
    assert reply(create(bea, body)) == DENIED  # sin rol
    give(
        crm.a, crm.m["bea"], {"contacts.view": "ORGANIZATION", "opportunities.view": "ORGANIZATION"}
    )
    assert reply(create(bea, body)) == DENIED  # ver no es crear
    assert reply(create(bea, "basura")) == DENIED  # y el cuerpo no se lee antes del permiso
    give(crm.a, crm.m["bea"], {"opportunities.create": None})
    assert create(bea, body).status_code == 201
    assert rows(migrator)[0] == before[0] + 1
    nobody = make_user(email="nadie@example.com")  # crear sin ver contactos
    give(crm.a, join(crm.a, nobody).pk, {"opportunities.create": None})
    assert reply(create(signed(nobody), body)) == DENIED
    assert reply(create(Client(), body)) == UNAUTHENTICATED
    assert reply(signed(crm.ana).post(OPPS, body, "application/json")) == CSRF_FAILED
    assert reply(create(signed(crm.luis), body)) == NOT_FOUND  # no es miembro de A
    assert rows(migrator)[0] == before[0] + 1


def test_the_client_never_chooses_the_owner_the_stage_or_the_number(
    crm: SimpleNamespace, migrator: psycopg.Connection[Any]
) -> None:
    ana, before = signed(crm.ana), rows(migrator)
    sent = {"contact_id": str(crm.ids["plain"]), "title": "x"}
    for key, value in NOT_OURS.items():
        assert fail(create(ana, {**sent, key: value})) == (400, "VALIDATION_ERROR", [key]), key
    assert rows(migrator) == before


def test_a_pipeline_is_chosen_only_among_the_live_active_ones_of_the_organization(
    crm: SimpleNamespace,
) -> None:
    from apps.deals.models import PipelineStage

    with tenant_scope(ctx(crm.a)):
        mine = Pipeline.objects.get()
        extra = Pipeline.objects.create(name="Otro", code="otro", kind="RETAIL", currency="PEN")
        PipelineStage.objects.create(
            pipeline=extra, name="Primera", code="primera", stage_type="OPEN",
            default_probability=5, color="#112233",
        )  # fmt: skip
        sleeping = Pipeline.objects.create(
            name="Dormido", code="dormido", kind="RETAIL", currency="PEN", is_active=False
        )
    with tenant_scope(ctx(crm.b)):
        foreign = Pipeline.objects.get()
    ana, base = signed(crm.ana), {"contact_id": str(crm.ids["plain"]), "title": "x"}
    ok = create(ana, {**base, "pipeline_id": str(extra.pk)})
    assert ok.status_code == 201 and ok.json()["pipeline_id"] == str(extra.pk)
    assert ok.json()["stage"]["name"] == "Primera" and ok.json()["probability"] == 5
    assert create(ana, {**base, "pipeline_id": str(mine.pk)}).status_code == 201
    answers = {
        create(ana, {**base, "pipeline_id": str(p)}).content
        for p in (sleeping.pk, foreign.pk, uuid4())
    }
    assert len(answers) == 1  # el mismo error para el dormido, el ajeno y el inexistente
    assert fail(create(ana, {**base, "pipeline_id": str(sleeping.pk)})) == (
        400, "VALIDATION_ERROR", ["pipeline_id"],
    )  # fmt: skip


def test_lines_need_active_variants_and_report_them_on_items(
    crm: SimpleNamespace, migrator: psycopg.Connection[Any]
) -> None:
    ana, before = signed(crm.ana), rows(migrator)
    base = {"contact_id": str(crm.ids["plain"]), "title": "x"}
    other = variant(crm.b, "OTRA").pk
    for sku in (uuid4(), other):  # inexistente y de otra organización
        bad = create(ana, {**base, "items": [{"variant_id": str(sku), "quantity": 1}]})
        assert fail(bad) == (400, "VALIDATION_ERROR", ["items"])
    assert rows(migrator) == before
    sku = variant(crm.a).pk
    ok = create(ana, {**base, "items": [{"variant_id": str(sku), "quantity": 1}]})
    assert ok.status_code == 201 and ok.json()["items"][0]["notes"] == ""


HUGE = "9" * 100_000
CREATE_HOSTILE: list[tuple[str, Any]] = [
    ("contact_id", None), ("contact_id", 5), ("contact_id", "x"), ("contact_id", [1]),
    ("title", None), ("title", ""), ("title", "  "), ("title", "a\nb"), ("title", "a\x00b"),
    ("title", "\u202e"), ("title", "x" * 201), ("title", 5), ("title", True), ("title", ["x"]),
    ("title", HUGE), ("title", "---"),
    ("amount", 5), ("amount", 5.5), ("amount", True), ("amount", None), ("amount", ""),
    ("amount", "abc"), ("amount", "-1"), ("amount", "1e3"), ("amount", "NaN"),
    ("amount", "Infinity"), ("amount", "1" * 13), ("amount", "1.234"), ("amount", HUGE),
    ("priority", "urgent"), ("priority", 1), ("priority", None), ("priority", ["LOW"]),
    ("source", "X"), ("source", 0), ("source", None),
    ("next_action", ""), ("next_action", 5), ("next_action", "x" * 201), ("next_action", "a\nb"),
    ("next_action", ["x"]), ("next_action", HUGE),
    ("next_action_at", "2026-11-01T09:00:00"), ("next_action_at", "x"), ("next_action_at", 5),
    ("next_action_at", "3000-01-01T00:00:00Z"), ("next_action_at", "1999-12-31T23:59:59Z"),
    ("next_action_at", "2026-11-01"), ("next_action_at", "2026-13-01T00:00:00Z"),
    ("next_action_at", True), ("next_action_at", ""),
    ("expected_close_date", "2026-13-01"), ("expected_close_date", "20261201"),
    ("expected_close_date", "2026-12-01T00:00:00Z"), ("expected_close_date", 5),
    ("expected_close_date", "1999-01-01"), ("expected_close_date", "2101-01-01"),
    ("expected_close_date", ""), ("expected_close_date", "２０２６-１２-０１"),
    ("items", None), ("items", {}), ("items", "x"), ("items", [None]), ("items", [[]]),
    ("items", [{}]), ("items", [{"variant_id": "x", "quantity": 1}]),
    ("items", [{"variant_id": str(uuid4())}]),
    ("items", [{"variant_id": str(uuid4()), "quantity": 0}]),
    ("items", [{"variant_id": str(uuid4()), "quantity": "1"}]),
    ("items", [{"variant_id": str(uuid4()), "quantity": 1.5}]),
    ("items", [{"variant_id": str(uuid4()), "quantity": True}]),
    ("items", [{"variant_id": str(uuid4()), "quantity": 1_000_001}]),
    ("items", [{"variant_id": str(uuid4()), "quantity": 10**30}]),
    ("items", [{"variant_id": str(uuid4()), "quantity": 1, "price": "1"}]),
    ("items", [{"variant_id": str(uuid4()), "quantity": 1, "estimated_unit_price": 5}]),
    ("items", [{"variant_id": str(uuid4()), "quantity": 1, "estimated_unit_price": "-1"}]),
    ("items", [{"variant_id": str(uuid4()), "quantity": 1, "notes": "a\nb"}]),
    ("items", [{"variant_id": str(uuid4()), "quantity": 1, "notes": "x" * 1001}]),
    ("items", [{"variant_id": (v := str(uuid4())), "quantity": 1}] * 2),
    ("items", [{"variant_id": str(uuid4()), "quantity": 1}] * 51),
    ("items", [{"variant_id": str(uuid4()), "quantity": 1_000_000,
                "estimated_unit_price": "999999999999.99"}]),
]  # fmt: skip


@pytest.mark.parametrize(("field", "value"), CREATE_HOSTILE)
def test_a_hostile_create_is_a_400_on_its_field_and_writes_nothing(
    crm: SimpleNamespace, migrator: psycopg.Connection[Any], field: str, value: Any
) -> None:
    ana, before = signed(crm.ana), rows(migrator)
    sent = {"contact_id": str(crm.ids["plain"]), "title": "x", field: value}
    response = create(ana, sent)
    assert fail(response) == (400, "VALIDATION_ERROR", [field]), response.content
    assert rows(migrator) == before
    if isinstance(value, str) and len(value) > 3 and value != HUGE:
        assert value not in response.text  # no repite lo recibido


@pytest.mark.parametrize("raw", ["null", "[]", '"x"', "5", "{", "", "[{}]", "true"])
def test_a_body_that_is_not_an_object_is_a_400(crm: SimpleNamespace, raw: str) -> None:
    client = signed(crm.ana)
    client.cookies["csrftoken"] = "t" * 32
    response = client.post(OPPS, raw, "application/json", headers={"X-CSRFToken": "t" * 32})
    assert response.status_code == 400 and response.json()["code"] in {
        "VALIDATION_ERROR", "PARSE_ERROR",
    }  # fmt: skip
    assert response.status_code != 500

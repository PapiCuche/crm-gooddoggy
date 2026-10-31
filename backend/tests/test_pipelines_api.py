"""F5-02: `opportunities.view` y `pipelines.manage`, y la lectura de pipelines (con sus etapas) y
motivos de pérdida (`…/pipelines/`, `…/lost-reasons/`). Middleware, sesión, motor de autorización
y PostgreSQL con el rol `crm_app`. Las escrituras: `test_pipelines_write_api` y
`test_lost_reasons_write_api`."""

from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

from apps.access.catalog import BY_CODE, ROLE_TEMPLATES, Scope
from apps.access.scopes import Everyone, policy_for
from apps.access.selectors import can, execution_context, scoped
from apps.deals import commands, selectors
from apps.deals.models import LostReason, Pipeline, PipelineStage
from core.tenancy.scope import tenant_scope
from tests import test_contacts_api, test_self_context
from tests.test_authorization import give
from tests.test_member_branch import DENIED
from tests.test_memberships import ctx
from tests.test_pipeline_commands import basic, spec
from tests.test_self_context import NOT_FOUND, reply, signed

crm, real_stack = test_contacts_api.crm, test_self_context.real_stack
pytestmark = pytest.mark.usefixtures("tenant_db")
PIPELINES, REASONS = "/api/v1/o/org-a/pipelines/", "/api/v1/o/org-a/lost-reasons/"
PIPELINE_FIELDS = {
    "id", "name", "code", "kind", "currency", "is_default", "is_active", "sort_order", "version",
    "stages",
}  # fmt: skip
STAGE_FIELDS = {
    "id", "name", "code", "sort_order", "stage_type", "default_probability", "stale_after_days",
    "color", "requires_fields", "is_active",
}  # fmt: skip
REASON_FIELDS = {"id", "name", "code", "sort_order", "is_active", "version"}
UNAUTHENTICATED = (401, b'{"code":"NOT_AUTHENTICATED"}')


def pipeline(crm: SimpleNamespace, org: str = "a", code: str = "retail", **fields: Any) -> Pipeline:
    owner = crm.ana if org == "a" else crm.luis
    wanted = {"name": code.title(), "code": code, "kind": "RETAIL", "stages": basic(), **fields}
    with tenant_scope(ctx(getattr(crm, org), owner)) as tenant:
        made: Pipeline = commands.create_pipeline(tenant, **wanted)
    return made


def reason(crm: SimpleNamespace, code: str, org: str = "a", **fields: Any) -> LostReason:
    with tenant_scope(ctx(getattr(crm, org), crm.ana if org == "a" else crm.luis)) as tenant:
        made: LostReason = commands.create_lost_reason(tenant, name=code.title(), code=code)
        for field, value in fields.items():
            setattr(made, field, value)
        made.save(update_fields=list(fields))
    return made


def read(client: Client, url: str, **query: Any) -> dict[str, Any]:
    response = client.get(url, query)
    assert response.status_code == 200, response.content
    body: dict[str, Any] = response.json()
    return body


def test_the_permissions_and_who_gets_them() -> None:
    view, manage = BY_CODE["opportunities.view"], BY_CODE["pipelines.manage"]
    assert (view.module, view.is_sensitive, view.supports_scope) == ("opportunities", False, True)
    assert (manage.module, manage.is_sensitive, manage.supports_scope) == (
        "opportunities",
        False,  # 03 §H no lo marca sensible
        False,
    )
    held = {t.code: t.grants for t in ROLE_TEMPLATES}
    assert held["owner"]["opportunities.view"] == Scope.ORGANIZATION  # ADR-018
    assert "pipelines.manage" in held["owner"]
    assert [code for code, grants in held.items() if "pipelines.manage" in grants] == [
        "owner",
        "admin",
    ]
    assert {code: grants.get("opportunities.view") for code, grants in held.items()} == {
        "owner": Scope.ORGANIZATION,
        "admin": Scope.ORGANIZATION,
        "supervisor": Scope.TEAM,  # la matriz de 03 §H: «leads / opportunities»
        "seller": Scope.OWN,
    }


def test_the_configuration_models_are_visible_to_every_scope() -> None:
    for model in (Pipeline, PipelineStage, LostReason):
        assert policy_for(model) == Everyone()


def test_the_owner_reads_the_default_pipeline_with_its_stages_in_order(
    crm: SimpleNamespace,
) -> None:
    made = pipeline(crm)
    other = pipeline(crm, code="mayoristas", is_default=False)
    body = read(signed(crm.ana), PIPELINES)
    assert body["next"] is None
    rows = body["results"]
    assert [row["id"] for row in rows] == [str(made.pk), str(other.pk)]  # por creación
    first = rows[0]
    assert set(first) == PIPELINE_FIELDS
    assert (first["code"], first["kind"], first["currency"], first["is_default"]) == (
        "retail",
        "RETAIL",
        "PEN",
        True,
    )
    assert (first["is_active"], first["version"], rows[1]["is_default"]) == (True, 1, False)
    stages = first["stages"]
    assert all(set(stage) == STAGE_FIELDS for stage in stages)
    assert [(s["code"], s["sort_order"], s["stage_type"]) for s in stages] == [
        ("nuevo", 0, "OPEN"),
        ("cotizado", 1, "OPEN"),
        ("ganado", 2, "WON"),
        ("perdido", 3, "LOST"),
    ]
    assert (stages[1]["stale_after_days"], stages[0]["stale_after_days"]) == (7, None)
    assert (stages[2]["default_probability"], stages[0]["color"]) == (100, "#112233")
    assert stages[0]["requires_fields"] == {} and stages[0]["is_active"] is True


def test_inactive_stages_and_pipelines_are_listed_and_deleted_ones_are_not(
    crm: SimpleNamespace, migrator: psycopg.Connection[Any]
) -> None:
    extra = [*basic(), spec("viejo", is_active=False)]
    made = pipeline(crm, stages=extra)
    gone = pipeline(crm, code="borrado")
    pipeline(crm, code="dormido", is_default=False)
    migrator.execute("UPDATE pipelines SET is_active = false WHERE code = 'dormido'")
    migrator.execute("UPDATE pipelines SET deleted_at = now() WHERE id = %s", [gone.pk])
    rows = {row["code"]: row for row in read(signed(crm.ana), PIPELINES)["results"]}
    assert set(rows) == {"retail", "dormido"}
    assert rows["dormido"]["is_active"] is False
    assert [(s["code"], s["is_active"]) for s in rows["retail"]["stages"]][-1] == ("viejo", False)
    assert str(made.pk) == rows["retail"]["id"]


def test_lost_reasons_are_listed_with_their_state_and_deleted_ones_are_not(
    crm: SimpleNamespace, migrator: psycopg.Connection[Any]
) -> None:
    reason(crm, "PRICE")
    reason(crm, "OLD", is_active=False)
    reason(crm, "GONE")
    migrator.execute("UPDATE lost_reasons SET deleted_at = now() WHERE code = 'GONE'")
    rows = read(signed(crm.ana), REASONS)["results"]
    assert all(set(row) == REASON_FIELDS for row in rows)
    assert [(row["code"], row["name"], row["is_active"], row["version"]) for row in rows] == [
        ("PRICE", "Price", True, 1),
        ("OLD", "Old", False, 1),
    ]


def test_reading_needs_opportunities_view_in_any_scope(crm: SimpleNamespace) -> None:
    pipeline(crm)
    reason(crm, "PRICE")
    for url in (PIPELINES, REASONS):
        for user in (crm.ana, crm.vera, crm.sam):  # Owner, `OWN` y `TEAM`: la misma lista entera
            assert len(read(signed(user), url)["results"]) == 1, (url, user.email)
        assert reply(signed(crm.bea).get(url)) == DENIED  # sin el permiso
        assert reply(Client().get(url)) == UNAUTHENTICATED
    give(crm.a, crm.m["bea"], {"pipelines.manage": None, "contacts.view": "ORGANIZATION"})
    for url in (PIPELINES, REASONS):  # administrar o ver contactos no es ver oportunidades
        assert reply(signed(crm.bea).get(url)) == DENIED
    give(crm.a, crm.m["bea"], {"opportunities.view": "BRANCH"})
    assert len(read(signed(crm.bea), PIPELINES)["results"]) == 1  # ni el alcance recorta


def test_another_organization_does_not_exist_and_sees_only_its_own(
    crm: SimpleNamespace,
) -> None:
    pipeline(crm)
    reason(crm, "PRICE")
    pipeline(crm, "b", code="solo-b")
    reason(crm, "SOLO_B", "b")
    give(crm.b, crm.m_luis, {"opportunities.view": "ORGANIZATION"})
    luis, ana = signed(crm.luis), signed(crm.ana)
    for url in (PIPELINES, REASONS):
        assert reply(luis.get(url)) == NOT_FOUND  # no es miembro de A
        assert reply(luis.get(url.replace("org-a", "no-existe"))) == NOT_FOUND
        assert reply(ana.get(url.replace("org-a", "org-b"))) == NOT_FOUND
    mine = read(luis, PIPELINES.replace("org-a", "org-b"))["results"]
    assert [row["code"] for row in mine] == ["solo-b"]
    codes = [row["code"] for row in read(luis, REASONS.replace("org-a", "org-b"))["results"]]
    assert codes == ["SOLO_B"]
    assert [row["code"] for row in read(ana, PIPELINES)["results"]] == ["retail"]
    assert [row["code"] for row in read(ana, REASONS)["results"]] == ["PRICE"]
    with tenant_scope(ctx(crm.a)):  # y por debajo de la API, RLS
        assert set(Pipeline.objects.values_list("code", flat=True)) == {"retail"}
        assert set(PipelineStage.objects.values_list("pipeline__code", flat=True)) == {"retail"}


def test_the_list_costs_the_same_queries_however_many_stages_and_pipelines(
    crm: SimpleNamespace,
) -> None:
    ana = signed(crm.ana)
    pipeline(crm)

    def queries() -> int:
        with CaptureQueriesContext(connection) as captured:
            read(ana, PIPELINES)
        return len(captured)

    one = queries()
    many = [spec(f"etapa-{i}") for i in range(30)]
    for i in range(5):
        pipeline(crm, code=f"p{i}", is_default=False, stages=[*many, *basic()[2:]])
    assert queries() == one


def test_the_pages_carry_their_stages_and_a_bad_limit_is_a_400(crm: SimpleNamespace) -> None:
    pipeline(crm, code="a1")
    pipeline(crm, code="a2", is_default=False)
    ana = signed(crm.ana)
    first = read(ana, PIPELINES, limit=1)
    assert [row["code"] for row in first["results"]] == ["a1"] and first["next"]
    assert len(first["results"][0]["stages"]) == 4
    second = read(ana, PIPELINES, limit=1, cursor=first["next"])
    assert [row["code"] for row in second["results"]] == ["a2"] and second["next"] is None
    for bad in ("0", "201", "x"):
        response = ana.get(PIPELINES, {"limit": bad})
        assert response.status_code == 400 and set(response.json()["fields"]) == {"limit"}


def test_stages_with_the_same_order_come_out_by_name_then_id(crm: SimpleNamespace) -> None:
    made = pipeline(crm)
    with tenant_scope(ctx(crm.a, crm.ana)):
        stages = PipelineStage.objects.filter(pipeline_id=made.pk)
        stages.update(sort_order=0)
        for code in ("perdido", "ganado"):  # mismo nombre, y la fila nueva no va primero por id
            stages.filter(code=code).update(name="Cierre")
        expected = list(stages.order_by("name", "id").values_list("code", flat=True))
        found = selectors.pipelines().get(pk=made.pk)
        assert [s.code for s in found.stages.all()] == expected
    api = read(signed(crm.ana), PIPELINES)["results"][0]["stages"]
    assert [stage["code"] for stage in api] == expected


@pytest.mark.parametrize("scope", [Scope.OWN, Scope.TEAM, Scope.BRANCH])
def test_everyone_allows_and_lists_the_whole_organization_under_any_scope(
    crm: SimpleNamespace, scope: Scope
) -> None:
    pipeline(crm)
    reason(crm, "PRICE")
    pipeline(crm, "b", code="solo-b")
    with tenant_scope(ctx(crm.b, crm.luis)):
        foreign = Pipeline.objects.get()
    with tenant_scope(ctx(crm.a, crm.ana)) as tenant:  # ana sin equipos ni sucursal
        narrow = replace(
            execution_context(tenant), permissions={"opportunities.view": frozenset({scope})}
        )
        for model in (Pipeline, PipelineStage, LostReason):
            rows = list(model.objects.all())
            assert rows and all(can(narrow, "opportunities.view", row) for row in rows)
            listed = scoped(narrow, "opportunities.view", model.objects.all())
            assert set(listed) == set(rows)  # ni el alcance recorta ni entra otra organización
        assert not can(narrow, "opportunities.view", foreign)  # lo de B, nunca


def test_head_on_the_read_routes_follows_the_same_rules(crm: SimpleNamespace) -> None:
    pipeline(crm)
    reason(crm, "PRICE")
    for url in (PIPELINES, REASONS):
        assert [signed(user).head(url).status_code for user in (crm.ana, crm.vera)] == [200, 200]
        assert signed(crm.ana).head(url).content == b""
        assert signed(crm.bea).head(url).status_code == 403  # sin `opportunities.view`
        assert signed(crm.luis).head(url).status_code == 404  # no es miembro de A
        assert Client().head(url).status_code == 401


def test_the_lost_reasons_page_by_creation_and_cost_the_same_queries(
    crm: SimpleNamespace,
) -> None:
    ana = signed(crm.ana)
    for code in ("A1", "A2", "A3"):
        reason(crm, code, sort_order=9 - len(code))
    first = read(ana, REASONS, limit=2)
    assert [row["code"] for row in first["results"]] == ["A1", "A2"] and first["next"]
    second = read(ana, REASONS, limit=2, cursor=first["next"])
    assert [row["code"] for row in second["results"]] == ["A3"] and second["next"] is None
    for bad in ("0", "201", "x"):
        response = ana.get(REASONS, {"limit": bad})
        assert response.status_code == 400 and set(response.json()["fields"]) == {"limit"}

    def queries() -> int:
        with CaptureQueriesContext(connection) as captured:
            read(ana, REASONS)
        return len(captured)

    few = queries()
    for index in range(20):
        reason(crm, f"B{index}")
    assert queries() == few

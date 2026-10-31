"""F5-04b: la lista de oportunidades (`…/opportunities/`): alcance por responsable, filtros,
nombre del contacto, paginación y consultas constantes. Middleware, sesión, motor de autorización
y PostgreSQL con el rol `crm_app`. Permisos y política: `test_opportunity_scopes`."""

from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.deals.models import Opportunity
from core.tenancy.scope import tenant_scope
from tests import test_opportunity_scopes as scopes
from tests.test_authorization import give
from tests.test_member_branch import DENIED
from tests.test_memberships import ctx
from tests.test_opportunities_tables import make, stage
from tests.test_self_context import NOT_FOUND, reply, signed
from tests.test_team_members import add
from tests.test_teams import team

crm, sales, pipelines = scopes.crm, scopes.sales, scopes.pipelines
real_stack, pick = scopes.real_stack, scopes.pick
pytestmark = pytest.mark.usefixtures("tenant_db")
OPPS = "/api/v1/o/org-a/opportunities/"
UNAUTHENTICATED = (401, b'{"code":"NOT_AUTHENTICATED"}')
LIST_FIELDS = {
    "id", "number", "title", "contact", "pipeline_id", "stage", "status", "amount", "currency",
    "probability", "priority", "assigned_user_id", "next_action", "expected_close_date",
    "stage_entered_at", "last_activity_at", "won_at", "lost_at", "lost_reason", "version",
}  # fmt: skip


def listed(client: Client, url: str = OPPS, **query: Any) -> list[dict[str, Any]]:
    response = client.get(url, query)
    assert response.status_code == 200, response.content
    return response.json()["results"]  # type: ignore[no-any-return]


def ids_of(client: Client, url: str = OPPS, **query: Any) -> set[UUID]:
    return {UUID(row["id"]) for row in listed(client, url, **query)}


def test_each_role_sees_what_its_scope_reaches(sales: SimpleNamespace) -> None:
    assert ids_of(signed(sales.ana)) == pick(sales, "vera", "sam", "ana", "nobody")
    assert ids_of(signed(sales.vera)) == pick(sales, "vera")  # `OWN`
    assert ids_of(signed(sales.sam)) == pick(sales, "vera", "sam")  # `TEAM`: su compañera
    assert reply(signed(sales.bea).get(OPPS)) == DENIED
    assert reply(Client().get(OPPS)) == UNAUTHENTICATED
    assert reply(signed(sales.luis).get(OPPS)) == NOT_FOUND  # no es miembro de A


def test_another_organization_is_a_404_and_sees_only_its_own(sales: SimpleNamespace) -> None:
    other = OPPS.replace("org-a", "org-b")
    assert reply(signed(sales.ana).get(other)) == NOT_FOUND  # ana no es miembro de B
    assert reply(signed(sales.luis).get(other)) == DENIED  # luis sí, sin `opportunities.view`
    give(sales.b, sales.m_luis, {"opportunities.view": "ORGANIZATION"})
    assert ids_of(signed(sales.luis), other) == pick(sales, "b")  # nunca las de A
    assert ids_of(signed(sales.ana)) == pick(sales, "vera", "sam", "ana", "nobody")
    with tenant_scope(ctx(sales.a)):  # y por debajo de la API, RLS
        assert Opportunity.objects.filter(pk=sales.opps["b"]).count() == 0


def test_the_view_permission_alone_decides_reading(sales: SimpleNamespace) -> None:
    give(
        sales.a,
        sales.m["bea"],
        {"opportunities.update": "ORGANIZATION", "contacts.view": "ORGANIZATION"},
    )
    assert reply(signed(sales.bea).get(OPPS)) == DENIED  # editar o ver contactos no es ver
    give(sales.a, sales.m["bea"], {"opportunities.view": "BRANCH"})
    assert ids_of(signed(sales.bea)) == set()  # `BRANCH` es lo propio, y no tiene
    give(sales.a, sales.m["bea"], {"opportunities.view": "OWN"})
    assert ids_of(signed(sales.bea)) == set()


def test_a_list_row_has_the_documented_fields_and_the_amount_is_text(
    sales: SimpleNamespace,
) -> None:
    rows = listed(signed(sales.ana))
    assert all(set(row) == LIST_FIELDS for row in rows)
    row = next(r for r in rows if r["id"] == str(sales.opps["vera"]))
    nuevo = stage(sales.a)
    assert row["contact"] == {"id": str(sales.ids["owned"]), "display_name": "De vera"}
    assert row["stage"] == {"id": str(nuevo.pk), "name": nuevo.name, "type": "OPEN"}
    assert (row["amount"], row["currency"], row["status"], row["version"]) == (
        "0.00",
        "PEN",
        "OPEN",
        1,
    )
    assert (row["pipeline_id"], row["probability"], row["priority"]) == (
        str(nuevo.pipeline_id), 10, "MEDIUM",
    )  # fmt: skip
    assert row["assigned_user_id"] == str(sales.vera.pk) and row["lost_reason"] is None
    assert row["won_at"] is None and row["lost_at"] is None and row["next_action"] is None


def test_the_contact_name_needs_the_contact_to_be_visible(sales: SimpleNamespace) -> None:
    def contact(client: Client, key: str) -> dict[str, Any]:
        rows = {r["id"]: r for r in listed(client)}
        return rows[str(sales.opps[key])]["contact"]  # type: ignore[no-any-return]

    ana, vera, sam = signed(sales.ana), signed(sales.vera), signed(sales.sam)
    assert contact(ana, "vera")["display_name"] == "De vera"  # Owner: todo
    assert contact(vera, "vera")["display_name"] == "De vera"  # lo suyo
    # sam ve la oportunidad de vera, pero el contacto es de vera: `TEAM` de contactos es `OWN`
    shown = contact(sam, "vera")
    assert shown == {"id": str(sales.ids["owned"]), "display_name": None}
    assert contact(sam, "sam")["display_name"] == "De sam"  # el que él creó
    give(sales.a, sales.m["bea"], {"opportunities.view": "ORGANIZATION"})
    bea = signed(sales.bea)  # ve todas las oportunidades y ningún contacto
    assert {r["contact"]["display_name"] for r in listed(bea)} == {None}
    assert len(listed(bea)) == 4
    give(sales.a, sales.m["bea"], {"contacts.view": "OWN"})
    assert {r["contact"]["display_name"] for r in listed(bea)} == {None}  # no es suyo ninguno
    with tenant_scope(ctx(sales.a)):  # un contacto borrado tampoco se nombra
        from apps.contacts.models import Contact

        Contact.objects.filter(pk=sales.ids["owned"]).update(deleted_at=timezone.now())
    assert contact(ana, "vera") == {"id": str(sales.ids["owned"]), "display_name": None}


def test_the_filters_combine_and_each_is_exact(sales: SimpleNamespace) -> None:
    ana = signed(sales.ana)
    nuevo, ganado = stage(sales.a), stage(sales.a, "ganado")
    with tenant_scope(ctx(sales.a)) as tenant:
        Opportunity.objects.filter(pk=sales.opps["sam"]).update(
            stage_id=ganado.pk, status="WON", won_at=timezone.now(), probability=100
        )
        del tenant
    assert ids_of(ana, status="WON") == pick(sales, "sam")
    assert ids_of(ana, status="OPEN") == pick(sales, "vera", "ana", "nobody")
    assert ids_of(ana, status="LOST") == set()
    assert ids_of(ana, stage_id=ganado.pk) == pick(sales, "sam")
    assert ids_of(ana, stage_id=nuevo.pk) == pick(sales, "vera", "ana", "nobody")
    assert ids_of(ana, pipeline_id=nuevo.pipeline_id) == pick(sales, "vera", "sam", "ana", "nobody")
    assert ids_of(ana, pipeline_id=uuid4()) == set()
    assert ids_of(ana, assigned_user_id=sales.vera.pk) == pick(sales, "vera")
    assert ids_of(ana, contact_id=sales.ids["plain"]) == pick(sales, "ana", "nobody")
    assert ids_of(
        ana, contact_id=sales.ids["plain"], status="OPEN", assigned_user_id=sales.ana.pk
    ) == pick(sales, "ana")
    assert ids_of(ana, assigned_user_id=sales.luis.pk) == set()  # de B: nada, ni de A ni de B
    sam = signed(sales.sam)  # los filtros recortan lo que el alcance deja; nunca lo amplían
    assert ids_of(sam, assigned_user_id=sales.ana.pk) == set()
    assert ids_of(sam, contact_id=sales.ids["plain"]) == set()
    assert ids_of(sam, status="OPEN") == pick(sales, "vera")


def test_q_matches_the_title_or_the_number_literally(sales: SimpleNamespace) -> None:
    ana = signed(sales.ana)
    with tenant_scope(ctx(sales.a)):
        Opportunity.objects.filter(pk=sales.opps["ana"]).update(title="Oferta 100% _especial_")
        Opportunity.objects.filter(pk=sales.opps["sam"]).update(title="Oferta 100 especial")
        number = Opportunity.objects.get(pk=sales.opps["vera"]).number
    assert ids_of(ana, q="DE VERA") == pick(sales, "vera")  # sin distinguir mayúsculas
    assert ids_of(ana, q=number.lower()) == pick(sales, "vera")
    assert ids_of(ana, q=number[-3:]) >= pick(sales, "vera")
    assert ids_of(ana, q="100%") == pick(sales, "ana")  # `%` y `_` son literales
    assert ids_of(ana, q="_especial_") == pick(sales, "ana")
    assert ids_of(ana, q="") == pick(sales, "vera", "sam", "ana", "nobody")
    assert ids_of(ana, q="no existe") == set()
    assert ids_of(signed(sales.sam), q="De ana") == set()  # dentro del alcance


@pytest.mark.parametrize(
    "bad",
    [
        {"pipeline_id": "x"}, {"stage_id": "1"}, {"assigned_user_id": ""}, {"contact_id": "no"},
        {"status": "won"}, {"status": "X"}, {"q": "a\nb"}, {"q": "a\x00b"}, {"q": "x" * 201},
        {"pipeline_id": ["a", "b"]}, {"q": ["a", "b"]}, {"status": ["OPEN", "OPEN"]},
    ],
)  # fmt: skip
def test_a_hostile_filter_is_a_400_never_a_500(sales: SimpleNamespace, bad: dict[str, Any]) -> None:
    response = signed(sales.ana).get(OPPS, bad)
    assert response.status_code == 400, response.content
    assert response.json()["code"] == "VALIDATION_ERROR"
    assert set(response.json()["fields"]) == set(bad)
    for value in bad.values():  # el error no repite lo recibido
        if isinstance(value, str) and len(value) > 2:
            assert value not in response.text


def test_the_list_pages_by_creation_even_when_the_numbers_sort_as_text(
    crm: SimpleNamespace,
) -> None:
    order = [
        make(crm.a, number=n, assigned_user_id=crm.ana.pk).pk
        for n in ("OPP-999999", "OPP-1000000", "OPP-1000001")
    ]  # como texto, `OPP-1000000` va antes que `OPP-999999`
    ana = signed(crm.ana)
    first = ana.get(OPPS, {"limit": 2}).json()
    assert [r["id"] for r in first["results"]] == [str(i) for i in order[:2]] and first["next"]
    second = ana.get(OPPS, {"limit": 2, "cursor": first["next"]}).json()
    assert [r["id"] for r in second["results"]] == [str(order[2])] and second["next"] is None
    for bad in ("0", "201", "x"):
        response = ana.get(OPPS, {"limit": bad})
        assert response.status_code == 400 and set(response.json()["fields"]) == {"limit"}
    assert ana.get(OPPS, {"cursor": "basura"}).status_code == 400
    # el cursor de otra organización no trae nada ajeno
    other = signed(crm.luis).get(OPPS.replace("org-a", "org-b"), {"cursor": first["next"]})
    assert other.status_code == 403  # luis es de B, pero aún sin `opportunities.view`
    give(crm.b, crm.m_luis, {"opportunities.view": "ORGANIZATION"})
    make(crm.b, assigned_user_id=crm.luis.pk)
    foreign = signed(crm.luis).get(OPPS.replace("org-a", "org-b"), {"cursor": first["next"]})
    assert foreign.status_code == 200 and not set(order) & {
        UUID(r["id"]) for r in foreign.json()["results"]
    }


def test_the_list_costs_the_same_queries_however_many_rows(sales: SimpleNamespace) -> None:
    ana = signed(sales.ana)

    def queries() -> int:
        with CaptureQueriesContext(connection) as captured:
            assert ana.get(OPPS).status_code == 200
        return len(captured)

    one = queries()
    for index in range(15):
        make(sales.a, title=f"Más {index}", assigned_user_id=sales.vera.pk)
    assert queries() == one
    give(sales.a, sales.m["bea"], {"opportunities.view": "TEAM"})
    add(sales.a, team(sales.a, "otro").pk, sales.m["bea"])
    bea = signed(sales.bea)
    with CaptureQueriesContext(connection) as captured:
        assert bea.get(OPPS).status_code == 200
    assert len(captured) <= one + 1


def test_head_and_the_writing_methods_follow_the_same_rules(sales: SimpleNamespace) -> None:
    assert signed(sales.vera).head(OPPS).status_code == 200
    assert signed(sales.vera).head(OPPS).content == b""
    assert signed(sales.bea).head(OPPS).status_code == 403
    assert signed(sales.luis).head(OPPS).status_code == 404
    assert Client().head(OPPS).status_code == 401
    for method in ("post", "put", "patch", "delete"):  # esta entrega solo lee
        assert getattr(signed(sales.ana), method)(OPPS).status_code in (403, 405), method


def test_a_role_with_team_scope_over_http_follows_the_team(sales: SimpleNamespace) -> None:
    give(sales.a, sales.m["bea"], {"opportunities.view": "TEAM"})
    assert ids_of(signed(sales.bea)) == set()  # sin equipo, solo lo propio
    add(sales.a, team(sales.a, "otro").pk, sales.m["bea"])
    assert ids_of(signed(sales.bea)) == set()  # ese equipo no tiene a nadie con oportunidades
    with tenant_scope(ctx(sales.a)):
        from apps.organizations.models import TeamMember

        mine = TeamMember.objects.filter(membership_id=sales.m["bea"]).values("team_id")
        TeamMember.objects.filter(membership_id=sales.m["vera"]).delete()
        add(sales.a, mine[0]["team_id"], sales.m["vera"])
    assert ids_of(signed(sales.bea)) == pick(sales, "vera")

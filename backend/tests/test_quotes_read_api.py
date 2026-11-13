"""F5-09d: los permisos de cotizaciones, la política de alcance por quien las creó y la lista
(`…/quotes/`): alcance, filtros, nombre del contacto, contenido y paginación. Middleware, sesión,
motor de autorización y PostgreSQL con el rol `crm_app`."""

from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest
from django.test import Client
from django.utils import timezone

from apps.access.catalog import BY_CODE, ROLE_TEMPLATES, Scope
from apps.access.scopes import policy_for
from apps.access.selectors import execution_context
from apps.deals.scopes import AssignedTo
from apps.quotes.models import Quote
from core.tenancy.scope import tenant_scope
from tests import test_opportunity_scopes as scopes
from tests.test_authorization import give
from tests.test_member_branch import DENIED
from tests.test_memberships import ctx
from tests.test_opportunities_tables import make
from tests.test_prices import price_list
from tests.test_quote_tables import issued
from tests.test_self_context import NOT_FOUND, reply, signed

crm, pipelines, real_stack = scopes.crm, scopes.pipelines, scopes.real_stack
pytestmark = pytest.mark.usefixtures("tenant_db")
QUOTES = "/api/v1/o/org-a/quotes/"
UNAUTHENTICATED = (401, b'{"code":"NOT_AUTHENTICATED"}')
FIELDS = {
    "id", "number", "revision", "status", "contact", "opportunity_id", "price_list_id",
    "currency", "tax_mode", "tax_rate", "subtotal", "discount_total", "tax_total", "total",
    "valid_until", "cancelled_at", "created_by_user_id", "created_at", "updated_at", "version",
}  # fmt: skip
NOW = timezone.now()
SCOPED = ("quotes.view", "quotes.update", "quotes.send", "quotes.cancel")
UNSCOPED = ("quotes.create", "quotes.approve", "quotes.override_price", "quotes.discount")
_numbers = iter(range(1, 10_000))


def make_quote(org: UUID, who: Any, contact: UUID, **over: Any) -> UUID:
    """Una cotización en borrador (sin líneas) de `who`, con su lista de precios."""
    fields: dict[str, Any] = {
        "number": f"COT-{next(_numbers):06d}", "contact_id": contact, "currency": "PEN",
        "price_list_id": price_list(org, f"L{next(_numbers)}"), "tax_rate": Decimal("0.18"),
        "created_by_user_id": None if who is None else who.pk,
    }  # fmt: skip
    with tenant_scope(ctx(org)):
        return Quote.objects.create(**{**fields, **over}).pk  # type: ignore[no-any-return]


@pytest.fixture
def board(crm: SimpleNamespace) -> SimpleNamespace:
    """Sobre `crm` (ana Owner, vera Vendedora y sam Supervisor, del mismo equipo; bea sin rol; luis
    de B): una cotización de cada uno, una sin autor y una de B."""
    ids = {
        "vera": make_quote(crm.a, crm.vera, crm.ids["owned"]),
        "sam": make_quote(crm.a, crm.sam, crm.ids["sam"]),
        "ana": make_quote(crm.a, crm.ana, crm.ids["plain"]),
        "nobody": make_quote(crm.a, None, crm.ids["plain"]),
        "b": make_quote(crm.b, crm.luis, crm.ids["b"]),
    }
    return SimpleNamespace(**vars(crm), quotes=ids)


def pick(board: SimpleNamespace, *keys: str) -> set[UUID]:
    return {board.quotes[key] for key in keys}


def ids_of(client: Client, url: str = QUOTES, **query: Any) -> set[UUID]:
    response = client.get(url, query)
    assert response.status_code == 200, response.content
    return {UUID(row["id"]) for row in response.json()["results"]}


def test_the_catalog_and_the_templates_carry_the_quote_permissions() -> None:
    for code in (*SCOPED, *UNSCOPED):
        permission = BY_CODE[code]
        assert (permission.module, permission.is_sensitive) == ("quotes", False)
        assert permission.supports_scope is (code in SCOPED)
    held = {
        t.code: {k: v for k, v in t.grants.items() if k.startswith("quotes.")}
        for t in ROLE_TEMPLATES
    }
    every = {**dict.fromkeys(SCOPED, Scope.ORGANIZATION), **dict.fromkeys(UNSCOPED)}
    assert held["owner"] == held["admin"] == every
    assert held["supervisor"] == {  # aprueba, pero no escribe un precio manual (a decidir)
        **dict.fromkeys(SCOPED, Scope.TEAM),
        **{k: None for k in UNSCOPED if k != "quotes.override_price"},
    }
    assert held["seller"] == {  # crea y descuenta (con su límite); no aprueba ni fija un precio
        **dict.fromkeys(SCOPED, Scope.OWN), "quotes.create": None, "quotes.discount": None,
    }  # fmt: skip
    assert policy_for(Quote) == AssignedTo("created_by_user_id")


def test_each_role_sees_what_its_scope_reaches(board: SimpleNamespace) -> None:
    assert ids_of(signed(board.ana)) == pick(board, "vera", "sam", "ana", "nobody")
    assert ids_of(signed(board.vera)) == pick(board, "vera")  # `OWN`
    assert ids_of(signed(board.sam)) == pick(board, "vera", "sam")  # `TEAM`: su compañera
    assert reply(signed(board.bea).get(QUOTES)) == DENIED
    assert reply(Client().get(QUOTES)) == UNAUTHENTICATED
    assert reply(signed(board.luis).get(QUOTES)) == NOT_FOUND  # no es miembro de A
    other = QUOTES.replace("org-a", "org-b")
    assert reply(signed(board.ana).get(other)) == NOT_FOUND  # ana no es miembro de B
    assert reply(signed(board.luis).get(other)) == DENIED  # luis sí, sin `quotes.view`
    give(board.b, board.m_luis, {"quotes.view": "ORGANIZATION"})
    assert ids_of(signed(board.luis), other) == pick(board, "b")  # nunca las de A


def test_the_view_permission_alone_decides_reading(board: SimpleNamespace) -> None:
    others = {"quotes.update": "ORGANIZATION", "quotes.create": None, "quotes.approve": None}
    give(board.a, board.m["bea"], others)
    assert reply(signed(board.bea).get(QUOTES)) == DENIED  # editar, crear o aprobar no es ver
    give(board.a, board.m["bea"], {"quotes.view": "BRANCH"})
    assert ids_of(signed(board.bea)) == set()  # `BRANCH` es lo propio, y no tiene
    give(board.a, board.m["bea"], {"quotes.view": "OWN"})
    assert ids_of(signed(board.bea)) == set()


def test_a_row_has_its_documented_shape_and_the_contact_name_follows_the_contact_scope(
    board: SimpleNamespace,
) -> None:
    with tenant_scope(ctx(board.a)):
        Quote.objects.filter(pk=board.quotes["vera"]).update(
            subtotal=Decimal("84.75"), tax_total=Decimal("15.25"), total=Decimal("100.00"),
            discount_total=Decimal("20"), valid_until=NOW,
        )  # fmt: skip
    row = next(
        r for r in signed(board.ana).get(QUOTES).json()["results"]
        if r["id"] == str(board.quotes["vera"])
    )  # fmt: skip
    assert set(row) == FIELDS
    assert (row["status"], row["revision"], row["currency"], row["tax_mode"], row["version"]) == (
        "DRAFT", 1, "PEN", "INCLUDED", 1,
    )  # fmt: skip
    assert (row["subtotal"], row["tax_total"], row["total"], row["discount_total"]) == (
        "84.75",
        "15.25",
        "100.00",
        "20.00",
    )  # importes: texto con dos decimales
    assert row["tax_rate"] == "0.1800" and row["created_by_user_id"] == str(board.vera.pk)
    assert row["contact"] == {"id": str(board.ids["owned"]), "display_name": "De vera"}
    mine = signed(board.vera).get(QUOTES).json()["results"][0]
    assert mine["contact"]["display_name"] == "De vera"  # el contacto es suyo (`OWN`)
    other = make_quote(board.a, board.vera, board.ids["plain"])  # suya, sobre un contacto ajeno
    rows = {r["id"]: r for r in signed(board.vera).get(QUOTES).json()["results"]}
    assert rows[str(other)]["contact"] == {"id": str(board.ids["plain"]), "display_name": None}
    give(board.a, board.m["vera"], {"contacts.view": "ORGANIZATION"})
    rows = {r["id"]: r for r in signed(board.vera).get(QUOTES).json()["results"]}
    assert rows[str(other)]["contact"]["display_name"] == "Sin nadie"


def test_the_list_never_carries_a_cost_a_margin_or_the_free_text(board: SimpleNamespace) -> None:
    with tenant_scope(ctx(board.a)):
        Quote.objects.update(notes_to_customer="Nota al cliente", internal_notes="Nota interna")
    body = signed(board.ana).get(QUOTES).content.decode().lower()
    for word in ("cost", "margin", "margen", "nota", "internal", "items"):
        assert word not in body, word


def test_the_filters_combine_and_each_one_is_asked_once(
    board: SimpleNamespace, migrator: Any
) -> None:
    opp = make(board.a, contact_id=board.ids["plain"])
    with tenant_scope(ctx(board.a)):
        Quote.objects.filter(pk=board.quotes["vera"]).update(
            status="CANCELLED", cancelled_at=NOW, cancel_reason="No"
        )
        for who in ("sam", "ana"):
            Quote.objects.filter(pk=board.quotes[who]).update(status="SENT", **issued())
        Quote.objects.filter(pk=board.quotes["ana"]).update(opportunity_id=opp.pk)
    migrator.execute(  # ya vencida: solo quien mantiene la base reescribe lo emitido
        "UPDATE quotes SET sent_at = %s, valid_until = %s WHERE id = %s",
        [NOW - timedelta(days=9), NOW - timedelta(days=1), board.quotes["sam"]],
    )
    with tenant_scope(ctx(board.a)):
        Quote.objects.filter(pk=board.quotes["nobody"]).update(valid_until=NOW - timedelta(days=1))
    number = {}
    with tenant_scope(ctx(board.a)):
        number = {k: Quote.objects.get(pk=v).number for k, v in board.quotes.items() if k != "b"}
    ana = signed(board.ana)
    assert ids_of(ana, mine="true") == pick(board, "ana")
    assert ids_of(ana, status="CANCELLED") == pick(board, "vera")
    assert ids_of(ana, status="SENT") == pick(board, "sam", "ana")
    assert ids_of(ana, expired="true") == pick(board, "sam")  # enviada con la validez pasada
    assert ids_of(ana, contact_id=str(board.ids["plain"]), expired="false") == pick(
        board, "ana", "nobody"
    )  # un borrador con la validez pasada no cuenta
    assert ids_of(ana, contact_id=str(board.ids["plain"])) == pick(board, "ana", "nobody")
    assert ids_of(ana, opportunity_id=str(opp.pk)) == pick(board, "ana")
    assert ids_of(ana, number=number["sam"]) == pick(board, "sam")
    assert ids_of(ana, status="SENT", mine="true") == pick(board, "ana")
    assert ids_of(ana, status="") == pick(
        board, "vera", "sam", "ana", "nobody"
    )  # vacío: sin filtro
    for bad in ({"mine": ["true", "false"]}, {"status": "SOON"}, {"mine": "x"},
                {"contact_id": "x"}, {"opportunity_id": "x"}, {"number": "COT-1"},
                {"number": "x" * 40}, {"expired": "x"}):  # fmt: skip
        assert ana.get(QUOTES, bad).status_code == 400, bad
    assert ids_of(signed(board.vera), mine="true") == pick(board, "vera")
    assert (
        ids_of(signed(board.vera), contact_id=str(board.ids["plain"])) == set()
    )  # solo su alcance


def test_the_list_pages_by_cursor_without_repeating_a_row(board: SimpleNamespace) -> None:
    ana, seen = signed(board.ana), []
    page = ana.get(QUOTES, {"limit": 2}).json()
    assert len(page["results"]) == 2 and page["next"] is not None
    seen += [r["id"] for r in page["results"]]
    page = ana.get(QUOTES, {"limit": 2, "cursor": page["next"]}).json()
    seen += [r["id"] for r in page["results"]]
    assert len(seen) == len(set(seen)) == 4 and page["next"] is None


def test_the_policy_decides_one_row_like_the_list_does(board: SimpleNamespace) -> None:
    """`allows` y `q` salen de la misma declaración: lo que lista, lo permite por objeto."""
    with tenant_scope(ctx(board.a)):
        rows = {key: Quote.objects.get(pk=pk) for key, pk in board.quotes.items() if key != "b"}
    policy = policy_for(Quote)
    for user, scope, allowed in (
        (board.vera, Scope.OWN, {"vera"}),
        (board.sam, Scope.OWN, {"sam"}),
        (board.sam, Scope.TEAM, {"vera", "sam"}),
        (board.ana, Scope.ORGANIZATION, {"ana"}),  # el resto lo resuelve el motor
    ):
        with tenant_scope(ctx(board.a, user)) as tenant:
            ectx = execution_context(tenant)
            assert {k for k, row in rows.items() if policy.allows(scope, ectx, row)} == allowed

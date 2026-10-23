"""F4-07: la búsqueda de contactos (`GET …/contacts/?q=`). Alcance, cada clase de búsqueda, los
caracteres de `LIKE`, los límites de `q` y los índices que la sirven. PostgreSQL con `crm_app`."""

from types import SimpleNamespace
from typing import Any
from urllib.parse import urlencode
from uuid import UUID

import psycopg
import pytest
from django.db import connection

from apps.contacts.selectors import contacts, search
from core.tenancy.scope import tenant_scope
from tests import test_contacts_api
from tests.test_authorization import give
from tests.test_contacts_api import CONTACTS, contact, listed, names
from tests.test_memberships import ctx
from tests.test_self_context import signed

crm = test_contacts_api.crm
real_stack = test_contacts_api.real_stack
pytestmark = pytest.mark.usefixtures("tenant_db")


def found(client: Any, q: str, url: str = CONTACTS) -> set[UUID]:
    return listed(client, f"{url}?{urlencode({'q': q})}")


@pytest.fixture
def people(crm: SimpleNamespace) -> SimpleNamespace:
    """Contactos de A con teléfono y correo: dos de vera, el resto fuera de su alcance."""
    a, vera = crm.a, crm.vera.pk
    ids = {
        "maria": contact(
            a,
            "María Quispe",
            owner_user_id=vera,
            primary_phone_e164="+51987654321",
            primary_email="Mquispe@Example.com",
        ),
        "rosa": contact(a, "Rosa Huamán", owner_user_id=vera, primary_phone_e164="+51987000111"),
        "hidden": contact(  # fuera del alcance de vera
            a,
            "Lucía Oculta",
            primary_phone_e164="+51911222333",
            primary_email="lucia.oculta@example.com",
        ),
        "argentina": contact(a, "Tomás Pereyra", primary_phone_e164="+5491112345678"),
        "percent": contact(a, "100% Natural"),
        "under": contact(a, "Pet_Shop"),
        "slash": contact(a, "Back\\slash"),
        "plain": contact(a, "Ape Store"),
        "digits": contact(a, "Tienda 987654"),
    }
    return SimpleNamespace(**ids)


def test_a_seller_finds_nothing_outside_their_scope(
    crm: SimpleNamespace, people: SimpleNamespace
) -> None:
    """Criterio de aceptación: el teléfono, el correo o el nombre de un contacto fuera del
    alcance no lo devuelven, aunque casen; el Owner sí lo encuentra."""
    vera, ana = signed(crm.vera), signed(crm.ana)
    for q in ("+51911222333", "+51 911", "lucia.oculta@", "Oculta", "De Lima"):
        assert found(vera, q) == set(), q
        assert found(ana, q), q
    assert found(vera, "+51 987") == {people.maria, people.rosa}  # lo suyo, sí
    assert found(vera, "de") == names(crm, "owned")  # «De vera»; no «De Lima» ni «De sam»


def test_branch_scope_bounds_the_search_too(crm: SimpleNamespace) -> None:
    give(crm.a, crm.m["bea"], {"contacts.view": "BRANCH"})
    bea = signed(crm.bea)
    assert found(bea, "De") == set()  # sin sucursal, `BRANCH` es `OWN`: nada suyo
    give(crm.a, crm.m["bea"], {"contacts.view": "ORGANIZATION"})
    assert found(bea, "De") == names(crm, "owned", "lima", "cusco", "sam")


def test_each_organization_searches_only_its_own_contacts(crm: SimpleNamespace) -> None:
    assert found(signed(crm.ana), "De B") == set()
    give(crm.b, crm.m_luis, {"contacts.view": "ORGANIZATION"})
    luis = signed(crm.luis)
    assert found(luis, "De", "/api/v1/o/org-b/contacts/") == names(crm, "b")
    assert found(luis, "De Lima", "/api/v1/o/org-b/contacts/") == set()


def test_the_phone_matches_by_its_e164_prefix(
    crm: SimpleNamespace, people: SimpleNamespace
) -> None:
    ana = signed(crm.ana)
    both = {people.maria, people.rosa}
    assert found(ana, "+51 987") == both
    assert found(ana, "+51987654321") == {people.maria}  # el número entero
    assert found(ana, "0051 (987) 654-321") == {people.maria}  # `00`, separadores
    assert found(ana, "+51.987.000") == {people.rosa}
    assert found(ana, "+549") == {people.argentina}
    assert found(ana, "+5") == {people.maria, people.rosa, people.hidden, people.argentina}
    assert found(ana, "+654321") == set()  # el final del número no es un prefijo
    # Sin `+` no es un teléfono (no se adivina el país): es texto, que busca en el nombre
    assert found(ana, "987654") == {people.digits}
    for q in ("51987654321", "+", "+51 987 x"):
        assert found(ana, q) == set(), q


def test_the_email_matches_by_its_start_without_case(
    crm: SimpleNamespace, people: SimpleNamespace
) -> None:
    ana = signed(crm.ana)
    for q in ("mquispe@example.com", "MQUISPE@EXAMPLE.COM", "mQuIsPe@", "mq"):
        assert found(ana, q) == {people.maria}, q
    for q in ("example.com", "@example.com", "quispe@"):  # no es «contiene»
        assert found(ana, q) == set(), q


def test_the_name_contains_the_text_without_case(
    crm: SimpleNamespace, people: SimpleNamespace
) -> None:
    ana = signed(crm.ana)
    for q in ("quisp", "MARÍA", "maría quispe", "  María   Quispe  ", "ía q"):
        assert found(ana, q) == {people.maria}, q
    assert found(ana, "Maria") == set()  # los acentos sí cuentan
    assert found(ana, "huamán") == {people.rosa}


def test_like_wildcards_are_literal(crm: SimpleNamespace, people: SimpleNamespace) -> None:
    ana = signed(crm.ana)
    assert found(ana, "%") == {people.percent}
    assert found(ana, "_") == {people.under}
    assert found(ana, "\\") == {people.slash}
    assert found(ana, "a%e") == found(ana, "a_e") == set()  # «Ape Store» no casa
    assert found(ana, "%@") == set()


def test_q_is_bounded_and_empty_means_no_filter(crm: SimpleNamespace) -> None:
    ana = signed(crm.ana)
    every = listed(ana)
    assert found(ana, "") == found(ana, "   ") == every
    assert found(ana, " " * 20 + "x" * 254 + " ") == set()  # se mide sin los espacios
    for q in ("x" * 255, "a\nb", "a\tb", "a\x00b"):
        response = ana.get(f"{CONTACTS}?{urlencode({'q': q})}")
        assert response.status_code == 400, q
        assert (response.json()["code"], set(response.json()["fields"])) == (
            "VALIDATION_ERROR",
            {"q"},
        ), q
        assert q not in response.content.decode()  # sin repetir lo buscado
    twice = ana.get(f"{CONTACTS}?q=De&q=Lima")
    assert twice.status_code == 400 and set(twice.json()["fields"]) == {"q"}


def test_the_search_pages_with_the_cursor_in_creation_order(crm: SimpleNamespace) -> None:
    ana, seen = signed(crm.ana), []
    url: str | None = f"{CONTACTS}?q=de&limit=1"
    while url:
        page = ana.get(url).json()
        seen += [row["id"] for row in page["results"]]
        url = page["next"] and f"{CONTACTS}?q=de&limit=1&cursor={page['next']}"
    assert seen == sorted(seen)
    assert set(map(UUID, seen)) == names(crm, "owned", "lima", "cusco", "sam")


def plan(org: UUID, q: str) -> str:
    """El plan de la búsqueda como `crm_app`, con RLS, sin recorrido secuencial."""
    with tenant_scope(ctx(org)):
        with connection.cursor() as cursor:
            cursor.execute("SET LOCAL enable_seqscan = off")
        return search(contacts(), q).explain()


def test_the_phone_prefix_uses_its_index_under_rls(
    orgs: dict[str, UUID], migrator: psycopg.Connection[Any]
) -> None:
    """3000 contactos en una organización, con estadísticas: el índice de la organización no
    filtra nada, y el prefijo entra en la condición del índice del teléfono (`~>=~`, `~<~`)."""
    migrator.execute(
        "INSERT INTO contacts (organization_id, display_name, first_name, last_name, "
        "primary_phone_e164, source, created_at, updated_at, version) "
        "SELECT %s, 'Cliente ' || n, '', '', '+5190' || lpad(n::text, 7, '0'), 'MANUAL', "
        "now(), now(), 1 FROM generate_series(1, 3000) AS n",
        [orgs["A"]],
    )
    migrator.execute("ANALYZE contacts")
    used = plan(orgs["A"], "+51 9000 0123")
    assert "contacts_org_phone_idx" in used and "~>=~ '+5190000123'" in used, used
    with tenant_scope(ctx(orgs["A"])):
        assert search(contacts(), "+5190000123").count() == 10  # 1230…1239

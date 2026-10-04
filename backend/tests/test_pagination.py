"""F2-15: paginación por cursor de los listados (ADR-016). Rutas de tenant reales: middleware,
sesión de Django, `ScopeFilter` y PostgreSQL con el rol `crm_app`."""

from base64 import b64encode
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlencode
from uuid import UUID, uuid4

import psycopg
import pytest
from django.core.exceptions import ImproperlyConfigured
from django.test import Client
from django.urls import path
from drf_spectacular.generators import SchemaGenerator
from rest_framework import generics

import config.urls
from core.api.pagination import CursorPagination
from tests import test_access_api, test_authorization
from tests.test_access_api import TENANT, WidgetView, url
from tests.test_authorization import VIEW, give

pytestmark = [pytest.mark.usefixtures("tenant_db"), pytest.mark.urls(__name__)]
api, world = test_access_api.api, test_authorization.world  # las fixtures de F2-05


class Widgets(WidgetView, generics.ListAPIView):
    required_permissions = {"GET": VIEW}


class ByName(Widgets):
    ordering = ("-name", "id")  # el orden lo declara la vista


urlpatterns = [
    *config.urls.urlpatterns,
    path(TENANT + "widgets/", Widgets.as_view()),
    path(TENANT + "by-name/", ByName.as_view()),
]


def seed(migrator: psycopg.Connection[Any], org: UUID, count: int) -> None:
    for index in range(count):
        migrator.execute(
            "INSERT INTO tenancy_app_widget (id, organization_id, name) VALUES (%s, %s, %s)",
            [uuid4(), org, f"extra {index:03} {uuid4().hex[:6]}"],
        )


def ids_of(migrator: psycopg.Connection[Any], org: UUID) -> set[str]:
    rows = migrator.execute("SELECT id FROM tenancy_app_widget WHERE organization_id = %s", [org])
    return {str(row[0]) for row in rows.fetchall()}


def walk(client: Client, target: str, limit: int, cursor: str | None = None) -> list[Any]:
    """Todas las filas desde `cursor`, página a página, siguiendo `next`."""
    rows: list[Any] = []
    while True:
        query: dict[str, str | int] = {"limit": limit, **({"cursor": cursor} if cursor else {})}
        body = client.get(target, query).json()
        assert set(body) == {"results", "next"} and len(body["results"]) <= limit
        rows += body["results"]
        cursor = body["next"]
        if cursor is None:
            return rows


def token(**parts: str) -> str:
    """Un cursor fabricado a mano, con el formato del que emite la API."""
    return b64encode(urlencode(parts).encode()).decode()


def test_walking_the_pages_returns_every_row_once(
    api: Any, migrator: psycopg.Connection[Any]
) -> None:
    give(api.a, api.membership, {VIEW: "ORGANIZATION"})
    seed(migrator, api.a, 7)  # con las 3 del mundo, 10 filas de A
    seed(migrator, api.orgs["B"], 5)
    before = ids_of(migrator, api.a)
    first = api.client.get(url(), {"limit": 4}).json()
    assert len(first["results"]) == 4 and not first["next"].startswith("http")  # no es una URL
    seed(migrator, api.a, 3)  # filas nuevas entre dos páginas
    found = [row["id"] for row in first["results"] + walk(api.client, url(), 4, first["next"])]
    assert len(found) == len(set(found)) and before <= set(found)  # ni repite ni se salta
    assert set(found) <= ids_of(migrator, api.a) and found == sorted(found)  # nada de B; por `id`


def test_the_page_size_has_a_default_and_a_ceiling(
    api: Any, migrator: psycopg.Connection[Any]
) -> None:
    give(api.a, api.membership, {VIEW: "ORGANIZATION"})
    seed(migrator, api.a, 60)
    body = api.client.get(url()).json()
    assert len(body["results"]) == 50 and body["next"]  # sin `limit`, nunca el listado entero
    assert len(api.client.get(url(), {"limit": 200}).json()["results"]) == 63
    for bad in ("0", "201", "-1", "abc", "", "1.5", "٣", "9" * 30):
        response = api.client.get(url(), {"limit": bad})
        assert response.status_code == 400 and response.json()["code"] == "VALIDATION_ERROR", bad
        assert list(response.json()["fields"]) == ["limit"]


def test_an_unreadable_or_impossible_cursor_is_a_validation_error(api: Any) -> None:
    give(api.a, api.membership, {VIEW: "ORGANIZATION"})
    position = str(api.mine)
    forged = ("", "%%%", "ñ", token(p="no-es-un-uuid"), token(o="x", p=position), token(o="2"))
    for bad in (*forged, token(r="1", p=position)):  # tampoco hay cursores hacia atrás
        response = api.client.get(url(), {"cursor": bad})
        assert response.status_code == 400 and list(response.json()["fields"]) == ["cursor"], bad
    assert api.client.get(url(), {"cursor": token(p=position)}).status_code == 200


def test_a_cursor_from_another_organization_reveals_nothing(
    api: Any, migrator: psycopg.Connection[Any]
) -> None:
    give(api.a, api.membership, {VIEW: "OWN"})  # y dentro de A, solo lo que su alcance permite
    seed(migrator, api.orgs["B"], 5)
    for foreign in ids_of(migrator, api.orgs["B"]) | {"00000000-0000-0000-0000-000000000000"}:
        rows = walk(api.client, url(), 2, token(p=foreign))  # la posición de una fila de B
        assert {row["id"] for row in rows} <= {str(api.mine)}
    assert [row["name"] for row in walk(api.client, url(), 2)] == ["mine"]


def test_the_view_declares_the_order_and_it_must_end_in_id(
    api: Any, migrator: psycopg.Connection[Any]
) -> None:
    give(api.a, api.membership, {VIEW: "ORGANIZATION"})
    seed(migrator, api.a, 4)
    names = [row["name"] for row in walk(api.client, url("by-name/"), 3)]
    assert len(names) == 7 and names == sorted(names, reverse=True)
    chosen = api.client.get(url(), {"ordering": "-name", "limit": 200}).json()["results"]
    assert [row["id"] for row in chosen] == sorted(row["id"] for row in chosen)  # no lo elige él
    paginator = CursorPagination()
    assert paginator.get_ordering(None, None, SimpleNamespace()) == ("id",)
    for unstable in ("name", ("name",), ("id", "name")):
        with pytest.raises(ImproperlyConfigured, match="terminar en `id`"):
            paginator.get_ordering(None, None, SimpleNamespace(ordering=unstable))


def test_the_contract_describes_the_envelope_and_its_parameters() -> None:
    schema = SchemaGenerator(patterns=urlpatterns[-2:-1]).get_schema(public=True)
    operation = schema["paths"]["/api/v1/o/{org_slug}/widgets/"]["get"]
    query = {
        item["name"]: item["schema"] for item in operation["parameters"] if item["in"] == "query"
    }
    assert query == {
        "cursor": {"type": "string"},
        "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50},
    }
    reply = operation["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
    page = schema["components"]["schemas"][reply.rsplit("/", 1)[1]]
    assert page["required"] == ["results", "next"] and page["properties"]["next"]["nullable"]
    assert page["properties"]["results"]["type"] == "array"

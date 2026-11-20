"""F8-07e (#1002; ADR-005 Parte C, ADR-034 §2, §6, §11): la captura de consultas ve todo lo que
escribe o bloquea, el contrato de importación del copiloto, el registro rechaza claves en español
o fuera de ASCII y lo comprueba en cada llamada, y `search_product` busca lo que dice. PostgreSQL
con el rol `crm_app`."""

import json
import pathlib
import tomllib
from dataclasses import replace
from datetime import datetime, timedelta
from string.templatelib import Interpolation, Template
from typing import Any, cast
from uuid import UUID

import pytest
from django.db import connection, connections
from django.utils import timezone
from psycopg import sql
from psycopg._cursor_base import BaseCursor

from apps.ai_agents import registry
from apps.ai_agents.fields import Uuid
from apps.ai_agents.registry import ToolRejected, check
from apps.ai_agents.tools import catalog
from apps.ai_agents.tools.catalog import SEARCH_PRODUCT
from apps.catalog import products as pc
from apps.catalog.brands import create_brand
from apps.catalog.categories import create_category
from apps.catalog.models import Brand, Category, ProductVariant
from core.tenancy.scope import tenant_scope
from tests.ai_queries import captured, writes
from tests.test_ai_read_tools import INVALID, LONG, active, allow, run, shop, variant, world
from tests.test_memberships import ctx

__all__ = ["shop", "world"]  # las fixtures
pytestmark = pytest.mark.usefixtures("tenant_db")
BACKEND = pathlib.Path(__file__).resolve().parents[1]
UNKNOWN = {"ok": False, "error": {"code": "UNKNOWN_TOOL"}}


# La captura de consultas (tests/ai_queries.py): una sentencia de cada forma que escribe, bloquea o
# cambia la sesión, y lecturas que se le parecen.
PLANTED = [
    'SELECT 1 FROM "t" FOR SHARE', 'SELECT 1 FROM "t" FOR KEY SHARE',
    "SELECT pg_advisory_lock(1)", "SELECT pg_advisory_xact_lock(1)",
    "SELECT pg_try_advisory_lock(1)", 'SELECT * INTO "copia" FROM "t"', "SELECT nextval('s')",
    "SELECT setval('s', 1)", "REFRESH MATERIALIZED VIEW v", "VACUUM t", "REINDEX TABLE t",
    "CLUSTER t", "COMMENT ON TABLE t IS 'x'", "NOTIFY canal", "SELECT pg_notify('c', 'x')",
    "DO $$ BEGIN END $$", "SELECT set_config('app.x', %s, false)",
    "SELECT set_config('lock_timeout', '1s', FALSE)", "SELECT set_config('a', %s, %s)",
    'UPDATE "products" SET "name" = %s', "TRUNCATE products", "LOCK TABLE t",
    # F8-07e, segunda revisión: la sesión por SQL, comentarios, objetos grandes y otros
    "SET app.tenant_id = 'x'", "SET SESSION statement_timeout = 0", "SET ROLE crm_migrator",
    "  set search_path = public", "SELECT 1; SET x = 1", "/* c */ SET x = 1", "RESET ALL",
    "DISCARD ALL", "LISTEN c", 'SELECT 1 FROM "t" FOR/**/SHARE',
    'SELECT 1 FROM "t" FOR -- x\n SHARE',
    "SELECT E'\\'' ; UPDATE t SET x = 'a'", "SELECT lo_create(0)", "SELECT lo_unlink(1)",
    "SELECT lo_import('/f')", "SELECT lo_from_bytea(0, '')", "SELECT lo_put(1, 0, '')",
    "SELECT pg_terminate_backend(1)", "SELECT pg_cancel_backend(1)", "CHECKPOINT",
    "SECURITY LABEL ON TABLE t IS 'x'", "PREPARE TRANSACTION 'x'", "PREPARE p AS SELECT 1",
    "EXECUTE p",
]  # fmt: skip
READS = [
    "SELECT set_config('app.tenant_id', %s, true), set_config('app.user_id', %s, true)",
    'SELECT "t"."cluster_id", "t"."do_not_disturb", "t"."notify_at" FROM "t" WHERE "t"."x" = '
    "'delete into vacuum'",
    "SELECT clock_timestamp()", 'SAVEPOINT "s1"', 'RELEASE SAVEPOINT "s1"',
    "SET LOCAL lock_timeout = '1s'", "SET TRANSACTION ISOLATION LEVEL SERIALIZABLE",
    "SET CONSTRAINTS ALL DEFERRED", 'SELECT "t"."reset_at", \'a--b\', \'/*\' FROM "t"',
    "SELECT 1 -- se borra con delete\n", "SELECT E'it''s \\\\ ok'",
]  # fmt: skip


def test_capture_expression_flags_each_write_lock_and_session_change() -> None:
    assert writes(PLANTED) == PLANTED
    assert writes(READS) == []


def test_capture_sees_driver_cursors_copy_and_other_connections() -> None:
    other = connections.create_connection("default")
    connection.ensure_connection()
    try:
        with captured() as seen:
            driver = connection.connection  # el psycopg de Django, sin su envoltorio
            driver.execute("SELECT pg_try_advisory_xact_lock(7)")
            # una t-string (`t"…({9})"`; mypy aún no las lee): la plantilla, sin texto plano
            driver.execute(Template("SELECT pg_try_advisory_xact_lock(", Interpolation(9), ")"))
            driver.execute(sql.SQL("SELECT {}").format(sql.Literal("compuesta")))
            with driver.cursor() as cursor, cursor.copy("COPY (SELECT 1) TO STDOUT") as copy:
                list(copy)
            with other.cursor() as cursor:  # otra conexión, como la de otro alias
                cursor.execute("SELECT pg_try_advisory_xact_lock(8)")
    finally:
        other.close()
    found = writes(seen)  # también el `set_config('TimeZone', …, false)` de la otra conexión
    locks = [f"SELECT pg_try_advisory_xact_lock({n})" for n in (7, 9, 8)]
    assert found[:3] == [*locks[:2], "COPY (SELECT 1) TO STDOUT"]
    assert found[3].startswith("SELECT set_config('TimeZone', ") and found[3].endswith(", false)")
    assert found[4:] == locks[2:]
    assert "SELECT 'compuesta'" in seen
    count = len(seen)
    connection.connection.execute("SELECT 1")  # fuera del bloque ya no se captura
    assert len(seen) == count and "record" not in BaseCursor._convert_query.__qualname__


# El contrato de importación (pyproject.toml): lo que `apps.ai_agents` no puede importar.
def test_ai_agents_cannot_import_writers_keys_whatsapp_sealed_data_or_people() -> None:
    config = tomllib.loads((BACKEND / "pyproject.toml").read_text())
    mine = [c for c in config["tool"]["importlinter"]["contracts"]
            if c["type"] == "forbidden" and c["source_modules"] == ["apps.ai_agents"]]  # fmt: skip
    assert len(mine) == 1
    assert {"apps.access.services", "apps.ai_gateway.switch", "apps.ai_gateway.accounts",
            "apps.ai_gateway.keys", "apps.audit.services", "apps.conversations.whatsapp",
            "core.sealed", "apps.contacts.models", "apps.contacts.selectors",
            } <= set(mine[0]["forbidden_modules"])  # fmt: skip


# El registro: claves fuera de ASCII, en español, sinónimos de identidad y códigos de producto.
QUERY = SEARCH_PRODUCT.fields["query"]
FIELDS = ("оrg_id", "cliente_id", "conversacion_id", "conversa_tion", "usuario", "telefono",
          "correo", "owner_id", "seller_id", "person_id", "company_id", "lead_id", "deal_id",
          "uid", "ｑuery", "qu ery", "sucursal_id", "vendedor", "empresa_id", "comprador",
          "mensaje", "agente_id", "oportunidad_id", "negocio_id", "dni", "rut", "message_id",
          "chat", "thread_id", "wa_id", "psid", "staff_id", "buyer_id")  # fmt: skip
OUTPUTS = ("ѕku", "ｓｋｕ", "skü", "descripción", "descripcion", "margen", "notas_internas",
           "proveedor", "almacen", "codigo_barras", "existencias", "ubicacion", "reservado",
           "compra", "supplier", "ean13", "gtins", "isbn", "mpn", "upc_a",
           "short_description_long", "x_short_description", "short_description2", "interno",
           "vendor", "bodega", "inventory", "quantity", "qty", "cantidad", "comment", "comentario",
           "observaciones", "productgtin", "variantIsbn", "partMpn")  # fmt: skip


@pytest.mark.parametrize(
    "change",
    [
        *({"fields": {key: QUERY}} for key in FIELDS),
        *({"output": {key: str}} for key in OUTPUTS),
        {"output": {"items": [{"name": str}, {"label": str}]}},  # una lista de dos formas
    ],
)
def test_registry_rejects_spanish_non_ascii_and_code_keys(change: dict[str, Any]) -> None:
    with pytest.raises(ToolRejected):
        check(replace(SEARCH_PRODUCT, **{"name": "otra_tool", **change}))


def test_registry_still_takes_the_public_keys_of_the_catalog_and_price_tools() -> None:
    price = {"variant_id": str, "prices": [{"price_list": str, "currency": str, "amount": str,
             "regular_amount": str, "tax_included": bool, "promotion": (str, type(None)),
             "promotion_terms": (str, type(None)), "valid_until": (str, type(None)),
             "price_id": str}]}  # fmt: skip
    for output in ({"products": [catalog.ROW]}, price, {"short-description": str}):
        check(replace(SEARCH_PRODUCT, name="otra_tool", output=output))
    check(replace(SEARCH_PRODUCT, name="otra_tool", fields={"variant_id": Uuid("x")}))


@pytest.mark.parametrize("where", ["output", "leaf", "fields"])
def test_a_tool_changed_after_loading_never_runs(shop: Any, monkeypatch: Any, where: str) -> None:
    allow(shop.world, "products.view")
    if where == "output":
        monkeypatch.setitem(catalog.ROW, "unit_cost", str)
    elif where == "leaf":
        monkeypatch.setitem(catalog.ROW, "name", dict)  # una hoja abierta
    else:
        monkeypatch.setitem(cast(dict[str, Any], SEARCH_PRODUCT.fields), "conversation_id", QUERY)
    assert run(shop.world, "search_product", {"query": "collar"})[1] == UNKNOWN


# El texto de búsqueda: lo que no se ve, con la regla de `core.text`.
@pytest.mark.parametrize(
    "query",
    ["colㅤlar", "colﾠlar", "colᅟlar", "colᅠlar", "col⠀lar",
     "col️lar", "collar️", "col͏lar", "col឴lar", "collar 2️"],
)  # fmt: skip
def test_invisible_search_text_is_rejected_without_echo(world: Any, query: str) -> None:
    done, _ = run(world, "search_product", {"query": query})
    assert done.text == INVALID and done.evidence is None


def test_visible_emoji_and_keycaps_are_still_searchable(world: Any) -> None:
    allow(world, "products.view")
    for query in ("collar 1️⃣", "collar ❤️"):
        assert run(world, "search_product", {"query": query})[1]["ok"] is True


# La búsqueda: nombre, descripción corta, marca y categoría vivas, sin tildes ni mayúsculas.
def names(world: Any, query: str) -> list[str]:
    body = run(world, "search_product", {"query": query})[1]
    return [p["name"] for p in body["data"]["products"]]


def test_search_finds_by_name_short_description_brand_and_category_without_accents(
    shop: Any,
) -> None:
    tenant = ctx(shop.world.a)
    with tenant_scope(tenant):
        made = pc.create_product(tenant, name="Pelota Pequeña Café", sku_code="PEL-1")
        pc.update_product(tenant, product_id=made.pk, status="ACTIVE")
        active(tenant, "Hueso Cafe")
        old = create_brand(tenant, name="Marca Vieja")
        gone = create_category(tenant, name="Categoria Vieja")
        active(tenant, "Correa Azul", brand_id=old.pk, category_id=gone.pk)
        Brand.objects.filter(pk=old.pk).update(deleted_at=timezone.now())  # como lo ve `_name`
        Category.objects.filter(pk=gone.pk).update(deleted_at=timezone.now())
    allow(shop.world, "products.view")
    for query in ("pequena", "PEQUEÑA", "pequeña café"):
        assert names(shop.world, query) == ["Pelota Pequeña Café"]
    assert names(shop.world, "CAFÉ") == names(shop.world, "cafe") == [
        "Hueso Cafe", "Pelota Pequeña Café"]  # fmt: skip
    for query in ("doggy", "PASEO", "nylon"):  # marca, categoría y descripción corta
        assert names(shop.world, query) == ["Collar Rojo"]
    assert names(shop.world, "vieja") == []  # una marca o categoría borrada no se busca
    for query in (LONG, "Collar-Rojo", "COL-Grande"):  # ni la descripción larga ni el SKU
        assert names(shop.world, query) == []


@pytest.fixture
def bags(world: Any) -> list[Any]:
    """11 productos que encajan, creados al revés de su orden por nombre; 10 no caben en 8 KiB."""
    tenant = ctx(world.a)
    with tenant_scope(tenant):
        return [active(tenant, f"Bolso {n:02}", short_description="€" * 255)
                for n in reversed(range(11))]  # fmt: skip


def test_search_returns_at_most_ten_by_name_and_fewer_if_they_do_not_fit(
    world: Any, bags: list[Any], monkeypatch: Any
) -> None:
    allow(world, "products.view")
    done, body = run(world, "search_product", {"query": "bolso"})
    rows = body["data"]["products"]
    with monkeypatch.context() as patch:
        patch.setattr(registry, "MAX_BYTES", 10**6)
        full = run(world, "search_product", {"query": "bolso"})[1]["data"]["products"]
    assert [p["name"] for p in full] == [f"Bolso {n:02}" for n in range(10)]
    assert done.ok and 1 <= len(rows) < 10 and rows == full[: len(rows)]
    assert len(done.text.encode()) <= registry.MAX_BYTES  # 8 KiB
    assert not registry.fits({"products": full[: len(rows) + 1]}, "CATALOG")  # una más no cabe
    assert done.evidence and done.evidence.subjects == tuple(UUID(p["product_id"]) for p in rows)


def test_variants_are_at_most_twenty_in_their_order(shop: Any) -> None:
    tenant = ctx(shop.world.a)
    with tenant_scope(tenant):
        made = [variant(tenant, shop.red, f"T{n:02}") for n in range(21)]
        for n, row in enumerate(made):  # al revés de su creación
            ProductVariant.objects.filter(pk=row.pk).update(sort_order=100 - n)
    allow(shop.world, "products.view")
    _, body = run(shop.world, "get_product_variants", {"product_id": str(shop.red.pk)})
    labels = [v["label"] for v in body["data"]["variants"]]
    assert labels == ["", "Grande", *(f"T{n:02}" for n in range(20, 2, -1))]


def test_as_of_comes_from_the_database_clock(shop: Any) -> None:
    def shift(execute: Any, statement: str, *args: Any) -> Any:
        moved = statement.replace("clock_timestamp()", "clock_timestamp() - interval '400 days'")
        return execute(moved, *args)

    allow(shop.world, "products.view")
    with connection.execute_wrapper(shift):
        done, body = run(shop.world, "search_product", {"query": "collar"})
    as_of = datetime.fromisoformat(body["meta"]["as_of"])
    assert done.evidence and done.evidence.as_of == as_of
    assert timedelta(days=399) < timezone.now() - as_of < timedelta(days=401)
    assert json.loads(done.text)["ok"] is True


def test_fits_reserves_the_longest_meta(shop: Any, monkeypatch: Any) -> None:
    allow(shop.world, "products.view")
    done, body = run(shop.world, "search_product", {"query": "collar"})
    size = len(done.text.encode())  # el sobre real, con su `meta`
    monkeypatch.setattr(registry, "MAX_BYTES", size - 1)
    assert not registry.fits(body["data"], "CATALOG")
    monkeypatch.setattr(registry, "MAX_BYTES", size + 32)  # el `as_of` puede ser más corto
    assert registry.fits(body["data"], "CATALOG")

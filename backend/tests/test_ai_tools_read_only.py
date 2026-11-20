"""F8-08c (#1020; ADR-034 §6, §9, §11): las tools listas para el bucle del copiloto. Cada llamada
corre en su propio `tenant_scope` de solo lectura (PostgreSQL rechaza lo que escribe, se escriba
como se escriba), `get_price` manda las condiciones de cada promoción una vez y las recorta antes
que fallar, resuelve en el instante de la evidencia, y la evidencia guarda lo que vio el modelo.
PostgreSQL con el rol `crm_app`."""

import dataclasses
import json
import threading
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

import psycopg
import pytest
from django.db import DatabaseError, connection, transaction
from django.utils import timezone

from apps.ai_agents import registry
from apps.ai_agents.registry import Found, invoke, invoke_read_only, read_only_scope
from apps.ai_agents.tools import catalog, price
from apps.ai_agents.tools.catalog import SEARCH_PRODUCT
from apps.ai_gateway.llm import ToolCall
from apps.catalog import products as pc
from apps.catalog.models import Brand
from apps.pricing.prices import set_price
from core.tenancy.context import ActorType, TenantContext, TenantContextError
from core.tenancy.scope import tenant_scope
from tests.ai_queries import captured, writes
from tests.logs import log_text
from tests.test_ai_price_tool import ask, store
from tests.test_ai_read_tools import allow, run, shop, world
from tests.test_authorization import acting
from tests.test_memberships import ctx
from tests.test_pricing_engine import person
from tests.test_promotion_tables import promo

pytestmark = pytest.mark.usefixtures("tenant_db")
__all__ = ["shop", "store", "world"]  # los fixtures, para pytest
SEARCH = ToolCall("toolu_1", "search_product", {"query": "collar"})
NEW_BRAND = "INSERT INTO brands (id, name) VALUES (gen_random_uuid(), 'Escrita')"
SESSION = "INSERT INTO django_session VALUES ('b1020', 'x', now() + interval '1 day')"


def faked(monkeypatch: Any, handler: Any) -> None:
    """`search_product` con otro handler: una tool registrada solo en esta prueba."""
    monkeypatch.setitem(registry._TOOLS, "search_product", replace(SEARCH_PRODUCT, handler=handler))


def executing(*statements: str) -> Any:
    def handler(ectx: Any, args: Any, as_of: datetime) -> Found:
        with connection.cursor() as cursor:
            for statement in statements:
                cursor.execute(statement)
        return Found({"products": []}, ())

    return handler


def brands(world: Any) -> int:
    with tenant_scope(ctx(world.a)):
        return Brand.objects.count()


def thaw(value: Any) -> Any:
    if isinstance(value, tuple):
        return [thaw(inner) for inner in value]
    if hasattr(value, "items"):
        return {key: thaw(inner) for key, inner in value.items()}
    return value


# La transacción de solo lectura: lo garantiza PostgreSQL, no una búsqueda en el SQL.
@pytest.mark.parametrize(
    ("statements", "error"),
    [
        ((NEW_BRAND,), psycopg.errors.ReadOnlySqlTransaction),
        ((f"DO $w$ BEGIN {NEW_BRAND}; END $w$",), psycopg.errors.ReadOnlySqlTransaction),
        ((NEW_BRAND.replace("brands", 'U&"br\\0061nds"'),), psycopg.errors.ReadOnlySqlTransaction),
        (("SELECT lo_create(0)",), psycopg.errors.ReadOnlySqlTransaction),
        (('SELECT 1 FROM "brands" FOR UPDATE',), psycopg.errors.ReadOnlySqlTransaction),
        (("set transaction read write", NEW_BRAND), psycopg.errors.ActiveSqlTransaction),
    ],
)
def test_a_tool_that_writes_fails_with_the_read_only_error_however_spelled(
    shop: Any, monkeypatch: Any, statements: tuple[str, ...], error: type[Exception]
) -> None:
    allow(shop.world, "products.view")
    before = brands(shop.world)
    faked(monkeypatch, executing(*statements))
    with pytest.raises(DatabaseError) as raised:
        invoke_read_only(ctx(shop.world.a, shop.world.ana), SEARCH)
    assert type(raised.value.__cause__) is error
    assert brands(shop.world) == before


def test_an_orm_write_in_a_tool_also_fails(shop: Any, monkeypatch: Any) -> None:
    def handler(ectx: Any, args: Any, as_of: datetime) -> Found:
        Brand.objects.create(organization_id=ectx.tenant.organization_id, name="Escrita")
        return Found({"products": []}, ())

    allow(shop.world, "products.view")
    faked(monkeypatch, handler)
    with pytest.raises(DatabaseError) as raised:
        run(shop.world, "search_product", {"query": "collar"})
    assert type(raised.value.__cause__) is psycopg.errors.ReadOnlySqlTransaction


def test_a_commit_inside_a_tool_fails_and_its_write_is_rolled_back(
    shop: Any, monkeypatch: Any
) -> None:
    allow(shop.world, "products.view")
    faked(monkeypatch, executing("COMMIT", SESSION))  # la siguiente ya es otra transacción
    with pytest.raises(RuntimeError, match="solo lectura"):
        invoke_read_only(ctx(shop.world.a, shop.world.ana), SEARCH)
    assert sessions() == 0


def sessions() -> int:
    with connection.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM django_session WHERE session_key = 'b1020'")
        found: int = cursor.fetchone()[0]
    return found


def test_the_scope_never_commits_even_if_the_caller_swallows_the_failure(
    shop: Any, monkeypatch: Any
) -> None:
    allow(shop.world, "products.view")
    faked(monkeypatch, executing("COMMIT", SESSION))
    with read_only_scope(ctx(shop.world.a, shop.world.ana)) as ectx:
        with pytest.raises(RuntimeError, match="solo lectura"):
            invoke(ectx, SEARCH)  # el fallo se atrapa dentro del scope: aun así, nada queda
    assert sessions() == 0


def test_an_on_commit_callback_of_a_tool_never_runs(shop: Any, monkeypatch: Any) -> None:
    def handler(ectx: Any, args: Any, as_of: datetime) -> Found:
        transaction.on_commit(write)  # tras un commit que nunca llega
        return Found({"products": []}, ())

    def write() -> None:
        with connection.cursor() as cursor:
            cursor.execute(SESSION)

    allow(shop.world, "products.view")
    faked(monkeypatch, handler)
    assert invoke_read_only(ctx(shop.world.a, shop.world.ana), SEARCH).ok
    assert sessions() == 0


def test_advisory_locks_and_session_settings_are_left_to_the_query_capture(
    shop: Any, monkeypatch: Any
) -> None:
    allow(shop.world, "products.view")
    lock, session = "SELECT pg_advisory_xact_lock(1020)", "SELECT set_config('a.b', 'c', false)"
    faked(monkeypatch, executing(lock, session))
    with captured() as seen:
        done = invoke_read_only(ctx(shop.world.a, shop.world.ana), SEARCH)
    assert done.ok and writes(seen) == [lock, session]  # la transacción los admite; esto no


def test_invoke_refuses_a_transaction_that_can_write(shop: Any, monkeypatch: Any) -> None:
    allow(shop.world, "products.view")
    faked(monkeypatch, executing(NEW_BRAND))
    with acting(shop.world.a, shop.world.ana) as ectx, captured() as seen:
        with pytest.raises(RuntimeError, match="solo lectura"):
            invoke(ectx, SEARCH)
    assert writes(seen) == []  # el handler no llegó a correr


def test_the_read_only_scope_is_never_opened_inside_another(shop: Any) -> None:
    tenant = ctx(shop.world.a, shop.world.ana)
    with tenant_scope(tenant), pytest.raises(TenantContextError):
        invoke_read_only(tenant, SEARCH)
    with tenant_scope(tenant), connection.cursor() as cursor:  # y el de fuera sigue escribiendo
        cursor.execute("SELECT current_setting('transaction_read_only')")
        assert cursor.fetchone() == ("off",)


def test_without_an_active_membership_or_as_system_it_is_forbidden(shop: Any, caplog: Any) -> None:
    allow(shop.world, "products.view")
    system = TenantContext(shop.world.a, "test", shop.world.ana.pk, ActorType.SYSTEM, None)
    with caplog.at_level("INFO"):
        texts = {invoke_read_only(tenant, SEARCH).text
                 for tenant in (system, ctx(shop.world.b, shop.world.ana))}  # fmt: skip
    assert texts == {'{"error":{"code":"FORBIDDEN"},"ok":false}'}
    assert log_text(caplog).count("FORBIDDEN") == 2


# `get_price`: las condiciones una vez, recortadas antes que fallar.
def long_promo(store: Any, terms: str) -> UUID:
    """Una promoción para la IA en las tres listas que se citan, que gana a las demás."""
    lists = [store.lists[code] for code in ("REG", "OFE", "TIE")]
    return promo(store.world.a, lists, [{"variant_id": store.shop.big.pk}], "AMOUNT_OFF", "15",
                 timezone.now() - timedelta(days=1), timezone.now() + timedelta(days=7),
                 name="Promo Larga", terms=terms,
                 priority=5, ai_exposable=True)  # fmt: skip


def test_dense_terms_shared_by_three_lists_go_once_and_whole(store: Any) -> None:
    allow(store.world, "prices.view")
    terms = ("Ñáñé íóú ñúñó áéí. " * 100)[:1800]  # 3 127 bytes: repetida en 3 listas, no cabe
    made = long_promo(store, terms)
    done, body = ask(store, store.shop.big)
    rows = body["data"]["prices"]
    assert [(p["price_list"], p["promotion_id"]) for p in rows] == [
        (name, str(made)) for name in ("Precio Publico", "Oferta Web", "Tienda")]  # fmt: skip
    assert body["data"]["promotions"] == [{"promotion_id": str(made), "terms": terms}]
    assert done.text.count(terms) == 1


def test_emoji_dense_terms_are_cut_with_the_mark_before_any_list(store: Any) -> None:
    allow(store.world, "prices.view")
    terms = "🐶🎁" * 900  # 1 800 caracteres, 7 200 bytes
    made = long_promo(store, terms)
    done, body = ask(store, store.shop.big)
    assert done.ok and len(body["data"]["prices"]) == 3
    [shown] = body["data"]["promotions"]
    kept = shown["terms"].removesuffix(price.CUT)
    assert shown["promotion_id"] == str(made) and shown["terms"].endswith(price.CUT)
    assert 0 < len(kept) < 1800 and terms.startswith(kept)
    longer = {
        **body["data"],
        "promotions": [{**shown, "terms": terms[: len(kept) + 2] + price.CUT}],
    }
    assert not registry.fits(longer, "PRICE")  # el recorte es el más largo que cabe (± 1)
    assert done.evidence is not None and thaw(done.evidence.data) == body["data"]


def test_prices_resolve_at_the_evidence_instant_not_at_the_scope_start(
    store: Any, monkeypatch: Any
) -> None:
    """Un precio que cambia (en otra conexión) después de empezar el scope de la tool y antes de
    su `as_of`: se cita el que rige en `as_of`."""
    allow(store.world, "prices.view")
    failed: list[BaseException] = []
    pricer = person(store.world, {"prices.manage": None})  # la base se lo pide (pricing/0011)

    def change() -> None:
        tenant = ctx(store.world.a, pricer)
        try:
            with tenant_scope(tenant):
                set_price(tenant, variant_id=store.shop.big.pk, price_list_id=store.lists["TIE"],
                          amount="61.00", reason="Cambio de precio")  # fmt: skip
        except BaseException as error:  # noqa: BLE001 — se comprueba fuera del hilo
            failed.append(error)
        finally:
            connection.close()

    def precheck() -> None:  # ya dentro del scope de la tool, antes de su `as_of`
        other = threading.Thread(target=change)
        other.start()
        other.join()

    monkeypatch.setitem(registry._TOOLS, "get_price", replace(price.GET_PRICE, precheck=precheck))
    _, body = ask(store, store.shop.big)
    assert failed == []
    assert [(p["price_list"], p["amount"]) for p in body["data"]["prices"]] == [
        ("Precio Publico", "49.90"), ("Oferta Web", "55.00"), ("Tienda", "61.00")]  # fmt: skip


# `get_product_variants`: menos filas, no `RESULT_TOO_LARGE`.
def test_twenty_long_variant_names_are_trimmed_to_what_fits(shop: Any) -> None:
    tenant = ctx(shop.world.a)
    with tenant_scope(tenant):
        made = pc.create_product(tenant, name="Arnés " + "Ñ" * 194, sku_code="ARN-LARGO")
        pc.update_product(tenant, product_id=made.pk, status="ACTIVE")
        for n in range(20):
            pc.create_variant(tenant, product_id=made.pk, sku_code=f"ARN-{n:02}",
                              tag=f"Talla {n:02} " + "É" * 91)  # fmt: skip
    allow(shop.world, "products.view")
    done, body = run(shop.world, "get_product_variants", {"product_id": str(made.pk)})
    rows = body["data"]["variants"]
    assert done.ok and 1 <= len(rows) < catalog.MAX_VARIANTS
    assert len(done.text.encode()) <= registry.MAX_BYTES
    assert done.evidence is not None
    assert done.evidence.subjects == (made.pk, *(UUID(v["variant_id"]) for v in rows))


# La evidencia guarda lo que vio el modelo: de solo lectura, fuera del `repr` y de la igualdad.
def test_evidence_keeps_the_data_the_model_saw_read_only(store: Any, caplog: Any) -> None:
    allow(store.world, "prices.view")
    with caplog.at_level("DEBUG"):
        done, body = ask(store, store.shop.big)
    evidence = done.evidence
    assert evidence is not None and thaw(evidence.data) == body["data"]
    assert evidence.data["prices"][0]["amount"] == "49.90"
    with pytest.raises(TypeError):
        evidence.data["prices"][0]["amount"] = "1.00"
    with pytest.raises(dataclasses.FrozenInstanceError):
        evidence.data = {}
    assert isinstance(evidence.data["prices"], tuple)
    for text in (repr(evidence), log_text(caplog)):  # el sobre (`done.text`) sí lo lleva
        assert "49.90" not in text and "Precio Publico" not in text
    assert evidence == dataclasses.replace(evidence, data={})
    assert hash(evidence) == hash(dataclasses.replace(evidence, data={}))
    assert json.loads(done.text)["data"] == body["data"]

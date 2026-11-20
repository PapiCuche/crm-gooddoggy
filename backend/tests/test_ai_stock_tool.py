"""F8-07d (#999; ADR-034 §6, §13 D-F8-07): `get_stock` da al modelo solo la franja y deja el
número exacto en la evidencia del servidor; cuenta solo los almacenes vivos, activos y vendibles.
Primero lo que se rechaza. PostgreSQL con el rol `crm_app`."""

import dataclasses
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings
from django.utils import timezone

from apps.ai_agents.registry import TOOLS, invoke
from apps.ai_agents.tools.stock import AVAILABLE, FEW_UNITS, NOT_TRACKED, OUT_OF_STOCK, band
from apps.ai_gateway.llm import ToolCall
from apps.catalog.models import ProductVariant
from apps.inventory.models import InventoryLevel, Warehouse
from apps.organizations.models import Branch
from core.tenancy.scope import tenant_scope
from tests.ai_queries import captured, writes
from tests.logs import log_text
from tests.test_ai_read_tools import active, allow, run, shop, world
from tests.test_authorization import acting
from tests.test_memberships import ctx

pytestmark = pytest.mark.usefixtures("tenant_db")
__all__ = ["shop", "world"]  # los fixtures, para pytest
SENTINEL = 731942  # el número exacto: nunca fuera de la evidencia
FORBIDDEN = {"ok": False, "error": {"code": "FORBIDDEN"}}
NOT_FOUND = '{"error":{"code":"NOT_FOUND"},"ok":false}'


def house(code: str, **fields: Any) -> Warehouse:
    branch = Branch.objects.get_or_create(code="LIM", defaults={"name": "Lima"})[0]
    made: Warehouse = Warehouse.objects.create(branch=branch, code=code, name=f"Almacen {code}",
                                               **fields)  # fmt: skip
    return made


def put(variant: Any, where: Warehouse, on_hand: int, reserved: int = 0) -> None:
    values = {"on_hand": on_hand, "reserved": reserved}
    InventoryLevel.objects.update_or_create(variant_id=variant.pk, warehouse=where, defaults=values)


@pytest.fixture
def store(shop: Any) -> SimpleNamespace:
    """En A: dos almacenes vendibles y tres que no cuentan (no vendible, inactivo, borrado), cada
    uno con 100 unidades del collar grande; un servicio. En B: stock del collar ajeno."""
    tenant, made = ctx(shop.world.a), SimpleNamespace(shop=shop, world=shop.world)
    with tenant_scope(tenant):
        made.main, made.side = house("MAIN"), house("SIDE")
        back, off = house("BACK", is_sellable=False), house("OFF", is_active=False)
        gone = house("GONE")
        for where in (back, off, gone):
            put(shop.big, where, 100)
        Warehouse.objects.filter(pk=gone.pk).update(deleted_at=timezone.now())  # con su saldo
        made.service = active(tenant, "Bano Canino", product_type="SERVICE").variants.get()
        made.hidden = list(ProductVariant.objects.filter(label__in=["Mini", "Secreta", "Vieja"]))
        made.hidden += [ProductVariant.objects.get(product=p) for p in (
            shop.draft, shop.hidden, shop.ended, shop.gone, shop.bare)]  # fmt: skip
    with tenant_scope(ctx(shop.world.b)):
        made.foreign = shop.foreign.variants.get()
        put(made.foreign, house("MAIN"), 50)
    return made


@pytest.fixture
def seller(store: SimpleNamespace) -> SimpleNamespace:
    """`store` con `inventory.view` para la persona que usa el copiloto."""
    allow(store.world, "inventory.view")
    return store


def stock(store: Any, variant: Any) -> tuple[Any, dict[str, Any]]:
    return run(store.world, "get_stock", {"variant_id": str(variant.pk)})


@pytest.mark.parametrize("granted", [(), ("products.view",), ("products.view", "prices.view")])
def test_get_stock_needs_inventory_view_of_the_person(store: Any, granted: tuple[str, ...]) -> None:
    allow(store.world, *granted)
    done, body = stock(store, store.shop.big)
    assert body == FORBIDDEN and done.evidence is None


@pytest.mark.parametrize("value", ["no-es-un-uuid", 7, None, ["x"], "%s ", "{%s}"])
def test_a_malformed_variant_id_is_rejected_without_echo(seller: Any, value: Any) -> None:
    value = value % seller.shop.big.pk if isinstance(value, str) and "%s" in value else value
    done, _ = run(seller.world, "get_stock", {"variant_id": value})
    assert done.text == '{"error":{"code":"INVALID_INPUT"},"ok":false}' and done.evidence is None
    extra = {"variant_id": str(seller.shop.big.pk), "warehouse_id": str(seller.main.pk)}
    assert run(seller.world, "get_stock", extra)[0].code == "INVALID_INPUT"


def test_unpublished_foreign_and_missing_variants_are_the_same_not_found(seller: Any) -> None:
    texts = {stock(seller, v)[0].text for v in (*seller.hidden, seller.foreign)}
    texts.add(run(seller.world, "get_stock", {"variant_id": str(uuid4())})[0].text)
    assert texts == {NOT_FOUND} and len(seller.hidden) == 8


@pytest.mark.parametrize(
    ("units", "expected"), [(0, OUT_OF_STOCK), (1, FEW_UNITS), (3, FEW_UNITS), (4, AVAILABLE)]
)
def test_the_band_at_its_edges_counts_available_not_on_hand(
    seller: Any, units: int, expected: str
) -> None:
    with tenant_scope(ctx(seller.world.a)):
        put(seller.shop.big, seller.main, units + 5, 5)  # lo reservado no está disponible
    done, body = stock(seller, seller.shop.big)
    assert body["data"] == {"variant_id": str(seller.shop.big.pk), "availability": expected}
    assert done.evidence is not None and dict(done.evidence.facts) == {"available": units}


def test_only_live_active_sellable_warehouses_count_and_they_add_up(seller: Any) -> None:
    assert stock(seller, seller.shop.big)[1]["data"]["availability"] == OUT_OF_STOCK  # 300 fuera
    with tenant_scope(ctx(seller.world.a)):
        put(seller.shop.big, seller.main, 2)
        put(seller.shop.big, seller.side, 2)
    done, body = stock(seller, seller.shop.big)
    assert body["data"]["availability"] == AVAILABLE and done.evidence is not None
    assert dict(done.evidence.facts) == {"available": 4}


def test_a_product_without_stock_is_not_tracked_and_a_tracked_one_without_rows_is_out(
    seller: Any,
) -> None:
    done, body = stock(seller, seller.service)
    assert body["data"]["availability"] == NOT_TRACKED and done.evidence is not None
    assert dict(done.evidence.facts) == {"available": None}
    with tenant_scope(ctx(seller.world.a)):
        plain = ProductVariant.objects.get(product=seller.shop.red, is_default=True)
    assert stock(seller, plain)[1]["data"]["availability"] == OUT_OF_STOCK


def test_the_band_function_and_its_threshold() -> None:
    assert [band(n, 3) for n in (None, -2, 0, 1, 3, 4)] == [
        NOT_TRACKED, OUT_OF_STOCK, OUT_OF_STOCK, FEW_UNITS, FEW_UNITS, AVAILABLE]  # fmt: skip
    assert (band(1, 1), band(2, 1), band(10, 10), band(11, 10)) == (
        FEW_UNITS, AVAILABLE, FEW_UNITS, AVAILABLE)  # fmt: skip


def test_the_threshold_is_the_server_setting(seller: Any) -> None:
    with tenant_scope(ctx(seller.world.a)):
        put(seller.shop.big, seller.main, 2)
    assert stock(seller, seller.shop.big)[1]["data"]["availability"] == FEW_UNITS
    with override_settings(AI_STOCK_FEW_UNITS=1):
        assert stock(seller, seller.shop.big)[1]["data"]["availability"] == AVAILABLE


@pytest.mark.parametrize("value", [0, -1, "3", 3.0, True, None])
def test_a_bad_threshold_fails_closed_before_any_query(seller: Any, value: Any) -> None:
    call = ToolCall("toolu_1", "get_stock", {"variant_id": str(seller.shop.big.pk)})
    with override_settings(AI_STOCK_FEW_UNITS=value), acting(seller.world.a, seller.world.ana) as e:
        with captured() as seen, pytest.raises(ImproperlyConfigured):
            invoke(e, call)
    assert seen == []


def test_the_exact_number_stays_in_immutable_evidence_never_in_the_envelope_or_logs(
    seller: Any, caplog: Any
) -> None:
    with tenant_scope(ctx(seller.world.a)):
        put(seller.shop.big, seller.main, SENTINEL + 40, 40)
    with caplog.at_level("DEBUG"):
        done, body = stock(seller, seller.shop.big)
    evidence = done.evidence
    assert evidence is not None and dict(evidence.facts) == {"available": SENTINEL}
    assert body["meta"] == {"evidence_id": str(evidence.evidence_id), "kind": "STOCK",
                            "as_of": evidence.as_of.isoformat()}  # fmt: skip
    assert evidence.subjects == (seller.shop.red.pk, seller.shop.big.pk)
    for text in (done.text, log_text(caplog), repr(done), repr(evidence)):
        assert str(SENTINEL) not in text and str(SENTINEL + 40) not in text
    with pytest.raises(TypeError):
        evidence.facts["available"] = 1
    with pytest.raises(dataclasses.FrozenInstanceError):
        evidence.facts = {}
    assert dict(evidence.facts) == {"available": SENTINEL}


def test_no_output_carries_warehouses_locations_or_quantities(seller: Any) -> None:
    with tenant_scope(ctx(seller.world.a)):
        put(seller.shop.big, seller.main, 5281, 1013)  # 4268 disponibles
    done, body = stock(seller, seller.shop.big)
    assert set(body) == {"ok", "data", "meta"} and set(body["data"]) == {"variant_id",
                                                                        "availability"}  # fmt: skip
    assert body["data"]["availability"] == "AVAILABLE"
    for leak in ("MAIN", "Almacen", "Lima", str(seller.main.pk), "on_hand", "reserved", "COL-",
                 "777.77", '"available"', "5281", "1013", "4268"):  # fmt: skip
        assert leak not in done.text


def test_get_stock_is_registered_read_public_and_needs_inventory_view() -> None:
    spec = TOOLS["get_stock"]
    assert (spec.permission, spec.kind, list(spec.fields)) == ("inventory.view", "STOCK",
                                                               ["variant_id"])  # fmt: skip
    assert "número" in spec.description and "FEW_UNITS" in spec.description


@pytest.mark.parametrize("granted", [True, False])
def test_get_stock_writes_or_locks_nothing(store: Any, granted: bool) -> None:
    if granted:
        allow(store.world, "inventory.view")
    with acting(store.world.a, store.world.ana) as ectx, captured() as seen:
        for variant in (store.shop.big, store.service, store.foreign):
            invoke(ectx, ToolCall("toolu_1", "get_stock", {"variant_id": str(variant.pk)}))
    assert writes(seen) == [] and bool(seen) is granted  # sin permiso no se lee nada
    assert sum("is_sellable" in sql for sql in seen) == int(granted)  # solo el collar lleva stock

"""F8-07h (#1028): `crm_app` con SQL a mano dentro del scope de su organización no borra una lista
de precios para crearla otra vez y marcarla (`pricing/0011` le quita `DELETE`), y los precios y las
promociones, que cita la IA, solo los escribe una persona activa con `prices.manage` (o, un precio,
con `prices.bulk_update` en una lista que ve). La API, los comandos y el migrador siguen igual."""

from collections.abc import Callable
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import psycopg
import pytest
from django.core.management import call_command
from django.db import IntegrityError, ProgrammingError
from django.utils import timezone

from apps.pricing import prices
from apps.pricing import promotions as commands
from apps.pricing.models import PriceList, Promotion, PromotionItem, PromotionPriceList
from core.tenancy.context import ActorType, TenantContext
from core.tenancy.scope import tenant_scope
from tests.test_authorization import give
from tests.test_channel_accounts_api import signed
from tests.test_contacts_api import crm, real_stack
from tests.test_memberships import ctx, raw
from tests.test_pipelines_tables import as_migrator
from tests.test_price_list_ai_exposable import DB, LISTS, exposed, shop
from tests.test_price_list_ai_exposable_guard import refused as unmarked
from tests.test_pricing_engine import item
from tests.test_team_writes import send

__all__ = ["crm", "real_stack", "shop"]  # los fixtures, para pytest
pytestmark = pytest.mark.usefixtures("tenant_db")
GUARD, DAY = "prices_writer_guard", timedelta(days=1)
LIST = "SELECT code, required_permission, ai_exposable, deleted_at FROM price_lists WHERE id = %s"
PRICES = "SELECT price_list_id, amount FROM product_prices ORDER BY starts_at"
# un precio que empieza mañana y dura un día: cada `day` es otra vigencia, sin solaparse
PRICE = (
    "INSERT INTO product_prices (id, organization_id, variant_id, price_list_id, amount,"
    " currency, tax_included, starts_at, ends_at, change_reason, created_at) SELECT uuidv7(),"
    " organization_id, %s, %s, {}, 'PEN', true, now() + %s * interval '1 day',"
    " now() + (%s + 1) * interval '1 day', 'copiado', now() FROM price_lists WHERE id = %s"
)


def refused(context: TenantContext, write: Callable[[], Any]) -> None:
    with pytest.raises(IntegrityError) as error, tenant_scope(context):
        write()
    cause = error.value.__cause__  # el nombre de la guarda, no solo el texto
    assert isinstance(cause, psycopg.Error) and cause.diag.constraint_name == GUARD


def done(context: TenantContext, write: Callable[[], Any]) -> Any:
    with tenant_scope(context):
        return write()


def price(variant: UUID, listed: UUID, day: int, amount: str = "90") -> Callable[[], Any]:
    return lambda: raw(PRICE.format(amount), [variant, listed, day, day, listed])


def test_a_list_is_not_deleted_and_made_again_to_mark_it(
    shop: SimpleNamespace, migrator: DB
) -> None:
    """vera (`ai.manage`, solo `prices.view`) no borra MAYORISTA (sin precios) para crearla otra
    vez, con su `id` y su `code`, visible para `prices.view` y marcarla; ni el Owner la borra."""
    wholesale = shop.lists["wholesale"]
    give(shop.a, shop.m["vera"], {"ai.manage": None})
    vera = ctx(shop.a, shop.vera)
    for context in (vera, ctx(shop.a, shop.ana), ctx(shop.a)):
        for sql, params in (
            ("DELETE FROM price_lists WHERE id = %s", [wholesale]),
            ("TRUNCATE price_lists CASCADE", None),
        ):
            with (
                pytest.raises(ProgrammingError, match="permission denied"),
                tenant_scope(context),
            ):
                raw(sql, params)
    again = {"name": "Mayorista", "kind": "RETAIL", "currency": "PEN"}
    again |= {"required_permission": "prices.view"}
    with pytest.raises(IntegrityError, match="pkey"), tenant_scope(vera):  # sigue ahí
        PriceList.objects.create(id=wholesale, code="MAYORISTA", **again)
    unmarked(vera, "ai_exposable = true", wholesale)  # y sigue sin verla: `0009`
    found = migrator.execute(LIST, [wholesale]).fetchone()
    assert found == ("MAYORISTA", "prices.view_wholesale", False, None) and exposed(migrator) == []
    with tenant_scope(ctx(shop.a, shop.bea)):  # el borrado de la aplicación es lógico
        PriceList.objects.filter(pk=wholesale).update(deleted_at=timezone.now())


def test_only_a_person_who_manages_prices_writes_one_with_sql(
    shop: SimpleNamespace, migrator: DB
) -> None:
    """vera no copia a REGULAR (marcada) el precio de MAYORISTA (oculta); ni el sistema, ni la IA
    con el id del Owner, ni sam, ni bea suspendida o desactivada; bea (`prices.manage`) sí, y
    `prices.bulk_update` solo en una lista que ve. Quien no los gestiona tampoco cierra uno."""
    regular, wholesale = shop.lists["regular"], shop.lists["wholesale"]
    variant = item(shop.a)
    migrator.execute("UPDATE price_lists SET ai_exposable = true WHERE id = %s", [regular])
    with as_migrator(), tenant_scope(ctx(shop.a)):  # la semilla, como el migrador
        raw(PRICE.format("70"), [variant, wholesale, 1, 1, wholesale])
    give(shop.a, shop.m["vera"], {"ai.manage": None})
    vera = ctx(shop.a, shop.vera)
    copy = PRICE.format("(SELECT amount FROM product_prices WHERE price_list_id = %s)")
    refused(vera, lambda: raw(copy, [variant, regular, wholesale, 1, 1, regular]))
    outsiders = (
        vera,
        ctx(shop.a),
        ctx(shop.a, shop.sam),
        TenantContext(shop.a, "test", shop.ana.pk, ActorType.AI_AGENT, shop.ana.pk),
    )
    for context in outsiders:
        refused(context, price(variant, regular, 1))
    bea = ctx(shop.a, shop.bea)
    member, user = "organization_memberships SET status", "users SET is_active"
    for broken, fixed, target in (
        (f"{member} = 'SUSPENDED'", f"{member} = 'ACTIVE'", shop.m["bea"]),
        (f"{user} = false", f"{user} = true", shop.bea.pk),
    ):
        migrator.execute(f"UPDATE {broken} WHERE id = %s", [target])
        refused(bea, price(variant, regular, 1))
        migrator.execute(f"UPDATE {fixed} WHERE id = %s", [target])
    done(bea, price(variant, regular, 1, "80"))
    close = "UPDATE product_prices SET ends_at = ends_at - interval '1 hour' WHERE amount = 80"
    refused(vera, lambda: raw(close))
    give(shop.a, shop.m["vera"], {"prices.bulk_update": None})  # el que aplicará un lote (#849)
    refused(vera, price(variant, wholesale, 2))  # no ve MAYORISTA
    done(vera, price(variant, regular, 2, "85"))
    assert done(vera, lambda: raw(close)) == [(1,)]
    found = migrator.execute(PRICES).fetchall()
    assert found == [(wholesale, Decimal(70)), (regular, Decimal(80)), (regular, Decimal(85))]


def test_only_a_person_who_manages_prices_writes_a_promotion_with_sql(
    shop: SimpleNamespace, migrator: DB
) -> None:
    """Ni crearla, ni añadirle objetivos o listas, ni publicarla o cancelarla sin `prices.manage`,
    tampoco con `prices.bulk_update`: una promoción baja (o fija) el precio que cita la IA."""
    regular = shop.lists["regular"]
    variant = item(shop.a)
    give(shop.a, shop.m["vera"], {"ai.manage": None, "prices.bulk_update": None})
    vera, bea = ctx(shop.a, shop.vera), ctx(shop.a, shop.bea)
    now = timezone.now()
    terms = {"name": "Rebaja", "discount_type": "PERCENT_OFF", "value": Decimal(50)}
    terms |= {"currency": "PEN", "starts_at": now + DAY, "ends_at": now + 2 * DAY}
    for context in (vera, ctx(shop.a)):
        refused(context, lambda: Promotion.objects.create(**terms))
    made: Promotion = done(bea, lambda: Promotion.objects.create(**terms))
    child = {"promotion_id": made.pk}
    refused(vera, lambda: PromotionItem.objects.create(variant_id=variant, **child))
    refused(vera, lambda: PromotionPriceList.objects.create(price_list_id=regular, **child))
    done(bea, lambda: PromotionItem.objects.create(variant_id=variant, **child))
    done(bea, lambda: PromotionPriceList.objects.create(price_list_id=regular, **child))
    for status in ("PUBLISHED", "CANCELLED"):

        def move(to: str = status) -> int:
            return Promotion.objects.filter(pk=made.pk).update(status=to)

        refused(vera, move)
        assert done(bea, move) == 1


def test_the_app_paths_still_write_prices_and_promotions(
    shop: SimpleNamespace, migrator: DB
) -> None:
    """bea (`prices.manage`, no Owner): la API fija un precio y cierra el anterior; los comandos
    crean, publican y cancelan una promoción. El migrador, propietario, no pasa por la guarda."""
    regular, variant = shop.lists["regular"], item(shop.a)
    path = f"{LISTS}{regular}/prices/"
    for amount in ("100", "110"):
        body = {"variant_id": str(variant), "amount": amount, "reason": "Lista nueva"}
        created = send(signed(shop.bea), "post", path, body)
        assert created.status_code == 201, created.json()
    with tenant_scope(ctx(shop.a, shop.ana)) as tenant:  # el Owner, por el comando
        prices.set_price(tenant, variant_id=variant, price_list_id=regular, amount="120",
                         reason="Ajuste", starts_at=timezone.now() + DAY)  # fmt: skip
    found = [row[1] for row in migrator.execute(PRICES).fetchall()]
    assert found == [Decimal(100), Decimal(110), Decimal(120)]
    now = timezone.now()
    wanted: dict[str, Any] = {"name": "Campaña", "discount_type": "PERCENT_OFF", "value": "10"}
    wanted |= {"starts_at": now + DAY, "ends_at": now + 2 * DAY, "price_list_ids": [regular]}
    with tenant_scope(ctx(shop.a, shop.bea)) as tenant:
        made = commands.create_promotion(tenant, items=[{"variant_id": variant}], **wanted)
    for move in (commands.publish_promotion, commands.cancel_promotion):
        with tenant_scope(ctx(shop.a, shop.bea)) as tenant:
            move(tenant, promotion_id=made.pk)
    other = item(shop.a, "IP17")
    migrator.execute(PRICE.format("5"), [other, regular, 1, 1, regular])  # el propietario


def test_the_runtime_cannot_switch_it_off_and_the_reverse_removes_it(
    shop: SimpleNamespace, migrator: DB
) -> None:
    escapes = (
        "ALTER TABLE product_prices DISABLE TRIGGER product_prices_writer_guard",
        "DROP TRIGGER promotions_writer_guard ON promotions",
        f"DROP FUNCTION {GUARD}()",
        f"CREATE OR REPLACE FUNCTION {GUARD}() RETURNS trigger LANGUAGE plpgsql"
        " AS 'BEGIN RETURN NEW; END'",
    )
    for sql in escapes:
        with (
            pytest.raises(ProgrammingError, match="must be owner|permission denied"),
            tenant_scope(ctx(shop.a, shop.ana)),
        ):
            raw(sql)
    installed = (
        "SELECT c.relname, p.prosecdef, p.proconfig, t.tgenabled, t.tgtype FROM pg_trigger t"
        " JOIN pg_proc p ON p.oid = t.tgfoid JOIN pg_class c ON c.oid = t.tgrelid"
        " WHERE p.proname = %s ORDER BY c.relname"
    )
    fixed = (False, ["search_path=pg_catalog, public, pg_temp"], "O")
    # BEFORE … FOR EACH ROW (1+2), INSERT (4), UPDATE (16)
    assert migrator.execute(installed, [GUARD]).fetchall() == [
        ("product_prices", *fixed, 23),
        ("promotion_items", *fixed, 7),
        ("promotion_price_lists", *fixed, 7),
        ("promotions", *fixed, 23),
    ]
    deletes = "SELECT has_table_privilege(%s, 'price_lists', 'DELETE')"
    assert migrator.execute(deletes, ["crm_app"]).fetchone() == (False,)
    try:
        with as_migrator():
            call_command("migrate", "pricing", "0010", verbosity=0)
        assert migrator.execute(installed, [GUARD]).fetchall() == []
        assert migrator.execute(deletes, ["crm_app"]).fetchone() == (True,)
        done(ctx(shop.a), price(item(shop.a), shop.lists["regular"], 1))  # sin ella, como antes
    finally:
        with as_migrator():
            call_command("migrate", "pricing", verbosity=0)
    refused(ctx(shop.a), price(item(shop.a, "IP17"), shop.lists["regular"], 1))

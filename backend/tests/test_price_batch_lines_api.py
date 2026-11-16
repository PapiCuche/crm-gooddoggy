"""F3-49c: las líneas de un lote de edición masiva (`…/pricing/batches/{id}/items/`) y cancelarlo
(`…/cancel/`), por HTTP con sesión, el motor de autorización y PostgreSQL con el rol `crm_app`.
Cancelar no cambia ningún precio; ninguna línea enseña costo, margen ni suelo."""

from dataclasses import replace
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

from apps.access.selectors import execution_context
from apps.pricing import batches
from apps.pricing.api.batches import NOT_CANCELLABLE, REFUSED
from apps.pricing.models import PriceChangeBatch
from apps.pricing.selectors import batch_lines
from core.tenancy.context import ActorType
from core.tenancy.resolution import resolve_tenant
from core.tenancy.scope import tenant_scope
from tests import test_price_batches_api
from tests.test_member_branch import DENIED
from tests.test_memberships import ctx
from tests.test_price_batches import EDITOR, PAST, apple, create
from tests.test_price_batches_api import BASE, GRANTS, WRITES, editor
from tests.test_prices import logged, price_list
from tests.test_pricing_engine import person, row
from tests.test_products import send
from tests.test_promotion_tables import stock
from tests.test_self_context import NOT_FOUND

world, shop = test_price_batches_api.world, test_price_batches_api.shop
real_stack = test_price_batches_api.real_stack
pytestmark = pytest.mark.usefixtures("tenant_db")
LINE = {"id", "variant", "status", "old_amount", "new_amount", "delta_percent", "alert", "error"}
STATUS = "UPDATE price_change_batches SET status = %s WHERE id = %s"


def items(client: Client, batch: Any, query: str = "") -> Any:
    return client.get(f"{BASE}{batch}/items/{query}")


def cancel(client: Client, batch: Any) -> Any:
    return send(client, "post", f"{BASE}{batch}/cancel/")


def selected(world: Any, user: Any, batch: PriceChangeBatch) -> int:
    """Cuántas líneas da el selector a `user`, sin pasar por la ruta y su 404."""
    with tenant_scope(ctx(world.a, user)) as tenant:
        return batch_lines(execution_context(tenant), batch.pk).count()


def iphone(world: Any, shop: Any) -> PriceChangeBatch:
    return create(
        world, shop.editor, shop.regular, {"category_id": shop.iphone}, rounding="INTEGER"
    )


def test_the_lines_of_minus_5_percent_to_iphone_in_regular_and_their_filters(
    world: Any, shop: Any
) -> None:
    """La aceptación de F3-49c: SKU, nombre, antes y después; `?status=` y `?alert=` filtran, y
    ningún campo de costo, margen ni suelo."""
    made, client = iphone(world, shop), editor(world)
    answer = items(client, made.pk)
    assert answer.status_code == 200, answer.content
    page = answer.json()
    assert not [word for word in ("cost", "margin", "floor") if word in answer.content.decode()]
    assert page["next"] is None and all(set(line) == LINE for line in page["results"])
    shown = {
        line["variant"]["id"]: (
            line["variant"]["sku"] == line["variant"]["name"] != "",
            line["status"],
            line["old_amount"],
            line["new_amount"],
            line["delta_percent"],
            line["alert"],
            line["error"],
        )
        for line in page["results"]
    }
    assert shown == {
        str(shop.ip16): (True, "PENDING", "4399.00", "4179.00", "-5.00", False, ""),
        str(shop.ip16_pro): (True, "PENDING", "5199.90", "4940.00", "-5.00", False, ""),
        str(shop.ip15): (True, "SKIPPED", None, None, None, False, batches.NO_PRICE),
    }
    assert [line["id"] for line in page["results"]] == sorted(
        line["id"] for line in page["results"]
    )
    skipped = items(client, made.pk, "?status=SKIPPED").json()["results"]
    assert [line["variant"]["id"] for line in skipped] == [str(shop.ip15)]
    assert items(client, made.pk, "?alert=true").json()["results"] == []
    assert len(items(client, made.pk, "?alert=false&status=PENDING").json()["results"]) == 2


def test_the_cursor_walks_the_lines_by_creation(world: Any, shop: Any) -> None:
    made, client = iphone(world, shop), editor(world)
    first = items(client, made.pk, "?limit=2").json()
    rest = items(client, made.pk, f"?limit=2&cursor={first['next']}").json()
    assert (len(first["results"]), len(rest["results"]), rest["next"]) == (2, 1, None)
    everything = items(client, made.pk).json()["results"]
    assert first["results"] + rest["results"] == everything


def priced(world: Any, operation: str, value: str, olds: list[str | None]) -> PriceChangeBatch:
    """Un lote de una lista nueva sobre una variante por precio de antes (`None`: sin precio)."""
    listed, ids = price_list(world.a, f"L{uuid4().hex[:8].upper()}"), []
    for old in olds:
        _, variant = stock(world.a)
        if old is not None:
            row(world.a, (variant, listed), old, PAST)
        ids.append(variant)
    filters = {"variant_ids": ids}
    return create(world, person(world, EDITOR), listed, filters, operation=operation, value=value)


@pytest.mark.parametrize(
    ("operation", "value", "deltas"),
    [  # justo el 30 % no es alerta; por poco más, en un sentido u otro, sí
        ("AMOUNT_INCREASE", "30", {"30.00": False, "30.30": True, None: False}),
        ("PERCENT_DECREASE", "30", {"-30.00": False}),
        ("PERCENT_DECREASE", "30.01", {"-30.01": True}),
        ("PERCENT_INCREASE", "30.01", {"30.01": True}),
    ],
)
def test_the_alert_filter_counts_what_the_summary_counts(
    world: Any, operation: str, value: str, deltas: dict[str | None, bool]
) -> None:
    olds = {"30.00": "100", "30.30": "99", None: None}
    made = priced(world, operation, value, [olds.get(delta, "100") for delta in deltas])
    client = editor(world)
    lines = items(client, made.pk).json()["results"]
    assert {line["delta_percent"]: line["alert"] for line in lines} == deltas
    alerts = items(client, made.pk, "?alert=true").json()["results"]
    rest = items(client, made.pk, "?alert=false").json()["results"]
    assert made.summary["alerts"] == len(alerts) == sum(deltas.values())
    assert {line["delta_percent"] for line in alerts} == {d for d, on in deltas.items() if on}
    assert {line["delta_percent"] for line in rest} == {d for d, on in deltas.items() if not on}


@pytest.mark.parametrize(
    "query",
    ["?status=APPLIEDX", "?status=", "?status=skipped", "?alert=maybe", "?alert=", "?limit=0"],
)
def test_each_bad_filter_is_a_400_on_its_field_never_a_500(
    world: Any, shop: Any, query: str
) -> None:
    answer = items(editor(world), apple(world, shop).pk, query)
    assert answer.status_code == 400, answer.content
    field = query[1:].split("=")[0]
    assert (answer.json()["code"], set(answer.json()["fields"])) == ("VALIDATION_ERROR", {field})


@pytest.mark.parametrize("missing", sorted(GRANTS))
def test_each_permission_is_needed_before_reading_or_cancelling(
    world: Any, shop: Any, missing: str, migrator: psycopg.Connection[Any]
) -> None:
    made = apple(world, shop)
    client = editor(world, {k: v for k, v in GRANTS.items() if k != missing})
    before = migrator.execute(WRITES).fetchone()
    for answer in (
        items(client, made.pk),
        items(client, made.pk, "?status=NOPE"),
        items(client, uuid4()),
        cancel(client, made.pk),
        cancel(client, uuid4()),
    ):
        assert (answer.status_code, answer.content) == DENIED
    assert migrator.execute(WRITES).fetchone() == before
    assert logged(migrator, "price_change_batches", "status") == [("PREVIEWED",)]


def test_another_organization_or_a_hidden_list_is_not_found(
    world: Any, shop: Any, migrator: psycopg.Connection[Any]
) -> None:
    wholesale = price_list(world.a, "MAYORISTA", required_permission="prices.view_wholesale")
    manager = person(world, {"prices.manage": None, "prices.bulk_update": None})
    hidden = create(world, manager, wholesale, {"brand_id": shop.apple})
    (_, foreign), foreign_list = stock(world.b), price_list(world.b)
    with tenant_scope(ctx(world.b, person(world, EDITOR, world.b))) as tenant:
        theirs = batches.create_batch(
            tenant,
            price_list_id=foreign_list,
            operation="PERCENT_DECREASE",
            value="5",
            filters={"variant_ids": [foreign]},
            reason="Campaña",
        )
    client = editor(world)
    for batch in (hidden.pk, theirs.pk, uuid4()):
        for answer in (
            items(client, batch),
            items(client, batch, "?alert=true"),
            cancel(client, batch),
        ):
            assert (answer.status_code, answer.content) == NOT_FOUND
    assert logged(migrator, "price_change_batches", "status") == [("PREVIEWED",)] * 2
    sees_all = editor(world, {**GRANTS, "prices.manage": None})
    assert items(sees_all, hidden.pk).status_code == 200
    assert (selected(world, shop.editor, hidden), selected(world, manager, hidden)) == (0, 5)


def test_a_deactivated_list_hides_the_lines_but_not_the_batch_and_it_can_be_cancelled(
    world: Any, shop: Any, migrator: psycopg.Connection[Any]
) -> None:
    """Decisión de F3-49c: las líneas enseñan precios y una lista desactivada ya no los enseña
    (`…/price-lists/{id}/prices/` da 404), así que sus líneas tampoco, ni con `prices.manage`."""
    made = apple(world, shop)
    migrator.execute(
        "UPDATE price_lists SET is_active = false, is_default = false WHERE id = %s",
        (shop.regular,),
    )
    client = editor(world, {**GRANTS, "prices.manage": None})
    assert (items(client, made.pk).status_code, items(client, made.pk).content) == NOT_FOUND
    assert client.get(f"{BASE}{made.pk}/").status_code == 200
    assert cancel(client, made.pk).json()["status"] == "CANCELLED"
    assert selected(world, shop.editor, made) == 0  # el selector tampoco, sin la ruta


def test_cancelling_a_previewed_batch_twice_changes_no_price_and_audits_once(
    world: Any, shop: Any, migrator: psycopg.Connection[Any]
) -> None:
    made, client = apple(world, shop), editor(world)
    prices_before = logged(migrator, "product_prices", "*")
    lines_before = logged(migrator, "price_change_batch_items", "*")
    answer = cancel(client, made.pk)
    assert answer.status_code == 200, answer.content
    body = answer.json()
    assert (body["id"], body["status"], body["version"]) == (str(made.pk), "CANCELLED", 2)
    assert body == client.get(f"{BASE}{made.pk}/").json()
    again = cancel(client, made.pk)
    assert (again.status_code, again.json()) == (200, body)  # tal cual: ni versión ni auditoría
    assert logged(migrator, "product_prices", "*") == prices_before
    assert logged(migrator, "price_change_batch_items", "*") == lines_before
    audits = logged(migrator, "audit_logs", "action, entity_type, entity_id, changes")
    cancelled = [entry for entry in audits if entry[0] == "price.bulk_cancelled"]
    assert cancelled == [
        (
            "price.bulk_cancelled",
            "price_change_batch",
            made.pk,
            {"status": ["PREVIEWED", "CANCELLED"]},
        )
    ]


@pytest.mark.parametrize(
    ("status", "answer"),
    [("DRAFT", 200), ("APPLYING", 409), ("APPLIED", 409), ("FAILED", 409)],
)
def test_only_a_draft_or_previewed_batch_can_be_cancelled(
    world: Any, shop: Any, status: str, answer: int, migrator: psycopg.Connection[Any]
) -> None:
    made = apple(world, shop)
    migrator.execute(STATUS, (status, made.pk))
    before = migrator.execute(WRITES).fetchone()
    got = cancel(editor(world), made.pk)
    assert got.status_code == answer, got.content
    after = logged(migrator, "price_change_batches", "status, version")
    if answer == 409:
        assert got.json() == {"code": "BATCH_STATE_CONFLICT", "message": NOT_CANCELLABLE}
        assert (after, migrator.execute(WRITES).fetchone()) == ([(status, 1)], before)
    else:
        assert after == [("CANCELLED", 2)]


def test_a_person_without_her_user_gets_the_403_before_the_batch_is_read(
    world: Any, shop: Any, monkeypatch: pytest.MonkeyPatch, migrator: psycopg.Connection[Any]
) -> None:
    made, client = apple(world, shop), editor(world)
    monkeypatch.setattr(
        "core.tenancy.middleware.resolve_tenant",
        lambda *a: replace(resolve_tenant(*a), actor_id=uuid4()),
    )
    before = migrator.execute(WRITES).fetchone()
    for batch in (made.pk, uuid4()):
        answer = cancel(client, batch)
        assert answer.status_code == 403, answer.content
        assert answer.json() == {"code": "ACTOR_NOT_ALLOWED", "message": REFUSED}
    assert migrator.execute(WRITES).fetchone() == before
    assert logged(migrator, "price_change_batches", "status") == [("PREVIEWED",)]


@pytest.mark.parametrize("actor", [ActorType.AI_AGENT, ActorType.INTEGRATION])
def test_a_robot_over_http_is_turned_away_before_the_route(
    world: Any, shop: Any, monkeypatch: pytest.MonkeyPatch, actor: ActorType
) -> None:
    made, client = apple(world, shop), editor(world)
    monkeypatch.setattr(
        "core.tenancy.middleware.resolve_tenant",
        lambda *a: replace(resolve_tenant(*a), actor_type=actor, actor_id=uuid4()),
    )
    for answer in (cancel(client, made.pk), items(client, made.pk)):
        assert (answer.status_code, answer.content) == NOT_FOUND


@pytest.mark.parametrize("actor", [ActorType.AI_AGENT, ActorType.INTEGRATION, ActorType.SYSTEM])
def test_cancel_refuses_another_actor_before_its_first_query(
    world: Any, shop: Any, actor: ActorType
) -> None:
    made = apple(world, shop)
    with tenant_scope(ctx(world.a, shop.editor)) as tenant:
        robot = replace(tenant, actor_type=actor, actor_id=uuid4())
        with CaptureQueriesContext(connection) as seen, pytest.raises(batches.ActorRefused):
            batches.cancel_batch(robot, made.pk)
    assert seen.captured_queries == []


def test_cancel_locks_the_batch_row(world: Any, shop: Any) -> None:
    made = apple(world, shop)
    with tenant_scope(ctx(world.a, shop.editor)) as tenant:
        with CaptureQueriesContext(connection) as seen:
            assert batches.cancel_batch(tenant, made.pk).version == 2
    locks = [q["sql"] for q in seen.captured_queries if "FOR NO KEY UPDATE" in q["sql"]]
    assert [sql.endswith('FOR NO KEY UPDATE OF "price_change_batches"') for sql in locks] == [True]

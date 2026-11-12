"""F3-52: ninguna función SQL del repositorio resuelve una relación por `pg_temp`. `crm_app` puede
crear tablas temporales (TEMPORARY en la BD) y `pg_temp` se mira primero para relaciones y tipos
si el `search_path` de la función no lo nombra; las funciones lo fijan al final y cualifican."""

from typing import Any
from uuid import uuid4

import psycopg
import pytest
from django.db import connection

from tests.test_outbox import emit_in

pytestmark = pytest.mark.usefixtures("tenant_db")
FIXED = ["search_path=pg_catalog, public, pg_temp"]
FUNCTIONS = (
    "public.outbox_pending_organizations(integer)",  # SECURITY DEFINER: la más grave
    "public.audit_ensure_partitions(integer)",
    "public.platform_audit_ensure_partitions(integer)",
    "public.product_prices_immutable()",
    "public.promotions_guard()",
    "public.promotions_born_draft()",
    "public.promotion_children_guard()",
    "public.variant_costs_immutable()",
    "public.messages_delivery_advance()",  # F6-01b
    "public.channel_accounts_identity_fixed()",  # F7-02a
    "public.messages_queued_organizations(timestamptz, integer)",  # F6-04b, SECURITY DEFINER
    "public.messages_sending_organizations(timestamptz, integer)",  # F7-05c, SECURITY DEFINER
    "public.channel_accounts_health_due(timestamptz, integer)",  # F7-06b, SECURITY DEFINER
    "public.credentials_stale_organizations(integer, integer)",  # F7-02b, SECURITY DEFINER
    "public.webhook_ingress_insert(text, text, text, text, text, jsonb)",  # F7-03b
    "public.webhook_ingress_claim(integer, integer)",  # F7-03c
    "public.webhook_ingress_mark(uuid, integer, text, uuid, uuid, text)",
    "public.webhook_ingress_list(text, integer)",
    "public.webhook_ingress_purge(integer, integer)",
    "public.resolve_channel_account(text, text)",
    "public.quotes_guard()",  # F5-09b: guardas de la cotización, INVOKER
    "public.quote_items_guard()",
    "public.quote_approvals_guard()",
    "public.inventory_reservations_guard()",  # F3-33, INVOKER
)


def test_the_delivery_trigger_is_not_security_definer(migrator: psycopg.Connection[Any]) -> None:
    row = migrator.execute(
        "SELECT prosecdef FROM pg_proc"
        " WHERE oid = 'public.messages_delivery_advance()'::regprocedure"
    ).fetchone()
    assert row == (False,)  # corre con los privilegios de quien actualiza


@pytest.mark.parametrize("function", FUNCTIONS)
def test_every_function_names_pg_temp_last(
    function: str, migrator: psycopg.Connection[Any]
) -> None:
    row = migrator.execute(
        "SELECT proconfig FROM pg_proc WHERE oid = %s::regprocedure", [function]
    ).fetchone()
    assert row == (FIXED,)


def test_a_temp_table_cannot_shadow_the_outbox_discovery(orgs: dict[str, Any]) -> None:
    emit_in(orgs["A"])
    with connection.cursor() as cursor:
        cursor.execute(
            "CREATE TEMP TABLE outbox_events (organization_id uuid, published_at timestamptz, "
            "occurred_at timestamptz)"
        )
        cursor.execute("INSERT INTO pg_temp.outbox_events VALUES (%s, NULL, now())", [uuid4()])
        cursor.execute("SELECT public.outbox_pending_organizations(10)")
        assert cursor.fetchall() == [(orgs["A"],)]  # la fila falsa no aparece


@pytest.mark.parametrize(
    "function, shadows",
    [
        ("audit_ensure_partitions", ("audit_logs",)),
        ("platform_audit_ensure_partitions", ("platform_audit_logs",)),
    ],
)
def test_a_temp_table_cannot_shadow_the_partition_functions(
    function: str, shadows: tuple[str, ...], migrator: psycopg.Connection[Any]
) -> None:
    parent = shadows[0]
    migrator.execute(f"CREATE TEMP TABLE {parent} (id int)")  # noqa: S608 — nombres fijos
    try:
        migrator.execute(f"SELECT public.{function}(1)")  # noqa: S608
        assert migrator.execute(
            "SELECT count(*) FROM pg_inherits WHERE inhparent = %s::regclass",
            [f"pg_temp.{parent}"],
        ).fetchone() == (0,)
        assert migrator.execute(
            "SELECT count(*) FROM pg_inherits WHERE inhparent = %s::regclass", [f"public.{parent}"]
        ).fetchone() != (0,)
    finally:
        migrator.execute(f"DROP TABLE pg_temp.{parent}")  # noqa: S608

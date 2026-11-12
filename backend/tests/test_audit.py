"""Auditoría append-only, particionada y con RLS (ADR-011; docs/fase-0/04 §N)."""

import json
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import psycopg
import pytest
from django.core.management import call_command
from django.db import DatabaseError, connection

from apps.audit.services import Entity, Result, record
from core.tenancy.context import ActorType, TenantContext, TenantContextError
from core.tenancy.scope import tenant_scope
from tests.conftest import migrator_settings
from tests.test_tenancy import raw_count

pytestmark = pytest.mark.usefixtures("tenant_db")
SECRET = "sk-" + "ant-api03-" + "Qw7" * 30  # construido en ejecución (gitleaks sin allowlist)


def partition(months_ahead: int = 0) -> str:
    now = datetime.now(UTC)
    year, month = divmod(now.year * 12 + now.month - 1 + months_ahead, 12)
    return f"audit_logs_y{year}m{month + 1:02d}"


def test_record_stores_redacted_changes_in_the_tenant(
    orgs: dict[str, UUID], migrator: psycopg.Connection[Any]
) -> None:
    user = orgs["widget_A"]  # cualquier UUID sirve como actor
    tenant = TenantContext(orgs["A"], "test", user, ActorType.USER, user, "corr-audit")
    with tenant_scope(tenant):
        record(
            tenant,
            "widget.updated",
            Entity("widget", orgs["widget_A"], "W-" + "x" * 190 + SECRET),  # redacta y trunca
            {"name": ["antes", "después"], "api_key": [None, SECRET]},
            {"note": f"reintento con {SECRET}", "body": "texto del mensaje"},
            result=Result.DENIED,
            actor_label=f"bot {SECRET}",
        )
    assert raw_count("audit_logs") == 0  # T1 no vacía: sin tenant, 0 filas aunque existan
    with tenant_scope(TenantContext(orgs["B"], "test")):
        assert raw_count("audit_logs") == 0  # RLS: B no ve la auditoría de A
    row = migrator.execute(
        "SELECT organization_id, actor_type, actor_id, action, entity_type, entity_id, "
        "length(entity_label), changes::text, metadata::text, correlation_id, result, "
        "entity_label || actor_label "
        "FROM audit_logs"
    ).fetchone()
    assert row is not None
    assert row[:7] == (orgs["A"], "USER", user, "widget.updated", "widget", orgs["widget_A"], 200)
    assert json.loads(row[7]) == {"name": ["antes", "después"], "api_key": [None, "[REDACTED]"]}
    assert json.loads(row[8]) == {"note": "reintento con [REDACTED]", "body": "[REDACTED]"}
    assert row[9:11] == ("corr-audit", "DENIED")
    assert SECRET not in row[7] + row[8] + row[11] and "[REDACTED]" in row[11]


def test_record_requires_the_active_scope_and_valid_input(orgs: dict[str, UUID]) -> None:
    tenant = TenantContext(orgs["A"], "test")
    entity = Entity("widget", orgs["widget_A"])
    with pytest.raises(TenantContextError):
        record(tenant, "widget.updated", entity)  # sin scope
    with tenant_scope(tenant):
        with pytest.raises(TenantContextError):
            record(replace(tenant, actor_type=ActorType.AI_AGENT), "widget.updated", entity)
        for action, ent, changes in (
            ("Widget.Updated", entity, None),
            ("widget", entity, None),
            ("widget.updated", Entity("Widget"), None),
            ("widget.updated", entity, {"name": "sin antes/después"}),
        ):
            with pytest.raises(ValueError):
                record(tenant, action, ent, changes)


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE audit_logs SET action = 'x.y'",
        "DELETE FROM audit_logs",
        "TRUNCATE audit_logs",
        "SELECT count(*) FROM {partition}",
        "UPDATE {partition} SET action = 'x.y'",
        "DELETE FROM {partition}",
        "INSERT INTO {partition} (organization_id, actor_type, action, entity_type) "
        "VALUES (app_current_tenant(), 'SYSTEM', 'x.y', 'x')",
    ],
)
def test_crm_app_cannot_modify_or_delete_audit(
    orgs: dict[str, UUID], migrator: psycopg.Connection[Any], statement: str
) -> None:
    tenant = TenantContext(orgs["A"], "test")
    with tenant_scope(tenant):
        record(tenant, "widget.created", Entity("widget", orgs["widget_A"]))
    sql = statement.format(partition=partition())
    with pytest.raises(DatabaseError, match="permission denied"), tenant_scope(tenant):
        with connection.cursor() as cursor:
            cursor.execute(sql)
    row = migrator.execute("SELECT count(*), min(action) FROM audit_logs").fetchone()
    assert row == (1, "widget.created")  # la evidencia sigue intacta


def test_audit_privileges_and_monthly_partitions(migrator: psycopg.Connection[Any]) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT p FROM unnest(ARRAY['SELECT','INSERT','UPDATE','DELETE','TRUNCATE']) p "
            "WHERE has_table_privilege(current_user, 'audit_logs', p)"
        )
        assert [r[0] for r in cursor.fetchall()] == ["SELECT", "INSERT"]
        cursor.execute(
            "SELECT c.relname, has_table_privilege(current_user, c.oid, "
            "'SELECT,INSERT,UPDATE,DELETE,TRUNCATE') FROM pg_inherits i "
            "JOIN pg_class c ON c.oid = i.inhrelid WHERE i.inhparent = 'audit_logs'::regclass"
        )
        partitions = dict(cursor.fetchall())
        cursor.execute(
            "SELECT has_function_privilege('audit_ensure_partitions(integer)', 'EXECUTE')"
        )
        assert cursor.fetchone() == (False,)  # crm_app no crea particiones (sin DDL)
    assert not any(partitions.values())  # ningún privilegio directo sobre particiones
    assert {partition(k) for k in range(13)} <= set(partitions)  # mes actual + 12


def test_post_migrate_restores_the_partition_horizon(migrator: psycopg.Connection[Any]) -> None:
    migrator.execute(f"DROP TABLE {partition(12)}")  # simula un año sin desplegar
    runtime = dict(connection.settings_dict)  # el job de migraciones: migrate como crm_migrator
    connection.close()
    connection.settings_dict.update(migrator_settings())
    try:
        call_command("migrate", verbosity=0)  # post_migrate → audit_ensure_partitions(12)
    finally:
        connection.close()
        connection.settings_dict.clear()
        connection.settings_dict.update(runtime)
    exists = migrator.execute("SELECT to_regclass(%s) IS NOT NULL", [partition(12)]).fetchone()
    assert exists == (True,)


def test_record_only_lets_the_system_act_for_itself(
    orgs: dict[str, UUID], migrator: psycopg.Connection[Any]
) -> None:
    user = orgs["widget_A"]
    tenant = TenantContext(orgs["A"], "test", user, ActorType.USER, user)
    entity = Entity("widget", orgs["widget_A"])
    with tenant_scope(tenant):
        record(tenant, "widget.updated", entity)  # sin `actor`: el del contexto
        record(tenant, "widget.synced", entity, actor=(ActorType.SYSTEM, None))
        for forged in ((ActorType.USER, user), (ActorType.USER, None), ("PLATFORM_STAFF", user)):
            with pytest.raises(ValueError, match="actor"):
                record(tenant, "widget.forged", entity, actor=forged)
    rows = migrator.execute("SELECT actor_type, actor_id FROM audit_logs ORDER BY id").fetchall()
    assert rows == [("USER", user), ("SYSTEM", None)]

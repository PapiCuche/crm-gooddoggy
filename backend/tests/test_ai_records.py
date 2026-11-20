"""F8-08b (#1019): el permiso `ai.use` (ADR-034 §10, D-F8-08) y el registro del copiloto:
`ai_runs` y `ai_actions` de solo inserción y particionadas por mes, `ai_suggestions` cuyo texto
solo pasa a NULL, con FORCE RLS; `records.save` todo o nada, con la organización y quien pide de
la sesión y la auditoría solo con ids (ADR-034 §5, §7; ADR-011). Con PostgreSQL y `crm_app`."""

import json
from types import MappingProxyType, SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from django.core.management import call_command
from django.db import DatabaseError, IntegrityError, ProgrammingError
from django.utils import timezone

from apps.access.apps import extend_owner_roles
from apps.access.catalog import BY_CODE, ROLE_TEMPLATES
from apps.ai_agents import records
from apps.ai_agents.models import AIRun
from apps.ai_agents.records import Action, Finding, Run, Suggestion
from apps.ai_agents.registry import Evidence
from core.ids import new_id
from core.tenancy.context import ActorType, TenantContext
from core.tenancy.scope import tenant_scope
from tests.test_access import as_migrator, rbac
from tests.test_conversations_tables import conversation, first, world
from tests.test_memberships import ctx, raw

__all__ = ["rbac", "world"]  # los fixtures, para pytest
pytestmark = pytest.mark.usefixtures("tenant_db")
DB = psycopg.Connection[Any]
TABLES = ("ai_runs", "ai_actions", "ai_suggestions")
DRAFT = "Hola, el producto cuesta [verificar]."
SEARCH = "croquetas para perro adulto"  # texto libre del modelo: solo su longitud se guarda
MONTH = "to_char((now() + make_interval(months => %s)) AT TIME ZONE 'UTC', 'YYYY\"m\"MM')"


def stock(variant: UUID) -> Evidence:
    facts = MappingProxyType({"available": 7})
    return Evidence(new_id(), "STOCK", "get_stock", timezone.now(), (variant,), facts)


def calls(variant: UUID) -> list[Action]:
    return [
        Action("toolu_01", "search_product", "INVALID_INPUT", {"query": SEARCH, "inventada": "x"}),
        Action("toolu_02", "get_stock", "OK", {"variant_id": str(variant).upper()}, stock(variant)),
    ]


def save(w: SimpleNamespace, convo: Any, **over: Any) -> tuple[Run, Suggestion]:
    run = Run("SUGGEST", convo.pk, "OK", 1200, {"PRICE": 1}, (uuid4(),), True, "CUSTOMER_ASKED")
    draft = Suggestion(DRAFT, (Finding("PRICE", 25, 36),), timezone.now())
    variant = uuid4()
    records.save(over.get("ctx", ctx(w.a, w.ana)), run, over.get("actions", calls(variant)), draft)
    return run, draft


def counts(migrator: DB) -> list[int]:
    found = [migrator.execute(f"SELECT count(*) FROM {t}").fetchone() for t in TABLES]  # noqa: S608
    return [row[0] for row in found if row]


def test_ai_use_is_born_with_the_owner_only_and_reaches_existing_owners(
    rbac: dict[str, Any], migrator: DB
) -> None:
    """No es sensible, no tiene alcance, ninguna plantilla lo recibe y el despliegue se lo da
    al Owner de las organizaciones que ya existen, a nadie más (ADR-018 §3, D-F8-08)."""
    assert not BY_CODE["ai.use"].is_sensitive and not BY_CODE["ai.use"].supports_scope
    born = {template.code for template in ROLE_TEMPLATES if "ai.use" in template.grants}
    assert born == {"owner"}
    grants = (
        "SELECT r.organization_id, r.code FROM role_permissions g JOIN roles r ON r.id = g.role_id"
        " WHERE g.permission_code = 'ai.use' ORDER BY 1, 2"
    )
    owners = sorted((rbac[key].org, "owner") for key in ("A", "B"))
    assert migrator.execute(grants).fetchall() == owners
    migrator.execute("DELETE FROM role_permissions WHERE permission_code = 'ai.use'")
    assert as_migrator(lambda: extend_owner_roles("default")) == 2
    assert migrator.execute(grants).fetchall() == owners


def test_save_writes_everything_once_with_ids_only_in_the_audit(
    world: SimpleNamespace, migrator: DB
) -> None:
    convo = first(world)
    run, draft = save(world, convo)
    row = migrator.execute(
        "SELECT organization_id, requested_by_user_id, conversation_id, purpose, outcome,"
        " tool_calls, findings, reservation_ids, retried, handoff_reason, duration_ms FROM ai_runs"
        " WHERE id = %s",
        [run.id],
    ).fetchone()
    assert row == (world.a, world.ana.pk, convo.pk, "SUGGEST", "OK", 2, {"PRICE": 1},
                   list(run.reservation_ids), True, "CUSTOMER_ASKED", 1200)  # fmt: skip
    actions = migrator.execute(
        "SELECT run_id, tool, code, arguments, kind, subjects, facts FROM ai_actions"
        " WHERE organization_id = %s ORDER BY tool_use_id",
        [world.a],
    ).fetchall()
    (variant,) = actions[1][5]
    assert actions == [  # el run, sin FK: la misma transacción
        (run.id, "search_product", "INVALID_INPUT", {"query": len(SEARCH)}, None, [], {}),
        (run.id, "get_stock", "OK", {"variant_id": str(variant)}, "STOCK", [variant],
         {"available": 7}),
    ]  # fmt: skip
    stored = migrator.execute(
        "SELECT run_id, conversation_id, requested_by_user_id, kind, text, findings,"
        " handoff_reason FROM ai_suggestions WHERE id = %s",
        [draft.id],
    ).fetchone()
    finding = [{"type": "PRICE", "start": 25, "end": 36}]
    assert stored == (run.id, convo.pk, world.ana.pk, "SUGGEST", DRAFT, finding, "CUSTOMER_ASKED")
    audit = migrator.execute(
        "SELECT actor_type, actor_id, action, entity_type, entity_id, changes, metadata"
        " FROM audit_logs WHERE organization_id = %s",
        [world.a],
    ).fetchall()
    ids = {"conversation_id": str(convo.pk), "run_id": str(run.id)}
    assert audit == [("USER", world.ana.pk, "ai.suggestion.created", "ai_suggestion", draft.id,
                      {}, ids)]  # fmt: skip
    assert migrator.execute("SELECT count(*) FROM outbox_events").fetchone() == (0,)
    dumped = json.dumps(migrator.execute("SELECT to_jsonb(r) FROM ai_runs r").fetchall())
    dumped += json.dumps(migrator.execute("SELECT to_jsonb(a) FROM ai_actions a").fetchall())
    assert SEARCH not in dumped and "inventada" not in dumped and "verificar" not in dumped


def test_a_summary_audits_its_own_event_and_a_run_without_draft_audits_nothing(
    world: SimpleNamespace, migrator: DB
) -> None:
    convo = first(world)
    alone = Run("SUMMARY", convo.pk, "AI_BUDGET_EXHAUSTED", 3)
    records.save(ctx(world.a, world.ana), alone, [], None)
    records.save(ctx(world.a, world.ana), Run("SUMMARY", convo.pk, "OK", 5), [], Suggestion("x"))
    actions = migrator.execute("SELECT action FROM audit_logs").fetchall()
    assert actions == [("ai.summary.created",)] and counts(migrator) == [2, 0, 1]


@pytest.mark.parametrize("broken", ["audit", "action", "draft"])
def test_save_is_all_or_nothing(
    world: SimpleNamespace, migrator: DB, monkeypatch: pytest.MonkeyPatch, broken: str
) -> None:
    def fails(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("sin auditoría")

    convo, variant = first(world), uuid4()
    if broken == "audit":
        monkeypatch.setattr(records, "record", fails)
    bad = Action("tool use con espacios", "get_stock", "OK", {}, stock(variant))
    actions = [*calls(variant), bad] if broken == "action" else calls(variant)
    text = "x" * 10001 if broken == "draft" else DRAFT
    with pytest.raises((RuntimeError, DatabaseError)):
        run = Run("SUGGEST", convo.pk, "OK", 1)
        records.save(ctx(world.a, world.ana), run, actions, Suggestion(text))
    assert counts(migrator) == [0, 0, 0]
    assert migrator.execute("SELECT count(*) FROM audit_logs").fetchone() == (0,)


def test_the_organization_and_the_requester_come_from_the_context(
    world: SimpleNamespace, migrator: DB
) -> None:
    """Una conversación de A con el contexto de B no se registra (FK compuesta); sin persona,
    tampoco."""
    convo = first(world)
    with pytest.raises(IntegrityError, match="ai_runs_conversation_fk"):
        save(world, convo, ctx=ctx(world.b, world.luis))
    for context in (ctx(world.a), TenantContext(world.a, "t", world.ana.pk, ActorType.AI_AGENT)):
        with pytest.raises(ValueError, match="persona"):
            save(world, convo, ctx=context)
    assert counts(migrator) == [0, 0, 0]


def test_a_member_of_b_neither_sees_nor_writes_rows_of_a(world: SimpleNamespace) -> None:
    run, draft = save(world, first(world))
    theirs = conversation(world.b, world.contact_b, world.account_b, "CONV-00002")
    with tenant_scope(ctx(world.b, world.luis)):
        assert [raw(f"SELECT count(*) FROM {t}") for t in TABLES] == [[(0,)]] * 3  # noqa: S608
        assert not AIRun.objects.exists()
        assert raw("UPDATE ai_suggestions SET text = NULL") == [(0,)]  # ni purga la de A
    statements = (
        ("INSERT INTO ai_runs (organization_id, purpose, requested_by_user_id, conversation_id,"
         " outcome, tool_calls, duration_ms) VALUES (%s, 'SUGGEST', %s, %s, 'OK', 0, 0)",
         [world.a, world.ana.pk, run.conversation_id]),
        ("INSERT INTO ai_actions (organization_id, run_id, tool_use_id, tool, code) VALUES"
         " (%s, %s, 't', 'get_stock', 'NOT_FOUND')", [world.a, run.id]),
        ("INSERT INTO ai_suggestions (organization_id, run_id, conversation_id,"
         " requested_by_user_id, kind) VALUES (%s, %s, %s, %s, 'SUGGEST')",
         [world.a, uuid4(), theirs.pk, world.luis.pk]),
    )  # fmt: skip
    for statement, params in statements:
        with (
            pytest.raises(ProgrammingError, match="row-level security"),
            tenant_scope(ctx(world.b, world.luis)),
        ):
            raw(statement, params)
    with tenant_scope(ctx(world.a, world.ana)):
        assert raw("SELECT text FROM ai_suggestions") == [(DRAFT,)] and draft.text == DRAFT


def test_forced_rls_and_one_policy_on_every_table_and_partition(migrator: DB) -> None:
    rows = migrator.execute(
        "SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity,"
        " (SELECT array_agg(p.polname || ':' || p.polcmd::text) FROM pg_policy p"
        "  WHERE p.polrelid = c.oid),"
        " has_table_privilege('crm_app', c.oid, 'SELECT, INSERT, UPDATE, DELETE, TRUNCATE')"
        " FROM pg_class c WHERE c.oid = ANY(%s::regclass[])"
        " OR c.oid IN (SELECT inhrelid FROM pg_inherits WHERE inhparent = ANY(%s::regclass[]))",
        [list(TABLES), ["ai_runs", "ai_actions"]],
    ).fetchall()
    assert len(rows) == 3 + 2 * 13
    for name, rls, forced, policies, privileged in rows:
        assert rls and forced and policies == ["tenant_isolation:*"], name
        assert privileged == (name in TABLES), name  # las particiones, sin privilegios


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE ai_runs SET outcome = 'OTHER'",
        "UPDATE ai_actions SET code = 'OTHER'",
        "DELETE FROM ai_runs",
        "DELETE FROM ai_actions",
        "DELETE FROM ai_suggestions",
        "TRUNCATE ai_runs",
        "TRUNCATE ai_actions",
        "TRUNCATE ai_suggestions",
        "UPDATE ai_suggestions SET kind = 'SUMMARY'",
        "UPDATE ai_suggestions SET findings = '[]', text = NULL",
        "UPDATE ai_runs_y{month} SET outcome = 'OTHER'",
        "SELECT count(*) FROM ai_actions_y{month}",
    ],
)
def test_crm_app_cannot_change_or_erase_the_records(
    world: SimpleNamespace, migrator: DB, statement: str
) -> None:
    save(world, first(world))
    month = migrator.execute(f"SELECT {MONTH}", [0]).fetchone()[0]  # type: ignore[index]
    with (
        pytest.raises(ProgrammingError, match="permission denied"),
        tenant_scope(ctx(world.a, world.ana)),
    ):
        raw(statement.format(month=month))
    assert counts(migrator) == [1, 2, 1]


def test_the_draft_text_only_goes_to_null_for_every_role(
    world: SimpleNamespace, migrator: DB
) -> None:
    _, draft = save(world, first(world))
    other = "UPDATE ai_suggestions SET text = 'otro texto' WHERE id = %s"
    with pytest.raises(IntegrityError) as refused, tenant_scope(ctx(world.a, world.ana)):
        raw(other, [draft.id])
    assert refused.value.__cause__.diag.constraint_name == "ai_suggestions_text_purge_only"  # type: ignore[union-attr]
    for statement in (other, "UPDATE ai_suggestions SET kind = 'SUMMARY' WHERE id = %s"):
        with pytest.raises(psycopg.errors.CheckViolation) as caught:
            migrator.execute(statement, [draft.id])  # el disparador también ata al dueño
        assert caught.value.diag.constraint_name == "ai_suggestions_text_purge_only"
    with tenant_scope(ctx(world.a, world.ana)):  # la purga de D-F8-05
        assert raw("UPDATE ai_suggestions SET text = NULL WHERE id = %s", [draft.id]) == [(1,)]
    assert migrator.execute("SELECT text, findings FROM ai_suggestions").fetchall() == [
        (None, [{"type": "PRICE", "start": 25, "end": 36}])
    ]


@pytest.mark.parametrize(
    ("table", "column", "value"),
    [
        ("ai_runs", "findings", {"PRICE": "el precio era 10"}),
        ("ai_runs", "findings", {"el precio era 10": 1}),
        ("ai_runs", "outcome", "texto libre"),
        ("ai_actions", "arguments", {"query": "texto libre"}),
        ("ai_actions", "arguments", {"query": {"length": "texto"}}),
        ("ai_actions", "facts", {"available": "siete"}),
        ("ai_actions", "tool", "busca croquetas"),
        ("ai_suggestions", "findings", [{"type": "PRICE", "start": 1, "end": 2, "was": "10"}]),
        ("ai_suggestions", "findings", [{"type": "precio 10", "start": 1, "end": 2}]),
        ("ai_suggestions", "findings", ["el precio era 10"]),
    ],
)
def test_no_free_text_fits_in_the_findings_arguments_or_codes(
    world: SimpleNamespace, migrator: DB, table: str, column: str, value: Any
) -> None:
    """Ni con el dueño: el CHECK de esa columna lo rechaza (una copia válida de una fila)."""
    save(world, first(world))
    insert = (
        "INSERT INTO {t} SELECT (jsonb_populate_record(NULL::{t}, to_jsonb(x)"
        " || jsonb_build_object('id', uuidv7(), 'run_id', uuidv7(), %s::text, %s::jsonb))).*"
        " FROM {t} x ORDER BY to_jsonb(x) ->> 'tool_use_id' DESC LIMIT 1"  # con evidencia
    )
    migrator.execute(insert.format(t=table), ["retried", "false"])  # la copia, sin más, entra
    with pytest.raises(psycopg.errors.CheckViolation) as caught:
        migrator.execute(insert.format(t=table), [column, json.dumps(value)])
    assert caught.value.diag.constraint_name in (f"{table}_{column}_ck", f"{table}_{column}_check")


def test_partitions_cover_this_month_and_twelve_more_and_post_migrate_keeps_them(
    migrator: DB,
) -> None:
    for table in ("ai_runs", "ai_actions"):
        expected = [migrator.execute(f"SELECT '{table}_y' || {MONTH}", [n]).fetchone()[0]  # type: ignore[index]
                    for n in range(13)]  # fmt: skip
        parts = migrator.execute(
            "SELECT inhrelid::regclass::text FROM pg_inherits WHERE inhparent = %s::regclass"
            " ORDER BY 1",
            [table],
        ).fetchall()
        assert [p for (p,) in parts] == expected
        function = f"public.{table}_ensure_partitions(integer)"
        assert migrator.execute(
            "SELECT p.prosecdef, pg_get_userbyid(p.proowner),"
            " has_function_privilege('crm_app', p.oid, 'EXECUTE') FROM pg_proc p"
            " WHERE p.oid = %s::regprocedure",
            [function],
        ).fetchone() == (False, "crm_migrator", False)
        with pytest.raises(ProgrammingError, match="permission denied"):
            raw(f"SELECT {function.removesuffix('(integer)')}(1)")
        migrator.execute(f"DROP TABLE {expected[-1]}")  # un año sin desplegar
    as_migrator(lambda: call_command("migrate", verbosity=0))
    for table in ("ai_runs", "ai_actions"):
        last = migrator.execute(f"SELECT '{table}_y' || {MONTH}", [12]).fetchone()[0]  # type: ignore[index]
        assert migrator.execute("SELECT to_regclass(%s) IS NOT NULL", [last]).fetchone() == (True,)


def test_the_models_only_read(world: SimpleNamespace) -> None:
    save(world, first(world))
    with tenant_scope(ctx(world.a, world.ana)):
        run = AIRun.objects.get()
        assert run.tool_calls == 2 and run.findings == {"PRICE": 1}
        for write in (run.save, run.delete, AIRun.objects.all().delete):
            with pytest.raises(TypeError, match=r"records\.save"):
                write()

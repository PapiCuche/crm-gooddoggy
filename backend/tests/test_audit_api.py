"""F2-73: `GET /api/v1/o/{slug}/audit/`, la auditoría de una organización para leer.
Middleware, sesión, motor de autorización y PostgreSQL con el rol `crm_app`."""

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from django.db import ProgrammingError, connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

from apps.audit.models import ACTOR_TYPES, AuditLog
from apps.audit.selectors import entries
from apps.audit.services import Entity, Result, record
from core.tenancy.context import ActorType, TenantContext, TenantContextMissing
from core.tenancy.scope import tenant_scope
from tests import test_authorization, test_self_context
from tests.factories import make_user
from tests.test_authorization import give
from tests.test_memberships import ctx, join, raw
from tests.test_self_context import NOT_FOUND, reply, signed

world, real_stack = test_authorization.world, test_self_context.real_stack
pytestmark = pytest.mark.usefixtures("tenant_db")
VIEW: dict[str, str | None] = {"audit.view": None}
SECRET = "sk-" + "ant-api03-" + "Qw7" * 30  # construido en ejecución (gitleaks sin allowlist)
DENIED = (403, b'{"code":"PERMISSION_DENIED"}')


def audit(client: Client, org: str = "org-a", **query: Any) -> Any:
    return client.get(f"/api/v1/o/{org}/audit/", query)


def write(org: UUID, action: str = "widget.updated", **entity: Any) -> UUID:
    with tenant_scope(ctx(org)):
        return record(ctx(org), action, Entity("widget", **entity))


def test_it_lists_the_rows_of_the_organization_newest_first_and_none_of_another(
    world: Any, migrator: psycopg.Connection[Any]
) -> None:
    give(world.a, world.membership, VIEW)
    actor, thing = uuid4(), uuid4()
    old = write(world.a, "widget.created")
    tenant = TenantContext(world.a, "test", actor, ActorType.USER, actor, "corr-1")
    with tenant_scope(tenant):
        new = record(
            tenant,
            "widget.updated",
            Entity("widget", thing, "W-1"),
            {"name": ["antes", "después"], "api_key": [None, SECRET]},
            {"note": f"con {SECRET}", "veces": 2},
            result=Result.DENIED,
            actor_label="Ana",
        )
    foreign = write(world.b, "solo.en_b", label="De B")
    body = audit(signed(world.ana)).json()
    stamps = dict(migrator.execute("SELECT id, occurred_at FROM audit_logs").fetchall())
    assert [datetime.fromisoformat(row.pop("occurred_at")) for row in body["results"]] == [
        stamps[new],
        stamps[old],
    ]
    assert body == {
        "results": [
            {
                "id": str(new),
                "actor_type": "USER",
                "actor_id": str(actor),
                "actor_label": "Ana",
                "action": "widget.updated",
                "entity_type": "widget",
                "entity_id": str(thing),
                "entity_label": "W-1",
                "changes": {"name": ["antes", "después"], "api_key": [None, "[REDACTED]"]},
                "metadata": {"note": "con [REDACTED]", "veces": 2},  # como se guardó
                "result": "DENIED",
                "correlation_id": "corr-1",
            },
            {  # lo opcional, vacío: `null` y objetos sin claves
                "id": str(old),
                "actor_type": "SYSTEM",
                "actor_id": None,
                "actor_label": None,
                "action": "widget.created",
                "entity_type": "widget",
                "entity_id": None,
                "entity_label": None,
                "changes": {},
                "metadata": {},
                "result": "SUCCESS",
                "correlation_id": None,
            },
        ],
        "next": None,
    }
    for alien in ("solo.en_b", "De B", str(foreign), str(world.b), SECRET):
        assert alien not in str(body)


def test_only_a_member_with_the_permission_reads_it_and_reading_writes_nothing(
    world: Any, migrator: psycopg.Connection[Any]
) -> None:
    write(world.a)
    assert audit(Client()).status_code == 401
    client = signed(world.ana)
    assert reply(audit(client)) == DENIED  # sin el permiso
    give(world.a, world.membership, {"organization.view": None, "roles.view": None})
    assert reply(audit(client)) == DENIED  # otros permisos de lectura no la abren
    give(world.a, world.membership, VIEW)
    assert audit(client).status_code == 200
    for org in ("org-b", "no-existe"):  # sin membresía, la organización no existe
        assert reply(audit(client, org)) == NOT_FOUND
    outsider = make_user()
    join(world.b, outsider)
    assert reply(audit(signed(outsider))) == NOT_FOUND
    client.cookies["csrftoken"] = token = "t" * 32  # con token responde la vista, no el CSRF
    for refused in (client.post, client.put, client.patch, client.delete):  # solo lectura
        assert reply(refused("/api/v1/o/org-a/audit/", headers={"X-CSRFToken": token})) == DENIED
    rows = migrator.execute("SELECT count(*) FROM audit_logs").fetchone()
    assert rows == (1,)  # ni leer ni un intento de escribir dejan una fila


def test_it_paginates_from_the_newest_without_a_query_per_row(world: Any) -> None:
    give(world.a, world.membership, VIEW)
    client = signed(world.ana)

    def queries() -> int:
        with CaptureQueriesContext(connection) as captured:
            assert audit(client).status_code == 200
        return len(captured)

    made = [str(write(world.a, f"widget.step{n}")) for n in range(2)]
    few = queries()
    made += [str(write(world.a, f"widget.step{n}")) for n in range(2, 5)]
    write(world.b, "widget.step9")
    assert queries() == few
    first = audit(client, limit=2).json()
    rest = audit(client, limit=200, cursor=first["next"]).json()
    assert [row["id"] for row in first["results"] + rest["results"]] == made[::-1]
    assert first["next"] and rest["next"] is None
    for bad in ({"limit": 201}, {"cursor": "no-es-un-cursor"}, {"cursor": first["next"][::-1]}):
        assert (audit(client, **bad).status_code, audit(client, **bad).json()["code"]) == (
            400,
            "VALIDATION_ERROR",
        )
    with CaptureQueriesContext(connection) as captured:
        audit(client, limit=2)
    listing = [query["sql"] for query in captured if 'FROM "audit_logs"' in query["sql"]]
    assert len(listing) == 1 and 'ORDER BY "audit_logs"."id" DESC' in listing[0]


def test_rls_hides_the_rows_of_another_organization_and_the_model_does_not_write(
    world: Any,
) -> None:
    mine, theirs = write(world.a), write(world.b)
    assert raw("SELECT count(*) FROM audit_logs") == [(0,)]  # sin tenant, nada
    with pytest.raises(TenantContextMissing):
        entries().count()
    with tenant_scope(ctx(world.a)):
        assert raw("SELECT id FROM audit_logs") == [(mine,)]  # RLS, no solo el manager
        assert [row.pk for row in entries()] == [mine]
        assert not AuditLog._base_manager.filter(pk=theirs).exists()
        row = entries().get()
        fresh = AuditLog(action="x.y", entity_type="x", actor_type="SYSTEM", result="SUCCESS")
        for write_it in (
            row.save,
            row.delete,
            fresh.save,
            lambda: AuditLog.objects.create(action="x.y"),
            lambda: AuditLog.objects.bulk_create([fresh]),
            lambda: AuditLog.objects.bulk_update([row], ["action"]),
            lambda: entries().update(action="x.y"),
            lambda: entries().delete(),
            lambda: entries().filter(pk=mine).delete(),
        ):
            with pytest.raises(TypeError, match="audit_logs solo se escribe con"):
                write_it()
        assert raw("SELECT action FROM audit_logs") == [("widget.updated",)]
    for statement in ("UPDATE audit_logs SET action = 'x.y'", "DELETE FROM audit_logs"):
        with pytest.raises(ProgrammingError, match="permission denied"):  # y la base, también
            with tenant_scope(ctx(world.a)):
                raw(statement)


def test_the_listing_has_its_index_and_knows_every_actor_type_of_the_table(
    world: Any, migrator: psycopg.Connection[Any]
) -> None:
    index = migrator.execute(
        "SELECT indexdef FROM pg_indexes WHERE indexname = 'audit_logs_org_id_idx'"
    ).fetchone()
    assert index is not None and index[0].endswith("USING btree (organization_id, id)")
    parts = "SELECT count(*) FROM pg_inherits WHERE inhparent = %s::regclass"
    tables = migrator.execute(parts, ["audit_logs"]).fetchone()
    indexes = migrator.execute(parts, ["audit_logs_org_id_idx"]).fetchone()
    assert tables == indexes and tables is not None and tables[0] >= 12  # uno por partición
    check = migrator.execute(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint"
        " WHERE conrelid = 'audit_logs'::regclass AND contype = 'c'"
        " AND pg_get_constraintdef(oid) LIKE '%actor_type%'"
    ).fetchone()
    assert check is not None and check[0].count("'::") == len(ACTOR_TYPES)
    give(world.a, world.membership, VIEW)
    for kind in ACTOR_TYPES:  # la API sabe nombrar todo lo que la tabla admite
        assert f"'{kind}'::" in check[0]
        migrator.execute(
            "INSERT INTO audit_logs (organization_id, actor_type, action, entity_type)"
            " VALUES (%s, %s, 'widget.seen', 'widget')",
            [world.a, kind],
        )
    seen = [row["actor_type"] for row in audit(signed(world.ana)).json()["results"]]
    assert seen == list(ACTOR_TYPES)[::-1]


def seen(client: Client, **query: Any) -> list[str]:
    return [row["id"] for row in audit(client, **query).json()["results"]]


def test_each_filter_keeps_only_its_rows_and_they_all_apply_together(world: Any) -> None:
    give(world.a, world.membership, VIEW)
    person, bot, thing, other = uuid4(), uuid4(), uuid4(), uuid4()

    def by(org: UUID, kind: ActorType, actor: UUID | None, action: str, *entity: Any) -> str:
        tenant = TenantContext(org, "test", None, kind, actor)
        with tenant_scope(tenant):
            return str(record(tenant, action, Entity(*entity)))

    made = by(world.a, ActorType.USER, person, "role.created", "role", thing)
    renamed = by(world.a, ActorType.USER, person, "role.updated", "role", thing)
    by_bot = by(world.a, ActorType.AI_AGENT, bot, "role.updated", "role", other)
    moved = by(world.a, ActorType.SYSTEM, None, "branch.updated", "branch", thing)
    by(world.b, ActorType.USER, person, "role.updated", "role", thing)  # lo mismo, en B
    only_b = uuid4()
    write(world.b, id=only_b, label="De B")
    client = signed(world.ana)
    everything = [moved, by_bot, renamed, made]
    assert seen(client) == everything
    assert seen(client, actor_id=person) == [renamed, made]
    assert seen(client, actor_id=str(person).upper()) == [renamed, made]  # el mismo UUID
    assert seen(client, actor_type="AI_AGENT") == [by_bot]
    assert seen(client, actor_type="SYSTEM") == [moved]  # lo que no tiene `actor_id`
    assert seen(client, action="role.updated") == [by_bot, renamed]
    assert seen(client, entity_type="role") == [by_bot, renamed, made]
    assert seen(client, entity_id=thing) == [moved, renamed, made]
    assert seen(client, entity_type="role", entity_id=thing) == [renamed, made]
    assert seen(client, actor_id=person, action="role.updated") == [renamed]
    assert seen(client, actor_type="USER", actor_id=bot) == []  # se cumplen todos, no alguno
    assert seen(client, action="role.deleted") == []  # nada: una lista vacía, no un error
    assert seen(client, actor_id=uuid4()) == []
    assert seen(client, entity_id=only_b) == []  # el identificador de otra organización
    assert seen(client, otro="x", actions="role.created") == everything  # no son filtros
    first = audit(client, limit=1, entity_type="role").json()
    assert [row["id"] for row in first["results"]] == [by_bot]
    assert seen(client, limit=1, entity_type="role", cursor=first["next"]) == [renamed]
    with CaptureQueriesContext(connection) as captured:
        audit(client, action="role.updated", actor_id=person)
    listing = [query["sql"] for query in captured if 'FROM "audit_logs"' in query["sql"]]
    assert len(listing) == 1  # filtra la base, no la vista
    assert '"audit_logs"."action" = ' in listing[0] and '"audit_logs"."actor_id" = ' in listing[0]


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("actor_id", "no-es-un-uuid"),
        ("actor_id", ""),
        ("entity_id", "123"),
        ("actor_type", "user"),  # los valores exactos de la tabla
        ("actor_type", "ROBOT"),
        ("actor_type", ""),
        ("action", "role"),  # sin módulo y acción no es una acción
        ("action", "Role.Created"),
        ("action", "role.created "),
        ("action", " role.created"),
        ("action", "role.created\n"),
        ("action", "role.%"),
        ("action", "a." + "b" * 99),  # 101 caracteres: no cabe en la columna
        ("action", ""),
        ("entity_type", "Role"),
        ("entity_type", "role.x"),
        ("entity_type", "r" * 51),
        ("entity_type", ""),
        ("action", ["role.created", "role.updated"]),  # repetido: no se elige uno
        ("entity_id", ["0190b0c0-0000-7000-8000-000000000001"] * 2),
    ],
)
def test_a_value_the_field_cannot_hold_is_a_400_on_that_filter(
    world: Any, name: str, value: Any
) -> None:
    give(world.a, world.membership, VIEW)
    write(world.a)
    response = audit(signed(world.ana), **{name: value})
    body = response.json()
    assert (response.status_code, body["code"]) == (400, "VALIDATION_ERROR")
    assert list(body["fields"]) == [name]  # ni una lista vacía ni la lista sin filtrar


def test_the_selective_filters_have_their_index_on_every_partition(
    migrator: psycopg.Connection[Any],
) -> None:
    parts = "SELECT count(*) FROM pg_inherits WHERE inhparent = %s::regclass"
    tables = migrator.execute(parts, ["audit_logs"]).fetchone()
    for name, columns in (
        ("audit_logs_org_entity_idx", "(organization_id, entity_type, entity_id, id)"),
        ("audit_logs_org_actor_idx", "(organization_id, actor_id, id)"),
    ):
        index = migrator.execute(
            "SELECT indexdef FROM pg_indexes WHERE indexname = %s", [name]
        ).fetchone()
        assert index is not None and index[0].endswith(f"USING btree {columns}")
        assert migrator.execute(parts, [name]).fetchone() == tables

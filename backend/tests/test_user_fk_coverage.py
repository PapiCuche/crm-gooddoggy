"""F2-128 (#819): toda columna `*_user_id` de una tabla de tenant lleva la FK compuesta a las
membresías de ADR-001 §3, salvo las excepciones documentadas de `EXCEPTIONS`. Además, las dos que
faltaban (`files` y `credentials`), sus migraciones (`files/0004` y `conversations/0034`, patrón de
`catalog/0005`) y que, tras un fallo por `lock_timeout`, la sesión del migrador vuelve a no tener
ninguno (también `catalog/0005`, F3-37). PostgreSQL real; el runtime es `crm_app`."""

from importlib import import_module
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from django.apps import apps as django_apps
from django.core.management import call_command
from django.db import IntegrityError, OperationalError, connection

from apps.conversations import credentials
from apps.files.models import File
from config.settings import base
from core import storage
from core.tenancy.context import ActorType, TenantContext
from core.tenancy.scope import tenant_scope
from tests import test_authorization
from tests.conftest import migrator_conninfo
from tests.factories import make_user
from tests.test_memberships import ctx, join
from tests.test_pipelines_tables import as_migrator

world = test_authorization.world
pytestmark = pytest.mark.usefixtures("tenant_db")
DB = psycopg.Connection[Any]
TOKEN = "meta-test-access-token-not-real-0819"  # noqa: S105 — ficticio
FILES = import_module("apps.files.migrations.0004_created_by_member_fk").FKS
TOKENS = import_module("apps.conversations.migrations.0034_credentials_created_by_member_fk").FKS
CATALOG = import_module("apps.catalog.migrations.0005_deleted_by_member_fk")
DEFINITION = (
    "FOREIGN KEY (organization_id, created_by_user_id)"
    " REFERENCES organization_memberships(organization_id, user_id)"
)

# Columnas `*_user_id` de tenant sin la FK compuesta, cada una con su motivo. Una nueva sin motivo
# hace fallar el test; una que ya tiene la FK, también (sobra aquí).
EXCEPTIONS = {
    ("audit_logs", "impersonated_by_user_id"): (
        "quien suplanta es personal de plataforma, sin membresía (ADR-001 §3); sin FK a `users`:"
        " la auditoría sobrevive a la cuenta (apps/audit/models.py)"
    ),
}
# Las tablas de las apps que solo instalan los tests (`tests.tenancy_app`, de las pruebas de RLS)
# no son del producto: se dejan fuera por regla, no por excepción.
TEST_ONLY = sorted(
    model._meta.db_table
    for config in django_apps.get_app_configs()
    if config.name not in base.INSTALLED_APPS
    for model in config.get_models()
)
COLUMNS = r"""
SELECT c.relname, a.attname, EXISTS (
  SELECT 1 FROM pg_constraint f WHERE f.conrelid = c.oid AND f.contype = 'f' AND f.convalidated
  AND pg_get_constraintdef(f.oid) = format('FOREIGN KEY (organization_id, %%s) REFERENCES'
    ' organization_memberships(organization_id, user_id)', a.attname))
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace AND n.nspname = 'public'
JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
WHERE c.relkind IN ('r', 'p') AND NOT c.relispartition AND c.relforcerowsecurity
  AND a.attname LIKE '%%\_user\_id' AND c.relname <> ALL(%s)
  AND EXISTS (SELECT 1 FROM pg_attribute o WHERE o.attrelid = c.oid
              AND o.attname = 'organization_id' AND NOT o.attisdropped)
"""


def test_every_tenant_user_column_has_its_member_fk_or_a_documented_exception(
    migrator: DB,
) -> None:
    assert TEST_ONLY == ["tenancy_app_widget", "tenancy_app_widgetpart"]
    found = {(table, column): fk for table, column, fk in migrator.execute(COLUMNS, [TEST_ONLY])}
    assert {where for where, fk in found.items() if not fk} == set(EXCEPTIONS)
    assert all(EXCEPTIONS.values())
    assert found[("files", "created_by_user_id")] and found[("credentials", "created_by_user_id")]
    assert sum(found.values()) >= 60  # la consulta ve las columnas del producto, no una tabla vacía


def as_user(org: UUID, user_id: UUID) -> TenantContext:
    return TenantContext(org, "test", user_id, ActorType.USER, user_id)


def upload(org: UUID, user_id: UUID | None) -> UUID:
    """Un archivo de `org` subido por `user_id`, como `crm_app` (`NULL`: un adjunto de webhook)."""
    pk = uuid4()
    with tenant_scope(ctx(org)):
        File.objects.create(
            id=pk, storage_key=storage.tenant_key(org, "attachment", pk), original_name="a.txt",
            mime_type="text/plain", size_bytes=1, sha256="0" * 64, purpose="attachment",
            created_by_user_id=user_id,
        )  # fmt: skip
    return pk


def save_token(org: UUID, user_id: UUID | None) -> UUID:
    who = ctx(org) if user_id is None else as_user(org, user_id)
    with tenant_scope(who):
        return credentials.store(who, uuid4(), TOKEN).pk


def constraint(raised: pytest.ExceptionInfo[IntegrityError]) -> str:
    return str(raised.value.__cause__.diag.constraint_name)  # type: ignore[union-attr]


def rows(migrator: DB) -> list[Any]:
    query = "SELECT array_agg(created_by_user_id ORDER BY id) FROM "
    return [migrator.execute(query + table).fetchone() for table in ("files", "credentials")]


def test_a_file_or_a_token_saved_by_someone_who_is_not_a_member_is_refused(
    world: Any, migrator: DB
) -> None:
    luis = make_user(email="luis@example.com")
    join(world.b, luis)  # miembro de B, no de A
    for who in (luis.pk, uuid4()):
        with pytest.raises(IntegrityError) as raised:
            upload(world.a, who)
        assert constraint(raised) == "files_created_by_fk"
        with pytest.raises(IntegrityError) as raised:
            save_token(world.a, who)
        assert constraint(raised) == "credentials_created_by_fk"
    assert rows(migrator) == [(None,), (None,)]  # nada se escribió
    for member in (world.ana.pk, None):  # quien es miembro, y el sistema sin persona
        upload(world.a, member), save_token(world.a, member)
    for table in ("files", "credentials"):  # también para el migrador
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            migrator.execute(f"UPDATE {table} SET created_by_user_id = %s", [luis.pk])  # noqa: S608
    saved = sorted([world.ana.pk, None], key=str)
    assert [sorted(found, key=str) for (found,) in rows(migrator)] == [saved, saved]


def fks(migrator: DB) -> dict[str, tuple[str, bool]]:
    found = migrator.execute(
        "SELECT conname, pg_get_constraintdef(oid), convalidated FROM pg_constraint"
        " WHERE conname IN ('files_created_by_fk', 'credentials_created_by_fk')"
    ).fetchall()
    return {name: (definition, valid) for name, definition, valid in found}


def migrate(*target: str) -> None:
    with as_migrator():
        call_command("migrate", *target, verbosity=0)


def test_the_migrations_refuse_orphans_repeat_and_go_back(world: Any, migrator: DB) -> None:
    luis = make_user(email="luis@example.com")
    join(world.b, luis)  # miembro de B, no de A
    upload(world.a, world.ana.pk), save_token(world.a, world.ana.pk)
    upload(world.a, None), save_token(world.a, None)  # sin persona: no es huérfana
    ours = {
        "files_created_by_fk": (DEFINITION, True), "credentials_created_by_fk": (DEFINITION, True)
    }  # fmt: skip
    orphan = "UPDATE {} SET created_by_user_id = %s"
    try:
        migrate("files", "0003")
        migrate("conversations", "0033")
        assert fks(migrator) == {}  # hacia atrás quita las dos
        by_ana = " WHERE created_by_user_id = %s"
        migrator.execute(orphan.format("files") + by_ana, [luis.pk, world.ana.pk])
        migrator.execute(orphan.format("credentials") + by_ana, [uuid4(), world.ana.pk])
        before = rows(migrator)
        message = r"files 0004: 1 rows .* \(files\.created_by_user_id: 1\)"
        with pytest.raises(RuntimeError, match=message):
            migrate("files")
        message = r"conversations 0034: 1 rows .* \(credentials\.created_by_user_id: 1\)"
        with pytest.raises(RuntimeError, match=message):
            migrate("conversations")
        assert (fks(migrator), rows(migrator)) == ({}, before)  # ni FKs ni datos tocados
        for fks_of in (FILES, TOKENS):  # `crm_app`: bajo RLS no vería las huérfanas
            with connection.schema_editor() as editor, pytest.raises(RuntimeError, match="BYPASS"):
                fks_of.add(None, editor)
        back = " WHERE created_by_user_id IS NOT NULL"
        migrator.execute(orphan.format("files") + back, [world.ana.pk])
        migrator.execute(orphan.format("credentials") + back, [world.ana.pk])
        migrator.execute(  # un intento anterior que falló a medias dejó una sin validar
            "ALTER TABLE files ADD CONSTRAINT files_created_by_fk FOREIGN KEY"
            " (organization_id, created_by_user_id) REFERENCES organization_memberships"
            " (organization_id, user_id) NOT VALID"
        )
        assert fks(migrator) == {"files_created_by_fk": (DEFINITION + " NOT VALID", False)}
        migrate()
        assert fks(migrator) == ours
        with as_migrator(), connection.schema_editor(atomic=False) as editor:
            FILES.add(None, editor), TOKENS.add(None, editor)  # repetirlas no cambia nada
        assert fks(migrator) == ours
    finally:
        for table in ("files", "credentials"):  # sin huérfanas, para dejar la base migrada
            clear = orphan.format(table) + " WHERE created_by_user_id <> %s"
            migrator.execute(clear, [None, world.ana.pk])
        migrate()


@pytest.mark.parametrize(
    ("add", "table"),
    [(CATALOG.add, "brands"), (FILES.add, "files"), (TOKENS.add, "credentials")],
    ids=["catalog-0005", "files-0004", "conversations-0034"],
)
def test_a_lock_timeout_leaves_the_session_without_one(add: Any, table: str) -> None:
    with psycopg.connect(migrator_conninfo()) as holder:  # una transacción larga en la tabla
        holder.execute(f"LOCK TABLE {table} IN ACCESS EXCLUSIVE MODE")
        with as_migrator(), connection.schema_editor(atomic=False) as editor:
            with pytest.raises(OperationalError, match="lock timeout"):
                add(None, editor)
            with connection.cursor() as cursor:  # la misma sesión que falló
                cursor.execute("SHOW lock_timeout")
                assert cursor.fetchone() == ("0",)

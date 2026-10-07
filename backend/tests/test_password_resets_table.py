"""F2-86: la tabla `password_resets` (ADR-021). Solo la base y el modelo: sin rutas todavía.
PostgreSQL con el rol `crm_app`; las restricciones se prueban con el migrador."""

from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from django.db import IntegrityError, connection
from django.db.models import ProtectedError

from apps.accounts.models import PasswordReset
from tests.factories import make_user

pytestmark = pytest.mark.usefixtures("tenant_db")
SOON = datetime.now(UTC) + timedelta(minutes=60)
HASH = "ab" * 32
INSERT = (
    "INSERT INTO password_resets (user_id, token_hash, created_at, expires_at, used_at)"
    " VALUES (%s, %s, now(), now() + %s::interval, now() + %s::interval)"
)


def test_it_keeps_a_sent_link_as_its_hash_without_any_organization() -> None:
    ana = make_user(email="ana@example.com")
    soon = datetime.now(UTC) + timedelta(minutes=60)  # ahora, no al cargar el módulo
    sent = PasswordReset.objects.create(user=ana, token_hash=HASH, expires_at=soon)  # sin tenant
    sent.refresh_from_db()
    assert (sent.user_id, sent.token_hash, sent.used_at) == (ana.pk, HASH, None)
    assert sent.expires_at - sent.created_at > timedelta(minutes=59)
    assert str(sent) == str(sent.pk)  # ni la cuenta ni el hash
    assert sent.pk.version == 7
    with connection.cursor() as cursor:  # platform-owned (ADR-001 §2): ni columna ni política
        cursor.execute(
            "SELECT count(*) FROM information_schema.columns"
            " WHERE table_name = 'password_resets' AND column_name = 'organization_id'"
        )
        assert cursor.fetchone() == (0,)
        cursor.execute("SELECT relrowsecurity FROM pg_class WHERE relname = 'password_resets'")
        assert cursor.fetchone() == (False,)
        cursor.execute("SELECT current_user")
        assert cursor.fetchone() == ("crm_app",)  # el runtime la lee y la escribe
    PasswordReset.objects.filter(pk=sent.pk).update(used_at=sent.created_at)  # usarlo: una fecha
    assert PasswordReset.objects.get().used_at == sent.created_at
    with pytest.raises(ProtectedError):  # la cuenta no se borra dejando enlaces huérfanos
        ana.delete()


def test_the_hash_is_unique_and_two_accounts_keep_their_own_links() -> None:
    ana, luis = make_user(email="ana@example.com"), make_user(email="luis@example.com")
    PasswordReset.objects.create(user=ana, token_hash=HASH, expires_at=SOON)
    PasswordReset.objects.create(user=ana, token_hash="cd" * 32, expires_at=SOON)  # varios: sí
    PasswordReset.objects.create(user=luis, token_hash="ef" * 32, expires_at=SOON)
    with pytest.raises(IntegrityError, match="password_resets_token_hash_key"):
        PasswordReset.objects.create(user=luis, token_hash=HASH, expires_at=SOON)


def test_the_index_and_the_runtime_privileges_are_the_documented_ones() -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT indexdef FROM pg_indexes WHERE indexname = 'password_resets_user_idx'"
        )
        (index,) = cursor.fetchone()  # contar los de una cuenta y encontrar el último
        assert index.endswith("USING btree (user_id, created_at DESC)")
        cursor.execute(
            "SELECT privilege_type FROM information_schema.role_table_grants"
            " WHERE table_name = 'password_resets' AND grantee = current_user"
        )  # `DELETE` se conserva para la purga; nada más que DML
        assert {row[0] for row in cursor.fetchall()} == {"SELECT", "INSERT", "UPDATE", "DELETE"}


@pytest.mark.parametrize(
    "token_hash",
    ["", "ab" * 31, "ab" * 31 + "a", "AB" * 32, "g" * 64, " " + "ab" * 31 + "a", "ab" * 31 + "a\n"],
)
def test_the_database_only_takes_a_sha256_in_lowercase_hex(
    migrator: psycopg.Connection[Any], token_hash: str
) -> None:
    ana = make_user(email="ana@example.com")
    with pytest.raises(psycopg.errors.CheckViolation, match="password_resets_token_hash_ck"):
        migrator.execute(INSERT, [ana.pk, token_hash, "1 hour", "1 minute"])


def test_the_database_keeps_the_dates_of_a_link_in_order(
    migrator: psycopg.Connection[Any],
) -> None:
    ana = make_user(email="ana@example.com")
    migrator.execute(INSERT, [ana.pk, HASH, "1 second", "0"])  # usado en el mismo instante: vale
    for expires in ("0", "-1 second"):  # un enlace caduca después de enviarse
        with pytest.raises(psycopg.errors.CheckViolation, match="password_resets_expires_ck"):
            migrator.execute(INSERT, [ana.pk, "cd" * 32, expires, "1 minute"])
    with pytest.raises(psycopg.errors.CheckViolation, match="password_resets_used_ck"):
        migrator.execute(INSERT, [ana.pk, "cd" * 32, "1 hour", "-1 second"])  # ni se usa antes
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        migrator.execute(INSERT, ["01a10000-0000-7000-8000-000000000000", "cd" * 32, "1 hour", "0"])
    with pytest.raises(psycopg.errors.StringDataRightTruncation):
        migrator.execute(INSERT, [ana.pk, "ab" * 33, "1 hour", "0"])
    for column in ("user_id", "token_hash", "expires_at"):
        with pytest.raises(psycopg.errors.NotNullViolation):
            migrator.execute(f"UPDATE password_resets SET {column} = NULL")  # noqa: S608
    assert migrator.execute("SELECT count(*) FROM password_resets").fetchone() == (1,)

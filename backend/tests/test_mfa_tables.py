"""F2-95: las tablas `user_mfa_devices` y `user_mfa_backup_codes` (ADR-023). Solo la base y los
modelos: sin rutas todavía. PostgreSQL con el rol `crm_app`; las restricciones, con el migrador."""

import base64
from typing import Any

import psycopg
import pytest
from django.db import IntegrityError, connection
from django.db.models import ProtectedError
from django.utils import timezone

from apps.accounts import models
from apps.accounts.models import MfaBackupCode, MfaDevice
from core import sealed
from tests.factories import make_user

pytestmark = pytest.mark.usefixtures("tenant_db")
TOTP = MfaDevice.Type.TOTP
KEY = b"k" * 20  # un secreto TOTP tiene 20 bytes (ADR-023 §2)
HASH = "ab" * 32
DEVICE = (
    "INSERT INTO user_mfa_devices (user_id, type, secret, created_at, confirmed_at, last_step)"
    " VALUES (%s, %s, %s, now(), now() + %s::interval, %s)"
)
CODE = (
    "INSERT INTO user_mfa_backup_codes (user_id, code_hash, created_at, used_at)"
    " VALUES (%s, %s, now(), now() + %s::interval)"
)
TABLES = ("user_mfa_devices", "user_mfa_backup_codes", "user_mfa_code_attempts")


def box(user: Any, secret: bytes = KEY) -> bytes:
    return sealed.seal("mfa.totp", f"user:{user.pk}", secret)


def test_a_device_keeps_its_secret_sealed_and_bound_to_its_account() -> None:
    ana, luis = make_user(email="ana@example.com"), make_user(email="luis@example.com")
    device = MfaDevice.objects.create(user=ana, type=TOTP, secret=box(ana))  # sin tenant
    device.refresh_from_db()
    assert (device.user_id, device.type, device.confirmed_at, device.last_step) == (
        ana.pk,
        "TOTP",
        None,
        None,
    )  # un alta a medias: sin confirmar y sin ningún paso aceptado
    assert device.failed_codes == 0  # ni ningún código fallido
    stored = bytes(device.secret)
    assert KEY not in stored and len(stored) == 3 + 12 + 20 + 16  # cifrado, nunca en claro
    assert sealed.unseal("mfa.totp", f"user:{ana.pk}", device.secret) == KEY
    with pytest.raises(sealed.SealError):  # copiado a la fila de otra cuenta, no se abre
        sealed.unseal("mfa.totp", f"user:{luis.pk}", device.secret)
    assert str(device) == str(device.pk) and device.pk.version == 7  # ni la cuenta ni el secreto
    assert repr(device) == f"<MfaDevice: {device.pk}>"  # tampoco al registrar la instancia
    MfaDevice.objects.filter(pk=device.pk).update(confirmed_at=timezone.now(), last_step=57_000_000)
    assert MfaDevice.objects.get().last_step == 57_000_000
    MfaDevice.objects.create(user=luis, type=TOTP, secret=box(luis))  # cada cuenta, el suyo
    with pytest.raises(IntegrityError, match="user_mfa_devices_user_type_uq"):
        MfaDevice.objects.create(user=ana, type=TOTP, secret=box(ana))  # como mucho uno
    with pytest.raises(ProtectedError):  # la cuenta no se borra dejando su segundo factor huérfano
        ana.delete()


def test_backup_codes_are_kept_as_hashes_several_per_account() -> None:
    ana, luis = make_user(email="ana@example.com"), make_user(email="luis@example.com")
    first = MfaBackupCode.objects.create(user=ana, code_hash=HASH)
    MfaBackupCode.objects.create(user=ana, code_hash="cd" * 32)  # varios por cuenta
    MfaBackupCode.objects.create(user=luis, code_hash=HASH)  # el mismo en otra cuenta no se nota
    first.refresh_from_db()
    assert (first.user_id, first.code_hash, first.used_at) == (ana.pk, HASH, None)
    assert str(first) == str(first.pk) and first.pk.version == 7  # ni la cuenta ni el hash
    assert repr(first) == f"<MfaBackupCode: {first.pk}>"  # tampoco al registrar la instancia
    with pytest.raises(IntegrityError, match="user_mfa_backup_codes_user_hash_uq"):
        MfaBackupCode.objects.create(user=ana, code_hash=HASH)
    MfaBackupCode.objects.filter(pk=first.pk).update(used_at=first.created_at)  # gastarlo
    assert MfaBackupCode.objects.get(pk=first.pk).used_at == first.created_at
    with pytest.raises(ProtectedError):
        luis.delete()


def test_both_tables_are_platform_owned_with_the_documented_indexes_and_privileges() -> None:
    assert (models.SEALED_MIN, models.SEALED_MAX, models.SEALED_FORMAT) == (
        sealed._HEAD + sealed._NONCE + 1 + sealed._TAG,
        sealed._HEAD + sealed._NONCE + sealed.PLAINTEXT_MAX + sealed._TAG,
        bytes([sealed.FORMAT]),
    )  # lo que la columna admite es lo que `core.sealed` produce
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_user")
        assert cursor.fetchone() == ("crm_app",)  # el runtime las lee y las escribe
        for table in TABLES:  # platform-owned (ADR-001 §2): ni columna ni política
            cursor.execute(
                "SELECT count(*) FROM information_schema.columns"
                " WHERE table_name = %s AND column_name = 'organization_id'",
                [table],
            )
            assert cursor.fetchone() == (0,)
            cursor.execute("SELECT relrowsecurity FROM pg_class WHERE relname = %s", [table])
            assert cursor.fetchone() == (False,)
            cursor.execute(
                "SELECT privilege_type FROM information_schema.role_table_grants"
                " WHERE table_name = %s AND grantee = current_user",
                [table],
            )  # nada más que DML
            assert {row[0] for row in cursor.fetchall()} == {"SELECT", "INSERT", "UPDATE", "DELETE"}
        cursor.execute(
            "SELECT indexname, indexdef FROM pg_indexes WHERE tablename = ANY(%s)", [list(TABLES)]
        )
        indexes = {name: definition.split(" USING ")[1] for name, definition in cursor.fetchall()}
    assert indexes == {  # buscar por cuenta: el dispositivo, y un código por su hash
        "user_mfa_devices_pkey": "btree (id)",
        "user_mfa_devices_user_type_uq": "btree (user_id, type)",
        "user_mfa_backup_codes_pkey": "btree (id)",
        "user_mfa_backup_codes_user_hash_uq": "btree (user_id, code_hash)",
        "user_mfa_code_attempts_pkey": "btree (id)",  # F2-102: los intentos de una cuenta, por hora
        "user_mfa_code_attempts_user_ix": "btree (user_id, created_at)",
    }


def test_the_database_refuses_a_device_that_is_not_one(migrator: psycopg.Connection[Any]) -> None:
    ana = make_user(email="ana@example.com")
    good = box(ana)

    def add(*row: Any) -> None:
        migrator.execute(DEVICE, [ana.pk, *row])

    with pytest.raises(psycopg.errors.CheckViolation, match="user_mfa_devices_type_ck"):
        add("WEBAUTHN", good, "0", 1)  # todavía no hay otro tipo
    text = (base64.b32encode(KEY), KEY.hex().encode())  # en claro como texto: 32 y 40 bytes
    sizes = (b"", b"\x01" + b"x" * 30, b"\x01" + b"x" * 4127)  # ni vacío, ni corto, ni enorme
    for secret in (KEY, *text, *sizes, b"\x02" + good[1:]):  # ni en claro ni de otro formato
        with pytest.raises(psycopg.errors.CheckViolation, match="user_mfa_devices_secret_ck"):
            add("TOTP", secret, "0", 1)
    with pytest.raises(psycopg.errors.CheckViolation, match="user_mfa_devices_confirmed_ck"):
        add("TOTP", good, "-1 second", 1)  # no se confirma antes de crearse
    for step in (0, -1, None):  # confirmar es aceptar un paso: confirmado, siempre hay uno
        with pytest.raises(psycopg.errors.CheckViolation, match="user_mfa_devices_last_step_ck"):
            add("TOTP", good, "0", step)
    with pytest.raises(psycopg.errors.CheckViolation, match="user_mfa_devices_last_step_ck"):
        migrator.execute(  # sin confirmar no hay ningún paso aceptado
            "INSERT INTO user_mfa_devices (user_id, type, secret, created_at, last_step)"
            " VALUES (%s, 'TOTP', %s, now(), 1)",
            [ana.pk, good],
        )
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        migrator.execute(DEVICE, ["01a10000-0000-7000-8000-000000000000", "TOTP", good, "0", 1])
    add("TOTP", b"\x01" + b"x" * 31, "0", 1)  # el valor cifrado más corto; confirmado al crearse
    for column in ("user_id", "type", "secret", "created_at"):
        with pytest.raises(psycopg.errors.NotNullViolation):
            migrator.execute(f"UPDATE user_mfa_devices SET {column} = NULL")  # noqa: S608
    fails = "UPDATE user_mfa_devices SET failed_codes = %s"
    with pytest.raises(psycopg.errors.CheckViolation, match="user_mfa_devices_failed_codes_ck"):
        migrator.execute(fails, [1])  # confirmado no guarda fallos (ADR-024)
    migrator.execute("UPDATE user_mfa_devices SET confirmed_at = NULL, last_step = NULL")
    migrator.execute(fails, [models.ENROLLMENT_FAILURES - 1])
    for count in (models.ENROLLMENT_FAILURES, -1, None):  # el que agota el alta la borra
        with pytest.raises((psycopg.errors.CheckViolation, psycopg.errors.NotNullViolation)):
            migrator.execute(fails, [count])
    assert migrator.execute("SELECT count(*) FROM user_mfa_devices").fetchone() == (1,)


@pytest.mark.parametrize(
    "code_hash",
    ["", "ab" * 31, "ab" * 31 + "a", "AB" * 32, "g" * 64, " " + "ab" * 31 + "a", "ab" * 31 + "a\n"],
)
def test_the_database_only_takes_the_sha256_of_a_backup_code(
    migrator: psycopg.Connection[Any], code_hash: str
) -> None:
    ana = make_user(email="ana@example.com")
    with pytest.raises(psycopg.errors.CheckViolation, match="user_mfa_backup_codes_code_hash_ck"):
        migrator.execute(CODE, [ana.pk, code_hash, "1 minute"])


def test_the_database_keeps_a_backup_code_in_order(migrator: psycopg.Connection[Any]) -> None:
    ana = make_user(email="ana@example.com")
    migrator.execute(CODE, [ana.pk, HASH, "0"])  # gastado en el mismo instante: vale
    with pytest.raises(psycopg.errors.CheckViolation, match="user_mfa_backup_codes_used_ck"):
        migrator.execute(CODE, [ana.pk, "cd" * 32, "-1 second"])  # no se gasta antes de existir
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        migrator.execute(CODE, ["01a10000-0000-7000-8000-000000000000", "cd" * 32, "0"])
    with pytest.raises(psycopg.errors.StringDataRightTruncation):
        migrator.execute(CODE, [ana.pk, "ab" * 33, "0"])
    for column in ("user_id", "code_hash", "created_at"):
        with pytest.raises(psycopg.errors.NotNullViolation):
            migrator.execute(f"UPDATE user_mfa_backup_codes SET {column} = NULL")  # noqa: S608
    assert migrator.execute("SELECT count(*) FROM user_mfa_backup_codes").fetchone() == (1,)

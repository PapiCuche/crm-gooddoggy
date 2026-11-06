"""F7-02b: el token de una cuenta de WhatsApp, cifrado en reposo (ADR-022, ADR-029 §2): la tabla
`credentials`, su RLS, sus privilegios, la FK compuesta de la cuenta, el cifrado atado a la cuenta
y el re-cifrado con una clave nueva (`apps.conversations.credentials`). PostgreSQL real con
`crm_app`."""

import logging
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.db import IntegrityError, ProgrammingError
from django.utils import timezone

from apps.conversations import credentials
from apps.conversations.models import ChannelAccount, Credential
from apps.conversations.tasks import reseal_credentials
from core import sealed
from core.tenancy.scope import tenant_scope
from tests.test_contacts_api import crm, real_stack
from tests.test_conversations_tables import refused
from tests.test_memberships import ctx
from tests.test_pipelines_tables import as_migrator
from tests.test_tenant_task_signature import broker
from tests.test_whatsapp_account import credential

__all__ = ["broker", "crm", "real_stack"]  # los fixtures, para pytest
pytestmark = pytest.mark.usefixtures("tenant_db")
DB = psycopg.Connection[Any]
TOKEN = "meta-test-access-token-not-real-0001"  # noqa: S105 — ficticio
OTHER_TOKEN = "meta-test-access-token-not-real-0002"  # noqa: S105 — ficticio
OLD_KEY, NEW_KEY = bytes(range(32)), bytes(range(32, 64))


def store(org: UUID, owner: UUID, token: str = TOKEN, **over: Any) -> Credential:
    with tenant_scope(ctx(org, None)) as tenant:
        made: Credential = credentials.store(tenant, owner, token, **over)
        return made


def cipher(row: Credential) -> bytes:
    assert row.ciphertext is not None
    return bytes(row.ciphertext)


def reload(org: UUID, pk: UUID) -> Credential:
    with tenant_scope(ctx(org)):
        found: Credential = Credential.objects.get(pk=pk)
        return found


def everything(migrator: DB) -> str:
    """Lo que la base guarda de la credencial, salvo el cifrado, y los registros de auditoría."""
    return str(migrator.execute("SELECT t::text FROM credentials t").fetchall())


def test_a_token_is_stored_sealed_and_only_opens_for_its_own_account(
    crm: SimpleNamespace, migrator: DB, caplog: pytest.LogCaptureFixture
) -> None:
    one, two = uuid4(), uuid4()
    with caplog.at_level(logging.DEBUG):
        first, second = store(crm.a, one), store(crm.a, two, OTHER_TOKEN)
    row = reload(crm.a, first.pk)
    box = cipher(row)
    assert TOKEN.encode() not in box and box[:3] == b"\x01\x00" + bytes([sealed.current()])
    assert (row.last_four, row.key_version, row.revoked_at) == (TOKEN[-4:], sealed.current(), None)
    assert len(row.fingerprint) == 64 and row.fingerprint != first.last_four
    assert credentials.open_token(row) == TOKEN
    assert store(crm.b, uuid4()).ciphertext != row.ciphertext  # dos cifrados nunca coinciden
    with tenant_scope(ctx(crm.a)):  # alguien con escritura copia el cifrado de una a la otra
        Credential.objects.filter(pk=second.pk).update(ciphertext=box)
        copied = Credential.objects.get(pk=second.pk)
    with pytest.raises(sealed.SealError):
        credentials.open_token(copied)  # atado a `channel_account:<id>`
    with pytest.raises(sealed.SealError):
        sealed.unseal("mfa.totp", credentials.owner(one), box)  # ni como secreto de otro uso
    assert TOKEN not in caplog.text + everything(migrator)


def test_revoking_wipes_the_cipher_and_a_new_live_credential_may_follow(
    crm: SimpleNamespace, migrator: DB
) -> None:
    owner = uuid4()
    old = store(crm.a, owner)
    with tenant_scope(ctx(crm.a, None)) as tenant:
        credentials.revoke(tenant, old.pk)
        credentials.revoke(tenant, None)  # sin credencial no hace nada
    gone = reload(crm.a, old.pk)
    assert gone.ciphertext is None and gone.revoked_at is not None and gone.last_four == TOKEN[-4:]
    with pytest.raises(sealed.SealError):
        credentials.open_token(gone)  # ya no se puede abrir
    new = store(
        crm.a, owner, OTHER_TOKEN, rotated=True
    )  # una viva por dueño: la vieja ya no cuenta
    assert credentials.open_token(reload(crm.a, new.pk)) == OTHER_TOKEN and new.rotated_at
    assert TOKEN not in everything(migrator) and OTHER_TOKEN not in everything(migrator)


def test_reseal_moves_every_token_to_the_current_key_and_reports_what_it_cannot_open(
    crm: SimpleNamespace, settings: Any, caplog: pytest.LogCaptureFixture, migrator: DB
) -> None:
    settings.SECRETS_KEKS = {1: OLD_KEY}
    mine, theirs = store(crm.a, uuid4()), store(crm.b, uuid4(), OTHER_TOKEN)
    revoked = store(crm.a, uuid4())
    with tenant_scope(ctx(crm.a, None)) as tenant:
        credentials.revoke(tenant, revoked.pk)  # las invalidadas no cuentan
    assert reseal_credentials() == {"key": 1, "resealed": 0, "unreadable": 0, "busy": 0}
    settings.SECRETS_KEKS = {1: OLD_KEY, 7: NEW_KEY}  # rotación de la clave maestra
    migrator.execute("UPDATE credentials SET owner_id = %s WHERE id = %s", [uuid4(), theirs.pk])
    # la de B quedó ilegible: el valor ya no es de esa cuenta
    with caplog.at_level(logging.DEBUG):
        assert reseal_credentials() == {"key": 7, "resealed": 1, "unreadable": 1, "busy": 0}
    kept = reload(crm.a, mine.pk)
    assert cipher(kept)[:3] == b"\x01\x00\x07" and kept.key_version == 7
    assert credentials.open_token(kept) == TOKEN and kept.last_four == mine.last_four
    stuck = reload(crm.b, theirs.pk)
    assert stuck.key_version == 1 and cipher(stuck)[:3] == b"\x01\x00\x01"  # intacta
    assert reseal_credentials() == {"key": 7, "resealed": 0, "unreadable": 1, "busy": 0}
    settings.SECRETS_KEKS = {7: NEW_KEY}  # la anterior ya se puede retirar: lo vuelto a cifrar abre
    assert credentials.open_token(reload(crm.a, mine.pk)) == TOKEN
    assert TOKEN not in caplog.text + everything(migrator) and OTHER_TOKEN not in caplog.text


def test_the_database_keeps_credentials_inside_their_organization_and_narrow(
    crm: SimpleNamespace, migrator: DB
) -> None:
    mine, theirs = credential(crm.a), credential(crm.b)
    with pytest.raises(IntegrityError, match="channel_accounts_credential_fk"):
        with tenant_scope(ctx(crm.a)):  # la credencial de B no es de A, aunque exista
            ChannelAccount.objects.create(
                platform="WHATSAPP", name="x", status="PENDING", credential_id=theirs,
                external_account_id="100000000000003", external_parent_id="100000000000001",
            )  # fmt: skip
    with tenant_scope(ctx(crm.a)):
        assert not Credential.objects.filter(pk=theirs).exists()  # la RLS no la deja ver
    flags = migrator.execute(
        "SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = 'credentials'"
    ).fetchone()
    assert flags == (True, True)
    column = "SELECT has_column_privilege('crm_app', %s, %s, 'UPDATE')"
    table = "SELECT has_table_privilege('crm_app', 'credentials', %s)"
    for privilege in ("DELETE", "TRUNCATE"):
        assert migrator.execute(table, [privilege]).fetchone() == (False,)
    allowed = {"ciphertext", "key_version", "revoked_at"}
    names = [r[0] for r in migrator.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'credentials'"
    ).fetchall()]  # fmt: skip
    assert {
        n for n in names if migrator.execute(column, ["credentials", n]).fetchone() == (True,)
    } == allowed
    kept = {"platform", "external_account_id", "external_parent_id", "organization_id", "id"}
    assert not any(migrator.execute(column, ["channel_accounts", n]).fetchone()[0] for n in kept)  # type: ignore[index]
    assert migrator.execute(column, ["channel_accounts", "credential_id"]).fetchone() == (True,)
    with pytest.raises(ProgrammingError, match="permission denied"):
        with tenant_scope(ctx(crm.a)):
            Credential.objects.filter(pk=mine).update(last_four="zzzz")  # ni la huella ni nada
    with pytest.raises(ProgrammingError, match="permission denied"):
        with tenant_scope(ctx(crm.a)):
            Credential.objects.filter(pk=mine).delete()


@pytest.mark.parametrize(
    ("fields", "constraint"),
    [
        ({"ciphertext": None}, "credentials_ciphertext_ck"),  # viva sin cifrado
        ({"ciphertext": b"\x02" + bytes(40)}, "credentials_ciphertext_ck"),  # otro formato
        ({"ciphertext": b"\x01" + bytes(8)}, "credentials_ciphertext_ck"),  # demasiado corto
        ({"revoked_at": timezone.now()}, "credentials_ciphertext_ck"),  # invalidada con cifrado
        ({"last_four": "abc"}, "credentials_shape_ck"),
        ({"fingerprint": "no-es-hex"}, "credentials_shape_ck"),
        ({"key_version": 0}, "credentials_shape_ck"),
        ({"owner_type": "USER"}, "credentials_owner_ck"),
    ],
)
def test_a_credential_row_must_have_the_shape_of_a_sealed_value(
    crm: SimpleNamespace, fields: dict[str, Any], constraint: str
) -> None:
    row: dict[str, Any] = {
        "owner_id": uuid4(), "ciphertext": b"\x01" + bytes(40), "key_version": 1,
        "last_four": "abcd", "fingerprint": "0" * 64,
    } | fields  # fmt: skip
    refused(crm.a, constraint, lambda: Credential.objects.create(**row))
    owner = uuid4()
    credential(crm.a, owner)
    with pytest.raises(IntegrityError, match="credentials_live_owner_uq"):
        credential(crm.a, owner)  # una viva por dueño


def test_the_reseal_task_validates_its_arguments_for_real_and_is_scheduled(
    settings: Any, broker: list[Any]
) -> None:
    """Lección de #568: `apply_async` valida la firma registrada, no la de la función."""
    assert reseal_credentials.tenancy == "platform"
    assert reseal_credentials.apply_async(kwargs={"max_orgs": 5, "batch_size": 10}).id
    assert reseal_credentials.apply_async().id
    with pytest.raises(TypeError):  # no es una tarea de tenant: nada de `organization_id`
        reseal_credentials.apply_async(kwargs={"organization_id": str(uuid4())})
    assert [name for name, _ in broker] == [reseal_credentials.name] * 2
    entry = settings.CELERY_BEAT_SCHEDULE["conversations.reseal_credentials"]
    assert entry["task"] == reseal_credentials.name


def test_without_keys_the_reseal_fails_closed(settings: Any) -> None:
    settings.SECRETS_KEKS = {}
    with pytest.raises(ImproperlyConfigured):
        credentials.reseal()


def test_the_migration_goes_back_and_forward(crm: SimpleNamespace, migrator: DB) -> None:
    mine = credential(crm.a)
    grant = "SELECT has_table_privilege('crm_app', 'channel_accounts', 'UPDATE')"
    exists = "SELECT to_regclass('public.credentials') IS NOT NULL"
    assert migrator.execute(exists).fetchone() == (True,)
    assert migrator.execute(grant).fetchone() == (False,)  # por columna
    with as_migrator():
        call_command("migrate", "conversations", "0005", verbosity=0)
    assert migrator.execute(exists).fetchone() == (False,)  # atrás: ni tabla ni función
    function = "SELECT count(*) FROM pg_proc WHERE proname = 'credentials_stale_organizations'"
    assert migrator.execute(function).fetchone() == (0,)
    assert migrator.execute(grant).fetchone() == (True,)  # y el permiso de la tabla, como antes
    with as_migrator():
        call_command("migrate", "conversations", verbosity=0)
    assert migrator.execute(exists).fetchone() == (True,) and migrator.execute(
        grant
    ).fetchone() == (False,)
    assert migrator.execute(function).fetchone() == (1,) and mine is not None
    execute = "SELECT has_function_privilege('public', 'public.credentials_stale_organizations(integer, integer)', 'EXECUTE')"  # noqa: E501
    assert migrator.execute(execute).fetchone() == (False,)

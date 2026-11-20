"""F8-03a: la clave del proveedor de IA de una organización, cifrada en reposo (ADR-034 §4,
ADR-022): las tablas `ai_provider_accounts` y `ai_provider_keys`, su RLS y sus privilegios, el
cifrado atado a la cuenta y al proveedor, el re-cifrado con una clave maestra nueva
(`apps.ai_gateway.keys`) y el permiso `ai.manage`. PostgreSQL real con `crm_app`. Ninguna clave
real: las de aquí son ficticias y no tienen la forma de una clave de ningún proveedor."""

import logging
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.db import IntegrityError, ProgrammingError, connection
from django.utils import timezone

from apps.access.catalog import BY_CODE, ROLE_TEMPLATES
from apps.ai_gateway import keys
from apps.ai_gateway.models import KEY_PURPOSE, AIProviderAccount, AIProviderKey
from apps.ai_gateway.tasks import reseal_provider_keys
from core import sealed
from core.observability import reporting
from core.tenancy.scope import tenant_scope
from tests.test_contacts_api import crm, real_stack
from tests.test_conversations_tables import refused
from tests.test_memberships import ctx
from tests.test_pipelines_tables import as_migrator
from tests.test_tenant_task_signature import broker

__all__ = ["broker", "crm", "real_stack"]  # los fixtures, para pytest
pytestmark = pytest.mark.usefixtures("tenant_db")
DB = psycopg.Connection[Any]
KEY = "fake-provider-key-not-real-0001"  # ficticia
OTHER_KEY = "fake-provider-key-not-real-0002"  # ficticia
ANTHROPIC = "ANTHROPIC"
OLD_KEK, NEW_KEK = bytes(range(32)), bytes(range(32, 64))


def store(org: UUID, key: str = KEY, user: Any = None) -> AIProviderAccount:
    with tenant_scope(ctx(org, user)) as tenant:
        return keys.store(tenant, ANTHROPIC, key)


def open_key(org: UUID) -> str:
    with tenant_scope(ctx(org)) as tenant:
        return keys.open_key(tenant, ANTHROPIC)


def revoke(org: UUID) -> bool:
    with tenant_scope(ctx(org)) as tenant:
        return keys.revoke(tenant, ANTHROPIC)


def rows(org: UUID) -> list[AIProviderKey]:
    with tenant_scope(ctx(org)):
        return list(AIProviderKey.objects.order_by("created_at", "id"))


def account(org: UUID) -> AIProviderAccount:
    with tenant_scope(ctx(org)):
        found: AIProviderAccount = AIProviderAccount.objects.get()
        return found


def cipher(row: AIProviderKey) -> bytes:
    assert row.ciphertext is not None
    return bytes(row.ciphertext)


def everything(migrator: DB) -> str:
    """Todo lo que la base guarda de las cuentas, sus claves, la auditoría y el outbox, en texto
    (el cifrado en hexadecimal)."""
    tables = ("ai_provider_accounts", "ai_provider_keys", "audit_logs", "outbox_events")
    return str([migrator.execute(f"SELECT t::text FROM {t} t").fetchall() for t in tables])  # noqa: S608


def test_a_key_is_stored_sealed_and_only_opens_for_its_own_account_and_provider(
    crm: SimpleNamespace, migrator: DB, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG):
        mine = store(crm.a, user=crm.ana)
        theirs = store(crm.b, OTHER_KEY)
    assert (mine.provider, mine.status, mine.key_last_four) == (ANTHROPIC, "ACTIVE", KEY[-4:])
    assert (mine.configured_by_user_id, mine.rotated_at, mine.revoked_at) == (
        crm.ana.pk,
        None,
        None,
    )
    [row] = rows(crm.a)
    box = cipher(row)
    assert KEY.encode() not in box and box[:3] == b"\x01\x00" + bytes([sealed.current()])
    assert (row.account_id, row.provider, row.key_version) == (mine.pk, ANTHROPIC, sealed.current())
    assert open_key(crm.a) == KEY and open_key(crm.b) == OTHER_KEY
    # alguien con acceso a la base copia el cifrado de A en la fila de B: no se abre
    migrator.execute(
        "UPDATE ai_provider_keys SET ciphertext = %s WHERE account_id = %s", [box, theirs.pk]
    )
    with pytest.raises(sealed.SealError):
        open_key(crm.b)
    with pytest.raises(sealed.SealError):  # ni como clave de otro proveedor de la misma cuenta
        sealed.unseal(KEY_PURPOSE, keys.owner("OPENAI", mine.pk), box)
    with pytest.raises(sealed.SealError):  # ni como secreto de otro uso
        sealed.unseal("channel.access_token", keys.owner(ANTHROPIC, mine.pk), box)
    assert KEY not in caplog.text + everything(migrator) and OTHER_KEY not in caplog.text


def test_rotating_and_revoking_wipe_the_old_cipher_and_a_new_key_reactivates(
    crm: SimpleNamespace, migrator: DB
) -> None:
    first = store(crm.a)
    [box] = [cipher(row) for row in rows(crm.a)]
    second = store(crm.a, OTHER_KEY, crm.ana)  # rotación: la misma cuenta
    assert second.pk == first.pk and second.rotated_at is not None
    assert second.updated_at > first.updated_at  # `update()` no aplica `auto_now`
    assert (second.key_last_four, second.configured_by_user_id) == (OTHER_KEY[-4:], crm.ana.pk)
    old, new = rows(crm.a)
    assert old.ciphertext is None and old.revoked_at is not None and new.revoked_at is None
    assert open_key(crm.a) == OTHER_KEY
    assert revoke(crm.a) is True and revoke(crm.a) is False  # la segunda no hace nada
    gone = account(crm.a)
    assert (gone.status, gone.revoked_at is not None) == ("DISABLED", True)
    assert gone.updated_at > second.updated_at
    assert all(row.ciphertext is None and row.revoked_at for row in rows(crm.a))
    with pytest.raises(sealed.SealError):
        open_key(crm.a)  # desactivada: no hay nada que abrir
    # alguien con acceso a la base revive la primera clave en la cuenta desactivada: no se abre
    revived = "UPDATE ai_provider_keys SET ciphertext = %s, revoked_at = NULL WHERE id = %s"
    migrator.execute(revived, [box, old.pk])
    with pytest.raises(sealed.SealError):
        open_key(crm.a)  # solo abre la clave de una cuenta activa
    migrator.execute(
        "UPDATE ai_provider_keys SET ciphertext = NULL, revoked_at = now() WHERE id = %s", [old.pk]
    )
    assert revoke(crm.b) is False  # sin cuenta
    back = store(crm.a)  # una clave nueva la reactiva; no sustituyó a ninguna viva
    assert (back.status, back.revoked_at, back.rotated_at) == ("ACTIVE", None, second.rotated_at)
    assert open_key(crm.a) == KEY and len(rows(crm.a)) == 3
    assert OTHER_KEY not in everything(migrator) and KEY not in everything(migrator)


def test_the_key_never_shows_in_errors_logs_or_values(
    crm: SimpleNamespace, migrator: DB, caplog: pytest.LogCaptureFixture
) -> None:
    bad = "fake key with spaces not real 0003"  # ficticia y mal formada
    with caplog.at_level(logging.DEBUG):
        for wrong in (bad, KEY[:19], KEY + "!"):
            with pytest.raises(keys.MalformedKey) as malformed:
                store(crm.a, wrong)
            assert wrong not in str(malformed.value) + repr(malformed.value.args)
        with pytest.raises(ValueError, match="proveedor") as unknown:
            with tenant_scope(ctx(crm.a)) as tenant:
                keys.store(tenant, "OPENAI", KEY)
        assert KEY not in str(unknown.value)
        with pytest.raises(sealed.SealError) as missing:
            open_key(crm.a)  # sin cuenta
        made = store(crm.a)
        [row] = rows(crm.a)
        with pytest.raises(IntegrityError) as broken:  # el error de la base no lleva la clave
            with tenant_scope(ctx(crm.a)):
                AIProviderKey.objects.create(
                    account_id=made.pk, provider=ANTHROPIC, ciphertext=row.ciphertext, key_version=1
                )
    seen = caplog.text + str(missing.value) + str(broken.value) + repr(made) + str(made) + repr(row)
    assert KEY not in seen and bad not in seen
    assert not any(isinstance(value, str) and KEY in value for value in vars(made).values())
    assert KEY not in everything(migrator)


def test_the_database_keeps_accounts_and_keys_inside_their_organization_and_narrow(
    crm: SimpleNamespace, migrator: DB
) -> None:
    mine, theirs = store(crm.a), store(crm.b)
    with tenant_scope(ctx(crm.a)):  # la RLS, sin el filtro del manager, no deja ver lo de B
        assert list(AIProviderAccount._base_manager.values_list("pk", flat=True)) == [mine.pk]
        assert {k.account_id for k in AIProviderKey._base_manager.all()} == {mine.pk}
    for table in ("ai_provider_accounts", "ai_provider_keys"):
        flags = migrator.execute(
            "SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = %s", [table]
        ).fetchone()
        assert flags == (True, True), table
        for privilege in ("DELETE", "TRUNCATE"):
            sql = "SELECT has_table_privilege('crm_app', %s, %s)"
            assert migrator.execute(sql, [table, privilege]).fetchone() == (False,)
    assert updatable(migrator, "ai_provider_keys") == {"ciphertext", "key_version", "revoked_at"}
    assert updatable(migrator, "ai_provider_accounts") == {
        "status", "model", "key_last_four", "configured_by_user_id", "configured_at",
        "rotated_at", "revoked_at", "updated_at",
    }  # fmt: skip
    for statement in (
        lambda: AIProviderAccount.objects.filter(pk=mine.pk).update(provider="OPENAI"),
        lambda: AIProviderKey.objects.filter(account_id=mine.pk).update(account_id=uuid4()),
        lambda: AIProviderKey.objects.filter(account_id=mine.pk).delete(),
        lambda: AIProviderAccount.objects.filter(pk=mine.pk).delete(),
    ):
        with pytest.raises(ProgrammingError, match="permission denied"):
            with tenant_scope(ctx(crm.a)):
                statement()
    live: dict[str, Any] = {"provider": ANTHROPIC, "ciphertext": b"\x01" + bytes(40)}
    gone: dict[str, Any] = {"ciphertext": None, "revoked_at": timezone.now()}  # sin la única viva

    def key(org: UUID, account_id: UUID, **fields: Any) -> None:
        refused(org, "ai_provider_keys_account_fk", lambda: AIProviderKey.objects.create(
            account_id=account_id, key_version=1, **(live | fields)))  # fmt: skip

    key(crm.a, theirs.pk)  # la cuenta de B no es de A
    key(crm.b, theirs.pk, **gone, provider="OPENAI")  # ni la clave de otro proveedor de la cuenta

    def configured_by(user_id: UUID) -> int:
        return AIProviderAccount.objects.filter(pk=mine.pk).update(configured_by_user_id=user_id)

    refused(crm.a, "configured_by_fk", lambda: configured_by(crm.luis.pk))  # luis es de B
    insert = (
        "INSERT INTO ai_provider_accounts (organization_id, provider, status, key_last_four,"
        " configured_at, updated_at, model)"
        " VALUES (%s, 'ANTHROPIC', 'ACTIVE', 'abcd', now(), now(), 'claude-sonnet-5-5')"
    )
    with pytest.raises(psycopg.errors.ForeignKeyViolation, match="organization_fk"):
        migrator.execute(insert, [uuid4()])  # ni con el migrador: la organización existe


def updatable(migrator: DB, table: str) -> set[str]:
    column = "SELECT has_column_privilege('crm_app', %s, %s, 'UPDATE')"
    names = [r[0] for r in migrator.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = %s", [table]
    ).fetchall()]  # fmt: skip
    return {n for n in names if migrator.execute(column, [table, n]).fetchone() == (True,)}


@pytest.mark.parametrize(
    ("fields", "constraint"),
    [
        ({"ciphertext": None}, "ai_provider_keys_ciphertext_ck"),  # viva sin cifrado
        ({"ciphertext": b"\x02" + bytes(40)}, "ai_provider_keys_ciphertext_ck"),  # otro formato
        ({"ciphertext": b"\x01" + bytes(8)}, "ai_provider_keys_ciphertext_ck"),  # muy corto
        ({"ciphertext": b"\x01" + bytes(4200)}, "ai_provider_keys_ciphertext_ck"),  # muy largo
        (
            {"revoked_at": timezone.now()},
            "ai_provider_keys_ciphertext_ck",
        ),  # invalidada con cifrado
        ({"key_version": 0}, "ai_provider_keys_version_ck"),
        ({}, "ai_provider_keys_live_uq"),  # una viva por cuenta
    ],
)
def test_a_key_row_must_have_the_shape_of_a_sealed_value(
    crm: SimpleNamespace, fields: dict[str, Any], constraint: str
) -> None:
    made = store(crm.a)
    row = {"account_id": made.pk, "provider": ANTHROPIC, "ciphertext": b"\x01" + bytes(40)}
    row |= {"key_version": 1, "revoked_at": None} | fields
    if constraint != "ai_provider_keys_live_uq":
        revoke(crm.a)  # sin clave viva, para que solo falle la forma
    refused(crm.a, constraint, lambda: AIProviderKey.objects.create(**row))


@pytest.mark.parametrize(
    ("fields", "constraint"),
    [
        ({"provider": "OPENAI"}, "ai_provider_accounts_provider_ck"),
        ({"status": "DISABLED"}, "ai_provider_accounts_status_ck"),  # desactivada sin fecha
        ({"revoked_at": timezone.now()}, "ai_provider_accounts_status_ck"),  # activa revocada
        ({"key_last_four": "abc"}, "ai_provider_accounts_last_four_ck"),
        ({"model": "Claude Opus"}, "ai_provider_accounts_model_ck"),  # solo la forma (F8-05b6)
        ({}, "ai_provider_accounts_provider_uq"),  # una por proveedor
    ],
)
def test_an_account_row_keeps_its_shape(
    crm: SimpleNamespace, fields: dict[str, Any], constraint: str
) -> None:
    if constraint == "ai_provider_accounts_provider_uq":
        store(crm.a)
    row = {"provider": ANTHROPIC, "key_last_four": "abcd", "configured_at": timezone.now(),
           "model": "claude-sonnet-5-5"}  # fmt: skip
    refused(crm.a, constraint, lambda: AIProviderAccount.objects.create(**(row | fields)))


def test_reseal_moves_every_key_to_the_current_master_key_and_reports_what_it_cannot_open(
    crm: SimpleNamespace,
    settings: Any,
    caplog: pytest.LogCaptureFixture,
    migrator: DB,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings.SECRETS_KEKS = {1: OLD_KEK}
    mine, theirs = store(crm.a), store(crm.b, OTHER_KEY)
    store(crm.a, OTHER_KEY)  # la anterior de A, invalidada: no cuenta
    assert reseal_provider_keys() == {"key": 1, "resealed": 0, "unreadable": 0, "busy": 0}
    # la de B, con un cifrado (de la clave 1) que no es suyo: ilegible
    alien = sealed.seal(KEY_PURPOSE, keys.owner(ANTHROPIC, uuid4()), OTHER_KEY.encode())
    sql = "UPDATE ai_provider_keys SET ciphertext = %s WHERE account_id = %s"
    migrator.execute(sql, [alien, theirs.pk])
    settings.SECRETS_KEKS = {1: OLD_KEK, 7: NEW_KEK}  # rotación de la clave maestra
    reports: list[tuple[BaseException, Any]] = []

    class Spy:
        def capture_exception(self, error: BaseException, context: Any = None) -> None:
            reports.append((error, context))

    monkeypatch.setattr(reporting, "reporter", lambda: Spy())
    with caplog.at_level(logging.DEBUG):
        assert reseal_provider_keys() == {"key": 7, "resealed": 1, "unreadable": 1, "busy": 0}
    [(error, context)] = reports  # una vez, solo con cuántas
    assert type(error) is keys.UnreadableKeys and error.args == (1,)
    assert context == {"action": "ai.provider_keys_reseal"} and OTHER_KEY not in repr(error)
    kept = rows(crm.a)[-1]
    assert cipher(kept)[:3] == b"\x01\x00\x07" and kept.key_version == 7
    assert open_key(crm.a) == OTHER_KEY and account(crm.a).pk == mine.pk
    stuck = rows(crm.b)[0]
    assert stuck.key_version == 1 and cipher(stuck) == alien  # intacta
    assert reseal_provider_keys() == {"key": 7, "resealed": 0, "unreadable": 1, "busy": 0}
    assert len(reports) == 2  # cada pasada lo vuelve a avisar

    def broken() -> Any:
        raise RuntimeError("sin reporte")

    monkeypatch.setattr(reporting, "reporter", broken)
    assert OTHER_KEY not in caplog.text and KEY not in caplog.text  # lo vuelto a cifrar, tampoco
    caplog.clear()
    with caplog.at_level(logging.DEBUG):  # un reporte roto no la para, y queda en el log
        assert reseal_provider_keys() == {"key": 7, "resealed": 0, "unreadable": 1, "busy": 0}
    told = [
        r for r in caplog.records if "ai.provider_keys_unreadable_not_reported" in r.getMessage()
    ]
    assert [r.levelname for r in told] == ["ERROR"]
    settings.SECRETS_KEKS = {7: NEW_KEK}  # la anterior ya se puede retirar
    assert open_key(crm.a) == OTHER_KEY
    assert OTHER_KEY not in caplog.text + everything(migrator) and KEY not in caplog.text


def test_the_reseal_task_validates_its_arguments_for_real_and_is_scheduled(
    settings: Any, broker: list[Any]
) -> None:
    """Lección de #568: `apply_async` valida la firma registrada, no la de la función."""
    assert reseal_provider_keys.tenancy == "platform"
    assert reseal_provider_keys.apply_async(kwargs={"max_orgs": 5, "batch_size": 10}).id
    with pytest.raises(TypeError):  # no es una tarea de tenant: nada de `organization_id`
        reseal_provider_keys.apply_async(kwargs={"organization_id": str(uuid4())})
    assert [name for name, _ in broker] == [reseal_provider_keys.name]
    entry = settings.CELERY_BEAT_SCHEDULE["ai_gateway.reseal_provider_keys"]
    assert entry["task"] == reseal_provider_keys.name


def test_without_master_keys_the_reseal_fails_closed(settings: Any) -> None:
    settings.SECRETS_KEKS = {}
    with pytest.raises(ImproperlyConfigured):
        keys.reseal()


def test_the_discovery_function_lists_only_organizations_with_a_live_key_under_another_master(
    crm: SimpleNamespace, settings: Any
) -> None:
    settings.SECRETS_KEKS = {1: OLD_KEK}
    store(crm.a)
    store(crm.b)
    revoke(crm.a)  # la de A está invalidada, la de B viva

    def stale(version: int, limit: int = 10) -> set[UUID]:
        with connection.cursor() as cursor:  # como la tarea: sin tenant
            cursor.execute(
                "SELECT public.ai_provider_keys_stale_organizations(%s, %s)", [version, limit]
            )
            return {UUID(str(row[0])) for row in cursor.fetchall()}

    assert stale(1) == set()  # ya están con la clave 1
    assert stale(7) == {crm.b} and stale(7, 0) == set()


def test_the_migration_goes_back_and_forward(crm: SimpleNamespace, migrator: DB) -> None:
    store(crm.a)
    exists = "SELECT to_regclass('public.ai_provider_keys') IS NOT NULL"
    function = "SELECT count(*) FROM pg_proc WHERE proname = 'ai_provider_keys_stale_organizations'"
    with as_migrator():
        call_command("migrate", "ai_gateway", "zero", verbosity=0)
    assert migrator.execute(exists).fetchone() == (False,)
    assert migrator.execute(function).fetchone() == (0,)
    with as_migrator():  # todo: `zero` deshizo también ai_agents 0002+ (depende de ai_gateway)
        call_command("migrate", verbosity=0)
    assert migrator.execute(exists).fetchone() == (True,)
    assert migrator.execute(function).fetchone() == (1,)
    signature = "public.ai_provider_keys_stale_organizations(integer, integer)"
    execute = "SELECT has_function_privilege(%s, %s, 'EXECUTE')"
    assert migrator.execute(execute, ["public", signature]).fetchone() == (False,)
    assert migrator.execute(execute, ["crm_app", signature]).fetchone() == (True,)
    assert updatable(migrator, "ai_provider_keys") == {"ciphertext", "key_version", "revoked_at"}


def test_ai_manage_is_sensitive_and_only_the_owner_template_is_born_with_it() -> None:
    assert BY_CODE["ai.manage"].is_sensitive and not BY_CODE["ai.manage"].supports_scope
    assert [t.code for t in ROLE_TEMPLATES if "ai.manage" in t.grants] == ["owner"]

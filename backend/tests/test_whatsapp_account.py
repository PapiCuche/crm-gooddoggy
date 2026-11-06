"""F7-02a: la cuenta de canal de WhatsApp (ADR-029 §2): plataforma, columnas, CHECK, identidad
fija fuera de `PENDING`, unicidad del número entre organizaciones y la migración, adelante y atrás.
PostgreSQL real con el rol `crm_app`."""

from collections.abc import Callable
from functools import partial
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from django.core.management import call_command
from django.db import IntegrityError, ProgrammingError
from django.utils import timezone

from apps.conversations import commands
from apps.conversations.models import ChannelAccount, Credential
from core.tenancy.scope import tenant_scope
from tests.test_conversations_tables import refused, world
from tests.test_memberships import ctx
from tests.test_pipelines_tables import as_migrator

__all__ = ["world"]  # el fixture, para pytest
pytestmark = pytest.mark.usefixtures("tenant_db")
DB = psycopg.Connection[Any]
WABA, NUMBER, OTHER = "100000000000001", "100000000000002", "100000000000009"
REAL: dict[str, Any] = {
    "platform": "WHATSAPP", "name": "WhatsApp", "status": "PENDING",
    "external_account_id": NUMBER, "external_parent_id": WABA,
}  # fmt: skip
ONE = "SELECT count(*) FROM {} WHERE {}"
COLUMNS = (
    "external_parent_id credential_id status_detail quality_rating messaging_limit_tier "
    "last_healthcheck_at"
).split()


def make(org: UUID, **over: Any) -> ChannelAccount:
    with tenant_scope(ctx(org)):
        made: ChannelAccount = ChannelAccount.objects.create(**REAL | over)
        return made


def credential(org: UUID, owner: UUID | None = None) -> UUID:
    """Una credencial viva de la organización (el cifrado es de relleno: importa la FK)."""
    with tenant_scope(ctx(org)):
        made = Credential.objects.create(
            owner_id=owner or uuid4(), ciphertext=b"\x01" + bytes(40), key_version=1,
            last_four="abcd",
        )  # fmt: skip
        return made.pk  # type: ignore[no-any-return]


def ready(org: UUID, **over: Any) -> ChannelAccount:
    fields = {"status": "CONNECTED", "credential_id": credential(org)}
    return make(org, **fields | {"last_healthcheck_at": timezone.now()} | over)


def change(org: UUID, pk: UUID, **fields: Any) -> None:
    with tenant_scope(ctx(org)):
        ChannelAccount.objects.filter(pk=pk).update(**fields)


def attempt(world: SimpleNamespace, constraint: str, action: Callable[[], Any]) -> None:
    refused(world.a, constraint, action)


def test_a_real_account_is_a_number_of_a_waba_and_starts_pending(world: SimpleNamespace) -> None:
    account = make(world.a, display_identifier="+51 900 000 002")
    assert (account.platform, account.status) == ("WHATSAPP", "PENDING")
    assert account.credential_id is None and account.quality_rating is None
    assert account.last_healthcheck_at is None


@pytest.mark.parametrize(
    "bad",
    [
        {"external_account_id": "no-numerico"},
        {"external_account_id": "123"},
        {"external_account_id": "1" * 31},
        {"external_parent_id": "abc"},
        {"external_parent_id": None},
    ],
)
def test_a_whatsapp_account_needs_numeric_ids_for_the_number_and_the_waba(
    world: SimpleNamespace, bad: dict[str, Any]
) -> None:
    attempt(world, "channel_accounts_whatsapp_ck", lambda: make(world.a, **bad))


@pytest.mark.parametrize(
    "extra",
    [
        {"external_parent_id": WABA},
        {"credential_id": uuid4()},
        {"status_detail": "TOKEN_INVALID"},
        {"quality_rating": "GREEN"},
        {"messaging_limit_tier": "TIER_250"},
        {"last_healthcheck_at": timezone.now()},
    ],
)
def test_the_sandbox_has_no_waba_credential_or_health(
    world: SimpleNamespace, extra: dict[str, Any]
) -> None:
    sandbox = {
        "platform": "SANDBOX",
        "external_account_id": "sandbox-x",
        "external_parent_id": None,
    }
    attempt(world, "channel_accounts_sandbox_ck", lambda: make(world.a, **sandbox | extra))


def test_a_real_account_is_connected_only_with_a_verified_credential(
    world: SimpleNamespace,
) -> None:
    now = timezone.now()
    for extra in ({}, {"credential_id": uuid4()}, {"last_healthcheck_at": now}):
        attempt(
            world,
            "channel_accounts_connected_ck",
            partial(make, world.a, status="CONNECTED", **extra),
        )
    assert ready(world.a).status == "CONNECTED"
    pending = make(world.a, external_account_id="100000000000003")
    attempt(
        world,
        "channel_accounts_connected_ck",
        lambda: change(world.a, pending.pk, status="CONNECTED"),
    )


@pytest.mark.parametrize(
    ("field", "bad", "good", "constraint"),
    [
        ("status_detail", "porque sí", "TOKEN_INVALID", "channel_accounts_detail_ck"),
        ("quality_rating", "AZUL", "GREEN", "channel_accounts_quality_ck"),
        ("messaging_limit_tier", "TIER_MUCHOS", "TIER_1K", "channel_accounts_tier_ck"),
    ],
)
def test_the_health_columns_take_only_their_closed_values(
    world: SimpleNamespace, field: str, bad: str, good: str, constraint: str
) -> None:
    attempt(world, constraint, lambda: make(world.a, **{field: bad}))
    assert getattr(make(world.a, **{field: good}), field) == good


def forced(migrator: DB, pk: UUID, **fields: Any) -> None:
    """Un UPDATE del dueño de la tabla: el rol de la aplicación ni siquiera puede escribir esas
    columnas (0006), así que el disparador se prueba por aquí."""
    sets = ", ".join(f"{name} = %s" for name in fields)
    migrator.execute(f"UPDATE channel_accounts SET {sets} WHERE id = %s", [*fields.values(), pk])  # noqa: S608


def test_the_identity_of_a_connected_account_never_changes(
    world: SimpleNamespace, migrator: DB
) -> None:
    sandbox, live = world.account_a, ready(world.a)  # el Sandbox nace CONNECTED
    for pk, fields in (
        (sandbox.pk, {"platform": "WHATSAPP"}),
        (sandbox.pk, {"external_account_id": "otro"}),
        (live.pk, {"external_account_id": OTHER}),
        (live.pk, {"external_parent_id": OTHER}),
        (live.pk, {"platform": "SANDBOX", "external_parent_id": None, "credential_id": None}),
        (live.pk, {"status": "PENDING"}),  # volver a PENDING reabriría la identidad
        (sandbox.pk, {"status": "PENDING"}),
        (live.pk, {"status": "PENDING", "external_account_id": OTHER}),
    ):
        with pytest.raises(psycopg.errors.CheckViolation, match="channel_accounts_identity_fixed"):
            forced(migrator, pk, **fields)
    change(world.a, sandbox.pk, name="Otro nombre")  # lo demás sí cambia
    change(world.a, live.pk, quality_rating="GREEN", status="DEGRADED", status_detail="QUALITY_LOW")
    with tenant_scope(ctx(world.a)):
        assert ChannelAccount.objects.get(pk=live.pk).external_account_id == NUMBER


def test_a_pending_account_may_still_fix_its_ids(world: SimpleNamespace, migrator: DB) -> None:
    pending = make(world.a)
    forced(migrator, pending.pk, external_account_id="100000000000004", external_parent_id=OTHER)
    with tenant_scope(ctx(world.a)):
        found = ChannelAccount.objects.get(pk=pending.pk)
    assert (found.external_account_id, found.external_parent_id) == ("100000000000004", OTHER)


@pytest.mark.parametrize(
    "fields",
    [{"platform": "SANDBOX"}, {"external_account_id": OTHER}, {"external_parent_id": OTHER}],
)
def test_the_application_role_cannot_even_write_the_identity_columns(
    world: SimpleNamespace, fields: dict[str, Any]
) -> None:
    """F7-02b: UPDATE por columna (como `messages`); ni en `PENDING` ni siendo la misma fila."""
    pending = make(world.a)
    with pytest.raises(ProgrammingError, match="permission denied"):
        change(world.a, pending.pk, **fields)


def test_a_number_belongs_to_one_organization_and_the_database_says_so(
    world: SimpleNamespace, migrator: DB
) -> None:
    mine = make(world.a)
    with pytest.raises(IntegrityError, match="channel_accounts_external_uq"):
        make(world.b)  # el índice es global: la RLS de la otra organización no lo esconde
    with tenant_scope(ctx(world.b)):
        assert not ChannelAccount.objects.filter(external_account_id=NUMBER).exists()
    change(world.a, mine.pk, deleted_at=timezone.now())
    assert make(world.b).organization_id == world.b  # borrada la primera, se puede registrar
    live = "external_account_id = %s AND deleted_at IS NULL"
    assert migrator.execute(ONE.format("channel_accounts", live), [NUMBER]).fetchone() == (1,)


def test_the_sandbox_account_still_works_as_in_phase_6(world: SimpleNamespace) -> None:
    with tenant_scope(ctx(world.a)) as tenant:
        found = commands.ensure_sandbox_account(tenant)
        again = commands.ensure_sandbox_account(tenant)
    assert found.pk == again.pk and (found.status, found.platform) == ("CONNECTED", "SANDBOX")
    assert (found.external_parent_id, found.credential_id, found.quality_rating) == (None,) * 3


def schema(migrator: DB) -> tuple[set[str], set[str], int]:
    columns = "SELECT column_name FROM information_schema.columns WHERE table_name = %s"
    checks = "SELECT conname FROM pg_constraint WHERE conrelid = %s::regclass AND contype = 'c'"
    trigger = migrator.execute(
        ONE.format("pg_trigger", "tgname = %s"), ["channel_accounts_identity_fixed"]
    )
    found = trigger.fetchone()
    assert found is not None
    return (
        {r[0] for r in migrator.execute(columns, ["channel_accounts"]).fetchall()},
        {r[0] for r in migrator.execute(checks, ["channel_accounts"]).fetchall()},
        int(found[0]),
    )


def test_the_migration_goes_back_and_forward_and_keeps_the_sandbox_accounts(
    world: SimpleNamespace, migrator: DB
) -> None:
    columns, checks, trigger = schema(migrator)
    assert set(COLUMNS) <= columns and "channel_accounts_whatsapp_ck" in checks and trigger == 1
    with as_migrator():
        call_command("migrate", "conversations", "0004", verbosity=0)
    columns, checks, trigger = schema(migrator)
    assert not set(COLUMNS) & columns and trigger == 0  # atrás: nada de lo nuevo queda
    assert "channel_accounts_whatsapp_ck" not in checks and "channel_accounts_platform_ck" in checks
    kept = "SELECT platform, status FROM channel_accounts WHERE id = %s"
    assert migrator.execute(kept, [world.account_a.pk]).fetchone() == ("SANDBOX", "CONNECTED")
    with as_migrator():
        call_command("migrate", "conversations", verbosity=0)
    columns, checks, trigger = schema(migrator)
    assert set(COLUMNS) <= columns and "channel_accounts_whatsapp_ck" in checks and trigger == 1


def test_going_back_refuses_while_a_whatsapp_account_exists(
    world: SimpleNamespace, migrator: DB
) -> None:
    """Quitar `WHATSAPP` del `CHECK` de plataforma con una cuenta real dentro es un error, no una
    pérdida en silencio: primero hay que borrar esas cuentas."""
    make(world.a)
    with as_migrator(), pytest.raises(Exception):  # noqa: B017, PT011 — error de la base
        call_command("migrate", "conversations", "0004", verbosity=0)
    with as_migrator():  # la migración es atómica: no dejó nada a medias
        call_command("migrate", "conversations", verbosity=0)
    assert set(COLUMNS) <= schema(migrator)[0]

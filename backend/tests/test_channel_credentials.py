"""F7-02b: el token de una cuenta de WhatsApp, cifrado en reposo (ADR-022, ADR-029 §2), y conectar,
rotar y desconectar la cuenta (`apps.conversations.accounts`). PostgreSQL real con `crm_app`, la
Graph API de Meta simulada (`tests/meta_sim`) servida por `core.http` con su política real."""

import logging
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from django.core.exceptions import ImproperlyConfigured, ObjectDoesNotExist
from django.core.management import call_command
from django.db import IntegrityError, ProgrammingError
from django.utils import timezone

from apps.access.selectors import AccessDenied
from apps.conversations import accounts, commands, graph
from apps.conversations.models import ChannelAccount, Credential
from apps.conversations.tasks import reseal_credentials
from core import sealed
from core.tenancy.scope import tenant_scope
from tests.meta_sim import transport
from tests.meta_sim.graph import TEST_TOKEN, Cut, FakeGraph
from tests.meta_sim.webhooks import PHONE_NUMBER_ID, WABA_ID
from tests.test_authorization import give
from tests.test_contacts_api import crm, real_stack
from tests.test_conversations_tables import refused
from tests.test_memberships import ctx
from tests.test_pipelines_tables import as_migrator
from tests.test_tenant_task_signature import broker
from tests.test_whatsapp_account import credential

__all__ = ["broker", "crm", "real_stack"]  # los fixtures, para pytest
pytestmark = pytest.mark.usefixtures("tenant_db")
DB = psycopg.Connection[Any]
NEW_TOKEN = "meta-sim-rotated-access-token-not-real-0000"  # noqa: S105 — ficticio
SECOND = "100000000000003"
OLD_KEY, NEW_KEY = bytes(range(32)), bytes(range(32, 64))


class Down(FakeGraph):
    """Meta no responde: la conexión se corta."""

    def handle(self, *args: Any) -> Any:
        raise Cut


@pytest.fixture
def meta(crm: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, settings: Any) -> FakeGraph:
    """El canal encendido, la Graph API falsa servida por `core.http` y `channels.manage` para
    ana (Owner de A) y para luis (de B)."""
    settings.META_ENABLED, settings.HTTP_ALLOWED_HOSTS = True, [transport.GRAPH_HOST]
    fake = FakeGraph(numbers={PHONE_NUMBER_ID: WABA_ID, SECOND: WABA_ID})
    transport.install(monkeypatch, fake)
    give(crm.b, crm.m_luis, {"channels.manage": None})
    return fake


def run(org: UUID, user: Any, command: Any, **fields: Any) -> Any:
    with tenant_scope(ctx(org, user)) as tenant:
        return command(tenant, **fields)


def connect(crm: SimpleNamespace, org: str = "a", **over: Any) -> tuple[ChannelAccount, Credential]:
    who: tuple[UUID, Any] = {"a": (crm.a, crm.ana), "b": (crm.b, crm.luis)}[org]
    wanted = {"name": "Ventas", "phone_number_id": PHONE_NUMBER_ID, "waba_id": WABA_ID}
    wanted |= {"access_token": TEST_TOKEN} | over
    made: tuple[ChannelAccount, Credential] = run(
        who[0], who[1], accounts.connect_whatsapp, **wanted
    )
    return made


def reload(org: UUID, model: Any, pk: UUID) -> Any:
    with tenant_scope(ctx(org)):
        return model.objects.get(pk=pk)


def everything(migrator: DB) -> str:
    """Lo que la base guarda de la conexión, salvo el cifrado: la auditoría y el outbox enteros y
    las filas de cuentas y credenciales."""
    queries = ("audit_logs", "outbox_events", "credentials", "channel_accounts")
    return "\n".join(
        str(migrator.execute(f"SELECT t::text FROM {table} t").fetchall())  # noqa: S608
        for table in queries
    )


def test_connecting_stores_a_pending_account_and_the_token_only_sealed(
    crm: SimpleNamespace, meta: FakeGraph, migrator: DB, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG):
        account, secret = connect(crm)
    assert (account.platform, account.status, account.status_detail) == (
        "WHATSAPP", "PENDING", "WEBHOOK_NOT_SEEN"  # nunca nace CONNECTED
    )  # fmt: skip
    assert (account.quality_rating, account.messaging_limit_tier) == ("GREEN", "TIER_1K")
    assert account.credential_id == secret.pk and account.last_healthcheck_at is not None
    assert (secret.last_four, secret.rotated_at, secret.revoked_at) == (TEST_TOKEN[-4:], None, None)
    assert secret.key_version == sealed.current() and secret.owner_id == account.pk
    box = bytes(reload(crm.a, Credential, secret.pk).ciphertext)
    assert TEST_TOKEN.encode() not in box and not sealed.stale(box)
    assert accounts.open_token(reload(crm.a, Credential, secret.pk)) == TEST_TOKEN
    assert meta.requests == [("GET", f"/v23.0/{WABA_ID}/phone_numbers")]
    seen = everything(migrator) + caplog.text
    assert TEST_TOKEN not in seen and secret.fingerprint in seen  # la huella no abre nada
    assert "channel_account.connected" in seen  # y sí quedó el hecho, en la auditoría y el outbox


def test_a_sealed_token_copied_to_another_account_or_purpose_does_not_open(
    crm: SimpleNamespace, meta: FakeGraph
) -> None:
    first, one = connect(crm)
    second, two = connect(crm, phone_number_id=SECOND)
    with tenant_scope(ctx(crm.a)):  # alguien con escritura copia el cifrado de una a la otra
        Credential.objects.filter(pk=two.pk).update(
            ciphertext=bytes(Credential.objects.get(pk=one.pk).ciphertext)
        )
        copied = Credential.objects.get(pk=two.pk)
    with pytest.raises(sealed.SealError):
        accounts.open_token(copied)  # atado a `channel_account:<id>`
    box = bytes(reload(crm.a, Credential, one.pk).ciphertext)
    with pytest.raises(sealed.SealError):
        sealed.unseal("mfa.totp", accounts.owner(first.pk), box)  # ni como secreto de otro uso
    assert second.pk != first.pk and accounts.owner(first.pk) != accounts.owner(second.pk)


def test_the_number_is_refused_with_one_error_whether_malformed_or_taken(
    crm: SimpleNamespace, meta: FakeGraph, migrator: DB
) -> None:
    connect(crm)
    before = everything(migrator).count("credentials")
    outcomes = []
    for org, number in (("a", "no-es-un-numero"), ("a", PHONE_NUMBER_ID), ("b", PHONE_NUMBER_ID)):
        with pytest.raises(accounts.BadNumber) as caught:
            connect(crm, org, phone_number_id=number)
        outcomes.append(
            (
                str(caught.value),
                caught.value.__cause__,
                caught.value.__suppress_context__ or caught.value.__context__ is None,
            )
        )
    assert len({o[0] for o in outcomes}) == 1 and outcomes[0][0] == accounts.NUMBER_REFUSED
    assert all(o[1] is None and o[2] for o in outcomes)  # sin el IntegrityError a la vista
    assert "channel_accounts_external_uq" not in outcomes[0][0] and "Key (" not in outcomes[0][0]
    assert everything(migrator).count("credentials") == before  # y la credencial no quedó


@pytest.mark.parametrize("why", ["token", "waba", "down", "off"])
def test_a_refused_connection_leaves_nothing_behind(
    crm: SimpleNamespace, meta: FakeGraph, monkeypatch: pytest.MonkeyPatch, settings: Any,
    migrator: DB, why: str,
) -> None:  # fmt: skip
    over: dict[str, Any] = {}
    expected: type[Exception] = graph.GraphRejected
    if why == "token":
        meta.token = "otro-token-de-la-api-falsa-0000"  # noqa: S105
    elif why == "waba":
        over["waba_id"] = "100000000000099"  # el número no es de ese WABA
    elif why == "down":
        transport.install(monkeypatch, Down())
        expected = graph.GraphUnavailable
    else:
        settings.META_ENABLED, expected = False, graph.ChannelNotEnabled
    with pytest.raises(expected):
        connect(crm, **over)
    for table in ("credentials", "channel_accounts WHERE platform = 'WHATSAPP'"):
        assert migrator.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,)  # noqa: S608


def test_without_channels_manage_nothing_is_read_and_meta_is_never_asked(
    crm: SimpleNamespace, meta: FakeGraph
) -> None:
    account, _ = connect(crm)
    meta.requests.clear()
    for user in (crm.vera, crm.sam, crm.bea):
        with pytest.raises(AccessDenied):
            run(crm.a, user, accounts.connect_whatsapp, name="x", phone_number_id=SECOND,
                waba_id=WABA_ID, access_token=NEW_TOKEN)  # fmt: skip
        with pytest.raises(AccessDenied):
            run(crm.a, user, accounts.rotate_token, channel_account_id=account.pk,
                access_token=NEW_TOKEN)  # fmt: skip
        with pytest.raises(AccessDenied):
            run(crm.a, user, accounts.disconnect, channel_account_id=account.pk)
    assert meta.requests == [] and reload(crm.a, ChannelAccount, account.pk).deleted_at is None


def test_rotating_replaces_the_token_and_wipes_the_old_one(
    crm: SimpleNamespace, meta: FakeGraph, migrator: DB
) -> None:
    account, old = connect(crm)
    with tenant_scope(ctx(crm.a)):
        ChannelAccount.objects.filter(pk=account.pk).update(
            status_detail="TOKEN_INVALID", status="ERROR"
        )
    meta.token = NEW_TOKEN
    rotated, new = run(crm.a, crm.ana, accounts.rotate_token, channel_account_id=account.pk,
                       access_token=NEW_TOKEN)  # fmt: skip
    assert rotated.credential_id == new.pk != old.pk and new.rotated_at is not None
    assert (rotated.status, rotated.status_detail, rotated.version) == ("ERROR", None, 2)
    gone = reload(crm.a, Credential, old.pk)
    assert (
        gone.ciphertext is None and gone.revoked_at is not None and gone.last_four == old.last_four
    )
    assert accounts.open_token(reload(crm.a, Credential, new.pk)) == NEW_TOKEN
    with pytest.raises(sealed.SealError):
        accounts.open_token(gone)  # el anterior ya no se puede abrir
    assert TEST_TOKEN not in everything(migrator) and NEW_TOKEN not in everything(migrator)
    meta.token = "tercer-token-de-la-api-falsa-000"  # noqa: S105  Meta rechaza el que llega
    with pytest.raises(graph.GraphRejected):
        run(crm.a, crm.ana, accounts.rotate_token, channel_account_id=account.pk,
            access_token=TEST_TOKEN)  # fmt: skip
    assert reload(crm.a, ChannelAccount, account.pk).credential_id == new.pk  # nada cambió


def test_an_account_that_is_not_a_live_whatsapp_account_of_the_organization_does_not_exist(
    crm: SimpleNamespace, meta: FakeGraph
) -> None:
    other, _ = connect(crm, "b", phone_number_id=SECOND)
    gone, _ = connect(crm)
    run(crm.a, crm.ana, accounts.disconnect, channel_account_id=gone.pk)
    sandbox = run(crm.a, crm.ana, commands.ensure_sandbox_account)
    meta.requests.clear()
    for target in (other.pk, gone.pk, sandbox.pk, uuid4()):  # ajena, desconectada, Sandbox, ninguna
        for command, extra in ((accounts.rotate_token, {"access_token": TEST_TOKEN}),
                               (accounts.disconnect, {})):  # fmt: skip
            with pytest.raises(ObjectDoesNotExist):
                run(crm.a, crm.ana, command, channel_account_id=target, **extra)
    assert meta.requests == []  # Meta no se entera de cuentas que no existen
    assert reload(crm.b, ChannelAccount, other.pk).deleted_at is None


def test_disconnecting_frees_the_number_and_reconnecting_is_a_new_account(
    crm: SimpleNamespace, meta: FakeGraph, migrator: DB
) -> None:
    account, secret = connect(crm)
    run(crm.a, crm.ana, accounts.disconnect, channel_account_id=account.pk)
    done = reload(crm.a, ChannelAccount, account.pk)
    assert (done.status, done.status_detail) == ("DISCONNECTED", "DISCONNECTED_BY_ADMIN")
    assert done.deleted_at is not None and done.deleted_by_user_id == crm.ana.pk
    assert reload(crm.a, Credential, secret.pk).ciphertext is None  # el token ya no está
    again, fresh = connect(crm, "b")  # el número se libera: otra organización lo registra
    assert again.pk != account.pk and fresh.pk != secret.pk and again.status == "PENDING"
    assert TEST_TOKEN not in everything(migrator)


def test_the_database_keeps_credentials_inside_their_organization_and_narrow(
    crm: SimpleNamespace, migrator: DB
) -> None:
    mine, theirs = credential(crm.a), credential(crm.b)
    with pytest.raises(IntegrityError, match="channel_accounts_credential_fk"):
        with tenant_scope(ctx(crm.a)):  # la credencial de B no es de A, aunque exista
            ChannelAccount.objects.create(
                platform="WHATSAPP", name="x", status="PENDING", credential_id=theirs,
                external_account_id=SECOND, external_parent_id=WABA_ID,
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


def test_reseal_moves_every_token_to_the_current_key_and_reports_what_it_cannot_open(
    crm: SimpleNamespace, meta: FakeGraph, settings: Any, caplog: pytest.LogCaptureFixture,
    migrator: DB,
) -> None:  # fmt: skip
    settings.SECRETS_KEKS = {1: OLD_KEY}
    one, mine = connect(crm)
    two, theirs = connect(crm, "b", phone_number_id=SECOND)
    assert reseal_credentials() == {"key": 1, "resealed": 0, "unreadable": 0, "busy": 0}
    settings.SECRETS_KEKS = {1: OLD_KEY, 7: NEW_KEY}  # rotación de la clave maestra
    migrator.execute("UPDATE credentials SET owner_id = %s WHERE id = %s", [uuid4(), theirs.pk])
    # la de B quedó ilegible: el valor ya no es de esa cuenta
    with caplog.at_level(logging.DEBUG):
        assert reseal_credentials() == {"key": 7, "resealed": 1, "unreadable": 1, "busy": 0}
    kept = reload(crm.a, Credential, mine.pk)
    assert bytes(kept.ciphertext)[:3] == b"\x01\x00\x07" and kept.key_version == 7
    assert accounts.open_token(kept) == TEST_TOKEN and kept.last_four == mine.last_four
    stuck = reload(crm.b, Credential, theirs.pk)
    assert stuck.key_version == 1 and bytes(stuck.ciphertext)[:3] == b"\x01\x00\x01"  # intacta
    assert reseal_credentials() == {"key": 7, "resealed": 0, "unreadable": 1, "busy": 0}
    settings.SECRETS_KEKS = {7: NEW_KEY}  # la anterior ya se puede retirar: lo vuelto a cifrar abre
    assert accounts.open_token(reload(crm.a, Credential, mine.pk)) == TEST_TOKEN
    assert TEST_TOKEN not in caplog.text + everything(migrator) and one.pk != two.pk


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
        accounts.reseal()


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

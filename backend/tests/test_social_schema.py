"""F7b-04a: el esquema de Instagram y Messenger (migración `0040`, ADR-035 §3–§5): las cuentas,
`contact_identities`, la identidad del hilo, la ingesta con plataforma y el enrutado por cuenta.
PostgreSQL real con `crm_app`; el migrador solo para mirar el catálogo."""

import time
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import psycopg
import pytest
from django.core.management import call_command
from django.db import DatabaseError, IntegrityError, OperationalError, connection
from django.utils import timezone
from psycopg.types.json import Jsonb

from apps.conversations import ingress
from apps.conversations.models import ChannelAccount, ContactIdentity, Conversation
from core.tenancy.scope import tenant_scope
from tests.conftest import migrator_conninfo
from tests.meta_sim.social import IG_ACCOUNT_ID, IGSID, PAGE_ID, PSID
from tests.test_conversations_tables import conversation, refused, world
from tests.test_memberships import ctx
from tests.test_pipelines_tables import as_migrator

__all__ = ["world"]  # el fixture, para pytest
pytestmark = pytest.mark.usefixtures("tenant_db")
DB = psycopg.Connection[Any]
OTHER = "100000000000999"
DEFINER = (
    "webhook_ingress_insert_social(text, text, text, text, text, jsonb)",
    "webhook_ingress_refuse_social(text, text, text, text, text, jsonb, text)",
    "resolve_social_account(text, text)",
)


def account(org: UUID, platform: str = "MESSENGER", external: str = PAGE_ID, **over: Any) -> Any:
    parent = over.pop("external_parent_id", PAGE_ID if platform == "INSTAGRAM" else None)
    with tenant_scope(ctx(org)):
        return ChannelAccount.objects.create(
            platform=platform, name=platform.title(), status="PENDING",
            external_account_id=external, external_parent_id=parent, **over,
        )  # fmt: skip


def identity(org: UUID, contact: Any, where: Any, external: str = PSID, **over: Any) -> Any:
    with tenant_scope(ctx(org)):
        return ContactIdentity.objects.create(
            contact_id=contact.pk, channel_account_id=where.pk, platform=where.platform,
            external_id=external, **over,
        )  # fmt: skip


def run(org: UUID | None, sql: str, params: list[Any]) -> list[Any]:
    if org is None:
        with connection.cursor() as cursor:
            cursor.execute(sql, params)
            return list(cursor.fetchall())
    with tenant_scope(ctx(org)), connection.cursor() as cursor:
        cursor.execute(sql, params)
        return cursor.fetchall() if cursor.description else []


# --- cuentas ----------------------------------------------------------------------------------


def test_each_platform_has_its_own_check_and_the_page_is_unique_across_organizations(
    world: SimpleNamespace,
) -> None:
    page = account(world.a)
    ig = account(world.a, "INSTAGRAM", IG_ACCOUNT_ID)  # la misma página como padre: otra cuenta
    assert (page.external_parent_id, ig.external_parent_id) == (None, PAGE_ID)
    bad: dict[str, list[dict[str, Any]]] = {
        "channel_accounts_messenger_ck": [
            {"external_account_id": "no-es-un-id"}, {"external_parent_id": PAGE_ID},
            {"quality_rating": "GREEN"}, {"messaging_limit_tier": "TIER_1K"},
        ],
        "channel_accounts_instagram_ck": [
            {"platform": "INSTAGRAM", "external_parent_id": None},
            {"platform": "INSTAGRAM", "external_parent_id": "pagina"},
            {"platform": "INSTAGRAM", "external_parent_id": PAGE_ID, "quality_rating": "GREEN"},
        ],
    }  # fmt: skip
    for constraint, rows in bad.items():
        for row in rows:
            fields = {"platform": "MESSENGER", "external_account_id": OTHER} | row
            refused(world.a, constraint, lambda f=fields: ChannelAccount.objects.create(
                name="x", status="PENDING", **f))  # fmt: skip
    refused(world.b, "channel_accounts_external_uq", lambda: ChannelAccount.objects.create(
        platform="MESSENGER", name="x", status="PENDING", external_account_id=PAGE_ID))  # fmt: skip


# --- identidades ------------------------------------------------------------------------------


def test_an_identity_is_unique_per_account_and_never_crosses_organizations(
    world: SimpleNamespace,
) -> None:
    page, ig = account(world.a), account(world.a, "INSTAGRAM", IG_ACCOUNT_ID)
    mine = identity(world.a, world.contact_a, page)
    identity(world.a, world.contact_a, ig, PSID)  # el mismo id en otra cuenta: otra identidad
    refused(world.a, "contact_identities_external_uq", lambda: ContactIdentity.objects.create(
        contact_id=world.contact_a.pk, channel_account_id=page.pk, platform="MESSENGER",
        external_id=PSID))  # fmt: skip
    for kind, external in (("INSTAGRAM", IGSID), ("MESSENGER", "con espacio")):
        name = (
            "account_fk" if kind == "INSTAGRAM" else "external_ck"
        )  # la plataforma, la de la cuenta
        with tenant_scope(ctx(world.a)), pytest.raises(DatabaseError, match=name):
            ContactIdentity.objects.create(
                contact_id=world.contact_a.pk, channel_account_id=page.pk, platform=kind,
                external_id=external,
            )  # fmt: skip
    with tenant_scope(ctx(world.a)), pytest.raises(DatabaseError, match="platform_ck"):
        ContactIdentity.objects.create(  # el Sandbox no tiene identidades de este tipo
            contact_id=world.contact_a.pk, channel_account_id=world.account_a.pk,
            platform="SANDBOX", external_id=PSID,
        )  # fmt: skip
    theirs = account(world.b, external=OTHER)
    with tenant_scope(ctx(world.b)), pytest.raises(DatabaseError, match="contact_fk"):
        ContactIdentity.objects.create(  # el contacto de A, desde B
            contact_id=world.contact_a.pk, channel_account_id=theirs.pk, platform="MESSENGER",
            external_id=PSID,
        )  # fmt: skip
    assert run(world.b, "SELECT id FROM contact_identities", []) == []  # RLS, no el manager
    assert len(run(world.a, "SELECT id FROM contact_identities", [])) == 2
    for change in (
        "UPDATE contact_identities SET external_id = 'x'",
        "DELETE FROM contact_identities",
    ):
        with pytest.raises(DatabaseError, match="permission denied"):  # inmutable para la app
            run(world.a, change + " WHERE id = %s", [mine.pk])


def test_a_social_thread_carries_the_identity_of_its_own_contact_and_account(
    world: SimpleNamespace,
) -> None:
    page = account(world.a)
    with tenant_scope(ctx(world.a)):
        other = type(world.contact_a).objects.create(display_name="Otro")
    mine, theirs = identity(world.a, world.contact_a, page), identity(world.a, other, page, "201")
    with pytest.raises(IntegrityError, match="identity_required"):
        conversation(world.a, world.contact_a, page)
    with pytest.raises(IntegrityError, match="contact_identity_fk"):
        conversation(world.a, world.contact_a, page, contact_identity_id=theirs.pk)
    thread = conversation(world.a, world.contact_a, page, contact_identity_id=mine.pk)
    with tenant_scope(ctx(world.a)), pytest.raises(IntegrityError, match="identity_required"):
        Conversation.objects.filter(pk=thread.pk).update(contact_identity_id=None)
    with pytest.raises(IntegrityError, match="contact_identity_fk"):  # ni en un hilo de otra cuenta
        conversation(world.a, world.contact_a, world.account_a, "CONV-00002",
                     contact_identity_id=mine.pk)  # fmt: skip
    assert conversation(world.a, world.contact_a, world.account_a, "CONV-00003").pk  # Sandbox: no


# --- ingesta y enrutado -----------------------------------------------------------------------


def test_the_new_functions_are_definer_owned_by_the_migrator_and_only_for_the_app(
    migrator: DB,
) -> None:
    fixed = ["search_path=pg_catalog, public, pg_temp"]
    for function in DEFINER:
        found = migrator.execute(
            "SELECT p.prosecdef, pg_get_userbyid(p.proowner), p.proconfig,"
            " has_function_privilege('public', p.oid, 'EXECUTE'),"
            " has_function_privilege('crm_app', p.oid, 'EXECUTE')"
            " FROM pg_proc p WHERE p.oid = %s::regprocedure",
            [f"public.{function}"],
        ).fetchone()
        assert found == (True, "crm_migrator", fixed, False, True), function
    trigger = "SELECT prosecdef FROM pg_proc WHERE oid = %s::regprocedure"
    assert migrator.execute(trigger, ["public.conversations_identity_required()"]).fetchone() == (
        False,
    )  # corre con la RLS de quien escribe


def test_the_resolver_gives_only_a_live_social_account_and_nothing_else(
    world: SimpleNamespace,
) -> None:
    page, ig = account(world.a), account(world.a, "INSTAGRAM", IG_ACCOUNT_ID)
    theirs = account(world.b, external=OTHER)
    assert ingress.resolve_social("MESSENGER", PAGE_ID) == (world.a, page.pk)
    assert ingress.resolve_social("INSTAGRAM", IG_ACCOUNT_ID) == (world.a, ig.pk)
    assert ingress.resolve_social("MESSENGER", OTHER) == (world.b, theirs.pk)  # de la cuenta
    with tenant_scope(ctx(world.a)):  # un número de WhatsApp con ese mismo id: no es social
        ChannelAccount.objects.create(
            platform="WHATSAPP", name="wa", status="PENDING", external_account_id=IG_ACCOUNT_ID,
            external_parent_id=PAGE_ID,
        )  # fmt: skip
    for platform, external in (("INSTAGRAM", PAGE_ID), ("MESSENGER", IG_ACCOUNT_ID)):
        assert ingress.resolve_social(platform, external) is None, (platform, external)
    # Argumentos que no son de Meta: un error, nunca una búsqueda.
    bad: list[tuple[Any, Any]] = [
        ("WHATSAPP", IG_ACCOUNT_ID), ("SANDBOX", f"fixture-{world.a}"), ("messenger", PAGE_ID),
        ("MESSENGER", "%"), ("MESSENGER", "' OR 1=1 --"), ("MESSENGER", None), (None, PAGE_ID),
        ("MESSENGER", "1234"),
    ]  # fmt: skip
    for kind, external_id in bad:
        with pytest.raises(DatabaseError, match="no válidos"):
            ingress.resolve_social(kind, external_id)
        connection.close()
    rows = run(None, "SELECT * FROM public.resolve_social_account('MESSENGER', %s)", [PAGE_ID])
    assert rows == [(world.a, page.pk)]  # una fila, y solo los dos ids
    with tenant_scope(ctx(world.a)):
        ChannelAccount.objects.filter(pk=page.pk).update(deleted_at=timezone.now())
    assert ingress.resolve_social("MESSENGER", PAGE_ID) is None  # solo las vivas


def insert(key: str, platform: str = "MESSENGER", routing: str = PAGE_ID, **over: Any) -> Any:
    args = {"provider": "meta", "kind": "message", "payload": {"mid": key}} | over
    sql = "SELECT public.webhook_ingress_insert_social(%s, %s, %s, %s, %s, %s)"
    params = [args["provider"], key, args["kind"], platform, routing, Jsonb(args["payload"])]
    return run(None, sql, params)[0][0]


def test_a_social_item_is_stored_once_with_its_platform_and_routing_id(migrator: DB) -> None:
    assert insert("fb:msg:m_1") is not None and insert("fb:msg:m_1") is None
    assert insert("ig:msg:m_1", "INSTAGRAM", IG_ACCOUNT_ID) is not None
    row = migrator.execute(
        "SELECT platform, routing_id, waba_id, phone_number_id, status FROM webhook_ingress"
        " WHERE event_key = 'fb:msg:m_1'"
    ).fetchone()
    assert row == ("MESSENGER", PAGE_ID, None, None, "RECEIVED")
    sql = "SELECT public.webhook_ingress_refuse_social(%s, %s, %s, %s, %s, %s, %s)"
    run(None, sql, ["meta", "fb:msg:m_2", "message", "MESSENGER", PAGE_ID, Jsonb({}), "REFUSED"])
    dead = migrator.execute(
        "SELECT w.status, w.payload, f.reason FROM webhook_ingress w"
        " JOIN webhook_failures f ON f.ingress_id = w.id WHERE w.event_key = 'fb:msg:m_2'"
    ).fetchone()
    assert dead == ("DEAD", None, "REFUSED")


@pytest.mark.parametrize(
    "bad",
    [
        {"key": "wa:msg:1"}, {"key": "ig:msg:1"}, {"platform": "INSTAGRAM", "key": "fb:msg:1"},
        {"platform": "WHATSAPP"}, {"platform": None}, {"routing": "pagina"}, {"routing": None},
        {"kind": "otro"}, {"provider": "otro"}, {"payload": [1]}, {"key": "fb:con espacio"},
        {"payload": {"x": "a" * 8_388_609}},
    ],
)  # fmt: skip
def test_the_social_insert_refuses_what_is_not_a_validated_item(bad: dict[str, Any]) -> None:
    args = {"key": "fb:msg:1", "platform": "MESSENGER", "routing": PAGE_ID} | bad
    with pytest.raises(DatabaseError, match="no válidos"):
        insert(args.pop("key"), args.pop("platform"), args.pop("routing"), **args)
    connection.close()


def test_a_social_item_that_does_not_name_its_account_is_closed_untouched(
    world: SimpleNamespace, migrator: DB
) -> None:
    account(world.a)
    insert("fb:msg:m_9", payload={"mid": "m_9", "text": "Hola"})
    assert ingress.process() == {"IGNORED": 1}
    row = migrator.execute(
        "SELECT ignored_reason, payload, routed_organization_id FROM webhook_ingress"
    ).fetchone()
    assert row == ("ACCOUNT_MISMATCH", None, None)  # sin enrutar ni escribir nada del tenant
    assert run(world.a, "SELECT count(*) FROM messages", []) == [(0,)]


def test_going_back_keeps_identities_waits_at_most_the_lock_timeout_and_comes_back(
    world: SimpleNamespace, migrator: DB
) -> None:
    names = (
        "SELECT count(*) FROM pg_proc WHERE proname LIKE %s OR proname = 'resolve_social_account'"
    )
    table = "SELECT to_regclass('public.contact_identities') IS NOT NULL"
    identity(world.a, world.contact_a, account(world.a))
    with as_migrator(), pytest.raises(RuntimeError, match="1 contact identities exist"):
        call_command("migrate", "conversations", "0039", verbosity=0)
    assert migrator.execute("SELECT count(*) FROM contact_identities").fetchone() == (1,)
    migrator.execute("DELETE FROM contact_identities")
    migrator.execute("DELETE FROM channel_accounts WHERE platform = 'MESSENGER'")
    for target in ("0039", None):  # una transacción larga en el Inbox: falla en vez de encolarlo
        with psycopg.connect(migrator_conninfo()) as holder:
            holder.execute("LOCK TABLE conversations IN ACCESS EXCLUSIVE MODE")
            started = time.monotonic()
            with as_migrator(), pytest.raises(OperationalError, match="lock timeout"):
                call_command("migrate", "conversations", *([target] if target else []), verbosity=0)
            assert time.monotonic() - started < 30
        with as_migrator():
            call_command("migrate", "conversations", *([target] if target else []), verbosity=0)
        if target:
            assert migrator.execute(names, ["%_social"]).fetchone() == (0,)
            assert migrator.execute(table).fetchone() == (False,)
    assert migrator.execute(names, ["%_social"]).fetchone() == (3,)
    assert migrator.execute(table).fetchone() == (True,)

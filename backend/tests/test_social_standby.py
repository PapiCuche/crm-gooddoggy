"""F7b-06b: el estado `standby` de Instagram y Messenger se guarda (ADR-035 §4, §10). Un entrante en
`standby` fija `conversations.standby_since`; uno normal, más reciente según el canal, la limpia;
nunca hay respuesta automática. La cuenta dice si tiene conversaciones en `standby` y la
conversación, desde cuándo. PostgreSQL real con `crm_app`; el simulador firma los webhooks."""

import datetime as dt
import importlib
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest
from django.test import Client
from django.utils import timezone

from apps.conversations.models import Conversation
from apps.organizations.models import OrganizationMembership
from core.tenancy.scope import tenant_scope
from tests import test_conversations_tables as tables
from tests.meta_sim.page_graph import FakePageGraph
from tests.test_authorization import give
from tests.test_channel_accounts_api import signed
from tests.test_conversation_commands import world, writer
from tests.test_memberships import ctx
from tests.test_meta_webhook import channel
from tests.test_self_context import real_stack
from tests.test_social_consent import PLATFORMS, event, say, seen
from tests.test_social_outbound import CHANNELS, graph, talk
from tests.test_social_schema import account, identity
from tests.test_whatsapp_outbound import redis

__all__ = [
    "channel",
    "graph",
    "real_stack",
    "redis",
    "world",
    "writer",
]  # las fixtures, para pytest
pytestmark = pytest.mark.usefixtures("tenant_db", "graph")
API = "/api/v1/o/{}/"
STANDBY = "standby"
MIGRATION = importlib.import_module("apps.conversations.migrations.0047_conversation_standby_since")


def standby_since(world: SimpleNamespace, convo: Conversation, org: Any = None) -> Any:
    with tenant_scope(ctx(org or world.a)):
        return Conversation.objects.get(pk=convo.pk).standby_since


def reader(world: SimpleNamespace, org: Any = None, user: Any = None) -> Client:
    """Quien gestiona los canales y ve las conversaciones de su organización."""
    org, user = org or world.a, user or world.ana
    with tenant_scope(ctx(org)):
        mine = OrganizationMembership.objects.get(user_id=user.pk).pk
    give(org, mine, {"channels.manage": None, "conversations.view": "ORGANIZATION"})
    return signed(user)


def flags(client: Client, slug: str = "org-a") -> dict[str, bool]:
    rows = client.get(API.format(slug) + "channels/").json()["results"]
    return {row["id"]: row["has_standby_conversations"] for row in rows}


@pytest.mark.parametrize("platform", PLATFORMS)
def test_a_standby_inbound_sets_the_state_and_is_never_answered(
    world: SimpleNamespace, graph: FakePageGraph, platform: str
) -> None:
    convo = talk(world, platform)
    assert standby_since(world, convo) is None
    said = event(platform, "BAJA", "m_SIMIN000001")  # una palabra que, fuera de `standby`, responde
    assert say(platform, said, field=STANDBY) == {"PROCESSED": 1}
    since = standby_since(world, convo)
    assert since is not None
    assert abs(since.timestamp() * 1000 - said["timestamp"]) < 1000  # la hora del canal
    found = seen(world)
    assert found.inbound == ["BAJA"]  # el cliente escribió de verdad: se registra
    assert found.replies == [] and graph.sent == []  # nada responde solo (ADR-035 §4)
    say(platform, event(platform, "Hola", "m_SIMIN000002", ago=dt.timedelta(0)), field=STANDBY)
    assert standby_since(world, convo) == since  # sigue en `standby` desde la primera


@pytest.mark.parametrize("platform", PLATFORMS)
def test_a_newer_normal_inbound_clears_it_and_an_older_one_does_not_change_it(
    world: SimpleNamespace, platform: str
) -> None:
    convo = talk(world, platform)
    minutes = dt.timedelta(minutes=1)
    say(platform, event(platform, "Hola", "m_SIMIN000001", ago=3 * minutes), field=STANDBY)
    assert standby_since(world, convo) is not None
    say(platform, event(platform, "Otra vez", "m_SIMIN000002", ago=minutes))
    assert standby_since(world, convo) is None  # la app vuelve a controlar el hilo
    say(platform, event(platform, "Tarde", "m_SIMIN000003", ago=2 * minutes), field=STANDBY)
    assert standby_since(world, convo) is None  # Meta lo reintentó tarde: no manda
    say(platform, event(platform, "Ahora", "m_SIMIN000004", ago=dt.timedelta(0)), field=STANDBY)
    since = standby_since(world, convo)
    assert since is not None
    say(platform, event(platform, "Antes", "m_SIMIN000005", ago=2 * minutes))
    assert standby_since(world, convo) == since  # uno normal más antiguo tampoco la limpia


def test_a_standby_inbound_that_reopens_a_resolved_thread_also_keeps_the_state(
    world: SimpleNamespace,
) -> None:
    convo = talk(world, "INSTAGRAM")
    with tenant_scope(ctx(world.a)):
        Conversation.objects.filter(pk=convo.pk).update(
            status="RESOLVED", resolved_at=timezone.now()
        )
    say("INSTAGRAM", event("INSTAGRAM", "Hola", "m_SIMIN000001"), field=STANDBY)
    with tenant_scope(ctx(world.a)):
        found = Conversation.objects.get(pk=convo.pk)
    assert (found.status, found.standby_since is not None) == ("OPEN", True)


@pytest.mark.parametrize("platform", PLATFORMS)
def test_the_account_and_the_conversation_expose_the_state(
    world: SimpleNamespace, platform: str
) -> None:
    convo = talk(world, platform)
    other = next(p for p in PLATFORMS if p != platform)
    calm = talk(world, other)  # otra cuenta de la misma organización, sin `standby`
    client = reader(world)
    path = API.format("org-a") + f"conversations/{convo.pk}/"
    assert client.get(path).json()["standby_since"] is None
    assert flags(client) == {
        str(world.account_a.pk): False, str(convo.channel_account_id): False,
        str(calm.channel_account_id): False,
    }  # fmt: skip
    say(platform, event(platform, "Hola", "m_SIMIN000001"), field=STANDBY)
    since = standby_since(world, convo).isoformat().replace("+00:00", "Z")
    assert client.get(path).json()["standby_since"] == since
    listed = {
        r["id"]: r for r in client.get(API.format("org-a") + "conversations/").json()["results"]
    }
    assert listed[str(convo.pk)]["standby_since"] == since
    assert listed[str(calm.pk)]["standby_since"] is None
    assert flags(client) == {
        str(world.account_a.pk): False, str(convo.channel_account_id): True,
        str(calm.channel_account_id): False,
    }  # fmt: skip
    with tenant_scope(ctx(world.a)):  # una conversación cerrada ya no cuenta para la cuenta
        Conversation.objects.filter(pk=convo.pk).update(status="CLOSED", closed_at=timezone.now())
    assert flags(client)[str(convo.channel_account_id)] is False


@pytest.mark.parametrize("platform", PLATFORMS)
def test_another_organization_never_sees_nor_gets_the_state(
    world: SimpleNamespace, platform: str
) -> None:
    convo = talk(world, platform)  # la cuenta de A, con el id real del simulador
    external, customer = CHANNELS[platform]
    theirs = account(world.b, platform, f"9{external[1:]}")  # B: otra cuenta, el mismo cliente
    who = identity(world.b, world.contact_b, theirs, customer)
    their_convo = tables.conversation(
        world.b, world.contact_b, theirs, "CONV-09999", contact_identity_id=who.pk
    )
    say(platform, event(platform, "Hola", "m_SIMIN000001"), field=STANDBY)
    assert standby_since(world, convo) is not None
    assert standby_since(world, their_convo, world.b) is None
    client = reader(world, world.b, world.luis)
    assert flags(client, "org-b") == {
        str(world.account_b.pk): False, str(theirs.pk): False,
    }  # fmt: skip
    assert client.get(API.format("org-b") + f"conversations/{convo.pk}/").status_code == 404
    with tenant_scope(ctx(world.b)):  # bajo la RLS de B, la de A no existe
        assert not Conversation.objects.filter(standby_since__isnull=False).exists()


def test_the_runtime_role_writes_the_column_and_going_back_refuses_while_a_thread_is_in_standby(
    world: SimpleNamespace, migrator: psycopg.Connection[Any]
) -> None:
    acl = migrator.execute(
        "SELECT attacl::text FROM pg_attribute WHERE attname = 'standby_since' "
        "AND attrelid = 'public.conversations'::regclass"
    ).fetchone()
    assert acl is not None and "crm_app=w/" in acl[0]  # la concesión de columna, explícita
    editor = SimpleNamespace(connection=migrator)
    MIGRATION.refuse_standby(None, editor)  # sin hilos en `standby`, vuelve atrás
    talk(world, "MESSENGER")
    say("MESSENGER", event("MESSENGER", "Hola", "m_SIMIN000001"), field=STANDBY)
    with pytest.raises(RuntimeError, match="1 conversations are in standby"):
        MIGRATION.refuse_standby(None, editor)

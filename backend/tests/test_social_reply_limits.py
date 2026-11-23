"""F7b-16: lo que Instagram o Messenger nunca aceptarían se rechaza al encolar, no al despachar.
El texto por encima del tope del canal (`limits`, ADR-035 §7: Instagram 1000 bytes UTF-8,
Messenger 2000 caracteres) y cualquier adjunto: sin fila, sin auditoría, sin evento y con el
mismo 404 de siempre fuera del alcance. WhatsApp y Sandbox, sin cambios. PostgreSQL real con
`crm_app`; el conteo, con el rol dueño."""

from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from django.core.exceptions import ObjectDoesNotExist
from django.test import Client

from apps.access.selectors import AccessDenied
from apps.conversations import limits, meta_page, outbound_media
from apps.conversations.commands import MediaNotSupported, TooLongForChannel
from apps.conversations.models import Conversation
from apps.organizations.models import OrganizationMembership
from core import storage
from core.tenancy.scope import tenant_scope
from tests import test_conversations_tables as tables
from tests.factories import make_user
from tests.test_authorization import give
from tests.test_conversation_commands import outbound, run, send, world, writer
from tests.test_conversations_api import BASE
from tests.test_conversations_send_api import counts, post
from tests.test_media_upload_api import blobs, reads, upload
from tests.test_memberships import ctx, join
from tests.test_self_context import NOT_FOUND, real_stack, reply, signed
from tests.test_social_outbound import talk

__all__ = ["blobs", "reads", "real_stack", "world", "writer"]  # los fixtures, para pytest
pytestmark = pytest.mark.usefixtures("tenant_db")
DB = psycopg.Connection[Any]
SOCIAL = ("INSTAGRAM", "MESSENGER")
# (plataforma, el texto más largo que cabe, uno que ya no): bytes UTF-8 en Instagram (tildes,
# emojis con su tono y letras con su acento combinado cuentan más de uno), caracteres en Messenger.
EDGES = [
    ("INSTAGRAM", "a" * 1000, "a" * 1001),
    ("INSTAGRAM", "ñ" * 500, "ñ" * 500 + "a"),  # 2 bytes cada una
    ("INSTAGRAM", "👍🏽" * 125, "👍🏽" * 125 + "a"),  # 8 bytes cada uno
    ("INSTAGRAM", "é" * 333 + "a", "é" * 333 + "ab"),  # 3 bytes cada una
    ("MESSENGER", "ñ" * 2000, "ñ" * 2001),  # 4000 bytes, pero 2000 caracteres
    ("MESSENGER", "👍🏽" * 1000, "👍🏽" * 1000 + "a"),  # 2 puntos de código cada uno
]


def jpeg(org: Any) -> outbound_media.Prepared:
    """Un archivo ya preparado para esta organización (lo que `prepare` entrega al comando)."""
    file_id = uuid4()
    key = storage.tenant_key(org, outbound_media.PURPOSE, file_id)
    return outbound_media.Prepared(file_id, key, "IMAGE", "image/jpeg", 44, "0" * 64, "foto.jpg")


def nothing_written(migrator: DB) -> bool:
    files = migrator.execute("SELECT count(*) FROM files").fetchone()
    return counts(migrator) == (0, 0, 0) and files == (0,)


@pytest.mark.parametrize(("platform", "fits", "too_long"), EDGES)
def test_the_longest_text_that_fits_is_queued_and_one_more_unit_is_refused_before_any_row(
    world: SimpleNamespace, migrator: DB, platform: str, fits: str, too_long: str
) -> None:
    convo = talk(world, platform)
    with pytest.raises(TooLongForChannel):
        outbound(world, convo, body=too_long)
    assert nothing_written(migrator)  # sin mensaje, sin evento, sin auditoría
    with tenant_scope(ctx(world.a)):
        kept = Conversation.objects.get(pk=convo.pk)
    assert (kept.status, kept.last_message_preview) == (convo.status, convo.last_message_preview)
    message, created = outbound(world, convo, body=fits)
    assert created and message.delivery_status == "QUEUED" and message.body == fits
    assert limits.fits(platform, fits) and not limits.fits(platform, too_long)


def test_whatsapp_and_sandbox_keep_the_api_limit_and_their_media(
    world: SimpleNamespace, migrator: DB
) -> None:
    convo = tables.conversation(world.a, world.contact_a, world.account_a)  # Sandbox
    assert outbound(world, convo, body="ñ" * 4096)[1]  # 8192 bytes: aquí cuentan caracteres
    assert outbound(world, convo, body="Mira", media=jpeg(world.a))[0].message_type == "IMAGE"
    assert limits.text_limit("WHATSAPP") == limits.text_limit("SANDBOX") == ("CHARACTERS", 4096)
    assert limits.takes_media("WHATSAPP") and limits.takes_media("SANDBOX")


@pytest.mark.parametrize("platform", SOCIAL)
def test_a_reply_with_media_to_instagram_or_messenger_is_refused_without_a_file_row(
    world: SimpleNamespace, migrator: DB, platform: str
) -> None:
    convo = talk(world, platform)
    with pytest.raises(MediaNotSupported):
        outbound(world, convo, body="Mira", media=jpeg(world.a))
    with pytest.raises(MediaNotSupported):  # el adjunto se dice antes que un pie demasiado largo
        outbound(world, convo, body="a" * 1001, media=jpeg(world.a))
    assert nothing_written(migrator)


def test_permission_and_scope_decide_before_the_channel_limit(
    world: SimpleNamespace, migrator: DB
) -> None:
    convo, too_long = talk(world, "INSTAGRAM"), "a" * 1001
    with tenant_scope(ctx(world.a)):  # de `ana`: quien solo envía lo suyo no la alcanza
        Conversation.objects.filter(pk=convo.pk).update(assigned_user_id=world.ana.pk)
    with tenant_scope(ctx(world.b)):
        luis = OrganizationMembership.objects.get(user_id=world.luis.pk).pk
    give(world.b, luis, {"messages.send": "ORGANIZATION"})
    fields: dict[str, Any] = {
        "conversation_id": convo.pk,
        "client_message_id": uuid4(),
        "body": too_long,
    }
    with pytest.raises(ObjectDoesNotExist):  # de otra organización: no existe, nada del canal
        run(world, send, org="b", **fields)
    own = make_user(email="propia@example.com")
    give(world.a, join(world.a, own).pk, {"messages.send": "OWN"})
    with pytest.raises(ObjectDoesNotExist):  # fuera de su alcance: el mismo «no existe»
        run(world, send, who=own, **fields)
    nobody = make_user(email="sinpermiso@example.com")
    join(world.a, nobody)
    with pytest.raises(AccessDenied):
        run(world, send, who=nobody, **fields)
    with pytest.raises(ObjectDoesNotExist):  # una conversación que no existe, igual
        run(world, send, **(fields | {"conversation_id": uuid4()}))
    assert nothing_written(migrator)


def test_a_replay_of_a_message_already_stored_is_returned_even_if_the_limit_shrank(
    world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    convo, key = talk(world, "INSTAGRAM"), uuid4()
    first, _ = outbound(world, convo, key, body="a" * 1000)  # p. ej., encolado por otra versión
    monkeypatch.setitem(limits.TEXT_LIMITS, "INSTAGRAM", (limits.BYTES, 10))
    again, created = outbound(world, convo, key, body="a" * 1000)
    assert (again.pk, created) == (first.pk, False)
    with pytest.raises(TooLongForChannel):  # el mismo texto con otro id sí es nuevo
        outbound(world, convo, body="a" * 1000)


def test_the_adapter_and_the_command_share_one_table() -> None:
    assert vars(meta_page)["fits"] is limits.fits  # el adaptador no tiene otra
    assert limits.TEXT_LIMITS == {"MESSENGER": ("CHARACTERS", 2000), "INSTAGRAM": ("BYTES", 1000)}
    assert limits.NO_MEDIA == {"MESSENGER", "INSTAGRAM"}


@pytest.fixture
def agent(world: SimpleNamespace) -> Client:
    """`ana` con lo que pide la ruta (`writer` le dio `messages.send`) y viendo al contacto."""
    with tenant_scope(ctx(world.a)):
        mine = OrganizationMembership.objects.get(user_id=world.ana.pk).pk
    give(world.a, mine, {"conversations.view": "ORGANIZATION", "contacts.view": "ORGANIZATION"})
    return signed(world.ana)


def test_the_api_answers_400_with_a_fixed_message_and_keeps_nothing(
    world: SimpleNamespace, agent: Client, migrator: DB
) -> None:
    convo = talk(world, "INSTAGRAM").pk
    refused = post(agent, convo, body="Señor " + "ñ" * 497)  # 7 + 994 = 1001 bytes
    assert (refused.status_code, refused.json()["code"]) == (400, "TEXT_TOO_LONG_FOR_CHANNEL")
    assert "Señor" not in refused.content.decode() and "1001" not in refused.content.decode()
    assert counts(migrator) == (0, 0, 0)
    assert post(agent, convo, body="Señor " + "ñ" * 496 + "a").status_code == 202  # 1000
    assert reply(post(signed(world.luis), convo, body="a" * 1001)) == NOT_FOUND  # de B


@pytest.mark.parametrize("platform", SOCIAL)
def test_the_media_route_answers_409_before_reading_or_storing_the_file(
    world: SimpleNamespace, agent: Client, migrator: DB, reads: list[int], blobs: Any,
    platform: str,
) -> None:  # fmt: skip
    refused = upload(agent, talk(world, platform).pk)
    assert (refused.status_code, refused.json()["code"]) == (409, "MEDIA_NOT_SUPPORTED_BY_CHANNEL")
    assert reads == [] and blobs.objects == {} and nothing_written(migrator)


@pytest.mark.parametrize(
    ("platform", "shown"),
    [
        ("INSTAGRAM", (1000, "BYTES", False)),
        ("MESSENGER", (2000, "CHARACTERS", False)),
        ("SANDBOX", (4096, "CHARACTERS", True)),
    ],
)
def test_the_conversation_tells_the_composer_its_channel_limits(
    world: SimpleNamespace, agent: Client, platform: str, shown: tuple[Any, ...]
) -> None:
    convo = (
        tables.conversation(world.a, world.contact_a, world.account_a)
        if platform == "SANDBOX"
        else talk(world, platform)
    )
    channel = agent.get(f"{BASE}{convo.pk}/").json()["channel"]
    assert (channel["text_max"], channel["text_unit"], channel["accepts_media"]) == shown
    listed = {row["id"]: row for row in agent.get(BASE).json()["results"]}
    assert listed[str(convo.pk)]["channel"] == channel

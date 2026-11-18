"""F7b-04d: la clave de deduplicación de Instagram y Messenger es por cuenta (ADR-035 §4, enmendado
el 2026-11-17 por decisión del propietario, #779). Dos cuentas conectadas que se escriben reciben el
mismo `mid` (eco en una, entrante en la otra) y las dos lo procesan; un reintento de Meta con la
misma clave sigue siendo un solo ítem, y uno de un evento guardado con la clave anterior (global)
crea un ítem nuevo que no duplica el mensaje ni retrocede su estado. PostgreSQL real con `crm_app`;
los webhooks firmados por la ruta pública con el simulador."""

import dataclasses
import itertools
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest
from django.utils import timezone

from apps.contacts.models import ContactConsent
from apps.conversations import ingress, meta_webhook
from apps.conversations.models import Message
from core.tenancy.scope import tenant_scope
from tests import test_social_outbound
from tests.meta_sim import social
from tests.meta_sim.page_graph import FakePageGraph
from tests.meta_sim.social import APP_ID, INSTAGRAM, MESSENGER, OTHER_ACCOUNT_ID, OTHER_APP_ID
from tests.test_conversation_commands import world, writer
from tests.test_conversations_dispatch import state
from tests.test_memberships import ctx
from tests.test_meta_webhook import channel, post
from tests.test_social_outbound import graph, send, talk
from tests.test_social_schema import account
from tests.test_webhook_ingress import fresh_heap, last_everywhere
from tests.test_whatsapp_outbound import redis

__all__ = ["channel", "graph", "redis", "world", "writer"]  # los fixtures, para pytest
pytestmark = pytest.mark.usefixtures("tenant_db")
DB = psycopg.Connection[Any]
SIM = {"MESSENGER": MESSENGER, "INSTAGRAM": INSTAGRAM}
PREFIX = {"page": "fb", "instagram": "ig"}
NEW_CUSTOMER = "100000000000203"  # otro cliente de la cuenta, sin identidad todavía
TRAIL = ("message.received", "contact_identity.created", "message.delivery_changed")
LONGEST_ACCOUNT = "9" * 30  # `META_ID` al máximo
LONGEST_MIDS = ["%" * 99 + c for c in "%#&+"]  # cada uno, 300 caracteres ya codificados


@pytest.fixture(autouse=True)
def our_app(settings: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    settings.META_APP_ID = APP_ID
    # `talk` numera sus hilos a mano: lejos de los que la organización numera al recibir
    monkeypatch.setattr(test_social_outbound, "NUMBERS", itertools.count(90001))


def keys(net: social.Channel, *events: dict[str, Any], entry: str | None = None) -> list[str]:
    body = social.signed(net, *events, entry_id=entry).body
    return [i.key for i in meta_webhook.parse(body).items]


def catalogue(net: social.Channel, here: str) -> list[dict[str, Any]]:
    """Un evento de cada clave, todos de la cuenta `here`: entrante, borrado, eco, leído (y en
    Messenger, entrega) y uno que no se entiende."""
    found = [
        social.text(net, mid="m_SIMIN000001", account=here),
        social.flagged(net, "is_deleted", mid="m_SIMIN000002", account=here),
        social.echo(net, mid="m_SIMOUT000001", account=here),
        social.event(
            net,
            account=here,
            read=({"mid": "m_SIMOUT000001"} if net is INSTAGRAM else {"watermark": social.AT}),
        ),
        social.reaction(net, "m_SIMOUT000001", account=here),
    ]
    if net is MESSENGER:
        found.append(social.event(net, at=None, account=here, delivery={"watermark": social.AT}))
    return found


@pytest.mark.parametrize("net", [MESSENGER, INSTAGRAM], ids=["messenger", "instagram"])
def test_every_social_key_names_the_account_of_its_entry(net: social.Channel) -> None:
    p, a, who = PREFIX[net.object], net.account, net.customer
    found = keys(net, *catalogue(net, a))
    assert found[:3] == [f"{p}:msg:{a}:m_SIMIN000001", f"{p}:del:{a}:m_SIMIN000002",
                         f"{p}:msg:{a}:m_SIMOUT000001"]  # fmt: skip
    if net is INSTAGRAM:
        assert found[3] == f"ig:read:{a}:m_SIMOUT000001"
    else:  # las marcas de agua ya llevaban la página
        assert [found[3], found[5]] == [f"fb:read:{a}:{who}:{social.AT}",
                                        f"fb:delivery:{a}:{who}:{social.AT}"]  # fmt: skip
    assert found[4].startswith(f"{p}:other:{a}:") and len(found[4]) == len(f"{p}:other:{a}:") + 32
    # el mismo evento en otra cuenta: ninguna clave coincide
    other = keys(net, *catalogue(net, OTHER_ACCOUNT_ID), entry=OTHER_ACCOUNT_ID)
    assert len(other) == len(found) and not set(other) & set(found)


@pytest.mark.parametrize("net", [MESSENGER, INSTAGRAM], ids=["messenger", "instagram"])
def test_the_longest_ids_fit_the_event_key_and_are_stored(
    net: social.Channel, migrator: DB
) -> None:
    events = catalogue(net, LONGEST_ACCOUNT)[:4]
    for found, mid in zip(events, LONGEST_MIDS, strict=True):  # el `mid` más largo que cabe
        for part in (found.get("message"), found.get("read")):
            if isinstance(part, dict) and "mid" in part:
                part["mid"] = mid
    parsed = meta_webhook.parse(social.signed(net, *events, entry_id=LONGEST_ACCOUNT).body)
    longest = max(len(i.key) for i in parsed.items)
    expected = len(f"{'ig:read' if net is INSTAGRAM else 'fb:msg'}:") + 30 + 1 + 300
    assert parsed.rejected == [] and longest == expected <= 700  # `event_key`, varchar(700)
    assert ingress.store(parsed.items) == len(parsed.items)
    stored = migrator.execute("SELECT event_key FROM webhook_ingress").fetchall()
    assert sorted(k for (k,) in stored) == sorted(i.key for i in parsed.items)
    over = social.text(net, mid=LONGEST_MIDS[0] + "%", account=LONGEST_ACCOUNT)  # 303: como hoy
    assert meta_webhook.parse(social.signed(net, over, entry_id=LONGEST_ACCOUNT).body).rejected == [
        "id"
    ]


def inbound_on(org: Any, account_id: Any) -> list[tuple[str | None, str]]:
    with tenant_scope(ctx(org)):
        found = Message.objects.filter(channel_account_id=account_id, direction="INBOUND")
        return list(found.values_list("external_message_id", "body"))


@pytest.mark.parametrize("echo_first", [True, False], ids=["echo-first", "inbound-first"])
@pytest.mark.parametrize("same_org", [True, False], ids=["same-org", "two-orgs"])
@pytest.mark.parametrize("platform", sorted(SIM))
def test_two_accounts_that_write_to_each_other_both_process_the_same_mid(
    world: SimpleNamespace, graph: FakePageGraph, migrator: DB, platform: str, same_org: bool,
    echo_first: bool,
) -> None:  # fmt: skip
    net = SIM[platform]
    graph.then("timeout_after")  # el envío de A salió y su respuesta se perdió: solo el eco
    message, _ = send(world, talk(world, platform))  # la reconcilia con su `mid`
    owner = world.a if same_org else world.b
    other = account(owner, platform, OTHER_ACCOUNT_ID)  # B, la cuenta que recibe a A
    echo = social.signed(net, graph.echo(net))  # en la cuenta de A
    received = social.text(net, "Respuesta", mid="m_SIMOUT000001", account=OTHER_ACCOUNT_ID)
    arrived = social.signed(net, received, entry_id=OTHER_ACCOUNT_ID)  # en la de B
    for delivery in (echo, arrived) if echo_first else (arrived, echo):
        assert post(delivery).status_code == 200
    assert ingress.process() == {"PROCESSED": 2}
    saved = state(world, message)
    assert (saved.delivery_status, saved.external_message_id) == ("SENT", "m_SIMOUT000001")
    assert inbound_on(owner, other.pk) == [("m_SIMOUT000001", "Respuesta")]
    stored = migrator.execute("SELECT event_key FROM webhook_ingress ORDER BY event_key").fetchall()
    p = PREFIX[net.object]
    assert [k for (k,) in stored] == [
        f"{p}:msg:{net.account}:m_SIMOUT000001",
        f"{p}:msg:{OTHER_ACCOUNT_ID}:m_SIMOUT000001",
    ]


@pytest.mark.parametrize("platform", sorted(SIM))
def test_a_retry_with_the_same_key_is_still_one_item(
    world: SimpleNamespace, migrator: DB, platform: str
) -> None:
    net = SIM[platform]
    mine = account(world.a, platform, net.account)
    events = catalogue(net, net.account)
    events[2]["message"]["app_id"] = int(OTHER_APP_ID)  # un eco ajeno: se cierra sin esperar
    delivery = social.signed(net, *events)
    assert post(delivery).status_code == 200
    first = ingress.process()
    assert post(delivery).status_code == 200  # Meta reintenta la misma entrega
    assert ingress.process() == {}  # nada nuevo que procesar
    count = migrator.execute("SELECT count(*) FROM webhook_ingress").fetchone()
    assert count == (len(events),) and sum(first.values()) == len(events)
    assert inbound_on(world.a, mine.pk) == [("m_SIMIN000001", "Hola")]


def legacy(item: meta_webhook.Item) -> meta_webhook.Item:
    """El mismo ítem con la clave global de antes de F7b-04d (`fb:msg:<mid>`, `ig:read:<mid>`)."""
    return dataclasses.replace(item, key=item.key.replace(f":{item.routing_id}:", ":", 1))


@pytest.mark.parametrize("platform", sorted(SIM))
def test_a_retry_of_an_event_stored_with_the_old_key_duplicates_nothing(
    world: SimpleNamespace, graph: FakePageGraph, migrator: DB, platform: str
) -> None:
    net = SIM[platform]
    message, _ = send(world, talk(world, platform))  # SENT con `m_SIMOUT000001`
    now = int(timezone.now().timestamp() * 1000)
    stop = social.text(net, "BAJA", mid="m_SIMIN000001", at=now - 2000)
    back = social.text(net, "ALTA", mid="m_SIMIN000002", at=now - 1000)  # después, por su hora
    for said in (stop, back):  # una identidad nueva: en Instagram, su evento de perfil
        said["sender"]["id"] = NEW_CUSTOMER
    events = [stop, back, graph.echo(net)]
    if net is INSTAGRAM:
        events.append(social.read(INSTAGRAM, mid="m_SIMOUT000001"))
    retry = social.signed(net, *events)
    old = [legacy(i) for i in meta_webhook.parse(retry.body).items]
    assert all(i.key.count(net.account) == 0 for i in old)  # antes del despliegue
    fresh_heap(migrator)
    assert ingress.store(old) == len(events)
    last_everywhere(migrator, next(i.key for i in old if i.key.endswith(":m_SIMIN000001")))
    assert ingress.process() == {"PROCESSED": len(events)}  # la BAJA, aún antes que la ALTA (#934)
    before, trail_before = seen(world, message), trail(migrator)
    assert post(retry).status_code == 200  # Meta reintenta (≤ 36 h) tras el despliegue
    assert ingress.process() == {"PROCESSED": len(events)}  # ítems nuevos, con la clave nueva
    assert seen(world, message) == before  # ni otro mensaje, ni otra confirmación, ni retroceso
    assert trail(migrator) == trail_before  # ni otra auditoría ni otro evento del outbox
    audit, outbox = trail_before
    assert ("message.delivery_changed" in dict(audit)) and dict(outbox)["message.received"] == 2
    assert dict(outbox).get("contact_identity.created") == (1 if net is INSTAGRAM else None)
    rows = migrator.execute("SELECT count(*) FROM webhook_ingress").fetchone()
    assert rows == (2 * len(events),)
    assert before.status == ("READ" if net is INSTAGRAM else "SENT")
    assert len(before.inbound) == 2 and len(before.replies) == 2  # baja y alta, una vez cada una
    assert [(c, s) for c, s, _ in before.consents] == [(platform, "GRANTED")]  # el alta, la última


def trail(migrator: DB) -> tuple[list[tuple[str, int]], list[tuple[str, int]]]:
    """Cuántas veces se auditó y se emitió cada hecho que un repetido no debe repetir."""
    audit = migrator.execute(
        "SELECT action, count(*) FROM audit_logs WHERE action = ANY(%s) GROUP BY 1 ORDER BY 1",
        [list(TRAIL)],
    ).fetchall()
    outbox = migrator.execute(
        "SELECT event_type, count(*) FROM outbox_events WHERE event_type = ANY(%s)"
        " GROUP BY 1 ORDER BY 1",
        [list(TRAIL)],
    ).fetchall()
    return audit, outbox


def seen(world: SimpleNamespace, message: Any) -> SimpleNamespace:
    saved = state(world, message)
    with tenant_scope(ctx(world.a)):
        return SimpleNamespace(
            status=saved.delivery_status, mid=saved.external_message_id,
            inbound=list(Message.objects.filter(direction="INBOUND").values_list("pk", flat=True)),
            replies=list(Message.objects.filter(sender_type="SYSTEM").values_list("pk", flat=True)),
            consents=list(ContactConsent.objects.values_list("channel", "status", "updated_at")),
        )  # fmt: skip


def test_an_oversized_watermark_is_refused_alone_and_the_rest_of_the_batch_is_stored(
    migrator: DB,
) -> None:
    good = social.text(MESSENGER, mid="m_SIMIN000001")
    edge = social.read(MESSENGER, watermark=999_999_999_999_999)  # 15 dígitos: vale
    huge = social.read(MESSENGER, watermark=10**700)  # firmado; antes rompía la clave y el lote
    over = social.read(MESSENGER, watermark=meta_webhook.WATERMARK_MAX)
    parsed = meta_webhook.parse(social.signed(MESSENGER, good, edge, huge, over).body)
    assert parsed.rejected == ["watermark", "watermark"] and len(parsed.items) == 2
    assert post(social.signed(MESSENGER, good, edge, huge, over)).status_code == 200
    stored = migrator.execute("SELECT event_key FROM webhook_ingress ORDER BY event_key").fetchall()
    assert [k for (k,) in stored] == [
        f"fb:msg:{MESSENGER.account}:m_SIMIN000001",
        f"fb:read:{MESSENGER.account}:{MESSENGER.customer}:999999999999999",
    ]

"""F7b-03: el simulador de Meta para Messenger e Instagram (`tests/meta_sim/social.py` y
`page_graph.py`) contra las formas que ADR-035 documenta. Sin red ni base de datos."""

import hashlib
import hmac
import json
import time
from typing import Any

import pytest
from django.test import override_settings
from urllib3.exceptions import ProtocolError, ReadTimeoutError

from core import http
from tests.meta_sim import GRAPH_VERSION, social, transport, webhooks
from tests.meta_sim.graph import TEST_TOKEN
from tests.meta_sim.page_graph import ERRORS, OUTCOMES, FakePageGraph
from tests.meta_sim.social import CHANNELS, INSTAGRAM, MESSENGER, Channel

ALLOWED = override_settings(HTTP_ALLOWED_HOSTS=[transport.GRAPH_HOST])
BASE = f"https://{transport.GRAPH_HOST}/{GRAPH_VERSION}/{social.PAGE_ID}"
AUTH = {"Authorization": f"Bearer {TEST_TOKEN}"}
BY_CHANNEL = pytest.mark.parametrize("channel", CHANNELS, ids=lambda c: c.object)


def send_body(recipient: str = social.PSID, **extra: Any) -> dict[str, Any]:
    return {"recipient": {"id": recipient}, "messaging_type": "RESPONSE"} | {
        "message": {"text": "Hola"}
    } | extra  # fmt: skip


@pytest.fixture
def graph(monkeypatch: pytest.MonkeyPatch) -> FakePageGraph:
    fake = FakePageGraph()
    transport.install(monkeypatch, fake)
    return fake


def post(path: str = "messages", payload: Any = None, headers: dict[str, str] | None = AUTH) -> Any:
    sent = send_body() if payload is None else payload
    return http.request("POST", f"{BASE}/{path}", headers=headers, json=sent)


def error_of(response: Any) -> tuple[int, int, int | None]:
    error = json.loads(response.data)["error"]
    assert error["message"] and error["type"] and error["fbtrace_id"]
    return response.status, error["code"], error.get("error_subcode")


def events(payload: Any, channel: Channel, field: str = "messaging") -> list[dict[str, Any]]:
    """Referencia de la forma de ADR-035 §4–5: sobre, entradas, eventos de `messaging[]` con
    `sender`/`recipient`, `timestamp` en ms (salvo la entrega, que no lo trae [S17]) y el cruce
    de la cuenta con `entry.id`."""
    assert payload["object"] == channel.object and isinstance(payload["entry"], list)
    found = []
    for entry in payload["entry"]:
        assert entry["id"] == channel.account and isinstance(entry["time"], int)
        for item in entry[field]:
            echo = item.get("message", {}).get("is_echo", False)
            us, them = (item["sender"], item["recipient"])[:: 1 if echo else -1]
            assert (us["id"], them["id"]) == (entry["id"], channel.customer), "cruce de §4"
            if "delivery" in item:
                assert "timestamp" not in item and isinstance(item["delivery"]["watermark"], int)
            else:
                assert isinstance(item["timestamp"], int) and item["timestamp"] > 10**12
            found.append(item)
    return found


def only(delivery: webhooks.Delivery, channel: Channel, field: str = "messaging") -> Any:
    (item,) = events(delivery.json, channel, field)
    return item


# --- la firma y el catálogo de ADR-035 §9 -----------------------------------------------------

SECTION_9 = {  # webhooks que ADR-035 §9 enumera, por canal
    "text", "reply", "own_echo", "foreign_echo", "read", "standby", "is_unsupported",
    "is_deleted", "reaction", "batch", "account_mismatch", "unknown_object",
    "unreact", "grouped", "echo_account_mismatch",  # variantes de la revisión de #776
}  # fmt: skip
EXTRA = {
    "page": {"delivery", "delivery_no_mids", "sticker"},
    "instagram": {"story_reply", "own_echo_no_app_id"},
}


@BY_CHANNEL
def test_the_catalogue_has_every_section_9_event_signed_with_the_app_secret(
    channel: Channel,
) -> None:
    found = social.catalogue(channel)
    attachments = {f"attachment_{k}" for k in channel.attachments}
    assert set(found) == SECTION_9 | EXTRA[channel.object] | attachments
    for name, sent in found.items():
        expected = hmac.new(webhooks.TEST_APP_SECRET.encode(), sent.body, hashlib.sha256)
        assert sent.headers[webhooks.SIGNATURE_HEADER] == f"sha256={expected.hexdigest()}", name
        assert webhooks.sign(sent.body, "otro-secreto") != sent.headers[webhooks.SIGNATURE_HEADER]
        assert sent.json["entry"] and sent.body.isascii(), name


@BY_CHANNEL
def test_non_ascii_is_escaped_with_lowercase_hex_and_signed_over_the_wire_bytes(
    channel: Channel,
) -> None:
    sent = social.signed(channel, social.text(channel, "¿Precio? 👍"))
    assert b"\\u00bf" in sent.body and b"\\ud83d\\udc4d" in sent.body  # [S17]
    assert only(sent, channel)["message"]["text"] == "¿Precio? 👍"
    assert sent.headers[webhooks.SIGNATURE_HEADER] == webhooks.sign(sent.body)


# --- las formas -------------------------------------------------------------------------------


@BY_CHANNEL
def test_an_inbound_text_goes_from_the_customer_to_the_account(channel: Channel) -> None:
    item = only(social.catalogue(channel)["text"], channel)
    assert item["sender"]["id"] == channel.customer and item["recipient"]["id"] == channel.account
    assert item["message"] == {"mid": "m_SIMIN000001", "text": "Hola"}
    reply = only(social.catalogue(channel)["reply"], channel)["message"]
    assert reply["reply_to"] == {"mid": "m_SIMOUT000001"}


@BY_CHANNEL
def test_each_attachment_has_its_type_and_a_cdn_url(channel: Channel) -> None:
    for kind in channel.attachments:
        (found,) = only(social.signed(channel, social.attachment(channel, kind)), channel)[
            "message"
        ]["attachments"]
        if kind == "fallback":  # Meta puede mandarlo sin `payload` [S18]
            assert found == {"type": "fallback"}
            continue
        assert found["type"] == kind and found["payload"]["url"].startswith("https://")
    (sticker,) = only(social.catalogue(MESSENGER)["sticker"], MESSENGER)["message"]["attachments"]
    assert sticker["type"] == "image" and sticker["payload"]["sticker_id"] == social.STICKER_ID
    with pytest.raises(ValueError, match="no envía"):
        social.attachment(MESSENGER, "story_mention")


def test_instagram_story_mention_and_reply_carry_the_story_url_to_strip() -> None:
    mention = only(social.signed(INSTAGRAM, social.story_mention()), INSTAGRAM)["message"]
    assert mention["attachments"] == [
        {"type": "story_mention", "payload": {"url": social.STORY_URL}}
    ]
    reply = only(social.catalogue(INSTAGRAM)["story_reply"], INSTAGRAM)["message"]
    assert reply["reply_to"]["story"] == {"url": social.STORY_URL, "id": social.STORY_ID}
    assert reply["text"]


@BY_CHANNEL
def test_echoes_go_from_the_account_and_name_the_app_that_sent_them(channel: Channel) -> None:
    catalogue = social.catalogue(channel)
    own = only(catalogue["own_echo"], channel)
    assert own["sender"]["id"] == channel.account and own["recipient"]["id"] == channel.customer
    assert own["message"]["is_echo"] is True and own["message"]["metadata"] == "sim-echo-token"
    if channel is MESSENGER:  # documentado [S19]; en Instagram, SIN VERIFICAR 11/12
        assert own["message"]["app_id"] == int(social.APP_ID)
    else:
        unsure = only(catalogue["own_echo_no_app_id"], channel)["message"]
        assert "app_id" not in unsure and unsure["metadata"] == "sim-echo-token"
    mismatch = catalogue["echo_account_mismatch"].json
    assert mismatch["entry"][0]["messaging"][0]["sender"]["id"] == social.OTHER_ACCOUNT_ID
    with pytest.raises(AssertionError, match="cruce"):
        events(mismatch, channel)
    foreign = only(social.catalogue(channel)["foreign_echo"], channel)["message"]
    assert foreign["app_id"] == int(social.OTHER_APP_ID) and "metadata" not in foreign
    bare = only(social.signed(channel, social.echo(channel, app_id=None)), channel)["message"]
    assert "app_id" not in bare  # el eco de Instagram sin `app_id` (SIN VERIFICAR 12)


def test_reads_use_a_watermark_in_messenger_and_a_mid_in_instagram() -> None:
    assert only(social.catalogue(MESSENGER)["read"], MESSENGER)["read"] == {"watermark": social.AT}
    assert only(social.catalogue(INSTAGRAM)["read"], INSTAGRAM)["read"] == {"mid": "m_SIMOUT000001"}
    catalogue = social.catalogue(MESSENGER)
    delivered = only(catalogue["delivery"], MESSENGER)
    assert delivered["delivery"] == {"mids": ["m_SIMOUT000001"], "watermark": social.AT}
    assert set(delivered) == {"sender", "recipient", "delivery"}  # sin `timestamp` [S17]
    bare = catalogue["delivery_no_mids"].json["entry"][0]
    assert bare["messaging"][0]["delivery"] == {"watermark": social.AT}
    assert bare["time"] == social.AT  # sin `timestamp`, la marca de agua


@BY_CHANNEL
def test_standby_flags_reactions_and_the_account_mismatch(channel: Channel) -> None:
    catalogue = social.catalogue(channel)
    assert "messaging" not in catalogue["standby"].json["entry"][0]
    assert only(catalogue["standby"], channel, "standby")["message"]["text"] == "Hola"
    for flag in ("is_unsupported", "is_deleted"):
        assert only(catalogue[flag], channel)["message"] == {"mid": "m_SIMIN000001", flag: True}
    with pytest.raises(ValueError):
        social.flagged(channel, "is_invented")
    assert only(catalogue["reaction"], channel)["reaction"] == {
        "mid": "m_SIMOUT000001", "action": "react", "reaction": "love", "emoji": "❤️",
    }  # fmt: skip
    unreact = only(catalogue["unreact"], channel)["reaction"]
    assert unreact == {"mid": "m_SIMOUT000001", "action": "unreact"}
    mismatch = catalogue["account_mismatch"].json
    with pytest.raises(AssertionError, match="cruce"):
        events(mismatch, channel)
    entry = mismatch["entry"][0]
    assert entry["id"] == channel.account
    assert entry["messaging"][0]["recipient"]["id"] == social.OTHER_ACCOUNT_ID
    assert catalogue["unknown_object"].json["object"] == "user"


@BY_CHANNEL
def test_a_batch_has_several_entries_out_of_order(channel: Channel) -> None:
    items = events(social.catalogue(channel)["batch"].json, channel)
    stamps = [i["timestamp"] for i in items]
    assert len(items) == 3 and stamps != sorted(stamps)
    assert len({i["message"]["mid"] for i in items}) == 3
    (entry,) = social.catalogue(channel)["grouped"].json["entry"]
    assert len(entry["messaging"]) == 2  # dos eventos en un mismo `messaging[]`
    assert len(events(social.catalogue(channel)["grouped"].json, channel)) == 2


# --- la Graph API falsa de la página, por core.http -------------------------------------------


@ALLOWED
@pytest.mark.parametrize("recipient", [social.PSID, social.IGSID])
def test_a_send_returns_the_recipient_and_a_new_mid(graph: FakePageGraph, recipient: str) -> None:
    response = post(payload=send_body(recipient, message={"text": "Hola", "metadata": "tok"}))
    assert response.status == 200
    assert json.loads(response.data) == {"recipient_id": recipient, "message_id": "m_SIMOUT000001"}
    assert graph.sent[0]["page_id"] == social.PAGE_ID
    assert graph.sent[0]["message"]["metadata"] == "tok"
    assert json.loads(post().data)["message_id"] == "m_SIMOUT000002"


KNOWN = {  # ADR-035 §6 [S26]: (estado, código, subcódigo) -> código propio
    "window_closed": ((400, 10, 2018278), "WINDOW_CLOSED"),
    "window_closed_2534022": ((400, 10, 2534022), "WINDOW_CLOSED"),
    "unreachable": ((400, 551, None), "RECIPIENT_UNREACHABLE"),
    "token": ((401, 190, None), "TOKEN_INVALID"),
    "token_400": ((400, 190, None), "TOKEN_INVALID"),  # el estado no decide: el código sí
    "rate_limit": ((400, 613, None), "RATE_LIMITED"),
    "messages_access_off": ((400, 200, 2534041), "MESSAGES_ACCESS_OFF"),
    "permission_10": ((400, 10, None), "PROVIDER_REJECTED"),  # `10` sin subcódigo de ventana
    "permission_200": ((400, 200, None), "PROVIDER_REJECTED"),  # `200` sin 2534041
    "invalid": ((400, 100, None), "PROVIDER_REJECTED"),
    "server": ((500, 1, None), "PROVIDER_ERROR"),
}


@ALLOWED
@pytest.mark.parametrize("name", sorted(KNOWN))
def test_each_section_6_error_has_its_code_and_subcode(graph: FakePageGraph, name: str) -> None:
    assert set(KNOWN) == set(ERRORS) and ERRORS[name][4] == KNOWN[name][1]
    graph.then(name)
    assert error_of(post()) == KNOWN[name][0] and graph.sent == []
    assert post().status == 200  # el guion se agotó


@ALLOWED
def test_human_agent_is_the_only_tag_and_bad_bodies_are_rejected(graph: FakePageGraph) -> None:
    human = send_body(messaging_type="MESSAGE_TAG", tag="HUMAN_AGENT")
    assert post(payload=human).status == 200 and graph.sent[0]["tag"] == "HUMAN_AGENT"
    for tag in ("CONFIRMED_EVENT_UPDATE", "ACCOUNT_UPDATE", "POST_PURCHASE_UPDATE", None):
        assert error_of(post(payload=send_body(messaging_type="MESSAGE_TAG", tag=tag)))[1] == 100
    for bad in (
        {}, send_body(""), send_body(messaging_type="PUSH"), send_body(message={"text": 5}),
        send_body(message={"attachment": {}}),
    ):  # fmt: skip
        assert error_of(post(payload=bad))[1] == 100, bad
    assert len(graph.sent) == 1
    with pytest.raises(ValueError, match="desconocido"):
        graph.then("inventado")


@ALLOWED
def test_a_missing_token_an_unknown_page_and_another_version_are_errors(
    graph: FakePageGraph,
) -> None:
    for headers in (None, {"Authorization": "Bearer otro"}):
        assert error_of(post(headers=headers))[:2] == (401, 190)
    other = f"https://{transport.GRAPH_HOST}/{GRAPH_VERSION}/{social.OTHER_ACCOUNT_ID}/messages"
    assert error_of(http.request("POST", other, headers=AUTH, json=send_body()))[1] == 100
    old = f"https://{transport.GRAPH_HOST}/v1.0/{social.PAGE_ID}/messages"
    assert error_of(http.request("POST", old, headers=AUTH, json=send_body()))[1] == 100
    assert graph.sent == []


@ALLOWED
def test_reading_the_page_shows_the_linked_instagram_account_only_when_asked(
    graph: FakePageGraph,
) -> None:
    def read(query: str = "") -> Any:
        return json.loads(http.request("GET", f"{BASE}{query}", headers=AUTH).data)

    assert read() == {"id": social.PAGE_ID, "name": "Página de prueba"}
    linked = read("?fields=id,name,instagram_business_account")
    assert linked["instagram_business_account"] == {"id": social.IG_ACCOUNT_ID}
    graph.pages[social.PAGE_ID] = None  # una página sin cuenta de Instagram vinculada
    assert "instagram_business_account" not in read("?fields=instagram_business_account")


@ALLOWED
def test_subscribing_the_app_records_the_fields(graph: FakePageGraph) -> None:
    def apps() -> Any:
        return json.loads(http.request("GET", f"{BASE}/subscribed_apps", headers=AUTH).data)

    assert apps() == {"data": []}  # una página sin suscribir: la lista vacía, no un fallo
    response = post("subscribed_apps?subscribed_fields=messages,message_echoes", payload={})
    assert json.loads(response.data) == {"success": True}
    assert graph.subscribed == {social.PAGE_ID: ["messages", "message_echoes"]}
    assert error_of(post("subscribed_apps", payload={}))[1] == 100
    assert post("subscribed_apps", payload={"subscribed_fields": "messages"}).status == 200
    assert graph.subscribed[social.PAGE_ID] == ["messages"]  # también en el cuerpo
    assert [app["subscribed_fields"] for app in apps()["data"]] == [["messages"]]


@ALLOWED
def test_slow_cut_timeout_and_a_lost_reply(graph: FakePageGraph) -> None:
    assert set(OUTCOMES) >= {"ok", "slow", "cut", "timeout", "cut_after", "timeout_after"}
    graph.slow_seconds = 0.1
    graph.then("slow", "cut", "timeout", "cut_after", "timeout_after")
    started = time.monotonic()
    assert post().status == 200 and time.monotonic() - started >= 0.1
    for error in (ProtocolError, ReadTimeoutError, ProtocolError, ReadTimeoutError):
        with pytest.raises(error):
            post()
    assert len(graph.sent) == 3  # el lento y los dos perdidos: Meta los envió (no deduplica, §6)

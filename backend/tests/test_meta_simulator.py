"""F7-01: el simulador de la API de Meta (`tests/meta_sim`). Sin red ni base de datos."""

import hashlib
import hmac
import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest
from django.conf import settings
from django.test import override_settings
from urllib3.exceptions import ProtocolError, ReadTimeoutError

from core import http
from tests.meta_sim import GRAPH_VERSION, transport, webhooks
from tests.meta_sim.graph import ERRORS, MEDIA_HOST, OUTCOMES, TEST_TOKEN, FakeGraph

ALLOWED = override_settings(HTTP_ALLOWED_HOSTS=[transport.GRAPH_HOST, MEDIA_HOST])
BASE = f"https://{transport.GRAPH_HOST}/{GRAPH_VERSION}"
AUTH = {"Authorization": f"Bearer {TEST_TOKEN}"}
SEND = {"messaging_product": "whatsapp", "to": webhooks.CUSTOMER, "type": "text"} | {
    "text": {"body": "Hola"}
}
NUMBER = webhooks.PHONE_NUMBER_ID


@pytest.fixture
def graph(monkeypatch: pytest.MonkeyPatch) -> FakeGraph:
    fake = FakeGraph()
    transport.install(monkeypatch, fake)
    return fake


def call(
    method: str, path: str, *, headers: dict[str, str] | None = AUTH, payload: Any = None
) -> Any:
    """Una petición a la Graph API falsa por `core.http` (con su política real)."""
    return http.request(method, f"{BASE}/{path}", headers=headers, json=payload)


def post(
    path: str = f"{NUMBER}/messages", *, headers: dict[str, str] | None = AUTH, payload: Any = SEND
) -> Any:
    return call("POST", path, headers=headers, payload=payload)


def read(path: str) -> Any:
    return json.loads(call("GET", path).data)


def error_of(response: Any) -> dict[str, Any]:
    found: dict[str, Any] = json.loads(response.data)["error"]
    return found


# --- firma ---------------------------------------------------------------------------------


def test_the_signature_is_the_hmac_sha256_of_the_raw_body_with_the_app_secret() -> None:
    body = webhooks.encode({"a": 1})
    expected = hmac.new(webhooks.TEST_APP_SECRET.encode(), body, hashlib.sha256).hexdigest()
    assert webhooks.sign(body) == f"sha256={expected}" and len(expected) == 64
    sent = webhooks.deliver(body)
    assert sent.headers[webhooks.SIGNATURE_HEADER] == webhooks.sign(body) and sent.body == body


def test_a_wrong_secret_a_tampered_body_and_a_forced_signature_do_not_match() -> None:
    good = webhooks.text_delivery("Hola")
    assert good.headers[webhooks.SIGNATURE_HEADER] == webhooks.sign(good.body)
    other = webhooks.deliver(good.body, secret="otro-secreto-de-prueba")  # noqa: S106
    assert other.headers[webhooks.SIGNATURE_HEADER] != good.headers[webhooks.SIGNATURE_HEADER]
    assert webhooks.sign(good.body + b" ") != good.headers[webhooks.SIGNATURE_HEADER]
    forced = webhooks.deliver(good.body, signature="sha256=" + "0" * 64)
    assert forced.headers[webhooks.SIGNATURE_HEADER] == "sha256=" + "0" * 64
    assert webhooks.SIGNATURE_HEADER not in webhooks.deliver(good.body, secret=None).headers


# --- cuerpos de webhook ----------------------------------------------------------------------


def items(payload: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Referencia mínima de la forma de Meta: (mensajes, estados) de un cuerpo bien formado."""
    assert payload["object"] == "whatsapp_business_account" and isinstance(payload["entry"], list)
    messages: list[dict[str, Any]] = []
    statuses: list[dict[str, Any]] = []
    for entry in payload["entry"]:
        assert isinstance(entry["id"], str)
        for change in entry["changes"]:
            value = change["value"]
            assert isinstance(value["metadata"]["phone_number_id"], str)
            messages += value.get("messages", [])
            statuses += value.get("statuses", [])
    return messages, statuses


def test_the_envelope_can_carry_another_object_type() -> None:
    assert webhooks.envelope(webhooks.value(), object_="page")["object"] == "page"
    assert webhooks.envelope(webhooks.value())["object"] == "whatsapp_business_account"


def test_a_text_delivery_has_the_shape_meta_sends() -> None:
    sent = webhooks.text_delivery("Hola, ¿precio?")
    messages, statuses = items(sent.json)
    assert statuses == [] and len(messages) == 1
    assert messages[0]["type"] == "text" and messages[0]["text"]["body"] == "Hola, ¿precio?"
    assert messages[0]["from"] == webhooks.CUSTOMER and messages[0]["id"].startswith("wamid.")
    value = sent.json["entry"][0]["changes"][0]["value"]
    assert value["contacts"][0]["wa_id"] == webhooks.CUSTOMER
    assert value["metadata"]["phone_number_id"] == webhooks.PHONE_NUMBER_ID
    assert sent.json["entry"][0]["id"] == webhooks.WABA_ID


@pytest.mark.parametrize("kind", webhooks.UNSUPPORTED_KINDS)
def test_each_unsupported_type_has_its_own_type_and_payload(kind: str) -> None:
    sent = webhooks.deliver(
        webhooks.encode(webhooks.envelope(webhooks.value(messages=[webhooks.other_message(kind)])))
    )
    (message,), _ = items(sent.json)
    assert message["type"] == kind and kind in message


@pytest.mark.parametrize("state", ["sent", "delivered", "read"])
def test_a_delivery_status_carries_the_message_id_and_the_opaque_data(state: str) -> None:
    sent = webhooks.status_delivery("wamid.SIMOUT000001", state, opaque="msg-1")
    _, (found,) = items(sent.json)
    assert (found["id"], found["status"], found["biz_opaque_callback_data"]) == (
        "wamid.SIMOUT000001", state, "msg-1",
    )  # fmt: skip
    assert "errors" not in found


def test_a_failed_status_carries_the_error_code_and_title() -> None:
    sent = webhooks.status_delivery(
        "wamid.SIMOUT000002", "failed", error=(131047, "Re-engagement message")
    )
    _, (found,) = items(sent.json)
    assert found["status"] == "failed" and found["errors"][0]["code"] == 131047


def test_a_batch_has_several_items_of_several_kinds() -> None:
    messages, statuses = items(webhooks.batch_delivery().json)
    assert [m["type"] for m in messages] == ["text", "text", "reaction"] and len(statuses) == 1
    assert len({m["from"] for m in messages}) == 2


def test_every_malformed_body_is_not_what_meta_sends() -> None:
    bodies = webhooks.malformed()
    assert {"not_json", "empty", "array", "null", "wrong_object"} <= set(bodies)
    for name, body in bodies.items():
        with pytest.raises((ValueError, AssertionError, KeyError, TypeError)):
            payload = json.loads(body)
            messages, _ = items(payload)
            assert all(isinstance(m["text"]["body"], str) for m in messages), (
                name
            )  # text_not_a_string
    signed = webhooks.deliver(
        bodies["not_json"]
    )  # firmado y roto: la firma no salva un cuerpo malo
    assert signed.headers[webhooks.SIGNATURE_HEADER] == webhooks.sign(b"esto no es json")


# --- Graph API falsa, por core.http ------------------------------------------------------------


@ALLOWED
def test_a_text_send_succeeds_with_a_synthetic_message_id(graph: FakeGraph) -> None:
    response = post()
    body = json.loads(response.data)
    assert response.status == 200 and body["messages"][0]["id"] == "wamid.SIMOUT000001"
    assert body["contacts"][0]["wa_id"] == webhooks.CUSTOMER
    assert (
        graph.sent[0]["text"]["body"] == "Hola"
        and graph.sent[0]["phone_number_id"] == webhooks.PHONE_NUMBER_ID
    )
    assert json.loads(post().data)["messages"][0]["id"] == "wamid.SIMOUT000002"  # distinto cada vez


# Lo que se conoce de Meta a la fecha del diseño (se reconfirma con su documentación en F7-05).
KNOWN = {
    "token": (401, 190, None),
    "rate_limit": (400, 130429, None),
    "pair_rate_limit": (400, 131056, None),
    "no_number": (400, 100, 33),
    "window_closed": (400, 131047, None),
    "unreachable": (400, 131026, None),
    "quality": (400, 131048, None),
    "template": (404, 132001, None),
    "server": (500, 1, None),
}


@ALLOWED
@pytest.mark.parametrize("name", sorted(KNOWN))
def test_each_error_has_meta_status_code_and_shape(graph: FakeGraph, name: str) -> None:
    assert set(KNOWN) == set(ERRORS)
    graph.then(name)
    response = post()
    error = error_of(response)
    assert (response.status, error["code"], error.get("error_subcode")) == KNOWN[name]
    assert error["message"] and error["fbtrace_id"] and graph.sent == []  # nada se envió
    assert post().status == 200  # el siguiente sale bien: el guion se agotó


@ALLOWED
def test_the_script_is_consumed_one_call_at_a_time(graph: FakeGraph) -> None:
    graph.then("server", "ok", "window_closed")
    assert [post().status for _ in range(4)] == [500, 200, 400, 200]
    with pytest.raises(ValueError, match="desconocido"):
        graph.then("inventado")
    assert set(OUTCOMES) >= {"ok", "slow", "cut", "timeout"}


@ALLOWED
def test_a_missing_or_wrong_token_is_rejected_before_anything_else(graph: FakeGraph) -> None:
    for headers in (None, {"Authorization": "Bearer otro"}, {"Authorization": TEST_TOKEN}):
        response = post(headers=headers)
        assert (response.status, error_of(response)["code"]) == (401, 190)
    assert graph.sent == []


@ALLOWED
def test_an_unknown_number_a_bad_body_and_another_version_are_errors(graph: FakeGraph) -> None:
    assert error_of(post("100000000000999/messages"))["code"] == 100
    for payload in (
        {},
        {**SEND, "type": "image"},
        {**SEND, "text": {"body": 5}},
        {**SEND, "to": ""},
    ):
        assert post(payload=payload).status == 400, payload
    other = http.request(
        "POST",
        f"https://{transport.GRAPH_HOST}/v1.0/{webhooks.PHONE_NUMBER_ID}/messages",
        headers=AUTH,
        json=SEND,
    )
    assert other.status == 400 and graph.sent == []


@ALLOWED
def test_the_simulator_refuses_hosts_it_was_not_installed_for(graph: FakeGraph) -> None:
    with pytest.raises(AssertionError, match="no atiende"):
        http.request("GET", f"https://{MEDIA_HOST}/media/x", headers=AUTH)


@ALLOWED
def test_a_cut_connection_and_a_timeout_raise_what_urllib3_raises(graph: FakeGraph) -> None:
    graph.then("cut", "timeout")
    with pytest.raises(ProtocolError):
        post()
    with pytest.raises(ReadTimeoutError):
        post()
    assert graph.sent == []


@ALLOWED
def test_a_slow_call_takes_the_configured_time_and_still_succeeds(graph: FakeGraph) -> None:
    graph.slow_seconds = 0.2
    graph.then("slow")
    started = time.monotonic()
    assert post().status == 200
    assert time.monotonic() - started >= 0.2


@ALLOWED
def test_reading_a_number_and_its_waba(graph: FakeGraph) -> None:
    number = read(f"{NUMBER}?fields=id")
    assert number["id"] == NUMBER and number["quality_rating"] == "GREEN"
    assert number["messaging_limit_tier"] == "TIER_1K"
    assert [n["id"] for n in read(f"{webhooks.WABA_ID}/phone_numbers")["data"]] == [NUMBER]
    assert call("GET", "100000000000777/phone_numbers").status == 400
    graph.quality, graph.tier = "RED", "TIER_250"
    after = read(NUMBER)
    assert (after["quality_rating"], after["messaging_limit_tier"]) == ("RED", "TIER_250")


@ALLOWED
def test_the_template_and_media_hooks_answer_what_the_test_configures(graph: FakeGraph) -> None:
    templates = f"{webhooks.WABA_ID}/message_templates"
    assert read(templates) == {"data": []}
    graph.templates.append({"name": "seguimiento", "language": "es", "status": "APPROVED"})
    assert read(templates)["data"][0]["status"] == "APPROVED"
    graph.media["SIMMEDIA0001"] = b"\x89PNG"
    assert read("SIMMEDIA0001")["url"].startswith(f"https://{MEDIA_HOST}/media/")
    media = f"https://{transport.GRAPH_HOST}/media/SIMMEDIA0001"
    assert http.request("GET", media, headers=AUTH).data == b"\x89PNG"


@ALLOWED
def test_core_http_policy_still_applies_to_the_simulator(graph: FakeGraph) -> None:
    for url in (
        f"http://{transport.GRAPH_HOST}/x",
        f"https://evil.example.com/{GRAPH_VERSION}/x",
        f"https://{transport.GRAPH_HOST}:8443/x",
    ):
        with pytest.raises(http.BlockedDestination):
            http.request("GET", url, headers=AUTH)
    with override_settings(HTTP_ALLOWED_HOSTS=[]), pytest.raises(http.BlockedDestination):
        post()  # sin allowlist no se permite nada, tampoco hacia el simulador
    assert graph.requests == []


def test_the_fixtures_follow_the_pinned_graph_version() -> None:
    """Cuando F7-02 añada `META_GRAPH_VERSION`, subirla obliga a revisar `meta_sim`."""
    assert getattr(settings, "META_GRAPH_VERSION", GRAPH_VERSION) == GRAPH_VERSION


# --- servidor local ---------------------------------------------------------------------------


def test_the_local_server_answers_over_a_real_socket() -> None:
    graph = FakeGraph()
    with transport.serve(graph) as base:

        def fetch(path: str, data: bytes | None = None, token: str = TEST_TOKEN) -> Any:
            headers = {"Authorization": f"Bearer {token}"}
            request = urllib.request.Request(f"{base}{path}", data=data, headers=headers)  # noqa: S310
            return urllib.request.urlopen(request, timeout=3)  # noqa: S310

        ok = fetch(
            f"/{GRAPH_VERSION}/{webhooks.PHONE_NUMBER_ID}/messages", json.dumps(SEND).encode()
        )
        assert (
            ok.status == 200 and json.loads(ok.read())["messages"][0]["id"] == "wamid.SIMOUT000001"
        )
        with pytest.raises(urllib.error.HTTPError) as denied:
            fetch(f"/{GRAPH_VERSION}/{webhooks.PHONE_NUMBER_ID}", token="otro")  # noqa: S106
        assert denied.value.code == 401
        graph.then("cut")
        with pytest.raises(OSError):  # noqa: PT011 — RemoteDisconnected / URLError
            fetch(
                f"/{GRAPH_VERSION}/{webhooks.PHONE_NUMBER_ID}/messages", json.dumps(SEND).encode()
            )


def test_the_test_secrets_exist_only_in_tests() -> None:
    root = Path(__file__).resolve().parents[1]
    needles = (webhooks.TEST_APP_SECRET, TEST_TOKEN, "meta_sim")
    offenders = [
        str(path.relative_to(root)) for folder in ("core", "apps", "config")
        for path in (root / folder).rglob("*.py") if any(n in path.read_text() for n in needles)
    ]  # fmt: skip
    assert offenders == []

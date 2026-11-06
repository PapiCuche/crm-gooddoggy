"""F7-03a: el webhook público de Meta (`apps.conversations.meta_webhook`, ADR-029 §3): el GET de
la suscripción, la firma del POST, el tope del cuerpo y el esquema, con el simulador de Meta
(`tests/meta_sim`). Sin base de datos: la ruta no escribe nada todavía."""

import hmac
import io
import logging
from typing import Any

import psycopg
import pytest
from django.http import HttpRequest
from django.test import Client

from apps.conversations import meta_webhook
from apps.conversations.meta_webhook import BODY_MAX, Invalid, parse, webhook
from config.settings import base
from tests.meta_sim import webhooks
from tests.meta_sim.webhooks import TEST_APP_SECRET

pytestmark = pytest.mark.usefixtures("tenant_db")
DB = psycopg.Connection[Any]
ITEM_LEVEL = ("no_phone_number_id", "text_not_a_string")  # se descartan por ítem, no el cuerpo
URL = "/webhooks/meta/"
VERIFY = "verify-token-test-not-real-0001"
OLD_SECRET = "meta-sim-previous-app-secret-not-real"  # noqa: S105 — ficticio
EMPTY = (b"", "text/html; charset=utf-8")


@pytest.fixture(autouse=True)
def channel(settings: Any) -> None:
    """El canal encendido, con el secreto vigente y el anterior (la rotación del app secret)."""
    settings.ROOT_URLCONF, settings.MIDDLEWARE = "config.urls", base.MIDDLEWARE
    settings.META_ENABLED, settings.META_WEBHOOK_VERIFY_TOKEN = True, VERIFY
    settings.META_APP_SECRETS = [TEST_APP_SECRET, OLD_SECRET]


def spy_compare(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Cuenta las llamadas a `hmac.compare_digest` sin cambiar lo que devuelve."""
    calls: list[int] = []
    real = hmac.compare_digest

    def counting(a: Any, b: Any) -> bool:
        calls.append(1)
        return real(a, b)

    monkeypatch.setattr(hmac, "compare_digest", counting)
    return calls


def post(delivery: webhooks.Delivery, client: Client | None = None) -> Any:
    extra: dict[str, Any] = {
        f"HTTP_{k.upper().replace('-', '_')}": v for k, v in delivery.headers.items()
    }
    return (client or Client()).post(URL, delivery.body, "application/json", **extra)


def stored(migrator: DB) -> int:
    return int(migrator.execute("SELECT count(*) FROM webhook_ingress").fetchone()[0])  # type: ignore[index]


def shape(response: Any) -> tuple[int, bytes, str]:
    return response.status_code, response.content, response["Content-Type"]


def subscribe(**over: str | None) -> Any:
    query = {"hub.mode": "subscribe", "hub.verify_token": VERIFY, "hub.challenge": "1158201444"}
    return Client().get(URL, {k: v for k, v in (query | over).items() if v is not None})


def test_with_the_channel_off_the_route_does_not_exist_for_any_method(settings: Any) -> None:
    settings.META_ENABLED = False
    unknown = shape(Client().get("/webhooks/nada/"))
    good = "?hub.mode=subscribe&hub.verify_token=" + VERIFY + "&hub.challenge=1"
    for method in ("GET", "POST", "PUT", "DELETE", "HEAD", "OPTIONS"):
        response = Client().generic(method, URL + (good if method == "GET" else ""), b"{}")
        assert response.status_code == 404, method
        assert method == "HEAD" or shape(response)[1:] == unknown[1:], (
            method
        )  # HEAD no lleva cuerpo
    assert post(webhooks.text_delivery()).status_code == 404  # ni firmado


def test_the_subscription_echoes_only_the_challenge_as_plain_text() -> None:
    response = subscribe()
    assert (response.status_code, response.content) == (200, b"1158201444")
    assert response["Content-Type"].startswith("text/plain")
    assert response["X-Content-Type-Options"] == "nosniff"
    assert VERIFY not in response.content.decode() and "Set-Cookie" not in response


@pytest.mark.parametrize(
    "over",
    [
        {"hub.verify_token": "otro-token-de-prueba-0000000000"},
        {"hub.verify_token": None},
        {"hub.verify_token": ""},
        {"hub.mode": "unsubscribe"},
        {"hub.mode": None},
        {"hub.challenge": None},
        {"hub.challenge": ""},
        {"hub.challenge": "<script>alert(1)</script>"},
        {"hub.challenge": "12 34"},
        {"hub.challenge": "1" * 129},
        {"hub.challenge": "ñandú"},
        {"hub.challenge": "1\r\nSet-Cookie: a=b"},
    ],
)
def test_any_other_subscription_is_a_bare_403_that_does_not_say_why(over: dict[str, Any]) -> None:
    assert shape(subscribe(**over)) == (403, *EMPTY)


def test_the_verify_token_is_compared_in_constant_time_whatever_else_is_wrong(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = spy_compare(monkeypatch)
    for over in ({}, {"hub.mode": "otro"}, {"hub.verify_token": None}, {"hub.challenge": "<"}):
        subscribe(**over)
    assert len(calls) == 4  # una comparación por petición, también con el modo o el reto malos


def test_a_signed_delivery_is_received_with_either_secret_and_an_empty_200() -> None:
    for secret in (TEST_APP_SECRET, OLD_SECRET):
        delivery = webhooks.deliver(
            webhooks.encode(webhooks.envelope(webhooks.value())), secret=secret
        )
        assert shape(post(delivery)) == (200, *EMPTY)
    good = webhooks.batch_delivery()
    assert shape(post(good)) == (200, *EMPTY)
    assert "Set-Cookie" not in post(good)


@pytest.mark.parametrize(
    "signature",
    [
        None,
        "",
        "sha256=",
        "sha256=" + "0" * 64,
        "sha256=" + "z" * 64,
        "sha256=" + "a" * 63,
        "sha256=" + "a" * 65,
        "sha1=" + "a" * 40,
        "SHA256=" + "a" * 64,
        " sha256=" + "a" * 64,
        "a" * 64,
    ],
)
def test_a_missing_malformed_or_wrong_signature_is_a_bare_401(signature: str | None) -> None:
    body = webhooks.encode(webhooks.envelope(webhooks.value()))
    delivery = webhooks.deliver(body, secret=None, signature=signature)
    assert shape(post(delivery)) == (401, *EMPTY)


def test_a_wrong_secret_or_a_tampered_body_is_the_same_401() -> None:
    body = webhooks.encode(webhooks.envelope(webhooks.value()))
    wrong = webhooks.deliver(body, secret="otro-secreto-que-no-es-de-la-app-0000")  # noqa: S106
    signed = webhooks.deliver(body)
    tampered = webhooks.Delivery(body + b" ", signed.headers)  # la firma es de otro cuerpo
    assert {shape(post(d)) for d in (wrong, tampered)} == {(401, *EMPTY)}


def test_a_right_hmac_in_the_wrong_form_is_still_a_401() -> None:
    """La forma es parte de la firma: sin el prefijo, con basura detrás o con un blanco delante."""
    body = webhooks.encode(webhooks.envelope(webhooks.value()))
    right = webhooks.sign(body)
    assert right.startswith("sha256=") and shape(post(webhooks.deliver(body)))[0] == 200
    for form in (
        right[7:],
        right + "00",
        right + "\n",
        " " + right,
        right.replace("sha256=", "sha256= "),
    ):
        assert shape(post(webhooks.deliver(body, secret=None, signature=form))) == (401, *EMPTY), (
            form
        )


def test_nothing_is_parsed_until_the_signature_is_valid(monkeypatch: pytest.MonkeyPatch) -> None:
    def never(body: bytes) -> None:
        raise AssertionError("se parseó un cuerpo sin firma válida")

    monkeypatch.setattr(meta_webhook, "parse", never)
    body = webhooks.encode(webhooks.envelope(webhooks.value()))
    for delivery in (webhooks.deliver(body, secret=None), webhooks.deliver(body, secret="x" * 40)):  # noqa: S106
        assert shape(post(delivery))[0] == 401


def test_every_secret_is_compared_even_after_a_match_and_even_with_a_bad_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = spy_compare(monkeypatch)
    body = webhooks.encode(webhooks.envelope(webhooks.value()))
    for delivery in (
        webhooks.deliver(body),  # el primero vale
        webhooks.deliver(body, secret=OLD_SECRET),  # el segundo vale
        webhooks.deliver(body, secret=None, signature="mala"),  # mal formada
        webhooks.deliver(body, secret=None),  # ausente
    ):
        calls.clear()
        post(delivery)
        assert len(calls) == 2, delivery.headers  # tantas comparaciones como secretos


def test_a_body_over_the_cap_is_413_and_exactly_the_cap_is_read() -> None:
    padding = b" " * (BODY_MAX - len(webhooks.encode(webhooks.envelope(webhooks.value()))))
    exact = webhooks.encode(webhooks.envelope(webhooks.value())) + padding
    assert len(exact) == BODY_MAX and shape(post(webhooks.deliver(exact))) == (200, *EMPTY)
    assert shape(post(webhooks.deliver(exact + b" ")))[0] == 413
    assert shape(post(webhooks.deliver(b"x" * (BODY_MAX + 1), secret=None)))[0] == 413


class Counting(io.BytesIO):
    """Un flujo que cuenta lo que se le pide leer."""

    asked = 0

    def read(self, size: int | None = -1) -> bytes:
        self.asked += size if size is not None and size > 0 else 10**9
        return super().read(size)


def raw_post(stream: Counting, length: int | None = None) -> HttpRequest:
    request = HttpRequest()
    request.method = "POST"
    request._stream = stream  # lo que lee `request.read`
    if length is not None:
        request.META["CONTENT_LENGTH"] = str(length)
    return request


def test_it_never_reads_more_than_the_cap_plus_one_and_nothing_if_the_length_says_so() -> None:
    stream = Counting(b"x" * (3 * BODY_MAX))
    assert webhook(raw_post(stream)).status_code == 413 and stream.asked == BODY_MAX + 1
    declared = Counting(b"x")
    assert webhook(raw_post(declared, BODY_MAX + 1)).status_code == 413 and declared.asked == 0


def test_a_route_that_is_not_for_the_api_has_no_csrf_and_no_session() -> None:
    client = Client(enforce_csrf_checks=True)
    response = post(webhooks.text_delivery(), client)
    assert response.status_code == 200 and not response.cookies
    assert Client().put(URL, b"{}").status_code == 405


@pytest.mark.parametrize(
    "body",
    [
        *(b for k, b in webhooks.malformed().items() if k not in ITEM_LEVEL),
        b"\xff\xfe\x00", b"[" * 200_000, b'{"a":' * 5000, b" " * 10,
    ],
)  # fmt: skip
def test_a_signed_body_that_is_not_the_envelope_is_a_uniform_200_and_stores_nothing(
    body: bytes, caplog: pytest.LogCaptureFixture, migrator: DB
) -> None:
    caplog.set_level(logging.DEBUG)
    assert shape(post(webhooks.deliver(body))) == (200, *EMPTY)
    assert "meta.webhook.invalid" in caplog.text and stored(migrator) == 0


def test_the_signature_is_checked_before_anything_is_parsed(
    caplog: pytest.LogCaptureFixture, migrator: DB
) -> None:
    caplog.set_level(logging.DEBUG)
    assert shape(post(webhooks.deliver(b"[" * 200_000, secret=None)))[0] == 401
    assert "meta.webhook" not in caplog.text and stored(migrator) == 0  # ni se miró


def message(body: str = "Hola", wamid: str = "wamid.A", **over: Any) -> dict[str, Any]:
    return webhooks.text_message(body, wamid=wamid) | over


def delivery_of(*changes: Any, waba: str = webhooks.WABA_ID) -> bytes:
    return webhooks.encode(
        {"object": "whatsapp_business_account", "entry": [{"id": waba, "changes": list(changes)}]}
    )


def messages_change(*rows: dict[str, Any], **kw: Any) -> dict[str, Any]:
    return {"field": "messages", "value": webhooks.value(messages=list(rows), **kw)}


def test_a_delivery_splits_into_one_item_per_logical_unit_with_a_dedup_key() -> None:
    parsed = parse(webhooks.batch_delivery().body)
    assert [(i.kind, i.key) for i in parsed.items] == [
        ("message", "wa:msg:wamid.SIMIN000010"),
        ("message", "wa:msg:wamid.SIMIN000011"),
        ("message", "wa:msg:wamid.SIMIN000012"),
        ("status", "wa:status:wamid.SIMOUT000001:delivered"),
    ] and parsed.rejected == []
    assert {(i.waba_id, i.phone_number_id) for i in parsed.items} == {
        (webhooks.WABA_ID, webhooks.PHONE_NUMBER_ID)
    }
    account = {"field": "account_update", "value": {"event": "x"}}
    [other] = parse(delivery_of(account)).items
    assert other.kind == "other" and other.phone_number_id is None
    assert other.key == parse(delivery_of(account)).items[0].key  # el mismo cambio, la misma clave


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        (b"x", "json"),
        (webhooks.encode([]), "object"),
        (webhooks.encode({"object": "page", "entry": []}), "object"),
        (webhooks.encode({"object": "whatsapp_business_account", "entry": []}), "entry"),
        (webhooks.encode({"object": "whatsapp_business_account", "entry": "x"}), "entry"),
        (webhooks.encode({"object": "whatsapp_business_account", "entry": [{}] * 21}), "entry"),
        (
            webhooks.encode(
                {
                    "object": "whatsapp_business_account",
                    "entry": [],
                    "a": [[[[[[[[[[[[[[[[[[1]]]]]]]]]]]]]]]]]],
                }
            ),
            "object",
        ),  # noqa: E501
    ],
)
def test_only_what_is_not_an_envelope_invalidates_the_whole_body(body: bytes, reason: str) -> None:
    with pytest.raises(Invalid) as caught:
        parse(body)
    assert caught.value.reason == reason


def test_what_is_wrong_with_one_part_discards_only_that_part_never_the_batch() -> None:
    good = messages_change(message(wamid="wamid.GOOD"))
    bad_text = messages_change(
        message(wamid="wamid.BAD", text={"body": 5}), message(wamid="wamid.OK")
    )
    no_number = {"field": "messages", "value": {"messages": [message(wamid="wamid.NO")]}}
    parsed = parse(delivery_of(good, bad_text, no_number, {"field": "A", "value": {}}, "x", {}))
    assert [i.key for i in parsed.items] == ["wa:msg:wamid.GOOD", "wa:msg:wamid.OK"]
    assert parsed.rejected == ["text", "phone_number_id", "change", "change", "change"]
    entries = {"object": "whatsapp_business_account", "entry": [
        {"id": "abc", "changes": [good]}, "x", {"id": webhooks.WABA_ID, "changes": []},
        {"id": webhooks.WABA_ID, "changes": [good]},
    ]}  # fmt: skip
    parsed = parse(webhooks.encode(entries))
    assert [i.key for i in parsed.items] == ["wa:msg:wamid.GOOD"]
    assert parsed.rejected == ["waba", "waba", "changes"]


@pytest.mark.parametrize("number", ["abc", "123", "1" * 31, "", 5, None])
def test_the_phone_number_id_of_a_messages_change_is_a_numeric_id(number: Any) -> None:
    change = {
        "field": "messages",
        "value": webhooks.value(messages=[message()], phone_number_id=number),
    }
    parsed = parse(delivery_of(change))
    assert parsed.items == [] and parsed.rejected == ["phone_number_id"]


@pytest.mark.parametrize(
    "fields",
    [
        {"id": "x" * 201}, {"id": ""}, {"id": 5}, {"id": "\ud800"}, {"type": 5}, {"type": "a\nb"},
        {"text": {"body": 5}}, {"text": {"body": ""}}, {"text": {"body": "x" * 8193}},
        {"text": "hola"}, {"text": None},
    ],
)  # fmt: skip
def test_a_bad_message_is_discarded_and_the_next_one_still_counts(fields: dict[str, Any]) -> None:
    parsed = parse(delivery_of(messages_change(message(**fields), message(wamid="wamid.NEXT"))))
    assert [i.key for i in parsed.items] == ["wa:msg:wamid.NEXT"] and len(parsed.rejected) == 1


def test_item_and_row_counts_are_bounded() -> None:
    many = [message(wamid=f"wamid.{n}") for n in range(100)]
    with pytest.raises(Invalid) as caught:
        parse(delivery_of(*[messages_change(*many)] * 6))
    assert caught.value.reason == "items"
    parsed = parse(delivery_of(messages_change(*many, message(wamid="wamid.101"))))
    assert parsed.items == [] and parsed.rejected == ["messages"]  # un cambio con más de 100 filas


@pytest.mark.parametrize(
    "text",
    [
        "hola\nqué tal", "a\tb", "a\u00a0b", "👨\u200d👩\u200d👧", "a\u200fb", "línea\r\n",
        "x" * 4096,
    ],
)  # fmt: skip
def test_the_text_a_customer_writes_is_never_a_reason_to_drop_the_batch(text: str) -> None:
    """Saltos de línea, tabuladores, espacios duros y uniones de emoji son texto normal."""
    [item] = parse(delivery_of(messages_change(message(text)))).items
    assert item.payload["text"]["body"] == text


def test_the_dedup_key_has_one_reading_whatever_characters_an_id_carries() -> None:
    def key(wamid: str, state: str) -> str:
        found = webhooks.status(wamid, "delivered") | {"id": wamid, "status": state}
        change = {"field": "messages", "value": webhooks.value(statuses=[found])}
        return parse(delivery_of(change)).items[0].key

    assert key("a:b", "c") != key("a", "b:c") and len({key("a b", "c"), key("a%20b", "c")}) == 2
    assert key("wamid.HBgM=-_.x==", "delivered") == "wa:status:wamid.HBgM%3D-_.x%3D%3D:delivered"
    assert key("a\nb/ñ", "Sent") == "wa:status:a%0Ab%2F%C3%B1:Sent"  # nada se pierde por raro
    assert message_key("a:b") != message_key("a%3Ab")


def message_key(wamid: str) -> str:
    return parse(delivery_of(messages_change(message(wamid=wamid)))).items[0].key


def test_no_output_carries_the_secrets_the_token_the_signature_or_the_body(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    delivery = webhooks.text_delivery("Texto privado del cliente")
    bad = webhooks.deliver(delivery.body, secret=None, signature="sha256=" + "b" * 64)
    responses = [post(delivery), post(bad), subscribe(), subscribe(**{"hub.verify_token": "x"})]
    signature = delivery.headers[webhooks.SIGNATURE_HEADER]
    outputs = caplog.text + "".join(r.content.decode() + str(r.headers) for r in responses)
    for secret in (TEST_APP_SECRET, OLD_SECRET, VERIFY, signature, "Texto privado", "5190000000"):
        assert secret not in outputs, secret


@pytest.mark.parametrize("body", [b'{"x": NaN}', b'{"x": Infinity}', b'{"x": -Infinity}'])
def test_python_only_json_constants_are_not_json(body: bytes) -> None:
    with pytest.raises(Invalid, match="json"):
        parse(body)


def test_a_content_length_that_is_not_ascii_digits_is_not_a_server_error() -> None:
    request = HttpRequest()
    request.method = "POST"
    request.META["CONTENT_LENGTH"] = "²"
    request._stream = io.BytesIO(b"{}")
    assert webhook(request).status_code == 401

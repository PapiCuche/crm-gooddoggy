"""F7-03a: el webhook público de Meta (`apps.conversations.meta_webhook`, ADR-029 §3): el GET de
la suscripción, la firma del POST, el tope del cuerpo y el esquema, con el simulador de Meta
(`tests/meta_sim`). Sin base de datos: la ruta no escribe nada todavía."""

import hmac
import io
import logging
from typing import Any

import pytest
from django.http import HttpRequest
from django.test import Client

from apps.conversations import meta_webhook
from apps.conversations.meta_webhook import BODY_MAX, Invalid, parse, webhook
from config.settings import base
from tests.meta_sim import webhooks
from tests.meta_sim.webhooks import TEST_APP_SECRET

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
    [*webhooks.malformed().values(), b"\xff\xfe\x00", b"[" * 200_000, b'{"a":' * 5000, b" " * 10],
)
def test_a_signed_body_that_is_not_what_meta_sends_is_a_uniform_200_never_a_500(
    body: bytes, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    assert shape(post(webhooks.deliver(body))) == (200, *EMPTY)
    assert "meta.webhook.invalid" in caplog.text


def test_the_signature_is_checked_before_anything_is_parsed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    assert shape(post(webhooks.deliver(b"[" * 200_000, secret=None)))[0] == 401
    assert "meta.webhook" not in caplog.text  # ni siquiera se miró


def valid(**over: Any) -> dict[str, Any]:
    return {**webhooks.envelope(webhooks.value(messages=[webhooks.text_message("Hola")])), **over}


def test_a_delivery_splits_into_one_item_per_logical_unit_with_a_dedup_key() -> None:
    items = parse(webhooks.batch_delivery().body)
    assert [(i.kind, i.key) for i in items] == [
        ("message", "wa:msg:wamid.SIMIN000010"),
        ("message", "wa:msg:wamid.SIMIN000011"),
        ("message", "wa:msg:wamid.SIMIN000012"),
        ("status", "wa:status:wamid.SIMOUT000001:delivered"),
    ]
    assert {(i.waba_id, i.phone_number_id) for i in items} == {
        (webhooks.WABA_ID, webhooks.PHONE_NUMBER_ID)
    }
    other = {"object": webhooks.envelope()["object"], "entry": [
        {"id": webhooks.WABA_ID, "changes": [{"field": "account_update", "value": {"event": "x"}}]}
    ]}  # fmt: skip
    [account] = parse(webhooks.encode(other))
    assert account.kind == "other" and account.phone_number_id is None
    assert account.key == parse(webhooks.encode(other))[0].key  # el mismo cambio, la misma clave


def broken(change: Any) -> bytes:
    return webhooks.encode(change(valid()))


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        (lambda e: {**e, "object": "page"}, "object"),
        (lambda e: {**e, "entry": []}, "entry"),
        (lambda e: {**e, "entry": [e["entry"][0]] * 21}, "entry"),
        (lambda e: {**e, "entry": ["x"]}, "entry"),
        (lambda e: [e], "object"),
        (lambda e: {**e, "entry": [{**e["entry"][0], "id": "abc"}]}, "waba"),
        (lambda e: {**e, "entry": [{**e["entry"][0], "changes": "x"}]}, "changes"),
        (lambda e: {**e, "entry": [{**e["entry"][0], "changes": []}]}, "changes"),
        (
            lambda e: {**e, "entry": [{**e["entry"][0], "changes": [{"field": "A", "value": {}}]}]},
            "change",
        ),
        (
            lambda e: {
                **e,
                "entry": [{**e["entry"][0], "changes": [{"field": "messages", "value": 1}]}],
            },
            "change",
        ),
        (lambda e: {"deep": [[[[[[[[[[[[[[[[[[[[1]]]]]]]]]]]]]]]]]]]], **e}, "object"),
    ],
)
def test_the_schema_is_strict_about_types_sizes_and_shape(change: Any, reason: str) -> None:
    with pytest.raises(Invalid) as caught:
        parse(broken(change))
    assert caught.value.reason == reason


@pytest.mark.parametrize("number", ["abc", "123", "1" * 31, "", 5, None])
def test_the_phone_number_id_of_a_messages_change_is_a_numeric_id(number: Any) -> None:
    with pytest.raises(Invalid) as caught:
        parse(webhooks.encode(webhooks.envelope(webhooks.value(phone_number_id=number))))
    assert caught.value.reason == "phone_number_id"


def test_ids_texts_and_item_counts_are_bounded() -> None:
    def with_message(**fields: Any) -> bytes:
        message = {**webhooks.text_message("Hola"), **fields}
        return webhooks.encode(webhooks.envelope(webhooks.value(messages=[message])))

    bad = (
        {"id": "x" * 201}, {"id": ""}, {"id": "a\nb"}, {"type": 5}, {"text": {"body": 5}},
        {"text": {"body": "x" * 8193}}, {"text": "hola"},
    )  # fmt: skip
    for fields in bad:
        with pytest.raises(Invalid):
            parse(with_message(**fields))
    many = [webhooks.text_message("x", wamid=f"wamid.{n}") for n in range(100)]
    six_pages = webhooks.encode(webhooks.envelope(*[webhooks.value(messages=many)] * 6))
    with pytest.raises(Invalid) as caught:
        parse(six_pages)
    assert caught.value.reason == "items"
    with pytest.raises(Invalid):
        parse(webhooks.encode(webhooks.envelope(webhooks.value(messages=many + many[:1]))))


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


@pytest.mark.parametrize(
    "text",
    [
        "hola\nqué tal",
        "a\tb",
        "a\u00a0b",
        "👨\u200d👩\u200d👧",
        "a\u200fb",
        "línea\r\n",
        "x" * 4096,
    ],
)
def test_the_text_a_customer_writes_is_never_a_reason_to_drop_the_batch(text: str) -> None:
    """Saltos de línea, tabuladores, espacios duros y uniones de emoji son texto normal."""
    body = webhooks.encode(
        webhooks.envelope(webhooks.value(messages=[webhooks.text_message(text)]))
    )
    [item] = parse(body)
    assert item.payload["text"]["body"] == text


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


def test_the_dedup_key_has_one_reading_ids_and_states_never_carry_the_separator() -> None:
    def status_body(wamid: str, state: str) -> bytes:
        found = webhooks.status(wamid, "delivered") | {"id": wamid, "status": state}
        return webhooks.encode(webhooks.envelope(webhooks.value(statuses=[found])))

    for wamid, state in (("a:b", "c"), ("a", "b:c"), ("a b", "sent"), ("a", "Sent"), ("a", "")):
        with pytest.raises(Invalid):
            parse(status_body(wamid, state))
    [first] = parse(status_body("wamid.HBgM=-_.x==", "delivered"))
    [second] = parse(status_body("wamid.HBgM=-_.x==", "read"))
    assert first.key == "wa:status:wamid.HBgM=-_.x==:delivered" and first.key != second.key
    message = webhooks.text_message("Hola", wamid="a:b")
    with pytest.raises(Invalid):
        parse(webhooks.encode(webhooks.envelope(webhooks.value(messages=[message]))))


@pytest.mark.parametrize("text", ["", "x" * 8193, None, 5])
def test_a_text_that_is_empty_too_long_or_not_a_string_is_invalid(text: Any) -> None:
    message = webhooks.text_message("Hola") | {"text": {"body": text}}
    with pytest.raises(Invalid) as caught:
        parse(webhooks.encode(webhooks.envelope(webhooks.value(messages=[message]))))
    assert caught.value.reason == "text"

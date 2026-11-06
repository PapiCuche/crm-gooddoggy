"""Cuerpos de webhook de Meta (WhatsApp Business) y su firma. Todo es ficticio."""

import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import Any

# Evidentemente de prueba, sin la forma de ninguna credencial real. No se usa fuera de tests.
TEST_APP_SECRET = "meta-sim-test-app-secret-not-real"  # noqa: S105 — dato ficticio de test
WABA_ID = "100000000000001"
PHONE_NUMBER_ID = "100000000000002"
DISPLAY_NUMBER = "51900000000"
CUSTOMER = "51900000001"  # `wa_id`: teléfono sin el `+`
SIGNATURE_HEADER = "X-Hub-Signature-256"
UNSUPPORTED_KINDS = ("image", "audio", "video", "document", "sticker", "location", "contacts")


def sign(body: bytes, secret: str = TEST_APP_SECRET) -> str:
    """`sha256=<hex>`: HMAC-SHA256 del cuerpo crudo con el app secret, como lo hace Meta."""
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


@dataclass(frozen=True)
class Delivery:
    """Lo que Meta envía por POST: el cuerpo crudo y sus cabeceras."""

    body: bytes
    headers: dict[str, str]

    @property
    def json(self) -> Any:
        return json.loads(self.body)


def deliver(
    body: bytes, *, secret: str | None = TEST_APP_SECRET, signature: str | None = None
) -> Delivery:
    """Firma `body` con `secret`; `signature` la fuerza (mala), y `secret=None` la omite."""
    headers = {"Content-Type": "application/json"}
    if signature is not None:
        headers[SIGNATURE_HEADER] = signature
    elif secret is not None:
        headers[SIGNATURE_HEADER] = sign(body, secret)
    return Delivery(body, headers)


def encode(payload: Any) -> bytes:
    # ASCII con \uXXXX, como Meta (a confirmar): así falla quien firma el JSON releído.
    return json.dumps(payload, separators=(",", ":")).encode()


def value(
    *, messages: list[dict[str, Any]] | None = None, statuses: list[dict[str, Any]] | None = None,
    phone_number_id: str = PHONE_NUMBER_ID, name: str | None = "Cliente de prueba",
) -> dict[str, Any]:  # fmt: skip
    """El `value` de un cambio `messages`: metadatos del número, el perfil y los ítems."""
    found: dict[str, Any] = {
        "messaging_product": "whatsapp",
        "metadata": {"display_phone_number": DISPLAY_NUMBER, "phone_number_id": phone_number_id},
    }
    senders = {m["from"] for m in messages or []}
    if senders:
        found["contacts"] = [{"profile": {"name": name}, "wa_id": w} for w in sorted(senders)]
    if messages:
        found["messages"] = messages
    if statuses:
        found["statuses"] = statuses
    return found


def envelope(
    *values: dict[str, Any], waba_id: str = WABA_ID, object_: str = "whatsapp_business_account"
) -> dict[str, Any]:
    """El sobre de Meta: una entrada por WABA con un cambio `messages` por cada `value`."""
    return {
        "object": object_,
        "entry": [{"id": waba_id, "changes": [{"field": "messages", "value": v} for v in values]}],
    }


def text_message(
    text: str, *, wamid: str = "wamid.SIMIN000001", sender: str = CUSTOMER, at: int = 1_700_000_000
) -> dict[str, Any]:
    return {
        "from": sender,
        "id": wamid,
        "timestamp": str(at),
        "type": "text",
        "text": {"body": text},
    }


def other_message(
    kind: str, *, wamid: str = "wamid.SIMIN000002", sender: str = CUSTOMER, at: int = 1_700_000_000
) -> dict[str, Any]:
    """Un mensaje de un tipo que el primer corte no soporta, con la forma que Meta le da."""
    inner: dict[str, Any] = {"id": "SIMMEDIA0001", "mime_type": "application/octet-stream"}
    if kind == "location":
        inner = {"latitude": 0.0, "longitude": 0.0}
    elif kind == "contacts":
        inner = {"name": {"formatted_name": "Contacto de prueba"}}
    body = [inner] if kind == "contacts" else inner
    return {"from": sender, "id": wamid, "timestamp": str(at), "type": kind, kind: body}


def reaction(
    target: str, emoji: str = "👍", *, wamid: str = "wamid.SIMIN000003", sender: str = CUSTOMER
) -> dict[str, Any]:
    return {
        "from": sender, "id": wamid, "timestamp": "1700000000", "type": "reaction",
        "reaction": {"message_id": target, "emoji": emoji},
    }  # fmt: skip


def status(
    wamid: str, state: str, *, recipient: str = CUSTOMER, opaque: str | None = None,
    error: tuple[int, str] | None = None, at: int = 1_700_000_100,
) -> dict[str, Any]:  # fmt: skip
    """Un estado de entrega; `failed` lleva `(código, título)` del error."""
    found: dict[str, Any] = {
        "id": wamid,
        "status": state,
        "timestamp": str(at),
        "recipient_id": recipient,
    }
    if opaque is not None:
        found["biz_opaque_callback_data"] = opaque
    if error is not None:
        found["errors"] = [
            {
                "code": error[0],
                "title": error[1],
                "message": error[1],
                "error_data": {"details": error[1]},
            }
        ]
    return found


def text_delivery(text: str = "Hola", **kwargs: Any) -> Delivery:
    return deliver(encode(envelope(value(messages=[text_message(text, **kwargs)]))))


def status_delivery(wamid: str, state: str, **kwargs: Any) -> Delivery:
    return deliver(encode(envelope(value(statuses=[status(wamid, state, **kwargs)]))))


def batch_delivery() -> Delivery:
    """Varios ítems en una entrega: dos textos de dos clientes, un estado y una reacción."""
    first = text_message("Uno", wamid="wamid.SIMIN000010")
    second = text_message("Dos", wamid="wamid.SIMIN000011", sender="51900000002")
    return deliver(encode(envelope(value(
        messages=[first, second, reaction("wamid.SIMOUT000001", wamid="wamid.SIMIN000012")],
        statuses=[status("wamid.SIMOUT000001", "delivered")],
    ))))  # fmt: skip


def malformed() -> dict[str, bytes]:
    """Cuerpos que no son lo que Meta envía; la firma, si se quiere, se pone con `deliver`."""
    good_text = text_message("x")
    good = envelope(value(messages=[good_text]))
    return {
        "not_json": b"esto no es json",
        "empty": b"",
        "array": b"[]",
        "null": b"null",
        "wrong_object": encode({**good, "object": "page"}),
        "entry_not_a_list": encode({"object": "whatsapp_business_account", "entry": "x"}),
        "no_phone_number_id": encode(envelope({"messages": [text_message("x")]})),
        "text_not_a_string": encode(envelope(value(messages=[{**good_text, "text": {"body": 5}}]))),
    }  # fmt: skip

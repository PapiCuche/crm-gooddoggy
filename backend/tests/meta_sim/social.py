"""Webhooks de Messenger (`page`) e Instagram (`instagram`) para la serie F7b (ADR-035 §9).

Las formas siguen lo que ADR-035 cita de Meta (fuentes leídas el 2026-11-13): `entry[].messaging[]`
con `sender`, `recipient` y `timestamp` [S7] [S18]; los campos del mensaje (`mid`, `text`,
`attachments`, `reply_to`, `is_echo`, `is_deleted`, `is_unsupported`) [S7]; el eco con `app_id` y
`metadata` [S19]; `delivery` y `read` [S17]; `standby` [S23]. Instagram con Facebook Login (§2):
`entry.id` es la cuenta profesional y el envío va por la página. La firma es la de `webhooks.sign`
(el mismo app secret para los tres objetos, §2). Todos los ids son ficticios y evidentes.
"""

from dataclasses import dataclass
from typing import Any

from tests.meta_sim.webhooks import MEDIA_HOST, Delivery, deliver, encode

PAGE_ID = "100000000000101"  # la página de Facebook (Messenger y la vinculada a Instagram)
IG_ACCOUNT_ID = "100000000000102"  # la cuenta profesional de Instagram
PSID = "100000000000201"  # el cliente en Messenger: id por página [S27]
IGSID = "100000000000202"  # el cliente en Instagram: id por app y cuenta [S1]
APP_ID = "100000000000301"  # nuestra app de Meta (`META_APP_ID`, §6)
OTHER_APP_ID = "100000000000302"  # otra app de la página, o la bandeja de Business Suite
OTHER_ACCOUNT_ID = "100000000000999"  # una página o cuenta que no es la del evento
STORY_ID = "100000000000401"
STORY_URL = f"https://{MEDIA_HOST}/sim/story/{STORY_ID}"  # media que no se guarda [S13]
AT = 1_700_000_000_000  # `timestamp` en milisegundos [S18]


@dataclass(frozen=True)
class Channel:
    object: str  # el `object` del cuerpo firmado
    account: str  # `entry.id`: la página o la cuenta profesional
    customer: str  # el PSID o el IGSID
    attachments: tuple[str, ...]  # los `attachments[].type` que ADR-035 enumera para el canal


MESSENGER = Channel("page", PAGE_ID, PSID, ("image", "audio", "video", "file", "fallback"))
# En Instagram `image` incluye gif y sticker [S7].
INSTAGRAM = Channel(
    "instagram", IG_ACCOUNT_ID, IGSID,
    ("image", "audio", "video", "file", "share", "ig_post", "ig_reel", "story_mention"),
)  # fmt: skip
CHANNELS = (MESSENGER, INSTAGRAM)


def event(
    channel: Channel, *, echo: bool = False, at: int = AT, account: str | None = None,
    **body: Any,
) -> dict[str, Any]:  # fmt: skip
    """Un evento de `messaging[]`: entrante del cliente a la cuenta, o al revés si es un eco [S7].
    `account` cambia el id de la cuenta dentro del evento (para el cruce con `entry.id`, §4)."""
    us, them = {"id": account or channel.account}, {"id": channel.customer}
    sender, recipient = (us, them) if echo else (them, us)
    return {"sender": sender, "recipient": recipient, "timestamp": at, **body}


def message(
    channel: Channel, *, mid: str = "m_SIMIN000001", echo: bool = False, at: int = AT,
    account: str | None = None, **fields: Any,
) -> dict[str, Any]:  # fmt: skip
    return event(channel, echo=echo, at=at, account=account, message={"mid": mid, **fields})


def text(channel: Channel, body: str = "Hola", **kwargs: Any) -> dict[str, Any]:
    return message(channel, text=body, **kwargs)


def attachment(channel: Channel, kind: str, **kwargs: Any) -> dict[str, Any]:
    """Un adjunto entrante: `attachments[].type` y `payload.url` del CDN de Meta [S7] [S18]."""
    if kind not in channel.attachments:
        raise ValueError(f"{channel.object} no envía adjuntos {kind!r}")
    url = STORY_URL if kind == "story_mention" else f"https://{MEDIA_HOST}/sim/{kind}"
    return message(channel, attachments=[{"type": kind, "payload": {"url": url}}], **kwargs)


def story_mention(**kwargs: Any) -> dict[str, Any]:
    """Instagram: el cliente mencionó a la cuenta en una historia (con la URL a quitar, §4)."""
    return attachment(INSTAGRAM, "story_mention", **kwargs)


def story_reply(body: str = "Me encanta", **kwargs: Any) -> dict[str, Any]:
    """Instagram: respuesta a una historia, `reply_to.story` con `url` e `id` [S7]."""
    return text(INSTAGRAM, body, reply_to={"story": {"url": STORY_URL, "id": STORY_ID}}, **kwargs)


def reply(channel: Channel, target: str, body: str = "Sí", **kwargs: Any) -> dict[str, Any]:
    """Una respuesta citando otro mensaje: `reply_to.mid` [S7]."""
    return text(channel, body, reply_to={"mid": target}, **kwargs)


def echo(
    channel: Channel, body: str = "Respuesta", *, mid: str = "m_SIMOUT000001",
    app_id: str | None = APP_ID, metadata: str | None = None, **kwargs: Any,
) -> dict[str, Any]:  # fmt: skip
    """Lo que la cuenta envió: `is_echo`, el `app_id` numérico de la app que lo envió y su
    `metadata` [S19]. `app_id=None` lo omite (el eco de Instagram, SIN VERIFICAR 12)."""
    fields: dict[str, Any] = {"is_echo": True, "text": body}
    if app_id is not None:
        fields["app_id"] = int(app_id)
    if metadata is not None:
        fields["metadata"] = metadata
    return message(channel, mid=mid, echo=True, **fields, **kwargs)


def flagged(channel: Channel, flag: str, **kwargs: Any) -> dict[str, Any]:
    """Un mensaje sin contenido útil: `is_unsupported` o `is_deleted` [S7]."""
    if flag not in ("is_unsupported", "is_deleted"):
        raise ValueError(flag)
    fields: dict[str, Any] = {flag: True}
    return message(channel, **fields, **kwargs)


def reaction(channel: Channel, target: str, emoji: str = "❤️", **kwargs: Any) -> dict[str, Any]:
    return event(channel, reaction={"mid": target, "action": "react", "emoji": emoji}, **kwargs)


def read(channel: Channel, *, mid: str = "m_SIMOUT000001", watermark: int = AT) -> dict[str, Any]:
    """Messenger marca lo leído con una marca de agua; Instagram, con el `mid` (§5)."""
    return event(channel, read={"mid": mid} if channel is INSTAGRAM else {"watermark": watermark})


def delivery(mids: tuple[str, ...] = ("m_SIMOUT000001",), watermark: int = AT) -> dict[str, Any]:
    """Messenger: `message_deliveries`, con los `mids` y la marca de agua [S17]."""
    return event(MESSENGER, delivery={"mids": list(mids), "watermark": watermark})


def envelope(
    channel: Channel, *events: dict[str, Any], entry_id: str | None = None,
    field: str = "messaging", object_: str | None = None,
) -> dict[str, Any]:  # fmt: skip
    """El sobre: una entrada por evento (Meta pone un solo evento en cada `messaging` [S18] y
    agrupa en lotes de entradas [S5]). `field="standby"` es el hilo que otra app controla [S23]."""
    entries = [
        {"id": entry_id or channel.account, "time": e["timestamp"], field: [e]} for e in events
    ]
    return {"object": object_ or channel.object, "entry": entries}


def signed(channel: Channel, *events: dict[str, Any], **kwargs: Any) -> Delivery:
    return deliver(encode(envelope(channel, *events, **kwargs)))


def batch(channel: Channel) -> Delivery:
    """Un lote desordenado: el `timestamp` decide el orden, no la posición [S17]."""
    return signed(channel, *(
        text(channel, f"Mensaje {n}", mid=f"m_SIMIN{n:06d}", at=AT + n * 1000) for n in (3, 1, 2)
    ))  # fmt: skip


def catalogue(channel: Channel) -> dict[str, Delivery]:
    """Cada evento de ADR-035 §9 para el canal, firmado como Meta: lo recorren las pruebas del
    analizador de F7b-04."""
    found = {
        "text": signed(channel, text(channel)),
        "reply": signed(channel, reply(channel, "m_SIMOUT000001")),
        "own_echo": signed(channel, echo(channel, metadata="sim-echo-token")),
        "foreign_echo": signed(channel, echo(channel, app_id=OTHER_APP_ID)),
        "read": signed(channel, read(channel)),
        "standby": signed(channel, text(channel), field="standby"),
        "is_unsupported": signed(channel, flagged(channel, "is_unsupported")),
        "is_deleted": signed(channel, flagged(channel, "is_deleted")),
        "reaction": signed(channel, reaction(channel, "m_SIMOUT000001")),
        "batch": batch(channel),
        "account_mismatch": signed(channel, text(channel, account=OTHER_ACCOUNT_ID)),
        "unknown_object": signed(channel, text(channel), object_="user"),
    }
    found |= {
        f"attachment_{k}": signed(channel, attachment(channel, k)) for k in channel.attachments
    }
    if channel is MESSENGER:
        found["delivery"] = signed(channel, delivery())
    else:
        found["story_reply"] = signed(channel, story_reply())
    return found

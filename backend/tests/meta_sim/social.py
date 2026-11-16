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
STICKER_ID = 100000000000501
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
    channel: Channel, *, echo: bool = False, at: int | None = AT, account: str | None = None,
    **body: Any,
) -> dict[str, Any]:  # fmt: skip
    """Un evento de `messaging[]`: entrante del cliente a la cuenta, o al revés si es un eco [S7].
    `account` cambia el id de la cuenta dentro del evento (para el cruce con `entry.id`, §4);
    `at=None` omite `timestamp` (la entrega de Messenger no lo trae)."""
    us, them = {"id": account or channel.account}, {"id": channel.customer}
    sender, recipient = (us, them) if echo else (them, us)
    found: dict[str, Any] = {"sender": sender, "recipient": recipient}
    if at is not None:
        found["timestamp"] = at
    return found | body


def message(
    channel: Channel, *, mid: str = "m_SIMIN000001", echo: bool = False, at: int = AT,
    account: str | None = None, **fields: Any,
) -> dict[str, Any]:  # fmt: skip
    return event(channel, echo=echo, at=at, account=account, message={"mid": mid, **fields})


def text(channel: Channel, body: str = "Hola", **kwargs: Any) -> dict[str, Any]:
    return message(channel, text=body, **kwargs)


def attachment(channel: Channel, kind: str, **kwargs: Any) -> dict[str, Any]:
    """Un adjunto entrante: `attachments[].type` y `payload.url` del CDN de Meta [S7] [S18]. El
    `fallback` de Messenger va sin `payload`: Meta avisa de que puede llegar así [S18]."""
    if kind not in channel.attachments:
        raise ValueError(f"{channel.object} no envía adjuntos {kind!r}")
    url = STORY_URL if kind == "story_mention" else f"https://{MEDIA_HOST}/sim/{kind}"
    found: dict[str, Any] = (
        {"type": kind} if kind == "fallback" else {"type": kind, "payload": {"url": url}}
    )
    return message(channel, attachments=[found], **kwargs)


def sticker(**kwargs: Any) -> dict[str, Any]:
    """Messenger: un sticker llega como `image` con `sticker_id` en el `payload` [S18]."""
    found = {"url": f"https://{MEDIA_HOST}/sim/sticker", "sticker_id": STICKER_ID}
    return message(MESSENGER, attachments=[{"type": "image", "payload": found}], **kwargs)


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
    `metadata` [S19], documentados para Messenger. En Instagram los dos son SIN VERIFICAR 11 y 12:
    `app_id=None` lo omite y `metadata=None` también."""
    fields: dict[str, Any] = {"is_echo": True, "text": body}
    if app_id is not None:
        fields["app_id"] = int(app_id)
    if metadata is not None:
        fields["metadata"] = metadata
    return message(channel, mid=mid, echo=True, **fields, **kwargs)


def flagged(channel: Channel, flag: str, **kwargs: Any) -> dict[str, Any]:
    """Un mensaje sin contenido útil: `is_unsupported` o `is_deleted`, documentados para Instagram
    [S7] y no para Messenger (allí el simulador los ofrece para probar la defensa)."""
    if flag not in ("is_unsupported", "is_deleted"):
        raise ValueError(flag)
    fields: dict[str, Any] = {flag: True}
    return message(channel, **fields, **kwargs)


def reaction(
    channel: Channel, target: str, emoji: str | None = "❤️", **kwargs: Any
) -> dict[str, Any]:
    """`react` con `reaction` y `emoji` [S7]; `emoji=None` es `unreact`, sin los dos."""
    found = {"mid": target, "action": "react", "reaction": "love", "emoji": emoji}
    if emoji is None:
        found = {"mid": target, "action": "unreact"}
    return event(channel, reaction=found, **kwargs)


def read(channel: Channel, *, mid: str = "m_SIMOUT000001", watermark: int = AT) -> dict[str, Any]:
    """Messenger marca lo leído con una marca de agua; Instagram, con el `mid` (§5)."""
    return event(channel, read={"mid": mid} if channel is INSTAGRAM else {"watermark": watermark})


def delivery(
    mids: tuple[str, ...] | None = ("m_SIMOUT000001",), watermark: int = AT
) -> dict[str, Any]:
    """Messenger: `message_deliveries`, sin `timestamp`; `watermark` siempre y `mids` puede faltar
    (`mids=None`) [S17]."""
    found: dict[str, Any] = {"watermark": watermark}
    if mids is not None:
        found["mids"] = list(mids)
    return event(MESSENGER, at=None, delivery=found)


def envelope(
    channel: Channel, *events: dict[str, Any], entry_id: str | None = None,
    field: str = "messaging", object_: str | None = None, time: int | None = None,
    grouped: bool = False,
) -> dict[str, Any]:  # fmt: skip
    """El sobre: una entrada por evento (Meta pone un solo evento en cada `messaging` [S18] y
    agrupa en lotes de entradas [S5]); `grouped` los pone todos en una entrada, por si Meta lo
    hace. `field="standby"` es el hilo que otra app controla [S23]. `entry.time` sale de `time`,
    del `timestamp` del primer evento o de la marca de agua de la entrega."""

    def when(e: dict[str, Any]) -> int:
        return time or e.get("timestamp") or e.get("delivery", {}).get("watermark") or AT

    account = entry_id or channel.account
    groups = [list(events)] if grouped else [[e] for e in events]
    entries = [{"id": account, "time": when(g[0]), field: g} for g in groups]
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
        # Con `app_id` y `metadata`: documentado en Messenger [S19]; SIN VERIFICAR 11/12 en IG.
        "own_echo": signed(channel, echo(channel, metadata="sim-echo-token")),
        "foreign_echo": signed(channel, echo(channel, app_id=OTHER_APP_ID)),
        "read": signed(channel, read(channel)),
        "standby": signed(channel, text(channel), field="standby"),
        "is_unsupported": signed(channel, flagged(channel, "is_unsupported")),
        "is_deleted": signed(channel, flagged(channel, "is_deleted")),
        "reaction": signed(channel, reaction(channel, "m_SIMOUT000001")),
        "unreact": signed(channel, reaction(channel, "m_SIMOUT000001", None)),
        "batch": batch(channel),
        "grouped": signed(channel, text(channel), text(channel, mid="m_SIMIN000002"), grouped=True),
        "account_mismatch": signed(channel, text(channel, account=OTHER_ACCOUNT_ID)),
        "echo_account_mismatch": signed(channel, echo(channel, account=OTHER_ACCOUNT_ID)),
        "unknown_object": signed(channel, text(channel), object_="user"),
    }
    found |= {
        f"attachment_{k}": signed(channel, attachment(channel, k)) for k in channel.attachments
    }
    if channel is MESSENGER:
        found["delivery"] = signed(channel, delivery())
        found["delivery_no_mids"] = signed(channel, delivery(None))
        found["sticker"] = signed(channel, sticker())
    else:
        found["story_reply"] = signed(channel, story_reply())
        # SIN VERIFICAR 11 y 12: el eco propio de Instagram puede no traer `app_id`.
        found["own_echo_no_app_id"] = signed(
            channel, echo(channel, app_id=None, metadata="sim-echo-token")
        )
    return found

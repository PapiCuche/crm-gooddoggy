"""El adaptador de Messenger e Instagram (F7b-05a; ADR-035 §6): `POST /<versión>/<PAGE_ID>/messages`
por `core.http` con la versión de `META_GRAPH_VERSION` (D-F7-08). Instagram con Facebook Login envía
por la página vinculada (`external_parent_id`); Messenger, por su página.

El destinatario es el PSID o el IGSID de la identidad de la conversación (`Claim.recipient`). Con la
ventana de 24 h cerrada, solo sale lo que la política marcó `HUMAN_AGENT_TAG` (`messaging_type =
MESSAGE_TAG`, `tag = HUMAN_AGENT`); lo demás va como `RESPONSE`. Medios salientes: después
(ADR-035 §11), así que un medio no sale.

Meta no deduplica: como en WhatsApp, un fallo antes de enviar es `TemporaryChannelError` y un tiempo
agotado o un corte es `AmbiguousChannelError`. El POST no repite una lectura ni espera un
`Retry-After` (urllib3 no reintenta un POST salvo al conectar; una prueba lo fija) y corre fuera de
toda transacción. Un error de Meta se clasifica por `code` y `error_subcode` (`classify`), **nunca**
por el estado HTTP (la API de páginas manda el `190` con 400 o con 401): el texto de Meta nunca se
guarda ni se registra.
"""

import re
from typing import Any

from django.conf import settings

from apps.conversations.channels import (
    AmbiguousChannelError,
    Claim,
    SendResult,
    TemporaryChannelError,
)
from apps.conversations.models import EXTERNAL_ID, META_ID
from apps.conversations.whatsapp import admit, field_at
from core import http
from core.observability.logging import get_logger

logger = get_logger(__name__)
MID = r"^[!-~]{1,200}$"  # el `message_id` de Meta: ASCII visible (`messages.external_message_id`)
WINDOW = (2018278, 2534022)  # los subcódigos de `10` que dicen «fuera de la ventana» [S26]
ACCESS_OFF = (200, 2534041)  # el titular apagó «Allow access to messages» (Instagram)
TOO_FAST = (4, 17, 32, 613)  # límites de tasa de la app, de la página y de la llamada: pasajeros
SERVER = (1, 2)  # error desconocido o servicio no disponible de Meta: pudo haberse enviado
CODES = {190: "TOKEN_INVALID", 551: "RECIPIENT_UNREACHABLE"}
TEXT_LIMITS = {"MESSENGER": ("chars", 2000), "INSTAGRAM": ("bytes", 1000)}  # ADR-035 §7


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def classify(code: Any, subcode: Any) -> str:
    """El código propio de un error de la API de páginas (ADR-035 §6): un `10` o un `200` sin el
    subcódigo de §6 es `PROVIDER_REJECTED`; sin `code` legible, `PROVIDER_ERROR` (no se sabe)."""
    code, subcode = _int(code), _int(subcode)
    if code is None or code in SERVER:
        return "PROVIDER_ERROR"
    if code in TOO_FAST:
        return "RATE_LIMITED"
    if code == 10 and subcode in WINDOW:
        return "WINDOW_CLOSED"
    if (code, subcode) == ACCESS_OFF:
        return "MESSAGES_ACCESS_OFF"
    return CODES.get(code, "PROVIDER_REJECTED")


def fits(platform: str, text: str) -> bool:
    """¿Cabe el texto en el canal? Messenger cuenta caracteres; Instagram, bytes UTF-8."""
    unit, limit = TEXT_LIMITS[platform]
    return (len(text.encode()) if unit == "bytes" else len(text)) <= limit


class PageAdapter:
    def send(self, claim: Claim) -> SendResult:
        if not settings.META_ENABLED or claim.credential is None:
            return SendResult(False, reason="CHANNEL_UNAVAILABLE")
        if claim.media is not None or not fits(claim.platform, claim.body):
            return SendResult(False, reason="ADAPTER_REJECTED")
        if not re.fullmatch(EXTERNAL_ID, claim.recipient):  # sin identidad en esta cuenta
            return SendResult(False, reason="RECIPIENT_UNREACHABLE")
        page = claim.parent_id if claim.platform == "INSTAGRAM" else claim.phone_number_id
        if page is None or not re.fullmatch(META_ID, page):
            raise ValueError("identificador de Meta no válido")  # nunca cambia la ruta de la URL
        url = f"https://{settings.META_GRAPH_HOST}/{settings.META_GRAPH_VERSION}/{page}/messages"
        body: dict[str, Any] = {
            "recipient": {"id": claim.recipient}, "messaging_type": "RESPONSE",
            "message": {"text": claim.body},
        }  # fmt: skip
        if claim.human_agent:  # solo una persona, con la función aprobada (la política lo decide)
            body |= {"messaging_type": "MESSAGE_TAG", "tag": "HUMAN_AGENT"}
        token = admit(claim)
        if isinstance(token, SendResult):
            return token
        try:
            reply = http.request(
                "POST", url, headers={"Authorization": f"Bearer {token}"}, json=body
            )
        except Exception as error:  # se clasifica; el texto del error puede llevar la URL
            if http.never_connected(error):
                raise TemporaryChannelError from None
            logger.warning("channel.page_send_ambiguous", message_id=str(claim.message_id))
            raise AmbiguousChannelError from None
        finally:
            del token
        if reply.status == 200:
            mid = field_at(reply.data, "message_id")
            if isinstance(mid, str) and re.fullmatch(MID, mid):
                return SendResult(True, mid)
            raise AmbiguousChannelError  # Meta lo aceptó (200) y no dijo con qué id
        reason = classify(
            field_at(reply.data, "error", "code"), field_at(reply.data, "error", "error_subcode")
        )
        if reason == "RATE_LIMITED":
            raise TemporaryChannelError  # Meta no lo aceptó: se reintenta con espera
        if reason == "PROVIDER_ERROR":
            raise AmbiguousChannelError  # pudo haberse enviado: no se reenvía a ciegas
        return SendResult(False, reason=reason)

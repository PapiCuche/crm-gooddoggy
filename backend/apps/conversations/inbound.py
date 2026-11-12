"""Mensajes entrantes de WhatsApp (F7-04; ADR-029 §5): del ítem enrutado por el procesador
(`ingress.route`, con el scope de la organización de la cuenta registrada) al contacto, la
conversación y el mensaje del Inbox, con los comandos de F6 (`record_inbound`).

Todo lo que viene de Meta es dato no confiable. El texto se sanea **antes** de los comandos, que
rechazan en vez de limpiar. El teléfono se normaliza a E.164 y el contacto se busca y se crea bajo
`pg_advisory_xact_lock` de (organización, teléfono), sin índice único (los contactos admiten
homónimos, ADR-028): el segundo proceso espera, encuentra el del primero y lo reutiliza. Nunca se
fusiona. El nombre del perfil no se usa para buscar. Nada de aquí registra un teléfono ni un texto.

Un texto (ya saneado) que es exactamente una palabra de baja o de alta de la organización
(`contacts.keywords`, F7-08b) revoca lo proactivo de WhatsApp o levanta una baja de palabra, con el
mensaje como evidencia, y si cambió algo encola una confirmación fija: en la transacción del mensaje
y solo la primera vez que llega.
"""

import datetime as dt
import unicodedata
from typing import Any

from django.conf import settings
from django.db import connections
from django.utils import timezone

from apps.contacts import keywords
from apps.contacts.commands import create_contact
from apps.contacts.consents import lift_keyword_stop, revoke_consent
from apps.contacts.models import ConsentSource, Contact
from apps.conversations import commands
from apps.conversations.models import (
    BODY_MAX,
    ChannelAccount,
    Conversation,
    Message,
    MessageType,
)
from core.observability.logging import get_logger
from core.tenancy.context import TenantContext
from core.tenancy.scope import require_scope

logger = get_logger(__name__)
ZWJ = "‍"
KEEP = frozenset({"\n", "\t", "‌", ZWJ, "‎", "‏", "؜"})  # y las marcas LRM, RLM, ALM
NAME_MAX = 100
LABELS = {  # lo que ve el chat de lo que aún no se entiende: una frase fija, nunca el contenido
    "image": "Imagen recibida", "audio": "Audio recibido", "video": "Video recibido",
    "document": "Documento recibido", "sticker": "Sticker recibido",
    "location": "Ubicación recibida", "contacts": "Contacto recibido",
    "interactive": "Respuesta interactiva recibida", "button": "Respuesta interactiva recibida",
}  # fmt: skip
GENERIC = "Mensaje no soportado"
CONFIRMATIONS = {  # D-F7-05: texto fijo en español (a confirmar por el propietario)
    keywords.OPT_OUT: "Listo: ya no te enviaremos mensajes por iniciativa nuestra por WhatsApp. "
    "Si nos escribes, te seguiremos respondiendo.",
    keywords.OPT_IN: "Listo: volverás a recibir nuestros mensajes por WhatsApp.",
}


class Throttled(Exception):  # noqa: N818 — el tope de contactos nuevos por minuto
    """Se alcanzó el tope de contactos nuevos de la cuenta: el ítem se reintenta, no se pierde."""


def _cut(text: str, limit: int) -> str:
    """Hasta `limit` caracteres sin partir una secuencia unida por ZWJ (un emoji de familia)."""
    cut = text[:limit]
    while cut and (cut[-1] == ZWJ or text[len(cut) : len(cut) + 1] == ZWJ):
        cut = cut[:-1]
    return cut


def tag(char: str) -> bool:
    """El bloque Tags entero (U+E0000–E007F), también sus puntos sin asignar (categoría `Cn`)."""
    return 0xE0000 <= ord(char) <= 0xE007F


def clean_text(raw: str) -> str:
    """El texto de un cliente listo para `record_inbound`: sin controles (salvo el salto de línea y
    el tabulador), sin caracteres de formato invisibles ni controles de dirección (se conservan
    ZWNJ, ZWJ, LRM, RLM y ALM), sin Tags U+E0000–E007F (un canal de inyección de instrucciones
    para la IA), sin sustitutos ni `\\u0000`, con `\\n` por `\\r\\n`, y recortado a `BODY_MAX`."""
    text = raw.replace("\r\n", "\n").replace("\r", "\n")
    kept = [
        c for c in text
        if c in KEEP or (unicodedata.category(c) not in ("Cc", "Cf", "Cs") and not tag(c))
    ]  # fmt: skip
    return _cut("".join(kept), BODY_MAX)


def visible(text: str) -> bool:
    return any(unicodedata.category(c) not in ("Cc", "Cf", "Zs", "Zl", "Zp") for c in text)


def profile_name(raw: Any) -> str:
    """El nombre del perfil, saneado, en una línea y de hasta 100 caracteres (o vacío)."""
    if not isinstance(raw, str):
        return ""
    return _cut(" ".join(clean_text(raw).split()), NAME_MAX).strip()


def normalize_phone(wa_id: Any) -> str | None:
    """El `wa_id` de Meta (el teléfono sin `+`) en E.164, o `None` si no lo es. Con las rarezas de
    dos países (a confirmar con Meta): México envía un `1` tras el `52` y Argentina un `9` tras el
    `54`; se quitan para que las dos formas del mismo número sean un solo contacto."""
    if not isinstance(wa_id, str) or not wa_id.isascii() or not wa_id.isdigit():
        return None
    digits = wa_id
    if len(digits) == 13 and digits.startswith(("521", "549")):
        digits = digits[:2] + digits[3:]
    phone = f"+{digits}"
    return phone if 8 <= len(digits) <= 15 and digits[0] != "0" else None


def variants(phone: str) -> list[str]:
    """Las formas con las que un contacto puede tener ya ese número (las de México y Argentina)."""
    forms = [phone]
    if len(phone) == 13 and phone.startswith(("+52", "+54")):
        forms.append(phone[:3] + ("1" if phone.startswith("+52") else "9") + phone[3:])
    return forms


def instant(raw: Any) -> dt.datetime | None:
    """El `timestamp` de Meta (segundos Unix en texto) como instante; `None` si no es creíble."""
    if not isinstance(raw, str) or not raw.isascii() or not raw.isdigit():
        return None
    seconds = int(raw)
    if not 1_000_000_000 <= seconds <= 4_102_444_800:  # de 2001 a 2100
        return None
    return dt.datetime.fromtimestamp(seconds, dt.UTC)


def _find(alias: str, account_id: Any, phones: list[str]) -> Contact | None:
    """El contacto de ese teléfono: el de conversación abierta en esta cuenta o el más antiguo."""
    found = list(
        Contact.objects.using(alias)
        .filter(primary_phone_e164__in=phones, deleted_at__isnull=True)
        .order_by("created_at", "id")
    )
    if len(found) > 1:
        talking = (
            Conversation.objects.using(alias)
            .filter(channel_account_id=account_id, contact_id__in=[c.pk for c in found])
            .exclude(status="CLOSED")
            .order_by("-last_message_at")
            .values_list("contact_id", flat=True)
            .first()
        )
        found.sort(key=lambda c: c.pk != talking)  # estable: si no hay, el más antiguo
    return found[0] if found else None


def _lock(alias: str, key: str) -> None:
    """Bloqueo de aviso hasta que acabe la transacción del scope (el segundo espera al primero)."""
    with connections[alias].cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", [key])


def _capped(alias: str, account_id: Any) -> bool:
    """¿Ya nacieron en el último minuto tantos contactos nuevos por esta cuenta como permite el
    tope? Cuenta los contactos de un mensaje entrante cuya primera conversación es de la cuenta."""
    since = timezone.now() - dt.timedelta(minutes=1)
    threads = Conversation.objects.using(alias).filter(
        channel_account_id=account_id, created_at__gte=since
    )
    born = Contact.objects.using(alias).filter(
        source=Contact.Source.INBOUND_MESSAGE, created_at__gte=since,
        pk__in=threads.values("contact_id"),
    )  # fmt: skip
    return born.count() >= settings.INBOUND_NEW_CONTACTS_PER_MINUTE


def receive(ctx: TenantContext, account: ChannelAccount, message: dict[str, Any]) -> str | None:
    """Registra un mensaje entrante de la cuenta. Devuelve `None` si quedó registrado (o ya lo
    estaba), o el motivo por el que se ignora (`REACTION`, `BAD_PHONE`). `Throttled` si hay que
    crear un contacto y la cuenta alcanzó el tope por minuto: el procesador reintenta."""
    alias = require_scope(ctx)
    kind, external = message.get("type"), message["id"]
    if kind == "reaction":  # se ignoran, con un contador (sin quién ni a qué mensaje)
        logger.info("meta.webhook.reaction_ignored")
        return "REACTION"
    if (phone := normalize_phone(message.get("from"))) is None:
        return "BAD_PHONE"
    body, message_type = None, MessageType.UNSUPPORTED
    if kind == "text":
        body = clean_text(message["text"]["body"])
    if body is not None and visible(body):
        message_type = MessageType.TEXT
    else:
        body = LABELS.get(str(kind), GENERIC) if kind != "text" else GENERIC
    _lock(alias, f"wa:{ctx.organization_id}:{phone}")
    contact = _find(alias, account.pk, variants(phone))
    if contact is None:
        _lock(alias, f"wa-cap:{account.pk}")  # contar y crear, uno a uno: orden teléfono, cuenta
        if _capped(alias, account.pk):
            raise Throttled
        name = profile_name(message.get("profile_name")) or phone
        try:
            contact = create_contact(
                ctx, display_name=name, primary_phone_e164=phone,
                source=Contact.Source.INBOUND_MESSAGE,
            )  # fmt: skip
        except ValueError:  # un nombre que el comando no acepta: el teléfono es el nombre
            contact = create_contact(
                ctx, display_name=phone, primary_phone_e164=phone,
                source=Contact.Source.INBOUND_MESSAGE,
            )  # fmt: skip
    saved, created = commands.record_inbound(
        ctx, contact_id=contact.pk, channel_account_id=account.pk, external_message_id=external,
        body=body, message_type=message_type, provider_at=instant(message.get("timestamp")),
    )  # fmt: skip
    if created and message_type == MessageType.TEXT:
        _keyword(ctx, contact.pk, saved)
    return None


def _keyword(ctx: TenantContext, contact_id: Any, saved: Message) -> None:
    """La baja o el alta, si el texto entero es una palabra; solo un cambio real confirma."""
    action = keywords.action_for(ctx, saved.body)
    if action is None:
        return
    where = {"contact_id": contact_id, "channel": "WHATSAPP", "purpose": "ALL_PROACTIVE"}
    proof = {"message_id": str(saved.pk)}
    if action == keywords.OPT_OUT:
        _, changed = revoke_consent(ctx, **where, source=ConsentSource.KEYWORD_STOP, evidence=proof)
    else:  # solo levanta una baja hecha con una palabra; si no, ni evento ni confirmación
        changed = lift_keyword_stop(ctx, **where, evidence=proof)
    logger.info("meta.webhook.consent_keyword", action=action, changed=changed)  # sin quién
    if changed:
        commands.queue_confirmation(
            ctx, conversation_id=saved.conversation_id, body=CONFIRMATIONS[action]
        )

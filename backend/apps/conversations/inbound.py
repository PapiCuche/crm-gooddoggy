"""Mensajes entrantes de WhatsApp (F7-04; ADR-029 §5): del ítem enrutado por el procesador
(`ingress.route`, con el scope de la organización de la cuenta registrada) al contacto, la
conversación y el mensaje del Inbox, con los comandos de F6 (`record_inbound`).

Todo lo que viene de Meta es dato no confiable. El texto se sanea **antes** de los comandos, que
rechazan en vez de limpiar. El teléfono se normaliza a E.164 y el contacto se busca y se crea bajo
`pg_advisory_xact_lock` de (organización, teléfono), sin índice único (los contactos admiten
homónimos, ADR-028): el segundo proceso espera, encuentra el del primero y lo reutiliza. Nunca se
fusiona. El nombre del perfil no se usa para buscar. Nada de aquí registra un teléfono ni un texto.
"""

import unicodedata
from typing import Any

from django.db import connections

from apps.contacts.commands import create_contact
from apps.contacts.models import Contact
from apps.conversations import commands
from apps.conversations.models import (
    BODY_MAX,
    ChannelAccount,
    Conversation,
)
from core.observability.logging import get_logger
from core.tenancy.context import TenantContext
from core.tenancy.scope import require_scope

logger = get_logger(__name__)
ZWJ = "‍"
KEEP = frozenset({"\n", "\t", "‌", ZWJ, "‎", "‏", "؜"})  # y las marcas LRM, RLM, ALM
NAME_MAX = 100


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


def receive(ctx: TenantContext, account: ChannelAccount, message: dict[str, Any]) -> str | None:
    """Registra un mensaje de texto entrante de la cuenta. Devuelve `None` si quedó registrado (o
    ya lo estaba), o el motivo por el que se ignora: `NOT_TEXT` (imágenes, audios, reacciones…:
    F7-04b los guarda como «no soportado»), `EMPTY` (sin nada visible) y `BAD_PHONE`."""
    alias = require_scope(ctx)
    if message.get("type") != "text":
        return "NOT_TEXT"
    if (phone := normalize_phone(message.get("from"))) is None:
        return "BAD_PHONE"
    body = clean_text(message["text"]["body"])
    if not visible(body):
        return "EMPTY"
    external = message["id"]
    with connections[alias].cursor() as cursor:  # se suelta al acabar la transacción del scope
        key = f"wa:{ctx.organization_id}:{phone}"
        cursor.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", [key])
    contact = _find(alias, account.pk, variants(phone))
    if contact is None:
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
    commands.record_inbound(
        ctx, contact_id=contact.pk, channel_account_id=account.pk, external_message_id=external,
        body=body,
    )  # fmt: skip
    return None

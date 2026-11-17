"""El nombre del cliente de Instagram tras su primer mensaje (F7b-04c; ADR-035 D-F7b-06, §5 y §8).

El contacto nuevo nace con el nombre fijo de `SOCIAL_NAMES` (`inbound`) y emite
`contact_identity.created`; `tasks.fetch_profile` lee después `name` y `username` con el token de
la página. Los dos los escribe el cliente: datos no confiables, saneados y acotados, solo para el
nombre del contacto, que se cambia **solo** si sigue siendo el fijo (nunca el que puso una
persona) y con el contacto bloqueado. Jamás sirven para buscar ni fusionar contactos (D-F7b-05).
La tabla de identidades no tiene un identificador visible: el `@usuario` solo se usa como nombre
cuando no llega `name`. Messenger conserva el nombre fijo hasta que Meta apruebe el acceso al
perfil (Business Asset User Profile Access).
"""

import re
import unicodedata
from typing import Any

from django.db import transaction

from apps.contacts.commands import update_contact
from apps.contacts.models import Contact
from apps.conversations.models import ChannelAccount, ContactIdentity, Credential
from core.tenancy.context import TenantContext
from core.tenancy.scope import require_scope
from core.text import FILLERS

SOCIAL_NAMES = {"MESSENGER": "Cliente de Messenger", "INSTAGRAM": "Cliente de Instagram"}
NAME_MAX = 100
USERNAME = re.compile(r"[A-Za-z0-9._]{1,30}")  # el `@usuario` de Instagram; otro no se usa
RAW_MAX = 1000  # lo que se mira de un dato del perfil antes de sanearlo


def untrusted(raw: Any, limit: int) -> str:
    """Un dato del perfil que escribe el cliente, como nombre: NFC, en una línea, sin controles,
    formato invisible ni marcas o controles de dirección (BIDI), sin Tags, acotado a `limit` y con
    alguna letra o cifra que se vea. Vacío si no queda nada así."""
    if not isinstance(raw, str):
        return ""
    spaced = "".join(" " if c.isspace() else c for c in raw[:RAW_MAX])
    kept = "".join(c for c in unicodedata.normalize("NFC", spaced) if c.isprintable())
    text = " ".join(kept.split())[:limit].strip()
    return text if any(c.isalnum() and c not in FILLERS for c in text) else ""


def _placeholder(alias: str, identity_id: Any) -> ContactIdentity | None:
    """La identidad de Instagram cuyo contacto vivo aún tiene el nombre fijo del código."""
    identity = ContactIdentity.objects.using(alias).filter(pk=identity_id, platform="INSTAGRAM")
    named = Contact.objects.using(alias).filter(
        display_name=SOCIAL_NAMES["INSTAGRAM"], deleted_at__isnull=True
    )
    return identity.filter(contact_id__in=named.values("pk")).first()


def profile_target(ctx: TenantContext, *, identity_id: Any) -> tuple[str, Credential] | None:
    """Lo que la tarea del nombre necesita: el IGSID y la credencial viva de su cuenta. `None` si
    la identidad no es de esta organización, ya tiene nombre o la cuenta no se puede usar."""
    alias = require_scope(ctx)
    identity = _placeholder(alias, identity_id)
    if identity is None:
        return None
    usable = ChannelAccount.objects.using(alias).filter(
        pk=identity.channel_account_id, platform="INSTAGRAM", deleted_at__isnull=True,
        status__in=("PENDING", "CONNECTED", "DEGRADED"),
    )  # fmt: skip
    account = usable.first()
    live = Credential.objects.using(alias).filter(
        pk=account.credential_id if account else None, owner_id=identity.channel_account_id,
        revoked_at__isnull=True,
    )  # fmt: skip
    credential = live.first()
    return None if credential is None else (identity.external_id, credential)


def apply_profile(ctx: TenantContext, *, identity_id: Any, profile: dict[str, Any]) -> str:
    """Pone el nombre leído de Instagram al contacto de la identidad **solo** si sigue con el
    nombre fijo, con el contacto bloqueado: `OK`; `KEPT` si una persona ya le puso otro (o ya no
    está); `BAD_DATA` si no llega ni un nombre ni un `@usuario` válidos (queda el fijo)."""
    alias = require_scope(ctx)
    name, user = untrusted(profile.get("name"), NAME_MAX), profile.get("username")
    handle = f"@{user}" if isinstance(user, str) and USERNAME.fullmatch(user) else ""
    if not (shown := name or handle):
        return "BAD_DATA"
    with transaction.atomic(using=alias):
        mine = ContactIdentity.objects.using(alias).filter(pk=identity_id, platform="INSTAGRAM")
        live = Contact.objects.using(alias).select_for_update(no_key=True)
        contact = live.filter(pk__in=mine.values("contact_id"), deleted_at__isnull=True).first()
        if contact is None or contact.display_name != SOCIAL_NAMES["INSTAGRAM"]:
            return "KEPT"  # lo cambió una persona entre la lectura y el bloqueo
        try:
            update_contact(ctx, contact_id=contact.pk, version=contact.version, display_name=shown)
        except ValueError:
            return "BAD_DATA"
    return "OK"

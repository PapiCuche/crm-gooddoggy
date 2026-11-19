"""El enlace de una invitación (ADR-020 §3 a §5): encontrarla por su secreto y enseñar una vista
previa. Es una ruta de plataforma con la excepción de ADR-014 §4 (D-F2-14): abre el `tenant_scope`
de la organización que invita, con actor `SYSTEM`, solo después de que la búsqueda por el hash
devuelva una invitación. La organización nunca sale de la petición.

El secreto no se registra, no se audita y no se devuelve. Cualquier enlace que no vale recibe la
misma respuesta y el mismo trabajo: contar el intento, el SHA-256 de lo presentado y una búsqueda,
también si no tiene la forma de un secreto.
"""

import re
from typing import NamedTuple

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import connection
from django.http import HttpRequest

from apps.accounts import recovery, throttle
from apps.accounts.services import client_of
from apps.organizations import services as organizations
from core.api.errors import ApiError
from core.tenancy.context import ActorType, TenantContext
from core.tenancy.scope import tenant_scope

SECRET = re.compile(r"[A-Za-z0-9_-]{43}")  # la forma de `secrets.token_urlsafe(32)`
INVALID_LINK = "INVALID_INVITATION_LINK"
INVALID_MESSAGE = "El enlace no vale o ya caducó. Pide que te inviten otra vez."


class InvitationPreview(NamedTuple):
    organization_name: str
    email: str
    needs_account: bool  # el correo no tiene cuenta: hay que crearla, no iniciar sesión


def _invalid() -> ApiError:
    return ApiError(INVALID_LINK, 400, INVALID_MESSAGE)


def preview(request: HttpRequest, *, token: str) -> InvitationPreview:
    """Lo que enseña el enlace `token` de una invitación pendiente y sin caducar: la organización
    que invita, el correo invitado y si ese correo tiene que crear una cuenta (ADR-020 §5). No
    cambia nada: abrir el enlace no lo gasta.

    Antes de todo, el límite de intentos por dirección de red (`ApiError` 429). Un enlace que no
    vale, sea por lo que sea, es `ApiError` `INVALID_INVITATION_LINK`, siempre igual.
    """
    if connection.in_atomic_block:  # la búsqueda va sin tenant, y el scope es dueño de lo suyo
        raise RuntimeError("preview no se llama dentro de una transacción")
    address = throttle.keys("", client_of(request)["ip"])["ip"]  # en IPv6, su red /64
    recovery.count_link(f"inv:{address}", settings.INVITATION_LINK_THROTTLE)
    token_hash = recovery.token_hash(token)  # también si está mal formado: el mismo trabajo
    found = organizations.find_invitation(token_hash)
    if found is None or not SECRET.fullmatch(token):
        raise _invalid()
    organization_id, invitation_id = found
    ctx = TenantContext(organization_id, "invitation_link", actor_type=ActorType.SYSTEM)
    with tenant_scope(ctx):
        shown = organizations.previewed_invitation(
            ctx, invitation_id=invitation_id, token_hash=token_hash
        )
    if shown is None:
        raise _invalid()
    name, email = shown
    has_account = get_user_model().objects.filter(email=email).exists()
    return InvitationPreview(name, email, not has_account)

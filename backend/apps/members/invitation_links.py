"""El enlace de una invitación (ADR-020 §3 a §6): encontrarla por su secreto, enseñar una vista
previa y aceptarla. Son rutas de plataforma con la excepción de ADR-014 §4 (D-F2-14): abren el
`tenant_scope` de la organización que invita, con actor `SYSTEM`, solo después de que la búsqueda
por el hash devuelva una invitación. La organización nunca sale de la petición.

El secreto no se registra, no se audita y no se devuelve. Cualquier enlace que no vale recibe la
misma respuesta y el mismo trabajo: contar el intento, el SHA-256 de lo presentado y una búsqueda,
también si no tiene la forma de un secreto.
"""

import re
from typing import Any, NamedTuple
from uuid import UUID

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import connection
from django.http import HttpRequest

from apps.accounts import recovery, throttle
from apps.accounts.services import AccountExists, client_of, unaudited
from apps.audit import platform
from apps.audit.services import Entity, Result
from apps.members import services as members
from apps.organizations import services as organizations
from core.api.errors import ApiError
from core.tenancy.context import ActorType, TenantContext
from core.tenancy.scope import tenant_scope

SECRET = re.compile(r"[A-Za-z0-9_-]{43}")  # la forma de `secrets.token_urlsafe(32)`
INVALID_LINK = "INVALID_INVITATION_LINK"
INVALID_MESSAGE = "El enlace no vale o ya caducó. Pide que te inviten otra vez."
ACCOUNT_EXISTS = "ACCOUNT_EXISTS"
EXISTS_MESSAGE = "Ese correo ya tiene cuenta. Inicia sesión con ella y vuelve a abrir el enlace."
DENIED = "invitation.accept.denied"


class InvitationPreview(NamedTuple):
    organization_name: str
    email: str
    needs_account: bool  # el correo no tiene cuenta: hay que crearla, no iniciar sesión


def _invalid() -> ApiError:
    return ApiError(INVALID_LINK, 400, INVALID_MESSAGE)


def _found(request: HttpRequest, token: str) -> tuple[str, tuple[UUID, UUID] | None]:
    """Cuenta el intento por dirección de red (`rst:inv:`, el mismo contador para ver y aceptar) y
    busca la invitación por el SHA-256 de lo presentado, también si está mal formado: el mismo
    trabajo. Devuelve el hash y la organización e invitación, o `None` si no vale la forma."""
    if connection.in_atomic_block:  # la búsqueda va sin tenant, y el scope es dueño de lo suyo
        raise RuntimeError("un enlace de invitación no se mira dentro de una transacción")
    address = throttle.keys("", client_of(request)["ip"])["ip"]  # en IPv6, su red /64
    recovery.count_link(f"inv:{address}", settings.INVITATION_LINK_THROTTLE)
    token_hash = recovery.token_hash(token)
    found = organizations.find_invitation(token_hash)
    return token_hash, found if SECRET.fullmatch(token) else None


def preview(request: HttpRequest, *, token: str) -> InvitationPreview:
    """Lo que enseña el enlace `token` de una invitación pendiente y sin caducar: la organización
    que invita, el correo invitado y si ese correo tiene que crear una cuenta (ADR-020 §5). No
    cambia nada: abrir el enlace no lo gasta.

    Antes de todo, el límite de intentos por dirección de red (`ApiError` 429). Un enlace que no
    vale, sea por lo que sea, es `ApiError` `INVALID_INVITATION_LINK`, siempre igual.
    """
    token_hash, found = _found(request, token)
    if found is None:
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


class Accepted(NamedTuple):
    organization_name: str
    account_created: bool


def _refuse(request: HttpRequest, actor: UUID | None, reason: str, **more: Any) -> None:
    """Un rechazo, en la auditoría de plataforma (ADR-020 §6), fuera de la transacción deshecha y
    sin el secreto, su hash, el correo ni la contraseña. Un fallo no cambia la respuesta."""
    who = platform.Actor.USER if actor else platform.Actor.ANONYMOUS
    try:
        metadata = {"reason": reason} | {k: str(v) for k, v in more.items() if v and k != "entity"}
        entity = Entity("invitation", more["entity"]) if more.get("entity") else None
        how: dict[str, Any] = {"actor_type": who, "actor_id": actor, "entity": entity}
        platform.record(
            DENIED, metadata=metadata, result=Result.DENIED, **how, **client_of(request)
        )
    except Exception as error:
        unaudited(error, DENIED)


def accept(request: HttpRequest, *, token: str, **account: Any) -> Accepted:
    """Acepta la invitación del enlace `token` (ADR-020 §4) con `account` (la cuenta nueva) y la
    sesión de la petición. No inicia sesión. Antes de todo, el límite compartido con la vista
    previa (429). Un enlace que no vale, o que ya no se puede aceptar, es la `ApiError` de la vista
    previa; solo después, `ACCOUNT_EXISTS` (409) o `ValidationError`. Cada rechazo se audita."""
    user = request.user
    actor = user.pk if user.is_authenticated else None
    token_hash, found = _found(request, token)
    if found is None:
        _refuse(request, actor, "unknown" if SECRET.fullmatch(token) else "malformed")
        raise _invalid()
    ctx = TenantContext(found[0], "invitation_link", actor_type=ActorType.SYSTEM)
    try:
        with tenant_scope(ctx):
            done = members.accept_invitation(
                ctx, found[1], token_hash, session_user_id=actor, **account
            )
    except members.NotAcceptable as refused:
        reason, *denied = refused.args
        _refuse(request, actor, reason, entity=found[1], denied=":".join(denied))
        raise _invalid() from None
    except AccountExists:
        _refuse(request, actor, "account_exists", entity=found[1])
        raise ApiError(ACCOUNT_EXISTS, 409, EXISTS_MESSAGE) from None
    except ValidationError:
        _refuse(request, actor, "password", entity=found[1])
        raise
    return Accepted(*done)

"""Tiempo real del Inbox (F6-07a; ADR-003 §6, tenancy-context §4).

`publish` (lo llama la tarea del outbox) manda al grupo de la organización el tipo del evento y sus
ids, nunca texto, nombre ni teléfono: es una pista, el cliente vuelve a pedir por la API.
`InboxConsumer` (`/ws/o/<slug>/inbox/`) saca la organización de la sesión y de la membresía del
slug, nunca de un mensaje del cliente (solo `ping`), y evalúa cada evento al entregarlo con el
permiso y el alcance de `conversations.view` de ese momento (`scoped`, con RLS).

Cierres: 4401 sin sesión, 4404 sin organización o membresía (indistinguibles), 4403 sin permiso,
4429 tope, 1013 capa caída, 1009 trama grande, 4408 inactividad. Ningún registro lleva ids.
"""

import asyncio
import json
import time
from collections import Counter
from contextlib import suppress
from typing import Any
from uuid import UUID

from asgiref.sync import async_to_sync
from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.layers import get_channel_layer
from django.conf import settings

from apps.access.selectors import AccessDenied, execution_context, has_permission, scoped
from apps.accounts.websocket import user_for
from apps.conversations.models import Conversation
from core.observability.logging import get_logger
from core.tenancy.channels import CLOSE_SUSPENDED as CLOSE_FORBIDDEN
from core.tenancy.channels import TenantConsumerMixin
from core.tenancy.context import require
from core.tenancy.resolution import OrganizationSuspended, TenantNotFound, resolve_tenant

logger = get_logger(__name__)
VIEW = "conversations.view"
EVENTS = frozenset(
    (
        "message.received message.queued message.updated conversation.assigned conversation.updated"
    ).split()
)
CLOSE_UNAUTHENTICATED, CLOSE_TOO_MANY, CLOSE_IDLE = 4401, 4429, 4408
CLOSE_TOO_BIG, CLOSE_RETRY = 1009, 1013
_open: Counter[UUID] = Counter()  # conexiones abiertas por persona en este proceso


def group_name(organization_id: UUID) -> str:
    return f"inbox.{organization_id}"


def outgoing(
    event_type: str, aggregate_id: UUID, payload: dict[str, Any], at: str
) -> dict[str, Any] | None:
    """El evento tal como viaja (tipo, ids y hora); `None` si no es del Inbox."""
    if event_type not in EVENTS:
        return None
    raw = aggregate_id if event_type.startswith("conversation.") else payload.get("conversation_id")
    try:
        conversation_id = UUID(str(raw))
    except ValueError:
        return None
    event = dict(type=event_type, id=str(aggregate_id), conversation_id=str(conversation_id), at=at)
    return {"type": "inbox.event", "event": event}


def publish(organization_id: UUID, message: dict[str, Any]) -> bool:
    """Manda el evento al grupo. `False` sin capa de canales; los fallos de Redis suben (la tarea
    reintenta). `channels_redis` cierra sus conexiones al cerrarse el bucle de cada llamada."""
    layer = get_channel_layer()
    if layer is None:
        return False
    async_to_sync(layer.group_send)(group_name(organization_id), message)
    return True


class InboxConsumer(TenantConsumerMixin, AsyncWebsocketConsumer):  # type: ignore[misc]
    user_id: UUID | None = None
    watcher: asyncio.Task[None] | None = None
    counted = False
    closed = False
    last_seen = 0.0

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        self.base_send = send  # para poder cerrar aunque la capa falle al empezar
        try:
            await super().__call__(scope, receive, send)
        except Exception as error:  # noqa: BLE001 — Redis o BD caídas: se cierra, nunca se sigue
            logger.warning("inbox.ws_failed", error=type(error).__name__)
            with suppress(Exception):  # ya cerrada: nada más que hacer
                await self.close(code=CLOSE_RETRY)
        finally:
            self._release()

    async def connect(self) -> None:
        user = self.scope.get("user")
        layer = self.channel_layer
        if layer is None or not getattr(user, "is_authenticated", False):
            await self.close(code=CLOSE_RETRY if layer is None else CLOSE_UNAUTHENTICATED)
            return
        if not await self.connect_tenant():  # 4404 (sin organización o sin membresía) o 4403
            return
        tenant = self.tenant
        if tenant is None or tenant.user_id is None or not await self.in_tenant(self._may_view):
            await self.close(code=CLOSE_FORBIDDEN)
            return
        if _open[tenant.user_id] >= settings.INBOX_WS_MAX_PER_USER:
            await self.close(code=CLOSE_TOO_MANY)
            return
        self.user_id = tenant.user_id
        _open[self.user_id] += 1  # se reserva sin un `await` tras comprobar el tope
        self.counted = True
        await layer.group_add(group_name(tenant.organization_id), self.channel_name)
        self.last_seen = time.monotonic()
        await self.accept()
        self.watcher = asyncio.create_task(self._watch())

    @staticmethod
    def _may_view() -> bool:
        """¿Es miembro activo y tiene el permiso, ahora?"""
        try:
            return has_permission(execution_context(require()), VIEW)
        except AccessDenied:
            return False

    @staticmethod
    def _visible(conversation_id: UUID) -> bool | None:
        """`None`: sin permiso (se expulsa). Si no, ¿está la conversación en su alcance?"""
        ectx = execution_context(require())
        if not has_permission(ectx, VIEW):
            return None
        return scoped(ectx, VIEW, Conversation.objects.filter(pk=conversation_id)).exists()

    async def _close(self, code: int) -> None:
        """Cierra una vez; después no se entrega nada más."""
        if not self.closed:
            self.closed = True
            await self.close(code=code)
            self._release()

    async def inbox_event(self, message: dict[str, Any]) -> None:
        """Un evento del grupo, evaluado AHORA para esta persona."""
        event = message["event"]
        if self.closed:
            return
        try:
            seen = await self.in_tenant(self._visible, UUID(event["conversation_id"]))
        except AccessDenied:
            seen = None
        if seen is None:
            await self._close(CLOSE_FORBIDDEN)
        elif seen:
            await self.send(text_data=json.dumps(event))

    async def receive(self, text_data: str | None = None, bytes_data: bytes | None = None) -> None:
        """Lo único que acepta el cliente es `{"type": "ping"}`; el resto se ignora."""
        self.last_seen = time.monotonic()
        if bytes_data is not None or len(text_data or "") > settings.INBOX_WS_MAX_FRAME:
            await self._close(CLOSE_TOO_BIG)
            return
        try:
            frame = json.loads(text_data or "")
        except ValueError:
            return
        if isinstance(frame, dict) and frame.get("type") == "ping":
            await self.send(text_data='{"type":"pong"}')

    async def _watch(self) -> None:
        """Cierra por inactividad y, cada `INBOX_WS_RECHECK_SECONDS`, comprueba sesión y permiso y
        renueva el grupo (Redis olvida a quien lleva más de 24 h). Un fallo cierra con 1013: si la
        tarea muriera, la conexión seguiría abierta sin comprobarse."""
        checked = time.monotonic()
        try:
            while True:
                await asyncio.sleep(settings.INBOX_WS_TICK_SECONDS)
                now = time.monotonic()
                if now - self.last_seen > settings.INBOX_WS_IDLE_SECONDS:
                    await self._close(CLOSE_IDLE)
                    return
                if now - checked >= settings.INBOX_WS_RECHECK_SECONDS:
                    checked = now
                    if not await self._still_allowed():
                        return
                    tenant, layer = self.tenant, self.channel_layer
                    if tenant is not None and layer is not None:
                        await layer.group_add(group_name(tenant.organization_id), self.channel_name)
        except Exception as error:  # noqa: BLE001 — BD o Redis caídas: se cierra, nunca se sigue
            logger.warning("inbox.ws_watch_failed", error=type(error).__name__)
            try:
                await self._close(CLOSE_RETRY)
            except Exception:  # noqa: BLE001, S110 — ya cerrada: nada más que hacer
                pass

    async def _still_allowed(self) -> bool:
        tenant = self.tenant
        user = await database_sync_to_async(user_for)(self.scope.get("session_key"))
        if tenant is None or not user.is_authenticated or user.pk != tenant.user_id:
            await self._close(CLOSE_UNAUTHENTICATED)
            return False
        slug = self.scope["url_route"]["kwargs"]["org_slug"]
        try:
            await database_sync_to_async(resolve_tenant)(slug, user, "ws")
            allowed = await self.in_tenant(self._may_view)
        except TenantNotFound, OrganizationSuspended, AccessDenied:
            allowed = False
        if not allowed:
            await self._close(CLOSE_FORBIDDEN)
        return allowed

    async def disconnect(self, code: int) -> None:
        self._release()
        tenant = self.tenant
        if tenant is not None and self.channel_layer is not None:
            await self.channel_layer.group_discard(
                group_name(tenant.organization_id), self.channel_name
            )

    def _release(self) -> None:
        if self.watcher is not None:
            if self.watcher is not asyncio.current_task():
                self.watcher.cancel()
            self.watcher = None
        if self.counted and self.user_id is not None:
            self.counted = False
            _open[self.user_id] -= 1
            if _open[self.user_id] <= 0:
                del _open[self.user_id]

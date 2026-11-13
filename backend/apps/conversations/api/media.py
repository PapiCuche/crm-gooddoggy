"""La URL firmada del medio de un mensaje (F7-09a2; ADR-008, ADR-029 §7).

`GET …/conversations/{id}/attachments/{attachment_id}/url/`: `conversations.view` y la
conversación en su alcance, `contacts.view` sobre su contacto (el medio es contenido, como el
texto: D-F6-10), y un adjunto `DONE` de un mensaje entrante de **esa** conversación cuyo archivo se
puede servir (`selectors.SERVABLE`: `message-media`, antivirus `CLEAN`, tipo del mapa canónico).
Cualquier otra cosa es el mismo 404, sin decir cuál. Sin bloqueos: es una lectura (un bloqueo
haría esperar a quien está fuera del alcance detrás de otro, y eso ya es una señal).

La URL dura `EXPIRES` segundos, siempre `attachment` con un nombre generado, el tipo del mapa y
`private, no-store` firmados; la respuesta no se guarda en caché y nunca se registra.

`POST …/conversations/{id}/media/` (F7-09b2, formulario `multipart/form-data`): el tope por persona
(`throttle`), el permiso, el 404 de alcance, el antivirus (D-F7-04) y la política se miran **antes**
de leer el cuerpo: nada llega al disco ni al almacén de quien no puede enviar. El cuerpo pide
`Content-Length` (411) dentro del tope (413) y lo lee `outbound_media.Capped`, que corta al pasarse;
luego el tipo real (415), el blob y el comando, que vuelve a mirarlo todo con la conversación
bloqueada. Toda la vista es una transacción (`TenantResolutionMiddleware`): ante cualquier error, o
si era un reintento, el blob nuevo se borra (el de un mensaje ya guardado tiene otra clave).
"""

import datetime as dt
import re
from typing import Any

from django.conf import settings
from django.core.exceptions import SuspiciousOperation
from django.http.multipartparser import MultiPartParser, MultiPartParserError
from django.utils import timezone
from drf_spectacular.utils import extend_schema, extend_schema_view
from rest_framework import serializers
from rest_framework.exceptions import NotFound
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.access.permissions import request_context
from apps.access.selectors import can, scoped
from apps.conversations import outbound_media, policy, selectors
from apps.conversations.api import inputs
from apps.conversations.api.messages import REASONS, MessageSerializer, queue, shows_body
from apps.conversations.media import EXTENSIONS
from apps.conversations.models import Conversation
from core import storage
from core.api.errors import ApiError
from core.api.schema import errors
from core.tenancy import context

EXPIRES = 60  # segundos (el almacén admite hasta `storage.MAX_PRESIGN_SECONDS`)
SLACK = 64 << 10  # bytes del formulario además del archivo (separadores, cabeceras y campos)
REFUSED = {
    "MEDIA_NOT_SCANNED": (409, "Aún no se pueden enviar archivos: falta el antivirus."),
    "MEDIA_TOO_LARGE": (413, "El archivo pasa del tamaño permitido para su tipo."),
    "MEDIA_TYPE_NOT_ALLOWED": (415, "Solo imágenes JPEG o PNG y documentos PDF."),
    "LENGTH_REQUIRED": (411, "Falta la longitud del cuerpo (Content-Length)."),
}
FORM = "Un formulario multipart/form-data con un solo archivo, en `file`."


class MessageAttachmentUrlSerializer(serializers.Serializer[Any]):
    url = serializers.CharField(
        help_text="Firmada; caduca en `expires_at`. No se guarda ni se registra."
    )
    expires_at = serializers.DateTimeField()


@extend_schema_view(
    get=extend_schema(
        operation_id="conversation_attachment_url",
        tags=["conversations"],
        responses={200: MessageAttachmentUrlSerializer, **errors(401, 403, 404)},
        description="Una URL firmada de corta vida (60 s) para descargar el medio de un mensaje: "
        "siempre como adjunto (nunca en línea), con el tipo real del archivo y sin caché. Pide "
        "`conversations.view` sobre la conversación y `contacts.view` sobre su contacto. Solo "
        "un adjunto `DONE` cuyo archivo pasó el antivirus. 404 en cualquier otro caso: no "
        "existe, es de otra organización o conversación, queda fuera del alcance o no se puede "
        "servir.",
    ),
)
class AttachmentUrlView(APIView):
    required_permissions = {"GET": "conversations.view"}

    def get(self, request: Request, **kwargs: Any) -> Response:
        ectx = request_context(request)
        assert ectx is not None  # noqa: S101 — `HasPermission` ya lo comprobó
        mine = scoped(ectx, "conversations.view", Conversation.objects.all())
        found = mine.filter(pk=kwargs["conversation_id"]).first()
        served = None
        if found is not None and shows_body(ectx, found):
            served = selectors.servable(found.pk, kwargs["attachment_id"])
        if served is None:  # desconocido, ajeno, fuera del alcance o sin servir: el mismo 404
            raise NotFound
        attachment, file = served
        url = storage.storage().presign_get(
            file.storage_key, expires_in=EXPIRES, content_type=file.mime_type,
            filename=f"{attachment.kind.lower()}.{EXTENSIONS[file.mime_type]}",
            disposition="attachment",
        )  # fmt: skip
        expires_at = timezone.now() + dt.timedelta(seconds=EXPIRES)
        response = Response(
            MessageAttachmentUrlSerializer({"url": url, "expires_at": expires_at}).data
        )
        response["Cache-Control"] = storage.PRIVATE_CACHE
        response["X-Content-Type-Options"] = "nosniff"
        return response


def _refuse(code: str) -> ApiError:
    return ApiError(code, *REFUSED[code])


def _read(request: Request, handler: outbound_media.Capped) -> dict[str, Any]:
    """Los campos del formulario (cada uno una vez); el archivo queda en `handler.sink`."""
    length = request.META.get("CONTENT_LENGTH") or ""
    if not re.fullmatch(r"[0-9]{1,12}", length):  # `chunked` no se acota solo
        raise _refuse("LENGTH_REQUIRED")
    if int(length) > handler.limit + SLACK:  # sin leer nada
        raise _refuse("MEDIA_TOO_LARGE")
    raw = request._request
    try:
        fields, _ = MultiPartParser(request.META, raw, [handler], raw.encoding or "utf-8").parse()
    except MultiPartParserError, SuspiciousOperation:
        raise serializers.ValidationError({"file": [FORM]}) from None
    if handler.refused == "MEDIA_TOO_LARGE":
        raise _refuse("MEDIA_TOO_LARGE")
    if handler.refused or handler.sink is None:
        raise serializers.ValidationError({"file": [FORM]})
    twice = sorted(name for name, values in fields.lists() if len(values) > 1)
    if twice:
        raise serializers.ValidationError(dict.fromkeys(twice, ["Una sola vez."]))
    return fields.dict()


@extend_schema_view(
    post=extend_schema(
        operation_id="conversation_media_send",
        tags=["conversations"],
        request={"multipart/form-data": inputs.SendMediaFormSerializer},
        responses={
            200: MessageSerializer,
            202: MessageSerializer,
            **errors(400, 401, 403, 404, 409, 411, 413, 415, 429),
        },
        description="Pone en cola una imagen (JPEG o PNG) o un documento (PDF) para el cliente "
        "(`IMAGE` o `DOCUMENT`, `QUEUED`), con la política del texto, idempotente por "
        "`client_message_id` (202; 200 con el mismo archivo). Pide `messages.send` y "
        "`conversations.view`; 404: no existe o queda fuera del alcance. 409: `MEDIA_NOT_SCANNED` "
        "(sin antivirus), la política o `CLIENT_MESSAGE_ID_TAKEN`; 411 sin `Content-Length`; 413 "
        "`MEDIA_TOO_LARGE`; 415 `MEDIA_TYPE_NOT_ALLOWED` (el tipo real); 429 `RATE_LIMITED`.",
    ),
)
class SendMediaView(APIView):
    required_permissions = {"POST": ("messages.send", "conversations.view")}

    def post(self, request: Request, **kwargs: Any) -> Response:
        ectx, tenant = request_context(request), context.current()
        assert ectx is not None and tenant is not None  # noqa: S101 — `HasPermission` ya lo comprobó
        mine = scoped(ectx, "conversations.view", Conversation.objects.all())
        found = mine.filter(pk=kwargs["conversation_id"]).first()
        if found is None or not can(ectx, "messages.send", found):  # antes de leer el cuerpo
            raise NotFound
        if not settings.MEDIA_SEND_UNSCANNED:  # D-F7-04: nada sale sin antivirus
            raise _refuse("MEDIA_NOT_SCANNED")
        decision = selectors.messaging_decision(found, purpose=policy.CUSTOMER_REPLY)
        if decision.reason is not None:  # antes de guardar nada; el comando la repite
            raise ApiError(decision.reason, 409, REASONS[decision.reason])
        handler = outbound_media.Capped(max(outbound_media.CAPS.values()))
        try:
            wanted = inputs.SendMediaSerializer(data=_read(request, handler))
            wanted.is_valid(raise_exception=True)
            assert handler.sink is not None  # noqa: S101 — `_read` lo exige
            prepared = outbound_media.prepare(tenant, handler.sink, handler.file_name)
        except outbound_media.Refused as refused:
            raise _refuse(refused.code) from None
        finally:
            if handler.sink is not None:
                handler.sink.file.close()
        try:  # la vista es una transacción: cualquier error la deshace, y el blob sobra
            caption = wanted.validated_data.get("caption")
            message, created = queue(
                found, body=caption or outbound_media.LABELS[prepared.kind], media=prepared,
                client_message_id=wanted.validated_data["client_message_id"],
            )  # fmt: skip
            shown = MessageSerializer(message, context={"show_body": shows_body(ectx, found)})
            response = Response(shown.data, status=202 if created else 200)
        except BaseException:  # también cada 4xx tras el comando: aquí todos son excepciones
            outbound_media.discard(prepared)
            raise
        if not created:  # un reintento: el mensaje guardado tiene su propio archivo
            outbound_media.discard(prepared)
        return response

"""La URL firmada del medio de un mensaje (F7-09a2; ADR-008, ADR-029 §7).

`GET …/conversations/{id}/attachments/{attachment_id}/url/`: `conversations.view` y la
conversación en su alcance, `contacts.view` sobre su contacto (el medio es contenido, como el
texto: D-F6-10), y un adjunto `DONE` de un mensaje entrante de **esa** conversación cuyo archivo se
puede servir (`selectors.SERVABLE`: `message-media`, antivirus `CLEAN`, tipo del mapa canónico).
Cualquier otra cosa es el mismo 404, sin decir cuál. Sin bloqueos: es una lectura (un bloqueo
haría esperar a quien está fuera del alcance detrás de otro, y eso ya es una señal).

La URL dura `EXPIRES` segundos, siempre `attachment` con un nombre generado, el tipo del mapa y
`private, no-store` firmados; la respuesta no se guarda en caché y nunca se registra.
"""

import datetime as dt
from typing import Any

from django.utils import timezone
from drf_spectacular.utils import extend_schema, extend_schema_view
from rest_framework import serializers
from rest_framework.exceptions import NotFound
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.access.permissions import request_context
from apps.access.selectors import scoped
from apps.conversations import selectors
from apps.conversations.api.messages import shows_body
from apps.conversations.media import EXTENSIONS
from apps.conversations.models import Conversation
from core import storage
from core.api.schema import errors

EXPIRES = 60  # segundos (el almacén admite hasta `storage.MAX_PRESIGN_SECONDS`)


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

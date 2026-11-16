"""Gestionar las respuestas rápidas (F6-08a3, E06-10): `…/quick-replies/` y `…/{id}/`.

Todo pide `quick_replies.manage` (sin alcance), comprobado antes de leer el cuerpo o la base.
Escribir pasa por los comandos de `apps.conversations.quick_replies`, que validan, bloquean y
auditan; aquí se exige el tipo de cada campo y se traduce lo que lanzan. La organización sale del
contexto. Lo que no existe, es de otra organización o está borrado: el mismo 404. Borrar es lógico
y sin `version`, como los demás `DELETE` de la API.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from uuid import UUID

from django.db.models import QuerySet
from drf_spectacular.utils import extend_schema, extend_schema_view
from rest_framework import generics, serializers
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.conversations import quick_replies as qr
from apps.conversations import selectors
from apps.conversations.models import BODY_MAX, QuickReply
from core.api.errors import ApiError
from core.api.schema import errors
from core.api.strict import Closed, Flag, Id, Text, Whole, sentence, version_required
from core.tenancy import context

STALE = "Otra persona la cambió: vuelve a cargarla y repite el cambio."
TAKEN = "Ya hay una respuesta activa con ese atajo."
GENERIC = "Dato no admitido."
SHORTCUT_HELP = "Sin la `/`: hasta 32 letras sin acento, cifras, `_` o `-`; en minúsculas."
BODY_HELP = (
    f"Hasta {BODY_MAX} caracteres, con algo visible. Variables: "
    + ", ".join(f"`{{{{{key}}}}}`" for key in qr.VARIABLES)
    + "; otras llaves dobles o triples son un 400."
)


class QuickReplySerializer(serializers.Serializer[Any]):
    id = serializers.UUIDField()
    shortcut = serializers.CharField(help_text="Único entre las activas de la organización.")
    title = serializers.CharField()
    category_id = serializers.UUIDField()
    body = serializers.CharField()
    is_active = serializers.BooleanField()
    usage_count = serializers.IntegerField()
    version = serializers.IntegerField()
    created_at = serializers.DateTimeField()
    updated_at = serializers.DateTimeField()


class QuickReplyCreateSerializer(Closed, serializers.Serializer[Any]):
    shortcut = Text(max_length=64, help_text=SHORTCUT_HELP)
    title = Text(max_length=qr.NAME_MAX)
    category_id = Id(help_text="Una categoría viva y activa de la organización.")
    body = Text(max_length=BODY_MAX, trim_whitespace=False, help_text=BODY_HELP)


@version_required
class QuickReplyUpdateSerializer(Closed, serializers.Serializer[Any]):
    shortcut = Text(max_length=64, required=False)
    title = Text(max_length=qr.NAME_MAX, required=False)
    category_id = Id(required=False)
    body = Text(max_length=BODY_MAX, trim_whitespace=False, required=False)
    is_active = Flag(required=False, help_text="`false` la desactiva y libera el atajo.")
    version = Whole(min_value=1, max_value=2**31 - 1, help_text="La que se leyó; si no, 409.")


@contextmanager
def _domain() -> Iterator[None]:
    """Lo que lanzan los comandos, con la forma del contrato (ADR-014): 409 o 400 en su campo; un
    `DoesNotExist` es el 404 del manejador común."""
    try:
        yield
    except qr.StaleVersion:
        raise ApiError("STALE_VERSION", 409, STALE) from None
    except qr.Taken:
        raise ApiError("SHORTCUT_TAKEN", 409, TAKEN) from None
    except qr.Invalid as error:
        raise serializers.ValidationError({error.field: [sentence(error)]}) from None
    except ValueError:  # un comando rechazó algo que el serializador dejó pasar
        raise serializers.ValidationError(GENERIC) from None


@extend_schema_view(
    get=extend_schema(
        operation_id="quick_replies_list",
        tags=["conversations"],
        responses={200: QuickReplySerializer(many=True), **errors(400, 401, 403, 404)},
        description="Las respuestas rápidas no borradas, activas o no, por creación (ADR-016). "
        "Con `quick_replies.manage`.",
    ),
    post=extend_schema(
        operation_id="quick_replies_create",
        tags=["conversations"],
        request=QuickReplyCreateSerializer,
        responses={201: QuickReplySerializer, **errors(400, 401, 403, 404, 409)},
        description="Crea una respuesta activa (`quick_replies.manage`), auditada sin el texto "
        "(solo su huella). 400 en `category_id`: no es una categoría viva y activa de la "
        "organización. 409 `SHORTCUT_TAKEN`: otra activa ya usa el atajo.",
    ),
)
class QuickRepliesView(generics.ListAPIView):
    required_permissions = {"GET": "quick_replies.manage", "POST": "quick_replies.manage"}
    serializer_class = QuickReplySerializer

    def get_queryset(self) -> QuerySet[QuickReply]:
        return selectors.quick_replies()

    def post(self, request: Request, **kwargs: Any) -> Response:
        wanted = QuickReplyCreateSerializer(data=request.data)
        wanted.is_valid(raise_exception=True)
        with _domain():
            made = qr.create_reply(context.require(), **wanted.validated_data)
        return Response(QuickReplySerializer(made).data, status=201)


class QuickReplyView(APIView):
    required_permissions = {"PATCH": "quick_replies.manage", "DELETE": "quick_replies.manage"}

    @extend_schema(
        operation_id="quick_replies_update",
        tags=["conversations"],
        request=QuickReplyUpdateSerializer,
        responses={200: QuickReplySerializer, **errors(400, 401, 403, 404, 409)},
        description="Cambia lo enviado, también `is_active` (activar o desactivar), si `version` "
        "sigue vigente (409 `STALE_VERSION` si no); lo que no cambia no se escribe ni se "
        "audita. 409 `SHORTCUT_TAKEN`. 400 en `category_id` o en `is_active`: la respuesta "
        "quedaría activa en una categoría desactivada. Con `quick_replies.manage`.",
    )
    def patch(self, request: Request, quick_reply_id: UUID, **kwargs: Any) -> Response:
        wanted = QuickReplyUpdateSerializer(data=request.data)
        wanted.is_valid(raise_exception=True)
        fields = dict(wanted.validated_data)
        version = fields.pop("version")
        with _domain():
            done = qr.update_reply(
                context.require(), quick_reply_id=quick_reply_id, version=version, **fields
            )
        return Response(QuickReplySerializer(done).data)

    @extend_schema(
        operation_id="quick_replies_delete",
        tags=["conversations"],
        responses={204: None, **errors(401, 403, 404)},
        description="Borra la respuesta (borrado lógico, sin `version`): deja de listarse y su "
        "atajo queda libre. 404: no es de la organización o ya está borrada. Con "
        "`quick_replies.manage`.",
    )
    def delete(self, request: Request, quick_reply_id: UUID, **kwargs: Any) -> Response:
        with _domain():
            qr.delete_reply(context.require(), quick_reply_id=quick_reply_id)
        return Response(status=204)

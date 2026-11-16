"""Respuestas rápidas desde una conversación (F6-08a2, E06-10): buscarlas y obtener su texto
resuelto, sin enviar nada. La API de gestión es otra (F6-08a3, F6-08a4).

Las dos rutas piden `conversations.view` y `messages.send`, comprobados antes de leer nada. La
conversación se resuelve con `scoped()` y se comprueba con `can(…, "messages.send", …)`, como la
política de mensajería: desconocida, de otra organización o fuera del alcance de cualquiera de
los dos, el mismo 404. Las respuestas son de toda la organización (ORG).

`render` resuelve la lista cerrada de variables en una sola pasada (`quick_replies.render`). Las
del contacto solo con `contacts.view` sobre ese contacto (D-F6-10, como el texto de los mensajes);
sin él, o sin valor, salen vacías y nombradas en `missing`. Suma un uso; no crea mensajes ni
eventos: el envío sigue siendo `POST …/messages/`.
"""

from typing import Any
from uuid import UUID

from django.db.models import QuerySet
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema, extend_schema_view
from rest_framework import generics, serializers
from rest_framework.exceptions import NotFound
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.access.permissions import request_context
from apps.access.selectors import ExecutionContext, can, scoped
from apps.contacts.selectors import contacts
from apps.conversations import quick_replies, selectors
from apps.conversations.models import Conversation, QuickReply
from apps.organizations.models import Organization
from core.api.schema import errors
from core.tenancy import context
from core.text import line

SEARCH_MAX = 100
PERMISSIONS = ("conversations.view", "messages.send")
VARIABLES = [(key, key) for key in quick_replies.VARIABLES]


def _conversation(request: Request, conversation_id: UUID) -> tuple[ExecutionContext, Conversation]:
    """La conversación que quien llama ve y en la que puede escribir; si no, 404."""
    ectx = request_context(request)
    assert ectx is not None  # noqa: S101 — `HasPermission` ya lo comprobó
    mine = scoped(ectx, "conversations.view", Conversation.objects.all())
    found = mine.filter(pk=conversation_id).first()
    if found is None or not can(ectx, "messages.send", found):  # el mismo 404 en todo caso
        raise NotFound
    return ectx, found


def _values(ectx: ExecutionContext, found: Conversation) -> dict[str, str]:
    """Los valores de las variables para esta conversación: los del contacto, solo si quien llama
    lo ve (`contacts.view` y su alcance, D-F6-10)."""
    tenant = context.current()
    assert tenant is not None  # noqa: S101 — la ruta es de un tenant
    org = Organization.objects.filter(pk=tenant.organization_id).values_list("name", flat=True)
    values = {"org.name": org.first() or ""}
    seen = scoped(ectx, "contacts.view", contacts().filter(pk=found.contact_id))
    person = seen.values("first_name", "display_name").first()
    if person is not None:
        values |= {
            "contact.first_name": person["first_name"],
            "contact.name": person["display_name"],
        }
    return values


class QuickReplyCategoryRefSerializer(serializers.Serializer[Any]):
    id = serializers.UUIDField()
    name = serializers.CharField()
    sort_order = serializers.IntegerField(help_text="Para agrupar y ordenar por categoría.")


class QuickReplyUseSerializer(serializers.Serializer[Any]):
    id = serializers.UUIDField()
    shortcut = serializers.CharField(help_text="Sin la `/`: `delivery`.")
    title = serializers.CharField()
    body = serializers.CharField(help_text="Con sus variables sin resolver, como `{{org.name}}`.")
    category = QuickReplyCategoryRefSerializer()


class RenderedSerializer(serializers.Serializer[Any]):
    id = serializers.UUIDField()
    shortcut = serializers.CharField()
    text = serializers.CharField(help_text="El texto resuelto, para el cuadro de redacción.")
    missing = serializers.ListField(
        child=serializers.ChoiceField(choices=VARIABLES),
        help_text="Las variables que quedaron vacías: sin valor o sin `contacts.view` sobre el "
        "contacto.",
    )


@extend_schema_view(
    get=extend_schema(
        operation_id="conversation_quick_replies_list",
        tags=["conversations"],
        parameters=[
            OpenApiParameter(
                "q",
                OpenApiTypes.STR,
                required=False,
                description=f"Busca en el atajo (con o sin `/`), el título y el texto, sin "
                f"distinguir mayúsculas; hasta {SEARCH_MAX} caracteres.",
            ),  # fmt: skip
        ],
        responses={200: QuickReplyUseSerializer(many=True), **errors(400, 401, 403, 404)},
        description="Las respuestas rápidas que se pueden usar en la conversación: activas, en "
        "una categoría activa, por creación (ADR-016); cada una con su categoría para agruparlas. "
        "Pide `conversations.view` y `messages.send`; 404: la conversación no existe, es de otra "
        "organización o queda fuera del alcance.",
    ),
)
class QuickRepliesView(generics.ListAPIView):
    required_permissions = {"GET": PERMISSIONS}
    serializer_class = QuickReplyUseSerializer

    def get_queryset(self) -> QuerySet[QuickReply]:
        if getattr(self, "swagger_fake_view", False):  # el generador de OpenAPI
            return QuickReply.objects.none()
        _conversation(self.request, self.kwargs["conversation_id"])
        wanted = self.request.query_params.get("q", "")
        try:
            search = line(wanted, SEARCH_MAX).lstrip("/").lower()
        except ValueError:
            message = f"Una línea de hasta {SEARCH_MAX} caracteres imprimibles."
            raise serializers.ValidationError({"q": [message]}) from None
        return selectors.quick_replies(search)


@extend_schema_view(
    post=extend_schema(
        operation_id="conversation_quick_replies_render",
        tags=["conversations"],
        request=None,
        responses={200: RenderedSerializer, **errors(401, 403, 404)},
        description="El texto de la respuesta con sus variables resueltas para esta conversación, "
        "y las que quedaron vacías. Suma un uso. **No envía nada**: el texto se envía, si se "
        "quiere, con `POST …/messages/`. Pide `conversations.view` y `messages.send`; 404: la "
        "conversación o la respuesta no existen, no se pueden usar o quedan fuera del alcance.",
    ),
)
class RenderView(APIView):
    required_permissions = {"POST": PERMISSIONS}

    def post(
        self, request: Request, conversation_id: UUID, quick_reply_id: UUID, **kwargs: Any
    ) -> Response:
        ectx, found = _conversation(request, conversation_id)
        tenant = context.current()
        assert tenant is not None  # noqa: S101 — la ruta es de un tenant
        try:
            used = quick_replies.use_reply(tenant, quick_reply_id=quick_reply_id)
        except QuickReply.DoesNotExist:
            raise NotFound from None
        text, missing = quick_replies.render(used.body, _values(ectx, found))
        rendered = {"id": used.pk, "shortcut": used.shortcut, "text": text, "missing": missing}
        return Response(RenderedSerializer(rendered).data)

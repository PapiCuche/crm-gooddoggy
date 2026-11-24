"""Rutas de tenant del Inbox (F6-02a, E06): `…/conversations/` (lista) y su detalle.

`conversations.view` admite alcance (`Visible`: las propias, las de su equipo y las sin asignar):
`ScopeFilter` deja en el listado y en `get_object()` solo las que la política permite, y fuera del
alcance la conversación no existe: 404, igual que una desconocida o la de otra organización. El
permiso de la ruta se comprueba antes de leer nada, y el cuerpo antes que el 404.

El contacto sale con `id` y `display_name`, y el nombre solo si quien consulta puede ver ese
contacto (`contacts.view` y su alcance); si no, `null`. Los mensajes y el simulador, en
`messages.py`.
"""

from typing import Any

from django.db.models import Case, Exists, F, OuterRef, QuerySet, Subquery, When
from drf_spectacular.utils import extend_schema, extend_schema_field, extend_schema_view
from rest_framework import generics, serializers
from rest_framework.filters import BaseFilterBackend
from rest_framework.request import Request
from rest_framework.response import Response

from apps.access.permissions import ScopeFilter, request_context
from apps.access.selectors import scoped
from apps.contacts.selectors import contacts
from apps.conversations import limits, selectors
from apps.conversations.api.attention import human_request
from apps.conversations.models import ChannelAccount, Conversation, Platform, Status
from core.api.pagination import RecencyPagination
from core.api.schema import errors
from core.tenancy import context

BOXES = (("all", "all"), ("mine", "mine"), ("unassigned", "unassigned"))
FILTERS = ("box", "status", "unanswered")


class ConversationContactSerializer(serializers.Serializer[Any]):
    id = serializers.UUIDField(source="contact_id")
    display_name = serializers.CharField(
        source="contact_name",
        allow_null=True,
        help_text="`null` si quien consulta no puede ver ese contacto (`contacts.view`).",
    )


class ChannelRefSerializer(serializers.Serializer[Any]):
    id = serializers.UUIDField()
    platform = serializers.ChoiceField(choices=Platform.choices)
    name = serializers.CharField()
    text_max = serializers.SerializerMethodField(
        help_text="El tope del texto de una respuesta en este canal (F7b-16), en `text_unit`."
    )
    text_unit = serializers.SerializerMethodField(
        help_text="`CHARACTERS` (puntos de código) o `BYTES` (UTF-8, Instagram)."
    )
    accepts_media = serializers.SerializerMethodField(
        help_text="Si una respuesta puede llevar un adjunto (no en Messenger ni en Instagram)."
    )

    def get_text_max(self, account: ChannelAccount) -> int:
        return limits.text_limit(account.platform)[1]

    @extend_schema_field(serializers.ChoiceField(choices=[limits.CHARACTERS, limits.BYTES]))
    def get_text_unit(self, account: ChannelAccount) -> str:
        return limits.text_limit(account.platform)[0]

    def get_accepts_media(self, account: ChannelAccount) -> bool:
        return limits.takes_media(account.platform)


class ConversationSerializer(serializers.Serializer[Any]):
    id = serializers.UUIDField()
    number = serializers.CharField(help_text="`CONV-00191`: único en la organización.")
    status = serializers.ChoiceField(choices=Status.choices)
    attention_mode = serializers.CharField()
    assignee_kind = serializers.CharField(help_text="`USER`, `TEAM` o `UNASSIGNED`.")
    assigned_user_id = serializers.UUIDField(allow_null=True)
    assigned_team_id = serializers.UUIDField(allow_null=True)
    contact = ConversationContactSerializer(source="*")
    channel = ChannelRefSerializer(source="channel_account")
    last_message_at = serializers.DateTimeField()
    last_inbound_at = serializers.DateTimeField(allow_null=True)
    last_message_preview = serializers.CharField(
        source="preview",
        allow_null=True,
        help_text="El comienzo del último mensaje (nunca de una nota interna); `null` si no hay o "
        "quien consulta no puede ver el contacto (`contacts.view`), igual que su nombre.",
    )
    last_message_direction = serializers.ChoiceField(
        choices=[("INBOUND", "INBOUND"), ("OUTBOUND", "OUTBOUND")], allow_null=True
    )
    last_reply_failed = serializers.BooleanField(
        help_text="F7b-14: lo último es nuestro, pero ninguna respuesta desde el último mensaje "
        "del cliente le llegó (fallaron): sigue sin responder."
    )
    standby_since = serializers.DateTimeField(
        allow_null=True,
        help_text="Instagram o Messenger: desde cuándo otra app controla el hilo (`standby`); "
        "`null` si no. Mientras tanto nada responde solo (ADR-035 §4).",
    )
    human_request = human_request()  # F8-08n
    ai_excluded_at = serializers.DateTimeField(
        allow_null=True, help_text="F8-13: desde cuándo está excluida de la IA; `null`: no lo está."
    )
    version = serializers.IntegerField()


class ConversationDetailSerializer(ConversationSerializer):
    assigned_at = serializers.DateTimeField(allow_null=True)
    assignment_version = serializers.IntegerField()
    resolved_at = serializers.DateTimeField(allow_null=True)
    closed_at = serializers.DateTimeField(allow_null=True)
    created_at = serializers.DateTimeField()
    updated_at = serializers.DateTimeField()


class BoxFilterSerializer(serializers.Serializer[Any]):
    box = serializers.ChoiceField(
        choices=BOXES,
        required=False,
        error_messages={"invalid_choice": "No válido."},
        help_text="`all` (por omisión), `mine` (asignadas a quien consulta) o `unassigned`.",
    )


class ConversationFilterSerializer(BoxFilterSerializer):
    status = serializers.ChoiceField(
        choices=Status.choices, required=False, error_messages={"invalid_choice": "No válido."}
    )
    unanswered = serializers.BooleanField(
        required=False,
        help_text="`true`: solo las abiertas cuyo último mensaje es del cliente o cuya respuesta "
        "falló (sin responder).",
    )


class UnansweredCountSerializer(serializers.Serializer[Any]):
    count = serializers.IntegerField()


class ConversationFilters(BaseFilterBackend):
    """`?box=`, `?status=` y `?unanswered=`, detrás de `ScopeFilter`: recortan lo que el alcance ya
    dejó. Cada uno, una sola vez; vacío es lo mismo que no pedirlo."""

    def filter_queryset(self, request: Any, queryset: Any, view: Any) -> QuerySet[Conversation]:
        params, names = request.query_params, getattr(view, "filters", FILTERS)
        if repeated := [k for k in names if len(params.getlist(k)) > 1]:
            raise serializers.ValidationError({k: ["Una sola vez."] for k in repeated})
        given = {k: params[k] for k in names if params.get(k)}
        wanted = ConversationFilterSerializer(data=given)
        wanted.is_valid(raise_exception=True)
        found: QuerySet[Conversation] = queryset
        if (status := wanted.validated_data.get("status")) is not None:
            found = found.filter(status=status)
        if wanted.validated_data.get("unanswered"):
            found = found.filter(selectors.UNANSWERED)
        match wanted.validated_data.get("box", "all"):
            case "mine":
                found = found.filter(assigned_user_id=context.current().user_id)  # type: ignore[union-attr]
            case "unassigned":
                found = found.filter(assignee_kind="UNASSIGNED")
        return found


def with_contact_content(found: QuerySet[Conversation], request: Any) -> QuerySet[Conversation]:
    """`contact_name` y `preview`, el contenido que identifica al cliente: el nombre del contacto
    y el comienzo de su último mensaje salen solo si `contacts.view` y su alcance dejan ver ese
    contacto (la misma regla en la lista y en el detalle, en la misma consulta); si no, `NULL`
    (sin el permiso, `scoped()` no deja ninguna fila)."""
    ectx = request_context(request)
    assert ectx is not None  # noqa: S101 — `HasPermission` ya lo comprobó
    visible = scoped(ectx, "contacts.view", contacts().filter(pk=OuterRef("contact_id")))
    return found.annotate(
        contact_name=Subquery(visible.values("display_name")[:1]),
        preview=Case(When(Exists(visible), then=F("last_message_preview"))),
    )


class ConversationsQuery:
    def get_queryset(self) -> QuerySet[Conversation]:
        found = selectors.conversations()
        request = getattr(self, "request", None)
        return found if request is None else with_contact_content(found, request)  # OpenAPI: sin


@extend_schema_view(
    get=extend_schema(
        operation_id="conversations_list",
        tags=["conversations"],
        parameters=[ConversationFilterSerializer],
        responses={200: ConversationSerializer(many=True), **errors(400, 401, 403, 404)},
        description="Las conversaciones dentro del alcance de `conversations.view`, la de mensaje "
        "más reciente primero (`last_message_at`, `id`; cursor de dos valores). `box`, `status` y "
        "`unanswered` se combinan y la página siguiente se pide con los mismos. 400: un filtro "
        "repetido o no válido.",
    ),
)
class ConversationsView(ConversationsQuery, generics.ListAPIView):
    required_permissions = {"GET": "conversations.view"}
    serializer_class = ConversationSerializer
    filter_backends = [ScopeFilter, ConversationFilters]  # en este orden
    pagination_class = RecencyPagination
    recency_field = "last_message_at"


@extend_schema_view(
    get=extend_schema(
        operation_id="conversations_retrieve",
        tags=["conversations"],
        responses={200: ConversationDetailSerializer, **errors(401, 403, 404)},
        description="Una conversación. 404: no es de la organización o queda fuera del alcance de "
        "`conversations.view`.",
    ),
)
class ConversationView(ConversationsQuery, generics.RetrieveAPIView):
    required_permissions = {"GET": "conversations.view"}
    serializer_class = ConversationDetailSerializer
    lookup_url_kwarg = "conversation_id"


@extend_schema_view(
    get=extend_schema(
        operation_id="conversations_unanswered_count",
        tags=["conversations"],
        parameters=[BoxFilterSerializer],
        responses={200: UnansweredCountSerializer, **errors(400, 401, 403, 404)},
        description="Cuántas conversaciones sin responder (`unanswered=true` de la lista) hay "
        "dentro del alcance de `conversations.view` y de `box`. Solo el número.",
    ),
)
class UnansweredCountView(generics.GenericAPIView[Conversation]):
    required_permissions = {"GET": "conversations.view"}
    filter_backends = [ScopeFilter, ConversationFilters]  # en este orden
    filters = ("box",)

    def get_queryset(self) -> QuerySet[Conversation]:
        return Conversation.objects.all()

    def get(self, request: Request, **kwargs: Any) -> Response:
        found = self.filter_queryset(self.get_queryset()).filter(selectors.UNANSWERED)
        return Response({"count": found.count()})

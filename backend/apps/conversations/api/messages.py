"""Mensajes de una conversación y el simulador del canal Sandbox (F6-02c, E06).

`GET …/conversations/{id}/messages/`: la conversación se resuelve con `scoped()` antes de leer
un mensaje (desconocida, ajena o fuera del alcance: el mismo 404); las notas, solo con
`messages.note`. `POST …/channels/sandbox/inbound/` simula un mensaje del cliente.
"""

from typing import Any
from uuid import UUID

from django.db.models import QuerySet
from drf_spectacular.utils import extend_schema, extend_schema_view
from rest_framework import generics, serializers
from rest_framework.exceptions import NotFound
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.access.permissions import ScopeFilter, rbac_errors, request_context
from apps.access.selectors import can, scoped
from apps.contacts.selectors import contacts
from apps.conversations import commands, policy, selectors
from apps.conversations.api import inputs
from apps.conversations.models import FAILURE_CODES, Conversation, Direction, Message
from core.api.errors import ApiError
from core.api.pagination import CursorPagination
from core.api.schema import errors
from core.tenancy import context

TAKEN = "Ese id externo ya es de otro mensaje."
BAD_CONTACT = "No es un contacto de la organización que puedas ver."


def shows_body(ectx: Any, found: Conversation) -> bool:
    """D-F6-10: el texto, como el nombre del contacto, pide poder ver a ese contacto."""
    return scoped(ectx, "contacts.view", contacts().filter(pk=found.contact_id)).exists()


class MessageSerializer(serializers.Serializer[Any]):
    id = serializers.UUIDField()
    direction = serializers.ChoiceField(choices=Direction.choices)
    sender_type = serializers.CharField()
    sender_user_id = serializers.UUIDField(allow_null=True)
    message_type = serializers.CharField(help_text="`TEXT`, `NOTE` o `UNSUPPORTED`.")
    body = serializers.CharField(
        allow_null=True, help_text="`null` si no se puede ver el contacto (`contacts.view`)."
    )
    client_message_id = serializers.UUIDField(
        allow_null=True, help_text="Solo en los mensajes salientes de quien consulta."
    )
    delivery_status = serializers.CharField(allow_null=True, help_text="Solo en lo saliente.")
    provider_error_code = serializers.ChoiceField(
        choices=[(code, code) for code in FAILURE_CODES],
        allow_null=True,
        help_text="Por qué un mensaje saliente quedó `FAILED`: un código cerrado propio, nunca el "
        "texto del proveedor; `null` si no falló (o falló antes de que se guardara el motivo).",
    )
    created_at = serializers.DateTimeField()
    sent_at = serializers.DateTimeField(allow_null=True)
    delivered_at = serializers.DateTimeField(allow_null=True)
    read_at = serializers.DateTimeField(allow_null=True)
    failed_at = serializers.DateTimeField(allow_null=True)

    def to_representation(self, instance: Message) -> dict[str, Any]:
        data: dict[str, Any] = super().to_representation(instance)
        mine = context.current()
        own = instance.direction == "OUTBOUND" and instance.sender_user_id == mine.user_id  # type: ignore[union-attr]
        if not own:  # la clave de idempotencia solo le sirve a quien envió
            data["client_message_id"] = None
        if not self.context.get("show_body", False):  # D-F6-10; sin el dato, cerrado
            data["body"] = None
        return data


class SandboxInboundResultSerializer(serializers.Serializer[Any]):
    created = serializers.BooleanField(help_text="`false`: era un reintento del mismo mensaje.")
    conversation_id = serializers.UUIDField()
    message = MessageSerializer()


REASONS = {
    "CONVERSATION_CLOSED": "La conversación está cerrada. Podrás responder cuando el cliente "
    "vuelva a escribir.",
    "CHANNEL_UNAVAILABLE": "El canal de esta conversación no está conectado. Reconéctalo para "
    "enviar mensajes.",
    "CONTACT_BLOCKED": "Este contacto está bloqueado: no se le pueden enviar mensajes.",
    "WINDOW_CLOSED": "Ya pasó la ventana de atención del canal desde el último mensaje del "
    "cliente. Podrás responder cuando el cliente vuelva a escribir.",
    "CONSENT_REVOKED": "El cliente se dio de baja de los mensajes por iniciativa nuestra en este "
    "canal. Podrás responderle cuando vuelva a escribir.",
    "CLIENT_MESSAGE_ID_TAKEN": "Ese id de mensaje ya se usó para otro envío.",
}


@extend_schema_view(
    get=extend_schema(
        operation_id="conversation_messages_list",
        tags=["conversations"],
        responses={200: MessageSerializer(many=True), **errors(400, 401, 403, 404)},
        description="Los mensajes de una conversación, el más reciente primero, 50 por página: "
        "`cursor` (el `next` anterior) pide los más antiguos. Las notas internas solo salen con "
        "`messages.note` sobre la conversación, y el texto (`body`) solo con `contacts.view` sobre "
        "el contacto. 404: no existe o está fuera del alcance.",
    ),
    post=extend_schema(
        operation_id="conversation_messages_send",
        tags=["conversations"],
        request=inputs.SendMessageSerializer,
        responses={
            200: MessageSerializer,
            202: MessageSerializer,
            **errors(400, 401, 403, 404, 409),
        },
        description="Pone en cola un mensaje al cliente (`QUEUED`; el envío por el canal es de la "
        "tarea de salida), idempotente por `client_message_id`: 202 la primera vez y 200 con el "
        "mismo mensaje en un reintento igual. Pide `messages.send` y `conversations.view`; no "
        "hace falta ver al contacto (el `body` de la respuesta sigue la regla de la lectura). 409: "
        "`CONVERSATION_CLOSED`, `CHANNEL_UNAVAILABLE` (canal sin conectar), `CONTACT_BLOCKED`, "
        "`WINDOW_CLOSED` (pasó la ventana de atención del canal desde el último mensaje del "
        "cliente) o `CLIENT_MESSAGE_ID_TAKEN` (ese id es de otro texto, otra conversación u otra "
        "persona).",
    ),
)
class MessagesView(generics.ListAPIView):
    required_permissions = {
        "GET": "conversations.view",
        "POST": ("messages.send", "conversations.view"),
    }
    serializer_class = MessageSerializer
    pagination_class = CursorPagination
    ordering = "-id"
    filter_backends = [ScopeFilter]  # los mensajes de las conversaciones que `Visible` deja ver

    def get_queryset(self) -> QuerySet[Message]:
        if getattr(self, "swagger_fake_view", False):  # el generador de OpenAPI
            return Message.objects.none()
        ectx = request_context(self.request)
        assert ectx is not None  # noqa: S101 — `HasPermission` ya lo comprobó
        mine = scoped(ectx, "conversations.view", Conversation.objects.all())
        found = mine.filter(pk=self.kwargs["conversation_id"]).first()
        if found is None:  # desconocida, de otra organización o fuera del alcance: la misma
            raise NotFound
        self.show_body = shows_body(ectx, found)
        return selectors.messages(found.pk, notes=can(ectx, "messages.note", found))

    def get_serializer_context(self) -> dict[str, Any]:
        return {**super().get_serializer_context(), "show_body": getattr(self, "show_body", False)}

    def post(self, request: Request, **kwargs: Any) -> Response:
        wanted = inputs.SendMessageSerializer(data=request.data)  # el cuerpo, antes del 404
        wanted.is_valid(raise_exception=True)
        ectx, tenant = request_context(request), context.current()
        assert ectx is not None and tenant is not None  # noqa: S101 — `HasPermission` ya lo comprobó
        mine = scoped(ectx, "conversations.view", Conversation.objects.all())
        found = mine.filter(pk=self.kwargs["conversation_id"]).first()
        if found is None:  # desconocida, de otra organización o fuera del alcance: la misma
            raise NotFound
        try:
            with rbac_errors():  # el comando vuelve a comprobar `messages.send` y su alcance
                message, created = commands.record_outbound(
                    tenant, conversation_id=found.pk, body=wanted.validated_data["body"],
                    client_message_id=wanted.validated_data["client_message_id"],
                    purpose=policy.CUSTOMER_REPLY,  # la pantalla solo responde al cliente
                )  # fmt: skip
        except tuple(policy.DENIALS.values()) as blocked:  # la política dijo que no
            code = policy.CODES[type(blocked)]
            raise ApiError(code, 409, REASONS[code]) from None
        except commands.ClientMessageConflict:
            raise ApiError(
                "CLIENT_MESSAGE_ID_TAKEN", 409, REASONS["CLIENT_MESSAGE_ID_TAKEN"]
            ) from None
        if not created and message.sender_user_id != tenant.user_id:  # el id es de otra persona
            raise ApiError("CLIENT_MESSAGE_ID_TAKEN", 409, REASONS["CLIENT_MESSAGE_ID_TAKEN"])
        shown = MessageSerializer(message, context={"show_body": shows_body(ectx, found)})
        return Response(shown.data, status=202 if created else 200)


class MessagingPolicySerializer(serializers.Serializer[Any]):
    allowed = serializers.BooleanField()
    mode = serializers.ChoiceField(
        choices=[(policy.FREE_FORM, policy.FREE_FORM), (policy.BLOCKED, policy.BLOCKED)]
    )
    reason = serializers.ChoiceField(
        choices=[(reason, reason) for reason in policy.REASONS],
        allow_null=True,
        help_text="Por qué no; `null` si se puede.",
    )
    window_expires_at = serializers.DateTimeField(
        allow_null=True, help_text="Cuándo cierra (o cerró) la ventana; `null` si no hay."
    )
    window_hours = serializers.IntegerField(allow_null=True, help_text="`null`: sin límite.")
    evaluated_at = serializers.DateTimeField()


@extend_schema_view(
    get=extend_schema(
        operation_id="conversation_messaging_policy_retrieve",
        tags=["conversations"],
        responses={200: MessagingPolicySerializer, **errors(401, 403, 404)},
        description="La decisión de la política de mensajería (ADR-010) para escribir ahora en la "
        "conversación, la misma que aplican el envío y el despachador. Pide `conversations.view` y "
        "`messages.send`; 404: no existe, es de otra organización o queda fuera del alcance de "
        "`messages.send`.",
    ),
)
class MessagingPolicyView(APIView):
    required_permissions = {"GET": ("conversations.view", "messages.send")}

    def get(self, request: Request, **kwargs: Any) -> Response:
        ectx = request_context(request)
        assert ectx is not None  # noqa: S101 — `HasPermission` ya lo comprobó
        mine = scoped(ectx, "conversations.view", Conversation.objects.all())
        found = mine.filter(pk=self.kwargs["conversation_id"]).first()
        if found is None or not can(ectx, "messages.send", found):  # el mismo 404 en todo caso
            raise NotFound
        return Response(MessagingPolicySerializer(selectors.messaging_decision(found)).data)


@extend_schema_view(
    post=extend_schema(
        operation_id="conversation_notes_create",
        tags=["conversations"],
        request=inputs.NoteSerializer,
        responses={
            200: MessageSerializer,
            201: MessageSerializer,
            **errors(400, 401, 403, 404, 409),
        },
        description="Añade una nota interna (`INTERNAL`, `NOTE`): nunca sale por un canal, el "
        "despachador la ignora y la lectura solo la muestra con `messages.note`. Idempotente por "
        "`client_message_id`: 201 la primera vez y 200 con la misma nota en un reintento igual. "
        "Pide `messages.note` y `conversations.view`; se admite también en una conversación "
        "cerrada. 409 `CLIENT_MESSAGE_ID_TAKEN`: ese id es de otro texto u otra persona.",
    ),
)
class NotesView(APIView):
    required_permissions = {"POST": ("messages.note", "conversations.view")}

    def post(self, request: Request, **kwargs: Any) -> Response:
        wanted = inputs.NoteSerializer(data=request.data)  # el cuerpo, antes del 404
        wanted.is_valid(raise_exception=True)
        ectx, tenant = request_context(request), context.current()
        assert ectx is not None and tenant is not None  # noqa: S101 — `HasPermission` ya lo comprobó
        mine = scoped(ectx, "conversations.view", Conversation.objects.all())
        found = mine.filter(pk=self.kwargs["conversation_id"]).first()
        if found is None:  # desconocida, de otra organización o fuera del alcance: la misma
            raise NotFound
        try:
            with rbac_errors():  # el comando vuelve a comprobar `messages.note` y su alcance
                note, created = commands.add_note(
                    tenant, conversation_id=found.pk, body=wanted.validated_data["body"],
                    client_message_id=wanted.validated_data["client_message_id"],
                )  # fmt: skip
        except commands.ClientMessageConflict:
            raise ApiError(
                "CLIENT_MESSAGE_ID_TAKEN", 409, REASONS["CLIENT_MESSAGE_ID_TAKEN"]
            ) from None
        if not created and note.sender_user_id != tenant.user_id:  # el id es de otra persona
            raise ApiError("CLIENT_MESSAGE_ID_TAKEN", 409, REASONS["CLIENT_MESSAGE_ID_TAKEN"])
        shown = MessageSerializer(note, context={"show_body": shows_body(ectx, found)})
        return Response(shown.data, status=201 if created else 200)


@extend_schema_view(
    post=extend_schema(
        operation_id="channels_sandbox_inbound",
        tags=["conversations"],
        request=inputs.SandboxInboundSerializer,
        responses={
            200: SandboxInboundResultSerializer,
            201: SandboxInboundResultSerializer,
            **errors(400, 401, 403, 404, 409),
        },
        description="Simula un mensaje del cliente por el canal Sandbox, idempotente por "
        "`external_message_id`: 201 la primera vez y 200 con `created: false` en un reintento "
        "igual. Pide `channels.manage` y `contacts.view`; 400 en `contact_id` si no es un "
        "contacto vivo que quien llama pueda ver. 409 `EXTERNAL_ID_TAKEN`: ese id ya es de "
        "un mensaje con otro texto u otro contacto.",
    ),
)
class SandboxInboundView(APIView):
    required_permissions = {"POST": ("channels.manage", "contacts.view")}

    def post(self, request: Request, **kwargs: Any) -> Response:
        wanted = inputs.SandboxInboundSerializer(data=request.data)
        wanted.is_valid(raise_exception=True)
        data = wanted.validated_data
        ectx, tenant = request_context(request), context.current()
        assert ectx is not None and tenant is not None  # noqa: S101 — `HasPermission` ya lo comprobó
        contact_id: UUID = data["contact_id"]
        seen = scoped(ectx, "contacts.view", contacts()).filter(pk=contact_id)
        if not seen.select_for_update(no_key=True).values_list("pk", flat=True)[:1]:
            raise serializers.ValidationError({"contact_id": [BAD_CONTACT]})
        account = commands.ensure_sandbox_account(tenant)
        try:
            message, created = commands.record_inbound(
                tenant,
                contact_id=contact_id,
                channel_account_id=account.pk,
                external_message_id=data["external_message_id"],
                body=data["text"],
            )
        except commands.BadReference as bad:
            raise serializers.ValidationError({bad.field: [BAD_CONTACT]}) from None
        same = Conversation.objects.filter(pk=message.conversation_id, contact_id=contact_id)
        if not created and (message.body != data["text"] or not same.exists()):
            raise ApiError("EXTERNAL_ID_TAKEN", 409, TAKEN)
        result = {
            "created": created,
            "conversation_id": message.conversation_id,
            "message": message,
        }
        return Response(
            SandboxInboundResultSerializer(result, context={"show_body": True}).data,
            status=201 if created else 200,
        )

"""Notas de un cliente, `…/contacts/{id}/notes/` (F4-09): sin alcance propio, se llega por el
contacto, que `ScopeFilter` filtra con los permisos del método. Leer exige `contacts.view`;
escribir, también `contacts.update`, y en la nota de otro con `ORGANIZATION` (a confirmar por el
mantenedor). Fuera del alcance, inexistente, borrado, de otra organización o ajeno: el mismo 404."""

from typing import Any, cast

from drf_spectacular.extensions import OpenApiSerializerExtension
from drf_spectacular.utils import extend_schema, extend_schema_view
from rest_framework import generics, serializers
from rest_framework.exceptions import NotFound
from rest_framework.request import Request
from rest_framework.response import Response

from apps.access.catalog import Scope
from apps.access.permissions import request_context
from apps.access.selectors import ExecutionContext
from apps.contacts import notes as commands
from apps.contacts.models import Contact, Note
from apps.contacts.selectors import contacts, notes
from core.api.errors import ApiError
from core.api.schema import errors

STALE = "Otra persona cambió la nota: vuelve a cargarla y repite el cambio."
WRITE = ("contacts.view", "contacts.update")
BODY = f"Una o varias líneas, hasta {commands.BODY_MAX} caracteres. No es HTML."


class NoteSerializer(serializers.Serializer[Any]):
    id = serializers.UUIDField()
    contact_id = serializers.UUIDField()
    body = serializers.CharField()
    is_pinned = serializers.BooleanField()
    author_user_id = serializers.UUIDField()
    updated_by_user_id = serializers.UUIDField(allow_null=True)
    created_at = serializers.DateTimeField()
    updated_at = serializers.DateTimeField()
    version = serializers.IntegerField()


class BodyField(serializers.CharField):
    def to_internal_value(self, data: Any) -> str:
        try:
            return commands.clean_body(super().to_internal_value(data))
        except ValueError as error:  # el texto del comando, que no repite el cuerpo
            raise serializers.ValidationError(f"{str(error).capitalize()}.") from None


class NoteWriteSerializer(serializers.Serializer[Any]):
    body = BodyField(help_text=BODY)
    is_pinned = serializers.BooleanField(required=False, default=False)


class NoteUpdateSerializer(serializers.Serializer[Any]):
    body = BodyField(required=False, help_text=BODY)
    is_pinned = serializers.BooleanField(required=False)
    version = serializers.IntegerField(min_value=1, help_text="La que se leyó; si no, 409.")


class NoteUpdateSchema(OpenApiSerializerExtension):  # spectacular haría opcional la `version`
    target_class = NoteUpdateSerializer

    def map_serializer(self, auto_schema: Any, direction: Any) -> Any:
        schema = auto_schema._map_serializer(self.target, direction, bypass_extensions=True)
        return {**schema, "required": ["version"]}


class ContactNotes(generics.GenericAPIView):
    serializer_class = NoteSerializer
    ordering = "-id"  # las más recientes primero; el cursor no admite fijadas delante (ADR-016)

    def ectx(self) -> ExecutionContext:  # `HasPermission` ya comprobó que existe
        return cast(ExecutionContext, request_context(self.request))

    def contact(self, *, lock: bool) -> Contact:  # `ScopeFilter`, solo sobre el contacto
        found = self.filter_queryset(
            contacts().select_for_update(no_key=True) if lock else contacts()
        )
        contact: Contact = generics.get_object_or_404(found, pk=self.kwargs["contact_id"])
        self.check_object_permissions(self.request, contact)  # red de seguridad
        return contact


@extend_schema_view(
    get=extend_schema(
        operation_id="contact_notes_list",
        tags=["contacts"],
        responses={200: NoteSerializer(many=True), **errors(400, 401, 403, 404)},
    ),
    post=extend_schema(
        operation_id="contact_notes_create",
        tags=["contacts"],
        request=NoteWriteSerializer,
        responses={201: NoteSerializer, **errors(400, 401, 403, 404)},
    ),
)
class NotesView(ContactNotes, generics.ListAPIView):
    required_permissions = {"GET": "contacts.view", "POST": WRITE}

    def list(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        page = self.paginate_queryset(notes(self.contact(lock=False).pk))
        return self.get_paginated_response(NoteSerializer(page, many=True).data)

    def post(self, request: Request, **kwargs: Any) -> Response:
        wanted = NoteWriteSerializer(data=request.data)
        wanted.is_valid(raise_exception=True)
        contact_id, tenant = self.contact(lock=True).pk, self.ectx().tenant
        note = commands.create_note(tenant, contact_id=contact_id, **wanted.validated_data)
        return Response(NoteSerializer(note).data, status=201)


@extend_schema_view(
    patch=extend_schema(
        operation_id="contact_notes_update",
        tags=["contacts"],
        request=NoteUpdateSerializer,
        responses={200: NoteSerializer, **errors(400, 401, 403, 404, 409)},
    ),
    delete=extend_schema(
        operation_id="contact_notes_delete",
        tags=["contacts"],
        responses={204: None, **errors(401, 403, 404)},
    ),
)
class NoteView(ContactNotes):
    required_permissions = {"PATCH": WRITE, "DELETE": WRITE}

    def note(self) -> Note:  # bloqueada, si es del autor o se edita en toda la organización
        contact, ectx = self.contact(lock=True), self.ectx()
        found = notes(contact.pk).select_for_update(no_key=True)
        note: Note = generics.get_object_or_404(found, pk=self.kwargs["note_id"])
        anyone = Scope.ORGANIZATION in ectx.permissions.get("contacts.update", ())
        if note.author_user_id != ectx.tenant.user_id and not anyone:
            raise NotFound
        return note

    def patch(self, request: Request, **kwargs: Any) -> Response:
        wanted = NoteUpdateSerializer(data=request.data)
        wanted.is_valid(raise_exception=True)
        note, tenant = self.note(), self.ectx().tenant
        try:
            note = commands.update_note(
                tenant, contact_id=note.contact_id, note_id=note.pk, **wanted.validated_data
            )
        except commands.StaleNote:
            raise ApiError("NOTE_VERSION_CONFLICT", 409, STALE) from None
        return Response(NoteSerializer(note).data)

    def delete(self, request: Request, **kwargs: Any) -> Response:
        note = self.note()
        commands.delete_note(self.ectx().tenant, contact_id=note.contact_id, note_id=note.pk)
        return Response(status=204)

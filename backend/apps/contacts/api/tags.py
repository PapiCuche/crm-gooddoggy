"""Rutas de tenant de las etiquetas de contactos (F4-11, E05-03): el catálogo `…/tags/`.

Lo lee quien ve contactos (`contacts.view`, con cualquier alcance: la política de `Tag` deja
verlas todas) y lo cambia `settings.manage` (03 §H, «Organización»).
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

from apps.contacts.api.views import _message, _tenant
from apps.contacts.models import Tag
from apps.contacts.selectors import tags
from apps.contacts.tags import (
    NAME_MAX,
    TagNameTaken,
    create_tag,
    tag_color,
    tag_name,
    update_tag,
)
from core.api.errors import ApiError
from core.api.schema import errors

TAKEN = "Ya existe una etiqueta con ese nombre en la organización."
COLOR_HELP = "`#RRGGBB`; se guarda en minúsculas."


class TagSerializer(serializers.Serializer[Any]):
    id = serializers.UUIDField()
    name = serializers.CharField(help_text="Único en la organización, sin distinguir mayúsculas.")
    color = serializers.CharField(help_text="`#rrggbb`.")
    applies_to = serializers.ChoiceField(
        choices=Tag.Target.choices, help_text="Por ahora, CONTACT."
    )
    is_active = serializers.BooleanField(help_text="Una desactivada no se pone en más contactos.")


class TagWriteSerializer(serializers.Serializer[Any]):
    name = serializers.CharField(max_length=NAME_MAX)
    color = serializers.CharField(max_length=16, help_text=COLOR_HELP)

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        problems = {}
        for field, clean in (("name", tag_name), ("color", tag_color)):
            if field in attrs:
                try:
                    attrs[field] = clean(attrs[field])
                except ValueError as error:
                    problems[field] = _message(error)
        if problems:
            raise serializers.ValidationError(problems)
        return attrs


class TagUpdateSerializer(TagWriteSerializer):
    name = serializers.CharField(max_length=NAME_MAX, required=False)
    color = serializers.CharField(max_length=16, required=False, help_text=COLOR_HELP)
    is_active = serializers.BooleanField(required=False, help_text="`false` la desactiva.")


@extend_schema_view(
    get=extend_schema(
        operation_id="tags_list",
        tags=["contacts"],
        responses={200: TagSerializer(many=True), **errors(400, 401, 403, 404)},
        description="Las etiquetas de contactos, activas o no, por creación (ADR-016). Con "
        "`contacts.view` y cualquier alcance.",
    ),
    post=extend_schema(
        operation_id="tags_create",
        tags=["contacts"],
        request=TagWriteSerializer,
        responses={201: TagSerializer, **errors(400, 401, 403, 404, 409)},
        description="Crea una etiqueta de contactos, activa (`settings.manage`). 409 "
        "`TAG_NAME_TAKEN`: ya hay una con ese nombre, sin distinguir mayúsculas, activa o no.",
    ),
)
class TagsView(generics.ListAPIView):
    required_permissions = {"GET": "contacts.view", "POST": "settings.manage"}
    serializer_class = TagSerializer

    def get_queryset(self) -> QuerySet[Tag]:
        return tags()

    def post(self, request: Request, **kwargs: Any) -> Response:
        wanted = TagWriteSerializer(data=request.data)
        wanted.is_valid(raise_exception=True)
        try:
            tag = create_tag(_tenant(), **wanted.validated_data)
        except TagNameTaken:
            raise ApiError("TAG_NAME_TAKEN", 409, TAKEN) from None
        return Response(TagSerializer(tag).data, status=201)


class TagView(APIView):
    required_permissions = {"PATCH": "settings.manage"}

    @extend_schema(
        operation_id="tags_update",
        tags=["contacts"],
        request=TagUpdateSerializer,
        responses={200: TagSerializer, **errors(400, 401, 403, 404, 409)},
        description="Renombra, cambia el color o activa y desactiva (`settings.manage`); lo que "
        "no cambia no se escribe. 409 `TAG_NAME_TAKEN`. Las etiquetas no se borran. 404: no es "
        "de la organización.",
    )
    def patch(self, request: Request, tag_id: UUID, **kwargs: Any) -> Response:
        wanted = TagUpdateSerializer(data=request.data)
        wanted.is_valid(raise_exception=True)
        try:
            tag = update_tag(_tenant(), tag_id=tag_id, **wanted.validated_data)
        except Tag.DoesNotExist:
            raise NotFound from None
        except TagNameTaken:
            raise ApiError("TAG_NAME_TAKEN", 409, TAKEN) from None
        return Response(TagSerializer(tag).data)

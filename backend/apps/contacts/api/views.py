"""Rutas de tenant de los contactos: `…/contacts/` (E05-01). Lectura (F4-02), escrituras (F4-03)
y búsqueda (F4-07, `?q=`).

`contacts.view`, `contacts.update` y `contacts.delete` admiten alcance: `ScopeFilter` deja en el
listado y en `get_object()` solo los contactos que la política de `Contact` permite con los
permisos del método (ADR-003 §5). Fuera del alcance, el contacto no existe: 404, igual que el de
otra organización o uno borrado. Editar o borrar exige además verlo (`contacts.view`), como el
cambio de responsable (F4-08): la respuesta de `PATCH` lleva sus datos personales.
"""

from collections.abc import Callable
from typing import Any
from uuid import UUID

from django.db.models import QuerySet
from drf_spectacular.extensions import OpenApiSerializerExtension
from drf_spectacular.utils import extend_schema, extend_schema_view
from rest_framework import generics, serializers
from rest_framework.filters import BaseFilterBackend
from rest_framework.request import Request
from rest_framework.response import Response

from apps.access.permissions import ScopeFilter, request_context
from apps.contacts.commands import (
    EMAIL_MAX,
    NAME_MAX,
    PART_MAX,
    BadReference,
    StaleContact,
    assign_contact,
    create_contact,
    delete_contact,
    document,
    email,
    phone,
    update_contact,
)
from apps.contacts.models import Contact
from apps.contacts.selectors import contacts, search
from core.api.errors import ApiError
from core.api.schema import errors
from core.tenancy import context
from core.text import line, name

STALE = "Otra persona cambió el contacto: vuelve a cargarlo y repite el cambio."
PHONE_HELP = "E.164: `+` y el código del país; no se adivina."
UNKNOWN = {"invalid_choice": "No es un valor admitido."}  # sin repetir lo recibido


class ContactSerializer(serializers.Serializer[Any]):
    """Los datos del propio contacto, también los personales: solo los ve quien lo tiene en su
    alcance. Nada enmascarado (el documento solo se enmascara en la auditoría)."""

    id = serializers.UUIDField()
    display_name = serializers.CharField()
    first_name = serializers.CharField()
    last_name = serializers.CharField()
    primary_phone_e164 = serializers.CharField(allow_null=True, help_text="En E.164.")
    primary_email = serializers.CharField(allow_null=True)
    document_type = serializers.ChoiceField(choices=Contact.DocumentType.choices, allow_null=True)
    document_number = serializers.CharField(allow_null=True)
    owner_user_id = serializers.UUIDField(allow_null=True, help_text="El responsable.")
    branch_id = serializers.UUIDField(allow_null=True)
    source = serializers.ChoiceField(choices=Contact.Source.choices)
    created_by_user_id = serializers.UUIDField(allow_null=True)
    created_at = serializers.DateTimeField()
    updated_at = serializers.DateTimeField()
    version = serializers.IntegerField(help_text="Sube con cada cambio.")


# Los mismos criterios que los comandos, para nombrar el campo en el 400
CLEANERS: dict[str, Callable[[Any], Any]] = {
    "display_name": lambda value: name(value, NAME_MAX),
    "first_name": lambda value: line(value, PART_MAX),
    "last_name": lambda value: line(value, PART_MAX),
    "primary_phone_e164": phone,
    "primary_email": email,
}


def _message(error: ValueError) -> list[str]:
    """El texto del comando, que nunca repite el dato recibido (04 §N.1)."""
    text = f"{error}."
    return [text[:1].upper() + text[1:]]


def _document_error(error: ValueError, sent: dict[str, Any]) -> serializers.ValidationError:
    """Tipo y número van juntos: el error, al tipo si llegó sin el número; si no, al número."""
    alone = "document_type" in sent and "document_number" not in sent
    return serializers.ValidationError(
        {"document_type" if alone else "document_number": _message(error)}
    )


class ContactWriteSerializer(serializers.Serializer[Any]):
    # Organización, responsable, sucursal o creador en el cuerpo: se ignoran (F4-08)
    display_name = serializers.CharField(max_length=NAME_MAX, required=False)
    first_name = serializers.CharField(max_length=PART_MAX, required=False, allow_blank=True)
    last_name = serializers.CharField(max_length=PART_MAX, required=False, allow_blank=True)
    primary_phone_e164 = serializers.CharField(
        max_length=32, required=False, allow_null=True, allow_blank=True, help_text=PHONE_HELP
    )
    primary_email = serializers.CharField(
        max_length=EMAIL_MAX, required=False, allow_null=True, allow_blank=True
    )
    document_type = serializers.ChoiceField(
        choices=Contact.DocumentType.choices,
        required=False,
        allow_null=True,
        error_messages=UNKNOWN,
    )
    document_number = serializers.CharField(
        max_length=32, required=False, allow_null=True, allow_blank=True, help_text="Con su tipo."
    )
    source = serializers.ChoiceField(
        choices=Contact.Source.choices, required=False, error_messages=UNKNOWN
    )

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        problems = {}
        for field, clean in CLEANERS.items():
            if field in attrs:
                try:
                    clean(attrs[field])
                except ValueError as error:
                    problems[field] = _message(error)
        if problems:
            raise serializers.ValidationError(problems)
        return attrs


class ContactCreateSerializer(ContactWriteSerializer):
    display_name = serializers.CharField(
        max_length=NAME_MAX,
        required=False,
        allow_blank=True,
        help_text="Si no se envía, se forma con el nombre y el apellido.",
    )

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        if not attrs.get("display_name"):
            attrs.pop("display_name", None)
        attrs = super().validate(attrs)
        if "display_name" not in attrs:  # el comando lo forma así, con las partes ya limpias
            parts = (attrs.get("first_name", ""), attrs.get("last_name", ""))
            try:
                name(" ".join(line(part, PART_MAX) for part in parts if part), NAME_MAX)
            except ValueError as error:
                raise serializers.ValidationError({"display_name": _message(error)}) from None
        try:
            document(attrs.get("document_type"), attrs.get("document_number"))
        except ValueError as error:
            raise _document_error(error, attrs) from None
        return attrs


class ContactUpdateSerializer(ContactWriteSerializer):
    version = serializers.IntegerField(
        min_value=1, help_text="La que se leyó. Si ya no es la vigente: 409."
    )


class ContactSearchSerializer(serializers.Serializer[Any]):
    q = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=EMAIL_MAX,  # cabe el correo más largo que se guarda
        help_text="Busca dentro del alcance. Con `+` (o `00`) y cifras, ignorando espacios, "
        "puntos, guiones y paréntesis: el teléfono empieza por ese prefijo E.164. Si no: el "
        "nombre contiene el texto o el correo empieza por él, sin distinguir mayúsculas. Unas "
        "cifras sin `+` no son un teléfono. Vacío: sin filtro.",
    )

    def validate_q(self, value: str) -> str:
        try:  # como se guarda un nombre: una línea en NFC, con un solo espacio entre palabras
            return line(value, EMAIL_MAX)
        except ValueError as error:
            raise serializers.ValidationError(_message(error)) from None


class ContactSearch(BaseFilterBackend):
    """`?q=` (F4-07), detrás de `ScopeFilter`: busca en lo que el alcance ya dejó."""

    def filter_queryset(self, request: Any, queryset: Any, view: Any) -> QuerySet[Contact]:
        params = request.query_params
        if len(params.getlist("q")) > 1:  # no se elige uno en silencio
            raise serializers.ValidationError({"q": ["Una sola vez."]})
        wanted = ContactSearchSerializer(data={"q": params["q"]} if "q" in params else {})
        wanted.is_valid(raise_exception=True)
        return search(queryset, wanted.validated_data.get("q", ""))


class ContactUpdateSchema(OpenApiSerializerExtension):
    """drf-spectacular trata todo `PATCH` como parcial; aquí la `version` sigue obligatoria."""

    target_class = ContactUpdateSerializer

    def map_serializer(self, auto_schema: Any, direction: Any) -> Any:
        schema = auto_schema._map_serializer(self.target, direction, bypass_extensions=True)
        return {**schema, "required": ["version"]}


def _tenant() -> Any:
    tenant = context.current()
    assert tenant is not None  # noqa: S101 — `HasPermission` ya lo comprobó
    return tenant


class ContactsQuery:
    serializer_class = ContactSerializer

    def get_queryset(self) -> QuerySet[Contact]:
        return contacts()


@extend_schema_view(
    get=extend_schema(
        operation_id="contacts_list",
        tags=["contacts"],
        parameters=[ContactSearchSerializer],
        responses={200: ContactSerializer(many=True), **errors(400, 401, 403, 404)},
        description="Los contactos vivos dentro del alcance de `contacts.view`, paginados por "
        "creación (ADR-016). `q` filtra sin cambiar el orden; la página siguiente se pide con "
        f"el mismo `q`. 400: `q` repetido, de más de {EMAIL_MAX} caracteres o que no es una "
        "línea de texto imprimible.",
    ),
    post=extend_schema(
        operation_id="contacts_create",
        tags=["contacts"],
        request=ContactCreateSerializer,
        responses={201: ContactSerializer, **errors(400, 401, 403, 404)},
        description="Crea un contacto: quien lo crea es su creador y responsable, en la sucursal "
        "de su membresía (o ninguna). Un 400 nombra el campo sin repetir el dato.",
    ),
)
class ContactsView(ContactsQuery, generics.ListAPIView):
    required_permissions = {"GET": "contacts.view", "POST": "contacts.create"}
    filter_backends = [ScopeFilter, ContactSearch]  # en este orden

    def post(self, request: Request, **kwargs: Any) -> Response:
        wanted = ContactCreateSerializer(data=request.data)
        wanted.is_valid(raise_exception=True)
        tenant, place = _tenant(), request_context(request)
        assert place is not None  # noqa: S101 — `HasPermission` ya lo comprobó
        contact = create_contact(
            tenant,
            **wanted.validated_data,
            owner_user_id=tenant.user_id,
            branch_id=next(iter(place.branch_ids), None),
        )
        return Response(ContactSerializer(contact).data, status=201)


@extend_schema_view(
    get=extend_schema(
        operation_id="contacts_retrieve",
        tags=["contacts"],
        responses={200: ContactSerializer, **errors(401, 403, 404)},
        description="Un contacto. 404: no es de la organización, está borrado o queda fuera "
        "del alcance de `contacts.view`.",
    ),
    patch=extend_schema(
        operation_id="contacts_update",
        tags=["contacts"],
        request=ContactUpdateSerializer,
        responses={200: ContactSerializer, **errors(400, 401, 403, 404, 409)},
        description="Cambia lo enviado si `version` sigue vigente (409 `CONTACT_VERSION_CONFLICT`"
        " si no). Exige también `contacts.view`; fuera del alcance de uno u otro: 404.",
    ),
    delete=extend_schema(
        operation_id="contacts_delete",
        tags=["contacts"],
        responses={204: None, **errors(401, 403, 404)},
        description="Borrado lógico. Exige también `contacts.view`; fuera del alcance de uno u "
        "otro, o ya borrado: 404.",
    ),
)
class ContactView(ContactsQuery, generics.RetrieveAPIView):
    required_permissions = {
        "GET": "contacts.view",
        "PATCH": ("contacts.view", "contacts.update"),
        "DELETE": ("contacts.view", "contacts.delete"),
    }
    lookup_url_kwarg = "contact_id"

    def get_queryset(self) -> QuerySet[Contact]:
        found = super().get_queryset()
        if self.request.method not in ("PATCH", "DELETE"):
            return found
        # El alcance se decide sobre la fila bloqueada hasta el final de la petición: un cambio
        # de responsable o de sucursal (F4-08) no se cuela entre la comprobación y la escritura.
        return found.select_for_update(no_key=True)

    def patch(self, request: Request, contact_id: UUID, **kwargs: Any) -> Response:
        wanted = ContactUpdateSerializer(data=request.data)
        wanted.is_valid(raise_exception=True)
        fields = dict(wanted.validated_data)
        version = fields.pop("version")
        found = self.get_object()
        try:
            contact = update_contact(_tenant(), contact_id=found.pk, version=version, **fields)
        except StaleContact:
            raise ApiError("CONTACT_VERSION_CONFLICT", 409, STALE) from None
        except ValueError as error:  # el documento, junto con el tipo o el número guardados
            raise _document_error(error, fields) from None
        return Response(ContactSerializer(contact).data)

    def delete(self, request: Request, contact_id: UUID, **kwargs: Any) -> Response:
        delete_contact(_tenant(), contact_id=self.get_object().pk)
        return Response(status=204)


class ContactAssignmentSerializer(serializers.Serializer[Any]):
    owner_user_id = serializers.UUIDField(
        allow_null=True, help_text="El responsable: un miembro activo de la organización, o null."
    )
    branch_id = serializers.UUIDField(
        allow_null=True, help_text="Una sucursal de la organización, o null."
    )
    version = serializers.IntegerField(
        min_value=1, help_text="La que se leyó. Si ya no es la vigente: 409."
    )


BAD_REFERENCE = {  # sin repetir el id recibido
    "owner_user_id": "No es un miembro activo de la organización.",
    "branch_id": "No es una sucursal de la organización.",
}


@extend_schema_view(
    put=extend_schema(
        operation_id="contacts_assign",
        tags=["contacts"],
        request=ContactAssignmentSerializer,
        responses={200: ContactSerializer, **errors(400, 401, 403, 404, 409)},
        description="Deja el contacto con ese responsable y esa sucursal (`contacts.assign`) si "
        "`version` sigue vigente (409 `CONTACT_VERSION_CONFLICT` si no). Exige también "
        "`contacts.view`; fuera de su alcance: 404. Solo se valida lo que cambia.",
    ),
)
class ContactAssignmentView(ContactsQuery, generics.GenericAPIView):
    """F4-08 (E05-01). `contacts.assign` no admite alcance (03 §H): qué contactos alcanza lo
    decide `contacts.view`, sobre la fila bloqueada hasta el final de la petición."""

    required_permissions = {"PUT": ("contacts.view", "contacts.assign")}
    lookup_url_kwarg = "contact_id"

    def get_queryset(self) -> QuerySet[Contact]:
        return super().get_queryset().select_for_update(no_key=True)

    def put(self, request: Request, contact_id: UUID, **kwargs: Any) -> Response:
        wanted = ContactAssignmentSerializer(data=request.data)
        wanted.is_valid(raise_exception=True)
        found = self.get_object()
        try:
            contact = assign_contact(_tenant(), contact_id=found.pk, **wanted.validated_data)
        except StaleContact:
            raise ApiError("CONTACT_VERSION_CONFLICT", 409, STALE) from None
        except BadReference as bad:
            raise serializers.ValidationError({bad.field: [BAD_REFERENCE[bad.field]]}) from None
        return Response(ContactSerializer(contact).data)

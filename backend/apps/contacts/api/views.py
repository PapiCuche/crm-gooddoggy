"""Rutas de tenant de los contactos: `…/contacts/` (F4-02, E05-01). Solo lectura por ahora.

`contacts.view` admite alcance: `ScopeFilter` deja en el listado y en `get_object()` solo los
contactos que la política de `Contact` permite (ADR-003 §5). Fuera del alcance, el contacto no
existe: 404, igual que el de otra organización o uno borrado.
"""

from typing import Any

from django.db.models import QuerySet
from drf_spectacular.utils import extend_schema, extend_schema_view
from rest_framework import generics, serializers

from apps.contacts.models import Contact
from apps.contacts.selectors import contacts
from core.api.schema import errors


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


class ContactsQuery:
    serializer_class = ContactSerializer

    def get_queryset(self) -> QuerySet[Contact]:
        return contacts()


@extend_schema_view(
    get=extend_schema(
        operation_id="contacts_list",
        tags=["contacts"],
        responses={200: ContactSerializer(many=True), **errors(400, 401, 403, 404)},
        description="Los contactos vivos dentro del alcance de `contacts.view`, paginados por "
        "creación (ADR-016).",
    ),
)
class ContactsView(ContactsQuery, generics.ListAPIView):
    required_permissions = {"GET": "contacts.view"}


@extend_schema_view(
    get=extend_schema(
        operation_id="contacts_retrieve",
        tags=["contacts"],
        responses={200: ContactSerializer, **errors(401, 403, 404)},
        description="Un contacto. 404: no es de la organización, está borrado o queda fuera "
        "del alcance de `contacts.view`.",
    ),
)
class ContactView(ContactsQuery, generics.RetrieveAPIView):
    required_permissions = {"GET": "contacts.view"}
    lookup_url_kwarg = "contact_id"

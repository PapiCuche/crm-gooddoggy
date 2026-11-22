"""Gestionar las categorías de las respuestas rápidas (F6-08a4, E06-10): `…/quick-reply-categories/`
y `…/{id}/`.

Todo pide `quick_replies.manage` (sin alcance), comprobado antes de leer el cuerpo o la base.
Escribir pasa por los comandos de `apps.conversations.quick_replies`, que validan, bloquean y
auditan; aquí se exige el tipo de cada campo y se traduce lo que lanzan. La organización sale del
contexto. Lo que no existe, es de otra organización o está borrado: el mismo 404. Renombrar,
reordenar (`sort_order`), activar y desactivar son un `PATCH` con `version`; desactivar una
categoría con respuestas activas es un 409 (se mueven o se desactivan antes, con F6-08a3).
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
from apps.conversations.models import QuickReplyCategory
from core.api.errors import ApiError
from core.api.schema import errors
from core.api.strict import Closed, Flag, Text, Whole, sentence, version_required
from core.tenancy import context

STALE = "Otra persona la cambió: vuelve a cargarla y repite el cambio."
TAKEN = "Ya hay una categoría con ese nombre."
IN_USE = "La categoría tiene respuestas activas: muévelas o desactívalas antes."
GENERIC = "Dato no admitido."
ORDER = (-qr.ORDER_MAX - 1, qr.ORDER_MAX)
NAME_HELP = f"Hasta {qr.NAME_MAX} caracteres; único sin distinguir mayúsculas."


class ManagedQuickReplyCategorySerializer(serializers.Serializer[Any]):
    id = serializers.UUIDField()
    name = serializers.CharField()
    sort_order = serializers.IntegerField(
        help_text="El orden en que se muestran, de menor a mayor."
    )
    is_active = serializers.BooleanField()
    version = serializers.IntegerField()
    created_at = serializers.DateTimeField()
    updated_at = serializers.DateTimeField()


class ManagedQuickReplyCategoryCreateSerializer(Closed, serializers.Serializer[Any]):
    name = Text(max_length=qr.NAME_MAX, help_text=NAME_HELP)
    sort_order = Whole(
        min_value=ORDER[0], max_value=ORDER[1], required=False, help_text="Sin él, al final."
    )


@version_required
class ManagedQuickReplyCategoryUpdateSerializer(Closed, serializers.Serializer[Any]):
    name = Text(max_length=qr.NAME_MAX, required=False, help_text=NAME_HELP)
    sort_order = Whole(min_value=ORDER[0], max_value=ORDER[1], required=False)
    is_active = Flag(
        required=False,
        help_text="`false` la desactiva si no tiene respuestas activas (si no, 409).",
    )
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
        raise ApiError("CATEGORY_NAME_TAKEN", 409, TAKEN) from None
    except qr.CategoryInUse:
        raise ApiError("CATEGORY_IN_USE", 409, IN_USE) from None
    except qr.Invalid as error:
        raise serializers.ValidationError({error.field: [sentence(error)]}) from None
    except ValueError:  # un comando rechazó algo que el serializador dejó pasar
        raise serializers.ValidationError(GENERIC) from None


@extend_schema_view(
    get=extend_schema(
        operation_id="quick_reply_categories_list",
        tags=["conversations"],
        responses={
            200: ManagedQuickReplyCategorySerializer(many=True),
            **errors(400, 401, 403, 404),
        },
        description="Las categorías no borradas, activas o no, por creación (ADR-016); la "
        "pantalla las ordena por `sort_order`. Con `quick_replies.manage`.",
    ),
    post=extend_schema(
        operation_id="quick_reply_categories_create",
        tags=["conversations"],
        request=ManagedQuickReplyCategoryCreateSerializer,
        responses={201: ManagedQuickReplyCategorySerializer, **errors(400, 401, 403, 404, 409)},
        description="Crea una categoría activa, auditada (`quick_replies.manage`). Sin "
        "`sort_order`, va al final. 409 `CATEGORY_NAME_TAKEN`: ya hay una con ese nombre, sin "
        "distinguir mayúsculas.",
    ),
)
class ManagedCategoriesView(generics.ListAPIView):
    required_permissions = {"GET": "quick_replies.manage", "POST": "quick_replies.manage"}
    serializer_class = ManagedQuickReplyCategorySerializer

    def get_queryset(self) -> QuerySet[QuickReplyCategory]:
        return selectors.managed_quick_reply_categories()

    def post(self, request: Request, **kwargs: Any) -> Response:
        wanted = ManagedQuickReplyCategoryCreateSerializer(data=request.data)
        wanted.is_valid(raise_exception=True)
        with _domain():
            made = qr.create_category(context.require(), **wanted.validated_data)
        return Response(ManagedQuickReplyCategorySerializer(made).data, status=201)


class ManagedCategoryView(APIView):
    required_permissions = {"PATCH": "quick_replies.manage"}

    @extend_schema(
        operation_id="quick_reply_categories_update",
        tags=["conversations"],
        request=ManagedQuickReplyCategoryUpdateSerializer,
        responses={200: ManagedQuickReplyCategorySerializer, **errors(400, 401, 403, 404, 409)},
        description="Renombra, reordena (`sort_order`), activa o desactiva, si `version` sigue "
        "vigente (409 `STALE_VERSION` si no); lo que no cambia no se escribe ni se audita. 409 "
        "`CATEGORY_NAME_TAKEN`; 409 `CATEGORY_IN_USE`: desactivarla con respuestas activas (se "
        "mueven o se desactivan antes). Con `quick_replies.manage`.",
    )
    def patch(self, request: Request, category_id: UUID, **kwargs: Any) -> Response:
        wanted = ManagedQuickReplyCategoryUpdateSerializer(data=request.data)
        wanted.is_valid(raise_exception=True)
        fields = dict(wanted.validated_data)
        version = fields.pop("version")
        with _domain():
            done = qr.update_category(
                context.require(), category_id=category_id, version=version, **fields
            )
        return Response(ManagedQuickReplyCategorySerializer(done).data)

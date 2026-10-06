"""Rutas de `organizations`: las de plataforma (`/api/v1/me/…`, sin tenant; ADR-014 §4) y las
de tenant (`…/branches/`, F2-43 a F2-45, y `…/teams/`, F2-50 y F2-53 a F2-55)."""

from typing import Any
from uuid import UUID

from django.conf import settings
from django.db.models import QuerySet
from drf_spectacular.utils import extend_schema, extend_schema_view
from rest_framework import generics, serializers
from rest_framework.exceptions import NotFound
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.organizations import teams as team_commands
from apps.organizations.branches import (
    CLEAN,
    CODE_MAX,
    NAME_MAX,
    TEXT_MAX,
    TIMEZONE_MAX,
    BranchCodeTaken,
    create_branch,
    update_branch,
)
from apps.organizations.models import Branch, Team, TeamMember
from apps.organizations.selectors import branches, organizations_for_user, team_members, teams
from core.api.errors import ApiError
from core.api.permissions import Authenticated
from core.api.schema import errors
from core.tenancy import context


class OrganizationSummarySerializer(serializers.Serializer[Any]):
    id = serializers.UUIDField()
    slug = serializers.CharField()
    name = serializers.CharField()


class MyOrganizationsView(APIView):
    permission_classes = [Authenticated]

    @extend_schema(
        operation_id="me_organizations",
        tags=["me"],
        responses={200: OrganizationSummarySerializer(many=True), **errors(401)},
    )
    def get(self, request: Request) -> Response:
        """Organizaciones del usuario: membresía activa y organización no suspendida. No abre
        un `tenant_scope`: lee con `user_scope`, que solo deja ver las membresías propias."""
        found = organizations_for_user(request.user)
        return Response(OrganizationSummarySerializer(found, many=True).data)


class BranchSerializer(serializers.Serializer[Any]):
    id = serializers.UUIDField()
    code = serializers.CharField(help_text="Clave corta de la sucursal, única en la organización.")
    name = serializers.CharField()
    address = serializers.CharField(allow_blank=True)
    district = serializers.CharField(allow_blank=True)
    city = serializers.CharField(allow_blank=True)
    phone = serializers.CharField(allow_blank=True)
    timezone = serializers.CharField(help_text="Zona horaria, por su nombre IANA.")
    is_active = serializers.BooleanField()


class BranchCreateSerializer(serializers.Serializer[Any]):
    code = serializers.CharField(
        max_length=CODE_MAX, help_text="Se guarda en mayúsculas y no cambia después."
    )
    name = serializers.CharField(max_length=NAME_MAX)
    address = serializers.CharField(
        max_length=TEXT_MAX["address"], required=False, allow_blank=True
    )
    district = serializers.CharField(
        max_length=TEXT_MAX["district"], required=False, allow_blank=True
    )
    city = serializers.CharField(max_length=TEXT_MAX["city"], required=False, allow_blank=True)
    phone = serializers.CharField(max_length=TEXT_MAX["phone"], required=False, allow_blank=True)
    timezone = serializers.CharField(
        max_length=TIMEZONE_MAX, required=False, help_text="Zona horaria, por su nombre IANA."
    )

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        """El mismo criterio que el comando, como un 400 por campo."""
        problems = {}
        for field, value in attrs.items():
            try:
                if field in CLEAN:  # `is_active` ya llega como booleano
                    attrs[field] = CLEAN[field](value)
            except ValueError as error:
                text = f"{error}."
                problems[field] = [text[:1].upper() + text[1:]]
        if problems:
            raise serializers.ValidationError(problems)
        return attrs


class JsonBooleanField(serializers.BooleanField):
    """Solo `true` o `false`, como el comando. DRF aceptaría además `"no"`, `"off"`, `0` o `"1"`."""

    def to_internal_value(self, data: Any) -> bool:
        if not isinstance(data, bool):
            self.fail("invalid", input=data)
        return bool(data)


class BranchUpdateSerializer(BranchCreateSerializer):
    """Lo que se envía cambia; lo que no, se queda como está. El código no se cambia."""

    code = None
    name = serializers.CharField(max_length=NAME_MAX, required=False)
    is_active = JsonBooleanField(required=False)


TAKEN = "Ya existe una sucursal con ese código en la organización."


@extend_schema_view(
    get=extend_schema(
        operation_id="branches_list",
        tags=["branches"],
        responses={200: BranchSerializer(many=True), **errors(400, 401, 403, 404)},
    ),
    post=extend_schema(
        operation_id="branches_create",
        tags=["branches"],
        request=BranchCreateSerializer,
        responses={201: BranchSerializer, **errors(400, 401, 403, 404, 409)},
        description="Crea una sucursal activa. `timezone` es `America/Lima` si no se envía. "
        "409 `BRANCH_CODE_TAKEN`: ya hay en la organización una sucursal con ese código.",
    ),
)
class BranchesView(generics.ListAPIView):
    """Las sucursales de la organización, activas e inactivas. Paginado por orden de creación
    (ADR-016). `POST` crea una (F2-44)."""

    required_permissions = {"GET": "organization.view", "POST": "branches.manage"}
    serializer_class = BranchSerializer

    def get_queryset(self) -> QuerySet[Branch]:
        return branches()

    def post(self, request: Request, **kwargs: Any) -> Response:
        wanted = BranchCreateSerializer(data=request.data)
        wanted.is_valid(raise_exception=True)
        tenant = context.current()
        assert tenant is not None  # noqa: S101 — `HasPermission` ya lo comprobó
        try:
            branch = create_branch(tenant, **wanted.validated_data)
        except BranchCodeTaken:
            raise ApiError("BRANCH_CODE_TAKEN", 409, TAKEN) from None
        return Response(BranchSerializer(branch).data, status=201)


class BranchView(APIView):
    """Una sucursal: `PATCH` cambia sus datos, la desactiva o la reactiva (`is_active`). No hay
    borrado."""

    required_permissions = {"PATCH": "branches.manage"}

    @extend_schema(
        operation_id="branches_update",
        tags=["branches"],
        request=BranchUpdateSerializer,
        responses={200: BranchSerializer, **errors(400, 401, 403, 404)},
        description="Cambia lo que se envía; lo demás se queda como está, y el código no "
        "cambia. Enviar lo que ya hay no escribe nada. 404: la sucursal no es de la "
        "organización.",
    )
    def patch(self, request: Request, branch_id: UUID, **kwargs: Any) -> Response:
        wanted = BranchUpdateSerializer(data=request.data)
        wanted.is_valid(raise_exception=True)
        tenant = context.current()
        assert tenant is not None  # noqa: S101 — `HasPermission` ya lo comprobó
        try:
            branch = update_branch(tenant, branch_id=branch_id, **wanted.validated_data)
        except Branch.DoesNotExist:
            raise NotFound from None
        return Response(BranchSerializer(branch).data)


class TeamSerializer(serializers.Serializer[Any]):
    id = serializers.UUIDField()
    slug = serializers.CharField(help_text="Clave estable del equipo, única en la organización.")
    name = serializers.CharField()
    description = serializers.CharField(allow_blank=True)
    assignment_strategy = serializers.ChoiceField(
        choices=Team.Strategy.choices,
        help_text="Cómo se repartirán sus conversaciones. Todavía no la aplica nada.",
    )
    is_active = serializers.BooleanField()


class TeamCreateSerializer(serializers.Serializer[Any]):
    slug = serializers.CharField(
        max_length=team_commands.SLUG_MAX,
        help_text="Se guarda en minúsculas y no cambia después.",
    )
    name = serializers.CharField(max_length=team_commands.NAME_MAX)
    description = serializers.CharField(
        max_length=team_commands.DESCRIPTION_MAX, required=False, allow_blank=True
    )
    assignment_strategy = serializers.ChoiceField(choices=Team.Strategy.choices, required=False)

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        """El mismo criterio que el comando, como un 400 por campo."""
        problems = {}
        for field, value in attrs.items():
            try:
                if field in team_commands.CLEAN:  # `is_active` ya llega como booleano
                    attrs[field] = team_commands.CLEAN[field](value)
            except ValueError as error:
                text = f"{error}."
                problems[field] = [text[:1].upper() + text[1:]]
        if problems:
            raise serializers.ValidationError(problems)
        return attrs


class TeamUpdateSerializer(TeamCreateSerializer):
    """Lo que se envía cambia; lo que no, se queda como está. El `slug` no se cambia."""

    slug = None
    name = serializers.CharField(max_length=team_commands.NAME_MAX, required=False)
    is_active = JsonBooleanField(required=False)


SLUG_TAKEN = "Ya existe un equipo con ese slug en la organización."


@extend_schema_view(
    get=extend_schema(
        operation_id="teams_list",
        tags=["teams"],
        responses={200: TeamSerializer(many=True), **errors(400, 401, 403, 404)},
    ),
    post=extend_schema(
        operation_id="teams_create",
        tags=["teams"],
        request=TeamCreateSerializer,
        responses={201: TeamSerializer, **errors(400, 401, 403, 404, 409)},
        description="Crea un equipo activo, sin integrantes. `assignment_strategy` es `MANUAL` "
        "si no se envía. 409 `TEAM_SLUG_TAKEN`: ya hay en la organización un equipo con ese "
        "`slug`.",
    ),
)
class TeamsView(generics.ListAPIView):
    """Los equipos de la organización, activos e inactivos. Paginado por orden de creación
    (ADR-016). `POST` crea uno (F2-53)."""

    required_permissions = {"GET": "teams.view", "POST": "teams.manage"}
    serializer_class = TeamSerializer

    def get_queryset(self) -> QuerySet[Team]:
        return teams()

    def post(self, request: Request, **kwargs: Any) -> Response:
        wanted = TeamCreateSerializer(data=request.data)
        wanted.is_valid(raise_exception=True)
        tenant = context.current()
        assert tenant is not None  # noqa: S101 — `HasPermission` ya lo comprobó
        try:
            team = team_commands.create_team(tenant, **wanted.validated_data)
        except team_commands.TeamSlugTaken:
            raise ApiError("TEAM_SLUG_TAKEN", 409, SLUG_TAKEN) from None
        return Response(TeamSerializer(team).data, status=201)


class TeamView(APIView):
    """Un equipo: `PATCH` cambia sus datos, lo desactiva o lo reactiva (`is_active`). No hay
    borrado."""

    required_permissions = {"PATCH": "teams.manage"}

    @extend_schema(
        operation_id="teams_update",
        tags=["teams"],
        request=TeamUpdateSerializer,
        responses={200: TeamSerializer, **errors(400, 401, 403, 404)},
        description="Cambia lo que se envía; lo demás se queda como está, y el `slug` no "
        "cambia. Enviar lo que ya hay no escribe nada. 404: el equipo no es de la organización.",
    )
    def patch(self, request: Request, team_id: UUID, **kwargs: Any) -> Response:
        wanted = TeamUpdateSerializer(data=request.data)
        wanted.is_valid(raise_exception=True)
        tenant = context.current()
        assert tenant is not None  # noqa: S101 — `HasPermission` ya lo comprobó
        try:
            team = team_commands.update_team(tenant, team_id=team_id, **wanted.validated_data)
        except Team.DoesNotExist:
            raise NotFound from None
        return Response(TeamSerializer(team).data)


class TeamMemberUserSerializer(serializers.Serializer[Any]):
    id = serializers.UUIDField()
    email = serializers.CharField()
    first_name = serializers.CharField()
    last_name = serializers.CharField()


class TeamMemberSerializer(serializers.Serializer[Any]):
    id = serializers.UUIDField(
        source="membership_id",
        help_text="Identificador de la membresía: el mismo del directorio de miembros.",
    )
    status = serializers.ChoiceField(
        source="membership.status",
        choices=settings.MEMBERSHIP_STATUSES,
        help_text="Estado de la membresía en la organización, no en el equipo.",
    )
    team_role = serializers.ChoiceField(choices=TeamMember.Role.choices)
    is_active = serializers.BooleanField(
        help_text="Participa en la asignación automática del equipo. Todavía no la aplica nada."
    )
    user = TeamMemberUserSerializer(source="membership.user")


@extend_schema_view(
    get=extend_schema(
        operation_id="team_members_list",
        tags=["teams"],
        responses={200: TeamMemberSerializer(many=True), **errors(400, 401, 403, 404)},
    )
)
class TeamMembersView(generics.ListAPIView):
    """Quién pertenece a un equipo, con su papel en él. Paginado por orden de incorporación
    (ADR-016). Enseña un equipo y personas: exige `teams.view` y `users.view` (F2-56). 404: el
    equipo no es de la organización."""

    required_permissions = {"GET": ("teams.view", "users.view")}
    serializer_class = TeamMemberSerializer

    def get_queryset(self) -> QuerySet[TeamMember]:
        team_id: UUID = self.kwargs["team_id"]
        if not teams().filter(pk=team_id).exists():
            raise NotFound
        return team_members(team_id)

"""Rutas de tenant de `members`: el estado de un miembro, suspendido o activo (F2-19), su
sucursal (F2-69), sus equipos (F2-58, F2-59, D-F2-13) y las invitaciones a la organización:
invitar (F2-80) o renovar, listarlas (F2-81), revocar una (F2-82) y reenviarla (F2-130e). Y dos
rutas de plataforma: la vista previa del enlace de una invitación (F2-130a) y aceptarla (F2-130b),
con la excepción de ADR-020 §4 a ADR-014 §4 (D-F2-14)."""

from typing import Any
from uuid import UUID

from django.core.exceptions import ValidationError
from django.db.models import QuerySet
from drf_spectacular.utils import extend_schema, extend_schema_field, extend_schema_view
from rest_framework import generics, serializers
from rest_framework.exceptions import PermissionDenied
from rest_framework.fields import get_error_detail
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.access.permissions import rbac_errors
from apps.access.services import UnknownRole
from apps.members import invitation_links
from apps.members.services import (
    ACTIVE,
    SUSPENDED,
    InvalidEmail,
    RoleGone,
    invite,
    put_team_member,
    remove_team_member,
    resend_invitation,
    revoke_invitation,
    set_member_branch,
    set_member_status,
)
from apps.organizations.api.views import TeamMemberPutSerializer, TeamMemberSerializer
from apps.organizations.selectors import invitations
from apps.organizations.services import (
    DELIVERIES,
    INVITATION_ROLES_MAX,
    INVITATION_STATUSES,
    AddressLimit,
    AlreadyMember,
    InvalidTransition,
    InvitationPending,
    SendLimit,
    SendTooSoon,
    TooManyInvitations,
    TooManyPending,
    UnknownBranch,
    shown_delivery,
    shown_status,
)
from apps.organizations.teams import OwnTeamMembership
from core.api.errors import ApiError
from core.api.permissions import Public
from core.api.schema import errors
from core.tenancy import context


class MemberStatusChangeSerializer(serializers.Serializer[Any]):
    status = serializers.ChoiceField(choices=[ACTIVE, SUSPENDED])


class MemberStatusSerializer(serializers.Serializer[Any]):
    id = serializers.UUIDField(help_text="Identificador de la membresía.")
    status = serializers.ChoiceField(choices=[ACTIVE, SUSPENDED])


class MemberStatusView(APIView):
    """Suspende (`SUSPENDED`) o reactiva (`ACTIVE`) a un miembro. Repetir la petición no cambia
    nada. 403: sin `users.manage`, uno mismo, o un miembro con un rol que el actor no podría
    asignar. 409 `LAST_OWNER`: sería el último Owner activo. 409 `INVALID_TRANSITION`: la
    membresía está invitada o dada de baja."""

    required_permissions = {"PUT": "users.manage"}

    @extend_schema(
        operation_id="members_set_status",
        tags=["members"],
        request=MemberStatusChangeSerializer,
        responses={200: MemberStatusSerializer, **errors(400, 401, 403, 404, 409)},
    )
    def put(self, request: Request, membership_id: UUID, **kwargs: Any) -> Response:
        wanted = MemberStatusChangeSerializer(data=request.data)
        wanted.is_valid(raise_exception=True)
        status = wanted.validated_data["status"]
        tenant = context.current()
        assert tenant is not None  # noqa: S101 — `HasPermission` ya lo comprobó
        try:
            with rbac_errors():
                set_member_status(tenant, membership_id=membership_id, status=status)
        except InvalidTransition:
            message = "Solo se suspende a un miembro activo y se reactiva a uno suspendido."
            raise ApiError("INVALID_TRANSITION", 409, message) from None
        return Response(MemberStatusSerializer({"id": membership_id, "status": status}).data)


class MemberBranchChangeSerializer(serializers.Serializer[Any]):
    branch_id = serializers.UUIDField(
        allow_null=True,
        help_text="La sucursal de la organización, o `null` para dejarlo sin ninguna.",
    )


# La misma forma que `default_branch` en el directorio de miembros (`apps.access`, F2-70).
class MemberBranchRefSerializer(serializers.Serializer[Any]):
    id = serializers.UUIDField()
    code = serializers.CharField()
    name = serializers.CharField()


class MemberBranchSerializer(serializers.Serializer[Any]):
    id = serializers.UUIDField(help_text="Identificador de la membresía.")
    default_branch = MemberBranchRefSerializer(allow_null=True)


REVOKED = "REVOKED"
UNKNOWN_BRANCH = "No es una sucursal de esta organización."


class MemberBranchView(APIView):
    """Asigna a un miembro su sucursal, la cambia o se la quita (`null`). Su sucursal es lo que
    alcanza una concesión con alcance `BRANCH`: las reglas son las de suspenderlo. Repetir la
    petición no cambia nada. 400: `branch_id` no es una sucursal de la organización. 403: sin
    `users.manage`, uno mismo, o un miembro con un rol que el actor no podría asignar. 409
    `LAST_OWNER`: la organización no tiene rol Owner y no admite ningún cambio."""

    required_permissions = {"PUT": "users.manage"}

    @extend_schema(
        operation_id="members_set_branch",
        tags=["members"],
        request=MemberBranchChangeSerializer,
        responses={200: MemberBranchSerializer, **errors(400, 401, 403, 404, 409)},
    )
    def put(self, request: Request, membership_id: UUID, **kwargs: Any) -> Response:
        wanted = MemberBranchChangeSerializer(data=request.data)
        wanted.is_valid(raise_exception=True)
        tenant = context.current()
        assert tenant is not None  # noqa: S101 — `HasPermission` ya lo comprobó
        try:
            with rbac_errors():
                branch = set_member_branch(
                    tenant,
                    membership_id=membership_id,
                    branch_id=wanted.validated_data["branch_id"],
                )
        except UnknownBranch:
            raise serializers.ValidationError({"branch_id": [UNKNOWN_BRANCH]}) from None
        row = {"id": membership_id, "default_branch": branch._asdict() if branch else None}
        return Response(MemberBranchSerializer(row).data)


class TeamMemberView(APIView):
    """Un integrante de un equipo: `PUT` deja a esa membresía en el equipo (F2-58) y `DELETE` la
    quita (F2-59). Cambian a quién alcanza una concesión con alcance `TEAM` y tratan con una
    persona: exigen `teams.manage` y `users.view`, y que el actor cubra sobre ese equipo cada
    concesión con alcance `TEAM` del miembro (D-F2-13)."""

    required_permissions = {
        "PUT": ("teams.manage", "users.view"),
        "DELETE": ("teams.manage", "users.view"),
    }

    @extend_schema(
        operation_id="team_members_put",
        tags=["teams"],
        request=TeamMemberPutSerializer,
        responses={
            200: TeamMemberSerializer,
            201: TeamMemberSerializer,
            **errors(400, 401, 403, 404, 409),
        },
        description="Incorpora a la membresía al equipo (201; `MEMBER` y activa si no se "
        "envían) o, si ya estaba, cambia lo que se envía (200). Repetir la petición no escribe "
        "nada. 403: sin los dos permisos, la membresía es la de quien hace la petición, o tiene "
        "un permiso con alcance de equipo que el actor no cubre en ese equipo. 404: el equipo o "
        "la membresía no son de la organización. 409 `LAST_OWNER`: la organización no tiene rol "
        "Owner y no admite ningún cambio.",
    )
    def put(self, request: Request, team_id: UUID, membership_id: UUID, **kwargs: Any) -> Response:
        wanted = TeamMemberPutSerializer(data=request.data)
        wanted.is_valid(raise_exception=True)
        tenant = context.current()
        assert tenant is not None  # noqa: S101 — `HasPermission` ya lo comprobó
        try:
            with rbac_errors():
                member, new = put_team_member(
                    tenant, team_id=team_id, membership_id=membership_id, **wanted.validated_data
                )
        except OwnTeamMembership:
            raise PermissionDenied from None
        return Response(TeamMemberSerializer(member).data, status=201 if new else 200)

    @extend_schema(
        operation_id="team_members_remove",
        tags=["teams"],
        responses={204: None, **errors(401, 403, 404, 409)},
        description="Quita a la membresía del equipo. 403: sin los dos permisos, la membresía "
        "es la de quien hace la petición, o tiene un permiso con alcance de equipo que el actor "
        "no cubre en ese equipo. 404: el equipo o la membresía no son de la organización, o la "
        "membresía no está en el equipo (también al repetir la petición). 409 `LAST_OWNER`: la "
        "organización no tiene rol Owner y no admite ningún cambio.",
    )
    def delete(
        self, request: Request, team_id: UUID, membership_id: UUID, **kwargs: Any
    ) -> Response:
        tenant = context.current()
        assert tenant is not None  # noqa: S101 — `HasPermission` ya lo comprobó
        try:
            with rbac_errors():
                remove_team_member(tenant, team_id=team_id, membership_id=membership_id)
        except OwnTeamMembership:
            raise PermissionDenied from None
        return Response(status=204)


class InviteSerializer(serializers.Serializer[Any]):
    email = serializers.CharField(max_length=254, help_text="El correo de la persona invitada.")
    role_ids = serializers.ListField(
        child=serializers.UUIDField(),
        min_length=1,
        max_length=INVITATION_ROLES_MAX,
        help_text="Los roles de la organización que tendrá al aceptar, sin repetir.",
    )

    def validate_role_ids(self, value: list[UUID]) -> list[UUID]:
        if len(set(value)) != len(value):
            raise serializers.ValidationError("Un rol está repetido.")
        return value


class InvitationSerializer(serializers.Serializer[Any]):
    id = serializers.UUIDField()
    email = serializers.CharField(help_text="El correo invitado, en la forma en que se guarda.")
    role_ids = serializers.ListField(child=serializers.UUIDField())
    status = serializers.SerializerMethodField(
        help_text="El que se enseña: una pendiente cuya fecha pasó sale como `EXPIRED`."
    )
    expires_at = serializers.DateTimeField()
    invited_by = serializers.UUIDField(source="invited_by_id", help_text="La cuenta que invitó.")
    created_at = serializers.DateTimeField()
    sent_at = serializers.DateTimeField(allow_null=True, help_text="El último envío.")
    send_count = serializers.IntegerField(help_text="Cuántas veces se envió (de 0 a 5).")
    delivery = serializers.SerializerMethodField(
        help_text="`NOT_DELIVERED`: el último enlace se emitió y contó sin entrega confirmada."
    )

    @extend_schema_field(serializers.ChoiceField(choices=INVITATION_STATUSES))
    def get_status(self, invitation: Any) -> str:
        return shown_status(invitation.status, invitation.expires_at)

    @extend_schema_field(serializers.ChoiceField(choices=DELIVERIES))
    def get_delivery(self, invitation: Any) -> str:
        sent, delivered = invitation.sent_at, invitation.delivered_at
        return shown_delivery(invitation.status, invitation.linked, sent, delivered)


# Lo que `create_invitation` rechaza → código, estado y texto de la respuesta.
_REFUSED = {
    AlreadyMember: ("ALREADY_MEMBER", 409, "Ese correo ya es miembro de la organización."),
    InvitationPending: ("INVITATION_PENDING", 409, "Ese correo ya tiene una invitación pendiente."),
    TooManyPending: ("INVITATION_LIMIT", 409, "Hay demasiadas invitaciones pendientes."),
    TooManyInvitations: ("RATE_LIMITED", 429, "Demasiadas invitaciones en las últimas 24 horas."),
    SendLimit: ("INVITATION_SEND_LIMIT", 409, "Ya se envió 5 veces: revócala e invita de nuevo."),
}
_SOON = "Ese correo recibió una invitación hace menos de 15 minutos."
_WAIT = {  # 429: el manejador de errores pasa `wait` a `Retry-After`
    AddressLimit: ("INVITATION_ADDRESS_LIMIT", "Ese correo ya recibió 3 invitaciones en 24 horas."),
    SendTooSoon: ("INVITATION_SEND_TOO_SOON", _SOON),
}


def _waiting(refused: SendTooSoon) -> ApiError:
    error = ApiError(_WAIT[type(refused)][0], 429, _WAIT[type(refused)][1])
    error.wait = refused.wait
    return error


@extend_schema_view(
    get=extend_schema(
        operation_id="invitations_list",
        tags=["members"],
        responses={200: InvitationSerializer(many=True), **errors(400, 401, 403, 404)},
    ),
    post=extend_schema(
        operation_id="invitations_create",
        tags=["members"],
        request=InviteSerializer,
        responses={
            201: InvitationSerializer,
            200: InvitationSerializer,
            **errors(400, 401, 403, 404, 409, 429),
        },
        description="Deja creada la invitación de una persona (201), pendiente y con los roles "
        "que tendrá al aceptar, y pide su correo, que sale después con el enlace. Si el correo "
        "tiene una pendiente, la renueva (200, F2-130e). 400: correo al que no se puede "
        "escribir, o `role_ids` vacío, con un rol repetido o que no es de la organización. 403: "
        "sin `users.invite` y `users.manage`, o con un rol que el actor no podría asignar (al "
        "renovar, también los que tenía). 409: `ALREADY_MEMBER`, `INVITATION_SEND_LIMIT`, "
        "`INVITATION_LIMIT` (50 pendientes) o `LAST_OWNER` (la organización no tiene rol Owner y "
        "no admite ningún cambio). 429: 100 invitaciones en 24 h; con `Retry-After`, "
        "`INVITATION_ADDRESS_LIMIT` o `INVITATION_SEND_TOO_SOON` (ver el reenvío).",
    ),
)
class InvitationsView(generics.ListAPIView[Any]):
    """Las invitaciones de la organización, en cualquier estado, de la más reciente a la más
    antigua. Paginado por cursor (ADR-016). Exige lo mismo que invitar. Cada fila lleva
    identificadores (roles, quien invitó), no nombres, y nunca el enlace ni su hash. `POST`
    invita (F2-80)."""

    required_permissions = {
        "GET": ("users.invite", "users.manage"),
        "POST": ("users.invite", "users.manage"),
    }
    serializer_class = InvitationSerializer
    ordering = "-id"  # UUIDv7: el orden en que se crearon

    def get_queryset(self) -> QuerySet[Any]:
        return invitations()

    def post(self, request: Request, **kwargs: Any) -> Response:
        wanted = InviteSerializer(data=request.data)
        wanted.is_valid(raise_exception=True)
        tenant = context.current()
        assert tenant is not None  # noqa: S101 — `HasPermission` ya lo comprobó
        try:
            with rbac_errors():
                invitation, renewed = invite(tenant, **wanted.validated_data)
        except InvalidEmail:
            message = "No es una dirección de correo a la que se pueda invitar."
            raise serializers.ValidationError({"email": [message]}) from None
        except UnknownRole:
            message = "No es un rol de esta organización."
            raise serializers.ValidationError({"role_ids": [message]}) from None
        except tuple(_REFUSED) as refused:
            raise ApiError(*_REFUSED[type(refused)]) from None
        except SendTooSoon as refused:
            raise _waiting(refused) from None
        return Response(InvitationSerializer(invitation).data, status=200 if renewed else 201)


class InvitationStatusChangeSerializer(serializers.Serializer[Any]):
    status = serializers.ChoiceField(choices=[REVOKED])


class InvitationStatusView(APIView):
    """Revoca (`REVOKED`) una invitación pendiente, caducada o no: deja de poder aceptarse y su
    correo se puede invitar otra vez. Repetir la petición no cambia nada. 403: sin
    `users.invite` y `users.manage`, o con un rol en la invitación que el actor no podría
    asignar. 404: la invitación no es de la organización. 409 `INVALID_TRANSITION`: ya se
    aceptó, o está anotada como caducada. 409 `LAST_OWNER`: la organización no tiene rol
    Owner y no admite ningún cambio."""

    required_permissions = {"PUT": ("users.invite", "users.manage")}

    @extend_schema(
        operation_id="invitations_set_status",
        tags=["members"],
        request=InvitationStatusChangeSerializer,
        responses={200: InvitationSerializer, **errors(400, 401, 403, 404, 409)},
    )
    def put(self, request: Request, invitation_id: UUID, **kwargs: Any) -> Response:
        InvitationStatusChangeSerializer(data=request.data).is_valid(raise_exception=True)
        tenant = context.current()
        assert tenant is not None  # noqa: S101 — `HasPermission` ya lo comprobó
        try:
            with rbac_errors():
                invitation = revoke_invitation(tenant, invitation_id=invitation_id)
        except InvalidTransition:
            message = "Solo se revoca una invitación pendiente."
            raise ApiError("INVALID_TRANSITION", 409, message) from None
        return Response(InvitationSerializer(invitation).data)


class InvitationResendView(APIView):
    """Reenvía una invitación pendiente, caducada o no: la renueva con sus roles y las reglas de
    invitar (ADR-020 §1). El enlace anterior deja de valer. 409: `INVALID_TRANSITION`,
    `ALREADY_MEMBER`, `INVITATION_SEND_LIMIT` (5 envíos), `INVITATION_ROLE_GONE` o `LAST_OWNER`.
    429 con `Retry-After`: `INVITATION_ADDRESS_LIMIT` (3 envíos a ese correo en 24 h) o
    `INVITATION_SEND_TOO_SOON` (15 minutos entre dos)."""

    required_permissions = {"POST": ("users.invite", "users.manage")}

    @extend_schema(
        operation_id="invitations_resend",
        tags=["members"],
        request=None,
        responses={200: InvitationSerializer, **errors(401, 403, 404, 409, 429)},
    )
    def post(self, request: Request, invitation_id: UUID, **kwargs: Any) -> Response:
        tenant = context.current()
        assert tenant is not None  # noqa: S101 — `HasPermission` ya lo comprobó
        try:
            with rbac_errors():
                invitation = resend_invitation(tenant, invitation_id=invitation_id)
        except InvalidTransition:
            message = "Solo se reenvía una invitación pendiente."
            raise ApiError("INVALID_TRANSITION", 409, message) from None
        except RoleGone:
            message = "Un rol de la invitación ya no existe: invita otra vez con otros roles."
            raise ApiError("INVITATION_ROLE_GONE", 409, message) from None
        except tuple(_REFUSED) as refused:
            raise ApiError(*_REFUSED[type(refused)]) from None
        except SendTooSoon as refused:
            raise _waiting(refused) from None
        return Response(InvitationSerializer(invitation).data)


class InvitationLinkSerializer(serializers.Serializer[Any]):
    token = serializers.CharField(
        max_length=128,
        trim_whitespace=False,
        write_only=True,
        help_text="El secreto del enlace, del fragmento de la URL.",
    )


class InvitationPreviewSerializer(serializers.Serializer[Any]):
    organization_name = serializers.CharField(help_text="La organización que invita.")
    email = serializers.CharField(help_text="El correo invitado.")
    needs_account = serializers.BooleanField(
        help_text="El correo no tiene cuenta: hay que crearla (si no, iniciar sesión)."
    )


class InvitationPreviewView(APIView):
    permission_classes = [Public]

    @extend_schema(
        operation_id="invitations_preview",
        tags=["invitations"],
        auth=[],
        request=InvitationLinkSerializer,
        responses={200: InvitationPreviewSerializer, **errors(400, 403, 429)},
    )
    def post(self, request: Request) -> Response:
        """Lo que enseña el enlace de una invitación pendiente: la organización, el correo
        invitado y si hay que crear una cuenta. No cambia nada. 400 `INVALID_INVITATION_LINK`:
        el enlace no vale, sin decir por qué. 429: demasiados intentos desde esta red."""
        sent = InvitationLinkSerializer(data=request.data)
        sent.is_valid(raise_exception=True)
        shown = invitation_links.preview(request._request, **sent.validated_data)
        return Response(InvitationPreviewSerializer(shown._asdict()).data)


_OPTIONAL: dict[str, Any] = {
    "required": False,
    "allow_blank": True,
    "default": "",
    "write_only": True,
}


class InvitationAcceptSerializer(InvitationLinkSerializer):  # sin cuenta: nombre y contraseña
    password = serializers.CharField(max_length=1024, trim_whitespace=False, **_OPTIONAL)
    first_name = serializers.CharField(max_length=150, **_OPTIONAL)
    last_name = serializers.CharField(max_length=150, **_OPTIONAL)


class InvitationAcceptedSerializer(serializers.Serializer[Any]):
    organization_name = serializers.CharField(help_text="La organización a la que se entró.")
    account_created = serializers.BooleanField(help_text="Se creó una cuenta para el correo.")


class InvitationAcceptView(APIView):
    permission_classes = [Public]

    @extend_schema(
        operation_id="invitations_accept",
        tags=["invitations"],
        auth=[],
        request=InvitationAcceptSerializer,
        responses={200: InvitationAcceptedSerializer, **errors(400, 403, 409, 429)},
    )
    def post(self, request: Request) -> Response:
        """Acepta la invitación: la membresía con sus roles y, sin cuenta, la crea (`first_name`,
        `password`); con cuenta, hay que tener su sesión. No inicia sesión. 400: el enlace no vale
        (`INVALID_INVITATION_LINK`, sin decir por qué) o `VALIDATION_ERROR`. 409 `ACCOUNT_EXISTS`:
        el correo tiene cuenta y no es la de la sesión. 429: demasiados intentos desde esta red."""
        sent = InvitationAcceptSerializer(data=request.data)
        sent.is_valid(raise_exception=True)
        try:
            done = invitation_links.accept(request._request, **sent.validated_data)
        except ValidationError as weak:
            raise serializers.ValidationError(get_error_detail(weak)) from None
        return Response(InvitationAcceptedSerializer(done._asdict()).data)

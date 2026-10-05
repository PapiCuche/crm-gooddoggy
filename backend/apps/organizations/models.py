"""Organizaciones (platform-owned, sin RLS de tenant; ADR-001 §2), sus membresías y sus
sucursales, equipos e integrantes de cada equipo (tenant-owned)."""

from django.conf import settings
from django.db import models

from core.db.models import TenantModel, uuid7_primary_key


class Organization(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "ACTIVE"
        TRIAL = "TRIAL"
        SUSPENDED = "SUSPENDED"

    id = uuid7_primary_key()
    slug = models.SlugField(max_length=63, unique=True)
    name = models.CharField(max_length=200)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.ACTIVE)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "organizations"

    def __str__(self) -> str:
        return self.slug


class OrganizationMembership(TenantModel):
    """Pertenencia de un usuario global a una organización (tenant-owned, ADR-002 §3.2).

    Es el "usuario" desde el punto de vista del CRM. No lleva rol: los roles se asignarán a la
    membresía (`membership_roles`, F2-04). Solo `ACTIVE` da acceso al tenant.
    """

    class Status(models.TextChoices):
        INVITED = "INVITED"
        ACTIVE = "ACTIVE"
        SUSPENDED = "SUSPENDED"
        DEACTIVATED = "DEACTIVATED"

    id = uuid7_primary_key()
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.ACTIVE)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # Sin tenant activo: solo dentro de `user_scope`, donde RLS deja ver las filas del usuario.
    for_user = models.Manager()

    class Meta:
        db_table = "organization_memberships"
        default_manager_name = "objects"
        constraints = [
            models.UniqueConstraint(  # destino de las FK compuestas `*_user_id` (ADR-001)
                fields=["organization_id", "user"], name="organization_memberships_org_user_uq"
            ),
            models.UniqueConstraint(  # destino de las FK compuestas `membership_id` (access)
                fields=["organization_id", "id"], name="organization_memberships_org_id_uq"
            ),
            models.CheckConstraint(
                condition=models.Q(status__in=["INVITED", "ACTIVE", "SUSPENDED", "DEACTIVATED"]),
                name="organization_memberships_status_ck",
            ),
        ]

    def __str__(self) -> str:
        return str(self.pk)


class Branch(TenantModel):
    """Sucursal o tienda física (tenant-owned; modelo de datos §E.1).

    `code` es la clave que eligen las personas. La BD solo admite mayúsculas ASCII, cifras y
    guiones entre ellas: así dos códigos de una organización no se distinguen solo por
    mayúsculas, acentos, espacios o letras Unicode de igual aspecto. Los parecidos dentro de
    ASCII (`O` y `0`, `I` y `1`) siguen siendo códigos distintos.
    """

    id = uuid7_primary_key()
    code = models.CharField(max_length=20)
    name = models.CharField(max_length=100)
    address = models.CharField(max_length=255, blank=True)
    district = models.CharField(max_length=100, blank=True)
    city = models.CharField(max_length=100, blank=True)
    phone = models.CharField(max_length=32, blank=True)
    timezone = models.CharField(max_length=64, default="America/Lima")  # nombre IANA
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "branches"
        constraints = [
            models.UniqueConstraint(
                fields=["organization_id", "code"], name="branches_org_code_uq"
            ),
            models.UniqueConstraint(  # destino de las FK compuestas `branch_id` que vendrán
                fields=["organization_id", "id"], name="branches_org_id_uq"
            ),
            models.CheckConstraint(
                condition=models.Q(code__regex=r"^[A-Z0-9]+(-[A-Z0-9]+)*$"), name="branches_code_ck"
            ),
        ]

    def __str__(self) -> str:
        return self.code


class Team(TenantModel):
    """Equipo de la organización: Ventas, Soporte… (tenant-owned; modelo de datos §E.2).

    `slug` es su clave estable. La BD solo admite minúsculas ASCII, cifras y guiones entre
    ellas. Sus integrantes están en `TeamMember`.
    """

    class Strategy(models.TextChoices):
        MANUAL = "MANUAL"
        ROUND_ROBIN = "ROUND_ROBIN"
        LOAD_BALANCED = "LOAD_BALANCED"
        SKILL_BASED = "SKILL_BASED"
        AI_RULES = "AI_RULES"

    id = uuid7_primary_key()
    slug = models.CharField(max_length=50)
    name = models.CharField(max_length=100)
    description = models.CharField(max_length=255, blank=True)
    # Cómo se reparten las conversaciones del equipo. Nada la aplica hasta el Inbox (Fase 6).
    assignment_strategy = models.CharField(
        max_length=16, choices=Strategy.choices, default=Strategy.MANUAL
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "teams"
        constraints = [
            models.UniqueConstraint(fields=["organization_id", "slug"], name="teams_org_slug_uq"),
            models.UniqueConstraint(  # destino de las FK compuestas `team_id` que vendrán
                fields=["organization_id", "id"], name="teams_org_id_uq"
            ),
            models.CheckConstraint(
                condition=models.Q(slug__regex=r"^[a-z0-9]+(-[a-z0-9]+)*$"), name="teams_slug_ck"
            ),
            models.CheckConstraint(
                condition=models.Q(
                    assignment_strategy__in=[
                        "MANUAL",
                        "ROUND_ROBIN",
                        "LOAD_BALANCED",
                        "SKILL_BASED",
                        "AI_RULES",
                    ]
                ),
                name="teams_assignment_strategy_ck",
            ),
        ]

    def __str__(self) -> str:
        return self.slug


class TeamMember(TenantModel):
    """Pertenencia de una membresía a un equipo (N:M; modelo de datos §E.2).

    Las FK al equipo y a la membresía son compuestas con `organization_id` (migración): la BD
    impide enlazar un equipo de una organización con una membresía de otra. Por eso las FK
    simples de Django no crean constraint ni índice propios. De aquí salen los equipos propios
    del motor de autorización (`ExecutionContext.team_ids`): cuentan todos, también si el
    equipo o la pertenencia están inactivos.
    """

    class Role(models.TextChoices):
        MEMBER = "MEMBER"
        SUPERVISOR = "SUPERVISOR"

    id = uuid7_primary_key()
    team = models.ForeignKey(
        Team, models.PROTECT, related_name="members", db_constraint=False, db_index=False
    )
    membership = models.ForeignKey(
        OrganizationMembership,
        models.PROTECT,
        related_name="+",
        db_constraint=False,
        db_index=False,
    )
    team_role = models.CharField(max_length=16, choices=Role.choices, default=Role.MEMBER)
    # Participa en la asignación automática del equipo. Nada la aplica hasta el Inbox (Fase 6).
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "team_members"
        constraints = [
            models.UniqueConstraint(
                fields=["organization_id", "team", "membership"],
                name="team_members_team_membership_uq",
            ),
            models.CheckConstraint(
                condition=models.Q(team_role__in=["MEMBER", "SUPERVISOR"]),
                name="team_members_team_role_ck",
            ),
        ]
        indexes = [  # lado referenciante de la FK a la membresía y «equipos de una membresía»
            models.Index(
                fields=["organization_id", "membership"], name="team_members_org_member_idx"
            )
        ]

    def __str__(self) -> str:
        return str(self.pk)

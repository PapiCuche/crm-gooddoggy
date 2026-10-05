"""Catálogo de permisos y roles plantilla (ADR-003 §5): lo define el código, no la UI.

El alcance es un atributo de la concesión, nunca parte del código del permiso. La autorización
dependerá de permisos y alcances (F2-05): nada debe decidir por el código o el nombre de un rol.
El catálogo solo incluye permisos de módulos que existen; cada fase añade los suyos.
"""

from dataclasses import dataclass

from django.db import models


class Scope(models.TextChoices):
    OWN = "OWN"
    TEAM = "TEAM"
    BRANCH = "BRANCH"
    ORGANIZATION = "ORGANIZATION"


@dataclass(frozen=True, slots=True)
class PermissionDef:
    code: str
    module: str
    is_sensitive: bool = False
    supports_scope: bool = False


PERMISSIONS: tuple[PermissionDef, ...] = (  # docs/fase-0/03 §H (catálogo v1)
    PermissionDef("organization.view", "organization"),
    PermissionDef("organization.manage", "organization", is_sensitive=True),
    PermissionDef("branches.manage", "organization"),
    PermissionDef("users.view", "users"),
    PermissionDef("users.manage", "users", is_sensitive=True),
    PermissionDef("users.invite", "users"),
    PermissionDef("teams.view", "users"),
    PermissionDef("teams.manage", "users"),
    PermissionDef("roles.view", "users"),
    PermissionDef("roles.manage", "users", is_sensitive=True),
    PermissionDef("audit.view", "audit", is_sensitive=True),
)
BY_CODE = {permission.code: permission for permission in PERMISSIONS}


@dataclass(frozen=True, slots=True)
class RoleTemplate:
    """Rol que se clona en cada organización. `code` solo sirve de clave de idempotencia."""

    code: str
    name: str
    grants: dict[str, Scope | None]
    is_owner_role: bool = False


def _unscoped(*codes: str) -> dict[str, Scope | None]:
    return dict.fromkeys(codes)


ROLE_TEMPLATES: tuple[RoleTemplate, ...] = (
    RoleTemplate(
        "owner",
        "Owner",
        {p.code: Scope.ORGANIZATION if p.supports_scope else None for p in PERMISSIONS},
        is_owner_role=True,
    ),
    RoleTemplate(
        "admin",
        "Administrador",
        _unscoped(
            "organization.view",
            "branches.manage",
            "users.view",
            "users.manage",
            "users.invite",
            "teams.view",
            "teams.manage",
            "roles.view",
        ),
    ),
    RoleTemplate(
        "supervisor", "Supervisor", _unscoped("organization.view", "users.view", "teams.view")
    ),
    RoleTemplate("seller", "Vendedor", _unscoped("organization.view")),
)

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
    PermissionDef("products.view", "products"),
    PermissionDef("products.manage", "products"),
    PermissionDef("products.import", "products"),
    PermissionDef("categories.manage", "products"),
    PermissionDef("brands.manage", "products"),
    PermissionDef("inventory.view", "inventory"),
    PermissionDef("inventory.manage", "inventory"),
    PermissionDef("inventory.adjust", "inventory"),
    PermissionDef("inventory.transfer", "inventory"),
    PermissionDef("warehouses.manage", "inventory"),
    PermissionDef("contacts.view", "contacts", supports_scope=True),  # los primeros con alcance
    PermissionDef("contacts.create", "contacts"),
    PermissionDef("contacts.update", "contacts", supports_scope=True),
    PermissionDef("contacts.delete", "contacts", supports_scope=True),
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
            "products.view",
            "products.manage",
            "products.import",
            "categories.manage",
            "brands.manage",
            "inventory.view",
            "inventory.manage",
            "inventory.adjust",
            "inventory.transfer",
            "warehouses.manage",
            "contacts.create",
        )
        | dict.fromkeys(
            ("contacts.view", "contacts.update", "contacts.delete"), Scope.ORGANIZATION
        ),
    ),
    RoleTemplate(
        "supervisor",
        "Supervisor",
        _unscoped(
            "organization.view", "users.view", "teams.view", "products.view", "inventory.view"
        )
        | {"contacts.view": Scope.TEAM},
    ),
    RoleTemplate(
        "seller",
        "Vendedor",
        _unscoped("organization.view", "products.view", "inventory.view", "contacts.create")
        | {"contacts.view": Scope.OWN, "contacts.update": Scope.OWN},
    ),
)

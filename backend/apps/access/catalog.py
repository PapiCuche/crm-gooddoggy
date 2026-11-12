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
    PermissionDef("settings.manage", "organization"),  # F4-11: las etiquetas, por ahora
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
    PermissionDef("prices.view", "prices"),  # F3-40: los precios
    PermissionDef("prices.view_wholesale", "prices"),
    PermissionDef("prices.view_distributor", "prices"),
    PermissionDef("prices.view_internal", "prices"),
    PermissionDef("prices.manage", "prices", is_sensitive=True),
    PermissionDef("prices.bulk_update", "prices", is_sensitive=True),
    PermissionDef("product_cost.view", "prices", is_sensitive=True),
    PermissionDef("product_cost.manage", "prices", is_sensitive=True),
    PermissionDef("promotions.manage", "prices"),
    PermissionDef("inventory.view", "inventory"),
    PermissionDef("inventory.manage", "inventory"),
    PermissionDef("inventory.adjust", "inventory"),
    PermissionDef("inventory.transfer", "inventory"),
    PermissionDef("warehouses.manage", "inventory"),
    PermissionDef("contacts.view", "contacts", supports_scope=True),  # los primeros con alcance
    PermissionDef("contacts.create", "contacts"),
    PermissionDef("contacts.update", "contacts", supports_scope=True),
    PermissionDef("contacts.delete", "contacts", supports_scope=True),
    PermissionDef("contacts.assign", "contacts"),  # sin alcance: lo decide `contacts.view`
    # F5-02: `opportunities.view` lee los pipelines y los motivos de pérdida (configuración de
    # la organización: cualquier alcance). F5-04: el resto de 03 §H; ninguno es sensible.
    PermissionDef("opportunities.view", "opportunities", supports_scope=True),
    PermissionDef("opportunities.create", "opportunities"),
    PermissionDef("opportunities.update", "opportunities", supports_scope=True),
    PermissionDef("opportunities.assign", "opportunities"),  # sin alcance: lo decide `.view`
    PermissionDef("opportunities.delete", "opportunities", supports_scope=True),
    PermissionDef("opportunities.mark_won", "opportunities", supports_scope=True),
    PermissionDef("opportunities.mark_lost", "opportunities", supports_scope=True),
    PermissionDef("pipelines.manage", "opportunities"),
    # F5-08b: las tareas (03 §H). `tasks.update` cambia, completa, cancela, reabre y borra; el
    # alcance de `tasks.view` decide a cuáles llega `tasks.assign`.
    PermissionDef("tasks.view", "tasks", supports_scope=True),
    PermissionDef("tasks.create", "tasks"),
    PermissionDef("tasks.update", "tasks", supports_scope=True),
    PermissionDef("tasks.assign", "tasks"),
    # F5-09d: las cotizaciones (03 §H). El alcance es por quien la creó; `quotes.update` cambia el
    # borrador, `send` y `cancel` actúan sobre las que alcanza `view`; `approve`,
    # `override_price` (precio manual) y `discount` (limitado por `discount_policies`, F5-09b) no
    # tienen alcance.
    PermissionDef("quotes.view", "quotes", supports_scope=True),
    PermissionDef("quotes.create", "quotes"),
    PermissionDef("quotes.update", "quotes", supports_scope=True),
    PermissionDef("quotes.send", "quotes", supports_scope=True),
    PermissionDef("quotes.approve", "quotes"),
    PermissionDef("quotes.cancel", "quotes", supports_scope=True),
    PermissionDef("quotes.override_price", "quotes"),
    PermissionDef("quotes.discount", "quotes"),
    # F5-09b4: configura el límite de descuento de cada rol; sensible, con step-up en la API.
    PermissionDef("discount_policies.manage", "quotes", is_sensitive=True),
    # F6-01: el Inbox. `conversations.view`, `messages.send` y `messages.note` admiten alcance
    # (propias, de su equipo y sin asignar); `assign` no: qué conversaciones alcanza lo decide
    # `view`, como `contacts.assign`. `channels.manage` configura las cuentas de canal.
    PermissionDef("conversations.view", "conversations", supports_scope=True),
    PermissionDef("conversations.assign", "conversations"),
    PermissionDef("messages.send", "conversations", supports_scope=True),
    PermissionDef("messages.note", "conversations", supports_scope=True),
    PermissionDef("channels.manage", "conversations"),
)
BY_CODE = {permission.code: permission for permission in PERMISSIONS}


@dataclass(frozen=True, slots=True)
class RoleTemplate:
    """Rol que se clona en cada organización. `code` solo sirve de clave de idempotencia."""

    code: str
    name: str
    grants: dict[str, Scope | None]
    is_owner_role: bool = False


# F5-04: las acciones con alcance de las oportunidades; Supervisor y Vendedor operan las suyas
# (ver, cambiar, ganar, perder) y solo el Administrador borra (03 §H: «leads / opportunities»).
INBOX_SCOPED = ("conversations.view", "messages.send", "messages.note")  # F6-01
OPPORTUNITY_OPERATE = ("view", "update", "mark_won", "mark_lost")
OPPORTUNITY_SCOPED = (*OPPORTUNITY_OPERATE, "delete")
TASK_SCOPED = (
    "tasks.view",
    "tasks.update",
)  # F5-08b: cada rol opera las suyas, las del equipo o todas


QUOTE_SCOPED = ("quotes.view", "quotes.update", "quotes.send", "quotes.cancel")  # F5-09d


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
            "settings.manage",  # F4-11: no está en la matriz de 03 §H; a confirmar
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
            "prices.view",  # todos menos los costos (⚙️ en la matriz)
            "prices.view_wholesale",
            "prices.view_distributor",
            "prices.view_internal",
            "prices.manage",
            "prices.bulk_update",
            "promotions.manage",
            "inventory.view",
            "inventory.manage",
            "inventory.adjust",
            "inventory.transfer",
            "warehouses.manage",
            "contacts.create",
            "contacts.assign",
            "opportunities.create",  # F5-04: «leads / opportunities» ORG en 03 §H
            "opportunities.assign",
            "tasks.create",  # F5-08b
            "tasks.assign",
            "quotes.create",  # F5-09d: 03 §H da crear y enviar a los cuatro y aprobar a los tres
            "quotes.approve",
            "quotes.override_price",  # 03 §H no lo lista; a confirmar
            "quotes.discount",
            "discount_policies.manage",  # F5-09b4: Owner y Administrador, 03 §H; sensible
            "pipelines.manage",  # F5-02: la matriz de 03 §H no lo lista; a confirmar
            "conversations.assign",  # F6-01: «conversations.* ✅» para el Administrador en 03 §H
            "channels.manage",  # 03 §H lo marca ⚙️ (lo activa el Owner); a confirmar
        )
        | dict.fromkeys(
            (
                "contacts.view",
                "contacts.update",
                "contacts.delete",
                *(f"opportunities.{action}" for action in OPPORTUNITY_SCOPED),
                *INBOX_SCOPED,
                *TASK_SCOPED,
                *QUOTE_SCOPED,
            ),
            Scope.ORGANIZATION,
        ),
    ),
    RoleTemplate(
        "supervisor",
        "Supervisor",
        _unscoped(
            "organization.view",
            "users.view",
            "teams.view",
            "products.view",
            "prices.view",
            "prices.view_wholesale",
            "inventory.view",
            "opportunities.create",  # F5-04: crea y reparte las de su equipo, no borra
            "opportunities.assign",
            "tasks.create",  # F5-08b: crea y reparte las de su equipo
            "tasks.assign",
            "quotes.create",  # F5-09d: crea y aprueba las de su equipo
            "quotes.approve",  # sin `quotes.override_price`: 03 §H no lo lista y el precio manual
            "quotes.discount",  # puede ir bajo el costo sin que la cotización lo vea (a decidir)
            "conversations.assign",  # F6-01: reparte las de su equipo
        )
        | {"contacts.view": Scope.TEAM}
        | {f"opportunities.{a}": Scope.TEAM for a in OPPORTUNITY_OPERATE}  # F5-04, 03 §H
        | dict.fromkeys((*INBOX_SCOPED, *TASK_SCOPED, *QUOTE_SCOPED), Scope.TEAM),
    ),
    RoleTemplate(
        "seller",
        "Vendedor",
        _unscoped(
            "organization.view",
            "products.view",
            "prices.view",
            "inventory.view",
            "contacts.create",
            "opportunities.create",
            "tasks.create",  # F5-08b: las suyas; no reparte
            "quotes.create",  # F5-09d: las suyas; no aprueba ni escribe un precio manual
            "quotes.discount",  # dentro de su `discount_policies` (F5-09b)
        )
        | {
            "contacts.view": Scope.OWN,
            "contacts.update": Scope.OWN,
        }
        | {f"opportunities.{a}": Scope.OWN for a in OPPORTUNITY_OPERATE}
        | dict.fromkeys(INBOX_SCOPED, Scope.OWN)  # F6-01: «OWN + sin asignar» en 03 §H
        | dict.fromkeys((*TASK_SCOPED, *QUOTE_SCOPED), Scope.OWN),
    ),
)

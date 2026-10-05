import logging
from typing import Any

from django.apps import AppConfig
from django.db import DEFAULT_DB_ALIAS, connections, transaction
from django.db.models.signals import post_migrate

FIELDS = ["code", "module", "is_sensitive", "supports_scope"]
logger = logging.getLogger(__name__)


def sync_permissions(sender: Any, using: str = "default", **kwargs: Any) -> None:
    """Tras cada `migrate` (crm_migrator): deja `permissions` igual que el catálogo del código.

    Solo escribe lo que cambia (un despliegue sin cambios no bloquea filas). Un código que ya
    no está en el catálogo se borra si nadie lo tiene concedido; si está concedido se conserva
    y se avisa: retirarlo o renombrarlo exige una migración de datos que reescriba las
    concesiones. Cambiar `supports_scope` de un permiso concedido falla por la FK, a propósito.
    Añadir permisos no toca los roles que ya existen, salvo el rol Owner de cada organización,
    que sigue al catálogo (`extend_owner_roles`, ADR-018).
    """
    from apps.access.catalog import PERMISSIONS
    from apps.access.models import Permission, RolePermission

    with connections[using].cursor() as cursor:
        cursor.execute("SELECT to_regclass('public.permissions')")
        if cursor.fetchone()[0] is None:
            return  # `migrate access zero` o migración parcial
    table = Permission.objects.using(using)
    with transaction.atomic(using=using):
        current = {row[0]: row[1:] for row in table.values_list(*FIELDS)}
        wanted = {p.code: (p.module, p.is_sensitive, p.supports_scope) for p in PERMISSIONS}
        table.bulk_create(
            Permission(code=code, **dict(zip(FIELDS[1:], values, strict=True)))
            for code, values in wanted.items()
            if code not in current
        )
        for code, values in wanted.items():
            if code in current and current[code] != values:
                table.filter(code=code).update(**dict(zip(FIELDS[1:], values, strict=True)))
        granted = RolePermission._base_manager.using(using).values("permission_id")
        stale = table.exclude(code__in=wanted)
        stale.exclude(code__in=granted).delete()
        kept = list(stale.values_list("code", flat=True))
        if kept:
            logger.warning(
                "permisos fuera del catálogo que siguen concedidos", extra={"codes": kept}
            )
        extend_owner_roles(using)


def extend_owner_roles(using: str) -> int:
    """ADR-018: el rol Owner de cada organización tiene todo el catálogo. Añade las concesiones
    que le falten, con alcance `ORGANIZATION` si el permiso lo admite; devuelve cuántas.

    Solo añade: no quita nada ni cambia un alcance, y no toca ningún otro rol. El rol Owner se
    localiza por `is_owner_role`. Corre en el job de migraciones (`crm_migrator`, dueño de las
    tablas), nunca en el runtime: `crm_app` no puede escribir en otra organización. Lo que añade
    queda en la auditoría de plataforma; no hay actor de tenant, y no se inventa uno.
    """
    from apps.access.catalog import PERMISSIONS, Scope
    from apps.access.models import Role, RolePermission
    from apps.audit import platform

    owners = Role._base_manager.using(using).filter(is_owner_role=True)
    grants = RolePermission._base_manager.using(using)
    held: set[tuple[Any, Any]] = set(
        grants.filter(role__in=owners).values_list("role_id", "permission_id")
    )
    missing = [
        RolePermission(
            organization_id=organization,
            role_id=role,
            permission_id=permission.code,
            supports_scope=permission.supports_scope,
            scope=Scope.ORGANIZATION if permission.supports_scope else None,
        )
        for role, organization in owners.values_list("pk", "organization_id")
        for permission in PERMISSIONS
        if (role, permission.code) not in held
    ]
    if missing:
        grants.bulk_create(missing)
        if using == DEFAULT_DB_ALIAS:  # la auditoría de plataforma escribe en esa conexión
            platform.record(
                "access.owner_roles.extended",
                actor_type=platform.Actor.SYSTEM,
                metadata={
                    "permissions": sorted({grant.permission_id for grant in missing}),
                    "roles": len({grant.role_id for grant in missing}),
                    "grants": len(missing),
                },
            )
    return len(missing)


class AccessConfig(AppConfig):
    name = "apps.access"
    label = "access"

    def ready(self) -> None:
        post_migrate.connect(sync_permissions, sender=self, dispatch_uid="access_permissions")

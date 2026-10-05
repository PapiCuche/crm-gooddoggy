import logging
from typing import Any

from django.apps import AppConfig
from django.db import DEFAULT_DB_ALIAS, connections, transaction
from django.db.models.signals import post_migrate

FIELDS = ["code", "module", "is_sensitive", "supports_scope"]
STARTED, DONE = "access.owner_roles.extend.started", "access.owner_roles.extended"
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
    extend_owner_roles(using)  # fuera de esa transacción: cada organización lleva la suya


def extend_owner_roles(using: str) -> int:
    """ADR-018: el rol Owner de cada organización tiene todo el catálogo. Añade las concesiones
    que le falten, con alcance `ORGANIZATION` si el permiso lo admite; devuelve cuántas.

    Solo añade: no quita nada ni cambia un alcance, y no toca ningún otro rol. El rol Owner se
    localiza por `is_owner_role`. Cada organización es una transacción, dentro de su
    `tenant_scope` con el actor SYSTEM: la concesión queda en la auditoría de esa organización,
    como cualquier otra (ADR-013 §1), y la operación entera, en la de plataforma: una fila de
    intención antes de entrar en la primera organización y una de resultado al terminar, también
    si falla (ADR-013 §5). Todas las filas de una ejecución comparten `correlation_id`.

    Corre en el job de migraciones (`crm_migrator`, dueño de las tablas). El runtime no puede:
    sin tenant activo `crm_app` no ve los roles de ninguna organización, y con uno activo la
    función se niega.
    """
    from django.db.models import Count, Q

    from apps.access.catalog import PERMISSIONS, Scope
    from apps.access.models import Role, RolePermission
    from apps.audit import platform
    from apps.audit.apps import ensure_partitions
    from apps.audit.services import Entity, Result, record
    from core.ids import new_id
    from core.observability.context import bound
    from core.tenancy.context import TenantContext, require_no_tenant
    from core.tenancy.scope import assert_clean_connection, tenant_scope

    require_no_tenant("extend_owner_roles")
    catalog = {permission.code: permission for permission in PERMISSIONS}
    held = Count("grants", filter=Q(grants__permission_id__in=catalog))
    owners = Role._base_manager.using(using).filter(is_owner_role=True)
    short = list(
        owners.annotate(held=held)
        .filter(held__lt=len(catalog))
        .values_list("pk", "organization_id")
    )
    if not short:
        return 0  # lo normal: un despliegue sin permisos nuevos no escribe nada
    assert_clean_connection(using=using)  # como un comando de plataforma: sin contexto heredado
    # `access` va antes que `audit` en `post_migrate`: la partición del mes puede faltar.
    ensure_partitions(sender=None, using=using)
    run = str(new_id())  # une las filas de cada organización con las de plataforma
    added: list[str] = []  # solo lo confirmado
    changed = 0

    def audit(action: str, metadata: dict[str, Any], result: Result = Result.SUCCESS) -> None:
        if using == DEFAULT_DB_ALIAS:  # la auditoría de plataforma escribe en esa conexión
            with bound(request_id=None, correlation_id=run):
                actor = platform.Actor.SYSTEM
                platform.record(action, actor_type=actor, metadata=metadata, result=result)

    def summary() -> dict[str, Any]:
        codes = sorted(set(added))
        return {
            "permissions": codes[:50],  # acotado: la fila tiene un tamaño máximo
            "codes": len(codes),
            "roles": changed,
            "grants": len(added),
        }

    audit(STARTED, {"roles": len(short)})  # intención: si no se puede auditar, no empieza
    try:
        for role_id, organization_id in short:
            ctx = TenantContext(organization_id, "command", correlation_id=run)  # actor SYSTEM
            mine: list[str] = []
            with tenant_scope(ctx, using=using):
                role = Role.objects.using(using).get(pk=role_id)
                grants = RolePermission.objects.using(using)
                has = set(grants.filter(role=role).values_list("permission_id", flat=True))
                for code, permission in catalog.items():
                    if code in has:
                        continue
                    scope = Scope.ORGANIZATION if permission.supports_scope else None
                    grants.create(
                        role=role,
                        permission_id=code,
                        supports_scope=permission.supports_scope,
                        scope=scope,
                    )
                    changes = {"permission": [None, code], "scope": [None, scope]}
                    entity = Entity("role", role.pk, role.name)
                    record(ctx, "role.permission_granted", entity, changes, {"source": "catalog"})
                    mine.append(code)
            added += mine
            changed += bool(mine)
    except Exception as error:
        try:  # lo ya confirmado no se deshace: queda dicho cuánto, y que no terminó
            audit(DONE, {**summary(), "error": type(error).__name__}, Result.FAILED)
        except Exception:
            logger.exception("rol Owner: no se pudo registrar el resultado")
        raise
    audit(DONE, summary())
    return len(added)


class AccessConfig(AppConfig):
    name = "apps.access"
    label = "access"

    def ready(self) -> None:
        post_migrate.connect(sync_permissions, sender=self, dispatch_uid="access_permissions")

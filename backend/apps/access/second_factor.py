"""Qué membresía exige segundo factor para entrar a su organización (ADR-003 §4, ADR-023 §9).

Es API pública de `access` junto a `selectors`, pero **no** forma parte del motor de
autorización: no concede ni deniega nada, y por eso puede leer la marca de Owner y el rol
Administrador de plantilla, que el motor nunca mira (`tests/test_authorization.py`). Solo
añade una exigencia; quien la aplica es la política de `apps.members.second_factor` (ADR-038).
"""

from django.db.models import Q

from apps.access.catalog import ADMIN_TEMPLATE, PERMISSIONS
from apps.access.models import Role
from apps.access.selectors import AccessDenied, Denied
from core.tenancy.context import ActorType, TenantContext
from core.tenancy.scope import require_scope

SENSITIVE = tuple(permission.code for permission in PERMISSIONS if permission.is_sensitive)


def required(ctx: TenantContext) -> bool:
    """Sí si alguno de los roles de la membresía de `ctx` es el Owner (`is_owner_role`), es el
    Administrador de plantilla (`is_system` y su código, nunca el nombre: quitarle `users.manage`
    no lo libera) o concede un permiso sensible, según el catálogo o según
    `permissions.is_sensitive`. Una consulta, dentro del `tenant_scope` de `ctx` (RLS)."""
    alias = require_scope(ctx)
    if ctx.actor_type != ActorType.USER or ctx.user_id is None:
        raise AccessDenied(Denied.MEMBERSHIP)
    held = Role.objects.using(alias).filter(assignments__membership__user_id=ctx.user_id)
    demands = (
        Q(is_owner_role=True)
        | Q(is_system=True, code=ADMIN_TEMPLATE)
        | Q(grants__permission_id__in=SENSITIVE)
        | Q(grants__permission__is_sensitive=True)
    )
    return held.filter(demands).exists()

"""El kill switch de IA en dos niveles (F8-06; ADR-034 §5, §10): `AI_ENABLED` (entorno, apagado por
omisión; la aplicación no lo cambia) y la fila de `ai_switches` de cada organización (sin fila,
apagada). El gateway llama solo con los dos encendidos y lee la fila en cada llamada (`is_on`).
Apaga `ai.kill_switch` o `ai.manage`; enciende solo `ai.manage`, con la confirmación del aviso de
datos (el segundo factor y el tope los ponen las rutas). Auditoría con el antes y el después:
`ai.kill_switch.activated` al apagar, `ai.kill_switch.deactivated` al encender; repetir el estado no
escribe ni audita. Como `crm_app` bajo RLS (0009)."""

from django.conf import settings
from django.db import connections, transaction
from django.utils import timezone

from apps.access.selectors import AccessDenied, Denied, execution_context, has_permission
from apps.ai_gateway.models import AISwitch
from apps.audit.services import Entity, record
from core.tenancy.context import TenantContext
from core.tenancy.scope import require_scope

MANAGE, KILL = "ai.manage", "ai.kill_switch"
STOPPERS = (KILL, MANAGE)  # cualquiera de los dos apaga
_CREATE = (  # la fila, apagada; si ya existe (o otra petición la crea a la vez), nada
    "INSERT INTO public.ai_switches (organization_id, enabled, changed_at)"
    " VALUES (%s, false, now()) ON CONFLICT (organization_id) DO NOTHING"
)
# Candado consultivo por organización (de transacción), no el de la fila: las llamadas (compartido)
# no se cuelan delante de quien apaga (exclusivo), que espera solo a las reservas ya en curso.
# `LOCK_SALT` fija el espacio de claves; una colisión entre organizaciones solo comparte esperas.
LOCK_SALT, _LOCK = 7_690_006, "hashtextextended(%s::text, %s)"
_SHARED = f"SELECT pg_advisory_xact_lock_shared({_LOCK})"
_EXCLUSIVE = f"SELECT pg_advisory_xact_lock({_LOCK})"
_READ = "SELECT enabled FROM public.ai_switches WHERE organization_id = %s"  # y RLS


class NoticeNotConfirmed(ValueError):  # noqa: N818 — sin la confirmación no se enciende
    """Quien enciende no confirmó que el aviso de tratamiento de datos está publicado."""


def globally_on() -> bool:
    """`AI_ENABLED`, leído en cada uso (nunca al importar)."""
    return settings.AI_ENABLED is True


def _allowed(ctx: TenantContext, codes: tuple[str, ...]) -> None:
    ectx = execution_context(ctx)  # `AccessDenied` si no es un miembro activo
    if not any(has_permission(ectx, code) for code in codes):
        raise AccessDenied(Denied.PERMISSION)


def is_on(ctx: TenantContext) -> bool:
    """El interruptor de la organización de `ctx` (RLS), dentro de su `tenant_scope`, con el
    candado compartido hasta el fin de la transacción: apagar espera a la reserva que lo leyó
    encendido, y la siguiente ya lo ve apagado. Sin fila, apagado."""
    with connections[require_scope(ctx)].cursor() as cursor:
        cursor.execute(_SHARED, [ctx.organization_id, LOCK_SALT])
        cursor.execute(_READ, [ctx.organization_id])
        row = cursor.fetchone()
    return row is not None and row[0] is True


def read(ctx: TenantContext) -> AISwitch | None:
    """La fila de la organización, o `None` (apagada). `AccessDenied` sin ninguno de los dos."""
    _allowed(ctx, STOPPERS)  # pide el scope de `ctx`
    found: AISwitch | None = AISwitch.objects.using(require_scope(ctx)).first()
    return found


def _set(ctx: TenantContext, enabled: bool, action: str, metadata: dict[str, bool]) -> AISwitch:
    alias = require_scope(ctx)
    switches = AISwitch.objects.using(alias)
    with transaction.atomic(using=alias):
        with connections[alias].cursor() as cursor:
            cursor.execute(_EXCLUSIVE, [ctx.organization_id, LOCK_SALT])
            cursor.execute(_CREATE, [ctx.organization_id])
        current = switches.select_for_update().get()
        if current.enabled != enabled:
            switches.filter(pk=current.pk).update(
                enabled=enabled, changed_by_user_id=ctx.user_id, changed_at=timezone.now()
            )
            changes = {"enabled": [current.enabled, enabled]}
            record(ctx, action, Entity("ai_switch", current.pk), changes, metadata)
        done: AISwitch = switches.get()
    return done


def turn_off(ctx: TenantContext) -> AISwitch:
    """Apaga la IA de la organización. `AccessDenied` sin `ai.kill_switch` ni `ai.manage`."""
    require_scope(ctx)
    _allowed(ctx, STOPPERS)
    return _set(ctx, False, "ai.kill_switch.activated", {})


def turn_on(ctx: TenantContext, *, notice_confirmed: bool) -> AISwitch:
    """Enciende la IA de la organización. `AccessDenied` sin `ai.manage`; `NoticeNotConfirmed`."""
    require_scope(ctx)
    _allowed(ctx, (MANAGE,))
    if notice_confirmed is not True:
        raise NoticeNotConfirmed
    return _set(ctx, True, "ai.kill_switch.deactivated", {"notice_confirmed": True})

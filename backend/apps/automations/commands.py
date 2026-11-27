"""Comandos de las automatizaciones (F11-02; ADR-049 §4, §7, §8): configurar una regla, pausar y
reanudar la organización, y las reglas que un barrido puede ejecutar. La organización sale del
contexto; las tablas, de RLS. Orden: permiso, bloqueo, versión y transición. Auditan con ids,
códigos y números; repetir el estado no escribe ni audita. Nada de aquí crea tareas ni mensajes."""

import datetime as dt
from collections.abc import Mapping
from typing import Any

from django.conf import settings
from django.db import IntegrityError, connections, transaction
from django.db.models import F

from apps.access.selectors import AccessDenied, Denied, execution_context, has_permission
from apps.audit.services import Entity, record
from apps.automations import params as schema
from apps.automations.models import AutomationRule, AutomationSwitch, Mode
from core.tenancy.context import TenantContext
from core.tenancy.scope import require_scope

VIEW, MANAGE, KILL = "automations.view", "automations.manage", "automations.kill_switch"
SIMULATE_FOR = dt.timedelta(days=7)  # E2: al menos una semana en `SIMULATE` antes de `ON`
_KEY_TAKEN = "automation_rules_key_uq"  # otra petición creó la misma regla a la vez
_ROW = (  # la fila, sin pausar; el `UPDATE` de después la pausa y lo audita
    "INSERT INTO public.automation_switches (organization_id, paused, changed_at)"
    " VALUES (%s, false, now()) ON CONFLICT (organization_id) DO NOTHING"
)


class StaleVersion(Exception):
    """Otra escritura cambió la regla después de la versión que se editaba."""


class InvalidParams(ValueError):
    """`params` fuera del esquema cerrado; el mensaje nombra el parámetro, nunca su valor."""


class NotSimulatedLongEnough(Exception):  # noqa: N818 — la regla todavía no puede pasar a `ON`
    """`ON` solo desde `SIMULATE` con al menos `SIMULATE_FOR` (reloj de la base)."""


def globally_on() -> bool:
    """`AUTOMATIONS_ENABLED`, leído en cada uso (nunca al importar); solo `True` enciende."""
    return settings.AUTOMATIONS_ENABLED is True


def _allowed(ctx: TenantContext, codes: tuple[str, ...]) -> None:
    ectx = execution_context(ctx)  # `AccessDenied` si no es un miembro activo
    if not any(has_permission(ectx, code) for code in codes):
        raise AccessDenied(Denied.PERMISSION)


def _db_now(alias: str) -> dt.datetime:
    with connections[alias].cursor() as cursor:
        cursor.execute("SELECT now()")
        now: dt.datetime = cursor.fetchone()[0]
    return now


def set_rule(
    ctx: TenantContext,
    *,
    key: str,
    version: int,
    mode: str | None = None,
    params: Mapping[str, Any] | None = None,
) -> AutomationRule:
    """Cambia el modo y los parámetros (se mezclan con los actuales) de una regla si nadie la
    cambió desde `version` (0: todavía sin fila, en `OFF`), y audita `automation.rule.updated`.
    Sin cambios no escribe y devuelve la regla (sin guardar, con versión 0, si no tiene fila).
    `AccessDenied` sin `automations.manage`; `StaleVersion`; `NotSimulatedLongEnough`;
    `InvalidParams` (un `ValueError`); `ValueError`."""
    alias = require_scope(ctx)
    _allowed(ctx, (MANAGE,))
    if key not in schema.KEYS:
        raise ValueError("key: una regla registrada")
    if isinstance(version, bool) or not isinstance(version, int) or not 0 <= version < 2**31:
        raise ValueError("version: un entero de 0 a 2147483647")
    if mode is not None and mode not in Mode.values:
        raise ValueError("mode: OFF, SIMULATE u ON")
    if params is not None and not isinstance(params, Mapping):
        raise ValueError("params: un objeto")
    rules = AutomationRule.objects.using(alias)
    try:
        with transaction.atomic(using=alias):
            rule: AutomationRule | None = rules.select_for_update().filter(key=key).first()
            if (0 if rule is None else rule.version) != version:
                raise StaleVersion(key)
            old = rule or AutomationRule(key=key, params=schema.defaults(key), version=0)
            old_params = {**schema.defaults(key), **old.params}
            try:
                new_params = schema.validate(key, {**old_params, **(params or {})})
            except ValueError as error:
                raise InvalidParams(str(error)) from None
            new_mode, now = mode or old.mode, _db_now(alias)
            since = old.simulate_since
            simulated = since is not None and now - since >= SIMULATE_FOR
            if (
                new_mode == Mode.ON
                and old.mode != Mode.ON
                and not (old.mode == Mode.SIMULATE and simulated)
            ):
                raise NotSimulatedLongEnough(key)
            if new_mode != Mode.SIMULATE:
                since = None
            elif old.mode != Mode.SIMULATE:
                since = now  # entra en `SIMULATE`: la cuenta de los 7 días empieza ahora
            changes = {
                f"params.{name}": [old_params[name], value]
                for name, value in new_params.items()
                if old_params[name] != value
            }
            if new_mode != old.mode:
                changes["mode"] = [old.mode, new_mode]
            if not changes:
                return old
            values = {"mode": new_mode, "params": new_params, "simulate_since": since,
                      "updated_by_user_id": ctx.user_id}  # fmt: skip
            if rule is None:
                rule = rules.create(key=key, **values)
            else:
                same = rules.filter(pk=rule.pk, version=version)  # además del bloqueo
                if same.update(**values, version=F("version") + 1, updated_at=now) != 1:
                    raise StaleVersion(key)
                rule = rules.get(pk=rule.pk)
            metadata = {"key": key, "version": rule.version}
            record(ctx, "automation.rule.updated", Entity("automation_rule", rule.pk), changes,
                   metadata)  # fmt: skip
    except IntegrityError as error:
        diag = getattr(error.__cause__, "diag", None)
        if diag is not None and diag.constraint_name == _KEY_TAKEN:
            raise StaleVersion(key) from None
        raise
    return rule


def _switch(ctx: TenantContext, paused: bool, action: str) -> bool:
    alias = require_scope(ctx)
    switches = AutomationSwitch.objects.using(alias)
    with transaction.atomic(using=alias):
        if paused:  # reanudar sin fila no escribe: sin fila ya no está pausada
            with connections[alias].cursor() as cursor:
                cursor.execute(_ROW, [ctx.organization_id])
        current = switches.select_for_update().first()
        if current is not None and current.paused != paused:
            switches.filter(pk=current.pk).update(
                paused=paused, changed_by_user_id=ctx.user_id, changed_at=_db_now(alias)
            )
            changes = {"paused": [current.paused, paused]}
            record(ctx, action, Entity("automation_switch", current.pk), changes)
    return paused


def pause(ctx: TenantContext) -> bool:
    """Pausa todas las reglas de la organización (`automation.paused`), sin tocar su configuración.
    `AccessDenied` sin `automations.kill_switch` ni `automations.manage`."""
    require_scope(ctx)
    _allowed(ctx, (KILL, MANAGE))
    return _switch(ctx, True, "automation.paused")


def resume(ctx: TenantContext) -> bool:
    """Reanuda la organización (`automation.resumed`). `AccessDenied` sin `automations.manage`."""
    require_scope(ctx)
    _allowed(ctx, (MANAGE,))
    return _switch(ctx, False, "automation.resumed")


def runnable(ctx: TenantContext) -> list[AutomationRule]:
    """Las reglas que un barrido puede ejecutar ahora (§7): ninguna con `AUTOMATIONS_ENABLED`
    apagado o la organización pausada; si no, las que están en `SIMULATE` u `ON` (sin fila,
    `OFF`). Sin permisos de persona: la llama el sistema, bajo RLS."""
    alias = require_scope(ctx)
    if not globally_on() or AutomationSwitch.objects.using(alias).filter(paused=True).exists():
        return []
    return list(
        AutomationRule.objects.using(alias)
        .filter(mode__in=(Mode.SIMULATE, Mode.ON))
        .order_by("key")
    )

"""Lecturas de las automatizaciones para la API (F11-07; ADR-049 §4, §7). Bajo RLS: la organización
es la del contexto. Sin permisos de persona: los comprueba la vista."""

from apps.automations import params as schema
from apps.automations.models import AutomationRule, AutomationSwitch


def rules() -> list[AutomationRule]:
    """Una por regla registrada, en el orden del registro. Sin fila, la regla sin guardar en `OFF`,
    con sus parámetros por omisión y versión 0 (la que `set_rule` espera para crearla)."""
    stored = {rule.key: rule for rule in AutomationRule.objects.filter(key__in=schema.KEYS)}
    found = []
    for key in schema.KEYS:
        rule = stored.get(key) or AutomationRule(key=key, version=0)
        rule.params = {**schema.defaults(key), **rule.params}
        found.append(rule)
    return found


def switch() -> AutomationSwitch | None:
    """El interruptor de la organización; sin fila, no está pausada."""
    return AutomationSwitch.objects.first()

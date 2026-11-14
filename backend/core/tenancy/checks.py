"""Comprobación de configuración: la política de segundo factor se puede importar (ADR-038).

Sin ella, con `enforce` toda ruta de tenant fallaría (cerrada, pero con un 500), y con `warn`,
`me/`. `manage.py check` lo dice antes de servir nada."""

from typing import Any

from django.conf import settings
from django.core.checks import CheckMessage, Error, register
from django.utils.module_loading import import_string

from core.tenancy.second_factor import MODES

UNUSABLE = "tenancy.E001"


@register()
def second_factor_policy(app_configs: Any = None, **kwargs: Any) -> list[CheckMessage]:
    errors: list[CheckMessage] = []
    if settings.MFA_ENFORCEMENT not in MODES:
        errors.append(Error("MFA_ENFORCEMENT debe ser warn o enforce", id=UNUSABLE))
    try:
        policy = import_string(settings.TENANCY_SECOND_FACTOR_POLICY)
    except ImportError as error:
        message = f"TENANCY_SECOND_FACTOR_POLICY no se puede importar: {error}"
        return [*errors, Error(message, id=UNUSABLE)]
    if not callable(policy):
        errors.append(Error("TENANCY_SECOND_FACTOR_POLICY no es una función", id=UNUSABLE))
    return errors

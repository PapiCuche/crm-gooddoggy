"""Comprobación de configuración: `step_up_methods` solo lo aplica `HasPermission` (F2-125b).

Una vista que lo declara con otras `permission_classes` no recibiría la comprobación ni un aviso;
`manage.py check` recorre las rutas y falla (ADR-003 §4)."""

from collections.abc import Iterator, Sequence
from typing import Any

from django.core.checks import CheckMessage, Error, Tags, register
from django.urls import URLPattern, URLResolver, get_resolver

from apps.access.permissions import HasPermission

UNENFORCED = "access.E001"


def views(patterns: Sequence[Any]) -> Iterator[tuple[str, type]]:
    """Cada clase de vista de las rutas, con su ruta; las funciones no declaran nada."""
    for pattern in patterns:
        if isinstance(pattern, URLResolver):
            yield from views(pattern.url_patterns)
        elif isinstance(pattern, URLPattern):
            cls = getattr(pattern.callback, "cls", None) or getattr(
                pattern.callback, "view_class", None
            )
            if isinstance(cls, type):
                yield str(pattern.pattern), cls


def unenforced(patterns: Sequence[Any]) -> list[type]:
    """Las vistas con `step_up_methods` no vacío y sin `HasPermission` (ni subclase) entre sus
    `permission_classes`: sin ella el step-up no se aplica."""
    found: dict[type, None] = {}
    for _, cls in views(patterns):
        classes = getattr(cls, "permission_classes", None) or ()
        has = any(isinstance(c, type) and issubclass(c, HasPermission) for c in classes)
        if getattr(cls, "step_up_methods", None) and not has:
            found[cls] = None
    return list(found)


@register(Tags.urls)
def step_up_is_enforced(app_configs: Any, **kwargs: Any) -> list[CheckMessage]:
    return [
        Error(
            f"{cls.__module__}.{cls.__qualname__} declara `step_up_methods` sin `HasPermission` "
            "en `permission_classes`: el step-up no se aplicaría.",
            hint="Añade `HasPermission` a `permission_classes` o quita `step_up_methods`.",
            obj=cls,
            id=UNENFORCED,
        )
        for cls in unenforced(get_resolver().url_patterns)
    ]

"""Tareas Celery con contexto explícito (tenancy-context §3).

- `@tenant_task`: exige `organization_id` (kwarg) al encolar y ejecuta el cuerpo en `tenant_scope`.
- `@platform_task`: sin tenant (TenantManager falla dentro).
- `TenancyCheck`: bootstep del worker; si hay una tarea registrada sin decorar, no arranca.
"""

import functools
import inspect
from collections.abc import Callable
from typing import Any
from uuid import UUID

from celery import Task, bootsteps, shared_task

from core.observability.context import bound, current_correlation_id
from core.tenancy.context import ActorType, TenantContext, TenantContextError, require_no_tenant
from core.tenancy.scope import assert_clean_connection, tenant_scope


class TenantTask(Task):  # type: ignore[misc]
    tenancy = "tenant"

    def apply_async(self, args: Any = None, kwargs: Any = None, **options: Any) -> Any:
        try:
            UUID(str((kwargs or {})["organization_id"]))
        except KeyError, ValueError:
            msg = f"{self.name}: organization_id (kwarg UUID) es obligatorio"
            raise TenantContextError(msg) from None
        return super().apply_async(args, kwargs, **options)


class PlatformTask(Task):  # type: ignore[misc]
    tenancy = "platform"


def _tenant_signature(fn: Callable[..., Any]) -> inspect.Signature:
    """La firma de `fn` más los argumentos de tenant que `run` consume antes de llamarla.

    Celery valida `apply_async` contra la firma de la función registrada (`check_arguments`), y
    en Python 3.14 la obtiene con `inspect.signature`, que sigue `__wrapped__` hasta la `fn`
    original: sin esto, `organization_id` y `correlation_id` se rechazan con un `TypeError`
    aunque `run` los acepte (y el publicador del outbox no podría encolar nada)."""
    kind = inspect.Parameter.KEYWORD_ONLY
    tenant = [
        inspect.Parameter("organization_id", kind, annotation=str),
        inspect.Parameter("actor_type", kind, default=ActorType.SYSTEM),
        inspect.Parameter("actor_id", kind, default=None),
        inspect.Parameter("correlation_id", kind, default=None),
    ]
    signature = inspect.signature(fn)
    if clash := {p.name for p in tenant} & set(signature.parameters):
        raise TypeError(f"{fn.__name__}: {sorted(clash)} ya los pone @tenant_task")
    params = list(signature.parameters.values())
    rest = [p for p in params if p.kind is not inspect.Parameter.VAR_KEYWORD]
    return signature.replace(parameters=[*rest, *tenant, *params[len(rest) :]])


def tenant_task(**options: Any) -> Callable[[Callable[..., Any]], Any]:
    def decorate(fn: Callable[..., Any]) -> Any:
        @functools.wraps(fn)
        def run(
            *args: Any,
            organization_id: str,
            actor_type: str = ActorType.SYSTEM,
            actor_id: str | None = None,
            correlation_id: str | None = None,
            **kwargs: Any,
        ) -> Any:
            actor = UUID(actor_id) if actor_id else None
            ctx = TenantContext(
                UUID(organization_id),
                "celery",
                None,
                ActorType(actor_type),
                actor,
                correlation_id or current_correlation_id(),  # cabecera Celery → contexto
            )
            assert_clean_connection()
            # La correlación del TenantContext también rige logs y tareas hijas (un solo valor).
            with bound(request_id=None, correlation_id=ctx.correlation_id), tenant_scope(ctx):
                return fn(*args, **kwargs)  # los argumentos son IDs: se re-lee el estado aquí

        run.__signature__ = _tenant_signature(fn)  # type: ignore[attr-defined]
        return shared_task(base=TenantTask, **options)(run)

    return decorate


def platform_task(**options: Any) -> Callable[[Callable[..., Any]], Any]:
    def decorate(fn: Callable[..., Any]) -> Any:
        @functools.wraps(fn)
        def run(*args: Any, **kwargs: Any) -> Any:
            require_no_tenant(f"@platform_task {fn.__name__}")  # antes del cuerpo
            assert_clean_connection()
            return fn(*args, **kwargs)

        return shared_task(base=PlatformTask, **options)(run)

    return decorate


def undecorated_tasks(tasks: dict[str, Any]) -> list[str]:
    return sorted(
        name
        for name, task in tasks.items()
        if not name.startswith("celery.")
        and getattr(task, "tenancy", None) not in ("tenant", "platform")
    )


class TenancyCheck(bootsteps.Step):  # type: ignore[misc]
    """Se instancia al construir el WorkController: una excepción aquí impide el arranque."""

    def __init__(self, worker: Any, **kwargs: Any) -> None:
        worker.app.loader.import_default_modules()
        if missing := undecorated_tasks(dict(worker.app.tasks)):
            raise TenantContextError(
                "Tareas sin @tenant_task/@platform_task: " + ", ".join(missing)
            )
        super().__init__(worker, **kwargs)

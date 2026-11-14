"""WebSockets con contexto de tenant (tenancy-context §4).

Nunca se mantiene una transacción ni un scope entre `await`s: cada acceso a BD es una función
síncrona corta con su propio `tenant_scope`, vía `database_sync_to_async`. El contexto se
guarda en `self.tenant` y se pasa explícitamente (no se fija el contextvar en la conexión WS).
"""

from collections.abc import Awaitable, Callable
from typing import Any

from channels.db import database_sync_to_async

from core.tenancy.context import TenantContext, TenantContextError
from core.tenancy.resolution import OrganizationSuspended, TenantNotFound, resolve_tenant
from core.tenancy.scope import assert_clean_connection, tenant_scope
from core.tenancy.second_factor import SecondFactorRequired

CLOSE_NOT_FOUND, CLOSE_SUSPENDED = 4404, 4403
CLOSE_MFA_REQUIRED = 4428  # ADR-038: el 403 `MFA_REQUIRED` de HTTP, que un cierre no puede llevar


class TenantConsumerMixin:
    scope: dict[str, Any]
    close: Callable[..., Awaitable[None]]  # lo aporta el consumer de Channels
    tenant: TenantContext | None = None

    async def connect_tenant(self) -> bool:
        """Llamar desde `connect()`: resuelve org + membresía igual que HTTP o cierra."""
        slug = self.scope["url_route"]["kwargs"]["org_slug"]
        try:
            self.tenant = await database_sync_to_async(resolve_tenant)(
                slug, self.scope.get("user"), "ws"
            )
        except TenantNotFound:
            await self.close(code=CLOSE_NOT_FOUND)
            return False
        except OrganizationSuspended:
            await self.close(code=CLOSE_SUSPENDED)
            return False
        except SecondFactorRequired:
            await self.close(code=CLOSE_MFA_REQUIRED)
            return False
        return True

    async def in_tenant[T](self, fn: Callable[..., T], *args: Any) -> T:
        if self.tenant is None:
            raise TenantContextError("Consumer sin tenant resuelto")
        ctx = self.tenant

        def run() -> T:
            assert_clean_connection()
            with tenant_scope(ctx):
                return fn(*args)

        return await database_sync_to_async(run)()  # type: ignore[no-any-return]

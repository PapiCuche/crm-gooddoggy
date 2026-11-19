"""Comandos de la cuenta del proveedor de IA (F8-03b; ADR-034 §4): crear, rotar la clave,
desactivar, probar y cambiar el modelo (F8-05b6). Cada uno comprueba `ai.manage` antes de leer nada
o de llamar al proveedor; el segundo factor reciente y el límite de tasa los ponen las rutas
(F8-03c, F8-03d). La clave la guarda `keys` (cifrada) y no sale: ni a la auditoría (solo el
proveedor, ids, estados y el código de la prueba), ni al outbox (estos comandos no emiten eventos),
ni a un error ni a un registro.
"""

from typing import Any

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.access.selectors import AccessDenied, Denied, execution_context, has_permission
from apps.ai_gateway import keys, probe
from apps.ai_gateway.models import AccountStatus, AIProviderAccount
from apps.audit.services import Entity, Result, record
from core.tenancy.context import TenantContext
from core.tenancy.scope import require_scope


class AccountExists(Exception):  # noqa: N818 — un estado, no un fallo
    """La organización ya tiene una cuenta activa de ese proveedor: se rota su clave."""


class AccountNotFound(Exception):  # noqa: N818 — un estado, no un fallo
    """La organización no tiene una cuenta activa de ese proveedor."""


class InvalidModel(ValueError):  # noqa: N818 — nunca lleva el modelo pedido
    """El modelo no está en la lista permitida (`AI_MODELS`) de ese proveedor."""


def _allowed(ctx: TenantContext) -> None:
    ectx = execution_context(ctx)  # `AccessDenied` si no es un miembro activo
    if not has_permission(ectx, "ai.manage"):
        raise AccessDenied(Denied.PERMISSION)


def _audit(
    ctx: TenantContext,
    action: str,
    account: AIProviderAccount,
    changes: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    result: Result = Result.SUCCESS,
) -> None:
    """Solo el proveedor, el id, los estados y el código de la prueba: nunca la clave."""
    entity = Entity("ai_provider_account", account.pk, account.provider)
    record(ctx, action, entity, changes, {"provider": account.provider, **(metadata or {})},
           result=result)  # fmt: skip


def _active(alias: str, provider: str) -> AIProviderAccount:
    found = AIProviderAccount.objects.using(alias).select_for_update().filter(provider=provider)
    account: AIProviderAccount | None = found.filter(status=AccountStatus.ACTIVE).first()
    if account is None:
        raise AccountNotFound
    return account


def create(ctx: TenantContext, *, provider: str, api_key: str) -> AIProviderAccount:
    """Guarda la primera clave de `provider` (o la de una cuenta desactivada, que se reactiva).
    `AccountExists`, `keys.AccountConflict`, `keys.MalformedKey`, `ValueError`, `AccessDenied`."""
    alias = require_scope(ctx)
    _allowed(ctx)
    with transaction.atomic(using=alias):
        before = (
            AIProviderAccount.objects.using(alias).select_for_update().filter(provider=provider)
        )
        old: AIProviderAccount | None = before.first()
        if old is not None and old.status == AccountStatus.ACTIVE:
            raise AccountExists
        account = keys.store(ctx, provider, api_key)
        status = [old.status if old else None, account.status]
        _audit(ctx, "ai.account.created", account, {"status": status})
    return account


def rotate(ctx: TenantContext, *, provider: str, api_key: str) -> AIProviderAccount:
    """Sustituye la clave de la cuenta activa: la anterior se invalida y pierde su cifrado.
    `AccountNotFound`, `keys.MalformedKey`, `ValueError`, `AccessDenied`."""
    alias = require_scope(ctx)
    _allowed(ctx)
    with transaction.atomic(using=alias):
        _active(alias, provider)
        account = keys.store(ctx, provider, api_key)
        _audit(ctx, "ai.account.key_rotated", account)
    return account


def disable(ctx: TenantContext, *, provider: str) -> AIProviderAccount:
    """Desactiva la cuenta y borra el cifrado de su clave. `AccountNotFound`, `AccessDenied`."""
    alias = require_scope(ctx)
    _allowed(ctx)
    with transaction.atomic(using=alias):
        _active(alias, provider)
        keys.revoke(ctx, provider)
        account: AIProviderAccount = AIProviderAccount.objects.using(alias).get(provider=provider)
        _audit(ctx, "ai.account.disabled", account, {"status": ["ACTIVE", account.status]})
    return account


def change_model(ctx: TenantContext, *, provider: str, model: str) -> AIProviderAccount:
    """Elige el modelo de la cuenta activa entre los de la lista permitida de su proveedor y lo
    audita con el antes y el después (el mismo modelo no cambia nada ni se audita). Cambia cuánto
    gasta la organización: `ai.manage` antes de nada (ADR-034 §4). `InvalidModel`,
    `AccountNotFound`, `AccessDenied`."""
    alias = require_scope(ctx)
    _allowed(ctx)
    if model not in settings.AI_MODELS.get(provider, {}):
        raise InvalidModel
    with transaction.atomic(using=alias):
        account = _active(alias, provider)
        before = account.model
        if before != model:
            found = AIProviderAccount.objects.using(alias).filter(pk=account.pk)
            found.update(model=model, updated_at=timezone.now())
            account = found.get()
            _audit(ctx, "ai.account.model_changed", account, {"model": [before, model]})
    return account


def check(ctx: TenantContext, *, provider: str) -> probe.Probe:
    """Prueba la clave de la cuenta activa contra el proveedor y audita solo el código.
    `AccountNotFound`, `sealed.SealError` (la clave guardada no se abre), `AccessDenied`."""
    alias = require_scope(ctx)
    _allowed(ctx)
    account = AIProviderAccount.objects.using(alias).filter(provider=provider).first()
    if account is None or account.status != AccountStatus.ACTIVE:
        raise AccountNotFound
    result = probe.check(provider, keys.open_key(ctx, provider))  # la clave, solo en memoria
    outcome = Result.SUCCESS if result == probe.Probe.OK else Result.FAILED
    _audit(ctx, "ai.account.tested", account, None, {"result": str(result)}, outcome)
    return result

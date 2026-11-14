"""Selectores públicos de `accounts` (docs/architecture/module-dependencies.md §3). Solo leen."""

from uuid import UUID

from apps.accounts.models import MfaDevice


def has_second_factor(user_id: UUID) -> bool:
    """¿Tiene la cuenta un dispositivo confirmado (ADR-023 §1)? Un alta a medias no cuenta, y un
    secreto que no se abre sigue contando: se mira la fila, nunca el secreto (ADR-022 §4)."""
    devices = MfaDevice.objects.filter(user_id=user_id, confirmed_at__isnull=False)
    return devices.exists()

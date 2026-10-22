"""Frontera de alta de `accounts` (F2-06): la cuenta del Owner inicial de una organización.

Solo la importa `apps.provisioning` (contrato de import-linter). El alta de usuarios por
invitación llega con E01-06.
"""

from uuid import UUID

from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from apps.accounts.models import User


def owner_account(email: str, password: str | None) -> tuple[UUID, bool]:
    """La cuenta global del Owner, como `(id, creada)`. `email` ya es canónico.

    Sin contraseña: una cuenta que ya existe, activa y capaz de iniciar sesión. Con contraseña:
    una cuenta nueva; la de una cuenta existente nunca se cambia.
    """
    existing = User.objects.filter(email=email).first()
    if password is None:
        if existing is None:
            raise ValidationError("El usuario no existe: necesita contraseña", code="required")
        if not existing.is_active:
            raise ValidationError("El usuario está desactivado", code="inactive")
        if not existing.has_usable_password():
            raise ValidationError("El usuario no tiene contraseña utilizable", code="unusable")
        return existing.pk, False
    exists = ValidationError("El usuario ya existe: se indica como cuenta existente", code="exists")
    if existing is not None:
        raise exists
    validate_password(password, User(email=email))
    try:
        with transaction.atomic():  # savepoint: otra alta pudo crear la misma cuenta a la vez
            return User.objects.create_user(email=email, password=password).pk, True
    except IntegrityError:
        raise exists from None


def test_account(email: str, password: str | None) -> tuple[UUID, bool]:
    """Una cuenta de prueba, como `(id, creada)` (ADR-026 §3): la que ya existe, con su
    contraseña, o una nueva con `password`. `email` ya es canónico. Sin contraseña y sin cuenta,
    `ValidationError`; una cuenta desactivada no se reactiva.
    """
    if User.objects.filter(email=email).exists():
        return owner_account(email, None)
    return owner_account(email, password)


def account_exists(email: str) -> bool:
    return User.objects.filter(email=email).exists()

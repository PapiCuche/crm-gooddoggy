"""Identidad global de la persona (ADR-001 §2): platform-owned, sin `organization_id`.

Un usuario no pertenece a una organización ni tiene un rol: se vincula a organizaciones con
membresías y los roles se asignan a la membresía (F2-02, F2-04). `is_platform_staff` es
administración técnica de la plataforma y no da acceso a ningún tenant.
"""

from typing import Any

from django.contrib.auth.base_user import AbstractBaseUser, BaseUserManager
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models.functions import Lower

from apps.accounts.emails import canonical_email
from core.db.models import uuid7_primary_key


class UserManager(BaseUserManager["User"]):
    def get_by_natural_key(self, username: str | None) -> User:
        try:
            return self.get(email=canonical_email(username or ""))
        except ValidationError:
            raise self.model.DoesNotExist from None  # email imposible: no existe

    def _create(self, email: str, password: str | None, **extra: Any) -> User:
        if not email:
            raise ValueError("El email es obligatorio")
        user = self.model(email=email, **extra)
        user.set_password(password)  # None → contraseña inutilizable
        user.save(using=self._db)
        return user

    def create_user(self, email: str, password: str | None = None, **extra: Any) -> User:
        if extra.get("is_platform_staff"):
            raise ValueError("El staff de plataforma se crea con create_superuser")
        return self._create(email, password, **extra)

    def create_superuser(self, email: str, password: str | None = None, **extra: Any) -> User:
        """Staff de plataforma (comando `createsuperuser`). No es un rol de organización."""
        if not password:
            raise ValueError("El staff de plataforma necesita contraseña")
        if not extra.setdefault("is_platform_staff", True) or not extra.setdefault(
            "is_active", True
        ):
            raise ValueError("El staff de plataforma debe estar activo y ser is_platform_staff")
        return self._create(email, password, **extra)


class User(AbstractBaseUser):
    id = uuid7_primary_key()
    email = models.EmailField(max_length=254, unique=True)  # siempre canónico (emails.py)
    first_name = models.CharField(max_length=150, blank=True)
    last_name = models.CharField(max_length=150, blank=True)
    is_active = models.BooleanField(default=True)
    is_platform_staff = models.BooleanField(default=False)
    # Época de sesión: cada sesión guarda la que había al iniciarla; revocar la incrementa y las
    # sesiones anteriores dejan de valer (ADR-003 §2, D-F2-11).
    session_epoch = models.PositiveIntegerField(default=0, db_default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = UserManager()

    USERNAME_FIELD = "email"
    EMAIL_FIELD = "email"
    REQUIRED_FIELDS = []

    class Meta:
        db_table = "users"
        constraints = [  # con `unique`, garantiza en BD la unicidad sin distinguir mayúsculas
            models.CheckConstraint(
                condition=models.Q(email=Lower("email")), name="users_email_lowercase"
            )
        ]

    def __str__(self) -> str:
        return str(self.pk)  # nunca el email: es PII y acabaría en logs

    def clean(self) -> None:
        self.email = canonical_email(self.email)  # sustituye al NFKC de AbstractBaseUser

    def save(self, *args: Any, **kwargs: Any) -> None:
        self.email = canonical_email(self.email)
        if not (self._state.adding or args or kwargs.get("force_insert")):
            names = kwargs.get("update_fields")
            if names is None:  # como Django: los campos cargados, menos la clave
                loaded = (f for f in self._meta.concrete_fields if f.attname in self.__dict__)
                names = [f.name for f in loaded if not f.primary_key]
            # La época de sesión solo la mueve `revoke_sessions`: una instancia leída antes de
            # una revocación no la devuelve atrás (D-F2-11).
            kwargs["update_fields"] = [name for name in names if name != "session_epoch"]
        super().save(*args, **kwargs)


class LoginThrottle(models.Model):
    """Contador de intentos de acceso (F2-03B): platform-owned, compartido entre instancias.

    `key` es `id:<huella>`, `par:<huella>:<dirección>` o `ip:<dirección>`. La huella
    es la del email presentado (la de la auditoría de plataforma): el email nunca se
    guarda. `updated_at` es el último fallo (o cuándo empezó el contador); las filas las
    escribe `apps.accounts.throttle`.
    """

    id = uuid7_primary_key()
    key = models.CharField(max_length=128, unique=True)
    failures = models.PositiveIntegerField()
    blocked_until = models.DateTimeField(null=True)
    updated_at = models.DateTimeField()

    class Meta:
        db_table = "login_throttles"

    def __str__(self) -> str:
        return self.key.split(":", 1)[0]  # el tipo de clave, nunca la huella ni la IP

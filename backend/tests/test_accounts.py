"""F2-01: identidad global. Email canónico, unicidad en BD, Argon2id y fail-closed de tenant."""

from typing import Any
from uuid import UUID

import psycopg
import pytest
from django.conf import settings
from django.contrib.auth import authenticate, get_user_model
from django.contrib.auth.hashers import identify_hasher
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection
from django.test import Client, override_settings

from apps.accounts.emails import canonical_email
from apps.accounts.models import User
from config.settings import base
from tests.factories import TEST_PASSWORD, make_user, sign_in

pytestmark = pytest.mark.usefixtures("tenant_db")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("  Ana.Perez@Example.COM \n", "ana.perez@example.com"),
        ("ANA@EXAMPLE.COM", "ana@example.com"),
        ("ana@MAÑANA.com", "ana@xn--maana-pta.com"),  # dominio IDN → punycode
        ("ana@xn--maana-pta.com", "ana@xn--maana-pta.com"),
        ("a.n.a+ventas@gmail.com", "a.n.a+ventas@gmail.com"),  # sin reglas por proveedor
    ],
)
def test_canonical_email_policy(raw: str, expected: str) -> None:
    assert canonical_email(raw) == expected
    assert canonical_email(expected) == expected  # idempotente


@pytest.mark.parametrize(
    "raw",
    ["", "   ", "sin-arroba", "@example.com", "ana@", "a b@c.com", "josé@example.com", "a@b@c.com"],
)
def test_canonical_email_rejects_invalid_input(raw: str) -> None:
    with pytest.raises(ValidationError):
        canonical_email(raw)


def test_provider_aliases_are_different_identities() -> None:
    make_user(email="ana@gmail.com")
    make_user(email="a.na@gmail.com")
    make_user(email="ana+crm@gmail.com")
    assert User.objects.count() == 3


def test_create_user_stores_canonical_email_and_argon2id_hash() -> None:
    user = User.objects.create_user("  Ana@Example.COM ", TEST_PASSWORD, first_name="Ana")
    user.refresh_from_db()
    assert isinstance(user.pk, UUID) and user.pk.version == 7
    assert user.email == "ana@example.com"
    assert user.is_active and not user.is_platform_staff
    assert TEST_PASSWORD not in user.password
    assert user.password.startswith("argon2$argon2id$")
    assert identify_hasher(user.password).algorithm == "argon2"
    assert user.check_password(TEST_PASSWORD) and not user.check_password("otra-clave")
    assert "@" not in str(user)  # sin PII en logs

    user.set_password("nueva-clave-larga-123")
    user.save()
    user.refresh_from_db()
    assert user.check_password("nueva-clave-larga-123") and not user.check_password(TEST_PASSWORD)


def test_user_without_password_cannot_authenticate() -> None:
    user = User.objects.create_user("ana@example.com")
    assert not user.has_usable_password()
    assert authenticate(username="ana@example.com", password="") is None


@pytest.mark.parametrize("email", ["", None])
def test_email_is_required(email: Any) -> None:
    with pytest.raises(ValueError, match="obligatorio"):
        User.objects.create_user(email, TEST_PASSWORD)


@pytest.mark.parametrize("duplicate", ["ANA@example.com", " ana@EXAMPLE.com", "Ana@Example.Com"])
def test_email_is_unique_case_insensitively(duplicate: str) -> None:
    make_user(email="ana@example.com")
    with pytest.raises(IntegrityError):
        make_user(email=duplicate)


def test_saving_through_the_orm_always_canonicalizes() -> None:
    user = User(email="Ana@Example.com")
    user.set_password(TEST_PASSWORD)
    user.save()
    assert User.objects.get(pk=user.pk).email == "ana@example.com"
    user.email = "OTRA@Example.com"
    user.full_clean()
    assert user.email == "otra@example.com"


def test_database_rejects_non_lowercase_email_even_from_raw_sql(
    migrator: psycopg.Connection[Any],
) -> None:
    """La unicidad sin mayúsculas la garantiza la BD (CHECK + UNIQUE), no solo la aplicación."""
    insert = (
        "INSERT INTO users (email, password, first_name, last_name, is_active, "
        "is_platform_staff, created_at, updated_at) VALUES (%s, '!', '', '', true, false, "
        "now(), now())"
    )
    migrator.execute(insert, ["ana@example.com"])
    with pytest.raises(psycopg.errors.CheckViolation):
        migrator.execute(insert, ["Ana@example.com"])
    with pytest.raises(psycopg.errors.UniqueViolation):
        migrator.execute(insert, ["ana@example.com"])


def test_login_lookup_is_case_insensitive_and_inactive_users_cannot_authenticate() -> None:
    user = make_user(email="ana@example.com")
    assert User.objects.get_by_natural_key(" ANA@Example.com ") == user
    assert authenticate(username="ANA@EXAMPLE.COM", password=TEST_PASSWORD) == user
    assert authenticate(username="ana@example.com", password=TEST_PASSWORD[::-1]) is None
    with pytest.raises(User.DoesNotExist):
        User.objects.get_by_natural_key("no es un email")
    User.objects.filter(pk=user.pk).update(is_active=False)
    assert authenticate(username="ana@example.com", password=TEST_PASSWORD) is None


def test_create_superuser_is_platform_staff() -> None:
    staff = User.objects.create_superuser("Ops@Example.com", TEST_PASSWORD)
    assert staff.email == "ops@example.com"
    assert staff.is_platform_staff and staff.is_active and staff.check_password(TEST_PASSWORD)


@pytest.mark.parametrize(
    "kwargs",
    [{"is_platform_staff": False}, {"is_active": False}, {"password": None}, {"password": ""}],
)
def test_create_superuser_rejects_inconsistent_flags(kwargs: dict[str, Any]) -> None:
    arguments = {"email": "ops@example.com", "password": TEST_PASSWORD, **kwargs}
    with pytest.raises(ValueError, match="staff de plataforma"):
        User.objects.create_superuser(**arguments)
    assert not User.objects.exists()


def test_create_user_cannot_silently_create_platform_staff() -> None:
    with pytest.raises(ValueError, match="create_superuser"):
        User.objects.create_user("ops@example.com", TEST_PASSWORD, is_platform_staff=True)


def test_user_is_global_without_organization_or_role_fields() -> None:
    """ADR-001 §2: sin organización ni rol en `users`; los roles van en la membresía (F2-04)."""
    assert get_user_model() is User and settings.AUTH_USER_MODEL == "accounts.User"
    fields = {field.name for field in User._meta.get_fields()}
    assert fields == {
        "id",
        "email",
        "password",
        "first_name",
        "last_name",
        "is_active",
        "is_platform_staff",
        "session_epoch",  # época de sesión: global, del usuario (D-F2-11)
        "last_login",
        "created_at",
        "updated_at",
    }
    assert not User._meta.many_to_many  # sin grupos ni permisos de Django
    assert not hasattr(User, "is_superuser") and not hasattr(User, "username")


def test_users_table_is_platform_owned_in_the_database() -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT c.relrowsecurity, pg_get_userbyid(c.relowner), "
            "(SELECT count(*) FROM pg_attribute a WHERE a.attrelid = c.oid "
            " AND a.attname IN ('organization_id', 'role', 'role_id')) "
            "FROM pg_class c WHERE c.relname = 'users'"
        )
        assert cursor.fetchone() == (False, settings.DB_MIGRATOR_ROLE, 0)


def test_password_hashing_settings_use_only_argon2() -> None:
    assert settings.PASSWORD_HASHERS == ["django.contrib.auth.hashers.Argon2PasswordHasher"]
    assert {str(v["NAME"]).rsplit(".", 1)[1] for v in base.AUTH_PASSWORD_VALIDATORS} >= {
        "MinimumLengthValidator",
        "CommonPasswordValidator",
    }


@override_settings(
    MIDDLEWARE=base.MIDDLEWARE, TENANCY_MEMBERSHIP_RESOLVER=base.TENANCY_MEMBERSHIP_RESOLVER
)
@pytest.mark.parametrize("platform_staff", [False, True])
def test_authenticated_user_without_membership_cannot_reach_tenant_routes(
    orgs: dict[str, UUID], platform_staff: bool
) -> None:
    """Fail-closed: autenticarse, incluso como staff de plataforma, no da acceso a un tenant."""
    create = User.objects.create_superuser if platform_staff else User.objects.create_user
    user = create("ana@example.com", TEST_PASSWORD)
    client = Client()
    assert client.get("/api/v1/o/org-a/widgets/").status_code == 401  # anónimo
    sign_in(client, user)
    response = client.get("/api/v1/o/org-a/widgets/")
    assert (response.status_code, response.json()) == (404, {"code": "NOT_FOUND"})
    assert client.get("/api/v1/o/no-existe/widgets/").status_code == 404  # indistinguible

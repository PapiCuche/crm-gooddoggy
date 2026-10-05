"""BD de test (ADR-002 §1.1, tenancy-context §8).

- Las migraciones se aplican como `crm_migrator` (DATABASE_MIGRATOR_URL).
- Los tests se ejecutan como el rol de DATABASE_URL, que debe ser `crm_app`: si es
  superusuario o tiene BYPASSRLS, la sesión se aborta (si no, RLS no se estaría probando).
- No se crea una BD `test_*` (crm_app no tiene CREATEDB): se usa la BD configurada.
"""

from collections.abc import Iterator
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from django.conf import settings
from django.core.management import call_command
from django.db import connection
from psycopg.conninfo import make_conninfo
from pytest_django import DjangoDbBlocker

from config.env import database

TENANT_TABLES: tuple[str, ...] = ("tenancy_app_widgetpart", "tenancy_app_widget", "org_sequences")
TENANT_TABLES += ("outbox_events", "audit_logs")  # F1-06
TENANT_TABLES += ("files",)  # F1-08
TENANT_TABLES += ("organization_memberships",)  # F2-02
# F2-04: delante, porque referencian a las membresías y la limpieza borra en este orden.
TENANT_TABLES = ("membership_roles", "role_permissions", "roles", *TENANT_TABLES)
TENANT_TABLES += ("branches",)  # F2-43
# F2-52: delante, porque referencia a los equipos y a las membresías.
TENANT_TABLES = ("team_members", *TENANT_TABLES, "teams")  # F2-50, F2-52
PLATFORM_AUDIT = "platform_audit_logs"  # F2-10: platform-owned, la limpia el migrador


def migrator_settings() -> dict[str, Any]:
    if not settings.MIGRATOR_DATABASE_URL:
        pytest.exit("DATABASE_MIGRATOR_URL es obligatoria para los tests de BD", returncode=1)
    return database(settings.MIGRATOR_DATABASE_URL)


def migrator_conninfo() -> str:
    db = migrator_settings()
    return make_conninfo(
        dbname=db["NAME"],
        user=db["USER"],
        password=db["PASSWORD"],
        host=db["HOST"],
        port=db["PORT"] or None,
    )


@pytest.fixture(scope="session")
def django_db_setup(django_db_blocker: DjangoDbBlocker) -> None:
    with django_db_blocker.unblock():
        runtime = dict(connection.settings_dict)
        connection.close()
        connection.settings_dict.update(
            {
                k: v
                for k, v in migrator_settings().items()
                if k in ("NAME", "USER", "PASSWORD", "HOST", "PORT")
            }
        )
        try:
            call_command("migrate", verbosity=0)
        finally:
            connection.close()
            connection.settings_dict.clear()
            connection.settings_dict.update(runtime)
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user"
            )
            rolsuper, rolbypassrls = cursor.fetchone()
        if rolsuper or rolbypassrls:
            pytest.exit("El rol de test no puede ser superusuario ni tener BYPASSRLS", returncode=1)


@pytest.fixture
def migrator() -> Iterator[psycopg.Connection[Any]]:
    """Conexión del migrador (propietario, BYPASSRLS): solo para sembrar y limpiar."""
    with psycopg.connect(migrator_conninfo(), autocommit=True) as conn:
        yield conn


@pytest.fixture
def tenant_db(
    django_db_setup: None, django_db_blocker: DjangoDbBlocker, migrator: psycopg.Connection[Any]
) -> Iterator[None]:
    """Acceso a BD como crm_app SIN transacción envolvente: tenant_scope es la transacción real."""
    with django_db_blocker.unblock():
        try:
            yield
        finally:
            connection.close()
            for table in (
                *TENANT_TABLES,
                "organizations",
                "django_session",
                "login_throttles",
                "users",
                PLATFORM_AUDIT,
            ):
                migrator.execute(f"DELETE FROM {table}")  # noqa: S608 — nombres fijos


@pytest.fixture
def orgs(tenant_db: None, migrator: psycopg.Connection[Any]) -> dict[str, UUID]:
    """Dos organizaciones (`org-a`, `org-b`) con un widget cada una (sembradas por el migrador)."""
    ids = {"A": uuid4(), "B": uuid4(), "widget_A": uuid4(), "widget_B": uuid4()}
    for org in ("A", "B"):
        migrator.execute(
            "INSERT INTO organizations (id, slug, name, status, created_at) "
            "VALUES (%s, %s, %s, 'ACTIVE', now())",
            [ids[org], f"org-{org.lower()}", f"Org {org}"],
        )
        migrator.execute(
            "INSERT INTO tenancy_app_widget (id, organization_id, name) VALUES (%s, %s, %s)",
            [ids[f"widget_{org}"], ids[org], f"widget {org}"],
        )
    return ids

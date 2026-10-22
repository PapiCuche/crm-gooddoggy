"""F2-116 (ADR-026): la organización de pruebas y sus cuatro cuentas, con su comando."""

import io
from typing import Any

import psycopg
import pytest
from django.contrib.auth import authenticate
from django.core.exceptions import PermissionDenied
from django.core.management import call_command
from django.core.management.base import CommandError

from apps.access.bootstrap import grant_template_role
from apps.access.catalog import ROLE_TEMPLATES
from apps.access.selectors import AccessDenied, execution_context
from apps.organizations.bootstrap import add_test_membership
from apps.provisioning import test_accounts
from config.settings import base
from core.tenancy.context import TenantContext
from core.tenancy.resolution import organization_by_slug, resolve_tenant
from core.tenancy.scope import tenant_scope
from tests.factories import TEST_PASSWORD
from tests.test_bootstrap import TABLES, bootstrap, counts, platform_rows
from tests.test_bootstrap_command import stdin

pytestmark = pytest.mark.usefixtures("tenant_db")
SLUG = "pruebas"
REASON = ["--reason", "pruebas locales"]
EMAILS = [
    "owner@pruebas.gooddoggy.test",
    "administrador@pruebas.gooddoggy.test",
    "supervisor@pruebas.gooddoggy.test",
    "vendedor@pruebas.gooddoggy.test",
]
OTHER, WEAK = "otra-clave-larga-de-prueba-9", "123"  # noqa: S105
TEMPLATE = {template.code: template for template in ROLE_TEMPLATES}


@pytest.fixture(autouse=True)
def real_resolver(settings: Any) -> None:
    settings.TENANCY_MEMBERSHIP_RESOLVER = base.TENANCY_MEMBERSHIP_RESOLVER  # el resolvedor real


@pytest.fixture
def enabled(settings: Any) -> None:
    settings.TEST_ACCOUNTS_ORGANIZATION = SLUG


def provision(monkeypatch: pytest.MonkeyPatch, password: str = TEST_PASSWORD) -> str:
    monkeypatch.setattr("sys.stdin", stdin(password + "\n"))
    out = io.StringIO()
    call_command("provision_test_accounts", "--password-stdin", *REASON, stdout=out)
    return out.getvalue()


def grants(email: str) -> set[str]:
    """Los permisos efectivos de esa cuenta en la organización de pruebas."""
    from apps.accounts.models import User  # noqa: PLC0415 — solo para el test

    user = User.objects.get(email=email)
    ctx = resolve_tenant(SLUG, user, "test")
    with tenant_scope(ctx):
        return set(execution_context(ctx).permissions)


def test_disabled_by_default_refuses_and_touches_nothing(
    migrator: psycopg.Connection[Any], settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert settings.TEST_ACCOUNTS_ORGANIZATION == ""  # el valor por defecto
    before = counts(migrator)
    with pytest.raises(CommandError, match="desactivados"):
        provision(monkeypatch)
    assert counts(migrator) == before and platform_rows(migrator) == []


@pytest.mark.usefixtures("enabled")
def test_it_provisions_four_real_profiles_once_and_is_idempotent(
    migrator: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    out = provision(monkeypatch)
    assert TEST_PASSWORD not in out and "creada" in out
    assert all(email in out for email in EMAILS)
    after = counts(migrator)
    assert after["users"] == 4 and after["organization_memberships"] == 4
    for email in EMAILS:
        assert authenticate(username=email, password=TEST_PASSWORD) is not None
    # Cada perfil tiene exactamente los permisos de su plantilla: ni más ni menos.
    for email, (_, code) in zip(EMAILS, test_accounts.PROFILES, strict=True):
        assert grants(email) == set(TEMPLATE[code].grants), email
    rows = platform_rows(migrator)
    assert [row[0] for row in rows].count("test_accounts.provisioned") == 1
    assert TEST_PASSWORD not in str(rows)
    activated = migrator.execute(
        "SELECT count(*) FROM audit_logs WHERE action = 'membership.activated'"
    ).fetchone()
    assert activated == (3,)  # el Owner llega por el alta de la organización

    # Otra vez, con otra contraseña: nada cambia y ninguna cuenta la toma.
    again = provision(monkeypatch, password=OTHER)
    assert "ya existía" in again and "Cuentas nuevas: 0 de 0" in again
    assert counts(migrator) == after
    assert authenticate(username=EMAILS[3], password=TEST_PASSWORD) is not None


@pytest.mark.usefixtures("enabled")
def test_it_recreates_only_what_is_missing(
    migrator: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    provision(monkeypatch)
    migrator.execute(
        "DELETE FROM membership_roles WHERE membership_id IN (SELECT m.id FROM "
        "organization_memberships m JOIN users u ON u.id = m.user_id WHERE u.email = %s)",
        [EMAILS[2]],
    )
    out = provision(monkeypatch)
    assert "roles añadidos: 1" in out and "membresías añadidas: 0" in out
    assert grants(EMAILS[2]) == set(TEMPLATE["supervisor"].grants)


@pytest.mark.usefixtures("enabled")
def test_it_never_touches_an_organization_that_is_not_the_test_one(
    migrator: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    bootstrap(slug=SLUG)  # una organización real que se llama como la de pruebas
    before, audit_before = counts(migrator), len(platform_rows(migrator))
    with pytest.raises(CommandError, match="no es la de pruebas"):
        provision(monkeypatch)
    assert counts(migrator) == before
    rows = platform_rows(migrator)[audit_before:]
    assert [(row[0], row[1]) for row in rows] == [
        ("test_accounts.provision.started", "SUCCESS"),
        ("test_accounts.provisioned", "FAILED"),
    ]


def test_the_frontier_refuses_any_other_organization_or_a_user_actor(
    settings: Any,
) -> None:
    made = bootstrap()  # «acme», que no es la de pruebas
    settings.TEST_ACCOUNTS_ORGANIZATION = SLUG
    system = TenantContext(made.organization_id, "command")
    with tenant_scope(system), pytest.raises(PermissionDenied):
        add_test_membership(system, user_id=made.owner_user_id)
    assert organization_by_slug("acme") is not None
    ana = authenticate(username="ana@acme.pe", password=TEST_PASSWORD)
    user_ctx = resolve_tenant("acme", ana, "test")
    with tenant_scope(user_ctx), pytest.raises(AccessDenied):
        grant_template_role(user_ctx, membership_id=made.membership_id, template="admin")


@pytest.mark.usefixtures("enabled")
def test_a_weak_password_creates_nothing(
    migrator: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    before = counts(migrator)
    with pytest.raises(CommandError):
        provision(monkeypatch, password=WEAK)
    assert counts(migrator) == before
    assert set(TABLES) <= set(before)

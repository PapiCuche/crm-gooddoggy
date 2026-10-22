"""F2-117 (ADR-026 §5): la ruta pública que lista las cuentas de prueba que se pueden usar."""

from typing import Any

import psycopg
import pytest
from django.test import Client

from apps.provisioning import test_accounts
from tests.factories import TEST_PASSWORD
from tests.test_bootstrap import bootstrap

pytestmark = pytest.mark.usefixtures("tenant_db")
URL = "/api/v1/auth/test-accounts/"
SLUG = "pruebas"
ROLES = ["Owner", "Administrador", "Supervisor", "Vendedor"]


@pytest.fixture
def enabled(settings: Any) -> None:
    settings.TEST_ACCOUNTS_ORGANIZATION = SLUG


def provision() -> None:
    test_accounts.provision(password=TEST_PASSWORD, operator="dev", reason="pruebas")


def listed(client: Client | None = None) -> list[dict[str, str]]:
    response = (client or Client()).get(URL)
    assert response.status_code == 200
    accounts: list[dict[str, str]] = response.json()["accounts"]
    return accounts


def test_disabled_a_get_is_a_404_like_any_unknown_route() -> None:
    response = Client().get(URL)
    unknown = Client().get("/api/v1/auth/no-existe/")
    assert response.status_code == unknown.status_code == 404
    assert response.json()["code"] == "NOT_FOUND"


@pytest.mark.usefixtures("enabled")
def test_without_provisioning_it_lists_nothing() -> None:
    assert listed() == []


@pytest.mark.usefixtures("enabled")
def test_it_lists_the_four_profiles_with_their_role_and_nothing_else() -> None:
    provision()
    client = Client(enforce_csrf_checks=True)  # sin sesión ni token: es pública y solo lee
    accounts = listed(client)
    assert accounts == [
        {"email": test_accounts.email_for(SLUG, local), "role": role}
        for (local, _), role in zip(test_accounts.PROFILES, ROLES, strict=True)
    ]
    assert TEST_PASSWORD not in str(accounts)
    assert "crm_session" not in client.cookies  # no abre sesión


@pytest.mark.usefixtures("enabled")
def test_it_hides_what_cannot_be_used_now(migrator: psycopg.Connection[Any]) -> None:
    provision()
    seller, supervisor, admin = (
        test_accounts.email_for(SLUG, local)
        for local in ("vendedor", "supervisor", "administrador")
    )
    migrator.execute("UPDATE users SET is_active = false WHERE email = %s", [seller])
    migrator.execute(
        "UPDATE organization_memberships SET status = 'SUSPENDED' WHERE user_id = "
        "(SELECT id FROM users WHERE email = %s)",
        [supervisor],
    )
    migrator.execute(
        "DELETE FROM membership_roles WHERE membership_id = (SELECT m.id FROM "
        "organization_memberships m JOIN users u ON u.id = m.user_id WHERE u.email = %s)",
        [admin],
    )
    assert [account["role"] for account in listed()] == ["Owner"]


@pytest.mark.usefixtures("enabled")
def test_it_hides_an_account_without_a_usable_password(migrator: psycopg.Connection[Any]) -> None:
    provision()
    seller = test_accounts.email_for(SLUG, "vendedor")
    migrator.execute("UPDATE users SET password = '!' WHERE email = %s", [seller])
    assert [account["role"] for account in listed()] == ROLES[:3]


@pytest.mark.usefixtures("enabled")
def test_it_lists_nobody_when_the_owner_is_not_the_synthetic_account(
    migrator: psycopg.Connection[Any],
) -> None:
    provision()  # las otras tres siguen ahí, activas y con su rol: solo cambia quién es el Owner
    owner = test_accounts.email_for(SLUG, "owner")
    migrator.execute("UPDATE users SET email = 'owner@acme.pe' WHERE email = %s", [owner])
    assert listed() == []


def test_it_never_lists_a_real_organization_with_the_test_slug(settings: Any) -> None:
    bootstrap(slug=SLUG)  # una organización real que se llama como la de pruebas
    settings.TEST_ACCOUNTS_ORGANIZATION = SLUG
    assert listed() == []


@pytest.mark.usefixtures("enabled")
def test_a_suspended_test_organization_lists_nobody(migrator: psycopg.Connection[Any]) -> None:
    provision()
    migrator.execute("UPDATE organizations SET status = 'SUSPENDED' WHERE slug = %s", [SLUG])
    assert listed() == []  # nadie podría entrar en ella

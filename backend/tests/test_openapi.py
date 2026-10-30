"""Contrato OpenAPI (F1-08A): servido en /api/schema/ y versionado sin drift."""

from pathlib import Path

import yaml
from django.core.management import call_command
from django.test import Client

COMMITTED = Path(__file__).resolve().parents[1] / "openapi" / "schema.yaml"


def generate(path: Path) -> str:
    call_command("spectacular", "--validate", "--fail-on-warn", "--file", str(path))
    return path.read_text()


def test_committed_schema_matches_the_generated_one_deterministically(tmp_path: Path) -> None:
    first, second = generate(tmp_path / "a.yaml"), generate(tmp_path / "b.yaml")
    assert first == second  # determinista
    assert first == COMMITTED.read_text(), "Regenerar: ver backend/README.md (Contrato OpenAPI)"


def test_schema_endpoint_serves_the_contract_without_internal_routes() -> None:
    response = Client().get("/api/schema/")
    assert response.status_code == 200
    schema = yaml.safe_load(response.content)
    assert schema["info"]["title"] == "Good Doggy CRM API"
    # Solo el contrato: ni health, ni el propio schema.
    assert schema["openapi"].startswith("3.") and set(schema["paths"]) == {
        "/api/v1/auth/csrf/",
        "/api/v1/auth/login/",
        "/api/v1/auth/login/mfa/",
        "/api/v1/auth/password-reset/",
        "/api/v1/auth/password-reset/confirm/",
        "/api/v1/auth/logout/",
        "/api/v1/auth/mfa/",
        "/api/v1/auth/mfa/backup-codes/",
        "/api/v1/auth/mfa/disable/",
        "/api/v1/auth/mfa/step-up/",
        "/api/v1/auth/mfa/totp/",
        "/api/v1/auth/mfa/totp/confirm/",
        "/api/v1/auth/session/",
        "/api/v1/auth/test-accounts/",
        "/api/v1/me/organizations/",
        "/api/v1/o/{org_slug}/audit/",
        "/api/v1/o/{org_slug}/brands/",
        "/api/v1/o/{org_slug}/brands/{brand_id}/",
        "/api/v1/o/{org_slug}/categories/",
        "/api/v1/o/{org_slug}/categories/{category_id}/",
        "/api/v1/o/{org_slug}/contacts/",  # F4-02
        "/api/v1/o/{org_slug}/contacts/{contact_id}/",
        "/api/v1/o/{org_slug}/contacts/{contact_id}/assignment/",  # F4-08
        "/api/v1/o/{org_slug}/contacts/{contact_id}/notes/",
        "/api/v1/o/{org_slug}/contacts/{contact_id}/notes/{note_id}/",
        "/api/v1/o/{org_slug}/contacts/{contact_id}/tags/{tag_id}/",  # F4-11b
        "/api/v1/o/{org_slug}/tags/",  # F4-11
        "/api/v1/o/{org_slug}/tags/{tag_id}/",
        "/api/v1/o/{org_slug}/products/",
        "/api/v1/o/{org_slug}/products/{product_id}/",
        "/api/v1/o/{org_slug}/products/{product_id}/variants/",
        "/api/v1/o/{org_slug}/products/{product_id}/variants/{variant_id}/",
        "/api/v1/o/{org_slug}/branches/",
        "/api/v1/o/{org_slug}/branches/{branch_id}/",
        "/api/v1/o/{org_slug}/inventory/imports/",  # F3-20b
        "/api/v1/o/{org_slug}/inventory/imports/template/",
        "/api/v1/o/{org_slug}/inventory/imports/{import_id}/apply/",  # F3-21
        "/api/v1/o/{org_slug}/inventory/imports/{import_id}/errors/",
        "/api/v1/o/{org_slug}/inventory/imports/{import_id}/rows/",
        "/api/v1/o/{org_slug}/inventory/imports/{import_id}/revert/",  # F3-24
        "/api/v1/o/{org_slug}/inventory/levels/",
        "/api/v1/o/{org_slug}/inventory/movements/",
        "/api/v1/o/{org_slug}/invitations/",
        "/api/v1/o/{org_slug}/invitations/{invitation_id}/status/",
        "/api/v1/o/{org_slug}/me/",
        "/api/v1/o/{org_slug}/members/",
        "/api/v1/o/{org_slug}/members/{membership_id}/roles/{role_id}/",
        "/api/v1/o/{org_slug}/members/{membership_id}/branch/",
        "/api/v1/o/{org_slug}/members/{membership_id}/status/",
        "/api/v1/o/{org_slug}/permissions/",
        "/api/v1/o/{org_slug}/pricing/price-lists/",  # F3-40b
        "/api/v1/o/{org_slug}/pricing/price-lists/{price_list_id}/",
        "/api/v1/o/{org_slug}/pricing/price-lists/{price_list_id}/prices/",  # F3-44
        "/api/v1/o/{org_slug}/pricing/variants/{variant_id}/price-history/",  # F3-44b
        "/api/v1/o/{org_slug}/pricing/promotions/",  # F3-48d
        "/api/v1/o/{org_slug}/pricing/promotions/{promotion_id}/",
        "/api/v1/o/{org_slug}/roles/",
        "/api/v1/o/{org_slug}/roles/{role_id}/",
        "/api/v1/o/{org_slug}/roles/{role_id}/permissions/{code}/",
        "/api/v1/o/{org_slug}/teams/",
        "/api/v1/o/{org_slug}/teams/{team_id}/",
        "/api/v1/o/{org_slug}/teams/{team_id}/members/",
        "/api/v1/o/{org_slug}/teams/{team_id}/members/{membership_id}/",
        "/api/v1/o/{org_slug}/warehouses/",
        "/api/v1/o/{org_slug}/warehouses/{warehouse_id}/",
    }
    login = schema["paths"]["/api/v1/auth/login/"]["post"]
    assert login["operationId"] == "auth_login" and "security" not in login  # sin sesión previa
    error = {"$ref": "#/components/schemas/Error"}
    assert login["responses"]["401"]["content"]["application/json"]["schema"] == error
    assert schema["paths"]["/api/v1/auth/session/"]["get"]["security"] == [{"sessionCookie": []}]
    parts = schema["components"]["schemas"]
    assert set(parts["User"]["properties"]) == {"id", "email", "first_name", "last_name"}
    assert parts["LoginRequest"]["properties"]["password"]["writeOnly"] is True

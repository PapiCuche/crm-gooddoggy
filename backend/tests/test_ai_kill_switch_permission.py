"""F8-06a (#981): el permiso `ai.kill_switch` (ADR-034 §10; decisión D-F8-08 en #769)
y el marcador `AnyOf` del motor de autorización: basta uno de sus permisos; una tupla normal, y
`AnyOf` en cualquier otro sitio, siguen exigiéndolos todos."""

from types import SimpleNamespace
from typing import Any

import psycopg
import pytest
from rest_framework.exceptions import NotFound

from apps.access import permissions
from apps.access.apps import extend_owner_roles
from apps.access.catalog import BY_CODE, ROLE_TEMPLATES
from apps.access.permissions import AnyOf, HasPermission
from tests.test_access import as_migrator
from tests.test_contacts_api import crm

__all__ = ["crm"]  # el fixture, para pytest
pytestmark = pytest.mark.usefixtures("tenant_db")
GRANTS = (
    "SELECT r.code FROM role_permissions g JOIN roles r ON r.id = g.role_id"
    " WHERE g.permission_code = 'ai.kill_switch' AND r.organization_id = %s ORDER BY 1"
)


def test_born_with_owner_admin_and_supervisor_and_existing_organizations_only_owner(
    crm: SimpleNamespace, migrator: psycopg.Connection[Any]
) -> None:
    """Una organización de antes no lo tiene en ningún rol: el despliegue se lo da solo al Owner,
    nunca al Administrador ni al Supervisor que ya existen (ADR-018 §3)."""
    assert BY_CODE["ai.kill_switch"].is_sensitive and not BY_CODE["ai.kill_switch"].supports_scope
    born = {template.code for template in ROLE_TEMPLATES if "ai.kill_switch" in template.grants}
    assert born == {"owner", "admin", "supervisor"}
    assert [c for (c,) in migrator.execute(GRANTS, [crm.a]).fetchall()] == sorted(born)
    migrator.execute("DELETE FROM role_permissions WHERE permission_code = 'ai.kill_switch'")
    assert as_migrator(lambda: extend_owner_roles("default")) == 1
    assert migrator.execute(GRANTS, [crm.a]).fetchall() == [("owner",)]


TWO = ("a.x", "b.y")  # basta uno con `AnyOf`; una tupla los sigue exigiendo todos; vacío, nada


@pytest.mark.parametrize(("declared", "held", "passes"), [
    (AnyOf(TWO), {"a.x"}, True), (AnyOf(TWO), {"b.y"}, True), (AnyOf(TWO), set(), False),
    (AnyOf(()), {"a.x"}, False), (TWO, {"a.x"}, False), (TWO, set(TWO), True),
    ("a.x", {"a.x"}, True),
])  # fmt: skip
def test_any_of_needs_one_code_and_a_plain_tuple_still_needs_all(
    monkeypatch: pytest.MonkeyPatch, declared: Any, held: set[str], passes: bool
) -> None:
    monkeypatch.setattr(permissions, "request_context", lambda request: object())
    monkeypatch.setattr(permissions, "has_permission", lambda ectx, code: code in held)
    monkeypatch.setattr(permissions, "can", lambda ectx, code, obj: code in held)
    view, request = SimpleNamespace(required_permissions={"GET": declared}), SimpleNamespace(
        method="GET")  # fmt: skip
    assert HasPermission().has_permission(request, view) is passes
    if isinstance(declared, AnyOf) and declared and 0 < len(held) < len(declared):
        with pytest.raises(NotFound):  # en el objeto, `AnyOf` es una tupla: los exige todos
            HasPermission().has_object_permission(request, view, object())  # type: ignore[arg-type]

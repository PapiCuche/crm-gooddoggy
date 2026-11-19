"""F8-06a (#981): el permiso `ai.kill_switch` (ADR-034 §10; decisión D-F8-08 en #769)
y el marcador `AnyOf` del motor de autorización: basta uno de sus permisos; una tupla normal, y
`AnyOf` en cualquier otro sitio, siguen exigiéndolos todos."""

from types import SimpleNamespace
from typing import Any

import psycopg
import pytest
from rest_framework.exceptions import NotFound

from apps.access import permissions, services
from apps.access.apps import extend_owner_roles
from apps.access.catalog import ADMIN_TEMPLATE, BY_CODE, ROLE_TEMPLATES
from apps.access.models import MembershipRole, Role
from apps.access.permissions import AnyOf, HasPermission, ScopeFilter
from apps.access.selectors import Denied
from core.tenancy.scope import tenant_scope
from tests.factories import make_user
from tests.test_access import as_migrator
from tests.test_anti_escalation import denied
from tests.test_contacts_api import crm
from tests.test_memberships import ctx, join

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


def test_only_an_owner_manages_invites_or_moves_a_new_supervisor(crm: SimpleNamespace) -> None:
    """D-F8-08, confirmado con todos sus efectos (comentario en #769, 2026-11-19): el Supervisor
    nuevo lleva un sensible, así que un Administrador que no es Owner no lo suspende, reactiva,
    cambia de sucursal, invita ni revoca su invitación (`SENSITIVE`); el Owner, sí."""
    ada = make_user(email="ada@example.com")
    with tenant_scope(ctx(crm.a)):
        roles = {r.code: r.pk for r in Role.objects.filter(code__in=(ADMIN_TEMPLATE, "supervisor"))}
        MembershipRole.objects.create(membership_id=join(crm.a, ada).pk, role_id=roles["admin"])
    sam, supervisor = {"membership_id": crm.m["sam"]}, {"role_ids": [roles["supervisor"]]}
    for service, kwargs in (
        (services.ensure_can_manage_member, sam | {"leaving": True}),
        (services.ensure_can_manage_member, sam | {"leaving": False}),
        (services.ensure_can_move_member, sam | {"branch_id": None}),
        (services.ensure_can_invite, supervisor),
        (services.ensure_can_revoke_invitation, supervisor),
    ):  # fmt: skip
        assert denied(crm.a, ada, service, **kwargs) is Denied.SENSITIVE, service
        with tenant_scope(ctx(crm.a, crm.ana)) as tenant:
            service(tenant, **kwargs)  # el Owner, sí


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
    scoped: list[str] = []  # el filtro acota por cada código, también con `AnyOf`

    def scope(ectx: Any, code: str, rows: Any) -> Any:
        scoped.append(code)
        return rows

    monkeypatch.setattr(permissions, "scoped", scope)
    monkeypatch.setattr(permissions, "hidden", lambda request, view, e, rows: rows)
    ScopeFilter().filter_queryset(request, SimpleNamespace(none=list), view)  # type: ignore[arg-type]
    assert scoped == list((declared,) if isinstance(declared, str) else declared)
    if passes:  # y el segundo factor, si la vista lo pide, también con `AnyOf`
        view.step_up_methods = {"GET"}
        monkeypatch.setattr(permissions, "require_recent_mfa", refuse)
        with pytest.raises(PermissionError):
            HasPermission().has_permission(request, view)
    if isinstance(declared, AnyOf) and declared and 0 < len(held) < len(declared):
        with pytest.raises(NotFound):  # en el objeto, `AnyOf` es una tupla: los exige todos
            HasPermission().has_object_permission(request, view, object())  # type: ignore[arg-type]


def refuse(request: Any) -> None:
    raise PermissionError

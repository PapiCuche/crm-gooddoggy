"""F2-22: `GET /api/v1/o/{slug}/roles/`, el directorio de roles de una organización.
Middleware, sesión, motor de autorización y PostgreSQL con el rol `crm_app`."""

from typing import Any

import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

from apps.access import directory
from apps.access.catalog import BY_CODE, PERMISSIONS
from apps.access.models import MembershipRole, Role
from core.tenancy.context import TenantContextError
from core.tenancy.scope import tenant_scope
from tests import test_anti_escalation, test_authorization, test_self_context
from tests.factories import make_user
from tests.test_anti_escalation import EDIT
from tests.test_authorization import VIEW, acting, give
from tests.test_memberships import ctx, join
from tests.test_self_context import NOT_FOUND, reply, signed

world, rbac = test_authorization.world, test_anti_escalation.rbac
real_stack = test_self_context.real_stack
pytestmark = pytest.mark.usefixtures("tenant_db")
DENIED = (403, b'{"code":"PERMISSION_DENIED"}')


def roles(client: Client, org: str = "org-a", **query: Any) -> Any:
    return client.get(f"/api/v1/o/{org}/roles/", query)


def test_it_lists_every_role_with_what_it_grants_and_how_many_hold_it(
    rbac: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    custom = give(rbac.a, rbac.m_eva, {VIEW: "BRANCH", "roles.view": None, EDIT: "OWN"}, code="x")
    with tenant_scope(ctx(rbac.a)):
        Role.objects.filter(pk=custom.pk).update(name="Lectura", description="Solo mira")
        MembershipRole.objects.create(membership_id=rbac.m_luis, role=custom)
        made = list(Role.objects.order_by("id").values_list("code", flat=True))
    others = [join(rbac.a, make_user(), s) for s in ("INVITED", "SUSPENDED", "DEACTIVATED")]
    others.append(join(rbac.a, make_user(is_active=False)))  # miembros que hoy no pueden entrar
    with tenant_scope(ctx(rbac.a)):
        for other in others:
            MembershipRole.objects.create(membership_id=other.pk, role=custom)
    give(rbac.b, join(rbac.b, make_user()).pk, {"users.view": None}, code="de-b")  # otra: no sale
    monkeypatch.delitem(BY_CODE, EDIT)  # un código que el catálogo ya no tiene: se sigue viendo
    body = roles(signed(rbac.ana)).json()
    assert body["next"] is None and [row["code"] for row in body["results"]] == made
    by_code = {row["code"]: row for row in body["results"]}
    assert by_code["x"] == {
        "id": str(custom.pk),
        "code": "x",
        "name": "Lectura",
        "description": "Solo mira",
        "is_system": False,
        "permissions": [  # por código; el alcance, nulo donde el permiso no lo admite
            {"code": "roles.view", "scope": None},
            {"code": EDIT, "scope": "OWN"},
            {"code": VIEW, "scope": "BRANCH"},
        ],
        "members": 6,  # en cualquier estado, y con la cuenta desactivada
    }
    owner = by_code["owner"]
    assert (owner["is_system"], owner["members"]) == (True, 1)
    assert [grant["code"] for grant in owner["permissions"]] == sorted(p.code for p in PERMISSIONS)
    assert by_code["target"] == {  # sin descripción, sin concesiones y sin miembros
        "id": str(rbac.target.pk),
        "code": "target",
        "name": "Rol destino",
        "description": "",
        "is_system": False,
        "permissions": [],
        "members": 0,
    }
    system = {code: by_code[code]["is_system"] for code in made}  # de plantilla, no «es Owner»
    assert [code for code in made if system[code]] == ["owner", "admin", "supervisor", "seller"]
    assert "is_owner_role" not in str(body) and "de-b" not in str(body)  # ni la marca de Owner


def test_without_a_session_membership_or_permission_it_reveals_nothing(rbac: Any) -> None:
    assert roles(Client()).status_code == 401
    give(rbac.a, rbac.m_eva, {"users.view": None, "users.manage": None})  # no basta
    eva = signed(rbac.eva)
    assert reply(roles(eva)) == DENIED
    give(rbac.a, rbac.m_eva, {"roles.view": None})
    assert roles(eva).status_code == 200
    for org in ("org-b", "no-existe"):  # eva no es de B: indistinguible de que no exista
        assert reply(roles(eva, org)) == NOT_FOUND
    assert reply(roles(signed(make_user(email="fuera@example.com")))) == NOT_FOUND
    eva.cookies["csrftoken"] = token = "t" * 32  # con token responde la vista, no el CSRF
    for write in (eva.post, eva.put, eva.patch, eva.delete):  # solo lectura
        denied = write("/api/v1/o/org-a/roles/", headers={"X-CSRFToken": token})
        assert reply(denied) == DENIED


def test_it_pages_by_cursor_without_a_query_per_role(rbac: Any) -> None:
    client = signed(rbac.ana)

    def queries() -> int:
        with CaptureQueriesContext(connection) as captured:
            assert roles(client).status_code == 200
        return len(captured)

    few, before = queries(), len(roles(client).json()["results"])
    for index in range(5):
        give(rbac.a, rbac.m_eva, {"users.view": None}, code=f"extra{index}")
    assert queries() == few  # roles, concesiones y miembros: una consulta cada uno
    first = roles(client, limit=2).json()
    assert len(first["results"]) == 2 and first["next"]
    rest = roles(client, limit=200, cursor=first["next"]).json()
    codes = [row["code"] for row in first["results"] + rest["results"]]
    assert len(codes) == len(set(codes)) == before + 5 and rest["next"] is None
    assert roles(client, limit=201).json()["code"] == "VALIDATION_ERROR"


def test_a_stale_context_fails_in_every_reader(rbac: Any) -> None:
    tenant = ctx(rbac.a, rbac.ana)
    with acting(rbac.a, rbac.ana) as ectx:
        assert directory.roles(ectx).count() == 6
        assert directory.grants_by_role(ectx, []) == {}
        assert directory.members_by_role(ectx, []) == {}
    readers = (
        lambda: list(directory.roles(ectx)),
        lambda: directory.grants_by_role(ectx, []),
        lambda: directory.members_by_role(ectx, []),
    )
    for read in readers:
        with pytest.raises(TenantContextError):
            read()  # fuera de toda transacción
        with tenant_scope(ctx(rbac.b)), pytest.raises(TenantContextError):
            read()  # scope de otra organización
        with tenant_scope(tenant), pytest.raises(TenantContextError, match="recalcularlo"):
            read()  # mismo contexto, otra transacción

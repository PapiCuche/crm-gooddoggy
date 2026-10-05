"""F2-29: `POST /api/v1/o/{slug}/roles/` crea un rol propio, vacío. Middleware, sesión, motor
de autorización y PostgreSQL con el rol `crm_app`."""

from typing import Any

import psycopg
import pytest
from django.test import Client

from apps.access.catalog import BY_CODE
from apps.access.models import Role
from apps.access.selectors import AccessDenied, Denied
from apps.access.services import RoleNameTaken, create_role
from core.tenancy.context import TenantContextError
from core.tenancy.scope import tenant_scope
from tests import test_anti_escalation, test_authorization, test_self_context
from tests.factories import make_user
from tests.test_anti_escalation import EDIT, acting, audit, race, state
from tests.test_authorization import VIEW, give
from tests.test_memberships import ctx, join
from tests.test_self_context import NOT_FOUND, reply, signed

world, rbac = test_authorization.world, test_anti_escalation.rbac
real_stack = test_self_context.real_stack
pytestmark = pytest.mark.usefixtures("tenant_db")
DENIED = (403, b'{"code":"PERMISSION_DENIED"}')
URL = "/api/v1/o/{}/roles/"


def create(client: Client, body: Any, org: str = "org-a") -> Any:
    client.cookies["csrftoken"] = token = "t" * 32
    return client.post(URL.format(org), body, "application/json", headers={"X-CSRFToken": token})


def test_it_creates_an_empty_role_that_can_be_listed_and_assigned(
    rbac: Any, migrator: psycopg.Connection[Any]
) -> None:
    ana, before = signed(rbac.ana), state(migrator)
    made = create(ana, {"name": "  Caja y Cobros  ", "description": " Cobra en tienda "})
    with tenant_scope(ctx(rbac.a)):
        role = Role.objects.get(code="caja-y-cobros")
    assert (made.status_code, made.json()) == (
        201,
        {
            "id": str(role.pk),
            "code": "caja-y-cobros",  # del nombre; el cliente no lo elige
            "name": "Caja y Cobros",
            "description": "Cobra en tienda",
            "is_system": False,
            "permissions": [],
            "members": 0,
        },
    )
    assert (role.organization_id, role.is_system, role.is_owner_role) == (rbac.a, False, False)
    assert state(migrator) == (before[0], before[1], before[2] + 1)  # ni concesiones ni miembros
    assert audit(migrator)[-1] == (
        "role.created", "role", role.pk, {"name": [None, "Caja y Cobros"]}, {}, rbac.ana.pk
    )  # fmt: skip
    listed = ana.get(URL.format("org-a")).json()["results"]
    assert listed[-1] == made.json()  # en el directorio, al final: por orden de creación
    link = f"/api/v1/o/org-a/members/{rbac.m_eva}/roles/{role.pk}/"
    assert ana.put(link, headers={"X-CSRFToken": "t" * 32}).status_code == 204  # y se asigna
    assert create(ana, {"name": "Sin descripción"}).json()["description"] == ""
    assert create(ana, {"name": "Otra", "code": "x", "is_system": True}).json()["code"] == "otra"


def test_the_name_is_unique_in_the_organization_and_the_code_never_collides(rbac: Any) -> None:
    ana = signed(rbac.ana)
    assert create(ana, {"name": "Ventas Norte"}).status_code == 201
    for taken in ("Ventas Norte", "ventas norte", " VENTAS NORTE ", "Owner", "vendedor"):
        again = create(ana, {"name": taken})  # también los de plantilla, sin distinguir mayúsculas
        assert (again.status_code, again.json()["code"]) == (409, "ROLE_NAME_TAKEN"), taken
        assert set(again.json()) == {"code", "message"}
    codes = [
        create(ana, {"name": name}).json()["code"] for name in ("Ventas-Norte", "ventas_norte")
    ]
    assert codes == ["ventas-norte-2", "ventas_norte"]  # otro nombre, mismo código de partida
    assert create(ana, {"name": "¡¿?!"}).json()["code"] == "rol"  # un nombre sin letras ni dígitos
    assert create(ana, {"name": "é" * 100}).status_code == 201  # el máximo: el código se recorta
    carla = make_user(email="carla@example.com")
    with tenant_scope(ctx(rbac.b)):
        Role.objects.create(code="owner", name="Owner", is_owner_role=True)
    give(rbac.b, join(rbac.b, carla).pk, {"roles.manage": None})
    there = create(signed(carla), {"name": "Ventas Norte"}, org="org-b")  # otra organización
    assert (there.status_code, there.json()["code"]) == (201, "ventas-norte")

    refused: list[str] = []

    def creating(name: str) -> Any:
        def run(tenant: Any) -> None:
            try:
                create_role(tenant, name=name)
            except RoleNameTaken:
                refused.append(name)

        return run

    changes = {
        "ana": (rbac.ana, creating("Simultáneo")),
        "luis": (rbac.luis, creating("simultáneo")),
    }
    race(rbac.a, changes, hold="ana")  # dos a la vez: el bloqueo de RBAC las pone en fila
    with tenant_scope(ctx(rbac.a)):
        assert Role.objects.filter(name__iexact="simultáneo").count() == 1
    assert refused == ["simultáneo"]  # la segunda ve a la primera, ya confirmada


def test_without_session_permission_or_a_valid_body_it_creates_nothing(
    rbac: Any, migrator: psycopg.Connection[Any]
) -> None:
    rest = [code for code in BY_CODE if code not in ("roles.manage", VIEW, EDIT)]
    give(rbac.a, rbac.m_eva, dict.fromkeys(rest))  # todo el catálogo menos `roles.manage`
    before, eva, ana = state(migrator), signed(rbac.eva), signed(rbac.ana)
    with tenant_scope(ctx(rbac.a)):
        roles = Role.objects.count()
    assert create(Client(), {"name": "X"}).status_code == 401
    assert reply(create(eva, {"name": "X"})) == DENIED
    assert reply(create(eva, {"nombre": "sin validar"})) == DENIED  # ni se mira el cuerpo
    for org in ("org-b", "no-existe"):
        assert reply(create(ana, {"name": "X"}, org)) == NOT_FOUND
    no_token = Client(enforce_csrf_checks=True)
    no_token.force_login(rbac.ana)
    assert no_token.post(URL.format("org-a"), {"name": "X"}, "application/json").json()["code"] == (
        "CSRF_FAILED"
    )
    bad: tuple[tuple[dict[str, Any], str], ...] = (
        ({}, "name"),
        ({"name": ""}, "name"),
        ({"name": "   "}, "name"),
        ({"name": None}, "name"),
        ({"name": "a" * 101}, "name"),
        ({"name": "dos\nlíneas"}, "name"),
        ({"name": "nulo\x00"}, "name"),
        ({"name": ["lista"]}, "name"),
        ({"name": "X", "description": "d" * 256}, "description"),
        ({"name": "X", "description": "con\ttabulador"}, "description"),
    )
    for body, field in bad:
        answer = create(ana, body)
        assert (answer.status_code, answer.json()["code"]) == (400, "VALIDATION_ERROR"), body
        assert list(answer.json()["fields"]) == [field], body
    with tenant_scope(ctx(rbac.a)):
        assert Role.objects.count() == roles
    assert state(migrator) == before


def test_the_service_checks_the_same_and_needs_the_active_scope(rbac: Any) -> None:
    with pytest.raises(TenantContextError):
        create_role(ctx(rbac.a, rbac.ana), name="Fuera de scope")
    with acting(rbac.a, rbac.eva) as tenant, pytest.raises(AccessDenied) as denied:
        create_role(tenant, name="Sin permiso")
    assert denied.value.reason is Denied.PERMISSION
    for name, description in (("", ""), ("a" * 101, ""), ("dos\nlíneas", ""), ("X", "d" * 256)):
        with acting(rbac.a, rbac.ana) as tenant, pytest.raises(ValueError, match="imprimible"):
            create_role(tenant, name=name, description=description)
    with acting(rbac.a, rbac.ana) as tenant:
        role = create_role(tenant, name="  Desde el servicio  ")
        assert (role.name, role.code, role.description) == (
            "Desde el servicio",
            "desde-el-servicio",
            "",
        )

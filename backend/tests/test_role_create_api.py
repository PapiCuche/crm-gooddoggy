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
    created = {"name": [None, "Caja y Cobros"], "description": [None, "Cobra en tienda"]}
    assert audit(migrator)[-1] == (
        "role.created", "role", role.pk, created, {"role": "caja-y-cobros"}, rbac.ana.pk
    )  # fmt: skip
    label = "SELECT entity_label FROM audit_logs WHERE action = 'role.created'"
    assert migrator.execute(label).fetchall() == [("Caja y Cobros",)]
    listed = ana.get(URL.format("org-a")).json()["results"]
    assert listed[-1] == made.json()  # en el directorio, al final: por orden de creación
    link = f"/api/v1/o/org-a/members/{rbac.m_eva}/roles/{role.pk}/"
    assert ana.put(link, headers={"X-CSRFToken": "t" * 32}).status_code == 204  # y se asigna
    assert create(ana, {"name": "Sin descripción"}).json()["description"] == ""
    assert create(ana, {"name": "Dos   espacios"}).json()["name"] == "Dos espacios"  # como se lee
    stored = [
        create(ana, {"name": name}).json()["name"] for name in ("Cafe\u0301 \u00b2", "Sur\u00a0A")
    ]
    assert stored == [
        "Caf\u00e9 \u00b2",
        "Sur A",
    ]  # NFC, no NFKC; el espacio de no separación, uno más
    assert (
        create(ana, {"name": "\U0001f436"}).status_code == 201
    )  # solo un emoji también es un nombre
    assert create(ana, {"name": "Otra", "code": "x", "is_system": True}).json()["code"] == "otra"


def test_the_name_is_unique_in_the_organization_and_the_code_never_collides(rbac: Any) -> None:
    ana = signed(rbac.ana)
    assert create(ana, {"name": "Ventas Norte"}).status_code == 201
    same = (
        "Ventas Norte", "ventas norte", " VENTAS NORTE ", "Ventas   Norte",  # mayúsculas y espacios
        "Owner", "vendedor",  # también los de plantilla
        "Owner\ufe0f", "Own\u034fer", "Owner\u3164", "Ｏｗｎｅｒ",  # no se ve, o es otra anchura
        "Owner \ufe0f", "Owner\u17b5", "Owner\u180b", "Owner\U000e0100", "Owner\ufffc",
        "Admi\u0307nistrador", "Supervi\u0307sor",  # un punto sobre la i, que ya lo lleva
        "Caja", "Cafe\u034f\u0301",  # frente a un nombre guardado con un carácter que no se ve
        "Caj\u0307a", "STRASSE",  # el punto sobre la j; «ß» es «ss»
        "Admi\u034f\u0307nistrador", "Supervi\ufe0f\u0307sor",  # el punto, tras algo que no se ve
        "Adm\u0131\u0307nistrador", "Ca\u0237\u0307a",  # sin punto propio: con él son «i» y «j»
        "Adm\u012f\u0307n", "Admi\u0307\u0328n",  # con una marca inferior en medio
        "Zona \u0456\u0307",  # la «i» cirílica también lleva su punto
        "Owner\U0001d159", "Owner\ufe00", "Owner\U000e01ef",  # los extremos de cada rango
        "Owner\u17b4", "Owner\u180f", "Owner\u115f",
    )  # fmt: skip
    assert create(ana, {"name": "Café"}).status_code == 201  # NFC…
    assert create(ana, {"name": "Caja\ufe0f"}).status_code == 201
    assert create(ana, {"name": "Adm\u012fn"}).status_code == 201
    assert create(ana, {"name": "Zona \u0456"}).status_code == 201
    assert create(ana, {"name": "Adm\u00edn"}).status_code == 201
    code = create(ana, {"name": "Stra\u00dfe"}).json()["code"]
    assert code == "strae"  # el código sale del nombre, no de la clave
    for taken in (*same, "Cafe\u0301"):  # …y NFD: la misma palabra
        again = create(ana, {"name": taken})
        assert (again.status_code, again.json()["code"]) == (409, "ROLE_NAME_TAKEN"), taken
        assert set(again.json()) == {"code", "message"}
    codes = [
        create(ana, {"name": name}).json()["code"] for name in ("Ventas-Norte", "ventas_norte")
    ]
    assert codes == ["ventas-norte-2", "ventas_norte"]  # otro nombre, mismo código de partida
    for hyphen in "\u2010\u2011":  # el guion tipográfico se pinta como el del teclado
        assert create(ana, {"name": f"Ventas{hyphen}Norte"}).status_code == 409, hyphen
    free = (
        "Ventas", "Norte", "Ventas Norte 2",  # contenido en otro, o que lo contiene
        "V\u0307entas Norte", "Ventas N\u022frte",  # un punto sobre una letra que no lo lleva
        "Adm\u0131nistrador", "Admi\u0301\u0307n",  # sin punto, o con el punto sobre una tilde
    )  # fmt: skip
    for name in free:
        assert create(ana, {"name": name}).status_code == 201, name
    long = [create(ana, {"name": "c" * 99 + last}).json()["code"] for last in "xyz"]
    assert long == ["c" * 40, "c" * 40 + "-2", "c" * 40 + "-3"]  # 40 más el sufijo caben en 50
    assert create(ana, {"name": "b" * 39 + " d"}).json()["code"] == "b" * 39  # sin guion final
    made = create(ana, {"name": "Larga", "description": "d" * 255})
    assert (made.status_code, len(made.json()["description"])) == (201, 255)
    assert create(ana, {"name": "¡¿?!"}).json()["code"] == "rol"  # un nombre sin letras ni dígitos
    assert create(ana, {"name": "é" * 100}).status_code == 201  # el máximo: el código se recorta
    carla = make_user(email="carla@example.com")
    give(rbac.b, join(rbac.b, carla).pk, {"roles.manage": None})
    stuck = create(signed(carla), {"name": "Sin Owner"}, org="org-b")  # sin rol Owner: nada cambia
    assert (stuck.status_code, stuck.json()["code"]) == (409, "LAST_OWNER")
    with tenant_scope(ctx(rbac.b)):
        assert not Role.objects.filter(name="Sin Owner").exists()
        Role.objects.create(code="owner", name="Owner", is_owner_role=True)
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
        ({"name": "\u3164"}, "name"),  # no se ve nada
        ({"name": "\u0301"}, "name"),  # solo una marca
        ({"name": "\u2800\ufe0f"}, "name"),
        ({"name": "sin\u200bjuntura"}, "name"),
        ({"name": "dos\u2028líneas"}, "name"),  # un separador de línea no es un espacio
        ({"name": "\u0301 \u0301"}, "name"),
        ({"name": "\u0958" * 100}, "name"),  # 100 al escribirlo, 200 en la forma en que se guarda
        ({"name": "a\u0001b", "description": "c\u0002d"}, "name"),
        ({"name": ["lista"]}, "name"),
        ({"name": "X", "description": "d" * 256}, "description"),
        ({"name": "X", "description": "con\ttabulador"}, "description"),
    )
    for body, field in bad:
        answer = create(ana, body)
        assert (answer.status_code, answer.json()["code"]) == (400, "VALIDATION_ERROR"), body
        assert list(answer.json()["fields"])[0] == field, body
    with tenant_scope(ctx(rbac.a)):
        assert Role.objects.count() == roles
    assert state(migrator) == before


def test_the_service_checks_the_same_and_needs_the_active_scope(rbac: Any) -> None:
    with pytest.raises(TenantContextError):
        create_role(ctx(rbac.a, rbac.ana), name="Fuera de scope")
    with acting(rbac.a, rbac.eva) as tenant, pytest.raises(AccessDenied) as denied:
        create_role(tenant, name="Sin permiso")
    assert denied.value.reason is Denied.PERMISSION
    refused = (("", ""), ("a" * 101, ""), ("dos\nlíneas", ""), ("\u3164", ""), ("X", "d" * 256))
    for name, description in (*refused, ("X", "a\tb")):
        with acting(rbac.a, rbac.ana) as tenant, pytest.raises(ValueError, match="imprimible"):
            create_role(tenant, name=name, description=description)
    with acting(rbac.a, rbac.ana) as tenant:
        assert create_role(tenant, name="Con texto", description="  x  ").description == "x"
    with acting(rbac.a, rbac.ana) as tenant:
        role = create_role(tenant, name="\t Desde   el servicio \n")
        assert (role.name, role.code, role.description) == (
            "Desde el servicio",
            "desde-el-servicio",
            "",
        )

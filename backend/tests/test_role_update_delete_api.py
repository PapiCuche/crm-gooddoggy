"""F2-38: `PATCH` y `DELETE /api/v1/o/{slug}/roles/{role_id}/` renombran y borran un rol.
Middleware, sesión, motor de autorización y PostgreSQL con el rol `crm_app`. Las reglas son las
de `access.services` (F2-05C); aquí se comprueba que la ruta no las salta."""

from typing import Any
from uuid import uuid4

import psycopg
import pytest
from django.test import Client

from apps.access.api import views
from apps.access.catalog import BY_CODE
from apps.access.models import MembershipRole, Role, RolePermission
from apps.access.selectors import AccessDenied, Denied
from apps.access.services import delete_role, update_role
from core.tenancy.scope import tenant_scope
from tests import test_anti_escalation, test_authorization, test_self_context
from tests.factories import make_user
from tests.test_anti_escalation import EDIT, acting, audit, state
from tests.test_authorization import VIEW, give
from tests.test_memberships import ctx, join
from tests.test_self_context import NOT_FOUND, reply, signed

world, rbac = test_authorization.world, test_anti_escalation.rbac
real_stack = test_self_context.real_stack
pytestmark = pytest.mark.usefixtures("tenant_db")
DENIED = (403, b'{"code":"PERMISSION_DENIED"}')
DONE = (204, b"")
ROLES = "SELECT count(*) FROM roles"


def edit(
    client: Client, role: Any, body: Any = None, org: str = "org-a", method: str = "patch"
) -> Any:
    client.cookies["csrftoken"] = token = "t" * 32
    path = f"/api/v1/o/{org}/roles/{role}/"
    send = getattr(client, method)
    return send(path, body or {}, "application/json", headers={"X-CSRFToken": token})


def delete(client: Client, role: Any, org: str = "org-a") -> Any:
    return edit(client, role, org=org, method="delete")


def count(migrator: psycopg.Connection[Any]) -> Any:
    return (*state(migrator), migrator.execute(ROLES).fetchone()[0])  # type: ignore[index]


def test_renaming_changes_the_name_everywhere_and_never_the_code(
    rbac: Any, migrator: psycopg.Connection[Any]
) -> None:
    target = rbac.target.pk
    give(rbac.a, rbac.membership, {VIEW: "ORGANIZATION"})
    with tenant_scope(ctx(rbac.a)):
        MembershipRole.objects.create(membership_id=rbac.m_eva, role=rbac.target)
        RolePermission.objects.create(
            role=rbac.target, permission_id=VIEW, supports_scope=True, scope="TEAM"
        )
    before, ana = count(migrator), signed(rbac.ana)
    stamp = "SELECT updated_at FROM roles WHERE id = %s"
    was = migrator.execute(stamp, [target]).fetchone()
    done = edit(ana, target, {"name": "  Caja   Fuerte ", "description": " Guarda la caja "})
    assert migrator.execute(stamp, [target]).fetchone() > was  # type: ignore[operator]
    assert (done.status_code, done.json()) == (
        200,
        {
            "id": str(target),
            "code": "target",  # el código es estable: no sigue al nombre
            "name": "Caja Fuerte",
            "description": "Guarda la caja",
            "is_system": False,
            "permissions": [{"code": VIEW, "scope": "TEAM"}],
            "members": 1,
            "editable": True,
        },
    )
    changes = {"name": ["Rol destino", "Caja Fuerte"], "description": ["", "Guarda la caja"]}
    row = ("role.updated", "role", target, changes, {"role": "target"}, rbac.ana.pk)
    assert audit(migrator)[-1] == row
    label = "SELECT entity_label FROM audit_logs WHERE action = 'role.updated'"
    assert migrator.execute(label).fetchall() == [("Caja Fuerte",)]
    listed = ana.get("/api/v1/o/org-a/members/").json()["results"]
    held = {member["id"]: member["roles"] for member in listed}[str(rbac.m_eva)]
    assert held == [{"id": str(target), "code": "target", "name": "Caja Fuerte"}]
    same: tuple[dict[str, str], ...] = (
        {},
        {"name": "Caja Fuerte"},
        {"name": " Caja  Fuerte "},
        {"description": "Guarda la caja"},
    )
    for body in same:  # lo que no cambia nada no escribe ni audita
        assert edit(ana, target, body).json()["name"] == "Caja Fuerte"
    assert count(migrator) == (before[0], before[1], before[2] + 1, before[3])
    only = edit(ana, target, {"description": ""}).json()  # un campo cada vez; vaciar vale
    assert (only["name"], only["description"]) == ("Caja Fuerte", "")
    assert audit(migrator)[-1][3] == {"description": ["Guarda la caja", ""]}
    own = edit(ana, target, {"name": "CAJA FUERTE"})  # su propio nombre no le estorba
    assert (own.status_code, own.json()["name"]) == (200, "CAJA FUERTE")
    for taken in ("vendedor", " OWNER ", f"Owner{chr(0xFE0F)}", "Rol propio"):  # el de otro, sí
        again = edit(ana, target, {"name": taken})
        assert (again.status_code, again.json()["code"]) == (409, "ROLE_NAME_TAKEN"), taken
    bad: tuple[tuple[dict[str, Any], str], ...] = (
        ({"name": ""}, "name"),
        ({"name": "   "}, "name"),
        ({"name": None}, "name"),
        ({"name": "a" * 101}, "name"),
        ({"name": f"dos{chr(10)}líneas"}, "name"),
        ({"description": "d" * 256}, "description"),
        ({"description": f"con{chr(9)}tabulador"}, "description"),
        ({"description": None}, "description"),
    )
    for body, field in bad:
        answer = edit(ana, target, body)
        assert (answer.status_code, answer.json()["code"]) == (400, "VALIDATION_ERROR"), body
        assert list(answer.json()["fields"]) == [field], body
    ignored = edit(ana, target, {"code": "x", "is_system": True, "editable": False}).json()
    assert (ignored["code"], ignored["is_system"], ignored["editable"]) == ("target", False, True)
    assert count(migrator) == (before[0], before[1], before[2] + 3, before[3])


def test_deleting_removes_the_role_and_its_grants_but_never_one_with_members(
    rbac: Any, migrator: psycopg.Connection[Any]
) -> None:
    give(rbac.a, rbac.membership, {VIEW: "ORGANIZATION", EDIT: "ORGANIZATION"})
    spare = give(rbac.a, rbac.m_eva, {VIEW: "TEAM", "users.view": None, EDIT: "OWN"}, code="sobra")
    idle = join(rbac.a, make_user(), "SUSPENDED").pk
    with tenant_scope(ctx(rbac.a)):
        Role.objects.filter(pk=spare.pk).update(name="Sobra", description="Ya no se usa")
        MembershipRole.objects.create(membership_id=idle, role=spare)
    ana = signed(rbac.ana)
    for holder in (rbac.m_eva, idle):  # con miembros, también uno suspendido, no se borra
        before = count(migrator)
        used = delete(ana, spare.pk)
        assert (used.status_code, used.json()["code"]) == (409, "ROLE_IN_USE")
        assert set(used.json()) == {"code", "message"} and count(migrator) == before
        with tenant_scope(ctx(rbac.a)):
            MembershipRole.objects.filter(membership_id=holder, role=spare).delete()
    before = count(migrator)
    gone = delete(ana, spare.pk)
    assert reply(gone) == DONE and "Content-Type" not in gone.headers
    assert count(migrator) == (before[0] - 3, before[1], before[2] + 1, before[3] - 1)
    changes = {"name": ["Sobra", None], "description": ["Ya no se usa", None]}
    grants = [["users.view", None], [EDIT, "OWN"], [VIEW, "TEAM"]]  # por código
    metadata = {"role": "sobra", "permissions": grants}
    assert audit(migrator)[-1] == (
        "role.deleted", "role", spare.pk, changes, metadata, rbac.ana.pk
    )  # fmt: skip
    label = "SELECT entity_label FROM audit_logs WHERE action = 'role.deleted'"
    assert migrator.execute(label).fetchall() == [("Sobra",)]
    listed = ana.get("/api/v1/o/org-a/roles/").json()["results"]
    assert "sobra" not in {row["code"] for row in listed}
    assert reply(delete(ana, spare.pk)) == NOT_FOUND  # repetirlo: ya no existe
    again = edit(ana, rbac.target.pk, {"name": "Sobra"})  # y su nombre queda libre
    assert (again.status_code, again.json()["name"]) == (200, "Sobra")


def test_without_session_permission_or_the_role_it_changes_nothing(
    rbac: Any, migrator: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    target = rbac.target.pk
    stranger = join(rbac.b, make_user(email="otra@example.com")).pk
    foreign = give(rbac.b, stranger, {"users.view": None}, code="de-b").pk
    rest = [code for code in BY_CODE if code not in ("roles.manage", VIEW, EDIT)]
    give(rbac.a, rbac.m_eva, dict.fromkeys(rest))  # todo el catálogo menos `roles.manage`
    before, eva, ana = count(migrator), signed(rbac.eva), signed(rbac.ana)
    name = {"name": "Otro nombre"}
    for method in ("patch", "delete"):
        assert edit(Client(), target, name, method=method).status_code == 401
        for role in (target, foreign, uuid4()):
            assert reply(edit(eva, role, {"name": ""}, method=method)) == DENIED  # ni se mira
        for absent in (foreign, uuid4(), "no-es-uuid"):
            assert reply(edit(ana, absent, name, method=method)) == NOT_FOUND, absent
        for org in ("org-b", "no-existe"):
            assert reply(edit(ana, target, name, org=org, method=method)) == NOT_FOUND
    no_token = Client(enforce_csrf_checks=True)
    no_token.force_login(rbac.ana)
    url = f"/api/v1/o/org-a/roles/{target}/"
    assert no_token.patch(url, name, "application/json").json()["code"] == "CSRF_FAILED"
    assert no_token.delete(url).json()["code"] == "CSRF_FAILED"
    for other in ("get", "post", "put"):  # solo PATCH y DELETE
        assert reply(edit(ana, target, name, method=other)) == DENIED
    assert count(migrator) == before
    reached: list[Any] = []
    for service in ("update_role", "delete_role"):
        monkeypatch.setattr(views, service, lambda *a, **kwargs: reached.append(kwargs))
    assert reply(edit(eva, target, name)) == DENIED and reply(delete(eva, target)) == DENIED
    assert reached == []  # sin `roles.manage` se deniega antes del servicio y de su bloqueo
    assert reply(delete(ana, target)) == DONE
    assert reached == [{"role_id": target}]
    for reason, expected in (
        (Denied.MEMBERSHIP, NOT_FOUND),  # lo que el actor perdió mientras esperaba el bloqueo
        (Denied.PERMISSION, DENIED),
        (Denied.OWNER_ROLE, DENIED),
    ):

        def lost(*args: Any, reason: Denied = reason, **kwargs: Any) -> None:
            raise AccessDenied(reason)

        for service in ("update_role", "delete_role"):
            monkeypatch.setattr(views, service, lost)
        assert reply(edit(ana, target, name)) == expected and reply(delete(ana, target)) == expected


def test_the_route_cannot_skip_the_rules_of_the_services(
    rbac: Any, migrator: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = rbac.roles["owner"].pk
    roles = {
        "wide": give(rbac.a, rbac.m_eva, {VIEW: "ORGANIZATION"}).pk,  # luis lo tiene con TEAM
        "other": give(rbac.a, rbac.m_eva, {"users.view": None}).pk,  # luis no lo tiene
        "admin": give(rbac.a, rbac.m_eva, {"users.manage": None}).pk,  # sensible: no es Owner
        "narrow": give(rbac.a, rbac.m_eva, {VIEW: "OWN"}).pk,
        "mixed": give(rbac.a, rbac.m_eva, {"users.view": None, VIEW: "OWN"}).pk,  # cada uno, una
        "mine": give(rbac.a, rbac.membership, {"users.view": None}).pk,  # otro rol de la Owner
    }
    marta = join(rbac.a, make_user(email="marta@example.com")).pk
    used = give(rbac.a, marta, {VIEW: "ORGANIZATION"}).pk  # con un miembro, y luis no lo cubre
    with tenant_scope(ctx(rbac.a)):  # sin miembros: lo que decide es la regla, no el 409
        MembershipRole.objects.filter(membership_id=rbac.m_eva).delete()
        Role.objects.filter(pk=owner).update(code="fundador", name="Otro")
        Role.objects.filter(pk=rbac.target.pk).update(code="owner", name="Owner")  # uno propio
    before, luis, ana = count(migrator), signed(rbac.luis), signed(rbac.ana)
    name = {"name": "Renombrado"}
    for client, role in (  # ni renombrar ni borrar: un rol propio (PO-2) o el rol Owner
        (luis, rbac.delegator.pk),
        (ana, roles["mine"]),
        (ana, owner),
        (luis, owner),
    ):
        assert reply(edit(client, role, name)) == DENIED, role
        assert reply(delete(client, role)) == DENIED, role
    for role in (roles["wide"], roles["other"], roles["admin"], roles["mixed"]):  # cubrirlo todo
        assert reply(delete(luis, role)) == DENIED, role
        assert reply(edit(luis, role, name)) == DENIED, role  # su nombre lo lee quien lo asigna
        assert reply(edit(luis, role)) == DENIED, role  # y un PATCH vacío enseñaría lo que concede
        assert reply(edit(luis, role, {"name": "owner"})) == DENIED, role  # ni si el nombre existe
    assert reply(delete(ana, roles["mixed"])) == DENIED  # todas las concesiones, no la primera
    assert reply(edit(ana, roles["mixed"], name)) == DENIED  # tampoco al renombrar
    assert reply(delete(luis, used)) == DENIED  # y eso va antes que decir si tiene miembros
    assert count(migrator) == before  # una negativa no escribe nada
    assert edit(luis, roles["narrow"], name).status_code == 200  # lo que sí cubre
    assert reply(delete(luis, roles["narrow"])) == DONE
    assert reply(delete(ana, roles["admin"])) == DONE  # la Owner retira lo sensible
    by_code = delete(ana, rbac.target.pk)  # nada decide por el código o el nombre
    assert reply(by_code) == DONE
    assert count(migrator) == (before[0] - 2, before[1], before[2] + 4, before[3] - 3)
    with tenant_scope(ctx(rbac.a)):  # una con un miembro, y otra vacía: luis la cubre
        MembershipRole.objects.create(membership_id=marta, role=rbac.roles["supervisor"])
        RolePermission.objects.filter(role=rbac.roles["seller"]).delete()
    kept = count(migrator)
    for template in ("admin", "supervisor", "seller"):  # una plantilla se edita, no se borra
        system = delete(ana, rbac.roles[template].pk)
        assert (system.status_code, system.json()["code"]) == (409, "ROLE_IS_SYSTEM"), template
        assert system.json()["message"] == "Un rol de plantilla no se borra."  # sin su nombre
        assert set(system.json()) == {"code", "message"}
    assert delete(luis, rbac.roles["seller"].pk).json()["code"] == "ROLE_IS_SYSTEM"  # no solo Owner
    assert reply(delete(luis, rbac.roles["admin"].pk)) == DENIED  # y cubrirla va antes
    assert count(migrator) == kept
    renamed = edit(ana, rbac.roles["seller"].pk, {"name": "Ventas"}).json()  # renombrarla, sí
    assert (renamed["name"], renamed["code"], renamed["is_system"]) == ("Ventas", "seller", True)
    carla = make_user(email="carla@example.com")  # una organización sin rol Owner: nada cambia
    give(rbac.b, join(rbac.b, carla).pk, {"roles.manage": None})
    orphan = give(rbac.b, join(rbac.b, make_user()).pk, {"users.view": None}).pk
    for stuck in (
        edit(signed(carla), orphan, name, org="org-b"),
        delete(signed(carla), orphan, "org-b"),
    ):
        assert (stuck.status_code, stuck.json()["code"]) == (409, "LAST_OWNER")
    give(rbac.a, rbac.membership, {VIEW: "ORGANIZATION"})
    monkeypatch.delitem(BY_CODE, VIEW)  # retirado del catálogo: falla cerrado, también la Owner
    assert reply(delete(ana, roles["wide"])) == DENIED
    assert reply(edit(ana, roles["wide"], {"name": "Otro más"})) == DENIED


def test_the_services_check_the_name_and_the_description_themselves(
    rbac: Any, migrator: psycopg.Connection[Any]
) -> None:
    before, target = count(migrator), rbac.target.pk
    refused: tuple[dict[str, str], ...] = (
        {"name": ""},
        {"name": f"dos{chr(10)}líneas"},
        {"name": "a" * 101},
        {"name": chr(0x3164)},  # no se ve nada
        {"description": "d" * 256},
        {"description": f"con{chr(9)}tabulador"},
    )
    for fields in refused:
        with acting(rbac.a, rbac.ana) as tenant, pytest.raises(ValueError, match="imprimible"):
            update_role(tenant, role_id=target, **fields)
    with acting(rbac.a, rbac.ana) as tenant:
        role = update_role(tenant, role_id=target, name="  Desde   el servicio ", description=" x ")
        assert (role.name, role.code, role.description) == ("Desde el servicio", "target", "x")
        assert update_role(tenant, role_id=target).name == "Desde el servicio"  # nada que cambiar
        delete_role(tenant, role_id=target)
    assert count(migrator) == (before[0], before[1], before[2] + 2, before[3] - 1)

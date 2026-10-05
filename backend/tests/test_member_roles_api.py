"""F2-25: `PUT` y `DELETE /api/v1/o/{slug}/members/{id}/roles/{role_id}/` asignan y quitan un
rol a un miembro. Middleware, sesión, motor de autorización y PostgreSQL con el rol `crm_app`.
Las reglas son las de `access.services` (F2-05C); aquí se comprueba que la ruta no las salta."""

from typing import Any
from uuid import uuid4

import psycopg
import pytest
from django.test import Client

from apps.access.api import views
from apps.access.catalog import BY_CODE
from apps.access.models import MembershipRole, RolePermission
from apps.access.selectors import AccessDenied, Denied
from core.tenancy.scope import tenant_scope
from tests import test_anti_escalation, test_authorization, test_self_context
from tests.factories import make_user
from tests.test_anti_escalation import EDIT, audit, state
from tests.test_authorization import VIEW, give
from tests.test_memberships import ctx, join
from tests.test_self_context import NOT_FOUND, me, reply, signed

world, rbac = test_authorization.world, test_anti_escalation.rbac
real_stack = test_self_context.real_stack
pytestmark = pytest.mark.usefixtures("tenant_db")
DENIED = (403, b'{"code":"PERMISSION_DENIED"}')
DONE = (204, b"")


def change(client: Client, member: Any, role: Any, method: str = "put", org: str = "org-a") -> Any:
    client.cookies["csrftoken"] = token = "t" * 32
    path = f"/api/v1/o/{org}/members/{member}/roles/{role}/"
    return getattr(client, method)(path, headers={"X-CSRFToken": token})


def codes(client: Client) -> list[str]:
    return [grant["code"] for grant in me(client).json()["permissions"]]


def test_assigning_gives_the_permissions_at_once_and_removing_takes_them_away(
    rbac: Any, migrator: psycopg.Connection[Any]
) -> None:
    seller, ana, eva = rbac.roles["seller"], signed(rbac.ana), signed(rbac.eva)
    before = state(migrator)
    assert codes(eva) == []
    done = change(ana, rbac.m_eva, seller.pk)
    assert reply(done) == DONE and "Content-Type" not in done.headers  # sin cuerpo
    assert codes(eva) == ["organization.view"]  # en su siguiente petición
    listed = ana.get("/api/v1/o/org-a/members/").json()["results"]
    held = {row["id"]: row["roles"] for row in listed}[str(rbac.m_eva)]
    assert held == [{"id": str(seller.pk), "code": "seller", "name": "Vendedor"}]  # el mismo id
    assert reply(change(ana, rbac.m_eva, seller.pk)) == DONE  # repetirlo no cambia nada
    assert state(migrator) == (before[0], before[1] + 1, before[2] + 1)
    assert reply(change(ana, rbac.m_eva, held[0]["id"], "delete")) == DONE  # el del directorio
    assert codes(eva) == []
    assert reply(change(ana, rbac.m_eva, seller.pk, "delete")) == NOT_FOUND  # ya no lo tiene
    assert state(migrator) == (before[0], before[1], before[2] + 2)
    assert audit(migrator)[-2:] == [
        (action, "membership", rbac.m_eva, {"role": change_}, {"role": "seller"}, rbac.ana.pk)
        for action, change_ in (
            ("membership.role_assigned", [None, str(seller.pk)]),
            ("membership.role_removed", [str(seller.pk), None]),
        )
    ]


def test_without_session_permission_or_membership_it_changes_and_reveals_nothing(
    rbac: Any, migrator: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    target = rbac.target.pk
    stranger = join(rbac.b, make_user(email="otra@example.com")).pk
    foreign = give(rbac.b, stranger, {"users.view": None}, code="de-b").pk
    give(rbac.a, rbac.m_eva, {"users.view": None, "roles.view": None, "roles.manage": None})
    before, eva, ana = state(migrator), signed(rbac.eva), signed(rbac.ana)
    for method in ("put", "delete"):
        assert change(Client(), rbac.m_luis, target, method).status_code == 401
        for member, role in ((rbac.m_luis, target), (stranger, foreign), (uuid4(), uuid4())):
            assert reply(change(eva, member, role, method)) == DENIED  # sin `users.manage`
        for member, role in (
            (stranger, target),  # miembro de otra organización
            (rbac.m_eva, foreign),  # rol de otra organización
            (uuid4(), target),
            (rbac.m_eva, uuid4()),
            ("no-es-uuid", target),
        ):
            assert reply(change(ana, member, role, method)) == NOT_FOUND, (method, member, role)
        assert reply(change(ana, rbac.m_eva, target, method, org="org-b")) == NOT_FOUND
    url = f"/api/v1/o/org-a/members/{rbac.m_eva}/roles/{target}/"
    no_token = Client(enforce_csrf_checks=True)
    no_token.force_login(rbac.ana)
    assert no_token.put(url).json()["code"] == "CSRF_FAILED"
    for other in ("get", "post", "patch"):  # solo PUT y DELETE
        assert reply(change(ana, rbac.m_eva, target, other)) == DENIED
    assert state(migrator) == before
    for reason, expected in ((Denied.MEMBERSHIP, NOT_FOUND), (Denied.PERMISSION, DENIED)):

        def lost(*args: Any, reason: Denied = reason, **kwargs: Any) -> None:
            raise AccessDenied(reason)  # lo que el actor perdió mientras esperaba el bloqueo

        monkeypatch.setattr(views, "assign_role", lost)
        assert reply(change(ana, rbac.m_eva, target)) == expected


def test_the_route_itself_requires_users_manage(rbac: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    rest = [code for code in BY_CODE if code not in ("users.manage", VIEW, EDIT)]
    give(rbac.a, rbac.m_eva, dict.fromkeys(rest))  # todo el catálogo menos `users.manage`
    reached: list[Any] = []
    for name in ("assign_role", "remove_role"):
        monkeypatch.setattr(views, name, lambda *args, **kwargs: reached.append(kwargs))
    eva = signed(rbac.eva)
    for method in ("put", "delete"):
        assert reply(change(eva, rbac.m_luis, rbac.target.pk, method)) == DENIED
    assert reached == []  # se deniega antes del servicio y de su bloqueo


def test_the_route_cannot_skip_the_rules_of_the_services(
    rbac: Any, migrator: psycopg.Connection[Any]
) -> None:
    roles = {
        "narrow": give(rbac.a, rbac.m_eva, {VIEW: "OWN"}).pk,  # luis lo tiene con TEAM
        "wide": give(rbac.a, rbac.m_eva, {VIEW: "ORGANIZATION"}).pk,
        "admin": give(rbac.a, rbac.m_eva, {"users.manage": None}).pk,  # sensible
    }
    marta = join(rbac.a, make_user(email="marta@example.com")).pk
    before, luis, ana = state(migrator), signed(rbac.luis), signed(rbac.ana)
    refused = (
        (luis, rbac.m_luis, rbac.target.pk),  # nadie cambia sus propios roles
        (ana, rbac.membership, rbac.target.pk),
        (luis, marta, roles["wide"]),  # más alcance del que tiene
        (luis, marta, rbac.roles["supervisor"].pk),  # permisos que no tiene
        (luis, marta, roles["admin"]),  # sensible: solo un Owner
        (luis, marta, rbac.roles["owner"].pk),
    )
    for client, member, role in refused:
        assert reply(change(client, member, role)) == DENIED  # sin decir cuál de las reglas
    for role in (roles["wide"], roles["admin"]):  # quitar exige lo mismo que asignar (PO-1)
        assert reply(change(luis, rbac.m_eva, role, "delete")) == DENIED
    assert reply(change(luis, rbac.membership, rbac.roles["owner"].pk, "delete")) == DENIED
    assert state(migrator) == before  # una negativa no escribe nada
    assert reply(change(luis, marta, roles["narrow"])) == DONE  # lo que sí cubre
    assert reply(change(luis, rbac.m_eva, roles["narrow"], "delete")) == DONE
    assert reply(change(ana, marta, roles["admin"])) == DONE  # la Owner delega lo sensible
    assert state(migrator) == (before[0], before[1] + 1, before[2] + 3)


def test_the_last_active_owner_keeps_the_owner_role(
    rbac: Any, migrator: psycopg.Connection[Any]
) -> None:
    owner = rbac.roles["owner"]
    # Un rol Owner sin permisos sensibles lo cubre quien no es Owner: decide el recuento.
    with tenant_scope(ctx(rbac.a)):
        RolePermission.objects.filter(role=owner).exclude(permission_id="users.view").delete()
    give(rbac.a, rbac.m_luis, {"users.view": None})
    before, luis = state(migrator), signed(rbac.luis)
    last = change(luis, rbac.membership, owner.pk, "delete")
    assert last.status_code == 409 and set(last.json()) == {"code", "message"}
    assert last.json()["code"] == "LAST_OWNER" and state(migrator) == before
    with tenant_scope(ctx(rbac.a)):
        MembershipRole.objects.create(membership_id=rbac.m_eva, role=owner)  # otra Owner activa
    assert reply(change(luis, rbac.membership, owner.pk, "delete")) == DONE
    # Una organización sin rol Owner no admite ningún cambio: tampoco asignar.
    carla = make_user(email="carla@example.com")
    orphan = give(rbac.b, join(rbac.b, carla).pk, {"users.manage": None}).pk
    stuck = change(signed(carla), join(rbac.b, make_user()).pk, orphan, org="org-b")
    assert (stuck.status_code, stuck.json()["code"]) == (409, "LAST_OWNER")

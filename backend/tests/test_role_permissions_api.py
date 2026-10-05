"""F2-31 y F2-33: `PUT /api/v1/o/{slug}/roles/{role_id}/permissions/{code}/` concede un permiso
a un rol o cambia su alcance, y `DELETE` lo retira. Middleware, sesión, motor de autorización y
PostgreSQL con el rol `crm_app`. Las reglas son las de `access.services` (F2-05C); aquí se
comprueba que la ruta no las salta."""

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
OMIT: Any = object()


def grant(
    client: Client, role: Any, code: str, scope: Any = OMIT, org: str = "org-a", method: str = "put"
) -> Any:
    client.cookies["csrftoken"] = token = "t" * 32
    path = f"/api/v1/o/{org}/roles/{role}/permissions/{code}/"
    body = {} if scope is OMIT else {"scope": scope}
    send = getattr(client, method)
    return send(path, body, "application/json", headers={"X-CSRFToken": token})


def held(client: Client) -> dict[str, list[str]]:
    return {grant["code"]: grant["scopes"] for grant in me(client).json()["permissions"]}


def test_granting_reaches_the_members_at_once_and_a_scope_can_change(
    rbac: Any, migrator: psycopg.Connection[Any]
) -> None:
    target = rbac.target.pk
    give(rbac.a, rbac.membership, {VIEW: "ORGANIZATION"})  # la Owner, además, con el de prueba
    with tenant_scope(ctx(rbac.a)):
        MembershipRole.objects.create(membership_id=rbac.m_eva, role=rbac.target)
    before, ana, eva = state(migrator), signed(rbac.ana), signed(rbac.eva)
    assert held(eva) == {}
    done = grant(ana, target, "organization.view")
    assert reply(done) == DONE and "Content-Type" not in done.headers  # sin cuerpo
    assert held(eva) == {"organization.view": []}  # en su siguiente petición
    assert reply(grant(ana, target, "organization.view", None)) == DONE  # repetirlo: nada
    assert state(migrator) == (before[0] + 1, before[1], before[2] + 1)
    assert reply(grant(ana, target, VIEW, "TEAM")) == DONE
    assert held(eva)[VIEW] == ["TEAM"]
    for scope in ("ORGANIZATION", "ORGANIZATION", "OWN"):  # más, lo mismo y menos
        assert reply(grant(ana, target, VIEW, scope)) == DONE
    assert held(eva) == {"organization.view": [], VIEW: ["OWN"]}
    listed = ana.get("/api/v1/o/org-a/roles/").json()["results"]
    row = {row["id"]: row for row in listed}[str(target)]
    assert row["permissions"] == [
        {"code": "organization.view", "scope": None},
        {"code": VIEW, "scope": "OWN"},
    ]
    assert state(migrator) == (before[0] + 2, before[1], before[2] + 4)  # una fila por permiso
    granted, changed = "role.permission_granted", "role.permission_scope_changed"
    changes = (
        (granted, [None, "organization.view"], [None, None]),
        (granted, [None, VIEW], [None, "TEAM"]),
        (changed, [VIEW, VIEW], ["TEAM", "ORGANIZATION"]),
        (changed, [VIEW, VIEW], ["ORGANIZATION", "OWN"]),
    )
    assert audit(migrator)[-4:] == [
        (action, "role", target, {"permission": permission, "scope": scope}, {}, rbac.ana.pk)
        for action, permission, scope in changes
    ]


def test_without_session_permission_or_a_valid_request_it_changes_nothing(
    rbac: Any, migrator: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    target = rbac.target.pk
    stranger = join(rbac.b, make_user(email="otra@example.com")).pk
    foreign = give(rbac.b, stranger, {"users.view": None}, code="de-b").pk
    rest = [code for code in BY_CODE if code not in ("roles.manage", VIEW, EDIT)]
    give(rbac.a, rbac.m_eva, dict.fromkeys(rest))  # todo el catálogo menos `roles.manage`
    give(rbac.a, rbac.membership, {VIEW: "ORGANIZATION"})
    before, eva, ana = state(migrator), signed(rbac.eva), signed(rbac.ana)
    assert grant(Client(), target, "organization.view").status_code == 401
    for role, code, scope in ((target, "organization.view", OMIT), (foreign, "no.existe", "X")):
        assert reply(grant(eva, role, code, scope)) == DENIED  # ni se mira lo que pide
    for role, code in (
        (foreign, "users.view"),  # rol de otra organización
        (uuid4(), "users.view"),
        ("no-es-uuid", "users.view"),
        (target, "users.mange"),  # fuera del catálogo
        (target, "users"),
    ):
        assert reply(grant(ana, role, code)) == NOT_FOUND, (role, code)
    for org in ("org-b", "no-existe"):
        assert reply(grant(ana, target, "users.view", org=org)) == NOT_FOUND
    bad = (
        (VIEW, OMIT),  # un permiso con alcance, sin alcance
        (VIEW, None),
        (VIEW, "GLOBAL"),
        (VIEW, "team"),
        (VIEW, ["TEAM"]),
        ("users.view", "TEAM"),  # un permiso sin alcance, con alcance
    )
    for code, scope in bad:
        answer = grant(ana, target, code, scope)
        assert (answer.status_code, answer.json()["code"]) == (400, "VALIDATION_ERROR"), scope
        assert list(answer.json()["fields"]) == ["scope"], (code, scope)
    no_token = Client(enforce_csrf_checks=True)
    no_token.force_login(rbac.ana)
    url = f"/api/v1/o/org-a/roles/{target}/permissions/users.view/"
    assert no_token.put(url, {}, "application/json").json()["code"] == "CSRF_FAILED"
    lone = b'{"scope": "%sud800"}' % bytes([92])  # un sustituto suelto: no es texto (F2-32)
    answer = ana.put(url, lone, "application/json", headers={"X-CSRFToken": "t" * 32})
    assert reply(answer) == (400, b'{"code":"PARSE_ERROR"}')
    for other in ("get", "post", "patch"):  # solo PUT y DELETE
        assert reply(grant(ana, target, "users.view", method=other)) == DENIED
    assert state(migrator) == before
    reached: list[Any] = []
    monkeypatch.setattr(views, "grant_permission", lambda *a, **kwargs: reached.append(kwargs))
    assert reply(grant(eva, target, "users.view")) == DENIED
    assert reached == []  # sin `roles.manage` se deniega antes del servicio y de su bloqueo
    assert reply(grant(ana, target, VIEW, "TEAM")) == DONE
    assert reached == [{"role_id": target, "code": VIEW, "scope": "TEAM"}]

    def broken(*args: Any, **kwargs: Any) -> None:
        raise ValueError("otro fallo, que no es del alcance")

    monkeypatch.setattr(views, "grant_permission", broken)
    with pytest.raises(ValueError, match="otro fallo"):  # no se disfraza de error de `scope`
        grant(ana, target, "users.view")
    for reason, expected in (
        (Denied.MEMBERSHIP, NOT_FOUND),  # lo que el actor perdió mientras esperaba el bloqueo
        (Denied.PERMISSION, DENIED),
        (Denied.OWNER_ROLE, DENIED),
    ):

        def lost(*args: Any, reason: Denied = reason, **kwargs: Any) -> None:
            raise AccessDenied(reason)

        monkeypatch.setattr(views, "grant_permission", lost)
        assert reply(grant(ana, target, "users.view")) == expected


def test_the_route_cannot_skip_the_rules_of_the_service(
    rbac: Any, migrator: psycopg.Connection[Any]
) -> None:
    target, owner = rbac.target.pk, rbac.roles["owner"].pk
    wide = give(rbac.a, rbac.m_eva, {VIEW: "ORGANIZATION"}).pk  # luis lo tiene con TEAM
    before, luis, ana = state(migrator), signed(rbac.luis), signed(rbac.ana)
    refused = (
        (luis, target, VIEW, "ORGANIZATION"),  # más alcance del que tiene
        (luis, target, VIEW, "BRANCH"),  # TEAM no contiene BRANCH
        (luis, target, "users.view", OMIT),  # un permiso que no tiene
        (luis, target, "users.manage", OMIT),  # sensible: lo tiene, pero no es Owner
        (luis, rbac.delegator.pk, VIEW, "OWN"),  # un rol que tiene asignado (PO-2)
        (ana, owner, "organization.view", OMIT),
        (luis, owner, VIEW, "TEAM"),  # el rol Owner no se edita, aunque cubra la concesión
        (luis, wide, VIEW, "TEAM"),  # reducir exige cubrir lo que había
        (luis, wide, VIEW, "OWN"),
        (ana, target, VIEW, "OWN"),  # la Owner tampoco concede lo que no tiene
    )
    for client, role, code, scope in refused:
        assert reply(grant(client, role, code, scope)) == DENIED, (role, code, scope)
    assert state(migrator) == before  # una negativa no escribe nada
    assert reply(grant(luis, target, VIEW, "TEAM")) == DONE  # lo que sí cubre
    assert reply(grant(luis, target, VIEW, "OWN")) == DONE  # y reducirlo: cubre los dos
    assert reply(grant(luis, target, VIEW, "ORGANIZATION")) == DENIED  # ampliarlo, no
    assert reply(grant(ana, target, "users.manage")) == DONE  # la Owner delega lo sensible
    assert state(migrator) == (before[0] + 2, before[1], before[2] + 3)
    carla = make_user(email="carla@example.com")  # una organización sin rol Owner: nada cambia
    orphan = give(rbac.b, join(rbac.b, carla).pk, {"roles.manage": None, "users.view": None}).pk
    stuck = grant(signed(carla), orphan, "users.view", org="org-b")
    assert (stuck.status_code, stuck.json()["code"]) == (409, "LAST_OWNER")


def test_revoking_takes_the_permission_from_the_members_at_once(
    rbac: Any, migrator: psycopg.Connection[Any]
) -> None:
    target = give(rbac.a, rbac.m_eva, {"organization.view": None, VIEW: "TEAM"}, code="lectura").pk
    give(rbac.a, rbac.membership, {VIEW: "ORGANIZATION"})  # la Owner, además, con el de prueba
    before, ana, eva = state(migrator), signed(rbac.ana), signed(rbac.eva)
    assert held(eva) == {"organization.view": [], VIEW: ["TEAM"]}
    done = grant(ana, target, VIEW, method="delete")
    assert reply(done) == DONE and "Content-Type" not in done.headers  # sin cuerpo
    assert held(eva) == {"organization.view": []}  # en su siguiente petición
    assert reply(grant(ana, target, VIEW, method="delete")) == NOT_FOUND  # ya no la tiene
    assert reply(grant(ana, target, "organization.view", method="delete")) == DONE
    listed = ana.get("/api/v1/o/org-a/roles/").json()["results"]
    assert {row["id"]: row for row in listed}[str(target)]["permissions"] == []
    assert held(eva) == {} and state(migrator) == (before[0] - 2, before[1], before[2] + 2)
    revoked = (([VIEW, None], ["TEAM", None]), (["organization.view", None], [None, None]))
    assert audit(migrator)[-2:] == [
        ("role.permission_revoked", "role", target, {"permission": p, "scope": s}, {}, rbac.ana.pk)
        for p, s in revoked
    ]


def test_revoking_without_session_permission_or_the_grant_changes_nothing(
    rbac: Any, migrator: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    target = give(rbac.a, rbac.m_luis, {"organization.view": None}, code="lectura").pk
    stranger = join(rbac.b, make_user(email="otra@example.com")).pk
    foreign = give(rbac.b, stranger, {"users.view": None}, code="de-b").pk
    rest = [code for code in BY_CODE if code not in ("roles.manage", VIEW, EDIT)]
    give(rbac.a, rbac.m_eva, dict.fromkeys(rest))  # todo el catálogo menos `roles.manage`
    before, eva, ana = state(migrator), signed(rbac.eva), signed(rbac.ana)
    assert grant(Client(), target, "organization.view", method="delete").status_code == 401
    for role, code in ((target, "organization.view"), (target, "users.view"), (foreign, "x")):
        assert reply(grant(eva, role, code, method="delete")) == DENIED  # ni dice qué existe
    missing: tuple[tuple[Any, str], ...] = (
        (foreign, "users.view"),  # rol de otra organización
        (uuid4(), "organization.view"),
        ("no-es-uuid", "organization.view"),
        (target, "users.view"),  # el rol no tiene esa concesión
        (target, "users.mange"),  # ni esta, que no está en el catálogo
    )
    for absent, code in missing:
        assert reply(grant(ana, absent, code, method="delete")) == NOT_FOUND, (absent, code)
    for org in ("org-b", "no-existe"):
        gone = grant(ana, target, "organization.view", org=org, method="delete")
        assert reply(gone) == NOT_FOUND
    no_token = Client(enforce_csrf_checks=True)
    no_token.force_login(rbac.ana)
    url = f"/api/v1/o/org-a/roles/{target}/permissions/organization.view/"
    assert no_token.delete(url).json()["code"] == "CSRF_FAILED"
    assert state(migrator) == before
    reached: list[Any] = []
    monkeypatch.setattr(views, "revoke_permission", lambda *a, **kwargs: reached.append(kwargs))
    assert reply(grant(eva, target, "organization.view", method="delete")) == DENIED
    assert reached == []  # sin `roles.manage` se deniega antes del servicio y de su bloqueo
    assert reply(grant(ana, target, "organization.view", method="delete")) == DONE
    assert reached == [{"role_id": target, "code": "organization.view"}]
    for reason, expected in (
        (Denied.MEMBERSHIP, NOT_FOUND),  # lo que el actor perdió mientras esperaba el bloqueo
        (Denied.PERMISSION, DENIED),
        (Denied.OWNER_ROLE, DENIED),
    ):

        def lost(*args: Any, reason: Denied = reason, **kwargs: Any) -> None:
            raise AccessDenied(reason)

        monkeypatch.setattr(views, "revoke_permission", lost)
        assert reply(grant(ana, target, "organization.view", method="delete")) == expected


def test_revoking_cannot_skip_the_rules_of_the_service(
    rbac: Any, migrator: psycopg.Connection[Any]
) -> None:
    owner = rbac.roles["owner"].pk
    roles = {
        "wide": give(rbac.a, rbac.m_eva, {VIEW: "ORGANIZATION"}).pk,  # luis lo tiene con TEAM
        "other": give(rbac.a, rbac.m_eva, {"users.view": None}).pk,  # luis no lo tiene
        "admin": give(rbac.a, rbac.m_eva, {"users.manage": None}).pk,  # sensible
        "narrow": give(rbac.a, rbac.m_eva, {VIEW: "OWN"}).pk,
    }
    with tenant_scope(ctx(rbac.a)):  # una concesión del rol Owner que luis sí cubre
        RolePermission.objects.create(
            role_id=owner, permission_id=VIEW, supports_scope=True, scope="TEAM"
        )
    before, luis, ana = state(migrator), signed(rbac.luis), signed(rbac.ana)
    refused = (
        (luis, roles["wide"], VIEW),  # más alcance del que tiene
        (luis, roles["other"], "users.view"),  # un permiso que no tiene
        (luis, roles["admin"], "users.manage"),  # sensible: lo tiene, pero no es Owner
        (luis, rbac.delegator.pk, VIEW),  # un rol que tiene asignado (PO-2)
        (ana, owner, "organization.view"),
        (luis, owner, VIEW),  # el rol Owner no se edita, aunque cubra la concesión
        (luis, owner, "no.existe"),  # ni dice qué concesiones tiene
        (ana, roles["wide"], VIEW),  # la Owner tampoco retira lo que no cubre: lo tiene con TEAM
    )
    for client, role, code in refused:
        assert reply(grant(client, role, code, method="delete")) == DENIED, (role, code)
    assert state(migrator) == before  # una negativa no escribe nada
    assert reply(grant(luis, roles["narrow"], VIEW, method="delete")) == DONE  # lo que sí cubre
    assert reply(grant(ana, roles["admin"], "users.manage", method="delete")) == DONE
    assert state(migrator) == (before[0] - 2, before[1], before[2] + 2)
    carla = make_user(email="carla@example.com")  # una organización sin rol Owner: nada cambia
    give(rbac.b, join(rbac.b, carla).pk, {"roles.manage": None})
    orphan = give(rbac.b, join(rbac.b, make_user()).pk, {"roles.manage": None}).pk
    stuck = grant(signed(carla), orphan, "roles.manage", org="org-b", method="delete")
    assert (stuck.status_code, stuck.json()["code"]) == (409, "LAST_OWNER")

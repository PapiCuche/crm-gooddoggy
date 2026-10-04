"""F2-16: `GET /api/v1/o/{slug}/members/`, el directorio de miembros de una organización.
Middleware, sesión, motor de autorización y PostgreSQL con el rol `crm_app`."""

from typing import Any

import pytest
from django.conf import settings
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

from apps.access.selectors import memberships, roles_by_membership
from apps.accounts.models import User
from apps.organizations.models import OrganizationMembership
from core.tenancy.context import TenantContextError
from core.tenancy.scope import tenant_scope
from tests import test_authorization, test_self_context
from tests.factories import TEST_PASSWORD, make_user
from tests.test_authorization import acting, give
from tests.test_memberships import ctx, join
from tests.test_self_context import NOT_FOUND, reply, signed

world, real_stack = test_authorization.world, test_self_context.real_stack
pytestmark = pytest.mark.usefixtures("tenant_db")
VIEW_USERS: dict[str, str | None] = {"users.view": None}


def members(client: Client, org: str = "org-a", **query: Any) -> Any:
    return client.get(f"/api/v1/o/{org}/members/", query)


def test_it_lists_every_member_of_the_organization_with_status_and_roles(world: Any) -> None:
    User.objects.filter(pk=world.ana.pk).update(first_name="Ana", last_name="López")
    give(world.a, world.membership, VIEW_USERS, code="lectora")
    luis, marta = make_user(email="luis@example.com"), make_user(email="marta@example.com")
    suspended, invited = join(world.a, luis, "SUSPENDED"), join(world.a, marta, "INVITED")
    give(world.a, suspended.pk, {}, code="b")
    give(world.a, suspended.pk, {}, code="a")
    join(world.b, make_user(email="otra@example.com"))  # otra organización: no aparece
    join(world.b, luis)  # ni su membresía en B
    body = members(signed(world.ana)).json()
    assert body["next"] is None and [row["id"] for row in body["results"]] == [
        str(world.membership),
        str(suspended.pk),
        str(invited.pk),
    ]  # por orden de alta
    first, second, third = body["results"]
    assert first == {
        "id": str(world.membership),
        "status": "ACTIVE",
        "joined_at": first["joined_at"],
        "user": {
            "id": str(world.ana.pk),
            "email": "ana@example.com",
            "first_name": "Ana",
            "last_name": "López",
        },
        "roles": [{"code": "lectora", "name": "Rol propio"}],
    }  # ni contraseña, ni marcas de plataforma, ni sus otras organizaciones
    assert (second["status"], second["user"]["email"]) == ("SUSPENDED", "luis@example.com")
    assert [role["code"] for role in second["roles"]] == ["a", "b"]  # todos sus roles
    assert (third["status"], third["roles"]) == ("INVITED", [])


def test_every_state_is_listed_whoever_the_user_is(world: Any) -> None:
    give(world.a, world.membership, VIEW_USERS)
    states = ["INVITED", "ACTIVE", "SUSPENDED", "DEACTIVATED"]
    assert states == OrganizationMembership.Status.values == settings.MEMBERSHIP_STATUSES
    made = [join(world.a, make_user(), status) for status in states]
    with tenant_scope(ctx(world.a)):
        made[0].save()  # mueve `updated_at`: `joined_at` es la fecha de alta, no la del cambio
    made.append(join(world.a, make_user(is_active=False)))  # usuario inactivo: sigue en la lista
    staff = User.objects.create_superuser("ops@example.com", TEST_PASSWORD)
    made.append(join(world.a, staff))  # staff de plataforma: un miembro más, sin su marca
    rows = members(signed(world.ana)).json()["results"][1:]  # la primera es la de ana
    assert [(row["id"], row["status"]) for row in rows] == [(str(m.pk), m.status) for m in made]
    assert set(rows[-1]["user"]) == {"id", "email", "first_name", "last_name"}
    joined = [m.created_at.isoformat().replace("+00:00", "Z") for m in made]  # en UTC
    assert [row["joined_at"] for row in rows] == joined


def test_without_a_session_membership_or_permission_it_reveals_nothing(world: Any) -> None:
    assert members(Client()).status_code == 401
    client = signed(world.ana)
    assert reply(members(client)) == (403, b'{"code":"PERMISSION_DENIED"}')  # miembro sin permiso
    give(world.a, world.membership, VIEW_USERS)
    assert members(client).status_code == 200
    for org in ("org-b", "no-existe"):  # ana no es de B: indistinguible de que no exista
        assert reply(members(client, org)) == NOT_FOUND
    outsider = signed(make_user(email="fuera@example.com"))
    assert reply(members(outsider)) == NOT_FOUND
    client.cookies["csrftoken"] = token = "t" * 32  # con token responde la vista, no el CSRF
    for write in (client.post, client.put, client.patch, client.delete):  # solo lectura
        denied = write("/api/v1/o/org-a/members/", headers={"X-CSRFToken": token})
        assert reply(denied) == (403, b'{"code":"PERMISSION_DENIED"}')


def test_it_pages_by_cursor_without_a_query_per_member(world: Any) -> None:
    give(world.a, world.membership, VIEW_USERS)
    client = signed(world.ana)

    def queries() -> int:
        with CaptureQueriesContext(connection) as captured:
            assert members(client).status_code == 200
        return len(captured)

    few = queries()
    for index in range(6):
        member = join(world.a, make_user(email=f"m{index}@example.com"))
        give(world.a, member.pk, {})
    assert queries() == few  # miembros y roles: una consulta cada uno, sean cuantos sean
    first = members(client, limit=3).json()
    assert len(first["results"]) == 3 and first["next"]
    rest = members(client, limit=200, cursor=first["next"]).json()
    emails = [row["user"]["email"] for row in first["results"] + rest["results"]]
    assert emails == ["ana@example.com", *(f"m{index}@example.com" for index in range(6))]
    assert members(client, limit=201).json()["code"] == "VALIDATION_ERROR"


def test_a_stale_context_fails_in_both_selectors(world: Any) -> None:
    tenant = ctx(world.a, world.ana)
    with acting(world.a, world.ana) as ectx:
        assert memberships(ectx).count() == 1 and roles_by_membership(ectx, []) == {}
    for read in (lambda: list(memberships(ectx)), lambda: roles_by_membership(ectx, [])):
        with pytest.raises(TenantContextError):
            read()  # fuera de toda transacción
        with tenant_scope(ctx(world.b)), pytest.raises(TenantContextError):
            read()  # scope de otra organización
        with tenant_scope(tenant), pytest.raises(TenantContextError, match="recalcularlo"):
            read()  # mismo contexto, otra transacción

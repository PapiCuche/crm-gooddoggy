"""F2-80: `POST /api/v1/o/{slug}/invitations/` deja creada una invitación (ADR-020). No envía
nada. Middleware, sesión, motor de autorización y PostgreSQL con el rol `crm_app`."""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from django.db import ProgrammingError
from django.test import Client

from apps.access.models import Role
from apps.members.services import invite
from apps.organizations.models import UserInvitation
from apps.organizations.services import TooManyPending, create_invitation
from core.tenancy.context import TenantContextError
from core.tenancy.scope import tenant_scope
from tests import test_anti_escalation, test_authorization, test_self_context
from tests.factories import make_user
from tests.test_anti_escalation import audit, race, state
from tests.test_authorization import VIEW, give
from tests.test_memberships import ctx, join
from tests.test_self_context import NOT_FOUND, reply, signed

world, rbac = test_authorization.world, test_anti_escalation.rbac
real_stack = test_self_context.real_stack
pytestmark = pytest.mark.usefixtures("tenant_db")
DENIED = (403, b'{"code":"PERMISSION_DENIED"}')
SOON = datetime.now(UTC) + timedelta(days=7)
REVOKE = "UPDATE user_invitations SET status = 'REVOKED' WHERE organization_id = %s"
ROWS = (
    "SELECT email, role_ids::text[], status, token_hash, send_count, sent_at, invited_by_user_id,"
    " organization_id FROM user_invitations ORDER BY created_at, id"
)
# Invitaciones sembradas de una vez: `%s` organización, quien invita, cuántas, estado y edad.
SEED = (
    "INSERT INTO user_invitations (organization_id, email, role_ids, expires_at,"
    " invited_by_user_id, status, created_at, updated_at) SELECT %s, 'sembrada' || n || %s,"
    " ARRAY[gen_random_uuid()], now() + interval '7 days', %s, %s, now() - %s::interval, now()"
    " FROM generate_series(1, %s) AS n"
)


def ask(client: Client, email: Any, roles: Any, org: str = "org-a") -> Any:
    client.cookies["csrftoken"] = token = "t" * 32
    body = {"email": email, "role_ids": [str(role) for role in roles]}
    path = f"/api/v1/o/{org}/invitations/"
    return client.post(path, body, "application/json", headers={"X-CSRFToken": token})


def rows(migrator: psycopg.Connection[Any]) -> list[tuple[Any, ...]]:
    return migrator.execute(ROWS).fetchall()


def seed(migrator: psycopg.Connection[Any], org: UUID, by: Any, count: int, **how: str) -> None:
    status, age, tag = how.get("status", "PENDING"), how.get("age", "0"), how.get("tag", "")
    migrator.execute(SEED, [org, f"{tag}@example.com", by.pk, status, age, count])


def refused(answer: Any) -> tuple[int, str]:
    return answer.status_code, answer.json()["code"]


def test_it_leaves_a_pending_invitation_without_a_link_and_audits_it(
    rbac: Any, migrator: psycopg.Connection[Any]
) -> None:
    agent, before = rbac.roles["seller"], state(migrator)
    users = migrator.execute("SELECT count(*) FROM users").fetchone()
    done = ask(signed(rbac.ana), "  Nueva.Persona@Example.COM ", [rbac.target.pk, agent.pk])
    assert done.status_code == 201
    made, wanted = done.json(), sorted([str(rbac.target.pk), str(agent.pk)])
    expires = datetime.fromisoformat(made.pop("expires_at"))
    assert timedelta(days=7) - timedelta(minutes=1) < expires - datetime.now(UTC) <= timedelta(7)
    assert made == {
        "id": made["id"],
        "email": "nueva.persona@example.com",  # la forma canónica de las cuentas (D-F2-3)
        "role_ids": wanted,
        "status": "PENDING",
    }
    stored = ("nueva.persona@example.com", wanted, "PENDING", None, 0, None, rbac.ana.pk, rbac.a)
    assert rows(migrator) == [stored]  # sin enlace y sin enviar: eso es de la tarea de envío
    assert audit(migrator)[-1] == (
        "membership.invited",
        "invitation",
        UUID(made["id"]),
        {"email": [None, "nueva.persona@example.com"], "role_ids": [None, wanted]},
        {},
        rbac.ana.pk,
    )
    assert state(migrator) == (before[0], before[1], before[2] + 1)  # ni roles ni membresías
    assert migrator.execute("SELECT count(*) FROM users").fetchone() == users  # ni una cuenta


def test_it_needs_both_permissions_and_covering_every_role_it_gives(
    rbac: Any, migrator: psycopg.Connection[Any]
) -> None:
    wide = give(rbac.a, rbac.membership, {VIEW: "ORGANIZATION"}, code="ancho")  # luis: `TEAM`
    narrow = give(rbac.a, rbac.membership, {VIEW: "OWN"}, code="estrecho")
    manager = give(rbac.a, rbac.membership, {"users.manage": None}, code="gestor")  # sensible
    give(rbac.a, rbac.m_eva, {"users.invite": None, VIEW: "ORGANIZATION"})  # eva: sin gestionar
    before, luis, eva = state(migrator), signed(rbac.luis), signed(rbac.eva)
    assert ask(Client(), "x@example.com", [rbac.target.pk]).status_code == 401
    for client in (luis, eva):  # a cada uno le falta uno de los dos, y no se valida nada antes
        for email, roles in (("x@example.com", [rbac.target.pk]), ("no", []), ("x@example.com", 1)):
            assert reply(ask(client, email, roles if roles != 1 else ["no-es-uuid"])) == DENIED
    give(rbac.a, rbac.m_luis, {"users.invite": None})  # ahora luis tiene los dos
    for roles in (
        [wide.pk],  # más alcance del que tiene
        [rbac.roles["owner"].pk],
        [manager.pk],  # un permiso sensible lo da solo un Owner, aunque luis lo tenga
        [narrow.pk, wide.pk],  # basta uno que no cubra
        [wide.pk, uuid4()],  # y lo que no cubre pesa más que lo que no existe
    ):
        assert reply(ask(luis, "x@example.com", roles)) == DENIED, roles
    assert rows(migrator) == [] and state(migrator)[2] == before[2]  # nada escrito ni auditado
    assert ask(luis, "x@example.com", [narrow.pk, rbac.target.pk]).status_code == 201
    assert ask(signed(rbac.ana), "y@example.com", [manager.pk, wide.pk]).status_code == 201
    assert [row[6] for row in rows(migrator)] == [rbac.luis.pk, rbac.ana.pk]  # quién invitó
    assert reply(ask(signed(rbac.ana), "z@example.com", [rbac.target.pk], "org-b")) == NOT_FOUND


@pytest.mark.parametrize(
    "email",
    [
        "sin-arroba",
        "dos@arrobas@example.com",
        "eñe@example.com",  # la parte local, solo ASCII
        '"con espacio"@example.com',  # `core.mail` no entrega una dirección entre comillas
        "=?utf-8?b?YW5h?=@example.com",
        "ana@example.com\nbcc: x@example.com",
        "ana@example.com, otra@example.com",
        "Ana <ana@example.com>",
        "a" * 245 + "@example.com",  # 257 caracteres: no cabe
        "",
        "   ",
        7,
        None,
        ["ana@example.com"],
    ],
)
def test_an_address_nobody_could_write_to_is_a_400(rbac: Any, email: Any) -> None:
    answer = ask(signed(rbac.ana), email, [rbac.target.pk])
    assert refused(answer) == (400, "VALIDATION_ERROR"), email
    assert list(answer.json()["fields"]) == ["email"]
    with tenant_scope(ctx(rbac.a)):
        assert not UserInvitation.objects.exists()


def test_roles_are_one_to_twenty_of_this_organization_without_repeats(
    rbac: Any, migrator: psycopg.Connection[Any]
) -> None:
    with tenant_scope(ctx(rbac.b)):
        theirs = Role.objects.create(code="ajeno", name="De otra organización")
    with tenant_scope(ctx(rbac.a)):
        many = [Role.objects.create(code=f"r{n}", name=f"Rol {n}").pk for n in range(21)]
    ana, mine = signed(rbac.ana), rbac.target.pk
    unknown = [{"code": "invalid", "message": "No es un rol de esta organización."}]
    for roles in ([theirs.pk], [uuid4()], [mine, theirs.pk]):  # el de otra, como el que no hay
        answer = ask(ana, "x@example.com", roles)
        assert refused(answer) == (400, "VALIDATION_ERROR")
        assert answer.json()["fields"] == {"role_ids": unknown}
    for roles in ([], many, [mine, mine], ["no-es-uuid"], [""]):
        answer = ask(ana, "x@example.com", roles)
        assert refused(answer) == (400, "VALIDATION_ERROR"), roles
        assert list(answer.json()["fields"]) == ["role_ids"]
    url, token = "/api/v1/o/org-a/invitations/", {"X-CSRFToken": ana.cookies["csrftoken"].value}
    bodies: tuple[Any, ...] = ({}, {"email": "x@example.com"}, {"role_ids": [str(mine)]}, [], "x")
    for body in bodies:
        assert ana.post(url, body, "application/json", headers=token).status_code == 400, body
    one = {"email": "x@example.com", "role_ids": str(mine)}  # un rol suelto no es una lista
    assert ana.post(url, one, "application/json", headers=token).status_code == 400
    assert rows(migrator) == []
    assert ask(ana, "x@example.com", many[:20]).status_code == 201  # veinte sí


def test_a_member_in_any_state_or_a_pending_address_is_not_invited_again(
    rbac: Any, migrator: psycopg.Connection[Any]
) -> None:
    join(rbac.a, make_user(email="suspendida@example.com"), "SUSPENDED")
    join(rbac.a, make_user(email="baja@example.com"), "DEACTIVATED")
    other = make_user(email="de-b@example.com")  # con cuenta, pero no de esta organización
    join(rbac.b, other)
    with tenant_scope(ctx(rbac.b)):  # la pendiente de otra organización no ocupa el correo aquí
        UserInvitation.objects.create(
            email="nadie@example.com", role_ids=[uuid4()], expires_at=SOON, invited_by=other
        )
    ana, target = signed(rbac.ana), [rbac.target.pk]
    for email in ("eva@example.com", "EVA@example.com", "suspendida@example.com"):
        assert refused(ask(ana, email, target)) == (409, "ALREADY_MEMBER"), email
    assert refused(ask(ana, "baja@example.com", target)) == (409, "ALREADY_MEMBER")
    assert refused(ask(ana, "ana@example.com", target)) == (409, "ALREADY_MEMBER")  # una misma
    assert len(rows(migrator)) == 1
    known, new = ask(ana, "de-b@example.com", target), ask(ana, "nadie@example.com", target)
    assert known.status_code == new.status_code == 201  # no dice quién tiene cuenta
    assert sorted(known.json()) == sorted(new.json())
    assert refused(ask(ana, "Nadie@Example.com", target)) == (409, "INVITATION_PENDING")
    migrator.execute("UPDATE user_invitations SET expires_at = now() - interval '1 day'")
    assert refused(ask(ana, "nadie@example.com", target)) == (409, "INVITATION_PENDING")  # caducó
    assert len(rows(migrator)) == 3 and state(migrator)[2] == 2  # ni fila ni auditoría de más
    migrator.execute("UPDATE user_invitations SET status = 'REVOKED' WHERE email LIKE 'nadie%'")
    assert ask(ana, "nadie@example.com", target).status_code == 201  # revocada: se puede otra vez


def test_an_organization_keeps_at_most_fifty_pending_and_makes_a_hundred_a_day(
    rbac: Any, migrator: psycopg.Connection[Any]
) -> None:
    ana, target = signed(rbac.ana), [rbac.target.pk]
    other = make_user(email="de-b@example.com")
    join(rbac.b, other)
    seed(migrator, rbac.b, other, 60, tag="-b")  # las de otra organización no cuentan
    seed(migrator, rbac.a, rbac.ana, 49, age="2 days")  # 49 pendientes, y caducadas o no
    migrator.execute("UPDATE user_invitations SET expires_at = now() - interval '1 hour'")
    assert ask(ana, "cincuenta@example.com", target).status_code == 201
    before = state(migrator)
    assert refused(ask(ana, "de-mas@example.com", target)) == (409, "INVITATION_LIMIT")
    assert state(migrator) == before
    migrator.execute(REVOKE, [rbac.a])
    seed(migrator, rbac.a, rbac.ana, 98, status="REVOKED", age="23 hours", tag="hoy")
    assert ask(ana, "cien@example.com", target).status_code == 201  # la número 100 del día
    limited = ask(ana, "ciento-una@example.com", target)
    assert refused(limited) == (429, "RATE_LIMITED")  # revocadas, pero creadas hoy: cuentan
    migrator.execute(
        "UPDATE user_invitations SET created_at = now() - interval '25 hours'"
        " WHERE email LIKE 'cien@%'"
    )
    assert ask(ana, "ciento-una@example.com", target).status_code == 201  # ayer ya no cuenta


def test_two_invitations_at_once_count_the_limit_one_after_the_other(
    rbac: Any, migrator: psycopg.Connection[Any]
) -> None:
    seed(migrator, rbac.a, rbac.ana, 49, age="2 days")
    late: list[str] = []

    def asking(email: str) -> Any:
        def change(tenant: Any) -> None:
            try:
                invite(tenant, email=email, role_ids=[rbac.target.pk])
            except TooManyPending:
                late.append(email)

        return rbac.ana, change

    done = race(
        rbac.a, {"una": asking("una@example.com"), "otra": asking("otra@example.com")}, "una"
    )
    assert done == {"una": "ok", "otra": "ok"} and late == ["otra@example.com"]
    assert [row[0] for row in rows(migrator)][-1] == "una@example.com" and len(rows(migrator)) == 50


def test_the_runtime_cannot_delete_an_invitation_and_the_services_need_their_scope(
    rbac: Any, migrator: psycopg.Connection[Any]
) -> None:
    seed(migrator, rbac.a, rbac.ana, 1)
    with pytest.raises(ProgrammingError, match="permission denied"), tenant_scope(ctx(rbac.a)):
        UserInvitation.objects.all().delete()
    with pytest.raises(TenantContextError):
        invite(ctx(rbac.a, rbac.ana), email="x@example.com", role_ids=[rbac.target.pk])
    with tenant_scope(ctx(rbac.a)) as system, pytest.raises(ValueError, match="persona"):
        create_invitation(system, email="x@example.com", role_ids=[rbac.target.pk])
    assert len(rows(migrator)) == 1

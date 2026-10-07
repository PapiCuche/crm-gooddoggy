"""F2-81: `GET /api/v1/o/{slug}/invitations/` lista las invitaciones de la organización
(ADR-020). Middleware, sesión, motor de autorización y PostgreSQL con el rol `crm_app`."""

from datetime import datetime
from typing import Any

import psycopg
import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

from tests import test_anti_escalation, test_authorization, test_self_context
from tests.factories import make_user
from tests.test_authorization import give
from tests.test_invite import ask, seed
from tests.test_memberships import join
from tests.test_self_context import NOT_FOUND, reply, signed

world, rbac = test_authorization.world, test_anti_escalation.rbac
real_stack = test_self_context.real_stack
pytestmark = pytest.mark.usefixtures("tenant_db")
DENIED = (403, b'{"code":"PERMISSION_DENIED"}')
HASH = "c0ffee" + "ab" * 29
# Lo que la tabla guarda de cada estado: `%s` estado, caducidad relativa, hash y correo.
MARK = (
    "UPDATE user_invitations SET status = %s, expires_at = now() + %s::interval,"
    " token_hash = %s, accepted_at = CASE WHEN %s = 'ACCEPTED' THEN now() END WHERE email = %s"
)


def listed(client: Client, org: str = "org-a", **query: Any) -> Any:
    return client.get(f"/api/v1/o/{org}/invitations/", query)


def test_it_lists_every_invitation_of_the_organization_newest_first_and_none_of_another(
    rbac: Any, migrator: psycopg.Connection[Any]
) -> None:
    other = make_user(email="de-b@example.com")
    join(rbac.b, other)
    seed(migrator, rbac.b, other, 3, tag="-b")
    ana, roles = signed(rbac.ana), sorted([str(rbac.target.pk), str(rbac.roles["seller"].pk)])
    made = [ask(ana, f"{name}@example.com", roles).json() for name in ("uno", "dos", "tres")]
    ask(ana, "cuatro@example.com", [rbac.target.pk])
    for mark, (email, status, expires) in enumerate(
        (
            ("uno@example.com", "PENDING", "-1 second"),  # pendiente y caducada: se enseña caducada
            ("dos@example.com", "REVOKED", "-1 day"),  # lo demás, como está guardado
            ("tres@example.com", "ACCEPTED", "-1 day"),
            ("cuatro@example.com", "PENDING", "1 minute"),
        )
    ):
        migrator.execute(MARK, [status, expires, f"{HASH[:-1]}{mark}", status, email])
    page = listed(ana)
    assert page.status_code == 200 and page.json()["next"] is None
    rows = page.json()["results"]
    assert [(row["email"].split("@")[0], row["status"]) for row in rows] == [
        ("cuatro", "PENDING"),
        ("tres", "ACCEPTED"),
        ("dos", "REVOKED"),
        ("uno", "EXPIRED"),
    ]
    assert rows[1] == {
        "id": made[2]["id"],
        "email": "tres@example.com",
        "role_ids": roles,
        "status": "ACCEPTED",
        "expires_at": rows[1]["expires_at"],
        "invited_by": str(rbac.ana.pk),
        "created_at": made[2]["created_at"],
    }
    assert datetime.fromisoformat(rows[1]["expires_at"]) < datetime.fromisoformat(
        rows[1]["created_at"]
    )  # la fecha guardada, no la de la respuesta de invitar
    assert made[2] == {**rows[1], "status": "PENDING", "expires_at": made[2]["expires_at"]}
    assert HASH[:20] not in page.content.decode() and "token" not in page.content.decode()
    assert "-b@" not in page.content.decode() and str(other.pk) not in page.content.decode()


def test_reading_them_needs_what_inviting_needs(
    rbac: Any, migrator: psycopg.Connection[Any]
) -> None:
    seed(migrator, rbac.a, rbac.ana, 1)
    give(rbac.a, rbac.m_eva, {"users.invite": None, "users.view": None})  # eva: sin gestionar
    assert listed(Client()).status_code == 401
    luis, eva = signed(rbac.luis), signed(rbac.eva)  # luis gestiona, pero no invita
    for client in (luis, eva):
        assert reply(listed(client)) == DENIED
        assert reply(listed(client, limit=0)) == DENIED  # y no se valida nada antes
    give(rbac.a, rbac.m_luis, {"users.invite": None})
    assert [row["email"] for row in listed(luis).json()["results"]] == ["sembrada1@example.com"]
    for org in ("org-b", "no-existe"):  # sin membresía, la organización no existe
        assert reply(listed(signed(rbac.ana), org)) == NOT_FOUND
    migrator.execute(
        "UPDATE organization_memberships SET status = 'SUSPENDED' WHERE id = %s", [rbac.m_luis]
    )
    assert reply(listed(luis)) == NOT_FOUND


def test_it_paginates_by_cursor_with_the_same_queries_for_any_number_of_rows(
    rbac: Any, migrator: psycopg.Connection[Any]
) -> None:
    ana = signed(rbac.ana)
    for name in "abcde":
        assert ask(ana, f"{name}@example.com", [rbac.target.pk]).status_code == 201
    first = listed(ana, limit=2).json()
    rest = listed(ana, limit=200, cursor=first["next"]).json()
    emails = [row["email"][0] for row in first["results"] + rest["results"]]
    assert emails == list("edcba") and first["next"] and rest["next"] is None
    assert listed(ana, limit=201).json()["code"] == "VALIDATION_ERROR"
    assert listed(ana, cursor="x").json()["code"] == "VALIDATION_ERROR"
    with CaptureQueriesContext(connection) as few:
        assert len(listed(ana, limit=1).json()["results"]) == 1
    seed(migrator, rbac.a, rbac.ana, 40, status="REVOKED")
    with CaptureQueriesContext(connection) as many:
        assert len(listed(ana).json()["results"]) == 45
    assert len(many) == len(few)

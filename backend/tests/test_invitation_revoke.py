"""F2-82: `PUT /api/v1/o/{slug}/invitations/{id}/status/` revoca una invitación pendiente
(ADR-020). Middleware, sesión, motor de autorización y PostgreSQL con el rol `crm_app`."""

from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from django.test import Client

from apps.access.selectors import Denied
from apps.members.services import invite, revoke_invitation
from tests import test_anti_escalation, test_authorization, test_self_context
from tests.factories import make_user
from tests.test_anti_escalation import audit, denied, race, state
from tests.test_authorization import VIEW, give
from tests.test_invitations_list import HASH, MARK, listed
from tests.test_invite import ask, refused, rows, seed
from tests.test_memberships import join
from tests.test_self_context import NOT_FOUND, reply, signed

world, rbac = test_authorization.world, test_anti_escalation.rbac
real_stack = test_self_context.real_stack
pytestmark = pytest.mark.usefixtures("tenant_db")
DENIED = (403, b'{"code":"PERMISSION_DENIED"}')
STORED = "SELECT status, token_hash, updated_at FROM user_invitations WHERE id = %s"
ID = "SELECT id FROM user_invitations WHERE email = %s"
LAPSE = "UPDATE user_invitations SET expires_at = now() - interval '1 day' WHERE id = %s"


def revoke(client: Client, invitation: Any, status: Any = "REVOKED", org: str = "org-a") -> Any:
    client.cookies["csrftoken"] = token = "t" * 32
    path = f"/api/v1/o/{org}/invitations/{invitation}/status/"
    return client.put(path, {"status": status}, "application/json", headers={"X-CSRFToken": token})


def stored(migrator: psycopg.Connection[Any], invitation: Any) -> Any:
    return migrator.execute(STORED, [invitation]).fetchone()


def test_it_revokes_a_pending_invitation_drops_its_link_and_frees_its_address(
    rbac: Any, migrator: psycopg.Connection[Any]
) -> None:
    ana, target = signed(rbac.ana), [rbac.target.pk]
    made = ask(ana, "x@example.com", target).json()
    migrator.execute("UPDATE user_invitations SET token_hash = %s", [HASH])  # como ya enviada
    before = state(migrator)
    done = revoke(ana, made["id"])
    assert (done.status_code, done.json()) == (200, {**made, "status": "REVOKED"})
    assert stored(migrator, made["id"])[:2] == ("REVOKED", None)  # el enlace no queda guardado
    assert audit(migrator)[-1] == (
        "membership.invitation_revoked",
        "invitation",
        UUID(made["id"]),
        {"status": ["PENDING", "REVOKED"]},
        {},
        rbac.ana.pk,
    )
    assert state(migrator) == (before[0], before[1], before[2] + 1)
    after = stored(migrator, made["id"])
    assert revoke(ana, made["id"]).json() == done.json()  # repetirlo responde lo mismo
    assert stored(migrator, made["id"]) == after and state(migrator)[2] == before[2] + 1
    again = ask(ana, "x@example.com", target)  # el correo queda libre
    assert again.status_code == 201 and again.json()["id"] != made["id"]
    migrator.execute(LAPSE, [again.json()["id"]])
    assert revoke(ana, again.json()["id"]).json()["status"] == "REVOKED"  # caducada: también
    assert [row["status"] for row in listed(ana).json()["results"]] == ["REVOKED", "REVOKED"]
    assert len(rows(migrator)) == 2  # ninguna fila se borra


def test_a_revoked_invitation_stops_counting_as_pending(
    rbac: Any, migrator: psycopg.Connection[Any]
) -> None:
    ana, target = signed(rbac.ana), [rbac.target.pk]
    seed(migrator, rbac.a, rbac.ana, 50, age="2 days")
    assert refused(ask(ana, "de-mas@example.com", target)) == (409, "INVITATION_LIMIT")
    one = migrator.execute(ID, ["sembrada7@example.com"]).fetchone()
    assert one and revoke(ana, one[0]).status_code == 200
    assert ask(ana, "de-mas@example.com", target).status_code == 201


def test_it_needs_both_permissions_and_covering_what_the_invitation_would_give(
    rbac: Any, migrator: psycopg.Connection[Any]
) -> None:
    wide = give(rbac.a, rbac.membership, {VIEW: "ORGANIZATION"}, code="ancho")  # luis: `TEAM`
    narrow = give(rbac.a, rbac.membership, {VIEW: "OWN"}, code="estrecho")
    manager = give(rbac.a, rbac.membership, {"users.manage": None}, code="gestor")  # sensible
    give(rbac.a, rbac.m_eva, {"users.invite": None, VIEW: "ORGANIZATION"})  # eva: sin gestionar
    ana, luis, eva = signed(rbac.ana), signed(rbac.luis), signed(rbac.eva)
    made = {
        name: ask(ana, f"{name}@example.com", roles).json()["id"]
        for name, roles in (
            ("plana", [narrow.pk, rbac.target.pk]),
            ("ancha", [narrow.pk, wide.pk]),
            ("sensible", [manager.pk, narrow.pk]),
            ("owner", [rbac.roles["owner"].pk]),
        )
    }
    seed(migrator, rbac.a, rbac.ana, 1)  # con un rol que ya no existe: no concede nada
    gone = migrator.execute(ID, ["sembrada1@example.com"]).fetchone()[0]  # type: ignore[index]
    other = make_user(email="de-b@example.com")
    join(rbac.b, other)
    seed(migrator, rbac.b, other, 1, tag="-b")
    theirs = migrator.execute(ID, ["sembrada1-b@example.com"]).fetchone()[0]  # type: ignore[index]
    before = state(migrator)
    assert revoke(Client(), made["plana"]).status_code == 401
    for client, user in ((luis, rbac.luis), (eva, rbac.eva)):  # a cada uno le falta uno
        for invitation in (made["plana"], theirs, uuid4()):  # y no aprende cuáles existen
            assert reply(revoke(client, invitation)) == DENIED
            assert reply(revoke(client, invitation, "nada")) == DENIED  # ni se valida antes
            asked = {"invitation_id": invitation}  # el servicio los relee antes de buscarla
            assert denied(rbac.a, user, revoke_invitation, **asked) is Denied.PERMISSION
    give(rbac.a, rbac.m_luis, {"users.invite": None})  # ahora luis tiene los dos
    for name in ("ancha", "sensible", "owner"):  # lo que no podría dar, tampoco lo retira
        assert reply(revoke(luis, made[name])) == DENIED, name
    for invitation in (theirs, uuid4()):  # la de otra organización, como la que no hay
        assert reply(revoke(luis, invitation)) == NOT_FOUND
        assert reply(revoke(ana, invitation)) == NOT_FOUND
    assert reply(revoke(ana, made["plana"], org="org-b")) == NOT_FOUND
    assert state(migrator)[2] == before[2] and {row[2] for row in rows(migrator)} == {"PENDING"}
    assert revoke(luis, made["plana"]).status_code == 200
    assert revoke(luis, gone).status_code == 200
    for name in ("ancha", "sensible", "owner"):
        assert revoke(ana, made[name]).status_code == 200, name
    assert stored(migrator, theirs)[0] == "PENDING"


def test_only_a_pending_invitation_changes_and_only_to_revoked(
    rbac: Any, migrator: psycopg.Connection[Any]
) -> None:
    ana = signed(rbac.ana)
    made = {
        name: ask(ana, f"{name}@example.com", [rbac.target.pk]).json()["id"]
        for name in ("aceptada", "caducada", "pendiente")
    }
    migrator.execute(MARK, ["ACCEPTED", "1 day", None, "ACCEPTED", "aceptada@example.com"])
    migrator.execute(MARK, ["EXPIRED", "1 day", None, "EXPIRED", "caducada@example.com"])
    before = state(migrator)
    for name in ("aceptada", "caducada"):
        was = stored(migrator, made[name])
        assert refused(revoke(ana, made[name])) == (409, "INVALID_TRANSITION"), name
        assert stored(migrator, made[name]) == was
    for bad in ("PENDING", "ACCEPTED", "EXPIRED", "revoked", "", None, 1, ["REVOKED"]):
        answer = revoke(ana, made["pendiente"], bad)
        assert refused(answer) == (400, "VALIDATION_ERROR"), bad
        assert list(answer.json()["fields"]) == ["status"]
    url = f"/api/v1/o/org-a/invitations/{made['pendiente']}/status/"
    token = {"X-CSRFToken": ana.cookies["csrftoken"].value}
    assert ana.put(url, {}, "application/json", headers=token).status_code == 400
    for other in (ana.get, ana.post, ana.patch, ana.delete):
        assert reply(other(url, headers=token)) == DENIED
    assert ana.put(url, {"status": "REVOKED"}, "application/json").status_code == 403  # CSRF
    assert state(migrator) == before and stored(migrator, made["pendiente"])[0] == "PENDING"


def test_revoking_waits_for_the_rbac_lock_of_the_organization(
    rbac: Any, migrator: psycopg.Connection[Any]
) -> None:
    made = ask(signed(rbac.ana), "x@example.com", [rbac.target.pk]).json()["id"]

    def inviting(tenant: Any) -> None:
        invite(tenant, email="y@example.com", role_ids=[rbac.target.pk])

    def revoking(tenant: Any) -> None:
        revoke_invitation(tenant, invitation_id=UUID(made))

    changes = {"invita": (rbac.ana, inviting), "revoca": (rbac.ana, revoking)}
    assert race(rbac.a, changes, "invita") == {"invita": "ok", "revoca": "ok"}
    assert stored(migrator, made)[0] == "REVOKED" and len(rows(migrator)) == 2

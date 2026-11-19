"""F2-130b: `POST /api/v1/invitations/accept/` acepta una invitación con el secreto de su enlace
(ADR-020 §2, §4 y §6, con la excepción de §4: D-F2-14). Middleware, CSRF, sesión, límite de
intentos, `resolve_invitation` y los bloqueos reales, como `crm_app`."""

import json
import logging
import secrets
import threading
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from django.conf import settings as django_settings
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

from apps.access.models import MembershipRole, Role, RolePermission
from apps.access.services import clone_role_templates
from apps.accounts.models import User
from apps.accounts.services import AccountExists
from apps.members import services as members
from apps.organizations import services as organizations
from core.tenancy.context import TenantContext
from core.tenancy.scope import tenant_scope
from tests import test_anti_escalation, test_auth_api, test_authorization, test_invitation_preview
from tests.factories import TEST_PASSWORD, make_user
from tests.test_anti_escalation import EDIT
from tests.test_authorization import give
from tests.test_invitation_preview import PAST, REFUSED, ROWS, ask, digest, heads, link, said, wrong
from tests.test_memberships import ctx, join
from tests.test_self_context import signed

real_stack, browser = test_auth_api.real_stack, test_auth_api.browser
world, rbac = test_authorization.world, test_anti_escalation.rbac
lookups, scopes = test_invitation_preview.lookups, test_invitation_preview.scopes
roomy = test_invitation_preview.roomy  # 100 intentos: el límite se prueba aparte
pytestmark = pytest.mark.usefixtures("tenant_db")
ACCEPT = "/api/v1/invitations/accept/"
NEW = {"first_name": "Nora", "last_name": "Paz", "password": "una clave larga de Nora 2026"}
EXISTS = (409, {"code": "ACCOUNT_EXISTS", "message": "Ese correo ya tiene cuenta. Inicia sesión "
                "con ella y vuelve a abrir el enlace."})  # fmt: skip
DENIALS = (
    "SELECT actor_type, actor_id, entity_type, entity_id, metadata, result, identifier_hash"
    " FROM platform_audit_logs WHERE action = 'invitation.accept.denied' ORDER BY id"
)
STATE = (
    "SELECT (SELECT count(*) FROM users), (SELECT count(*) FROM organization_memberships),"
    " (SELECT count(*) FROM membership_roles), (SELECT count(*) FROM audit_logs)"
)
JOINED = "SELECT id, organization_id, status FROM organization_memberships WHERE user_id = %s"
AUDITED = (
    "SELECT actor_type, actor_id, entity_type, entity_id, changes, metadata, organization_id"
    " FROM audit_logs WHERE action = 'membership.activated'"
)
MEMBERS = (
    "SELECT email, organization_id FROM organization_memberships JOIN users ON users.id = user_id"
)


def take(client: Client, token: Any, *, address: str = "127.0.0.1", **extra: Any) -> Any:
    client.cookies["csrftoken"] = csrf = "t" * 32
    body = json.dumps({"token": token, **extra})
    headers = {"X-CSRFToken": csrf}
    return client.post(ACCEPT, body, "application/json", headers=headers, REMOTE_ADDR=address)


def reasons(migrator: psycopg.Connection[Any]) -> list[Any]:
    return [row[4] for row in migrator.execute(DENIALS).fetchall()]


def q(migrator: psycopg.Connection[Any], sql: str, *params: Any) -> list[Any]:
    return migrator.execute(sql, list(params)).fetchall()


def directly(org: UUID, secret: str, session: Any = None) -> Callable[[TenantContext], Any]:
    """Aceptar con el servicio, en el scope `SYSTEM` que abre la ruta."""
    _, invitation_id = organizations.find_invitation(digest(secret)) or (None, uuid4())
    how = {"session_user_id": session, **NEW}
    return lambda tenant: members.accept_invitation(tenant, invitation_id, digest(secret), **how)


def held(org: UUID, first: Callable[[TenantContext], Any], second: Callable[[], Any]) -> Any:
    """`first` acepta y retiene su transacción; `second`, en otro hilo, espera sus bloqueos."""
    wrote, release, out = threading.Event(), threading.Event(), dict[str, Any]()

    def run_first() -> None:
        try:
            with tenant_scope(TenantContext(org, "invitation_link")) as tenant:
                out["first"] = first(tenant)
                wrote.set()
                release.wait(timeout=10)
        finally:
            wrote.set()
            connection.close()

    def run_second() -> None:
        out["second"] = second()
        connection.close()

    one, two = threading.Thread(target=run_first), threading.Thread(target=run_second)
    one.start()
    assert wrote.wait(timeout=10) and "first" in out
    two.start()
    two.join(timeout=1)
    assert two.is_alive() and "second" not in out  # espera los bloqueos de la primera
    release.set()
    for thread in (one, two):
        thread.join(timeout=10)
    return out


@pytest.fixture
def team(rbac: Any) -> Any:
    """ana es Owner; luis invita sin serlo; tres roles vacíos para cambiarlos tras invitar."""
    give(rbac.a, rbac.m_luis, {"users.invite": None}, code="invitador")
    with tenant_scope(ctx(rbac.a)):
        extra = {c: Role.objects.create(code=c, name=c) for c in ("escala", "sensible", "borrar")}
    return SimpleNamespace(**vars(rbac), extra=extra)


def grant(org: UUID, role: Role, code: str, scope: str | None) -> None:
    with tenant_scope(ctx(org)):
        RolePermission.objects.create(
            role=role, permission_id=code, supports_scope=bool(scope), scope=scope
        )


def test_every_link_that_cannot_be_accepted_gets_the_preview_answer_and_changes_nothing(
    team: Any, browser: Client, migrator: psycopg.Connection[Any], caplog: Any
) -> None:
    t, target = team, [team.target.pk]
    join(t.b, t.ana)  # B no tiene rol Owner: no se puede garantizar nada
    give(t.a, t.m_eva, {"users.invite": None, "users.manage": None}, code="eva")
    mia = make_user(email="mia@example.com")
    lost = give(t.a, join(t.a, mia).pk, {"users.invite": None, "users.manage": None}, code="mia")
    leo = make_user(email="leo@example.com")  # nunca tuvo `users.manage`
    give(t.a, join(t.a, leo).pk, {"users.invite": None}, code="leo")
    for email in ("e@cliente.pe", "s@cliente.pe", "socio@cliente.pe"):
        make_user(email=email)  # con cuenta: aun así, un enlace que no vale es un 400, no un 409
    User.objects.create_superuser(email="staff@cliente.pe", password=TEST_PASSWORD)
    cases = {
        "malformed": "no es un secreto!",
        "unknown": secrets.token_urlsafe(32),
        "wrong_secret": wrong(link(t.a, "w@cliente.pe", t.ana, role_ids=target)),
        "expired": link(t.a, "e@cliente.pe", t.ana, role_ids=target, expires_at=PAST),
        "revoked": link(t.a, "r@cliente.pe", t.ana, role_ids=target, status="REVOKED"),
        "accepted": link(t.a, "a@cliente.pe", t.ana, status="ACCEPTED", accepted_at=PAST),
        "inviter_suspended": link(t.a, "s@cliente.pe", t.eva, role_ids=target),
        "inviter_lost_users_invite": link(t.a, "l@cliente.pe", mia, role_ids=target),
        "inviter_without_users_manage": link(t.a, "m@cliente.pe", leo, role_ids=target),
        "escalation": link(t.a, "x@cliente.pe", t.luis, role_ids=[t.extra["escala"].pk]),
        "sensitive": link(t.a, "y@cliente.pe", t.luis, role_ids=[t.extra["sensible"].pk]),
        "role_deleted": link(t.a, "d@cliente.pe", t.ana, role_ids=[t.extra["borrar"].pk]),
        "member": link(t.a, "socio@cliente.pe", t.ana, role_ids=target),
        "staff": link(t.a, "staff@cliente.pe", t.ana, role_ids=target),
        "no_owner_role": link(t.b, "b@cliente.pe", t.ana, role_ids=[uuid4()]),
    }
    # Después de invitar: quien invitó pierde lo que tenía, o el rol gana lo que no cubre.
    db = migrator.execute
    db("UPDATE organization_memberships SET status = 'SUSPENDED' WHERE id = %s", [t.m_eva])
    db(
        "DELETE FROM role_permissions WHERE role_id = %s AND permission_code = 'users.invite'",
        [lost.pk],
    )
    grant(t.a, t.extra["escala"], EDIT, "ORGANIZATION")
    grant(t.a, t.extra["sensible"], "users.manage", None)  # luis lo tiene, pero no es Owner
    db("DELETE FROM roles WHERE id = %s", [t.extra["borrar"].pk])
    join(t.a, User.objects.get(email="socio@cliente.pe"), "DEACTIVATED")
    preview = ask(browser, "x")
    before, rows = migrator.execute(STATE).fetchone(), migrator.execute(ROWS).fetchall()
    caplog.set_level(logging.DEBUG)
    answers = {case: take(Client(), token, **NEW) for case, token in cases.items()}
    for case, answer in answers.items():
        assert (answer.content, heads(answer)) == (preview.content, heads(preview)), case
    assert said(preview) == REFUSED
    assert migrator.execute(STATE).fetchone() == before  # ni cuenta, ni membresía, ni rol
    assert migrator.execute(ROWS).fetchall() == rows
    denials = migrator.execute(DENIALS).fetchall()
    said_why = [":".join(v for _, v in sorted(row[4].items(), reverse=True)) for row in denials]
    assert said_why == ["malformed"] + ["unknown"] * 5 + [
        "inviter:membership", "inviter:permission", "inviter:permission", "inviter:escalation",
        "inviter:sensitive", "role", "member", "account", "inviter:last_owner",
    ]  # fmt: skip
    assert {row[:2] + row[5:] for row in denials} == {("ANONYMOUS", None, "DENIED", None)}
    assert [row[2] for row in denials] == [None] * 6 + ["invitation"] * 9
    logged = caplog.text + json.dumps([vars(r) for r in caplog.records], default=str)
    stored = json.dumps(denials, default=str)
    for token in cases.values():
        assert token not in logged + stored and digest(token) not in logged + stored
    for value in ("@cliente.pe", NEW["password"], "Nora"):
        assert value not in logged + stored


def test_a_new_account_joins_with_the_invited_roles_and_nothing_logs_in(
    team: Any, migrator: psycopg.Connection[Any], scopes: list[Any], caplog: Any
) -> None:
    roles = sorted([team.target.pk, team.roles["seller"].pk])
    secret = link(team.a, "nora@cliente.pe", team.ana, role_ids=roles)
    eva = signed(team.eva)  # con la sesión de otra cuenta: el enlace prueba el buzón
    caplog.set_level(logging.DEBUG)
    answer = take(eva, secret, organization_id=str(team.b), slug="org-b", **NEW)  # se ignoran
    assert said(answer) == (200, {"organization_name": "Org A", "account_created": True})
    assert answer.headers["Cache-Control"] == "no-store"
    assert django_settings.SESSION_COOKIE_NAME not in answer.cookies  # no inicia sesión
    (scope,) = scopes
    assert (scope.organization_id, scope.actor_type, scope.user_id) == (team.a, "SYSTEM", None)
    nora = User.objects.get(email="nora@cliente.pe")
    assert nora.password.startswith("argon2$argon2id$") and nora.check_password(NEW["password"])
    assert [nora.first_name, nora.last_name, nora.password_change_required] == ["Nora", "Paz", 0]
    joined = q(migrator, JOINED, nora.pk)
    assert [row[1:] for row in joined] == [(team.a, "ACTIVE")]
    given = q(
        migrator, "SELECT role_id FROM membership_roles WHERE membership_id = %s", joined[0][0]
    )
    assert sorted(row[0] for row in given) == roles
    (invitation,) = q(migrator, "SELECT id, status, token_hash, accepted_at FROM user_invitations")
    assert invitation[1:3] == ("ACCEPTED", None) and invitation[3] is not None
    (row,) = q(migrator, AUDITED)
    assert row[:5] == ("SYSTEM", None, "membership", joined[0][0], {"status": [None, "ACTIVE"]})
    assert row[5:] == (
        {"via": "invitation", "invitation_id": str(invitation[0]), "user_id": str(nora.pk),
         "user_created": True, "invited_by": str(team.ana.pk), "role_ids": list(map(str, roles))},
        team.a,
    )  # fmt: skip
    assert [row[1] for row in q(migrator, MEMBERS) if row[0] == "eva@example.com"] == [team.a]
    assert User.objects.get(pk=team.eva.pk).check_password(TEST_PASSWORD)
    assert said(take(Client(), secret, **NEW)) == REFUSED  # el enlace vale una vez
    assert reasons(migrator) == [{"reason": "unknown"}]
    logged = caplog.text + json.dumps([vars(r) for r in caplog.records], default=str)
    for value in (secret, digest(secret), "nora@cliente.pe", NEW["password"], "Org A"):
        assert value not in logged


def test_the_new_account_needs_a_name_and_a_password_the_platform_accepts(
    team: Any, migrator: psycopg.Connection[Any]
) -> None:
    secret = link(team.a, "nora@cliente.pe", team.ana, role_ids=[team.target.pk])
    before, rows = migrator.execute(STATE).fetchone(), migrator.execute(ROWS).fetchall()
    weak = take(Client(), secret, first_name=" ", password="nora@cliente.pe")  # noqa: S106
    status, body = said(weak)
    assert (status, body["code"]) == (400, "VALIDATION_ERROR")
    assert sorted(body["fields"]) == ["first_name", "password"]
    assert b"nora@cliente" not in weak.content  # ni el correo ni la contraseña
    assert said(take(Client(), secret))[1]["code"] == "VALIDATION_ERROR"  # sin contraseña
    assert migrator.execute(STATE).fetchone() == before
    assert migrator.execute(ROWS).fetchall() == rows  # el enlace no se gasta
    assert reasons(migrator) == [{"reason": "password"}] * 2
    assert said(take(Client(), secret, **NEW))[0] == 200


def test_an_existing_account_accepts_only_with_its_own_session_and_keeps_its_password(
    team: Any, browser: Client, migrator: psycopg.Connection[Any]
) -> None:
    ines = make_user(email="ines@cliente.pe")
    secret = link(team.a, "ines@cliente.pe", team.ana, role_ids=[team.target.pk])
    for client in (Client(), signed(team.eva)):  # sin sesión, o con la de otra cuenta
        assert said(take(client, secret, **NEW)) == EXISTS
    accepted = said(take(signed(ines), secret, **NEW))
    assert accepted == (200, {"organization_name": "Org A", "account_created": False})
    ines.refresh_from_db()
    assert ines.check_password(TEST_PASSWORD) and ines.first_name == ""  # no se toca
    dora_session = signed(dora := make_user(email="dora@cliente.pe"))
    deactivated = link(team.a, "dora@cliente.pe", team.ana, role_ids=[team.target.pk])
    User.objects.filter(pk=dora.pk).update(is_active=False)
    assert said(take(dora_session, deactivated, **NEW)) == EXISTS  # nunca se reactiva
    with pytest.raises(AccountExists), tenant_scope(TenantContext(team.a, "invitation_link")) as t:
        directly(team.a, deactivated, dora.pk)(t)  # ni con su id como sesión
    late = link(team.a, "tarde@cliente.pe", team.ana, role_ids=[team.target.pk])
    assert said(ask(browser, late))[1]["needs_account"] is True
    made = make_user(email="tarde@cliente.pe")  # la cuenta nace después de la vista previa
    assert said(take(Client(), late, **NEW)) == EXISTS
    assert User.objects.get(pk=made.pk).check_password(TEST_PASSWORD)  # su contraseña sigue
    joined = {row[0] for row in q(migrator, MEMBERS)}
    assert "ines@cliente.pe" in joined and not {"dora@cliente.pe", "tarde@cliente.pe"} & joined
    denials = migrator.execute(DENIALS).fetchall()
    assert [(row[0], row[1], row[4]) for row in denials] == [
        ("ANONYMOUS", None, {"reason": "account_exists"}),
        ("USER", team.eva.pk, {"reason": "account_exists"}),
        ("ANONYMOUS", None, {"reason": "account_exists"}),  # una cuenta desactivada no tiene sesión
        ("ANONYMOUS", None, {"reason": "account_exists"}),
    ]


def test_two_accepts_of_one_link_wait_for_each_other_and_only_one_joins(
    team: Any, migrator: psycopg.Connection[Any]
) -> None:
    secret = link(team.a, "nora@cliente.pe", team.ana, role_ids=[team.target.pk])
    out = held(team.a, directly(team.a, secret), lambda: take(Client(), secret, **NEW))
    assert out["first"] == ("Org A", True)
    assert said(out["second"]) == REFUSED
    assert reasons(migrator) == [{"reason": "stale"}]  # la encontró, y al bloquearla ya no valía
    assert [row[1] for row in q(migrator, MEMBERS) if row[0] == "nora@cliente.pe"] == [team.a]


def test_two_organizations_creating_the_same_account_at_once_leave_one_account(
    team: Any, migrator: psycopg.Connection[Any]
) -> None:
    with tenant_scope(ctx(team.b)) as scope:
        owner_b = next(role for role in clone_role_templates(scope) if role.is_owner_role)
        MembershipRole.objects.create(membership_id=join(team.b, team.ana).pk, role=owner_b)
    in_a = link(team.a, "carla@cliente.pe", team.ana, role_ids=[team.target.pk])
    in_b = link(team.b, "carla@cliente.pe", team.ana, role_ids=[owner_b.pk])
    out = held(team.a, directly(team.a, in_a), lambda: take(Client(), in_b, **NEW))
    assert out["first"] == ("Org A", True)
    assert said(out["second"]) == EXISTS  # chocó con `users.email` y se deshizo entera
    assert q(migrator, "SELECT count(*) FROM users WHERE email = 'carla@cliente.pe'") == [(1,)]
    assert [row[1] for row in q(migrator, MEMBERS) if row[0] == "carla@cliente.pe"] == [team.a]
    left = "SELECT status, token_hash FROM user_invitations WHERE organization_id = %s"
    assert q(migrator, left, team.b) == [("PENDING", digest(in_b))]
    assert reasons(migrator) == [{"reason": "account_exists"}]


def test_a_row_that_changed_after_the_lookup_is_not_accepted_and_locks_follow_the_rbac_order(
    team: Any, migrator: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = link(team.a, "nora@cliente.pe", team.ana, role_ids=[team.target.pk])
    found = organizations.find_invitation(digest(secret))
    monkeypatch.setattr(organizations, "find_invitation", lambda hashed: found)
    other = link(team.a, "otra@cliente.pe", team.ana, role_ids=[team.target.pk])
    assert said(take(Client(), other, **NEW)) == REFUSED  # su hash no es el de la fila
    change = "UPDATE user_invitations SET {} WHERE token_hash = %s"
    for stale, back in (
        ("status = 'REVOKED'", "status = 'PENDING'"),
        ("expires_at = now() - interval '1 second'", "expires_at = now() + interval '1 day'"),
    ):
        migrator.execute(change.format(stale), [digest(secret)])
        assert said(take(Client(), secret, **NEW)) == REFUSED
        migrator.execute(change.format(back), [digest(secret)])
    suspend = "UPDATE organizations SET status = %s WHERE id = %s"
    migrator.execute(suspend, ["SUSPENDED", team.a])
    assert said(take(Client(), secret, **NEW)) == REFUSED
    migrator.execute(suspend, ["ACTIVE", team.a])
    assert reasons(migrator) == [{"reason": "stale"}] * 4
    with CaptureQueriesContext(connection) as seen:
        with tenant_scope(TenantContext(team.a, "invitation_link")) as tenant:
            assert directly(team.a, secret)(tenant) == ("Org A", True)
    locks = [x["sql"] for x in seen.captured_queries if "FOR NO KEY UPDATE" in x["sql"]]
    assert ['FROM "roles"' in locks[0], 'FROM "user_invitations"' in locks[1]] == [True, True]


def test_preview_and_accept_share_the_limit_per_address_and_without_csrf_nothing_counts(
    team: Any, browser: Client, settings: Any, lookups: list[str], migrator: psycopg.Connection[Any]
) -> None:
    settings.INVITATION_LINK_THROTTLE = (3, 900)
    secret = link(team.a, "nora@cliente.pe", team.ana, role_ids=[team.target.pk])
    bare = Client(enforce_csrf_checks=True)
    answer = bare.post(ACCEPT, json.dumps({"token": secret, **NEW}), "application/json")
    assert said(answer)[0] == 403 and said(answer)[1]["code"] == "CSRF_FAILED"
    assert lookups == [] and q(migrator, "SELECT count(*) FROM login_throttles") == [(0,)]
    assert said(ask(browser, "x"))[0] == 400
    assert said(take(Client(), "y"))[0] == 400
    assert said(ask(browser, secret))[0] == 200
    refused = take(Client(), secret, **NEW)  # el cuarto intento desde esta dirección
    assert refused.status_code == 429 and 0 < int(refused.headers["Retry-After"]) <= 900
    assert len(lookups) == 3  # cuenta antes de buscar
    assert q(migrator, "SELECT key FROM login_throttles") == [("rst:inv:ip:127.0.0.1",)]
    assert said(take(Client(), secret, address="198.51.100.7", **NEW))[0] == 200

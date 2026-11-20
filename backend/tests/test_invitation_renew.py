"""F2-130e: invitar otra vez a un correo con invitación pendiente la renueva, y una ruta la reenvía
(ADR-020 §1 y §3); el tope diario por correo (decisión del dueño) y si salió el correo. Rutas,
tarea de envío, bloqueos reales y PostgreSQL como `crm_app`; el correo, en memoria."""

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from django.core import mail as sent
from django.core.mail import EmailMessage
from django.db import OperationalError
from django.test import Client

from apps.access.models import Role
from apps.members import invitation_links, invitation_mail
from apps.members import services as members
from apps.organizations import services as organizations
from core import mail
from core.tenancy.scope import tenant_scope
from tests import test_invitation_send as sending
from tests.factories import make_user
from tests.test_anti_escalation import audit
from tests.test_authorization import give
from tests.test_invitation_accept import NEW, held, take
from tests.test_invitation_preview import REFUSED, said
from tests.test_invitation_preview import ask as preview
from tests.test_invitation_send import EVENTS, invited, run, secret_in, send_now
from tests.test_invitations_list import listed
from tests.test_invitations_table import invite as stored
from tests.test_invite import ask
from tests.test_memberships import ctx, join
from tests.test_self_context import NOT_FOUND, reply, signed

world, rbac, origin = sending.world, sending.rbac, sending.origin
real_stack, browser = sending.real_stack, sending.browser
pytestmark = pytest.mark.usefixtures("tenant_db")
DENIED = (403, b'{"code":"PERMISSION_DENIED"}')
ROW = (
    "SELECT role_ids::text[], invited_by_user_id, expires_at, token_hash IS NOT NULL,"
    " send_count, sent_at, cardinality(send_times), delivered_at IS NOT NULL"
    " FROM user_invitations WHERE id = %s"
)
AGE = (  # todo lo enviado, `by` antes
    "UPDATE user_invitations SET sent_at = sent_at - %(by)s::interval, delivered_at ="
    " delivered_at - %(by)s::interval, send_times = ARRAY(SELECT t - %(by)s::interval"
    " FROM unnest(send_times) AS t)"
)
SOON = datetime.now(UTC)  # una fecha cualquiera, pasada al usarla
TOTALS = "SELECT (SELECT count(*) FROM user_invitations), (SELECT count(*) FROM audit_logs)"


def age(migrator: psycopg.Connection[Any], by: str = "16 minutes") -> None:
    migrator.execute(AGE, {"by": by})  # y los pedidos: lo servido sigue siendo posterior
    migrator.execute("UPDATE outbox_events SET occurred_at = occurred_at - %s::interval", [by])


def row(migrator: psycopg.Connection[Any], invitation: Any) -> Any:
    return migrator.execute(ROW, [invitation]).fetchone()


def resend(client: Client, invitation: Any, org: str = "org-a", csrf: bool = True) -> Any:
    client.cookies["csrftoken"] = token = "t" * 32
    path = f"/api/v1/o/{org}/invitations/{invitation}/resend/"
    return client.post(path, headers={"X-CSRFToken": token} if csrf else {})


def totals(migrator: psycopg.Connection[Any]) -> Any:
    return (*(migrator.execute(TOTALS).fetchone() or ()), len(migrator.execute(EVENTS).fetchall()))


def code(answer: Any) -> tuple[int, str]:
    return answer.status_code, answer.json()["code"]


def event_of(migrator: psycopg.Connection[Any], invitation: UUID, which: int = 0) -> UUID:
    rows = [row[0] for row in migrator.execute(EVENTS).fetchall() if row[3] == invitation]
    return UUID(str(rows[which]))  # -1: el último pedido


def fails(error: type[Exception]) -> Any:
    def failing(*args: Any, **kwargs: Any) -> None:
        raise error("falla")

    return failing


def delivery(client: Client) -> dict[str, str]:
    return {item["email"]: item["delivery"] for item in listed(client).json()["results"]}


@pytest.fixture
def other(rbac: Any) -> Role:
    with tenant_scope(ctx(rbac.a)):
        role: Role = Role.objects.create(code="otro", name="Otro rol")
    return role


def test_renewing_kills_the_old_link_at_once_and_only_the_new_one_opens(
    rbac: Any, migrator: psycopg.Connection[Any], browser: Client, other: Role
) -> None:
    invitation, _ = send_now(rbac, migrator)
    old, first = secret_in(sent.outbox[0]), row(migrator, invitation)
    age(migrator)
    give(rbac.a, rbac.m_luis, {"users.invite": None})
    answer = ask(signed(rbac.luis), "Nueva@Example.com", [other.pk])
    assert answer.status_code == 200  # renovada, no creada
    body = {k: v for k, v in answer.json().items() if k in ("id", "role_ids", "send_count")}
    assert body == {"id": str(invitation), "role_ids": [str(other.pk)], "send_count": 1}
    assert answer.json()["delivery"] == "NOT_SENT" and answer.json()["status"] == "PENDING"
    now = row(migrator, invitation)
    assert now[0] == [str(other.pk)] and now[1] == rbac.luis.pk and now[2] > first[2]
    assert now[3:5] == (False, 1)  # sin enlace hasta que la tarea emita otro; la cuenta sigue
    assert said(preview(browser, old)) == REFUSED  # el enlace viejo muere al momento
    (*_, changes, metadata, actor) = audit(migrator)[-1]
    assert audit(migrator)[-1][:3] == ("membership.invited", "invitation", invitation)
    assert changes["role_ids"] == [[str(rbac.target.pk)], [str(other.pk)]]
    assert changes["invited_by"] == [str(rbac.ana.pk), str(rbac.luis.pk)] and actor == rbac.luis.pk
    assert metadata == {"renewal": True, "email": "nueva@example.com"}
    assert run(rbac.a, invitation, event_of(migrator, invitation)) == "served"  # el pedido viejo
    assert run(rbac.a, invitation, event_of(migrator, invitation, -1)) == "sent"
    new = secret_in(sent.outbox[-1])
    assert len(sent.outbox) == 2 and new != old
    assert (row(migrator, invitation)[4], row(migrator, invitation)[6]) == (2, 2)
    assert said(preview(browser, old)) == REFUSED and preview(browser, new).status_code == 200
    assert said(take(Client(), old, **NEW)) == REFUSED
    assert take(Client(), new, **NEW).status_code == 200  # con los roles de la renovación
    joined = "SELECT 1 FROM membership_roles WHERE role_id = %s"
    assert migrator.execute(joined, [other.pk]).fetchone()
    for leaked in (old, new, sending.digest(old), sending.digest(new)):
        assert leaked not in sending.everything(migrator)


def test_renewing_needs_what_inviting_needs_and_the_roles_it_had(
    rbac: Any, migrator: psycopg.Connection[Any]
) -> None:
    manager = give(rbac.a, rbac.membership, {"users.manage": None}, code="gestor")  # sensible
    ana, luis, eva = signed(rbac.ana), signed(rbac.luis), signed(rbac.eva)
    invitation = UUID(ask(ana, "x@example.com", [manager.pk]).json()["id"])
    give(rbac.a, rbac.m_luis, {"users.invite": None})
    before = totals(migrator)
    assert reply(ask(eva, "x@example.com", [rbac.target.pk])) == DENIED  # sin permisos
    assert reply(ask(luis, "x@example.com", [manager.pk])) == DENIED  # no cubre el nuevo
    assert reply(ask(luis, "x@example.com", [rbac.target.pk])) == DENIED  # ni el que tenía
    assert reply(resend(luis, invitation)) == DENIED
    assert totals(migrator) == before and row(migrator, invitation)[1] == rbac.ana.pk
    assert ask(ana, "x@example.com", [rbac.target.pk]).status_code == 200
    assert resend(luis, invitation).status_code == 200  # ahora cubre lo que tiene
    assert row(migrator, invitation)[1] == rbac.luis.pk


def test_a_renewal_is_one_more_send_and_the_fifth_is_the_last(
    rbac: Any, migrator: psycopg.Connection[Any]
) -> None:
    invitation = invited(rbac)
    migrator.execute(
        "UPDATE user_invitations SET send_count = 4, sent_at = now() - interval '2 days',"
        " send_times = ARRAY[now() - interval '2 days']"
    )
    ana = signed(rbac.ana)
    assert resend(ana, invitation).status_code == 200
    assert row(migrator, invitation)[4] == 4  # renovar no pone la cuenta a cero
    assert run(rbac.a, invitation, event_of(migrator, invitation, -1)) == "sent"
    assert row(migrator, invitation)[4] == 5
    age(migrator)
    before = totals(migrator)
    assert code(resend(ana, invitation)) == (409, "INVITATION_SEND_LIMIT")
    assert code(ask(ana, "nueva@example.com", [rbac.target.pk])) == (409, "INVITATION_SEND_LIMIT")
    assert totals(migrator) == before and row(migrator, invitation)[4] == 5


def test_within_fifteen_minutes_of_a_send_renewing_and_resending_are_a_429(
    rbac: Any, migrator: psycopg.Connection[Any]
) -> None:
    invitation, _ = send_now(rbac, migrator)
    ana, before = signed(rbac.ana), totals(migrator)
    for answer in (resend(ana, invitation), ask(ana, "nueva@example.com", [rbac.target.pk])):
        assert code(answer) == (429, "INVITATION_SEND_TOO_SOON")
        assert 14 * 60 < int(answer.headers["Retry-After"]) <= 15 * 60
    assert totals(migrator) == before and row(migrator, invitation)[3] is True
    age(migrator)
    assert resend(ana, invitation).status_code == 200


def test_an_organization_sends_one_address_at_most_three_invitations_a_day(
    rbac: Any, migrator: psycopg.Connection[Any]
) -> None:
    ana = signed(rbac.ana)
    first, _ = send_now(rbac, migrator)
    age(migrator)
    assert resend(ana, first).status_code == 200
    assert run(rbac.a, first, event_of(migrator, first, -1)) == "sent"
    age(migrator)
    with tenant_scope(ctx(rbac.a, rbac.ana)) as tenant:
        members.revoke_invitation(tenant, invitation_id=first)
    second, outcome = send_now(rbac, migrator)  # revocar e invitar otra vez: cuenta igual
    assert outcome == "sent" and code(resend(ana, second))[1] == "INVITATION_ADDRESS_LIMIT"
    age(migrator)
    before = totals(migrator)
    for answer in (resend(ana, second), ask(ana, "nueva@example.com", [rbac.target.pk])):
        assert code(answer) == (429, "INVITATION_ADDRESS_LIMIT")  # el cuarto del día
        wait = int(answer.headers["Retry-After"])
        assert 24 * 3600 - 48 * 60 - 60 < wait <= 24 * 3600 - 48 * 60  # cuando salga el primero
    assert ask(ana, "otra@example.com", [rbac.target.pk]).status_code == 201  # otro correo, sí
    assert totals(migrator)[0] == before[0] + 1
    age(migrator, "24 hours")
    assert resend(ana, second).status_code == 200  # pasado el día


def test_the_task_counts_the_daily_sends_again_under_the_rbac_lock(
    rbac: Any, migrator: psycopg.Connection[Any]
) -> None:
    invitation = invited(rbac)  # un pedido que se coló: entretanto salieron tres a ese correo
    hours = [datetime.now(UTC) - timedelta(hours=n) for n in (23, 2, 1)]
    revoked = {"status": "REVOKED", "send_count": 3, "sent_at": hours[-1], "send_times": hours}
    stored(rbac.a, "nueva@example.com", rbac.ana, **revoked)
    assert run(rbac.a, invitation, event_of(migrator, invitation)) == "address_limit"
    assert sent.outbox == [] and row(migrator, invitation)[3:5] == (False, 0)
    age(migrator, "1 hour 1 minute")  # el de hace 23 horas sale de la cuenta
    assert run(rbac.a, invitation, event_of(migrator, invitation)) == "sent"


def test_resending_renews_with_the_same_roles_and_refuses_what_cannot_be_sent(
    rbac: Any, migrator: psycopg.Connection[Any], browser: Client, other: Role
) -> None:
    ana = signed(rbac.ana)
    invitation, _ = send_now(rbac, migrator)
    old = secret_in(sent.outbox[0])
    age(migrator)
    assert resend(ana, invitation, csrf=False).status_code == 403  # sin CSRF, nada
    answer = resend(ana, invitation)
    assert answer.status_code == 200 and answer.json()["role_ids"] == [str(rbac.target.pk)]
    assert said(preview(browser, old)) == REFUSED
    assert reply(resend(ana, uuid4())) == NOT_FOUND
    stranger = make_user(email="de-b@example.com")
    join(rbac.b, stranger)
    theirs = stored(rbac.b, "z@example.com", stranger)
    assert reply(resend(ana, theirs.pk)) == NOT_FOUND  # la de otra organización no existe aquí
    ended: tuple[dict[str, Any], ...] = (
        {"status": "REVOKED"},
        {"status": "ACCEPTED", "accepted_at": SOON},
    )
    for n, done in enumerate(ended):
        gone = stored(rbac.a, f"{n}@example.com", rbac.ana, role_ids=[rbac.target.pk], **done)
        assert code(resend(ana, gone.pk)) == (409, "INVALID_TRANSITION")
    deleted = stored(rbac.a, "rol@example.com", rbac.ana, role_ids=[other.pk])
    migrator.execute("DELETE FROM roles WHERE id = %s", [other.pk])
    assert code(resend(ana, deleted.pk)) == (409, "INVITATION_ROLE_GONE")
    join(rbac.a, make_user(email="nueva@example.com"), "SUSPENDED")
    age(migrator)
    assert code(resend(ana, invitation)) == (409, "ALREADY_MEMBER")


def test_the_list_says_whether_the_last_link_went_out(
    rbac: Any, migrator: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    ana = signed(rbac.ana)
    invited(rbac, "cola@example.com")
    ok, _ = send_now(rbac, migrator, "bien@example.com")
    assert row(migrator, ok)[7] is True
    with monkeypatch.context() as patched:
        patched.setattr(EmailMessage, "send", fails(OSError))
        with pytest.raises(mail.MailError):
            send_now(rbac, migrator, "rebota@example.com")
    with monkeypatch.context() as patched:  # la base falla al anotar: queda sin entregar
        patched.setattr(organizations, "invitation_delivered", fails(OperationalError))
        unnoted, _ = send_now(rbac, migrator, "sin-nota@example.com")
    shown = ("NOT_SENT", "SENT", "NOT_DELIVERED", "NOT_DELIVERED")  # cola, bien, rebota, sin-nota
    assert list(delivery(ana).values()) == list(reversed(shown))
    with tenant_scope(ctx(rbac.a)) as tenant:  # un enlace nuevo no consta entregado
        organizations.store_invitation_link(tenant, invitation_id=ok, token_hash="1" * 64)
    assert row(migrator, ok)[7] is False
    age(migrator)
    assert resend(ana, ok).json()["delivery"] == "NOT_SENT"  # espera su enlace nuevo
    for hashed in (
        "0" * 64,
        sending.digest(secret_in(sent.outbox[-1])),
    ):  # la nota, del enlace vigente
        with tenant_scope(ctx(rbac.a)) as tenant:
            organizations.invitation_delivered(tenant, invitation_id=unnoted, token_hash=hashed)
        assert row(migrator, unnoted)[7] is (hashed != "0" * 64)
    for change in ("delivered_at = now(), sent_at = NULL", "send_times = send_times || now()"):
        with pytest.raises(psycopg.errors.CheckViolation):
            migrator.execute(f"UPDATE user_invitations SET {change} WHERE id = %s", [ok])  # noqa: S608


def test_a_lost_commit_acknowledgement_ends_served_and_shows_not_delivered(
    rbac: Any, migrator: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    real, lost = tenant_scope, list[bool]()

    @contextmanager
    def unacknowledged(tenant: Any) -> Any:
        with real(tenant) as scope:
            yield scope
        if not lost:  # el COMMIT se confirmó, pero el acuse no llegó
            lost.append(True)
            raise OperationalError("server closed the connection unexpectedly")

    monkeypatch.setattr(invitation_mail, "tenant_scope", unacknowledged)
    invitation, outcome = send_now(rbac, migrator)
    assert outcome == "served" and sent.outbox == []  # el reintento lo encuentra servido
    assert row(migrator, invitation)[3:5] == (True, 1)  # el enlace cuenta y no salió
    assert delivery(signed(rbac.ana)) == {"nueva@example.com": "NOT_DELIVERED"}


def test_a_hash_that_collides_is_retried_with_a_new_secret(
    rbac: Any, migrator: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    taken, _ = send_now(rbac, migrator, "primera@example.com")
    clash, issued = secret_in(sent.outbox[0]), list[str]()

    def colliding() -> str:
        issued.append(clash if not issued else invitation_links.new_secret())
        return issued[-1]

    monkeypatch.setattr(invitation_mail, "new_secret", colliding)
    invitation, outcome = send_now(rbac, migrator)
    assert outcome == "sent" and len(issued) == 2  # el índice único lo rechazó; otro secreto
    assert secret_in(sent.outbox[-1]) == issued[1] != clash
    assert row(migrator, invitation)[3:5] == (True, 1) and row(migrator, taken)[3:5] == (True, 1)


def test_a_renewal_and_an_accept_wait_for_each_other_in_the_rbac_order(
    rbac: Any, migrator: psycopg.Connection[Any], other: Role
) -> None:
    invitation, _ = send_now(rbac, migrator)
    secret = secret_in(sent.outbox[0])
    age(migrator)
    renew = lambda tenant: members.invite(tenant, email="nueva@example.com", role_ids=[other.pk])  # noqa: E731
    out = held(rbac.a, renew, lambda: take(Client(), secret, **NEW), by=rbac.ana)
    assert said(out["second"]) == REFUSED  # la renovación mató el enlace que traía
    age(migrator)
    again = lambda tenant: members.resend_invitation(tenant, invitation_id=invitation)  # noqa: E731
    out = held(
        rbac.a, again, lambda: run(rbac.a, invitation, event_of(migrator, invitation)), by=rbac.ana
    )
    assert out["second"] == "served"  # esperó el bloqueo de RBAC; el pedido viejo ya se sirvió
    assert run(rbac.a, invitation, event_of(migrator, invitation, -1)) == "sent"
    assert row(migrator, invitation)[0] == [str(other.pk)]  # el enlace, con los roles nuevos
    age(migrator)
    revoke = lambda tenant: members.revoke_invitation(tenant, invitation_id=invitation)  # noqa: E731
    out = held(rbac.a, revoke, lambda: resend(signed(rbac.ana), invitation), by=rbac.ana)
    assert code(out["second"]) == (409, "INVALID_TRANSITION")

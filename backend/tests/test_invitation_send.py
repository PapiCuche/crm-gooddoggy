"""F2-130d: el correo de una invitación (ADR-020 §3, ADR-019 §4). Invitar deja un evento del outbox
con solo identificadores; su handler encola la tarea que genera el secreto, guarda su SHA-256,
confirma y solo entonces envía. Middleware, outbox, tareas en proceso y PostgreSQL como `crm_app`;
el correo, en memoria (`locmem`)."""

import hashlib
import re
import threading
import time
from smtplib import SMTPException
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from django.core import mail as sent
from django.core.exceptions import ImproperlyConfigured
from django.core.mail import EmailMessage
from django.db import OperationalError, connection
from django.test import Client

from apps.access.services import lock_rbac
from apps.members import invitation_links, invitation_mail, tasks
from apps.organizations import services as organizations
from apps.organizations.models import UserInvitation
from core import mail
from core.outbox import handlers_for
from core.outbox.publisher import publish_outbox
from core.tenancy.context import TenantContext
from core.tenancy.scope import tenant_scope
from tests import test_anti_escalation, test_auth_api, test_authorization, test_self_context
from tests.factories import make_user
from tests.logs import log_text
from tests.test_invitation_accept import waiting_for_a_lock
from tests.test_invitation_preview import ask as preview
from tests.test_invitations_table import invite as stored_invitation
from tests.test_invite import ask as invite_route
from tests.test_memberships import join
from tests.test_self_context import signed

world, rbac = test_authorization.world, test_anti_escalation.rbac
real_stack, browser = test_self_context.real_stack, test_auth_api.browser
pytestmark = pytest.mark.usefixtures("tenant_db")
ORIGIN = "https://app.example.com"
LINK = re.compile(r"^https://app\.example\.com/invitacion#([A-Za-z0-9_-]{43})$", re.M)
EVENTS = (
    "SELECT id, event_type, aggregate_type, aggregate_id, payload::text, organization_id"
    " FROM outbox_events ORDER BY occurred_at, id"
)
ROW = (
    "SELECT status, token_hash, send_count, sent_at IS NOT NULL FROM user_invitations WHERE id = %s"
)
SET = "UPDATE user_invitations SET {} WHERE id = %s"
SEND = "invitation.send_requested"


@pytest.fixture(autouse=True)
def origin(settings: Any) -> None:
    settings.APP_ORIGIN = ORIGIN
    settings.INVITATION_LINK_THROTTLE = (100, 900)


def invited(rbac: Any, email: str = "nueva@example.com") -> UUID:
    answer = invite_route(signed(rbac.ana), email, [rbac.target.pk])
    assert answer.status_code == 201, answer.content
    return UUID(answer.json()["id"])


def event_of(migrator: psycopg.Connection[Any], invitation: UUID) -> UUID:
    (event,) = [row[0] for row in migrator.execute(EVENTS).fetchall() if row[3] == invitation]
    return UUID(str(event))


def run(org: UUID, invitation: UUID, event: UUID) -> Any:
    ids = {"organization_id": str(org), "invitation_id": str(invitation), "event_id": str(event)}
    return tasks.send_invitation.apply(kwargs=ids).get()


def send_now(rbac: Any, migrator: psycopg.Connection[Any], email: str = "nueva@example.com") -> Any:
    invitation = invited(rbac, email)
    return invitation, run(rbac.a, invitation, event_of(migrator, invitation))


def digest(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def secret_in(message: Any) -> str:
    (secret,) = LINK.findall(message.body)
    return str(secret)


def row(migrator: psycopg.Connection[Any], invitation: UUID) -> Any:
    return migrator.execute(ROW, [invitation]).fetchone()


def everything(migrator: psycopg.Connection[Any]) -> str:
    """Lo que queda en la auditoría (de la organización y de plataforma) y en el outbox."""
    tables = ("audit_logs", "platform_audit_logs", "outbox_events")
    return " ".join(str(migrator.execute(f"SELECT * FROM {t}").fetchall()) for t in tables)  # noqa: S608


def test_inviting_queues_one_send_with_ids_only_and_nothing_else_does(
    rbac: Any, migrator: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    ana = signed(rbac.ana)
    assert invite_route(ana, "eva@example.com", [rbac.target.pk]).status_code == 409  # miembro
    assert migrator.execute(EVENTS).fetchall() == []
    invitation = invited(rbac)
    ((event, *rest),) = migrator.execute(EVENTS).fetchall()
    assert rest == [SEND, "invitation", invitation, "{}", rbac.a]  # sin correo ni secreto
    path = f"/api/v1/o/org-a/invitations/{invitation}/status/"
    headers = {"X-CSRFToken": ana.cookies["csrftoken"].value}
    revoked = ana.put(path, {"status": "REVOKED"}, "application/json", headers=headers)
    assert revoked.status_code == 200
    assert len(migrator.execute(EVENTS).fetchall()) == 1  # revocar no pide un envío
    # El handler, suscrito, solo encola la tarea de envío con identificadores.
    assert tasks.invitation_event in handlers_for(SEND)
    queued: list[Any] = []
    published: list[Any] = []  # el handler corre después, en su worker: fuera de este scope
    monkeypatch.setattr(tasks.invitation_event, "apply_async", lambda **kw: published.append(kw))
    monkeypatch.setattr(tasks.send_invitation, "apply_async", lambda **kw: queued.append(kw))
    assert publish_outbox() == 1
    (handled,) = published
    assert tasks.invitation_event.apply(**handled).successful()
    ids = {"organization_id": str(rbac.a), "invitation_id": str(invitation), "event_id": str(event)}
    assert queued == [{"kwargs": ids}]
    assert sent.outbox == []  # el handler no envía nada dentro de su transacción


def test_the_task_sends_one_link_keeps_only_its_hash_and_the_preview_opens_it(
    rbac: Any,
    migrator: psycopg.Connection[Any],
    browser: Client,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("DEBUG")
    invitation, outcome = send_now(rbac, migrator)
    assert outcome == "sent"
    (message,) = sent.outbox
    secret = secret_in(message)
    assert invitation_links.SECRET.fullmatch(secret)
    assert message.to == ["nueva@example.com"]
    assert message.subject == "Te invitaron a Good Doggy CRM"  # fijo: nada de la organización
    assert "«Org A»" in message.body and "7 días y una sola vez" in message.body
    assert "?" not in message.body.split(ORIGIN, 1)[1].split()[0]  # el secreto, en el fragmento
    assert "ana@example.com" not in message.body  # ni quién invitó
    assert row(migrator, invitation) == ("PENDING", digest(secret), 1, True)
    # Ida y vuelta: el enlace emitido lo abre la vista previa (F2-130a).
    answer = preview(browser, secret)
    assert answer.status_code == 200
    assert answer.json() == {
        "organization_name": "Org A",
        "email": "nueva@example.com",
        "needs_account": True,
    }
    # El secreto y su hash no quedan en logs, auditoría ni outbox.
    for leaked in (secret, digest(secret)):
        assert leaked not in log_text(caplog)
        assert leaked not in everything(migrator)
    assert "invitation.sent" in log_text(caplog)


def test_one_generator_with_the_shape_the_preview_accepts() -> None:
    secrets = {invitation_links.new_secret() for _ in range(200)}
    assert len(secrets) == 200
    assert all(invitation_links.SECRET.fullmatch(secret) for secret in secrets)


def test_the_same_event_twice_sends_one_email(rbac: Any, migrator: psycopg.Connection[Any]) -> None:
    invitation = invited(rbac)
    event = event_of(migrator, invitation)
    assert run(rbac.a, invitation, event) == "sent"
    first = row(migrator, invitation)
    assert run(rbac.a, invitation, event) == "served"  # una entrega repetida del outbox
    assert len(sent.outbox) == 1 and row(migrator, invitation) == first


def test_an_event_that_is_not_this_invitations_request_sends_nothing(
    rbac: Any, migrator: psycopg.Connection[Any]
) -> None:
    mine = invited(rbac)
    other = invited(rbac, "otra@example.com")
    before = row(migrator, mine)
    assert run(rbac.a, mine, event_of(migrator, other)) == "event"
    assert run(rbac.a, mine, uuid4()) == "event"
    assert run(rbac.b, mine, event_of(migrator, mine)) == "event"  # el de A no se ve desde B
    assert sent.outbox == [] and row(migrator, mine) == before


SKIPPED = {  # lo que se hace a la invitación (o a su alrededor) → la razón por la que no se envía
    "revoked": ("state", "status = 'REVOKED'"),
    "accepted": ("state", "status = 'ACCEPTED', accepted_at = now()"),
    "expired": ("state", "expires_at = now() - interval '1 second'"),
    "suspended": ("state", None),
    "limit": ("limit", "send_count = 5"),
    "member": ("member", None),
    "inviter": ("inviter", None),
    "role": ("inviter", None),
    "address": ("address", "email = 'sin@punto'"),
    "too_soon": ("too_soon", None),
}


@pytest.mark.parametrize("case", list(SKIPPED))
def test_nothing_is_sent_for_an_invitation_that_should_not_get_one(
    case: str, rbac: Any, migrator: psycopg.Connection[Any]
) -> None:
    reason, change = SKIPPED[case]
    invitation = invited(rbac)
    if change:
        migrator.execute(SET.format(change), [invitation])
    elif case == "suspended":
        migrator.execute("UPDATE organizations SET status = 'SUSPENDED' WHERE id = %s", [rbac.a])
    elif case == "member":
        join(rbac.a, make_user(email="nueva@example.com"), "DEACTIVATED")
    elif case == "inviter":
        migrator.execute(
            "UPDATE organization_memberships SET status = 'SUSPENDED' WHERE id = %s",
            [rbac.membership],
        )
    elif case == "role":
        migrator.execute(SET.format("role_ids = ARRAY[%s::uuid]"), [uuid4(), invitation])
    else:  # otra invitación de esta organización al mismo correo, enviada hace 14 minutos
        stored_invitation(rbac.a, "nueva@example.com", rbac.ana, status="REVOKED", send_count=1)
        migrator.execute(
            "UPDATE user_invitations SET sent_at = now() - interval '14 minutes'"
            " WHERE status = 'REVOKED'"
        )
    before = row(migrator, invitation)
    assert run(rbac.a, invitation, event_of(migrator, invitation)) == reason
    assert sent.outbox == [] and row(migrator, invitation) == before
    assert before[1] is None  # sin enlace


def test_fifteen_minutes_later_or_from_another_organization_it_is_sent(
    rbac: Any, migrator: psycopg.Connection[Any]
) -> None:
    other = make_user(email="de-b@example.com")
    join(rbac.b, other)
    stored_invitation(rbac.b, "nueva@example.com", other, send_count=1)  # de otra organización
    stored_invitation(rbac.a, "nueva@example.com", rbac.ana, status="REVOKED", send_count=1)
    migrator.execute("UPDATE user_invitations SET sent_at = now()")  # la de B, recién
    migrator.execute(
        "UPDATE user_invitations SET sent_at = now() - interval '16 minutes'"
        " WHERE organization_id = %s",
        [rbac.a],
    )
    assert send_now(rbac, migrator)[1] == "sent"


def test_a_fifth_send_is_the_last(rbac: Any, migrator: psycopg.Connection[Any]) -> None:
    invitation = invited(rbac)
    migrator.execute(SET.format("send_count = 4"), [invitation])
    assert run(rbac.a, invitation, event_of(migrator, invitation)) == "sent"
    assert row(migrator, invitation)[2] == 5


def test_inviting_the_same_address_again_within_fifteen_minutes_is_a_429(
    rbac: Any, migrator: psycopg.Connection[Any]
) -> None:
    ana = signed(rbac.ana)
    unsent = invited(rbac, "x@example.com")  # sin enviar: no cuenta
    migrator.execute(SET.format("status = 'REVOKED'"), [unsent])
    assert invite_route(ana, "x@example.com", [rbac.target.pk]).status_code == 201
    invitation, outcome = send_now(rbac, migrator)
    assert outcome == "sent"
    migrator.execute(SET.format("status = 'REVOKED'"), [invitation])
    rows, events = len(migrator.execute(ROW.split(" WHERE")[0]).fetchall()), len(sent.outbox)
    answer = invite_route(ana, "Nueva@Example.com", [rbac.target.pk])
    assert (answer.status_code, answer.json()["code"]) == (429, "INVITATION_SEND_TOO_SOON")
    assert 14 * 60 < int(answer.headers["Retry-After"]) <= 15 * 60
    assert len(migrator.execute(ROW.split(" WHERE")[0]).fetchall()) == rows  # ni fila
    assert len(migrator.execute(EVENTS).fetchall()) == 3 and len(sent.outbox) == events
    migrator.execute(SET.format("sent_at = now() - interval '15 minutes 1 second'"), [invitation])
    assert invite_route(ana, "nueva@example.com", [rbac.target.pk]).status_code == 201


def test_a_failed_delivery_keeps_the_counted_link_and_is_not_retried(
    rbac: Any,
    migrator: psycopg.Connection[Any],
    browser: Client,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("DEBUG")
    issued: list[str] = []

    def spy() -> str:
        issued.append(invitation_links.new_secret())
        return issued[-1]

    monkeypatch.setattr(invitation_mail, "new_secret", spy)

    def refuse(*args: Any, **kwargs: Any) -> None:
        raise SMTPException("550 nueva@example.com " + issued[0])

    monkeypatch.setattr(EmailMessage, "send", refuse)
    invitation = invited(rbac)
    with pytest.raises(mail.MailError) as error:
        run(rbac.a, invitation, event_of(migrator, invitation))
    assert str(error.value) == "SMTPException" and error.value.__context__ is None
    (secret,) = issued
    assert row(migrator, invitation) == ("PENDING", digest(secret), 1, True)  # confirmado antes
    assert preview(browser, secret).status_code == 200  # vale, aunque no llegó
    assert secret not in log_text(caplog) and secret not in everything(migrator)
    assert not tasks.send_invitation.acks_late  # y no se reintentó: un solo secreto


@pytest.mark.parametrize(
    "missing", [{"APP_ORIGIN": ""}, {"MAIL_BACKEND": "smtp", "EMAIL_HOST": ""}]
)
def test_without_app_origin_or_mail_nothing_is_issued(
    missing: dict[str, str], rbac: Any, migrator: psycopg.Connection[Any], settings: Any
) -> None:
    invitation = invited(rbac)
    for name, value in missing.items():
        setattr(settings, name, value)
    with pytest.raises(ImproperlyConfigured, match="APP_ORIGIN y el correo"):
        run(rbac.a, invitation, event_of(migrator, invitation))
    assert row(migrator, invitation) == ("PENDING", None, 0, False) and sent.outbox == []


def test_a_failure_before_the_commit_is_retried_and_one_after_it_is_not(
    rbac: Any, migrator: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    invitation = invited(rbac)
    event = event_of(migrator, invitation)
    tries: list[str] = []

    def broken(*args: Any, **kwargs: Any) -> None:
        tries.append("store")
        raise OperationalError("se cayó la conexión")

    with monkeypatch.context() as patched:  # la base falla antes de confirmar: sin enlace
        patched.setattr(organizations, "store_invitation_link", broken)
        with pytest.raises(invitation_mail.NotIssued, match="^OperationalError$"):
            run(rbac.a, invitation, event)
    assert len(tries) == 4  # el intento y 3 reintentos
    assert row(migrator, invitation) == ("PENDING", None, 0, False) and sent.outbox == []
    assert run(rbac.a, invitation, event) == "sent"  # nada se emitió: el evento sigue sin servir
    # El handler tampoco pierde el evento si el broker falla al encolar el envío.
    monkeypatch.setattr(tasks.send_invitation, "apply_async", broken)
    ids = {"event_id": str(event), "organization_id": str(rbac.a)}
    assert tasks.invitation_event.apply(kwargs=ids).failed() and len(tries) == 8


def test_the_task_takes_the_rbac_lock_before_the_row_like_revoke_and_accept(
    rbac: Any, migrator: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Quien escribe en el orden de invitar, revocar y aceptar (RBAC y después la fila) y la tarea,
    a la vez: con el orden al revés se interbloquean, y la base aborta a uno (el reintento de la
    tarea lo taparía: se cuenta cuántas veces lo intentó)."""
    invitation = invited(rbac)
    event = event_of(migrator, invitation)
    locked, go, out = threading.Event(), threading.Event(), dict[str, Any]()
    tries: list[UUID] = []
    real = invitation_mail._issue

    def counted(*args: Any) -> Any:
        tries.append(args[1])
        return real(*args)

    monkeypatch.setattr(invitation_mail, "_issue", counted)

    def writer() -> None:
        try:
            with tenant_scope(TenantContext(rbac.a, "test")) as tenant:
                lock_rbac(tenant)
                locked.set()
                go.wait(10)
                rows = UserInvitation.objects.select_for_update(no_key=True)
                out["row"] = rows.filter(pk=invitation).first() is not None
        except Exception as error:  # noqa: BLE001 — el interbloqueo, por su nombre
            out["writer"] = type(error).__name__
        finally:
            connection.close()

    def task() -> None:
        try:
            out["task"] = run(rbac.a, invitation, event)
        except Exception as error:  # noqa: BLE001
            out["task"] = type(error).__name__
        finally:
            connection.close()

    one, two = threading.Thread(target=writer), threading.Thread(target=task)
    one.start()
    assert locked.wait(10)
    two.start()
    waiting_for_a_lock(two)
    time.sleep(0.3)  # sin el bloqueo de RBAC, la tarea ya tendría la fila
    go.set()
    one.join(30)
    two.join(30)
    assert out == {"row": True, "task": "sent"} and tries == [invitation]

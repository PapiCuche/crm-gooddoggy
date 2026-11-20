"""F2-130g: lo enviado de una invitación no se deshace en la base (`user_invitations_sends_guard`,
para todos los roles), el tope diario por correo cuenta con el reloj de la base, reenviar espera el
bloqueo de RBAC antes de leer los roles, y una nota de entrega que falla no reintenta la tarea.
PostgreSQL como `crm_app`, con SQL a mano dentro del scope de su organización."""

from datetime import timedelta
from typing import Any

import psycopg
import pytest
from django.core import mail as sent
from django.db import IntegrityError, OperationalError, ProgrammingError
from django.utils import timezone

from apps.members import services as members
from apps.organizations import services as organizations
from core.tenancy.scope import tenant_scope
from tests import test_invitation_renew as renewing
from tests.logs import log_text
from tests.test_authorization import give
from tests.test_invitation_accept import held
from tests.test_invitation_preview import REFUSED, said
from tests.test_invitation_preview import ask as preview
from tests.test_invitation_renew import DENIED, age, code, delivery, event_of, resend, row
from tests.test_invitation_send import invited, run, secret_in, send_now
from tests.test_invitations_table import SENDS_GUARD
from tests.test_invitations_table import invite as stored
from tests.test_memberships import ctx, raw
from tests.test_self_context import reply, signed

world, rbac, origin = renewing.world, renewing.rbac, renewing.origin
real_stack, browser = renewing.real_stack, renewing.browser
pytestmark = pytest.mark.usefixtures("tenant_db")
UPDATE = "UPDATE user_invitations SET {} WHERE id = %s"
SENDS = "SELECT send_count, send_times, sent_at FROM user_invitations WHERE id = %s"
UNDONE = (  # bajar la cuenta, quitar, cambiar o anteponer horas, o borrar o adelantar el último
    "send_count = send_count - 1",
    "send_count = 0",
    "send_times = '{}'",
    "send_times = send_times[2:]",
    "send_times = send_times[1:1]",
    "send_times[2] = send_times[2] - interval '1 second'",
    "send_times = array_prepend(now(), send_times), send_count = send_count + 1",
    "sent_at = NULL",
    "sent_at = sent_at - interval '1 second'",
    "send_times = array_cat('[0:0]={2000-01-01 00:00:00+00}'::timestamptz[], send_times),"
    " send_count = send_count + 1",  # delante, con otros índices
    "email = 'otra@example.com'",  # el tope diario cuenta por correo
)
THREE = (  # tres envíos al mismo correo, con la hora de la base
    "UPDATE user_invitations SET sent_at = now() - interval '1 hour', send_times ="
    " ARRAY[now() - interval '23 hours', now() - interval '2 hours', now() - interval '1 hour']"
    " WHERE status = 'REVOKED'"
)


def sent_twice(rbac: Any, migrator: psycopg.Connection[Any]) -> Any:
    made = stored(rbac.a, "dos@example.com", rbac.ana, send_count=2)
    times = "ARRAY[now() - interval '2 hours', now() - interval '1 hour']"
    migrator.execute(UPDATE.format(f"send_times = {times}, sent_at = now()"), [made.pk])
    return made.pk


def sends(migrator: psycopg.Connection[Any], invitation: Any) -> Any:
    return migrator.execute(SENDS, [invitation]).fetchone()


def test_the_runtime_cannot_undo_a_send_but_can_add_one(
    rbac: Any, migrator: psycopg.Connection[Any]
) -> None:
    invitation = sent_twice(rbac, migrator)
    before = sends(migrator, invitation)
    for change in (*UNDONE, f"organization_id = '{rbac.b}'"):
        with pytest.raises(IntegrityError, match=SENDS_GUARD), tenant_scope(ctx(rbac.a)):
            raw(UPDATE.format(change), [invitation])
    assert sends(migrator, invitation) == before
    grown = "send_count = send_count + 1, send_times = send_times || now(), sent_at = now()"
    with tenant_scope(ctx(rbac.a)):
        assert raw(UPDATE.format(grown), [invitation]) == [(1,)]
        assert raw(UPDATE.format("status = 'REVOKED', token_hash = NULL"), [invitation]) == [(1,)]
        unsent = stored(rbac.a, "sin@example.com", rbac.ana).pk  # sin enviar, el correo sí cambia
        assert raw(UPDATE.format("email = 'otro@example.com'"), [unsent]) == [(1,)]
    once = stored(rbac.a, "una@example.com", rbac.ana, send_count=1).pk  # con un envío, ya no
    with pytest.raises(IntegrityError, match=SENDS_GUARD), tenant_scope(ctx(rbac.a)):
        raw(UPDATE.format("email = 'otro2@example.com'"), [once])
    count, times, _ = sends(migrator, invitation)
    assert count == 3 and times[:2] == before[1] and len(times) == 3


def test_the_owner_of_the_table_cannot_undo_a_send_either(
    rbac: Any, migrator: psycopg.Connection[Any]
) -> None:
    invitation = sent_twice(rbac, migrator)
    for change in (*UNDONE, f"organization_id = '{rbac.b}'"):
        with pytest.raises(psycopg.errors.CheckViolation, match=SENDS_GUARD):
            migrator.execute(UPDATE.format(change), [invitation])
    function = (
        "SELECT p.prosecdef, p.proconfig, t.tgenabled, t.tgtype FROM pg_trigger t"
        " JOIN pg_proc p ON p.oid = t.tgfoid WHERE t.tgname = %s"
    )
    found = migrator.execute(function, [SENDS_GUARD]).fetchone()
    # INVOKER, `search_path` fijo, siempre activo, y BEFORE UPDATE FOR EACH ROW (1 + 2 + 16)
    assert found == (False, ["search_path=pg_catalog, public, pg_temp"], "O", 19)


def test_the_runtime_cannot_switch_off_the_guard_nor_delete_nor_forge_on_insert(
    rbac: Any, migrator: psycopg.Connection[Any]
) -> None:
    invitation = sent_twice(rbac, migrator)
    escapes = (
        f"ALTER TABLE user_invitations DISABLE TRIGGER {SENDS_GUARD}",
        f"DROP TRIGGER {SENDS_GUARD} ON user_invitations",
        f"DROP FUNCTION {SENDS_GUARD}()",
        f"CREATE OR REPLACE FUNCTION {SENDS_GUARD}() RETURNS trigger LANGUAGE plpgsql"
        " AS 'BEGIN RETURN NEW; END'",
        "SET session_replication_role = replica",
        "DELETE FROM user_invitations",
    )
    for sql in escapes:
        with (
            pytest.raises(ProgrammingError, match="must be owner|permission denied"),
            tenant_scope(ctx(rbac.a)),
        ):
            raw(sql)
    for forged in ({"send_count": -1}, {"send_count": 1, "send_times": [timezone.now()] * 2}):
        with pytest.raises(IntegrityError, match="user_invitations_send_"):
            stored(rbac.a, "falsa@example.com", rbac.ana, **forged)
    assert migrator.execute("SELECT count(*) FROM user_invitations").fetchone() == (1,)
    assert sends(migrator, invitation)[0] == 2


@pytest.mark.parametrize("ahead", [timedelta(minutes=30), timedelta(hours=25)])
def test_the_daily_cap_counts_with_the_database_clock(
    rbac: Any, migrator: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch, ahead: Any
) -> None:
    invitation = invited(rbac)
    stored(rbac.a, "nueva@example.com", rbac.ana, status="REVOKED", send_count=3)
    migrator.execute(THREE)
    real = timezone.now
    monkeypatch.setattr(timezone, "now", lambda: real() + ahead)  # el reloj de la aplicación
    with (
        pytest.raises(organizations.AddressLimit) as refused,
        tenant_scope(ctx(rbac.a, rbac.ana)) as tenant,
    ):
        members.resend_invitation(tenant, invitation_id=invitation)
    assert 3600 - 60 < refused.value.wait <= 3600  # cuando salga el de hace 23 horas, en la base


def test_retry_after_never_passes_a_day_even_with_forged_future_sends(
    rbac: Any, migrator: psycopg.Connection[Any]
) -> None:
    invitation = invited(rbac)
    stored(rbac.a, "nueva@example.com", rbac.ana, status="REVOKED", send_count=3)
    migrator.execute(THREE.replace("now() - interval", "now() + interval"))
    answer = resend(signed(rbac.ana), invitation)
    assert answer.status_code == 429 and answer.headers["Retry-After"] == str(24 * 3600)


def test_the_gap_wait_never_passes_fifteen_minutes_even_with_a_forged_future_send(
    rbac: Any, migrator: psycopg.Connection[Any]
) -> None:
    invitation = invited(rbac)
    stored(rbac.a, "nueva@example.com", rbac.ana, status="REVOKED", send_count=1)
    migrator.execute("UPDATE user_invitations SET sent_at = now() + interval '1 year'")
    answer = resend(signed(rbac.ana), invitation)
    assert code(answer) == (429, "INVITATION_SEND_TOO_SOON")
    assert answer.headers["Retry-After"] == str(15 * 60)


@pytest.mark.parametrize(
    ("oldest", "low", "high"),
    [
        ("23 hours 59 minutes 50 seconds", 1, 10),
        ("24 hours 1 second", 0, 0),
        ("23 hours", 3540, 3600),
    ],
)
def test_the_daily_window_ends_24_hours_after_a_send_and_counts_only_that_address(
    rbac: Any, migrator: psycopg.Connection[Any], oldest: str, low: int, high: int
) -> None:
    stored(rbac.a, "nueva@example.com", rbac.ana, status="REVOKED", send_count=3)
    migrator.execute(THREE.replace("23 hours", oldest))
    with tenant_scope(ctx(rbac.a)) as tenant:
        waits = [
            organizations._address_wait(tenant, f"{who}@example.com") for who in ("nueva", "x")
        ]
    assert low <= waits[0] <= high and waits[1] == 0  # los de otro correo no cuentan


def test_a_database_clock_stepping_back_never_fails_a_legitimate_send(
    rbac: Any, migrator: psycopg.Connection[Any]
) -> None:
    invitation = invited(rbac)
    ahead = "now() + interval '5 seconds'"  # el último envío, con un reloj que iba adelantado
    migrator.execute(
        UPDATE.format(f"send_count = 1, send_times = ARRAY[{ahead}], sent_at = {ahead}"),
        [invitation],
    )
    before = sends(migrator, invitation)[2]
    with tenant_scope(ctx(rbac.a)) as tenant:
        organizations.store_invitation_link(tenant, invitation_id=invitation, token_hash="2" * 64)
    with tenant_scope(ctx(rbac.a)) as tenant:
        organizations.invitation_delivered(tenant, invitation_id=invitation, token_hash="2" * 64)
    count, times, sent_at = sends(migrator, invitation)
    assert (count, len(times), sent_at) == (2, 2, before)
    assert row(migrator, invitation)[7] is True  # entregado, no antes de enviarse


def test_a_lesser_admin_resending_waits_for_the_owner_renewal_and_is_refused(
    rbac: Any, migrator: psycopg.Connection[Any]
) -> None:
    invitation = invited(rbac)  # sin enviar: los 15 minutos no cuentan
    manager = give(rbac.a, rbac.membership, {"users.manage": None}, code="gestor")  # sensible
    give(rbac.a, rbac.m_luis, {"users.invite": None})
    luis = signed(rbac.luis)
    assert resend(luis, invitation).status_code == 200  # con el rol que tiene, sí puede
    renew = lambda tenant: members.invite(tenant, email="nueva@example.com", role_ids=[manager.pk])  # noqa: E731
    out = held(rbac.a, renew, lambda: resend(luis, invitation), by=rbac.ana)
    assert reply(out["second"]) == DENIED  # leyó los roles después del bloqueo: los del Owner
    assert row(migrator, invitation)[:2] == ([str(manager.pk)], rbac.ana.pk)


def test_when_the_delivery_note_fails_the_task_ends_sent_and_is_not_retried(
    rbac: Any,
    migrator: psycopg.Connection[Any],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    calls = list[str]()

    def failing(*args: Any, **kwargs: Any) -> None:
        calls.append("nota")
        raise OperationalError("falla")

    monkeypatch.setattr(organizations, "invitation_delivered", failing)
    invitation, outcome = send_now(rbac, migrator)
    assert outcome == "sent" and calls == ["nota"] and len(sent.outbox) == 1
    assert row(migrator, invitation)[3:5] == (True, 1) and row(migrator, invitation)[7] is False
    logged = log_text(caplog)
    assert "invitation.delivery_unrecorded" in logged and "nueva@example.com" not in logged


def test_a_renewal_whose_send_is_refused_leaves_no_link_and_shows_not_sent(
    rbac: Any, migrator: psycopg.Connection[Any], browser: Any
) -> None:
    invitation, _ = send_now(rbac, migrator)
    old, ana = secret_in(sent.outbox[0]), signed(rbac.ana)
    age(migrator)
    assert resend(ana, invitation).status_code == 200
    status = "UPDATE organizations SET status = %s WHERE id = %s"
    migrator.execute(status, ["SUSPENDED", rbac.a])
    assert run(rbac.a, invitation, event_of(migrator, invitation, -1)) == "state"
    migrator.execute(status, ["ACTIVE", rbac.a])
    assert row(migrator, invitation)[3:5] == (False, 1) and len(sent.outbox) == 1
    assert delivery(ana) == {"nueva@example.com": "NOT_SENT"}  # sin enlace: no se envió
    assert said(preview(browser, old)) == REFUSED

"""F2-87: emitir y enviar el enlace de recuperación de contraseña (ADR-021 §3 y §4). La tarea
`accounts.send_password_reset`, sin ruta todavía. PostgreSQL con el rol `crm_app`."""

import json
import logging
import re
import secrets
import threading
from datetime import timedelta
from typing import Any

import psycopg
import pytest
from django.core import mail as outbox
from django.core.exceptions import ImproperlyConfigured
from django.db import connection
from django.utils import timezone

from apps.accounts import recovery
from apps.accounts.models import PasswordReset, User
from apps.accounts.tasks import send_password_reset
from core.mail import MailError
from core.observability.logging import json_formatter
from core.tenancy.context import TenantContextError
from core.tenancy.scope import tenant_scope
from tests.factories import TEST_PASSWORD, make_user
from tests.test_memberships import ctx

pytestmark = pytest.mark.usefixtures("tenant_db")
ORIGIN = "https://app.example.com"
EMAIL = "ana@example.com"
LINK = re.compile(r"https://app\.example\.com/restablecer#([A-Za-z0-9_-]{43})\n")
AGE = (
    "UPDATE password_resets SET created_at = now() - %s::interval"
    " WHERE created_at > now() - %s::interval"
)
SPENT = "UPDATE password_resets SET used_at = now(), expires_at = created_at + interval '1 second'"
# El enlace más antiguo, a esa edad (y caducado, que es lo que estaría).
OLDEST = (
    "UPDATE password_resets SET created_at = now() - %s::interval, expires_at = now()"
    " WHERE id = (SELECT id FROM password_resets ORDER BY created_at LIMIT 1)"
)


@pytest.fixture(autouse=True)
def origin(settings: Any) -> None:
    settings.APP_ORIGIN = ORIGIN


def rows() -> list[PasswordReset]:
    return list(PasswordReset.objects.order_by("created_at"))


def test_it_issues_a_link_keeps_only_its_hash_and_mails_it_to_the_account(
    caplog: pytest.LogCaptureFixture,
) -> None:
    ana = make_user(email=EMAIL)
    before = timezone.now()
    with caplog.at_level(logging.DEBUG):
        assert recovery.issue_link(EMAIL) is True
    (row,) = rows()
    (sent,) = outbox.outbox
    assert (sent.to, sent.subject) == ([EMAIL], "Restablece tu contraseña de Good Doggy CRM")
    (secret,) = LINK.findall(str(sent.body))  # un solo enlace, con el secreto en el fragmento
    assert (row.user_id, row.token_hash, row.used_at) == (ana.pk, recovery.token_hash(secret), None)
    assert re.fullmatch(r"[0-9a-f]{64}", row.token_hash) and row.token_hash != secret
    assert timedelta(minutes=59, seconds=59) < row.expires_at - before <= timedelta(minutes=61)
    assert "60 minutos y una sola vez" in sent.body and "Si no fuiste tú" in sent.body
    logged = " ".join(json_formatter().format(record) for record in caplog.records)
    assert json.loads(json_formatter().format(caplog.records[-1]))["purpose"] == "password_reset"
    for private in (secret, row.token_hash, EMAIL, "ana@", str(ana.pk)):
        assert private not in logged + repr(caplog.records)  # ni el enlace ni de quién es


@pytest.mark.parametrize(
    ("made", "asked"),
    [
        ({"email": EMAIL}, "otra@example.com"),  # sin cuenta
        ({"email": EMAIL}, "ANA@example.com"),  # llega canónico: otra escritura no es la cuenta
        ({"email": EMAIL, "is_active": False}, EMAIL),
        ({"email": EMAIL, "staff": True}, EMAIL),
        ({"email": '"ab"@example.com'}, '"ab"@example.com'),  # `core.mail` no le escribiría
    ],
)
def test_it_issues_nothing_and_says_nothing_when_the_account_cannot_recover(
    made: dict[str, Any], asked: str
) -> None:
    if made.pop("staff", False):
        User.objects.create_superuser(password=TEST_PASSWORD, **made)
    else:
        make_user(**made)
    assert recovery.issue_link(asked) is False
    assert rows() == [] and outbox.outbox == []


def test_an_account_gets_five_links_a_day_two_minutes_apart(
    migrator: psycopg.Connection[Any],
) -> None:
    make_user(email=EMAIL)
    luis = make_user(email="luis@example.com")
    assert recovery.issue_link(EMAIL) is True
    assert recovery.issue_link(EMAIL) is False  # a menos de 2 minutos del anterior
    assert recovery.issue_link("luis@example.com") is True  # cada cuenta, lo suyo
    migrator.execute(AGE, ["119 seconds", "1 minute"])
    assert recovery.issue_link(EMAIL) is False  # todavía no
    migrator.execute(AGE, ["121 seconds", "5 minutes"])
    for _ in range(4):  # hasta cinco en 24 horas
        assert recovery.issue_link(EMAIL) is True
        migrator.execute(AGE, ["121 seconds", "1 minute"])
    assert PasswordReset.objects.exclude(user=luis).count() == 5
    assert recovery.issue_link(EMAIL) is False  # el sexto, no
    migrator.execute(SPENT)
    assert recovery.issue_link(EMAIL) is False  # usados o caducados: emitidos, cuentan igual
    migrator.execute(OLDEST, ["23 hours 59 minutes"])
    assert recovery.issue_link(EMAIL) is False
    migrator.execute(OLDEST, ["24 hours 1 minute"])
    assert recovery.issue_link(EMAIL) is True  # el de ayer ya no cuenta
    assert (
        len(outbox.outbox) == 7 and len({m.body for m in outbox.outbox}) == 7
    )  # cada uno, el suyo


def test_two_tasks_at_once_for_one_account_issue_a_single_link(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    make_user(email=EMAIL)
    together, real, results = threading.Barrier(2), secrets.token_urlsafe, list[bool]()

    def slow(size: int) -> str:
        try:  # sin el bloqueo de la cuenta, las dos llegan aquí antes de escribir
            together.wait(timeout=1)
        except threading.BrokenBarrierError:
            pass
        return real(size)

    def run() -> None:
        try:
            results.append(recovery.issue_link(EMAIL))
        finally:
            connection.close()

    monkeypatch.setattr(secrets, "token_urlsafe", slow)
    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert sorted(results) == [False, True] and len(rows()) == 1 and len(outbox.outbox) == 1


def test_a_failed_delivery_keeps_the_issued_link_and_fails_without_retrying(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    make_user(email=EMAIL)

    def refuse(*args: Any, **kwargs: Any) -> int:
        raise ConnectionRefusedError(61, f"no route to deliver {EMAIL}")

    monkeypatch.setattr("django.core.mail.backends.locmem.EmailBackend.send_messages", refuse)
    with caplog.at_level(logging.DEBUG), pytest.raises(MailError) as error:
        send_password_reset.run(EMAIL)
    assert EMAIL not in str(error.value) + " ".join(
        json_formatter().format(r) for r in caplog.records
    )
    assert len(rows()) == 1  # emitido: cuenta, se entregue o no (ADR-021 §3)
    assert recovery.issue_link(EMAIL) is False  # y otro intento enseguida no sale
    assert (send_password_reset.max_retries, send_password_reset.ignore_result) == (0, True)
    assert not send_password_reset.acks_late and not getattr(
        send_password_reset, "autoretry_for", ()
    )


def test_without_the_public_origin_nothing_is_issued_and_the_task_is_platform_only(
    settings: Any, orgs: dict[str, Any]
) -> None:
    make_user(email=EMAIL)
    with tenant_scope(ctx(orgs["A"])), pytest.raises(TenantContextError):
        send_password_reset.run(EMAIL)  # una tarea de plataforma no corre dentro de un tenant
    settings.APP_ORIGIN = ""
    for email in (EMAIL, "nadie@example.com"):  # antes de mirar la cuenta: igual para cualquiera
        with pytest.raises(ImproperlyConfigured, match="APP_ORIGIN"):
            send_password_reset.run(email)
    assert rows() == [] and outbox.outbox == []
    settings.APP_ORIGIN = ORIGIN
    send_password_reset.run(EMAIL)
    assert len(rows()) == 1 and len(outbox.outbox) == 1

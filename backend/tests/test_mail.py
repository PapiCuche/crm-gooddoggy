"""ADR-019: `core.mail`, la única salida de correo. Sin red: backend en memoria y un SMTP falso."""

import logging
import re
import smtplib
from pathlib import Path
from typing import Any

import pytest
from django.core import mail as outbox
from django.core.exceptions import ImproperlyConfigured

from core.mail import MailError, Message, send

TO, SUBJECT, BODY = "ana.lopez@cliente.pe", "Te invitaron a Acme", "Abre el enlace: https://x/y"
VALID = Message(TO, SUBJECT, BODY, "invitation")


def logged(caplog: pytest.LogCaptureFixture) -> str:
    """Todo lo que quedó en el log: el mensaje y sus campos."""
    return " ".join(f"{record.getMessage()} {record.__dict__}" for record in caplog.records)


def test_it_delivers_the_message_as_given_and_logs_only_the_purpose(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.DEBUG):
        send(VALID)
    (sent,) = outbox.outbox
    assert (sent.to, sent.cc, sent.bcc) == ([TO], [], [])
    assert (sent.from_email, sent.subject, sent.body) == ("no-reply@crm.test", SUBJECT, BODY)
    assert sent.content_subtype == "plain" and sent.attachments == []
    (record,) = [r for r in caplog.records if r.name == "core.mail"]
    assert (record.getMessage(), record.levelname) == ("mail.sent", "INFO")
    assert (record.purpose, record.to) == ("invitation", "[EMAIL]")  # type: ignore[attr-defined]
    for private in (TO, "ana.lopez", SUBJECT, BODY, "https://x/y"):
        assert private not in logged(caplog)


@pytest.mark.parametrize(
    "changes",
    [
        {"to": "ana@x.pe\nBcc: otro@x.pe"},  # inyección de cabeceras
        {"to": "ana@x.pe\r\n"},
        {"to": "ana@x.pe "},
        {"to": "ana@x.pe, otro@x.pe"},  # una sola dirección
        {"to": "ana@x.pe;otro@x.pe"},
        {"to": "Ana <ana@x.pe>"},  # sin nombre
        {"to": " ana@x.pe"},
        {"to": "ana@x.pe "},
        {"to": "ana"},
        {"to": "ana@"},
        {"to": ""},
        {"subject": "Hola\nBcc: otro@x.pe"},
        {"subject": "Hola\rmundo"},
        {"subject": "Hola\x00"},
        {"subject": "   "},
        {"subject": "s" * 151},
        {"body": ""},
        {"body": "  \n "},
        {"body": "b" * 20_001},
        {"body": "hola\x00"},
        {"purpose": "Invitation"},
        {"purpose": "invitation sent"},
        {"purpose": ""},
        {"purpose": "p" * 33},
        {"purpose": "invitation\n"},
    ],
)
def test_an_invalid_message_is_refused_before_anything_is_sent(changes: dict[str, str]) -> None:
    fields = {"to": TO, "subject": SUBJECT, "body": BODY, "purpose": "invitation", **changes}
    with pytest.raises(ValueError) as error:
        send(Message(**fields))
    assert outbox.outbox == []
    assert "otro@x.pe" not in str(error.value)  # el error no repite lo que llegó


def test_the_limits_are_inclusive() -> None:
    send(Message("a@b.pe", "s" * 150, "b" * 20_000, "p" + "x" * 31))
    assert len(outbox.outbox) == 1


@pytest.mark.parametrize(
    "failure",
    [
        smtplib.SMTPRecipientsRefused({TO: (550, b"no such user")}),
        smtplib.SMTPAuthenticationError(535, b"bad credentials for apikey"),
        smtplib.SMTPServerDisconnected(f"lost while sending to {TO}"),
        TimeoutError("timed out"),
        ConnectionRefusedError(61, "Connection refused"),
    ],
)
def test_a_failed_delivery_is_a_mail_error_that_names_no_one(
    failure: Exception, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def refuse(*args: Any, **kwargs: Any) -> int:
        raise failure

    monkeypatch.setattr("django.core.mail.backends.locmem.EmailBackend.send_messages", refuse)
    with caplog.at_level(logging.DEBUG), pytest.raises(MailError) as error:
        send(VALID)
    assert str(error.value) == type(failure).__name__
    assert error.value.__cause__ is None and error.value.__suppress_context__  # sin el original
    (record,) = [r for r in caplog.records if r.name == "core.mail"]
    assert (record.getMessage(), record.levelname) == ("mail.failed", "WARNING")
    assert (record.purpose, record.to, record.error) == (  # type: ignore[attr-defined]
        "invitation",
        "[EMAIL]",
        type(failure).__name__,
    )
    for private in (TO, SUBJECT, BODY, "apikey", "no such user"):
        assert private not in logged(caplog) + str(error.value)


def test_a_bug_is_not_dressed_up_as_a_delivery_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*args: Any, **kwargs: Any) -> int:
        raise KeyError("un fallo nuestro")

    monkeypatch.setattr("django.core.mail.backends.locmem.EmailBackend.send_messages", broken)
    with pytest.raises(KeyError):
        send(VALID)


@pytest.mark.parametrize(
    "configuration",
    [
        {"MAIL_BACKEND": "smtp", "EMAIL_HOST": ""},  # sin servidor
        {"DEFAULT_FROM_EMAIL": ""},  # sin remitente
        {"MAIL_BACKEND": "smtp", "EMAIL_HOST": "smtp.example.com", "DEFAULT_FROM_EMAIL": ""},
        {"MAIL_BACKEND": "console"},  # solo los dos conocidos
        {"MAIL_BACKEND": "django.core.mail.backends.filebased.EmailBackend"},
    ],
)
def test_without_configuration_nothing_is_sent_and_it_says_so(
    configuration: dict[str, str], settings: Any
) -> None:
    for name, value in configuration.items():
        setattr(settings, name, value)
    with pytest.raises(ImproperlyConfigured):
        send(VALID)
    assert outbox.outbox == []


def test_smtp_uses_the_configured_server_with_a_timeout(
    settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: dict[str, Any] = {}

    class FakeSMTP:
        def __init__(self, host: str, port: int, **options: Any) -> None:
            calls["connect"] = (host, port, options)

        def ehlo(self) -> None: ...

        def starttls(self, **options: Any) -> None:
            calls["starttls"] = True

        def login(self, user: str, password: str) -> None:
            calls["login"] = user

        def sendmail(self, sender: str, recipients: list[str], data: bytes) -> dict[str, Any]:
            calls["envelope"] = (sender, recipients)
            calls["data"] = data
            return {}

        def quit(self) -> None:
            calls["quit"] = True

    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    settings.MAIL_BACKEND, settings.EMAIL_HOST, settings.EMAIL_PORT = (
        "smtp",
        "smtp.example.com",
        2525,
    )
    settings.EMAIL_USE_TLS, settings.EMAIL_HOST_USER, settings.EMAIL_HOST_PASSWORD = True, "u", "p"
    settings.EMAIL_TIMEOUT = 10
    send(VALID)
    host, port, options = calls["connect"]
    assert (host, port, options["timeout"]) == ("smtp.example.com", 2525, 10)
    assert calls["starttls"] and calls["login"] == "u" and calls["quit"]
    assert calls["envelope"] == ("no-reply@crm.test", [TO])  # un solo destinatario en el sobre
    assert b"Bcc" not in calls["data"] and b"Subject: Te invitaron a Acme" in calls["data"]


def test_outbound_mail_only_through_core_mail() -> None:
    """Como `core.http` con los clientes HTTP: ningún otro módulo abre una conexión de correo."""
    pattern = re.compile(
        r"^\s*(import|from)\s+(smtplib|django\.core\.mail|django\.core\s+import\s+.*\bmail\b)", re.M
    )
    root = Path(__file__).resolve().parents[1]
    offenders = [
        str(path.relative_to(root)) for folder in ("core", "apps", "config")
        for path in (root / folder).rglob("*.py")
        if path != root / "core" / "mail.py" and pattern.search(path.read_text())
    ]  # fmt: skip
    assert offenders == []
    assert pattern.search("from django.core import mail") and pattern.search("import smtplib")

"""ADR-019: `core.mail`, la única salida de correo. Sin red: backend en memoria y un SMTP falso."""

import json
import logging
import re
import smtplib
from pathlib import Path
from typing import Any

import pytest
from django.core import mail as outbox
from django.core.exceptions import ImproperlyConfigured

from core.mail import MailError, Message, send
from core.observability.logging import json_formatter

TO, SUBJECT, BODY = "ana.lopez@cliente.pe", "Te invitaron a Acme", "Abre el enlace: https://x/y"
VALID = Message(to=TO, subject=SUBJECT, body=BODY, purpose="invitation")


def logged(caplog: pytest.LogCaptureFixture) -> str:
    """Todo lo que quedó en el log: el mensaje y sus campos."""
    return " ".join(f"{record.getMessage()} {record.__dict__}" for record in caplog.records)


def line(caplog: pytest.LogCaptureFixture) -> dict[str, Any]:
    """La línea que sale por stdout, con el formatter de LOGGING: un campo que no está ahí no
    se registró (el `extra` de `logging` no llega)."""
    (record,) = [r for r in caplog.records if r.name == "core.mail"]
    return json.loads(json_formatter().format(record))  # type: ignore[no-any-return]


def test_it_delivers_the_message_as_given_and_logs_only_the_purpose(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.DEBUG):
        send(VALID)
    (sent,) = outbox.outbox
    assert (sent.to, sent.cc, sent.bcc) == ([TO], [], [])
    assert (sent.from_email, sent.subject, sent.body) == ("no-reply@crm.test", SUBJECT, BODY)
    assert sent.content_subtype == "plain" and sent.attachments == []
    sent_line = line(caplog)
    assert (sent_line["event"], sent_line["level"]) == ("mail.sent", "info")
    assert (sent_line["purpose"], sent_line["to"]) == ("invitation", "[EMAIL]")
    for private in (TO, "ana.lopez", SUBJECT, BODY, "https://x/y"):
        assert private not in logged(caplog) + json.dumps(sent_line)


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
        {"to": '"ana"@x.pe'},  # el transporte reescribe después de validar: sale `ana@x.pe`
        {"to": "=?utf-8?b?YW5h?=@x.pe"},  # palabra codificada: sale `ana@x.pe`
        {"to": "=?utf-8?q?otro=40y.pe?=@x.pe"},  # sale `"otro@y.pe"@x.pe`
        {"to": "ana@straße.de"},  # IDNA la manda a `strasse.de`: el dominio llega ya codificado
        {"to": "anſ@x.pe"},  # una de las letras no ASCII que el validador de Django admite
        {"subject": "Hola\nBcc: otro@x.pe"},
        {"subject": "Hola\rmundo"},
        {"subject": "Hola\x00"},
        {"subject": "Hola\x0cBcc: otro@x.pe"},  # Python la rechazaría con un error sin mapear
        {"subject": "Hola\u2028Bcc: otro@x.pe"},  # separador de línea Unicode
        {"subject": "   "},
        {"subject": "s" * 151},
        {"subject": "Hola\ud800"},  # no se puede codificar: fallaría con la conexión ya abierta
        {"body": "hola\ud800"},
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
    assert type(error.value) is ValueError  # el de `core.mail`, no uno de Django o del códec
    assert outbox.outbox == []
    # El error no repite lo que llegó: su texto sí acaba en el log de la tarea que falla.
    assert not any(value in str(error.value) for value in changes.values() if len(value) > 3)


def test_a_message_is_built_by_name_and_its_repr_shows_only_the_purpose() -> None:
    with pytest.raises(TypeError):  # por posición, un cuerpo podría acabar donde va el propósito
        Message(TO, SUBJECT, BODY, "invitation")  # type: ignore[misc]
    assert repr(VALID) == "Message(purpose='invitation')"
    for private in (TO, SUBJECT, BODY):
        assert private not in repr(VALID) + str(VALID)


def test_the_limits_are_inclusive() -> None:
    send(Message(to="a@b.pe", subject="s" * 150, body="b" * 20_000, purpose="p" + "x" * 31))
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
    assert error.value.__cause__ is None and error.value.__context__ is None  # sin el original
    failed = line(caplog)
    assert (failed["event"], failed["level"]) == ("mail.failed", "warning")
    assert (failed["purpose"], failed["to"]) == ("invitation", "[EMAIL]")
    assert failed["error"] == type(failure).__name__
    for private in (TO, SUBJECT, BODY, "apikey", "no such user"):
        assert private not in logged(caplog) + json.dumps(failed) + str(error.value)


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


@pytest.mark.parametrize("to", [TO, "o'brien+x@x.pe", "{a}=b%c.pe@xn--and-6ma2c.pe", "-a@x.pe"])
def test_smtp_uses_the_configured_server_with_a_timeout(
    to: str, settings: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
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
    with caplog.at_level(logging.DEBUG):
        send(Message(to=to, subject=SUBJECT, body=BODY, purpose="invitation"))
    host, port, options = calls["connect"]
    assert (host, port, options["timeout"]) == ("smtp.example.com", 2525, 10)
    assert calls["starttls"] and calls["login"] == "u" and calls["quit"]
    # Un solo destinatario, y el mismo en el sobre, en la cabecera y en lo que se validó.
    assert calls["envelope"] == ("no-reply@crm.test", [to])
    assert b"Bcc" not in calls["data"] and b"Subject: Te invitaron a Acme" in calls["data"]
    assert f"\r\nTo: {to}\r\n".encode() in calls["data"]
    # Ninguna forma de dirección queda en el log, ni entera ni a medias (`{a}=b%c.pe@…`).
    assert line(caplog)["to"] == "[EMAIL]" and to.split("@")[0] not in logged(caplog)


def test_a_failure_of_the_smtp_backend_is_a_mail_error_too(
    settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Con el backend real: uno con `fail_silently` daría por enviado lo que no salió."""

    def refuse(*args: Any, **kwargs: Any) -> None:
        raise ConnectionRefusedError(61, "Connection refused")

    monkeypatch.setattr(smtplib, "SMTP", refuse)
    settings.MAIL_BACKEND, settings.EMAIL_HOST = "smtp", "smtp.example.com"
    with pytest.raises(MailError, match="ConnectionRefusedError"):
        send(VALID)


def test_outbound_mail_only_through_core_mail() -> None:
    """Como `core.http` con los clientes HTTP: ningún otro módulo abre una conexión de correo."""
    pattern = re.compile(
        r"^\s*(import|from)\s+"
        r"(smtplib|django\.core\.mail|django\.core\s+import\s+(\([^)]*|.*)\bmail\b)",
        re.M,
    )
    root = Path(__file__).resolve().parents[1]
    offenders = [
        str(path.relative_to(root)) for folder in ("core", "apps", "config")
        for path in (root / folder).rglob("*.py")
        if path != root / "core" / "mail.py" and pattern.search(path.read_text())
    ]  # fmt: skip
    assert offenders == []
    alive = ("import smtplib", "from django.core.mail import x", "from django.core import a, mail")
    assert all(pattern.search(form) for form in (*alive, "from django.core import (\n    mail,\n)"))
    assert not pattern.search("from django.core import (\n    signing,\n)\nmail = 1")

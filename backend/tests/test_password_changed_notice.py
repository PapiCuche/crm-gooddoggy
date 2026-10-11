"""F2-93: el aviso por correo de que la contraseña cambió con un enlace de recuperación
(ADR-021, Consecuencias). La ruta lo encola con el cambio confirmado; la tarea
`accounts.send_password_changed` lo envía. PostgreSQL con el rol `crm_app`."""

import logging
from typing import Any
from uuid import uuid4

import pytest
from django.contrib.auth import authenticate
from django.core import mail as outbox
from django.db import connection
from django.test import Client

from apps.accounts import recovery
from apps.accounts.models import User
from apps.accounts.tasks import send_password_changed
from core import mail as core_mail
from core.mail import MailError
from core.observability.logging import json_formatter
from tests import test_auth_api
from tests.factories import TEST_PASSWORD, make_user
from tests.test_auth_api import reply
from tests.test_password_reset_change import EMAIL, NEW, REFUSED, change, issued, said

real_stack, browser = test_auth_api.real_stack, test_auth_api.browser
pytestmark = pytest.mark.usefixtures("tenant_db")
ORIGIN = "https://app.example.com"


@pytest.fixture(autouse=True)
def origin(settings: Any) -> None:
    settings.APP_ORIGIN = ORIGIN


@pytest.fixture
def queued(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Lo que se encola, sin broker: los argumentos de cada `apply_async`, y si en ese momento
    el cambio ya estaba confirmado."""
    calls: list[dict[str, Any]] = []

    def enqueue(connection: Any, **how: Any) -> None:  # con una conexión propia, que no espera
        options = connection.transport_options
        assert (connection.connect_timeout, options["max_retries"]) == (recovery.BROKER_WAIT, 0)
        assert options["socket_connect_timeout"] == options["socket_timeout"] == 3
        assert not connection.connected  # perezosa: aquí nadie abrió nada
        calls.append({**how, "committed": not globals()["connection"].in_atomic_block})

    monkeypatch.setattr(send_password_changed, "apply_async", enqueue)
    return calls


def test_a_change_queues_the_notice_for_the_account_once_it_is_committed(
    browser: Client, queued: list[dict[str, Any]]
) -> None:
    ana = make_user(email=EMAIL)
    secret = issued()
    assert said(change(browser, "a" * 43)) == REFUSED  # un enlace que no vale no avisa a nadie
    assert change(browser, secret, "corta").status_code == 400 and queued == []  # ni un rechazo
    assert reply(change(browser, secret)) == (204, b"")
    assert queued == [
        {
            "args": [str(ana.pk)],  # el identificador de la cuenta: su correo no va al broker
            "expires": 24 * 60 * 60,  # el aviso sirve aunque llegue tarde
            "retry": False,
            "committed": True,
        }
    ]
    assert said(change(browser, secret, NEW + NEW)) == REFUSED and len(queued) == 1


def test_a_broker_that_refuses_the_notice_does_not_undo_or_hide_the_change(
    browser: Client, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    ana = make_user(email=EMAIL)
    secret = issued()

    def refuse(**how: Any) -> None:
        raise ConnectionError("broker caído")

    monkeypatch.setattr(send_password_changed, "apply_async", refuse)
    with caplog.at_level(logging.DEBUG):
        assert reply(change(browser, secret)) == (204, b"")  # la contraseña ya cambió
    assert authenticate(username=EMAIL, password=NEW) == ana
    lines = [json_formatter().format(record) for record in caplog.records]
    told = [line for line in lines if "sin encolar" in line]  # y la traza del fallo, sin datos
    assert len(told) == 1 and "ConnectionError" in told[0] and str(ana.pk) not in told[0]
    logged = "\n".join(lines)
    assert EMAIL not in logged and NEW not in logged and secret not in logged


def test_the_task_mails_the_account_what_happened_and_what_to_do_without_any_secret(
    caplog: pytest.LogCaptureFixture, settings: Any
) -> None:
    ana = make_user(email=EMAIL)
    outbox.outbox.clear()
    with caplog.at_level(logging.DEBUG):
        assert send_password_changed.apply(args=[str(ana.pk)]).get() is None
    (message,) = outbox.outbox
    assert message.to == [EMAIL] and message.subject == recovery.CHANGED_SUBJECT
    body = str(message.body)
    assert "se cambió con un enlace de recuperación" in body and "se cerraron" in body
    assert f"Pide un enlace nuevo en {ORIGIN}/recuperar," in body  # dónde, si no fue ella
    assert "#" not in body and "restablecer" not in body  # ningún enlace que cambie nada
    assert User.objects.get().password not in body
    logged = "\n".join(json_formatter().format(record) for record in caplog.records)
    assert EMAIL not in logged and '"purpose": "password_changed"' in logged
    settings.APP_ORIGIN = ""  # sin origen público también avisa, sin dirección
    assert recovery.notify_change(str(ana.pk)) is True
    assert "Pide un enlace nuevo,\n" in str(outbox.outbox[-1].body)
    assert "http" not in str(outbox.outbox[-1].body)


def test_the_task_sends_nothing_for_an_account_it_cannot_or_should_not_write_to(
    settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    ana = make_user(email=EMAIL)
    gone = make_user(email="baja@example.com", is_active=False)
    local = make_user(email='"ab"@example.com')  # `core.mail` no escribe a esa dirección
    outbox.outbox.clear()
    for unknown in (str(uuid4()), str(gone.pk), str(local.pk), "no-es-un-identificador", ""):
        assert recovery.notify_change(unknown) is False
    assert outbox.outbox == []
    assert not connection.in_atomic_block  # no deja nada abierto

    def down(*args: Any, **kwargs: Any) -> None:
        raise MailError("entrega fallida")

    monkeypatch.setattr(core_mail, "send", down)
    failed = send_password_changed.apply(args=[str(ana.pk)])  # la entrega falla: la tarea falla
    assert isinstance(failed.result, MailError) and failed.failed()
    assert send_password_changed.max_retries == 0 and send_password_changed.ignore_result
    assert authenticate(username=EMAIL, password=TEST_PASSWORD) == ana  # avisar no cambia nada

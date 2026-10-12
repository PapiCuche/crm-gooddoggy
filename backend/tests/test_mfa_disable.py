"""F2-109: `POST /api/v1/auth/mfa/disable/` quita el segundo factor propio (ADR-023 §8).
Middleware, CSRF, límites y auditoría de plataforma reales; se entra por el acceso de verdad."""

import logging
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from django.core import mail as outbox
from django.db import connection
from django.test import Client

from apps.accounts import code_limit, mfa, mfa_disable, throttle
from apps.accounts.models import MfaBackupCode, MfaCodeAttempt, MfaDevice, User
from apps.accounts.tasks import send_mfa_code_limit, send_mfa_disabled
from apps.audit import platform
from core.observability.logging import json_formatter
from tests import test_auth_api, test_mfa_second_factor
from tests.factories import TEST_PASSWORD, make_user
from tests.test_auth_api import CSRF, EMAIL, SESSION, audit, login, reply, send
from tests.test_login_second_step import STEP_TWO, counters
from tests.test_mfa_second_factor import SECRET, code_at, enrolled

real_stack, browser = test_auth_api.real_stack, test_auth_api.browser
clock = test_mfa_second_factor.clock
pytestmark = pytest.mark.usefixtures("tenant_db")
DISABLE = "/api/v1/auth/mfa/disable/"
INVALID = "invalid"


@pytest.fixture(autouse=True)
def queued(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    calls: list[Any] = []

    def sent(**how: Any) -> None:
        assert not connection.in_atomic_block  # con el factor ya quitado, no antes
        calls.append(how["args"])

    monkeypatch.setattr(send_mfa_disabled, "apply_async", sent)
    monkeypatch.setattr(send_mfa_code_limit, "apply_async", lambda **how: None)
    return calls


def inside(client: Client, shown: list[str]) -> None:
    """Entra por el acceso de verdad: la contraseña y, en el segundo paso, un código de respaldo
    (así el paso TOTP de ahora sigue sin gastar)."""
    assert login(client).status_code == 202
    assert send(client, "POST", STEP_TWO, {"code": shown.pop()}).status_code == 200


def off(client: Client, code: Any, password: Any = TEST_PASSWORD) -> Any:
    return send(client, "POST", DISABLE, {"password": password, "code": code})


def unspent() -> int:
    return MfaBackupCode.objects.filter(used_at__isnull=True).count()


def refused(answer: Any) -> tuple[int, dict[str, str]]:
    fields = answer.json().get("fields", {})
    return answer.status_code, {name: errors[0]["code"] for name, errors in fields.items()}


@pytest.mark.parametrize("method", ["totp", "backup_code"])
def test_with_the_password_and_a_code_the_factor_goes_and_the_other_sessions_end(
    browser: Client,
    clock: SimpleNamespace,
    migrator: psycopg.Connection[Any],
    queued: list[Any],
    caplog: pytest.LogCaptureFixture,
    method: str,
) -> None:
    ana, luis = make_user(email=EMAIL), make_user(email="luis@example.com")
    shown, theirs = enrolled(ana), enrolled(luis, bytes(reversed(SECRET)))
    other = Client(enforce_csrf_checks=True)
    other.get(CSRF)
    inside(browser, shown)
    inside(other, shown)
    before = browser.cookies["crm_session"].value
    migrator.execute("DELETE FROM platform_audit_logs")
    caplog.set_level(logging.DEBUG)
    code = code_at(clock) if method == "totp" else shown[0]
    assert reply(off(browser, code)) == (204, b"")
    assert not MfaDevice.objects.filter(user=ana).exists()  # ni dispositivo ni códigos
    assert not MfaBackupCode.objects.filter(user=ana).exists()
    assert MfaDevice.objects.filter(user=luis).count() == 1  # el de otra cuenta, sin tocar
    assert MfaBackupCode.objects.filter(user=luis).count() == len(theirs)
    (row,) = audit(migrator)
    assert row[:4] == ("auth.mfa.disabled", "SUCCESS", "USER", ana.pk)
    assert row[5:8] == ("user", ana.pk, f'{{"method": "{method}"}}')
    assert other.get(SESSION).status_code == 401  # las demás sesiones dejan de valer
    assert browser.get(SESSION).status_code == 200  # esta sigue, con otro ID
    assert browser.cookies["crm_session"].value != before
    assert mfa.MFA_AT not in browser.session
    assert browser.get("/api/v1/auth/mfa/").json()["enabled"] is False
    assert queued == [[str(ana.pk)]]  # el aviso, con el identificador de la cuenta
    assert "par" not in counters(migrator)  # el acierto devuelve lo que contó
    assert login(other).status_code == 200  # el acceso vuelve a pedir solo la contraseña
    assert reply(off(browser, code)) == (409, b'{"code":"MFA_NOT_ENABLED"}')
    logged = "\n".join(json_formatter().format(record) for record in caplog.records)
    kept = str(audit(migrator))
    for text in (code, TEST_PASSWORD, SECRET.hex()):
        assert text not in logged and text not in kept


def test_it_needs_a_session_csrf_the_password_and_a_code(
    browser: Client, clock: SimpleNamespace, migrator: psycopg.Connection[Any]
) -> None:
    ana = make_user(email=EMAIL)
    shown = enrolled(ana)
    assert reply(off(browser, code_at(clock))) == (401, b'{"code":"NOT_AUTHENTICATED"}')
    inside(browser, shown)
    bare = Client(enforce_csrf_checks=True)
    bare.cookies = browser.cookies
    body = {"password": TEST_PASSWORD, "code": code_at(clock)}
    assert bare.post(DISABLE, body, "application/json").status_code == 403
    assert [send(browser, verb, DISABLE, {}).status_code for verb in ("GET", "PUT", "DELETE")] == [
        405
    ] * 3
    assert refused(send(browser, "POST", DISABLE, {})) == (
        400,
        {"password": "required", "code": "required"},
    )
    migrator.execute("DELETE FROM platform_audit_logs")
    # La contraseña va primero: si no vale, el código ni se mira ni cuenta contra la cuenta.
    assert refused(off(browser, code_at(clock), test_auth_api.WRONG)) == (
        400,
        {"password": INVALID},
    )
    assert not MfaCodeAttempt.objects.exists() and MfaDevice.objects.get().last_step == 1
    assert refused(off(browser, code_at(clock, -2))) == (400, {"code": INVALID})
    assert MfaCodeAttempt.objects.count() == 1  # el código que no vale cuenta en el tope
    rows = audit(migrator)
    assert [row[:2] for row in rows] == [("auth.mfa.disabled", "DENIED")] * 2
    assert [row[7] for row in rows] == ['{"reason": "password"}', '{"reason": "code"}']
    assert counters(migrator)["par"] == 2  # los dos rechazos cuentan por dirección
    assert MfaDevice.objects.get().confirmed_at is not None  # el factor sigue
    assert browser.get(SESSION).status_code == 200
    typed = code_at(clock)  # como lo enseñan algunas aplicaciones: no es un fallo más
    assert reply(off(browser, f"{typed[:3]} {typed[3:]}")) == (204, b"")
    assert MfaCodeAttempt.objects.count() == 1  # solo el de antes: el que valió se devuelve


def test_without_a_device_there_is_nothing_to_remove(browser: Client) -> None:
    luis = make_user(email=EMAIL)
    enrolled(luis, confirmed=False)  # un alta a medias no es un segundo factor
    assert login(browser).status_code == 200
    assert reply(off(browser, "123456")) == (409, b'{"code":"MFA_NOT_ENABLED"}')
    assert MfaDevice.objects.count() == 1  # y esa alta no se toca


def test_without_its_audit_row_nothing_is_removed_and_the_code_is_not_spent(
    browser: Client,
    clock: SimpleNamespace,
    migrator: psycopg.Connection[Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ana = make_user(email=EMAIL)
    shown = enrolled(ana)
    other = Client(enforce_csrf_checks=True)
    other.get(CSRF)
    inside(browser, shown)
    inside(other, shown)
    before, record = browser.cookies["crm_session"].value, platform.record

    def unwritable(action: str, **how: Any) -> Any:
        if action == "auth.mfa.disabled" and "result" not in how:
            raise ConnectionError("sin auditoría")
        return record(action, **how)

    monkeypatch.setattr(platform, "record", unwritable)
    browser.raise_request_exception = False
    assert off(browser, code_at(clock)).status_code == 500
    assert MfaDevice.objects.get().last_step == 1 and unspent() == 8  # los dos del acceso
    assert User.objects.get(pk=ana.pk).session_epoch == ana.session_epoch  # nadie revocado
    assert other.get(SESSION).status_code == 200 and browser.get(SESSION).status_code == 200
    assert browser.cookies["crm_session"].value == before
    monkeypatch.setattr(platform, "record", record)
    assert reply(off(browser, code_at(clock))) == (204, b"")  # el mismo código sigue valiendo


@pytest.mark.parametrize("change", ["epoch", "logout", "inactive"])
def test_a_revocation_that_arrives_during_the_request_removes_nothing(
    browser: Client,
    clock: SimpleNamespace,
    migrator: psycopg.Connection[Any],
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    ana = make_user(email=EMAIL)
    inside(browser, enrolled(ana))
    admit = throttle.admit

    def meanwhile(attempt: dict[str, str]) -> Any:  # con la petición ya pasada por el middleware
        migrator.execute(
            {
                "epoch": "UPDATE users SET session_epoch = session_epoch + 1",
                "logout": "DELETE FROM django_session",
                "inactive": "UPDATE users SET is_active = false",
            }[change]
        )
        return admit(attempt)

    monkeypatch.setattr(throttle, "admit", meanwhile)
    assert reply(off(browser, code_at(clock))) == (401, b'{"code":"NOT_AUTHENTICATED"}')
    assert MfaDevice.objects.get().last_step == 1 and unspent() == 9  # solo el del acceso
    assert not MfaCodeAttempt.objects.exists()  # no se llegó a mirar el código
    assert browser.get(SESSION).status_code == 401  # y esa sesión ya no existe
    assert "auth.mfa.disabled" not in [row[0] for row in audit(migrator)]


def test_a_factor_that_went_meanwhile_is_a_409_with_the_attempt_counted(
    browser: Client,
    clock: SimpleNamespace,
    migrator: psycopg.Connection[Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inside(browser, enrolled(make_user(email=EMAIL)))
    admit = throttle.admit

    def meanwhile(attempt: dict[str, str]) -> Any:  # pasada ya la comprobación sin bloqueo
        migrator.execute("DELETE FROM user_mfa_devices")
        return admit(attempt)

    monkeypatch.setattr(throttle, "admit", meanwhile)
    assert reply(off(browser, code_at(clock))) == (409, b'{"code":"MFA_NOT_ENABLED"}')
    assert MfaCodeAttempt.objects.count() == 1 and browser.get(SESSION).status_code == 200


def test_the_rejection_that_starts_a_block_of_the_address_leaves_its_row(
    browser: Client, clock: SimpleNamespace, migrator: psycopg.Connection[Any]
) -> None:
    inside(browser, enrolled(make_user(email=EMAIL)))
    migrator.execute("DELETE FROM platform_audit_logs")
    for _ in range(4):
        assert off(browser, code_at(clock), test_auth_api.WRONG).status_code == 400
    assert refused(off(browser, code_at(clock, -2))) == (400, {"code": INVALID})  # el quinto
    rows = [row[0] for row in audit(migrator)]
    assert rows == ["auth.mfa.disabled"] * 5 + ["auth.login.throttled"]
    blocked = off(browser, code_at(clock))  # ni con las dos cosas, mientras dure el bloqueo
    assert reply(blocked) == (429, b'{"code":"RATE_LIMITED"}') and int(blocked["Retry-After"]) > 0
    assert MfaDevice.objects.get().last_step == 1 and MfaCodeAttempt.objects.count() == 1


def test_the_account_cap_refuses_also_the_right_code(
    browser: Client, clock: SimpleNamespace
) -> None:
    ana = make_user(email=EMAIL)
    inside(browser, enrolled(ana))
    for _ in range(10):
        code_limit.admit(ana.pk)
    capped = off(browser, code_at(clock))
    assert reply(capped) == (429, b'{"code":"RATE_LIMITED"}') and int(capped["Retry-After"]) > 86000
    assert MfaDevice.objects.get().last_step == 1  # ni se miró


def test_the_notice_is_best_effort_and_the_task_mails_the_account_without_any_code(
    browser: Client,
    clock: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    ana = make_user(email=EMAIL)
    gone = make_user(email="baja@example.com", is_active=False)
    shown = enrolled(ana)
    inside(browser, shown)

    def refuse(**how: Any) -> None:
        raise ConnectionError("broker caído")

    monkeypatch.setattr(send_mfa_disabled, "apply_async", refuse)
    with caplog.at_level(logging.ERROR):
        assert reply(off(browser, code_at(clock))) == (204, b"")  # sin aviso, ya está quitado
    assert "sin encolar" in caplog.text and not MfaDevice.objects.exists()
    outbox.outbox.clear()
    assert send_mfa_disabled.apply(args=[str(ana.pk)]).get() is None
    (message,) = outbox.outbox
    assert message.to == [EMAIL] and message.subject == mfa_disable.DISABLED_SUBJECT
    body = str(message.body)
    assert "Se quitó el segundo factor" in body and "entra solo con su\ncontraseña" in body
    assert "se cerraron" in body and "Cambia la contraseña con un enlace de recuperación" in body
    assert "http" not in body and TEST_PASSWORD not in body
    assert all(code not in body for code in shown)
    for unknown in (str(uuid4()), str(gone.pk), "no-es-un-identificador", ""):
        assert mfa_disable.notify_disabled(unknown) is False
    assert len(outbox.outbox) == 1 and send_mfa_disabled.max_retries == 0

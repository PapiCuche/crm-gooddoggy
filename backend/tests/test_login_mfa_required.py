"""F2-106: con un dispositivo confirmado, la contraseña correcta no abre sesión: deja el acceso a
medias (ADR-023 §5 y §10). Middleware, CSRF, límites y auditoría de plataforma reales."""

import logging
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest
from django.contrib.sessions.models import Session
from django.test import Client

from apps.accounts import second_step, throttle
from apps.accounts.models import MfaDevice
from apps.audit import platform
from core.observability.logging import json_formatter
from tests import test_auth_api, test_mfa_second_factor
from tests.factories import TEST_PASSWORD, make_user
from tests.test_auth_api import AGENT, EMAIL, INVALID, SESSION, audit, login, reply, send
from tests.test_login_second_step import STEP_TWO, counters
from tests.test_mfa_second_factor import code_at, enrolled

real_stack, browser = test_auth_api.real_stack, test_auth_api.browser
clock = test_mfa_second_factor.clock
pytestmark = pytest.mark.usefixtures("tenant_db")
PENDING = (202, b'{"mfa_required":true}')
LUIS = "luis@example.com"


def test_the_right_password_of_an_account_with_a_device_leaves_the_mark_and_no_session(
    browser: Client,
    clock: SimpleNamespace,
    migrator: psycopg.Connection[Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    ana, luis = make_user(email=EMAIL), make_user(email=LUIS)
    enrolled(ana)
    assert login(browser, email=LUIS).status_code == 200  # otra cuenta, ya dentro
    before, other = browser.cookies["crm_session"].value, Client()
    other.cookies["crm_session"] = before
    caplog.set_level(logging.DEBUG)
    answer = login(browser, email=f" {EMAIL.upper()} ")
    assert reply(answer) == PENDING  # ni un dato de la cuenta
    assert browser.cookies["crm_session"].value != before  # la marca, bajo otro ID
    assert browser.get(SESSION).status_code == 401 and other.get(SESSION).status_code == 401
    assert Session.objects.count() == 1  # la sesión que había se cerró
    mark = browser.session[second_step.PENDING]
    assert (mark["user"], mark["epoch"]) == (str(ana.pk), ana.session_epoch)
    assert mark["keys"] == throttle.keys(EMAIL, "127.0.0.1") and set(mark) == {
        "user",
        "epoch",
        "at",
        "keys",
    }
    row = audit(migrator)[-1]
    assert (
        row[:8]
        == (
            "auth.login.mfa_required",
            "SUCCESS",
            "ANONYMOUS",  # todavía no hay sesión
            None,
            platform.identifier_hash(EMAIL),
            "user",
            ana.pk,
            "{}",
        )
        and row[8:] == ("127.0.0.1", AGENT)
    )
    assert [r[0] for r in audit(migrator)] == ["auth.login.succeeded", "auth.login.mfa_required"]
    assert counters(migrator)["par"] == 1  # la contraseña no devuelve lo que contó
    done = send(browser, "POST", STEP_TWO, {"code": code_at(clock)})  # lo devuelve el segundo paso
    assert done.status_code == 200 and done.json()["user"]["id"] == str(ana.pk)
    assert browser.get(SESSION).json()["user"]["email"] == EMAIL
    assert "par" not in counters(migrator) and audit(migrator)[-1][7] == '{"method": "totp"}'
    logged = "\n".join(json_formatter().format(record) for record in caplog.records)
    assert TEST_PASSWORD not in logged and EMAIL not in logged and str(luis.pk) not in str(mark)


def test_a_wrong_password_answers_the_same_with_or_without_a_second_factor(
    browser: Client, migrator: psycopg.Connection[Any]
) -> None:
    ana = make_user(email=EMAIL)
    make_user(email=LUIS)
    enrolled(ana)
    answers = [
        reply(login(browser, email=who, password=test_auth_api.WRONG)) for who in (EMAIL, LUIS)
    ]
    assert answers == [INVALID, INVALID]
    assert second_step.PENDING not in browser.session
    assert [row[:2] for row in audit(migrator)] == [("auth.login.failed", "FAILED")] * 2
    migrator.execute("UPDATE users SET is_active = false WHERE id = %s", [ana.pk])
    assert reply(login(browser)) == INVALID  # desactivada: como hoy, con o sin segundo factor
    assert second_step.PENDING not in browser.session


def test_without_its_audit_row_there_is_no_mark_and_the_open_session_stays(
    browser: Client, migrator: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    ana = make_user(email=EMAIL)
    make_user(email=LUIS)
    enrolled(ana)
    assert login(browser, email=LUIS).status_code == 200
    before, record = browser.cookies["crm_session"].value, platform.record

    def unwritable(action: str, **how: Any) -> Any:
        if action == "auth.login.mfa_required":
            raise ConnectionError("sin auditoría")
        return record(action, **how)

    monkeypatch.setattr(platform, "record", unwritable)
    browser.raise_request_exception = False
    assert login(browser).status_code == 500
    assert browser.cookies["crm_session"].value == before  # la sesión que había, intacta
    assert browser.get(SESSION).json()["user"]["email"] == LUIS
    assert second_step.PENDING not in browser.session and Session.objects.count() == 1
    assert [row[0] for row in audit(migrator)] == ["auth.login.succeeded"]


@pytest.mark.parametrize("device", ["none", "pending"])
def test_without_a_confirmed_device_sign_in_is_as_before_and_drops_a_mark(
    browser: Client, clock: SimpleNamespace, migrator: psycopg.Connection[Any], device: str
) -> None:
    ana, luis = make_user(email=EMAIL), make_user(email=LUIS)
    enrolled(ana)
    if device == "pending":  # un alta a medias no exige nada
        enrolled(luis, confirmed=False)
    assert reply(login(browser)) == PENDING  # Ana deja su marca en este navegador
    answer = login(browser, email=LUIS)  # y entra Luis, que no tiene segundo factor
    assert answer.status_code == 200 and answer.json()["user"]["id"] == str(luis.pk)
    assert second_step.PENDING not in browser.session  # la marca no vive en su sesión
    assert reply(send(browser, "POST", STEP_TWO, {"code": code_at(clock)})) == (
        401,
        b'{"code":"MFA_LOGIN_NOT_STARTED"}',
    )
    assert browser.get(SESSION).json()["user"]["email"] == LUIS  # y sigue siendo la de Luis
    assert MfaDevice.objects.filter(user=ana).get().last_step == 1


@pytest.mark.parametrize("scope", ["identifier", "pair", "ip"])
def test_a_right_password_that_reaches_a_limit_still_gets_to_its_second_step(
    browser: Client, clock: SimpleNamespace, migrator: psycopg.Connection[Any], scope: str
) -> None:
    ana = make_user(email=EMAIL)
    enrolled(ana)
    seen = throttle.limits()[scope].failures - (scope != "identifier")
    migrator.execute(
        "INSERT INTO login_throttles (key, failures, updated_at) VALUES (%s, %s, now())",
        [throttle.keys(EMAIL, "127.0.0.1")[scope], seen],
    )
    assert reply(login(browser)) == PENDING  # el intento que llega al límite era el correcto
    assert send(browser, "POST", STEP_TWO, {"code": code_at(clock)}).status_code == 200
    assert browser.get(SESSION).status_code == 200

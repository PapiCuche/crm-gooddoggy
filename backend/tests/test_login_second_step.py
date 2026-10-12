"""F2-105: `POST /api/v1/auth/login/mfa/`, el segundo paso del acceso (ADR-023 §5, §6 y §10).
Middleware, CSRF, límites y auditoría de plataforma reales."""

import logging
import time
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest
from django.contrib.sessions.models import Session
from django.test import Client

from apps.accounts import code_limit, mfa, second_step, throttle
from apps.accounts.models import MfaCodeAttempt, MfaDevice, User
from apps.accounts.tasks import send_mfa_code_limit
from apps.audit import platform
from core.observability.logging import json_formatter
from tests import test_auth_api, test_mfa_second_factor
from tests.factories import make_user
from tests.test_auth_api import AGENT, EMAIL, SESSION, audit, login, reply, send
from tests.test_mfa_enrollment_start import KEYS
from tests.test_mfa_second_factor import SECRET, code_at, enrolled

real_stack, browser = test_auth_api.real_stack, test_auth_api.browser
clock = test_mfa_second_factor.clock
pytestmark = pytest.mark.usefixtures("tenant_db")
STEP_TWO = "/api/v1/auth/login/mfa/"
NOT_STARTED = (401, b'{"code":"MFA_LOGIN_NOT_STARTED"}')
INVALID = {"code": "invalid", "message": "El código no vale."}
WRONG = {"code": "VALIDATION_ERROR", "fields": {"code": [INVALID]}}


@pytest.fixture(autouse=True)
def queued(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    calls: list[Any] = []
    monkeypatch.setattr(send_mfa_code_limit, "apply_async", lambda **how: calls.append(how["args"]))
    return calls


def halfway(client: Client, user: User, change: dict[str, Any] | None = None) -> None:
    """Como lo dejará el acceso: la contraseña correcta, contada en el límite por dirección y
    sin devolver, y la marca en la sesión del navegador."""
    attempt = throttle.keys(user.email, "127.0.0.1")
    session = client.session
    second_step.mark(SimpleNamespace(session=session), user, attempt, throttle.admit(attempt))  # type: ignore[arg-type]
    session[second_step.PENDING] = {**session[second_step.PENDING], **(change or {})}
    session.save()


def second(client: Client, code: Any) -> Any:
    return send(client, "POST", STEP_TWO, {"code": code})


def counters(migrator: psycopg.Connection[Any]) -> dict[str, int]:
    return {key.split(":")[0]: failures for key, failures in migrator.execute(KEYS).fetchall()}


def test_with_the_mark_and_a_valid_code_the_session_opens_like_a_sign_in(
    browser: Client,
    clock: SimpleNamespace,
    migrator: psycopg.Connection[Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    ana = make_user(email=EMAIL, first_name="Ana", last_name="López")
    shown = enrolled(ana)
    halfway(browser, ana)
    marked, csrf = browser.cookies["crm_session"].value, browser.cookies["csrftoken"].value
    assert browser.get(SESSION).status_code == 401  # la marca no es una sesión
    caplog.set_level(logging.DEBUG)
    typed = code_at(clock)
    answer = second(browser, f"{typed[:3]} {typed[3:]}")  # como lo enseñan algunas aplicaciones
    assert answer.status_code == 200 and answer.json() == {
        "user": {"id": str(ana.pk), "email": EMAIL, "first_name": "Ana", "last_name": "López"}
    }  # la respuesta del acceso
    assert browser.cookies["crm_session"].value != marked  # otro ID de sesión
    assert browser.cookies["csrftoken"].value != csrf  # y otro token CSRF
    assert browser.get(SESSION).json()["user"]["id"] == str(ana.pk)
    opened = browser.session
    assert second_step.PENDING not in opened and opened["epoch"] == ana.session_epoch
    assert abs(opened[mfa.MFA_AT] - time.time()) < 60 and opened[mfa.MFA_AT] == opened["auth_at"]
    assert Session.objects.count() == 1  # la de la marca ya no existe
    (row,) = audit(migrator)
    assert row[:8] == (
        "auth.login.succeeded",
        "SUCCESS",
        "USER",
        ana.pk,
        None,
        None,
        None,
        '{"method": "totp"}',
    ) and row[8:] == ("127.0.0.1", AGENT)
    assert counters(migrator) == {"id": 0, "ip": 0}  # los dos intentos devueltos, sin `par:`
    assert not MfaCodeAttempt.objects.exists()
    assert reply(second(browser, typed)) == NOT_STARTED  # ya no hay acceso a medias
    again = Client(enforce_csrf_checks=True)
    again.get(test_auth_api.CSRF)
    halfway(again, ana)
    assert second(again, typed).json() == WRONG  # el mismo código no vale otra vez
    assert second(again, shown[0].lower()).status_code == 200  # uno de respaldo, sí
    assert audit(migrator)[-1][7] == '{"method": "backup_code"}'
    logged = "\n".join(json_formatter().format(record) for record in caplog.records)
    assert not any(text in logged for text in (typed, shown[0], shown[0].lower(), SECRET.hex()))


def test_it_needs_csrf_and_a_mark_that_is_whole_and_not_five_minutes_old(
    browser: Client, clock: SimpleNamespace, migrator: psycopg.Connection[Any]
) -> None:
    ana = make_user(email=EMAIL)
    enrolled(ana)
    assert reply(second(browser, code_at(clock))) == NOT_STARTED  # sin marca
    halfway(browser, ana)
    bare = Client(enforce_csrf_checks=True)
    bare.cookies = browser.cookies
    assert bare.post(STEP_TWO, {"code": code_at(clock)}, "application/json").status_code == 403
    assert [send(browser, verb, STEP_TWO, {}).status_code for verb in ("GET", "PUT", "DELETE")] == [
        405
    ] * 3
    for body in ({}, {"code": ""}, {"code": None}, {"code": "1" * 33}):
        answer = send(browser, "POST", STEP_TWO, body)
        assert (answer.status_code, list(answer.json()["fields"])) == (400, ["code"])
    assert counters(migrator)["par"] == 1 and audit(migrator) == []  # un cuerpo así no cuenta
    old = int(time.time()) - second_step.PENDING_FOR
    broken: tuple[dict[str, Any], ...] = (
        {"at": old},  # caducó
        {"at": int(time.time()) + 60},  # del futuro
        {"at": "ayer"},
        {"at": 10**400},  # no cabe en la resta
        {"user": "no-es-una-cuenta"},
        {"epoch": "0"},
        {"keys": {"pair": "par:x"}},
    )
    for change in broken:
        migrator.execute("DELETE FROM login_throttles")  # cada marca, con su contraseña
        halfway(browser, ana, change)
        assert reply(second(browser, code_at(clock))) == NOT_STARTED, change
        assert second_step.PENDING not in browser.session  # y la marca se borra
    session = browser.session
    session[second_step.PENDING] = "marca"
    session.save()
    assert reply(second(browser, code_at(clock))) == NOT_STARTED
    assert MfaDevice.objects.get().last_step == 1 and not MfaCodeAttempt.objects.exists()
    migrator.execute("DELETE FROM login_throttles")
    halfway(browser, ana, {"at": old + 5})  # dentro del plazo
    assert second(browser, code_at(clock)).status_code == 200


@pytest.mark.parametrize("change", ["epoch", "inactive", "no_device", "pending_device"])
def test_a_mark_that_no_longer_holds_is_refused_without_looking_at_the_code(
    browser: Client, clock: SimpleNamespace, migrator: psycopg.Connection[Any], change: str
) -> None:
    ana = make_user(email=EMAIL)
    enrolled(ana)
    halfway(browser, ana)
    migrator.execute(
        {
            "epoch": "UPDATE users SET session_epoch = session_epoch + 1",  # cambió la contraseña
            "inactive": "UPDATE users SET is_active = false",
            "no_device": "DELETE FROM user_mfa_devices",
            "pending_device": "UPDATE user_mfa_devices SET confirmed_at = NULL, last_step = NULL",
        }[change]
    )
    assert reply(second(browser, code_at(clock))) == NOT_STARTED
    assert second_step.PENDING not in browser.session and browser.get(SESSION).status_code == 401
    assert counters(migrator) == {"id": 1, "par": 1, "ip": 1}  # solo el de la contraseña
    assert not MfaCodeAttempt.objects.exists() and audit(migrator) == []


@pytest.mark.parametrize("change", ["epoch", "inactive", "no_device"])
def test_a_change_that_arrives_during_the_request_opens_no_session(
    browser: Client,
    clock: SimpleNamespace,
    migrator: psycopg.Connection[Any],
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    ana = make_user(email=EMAIL)
    enrolled(ana)
    halfway(browser, ana)
    admit = throttle.admit

    def meanwhile(attempt: dict[str, str]) -> Any:  # ya pasó la comprobación sin bloqueo
        migrator.execute(
            {
                "epoch": "UPDATE users SET session_epoch = session_epoch + 1",
                "inactive": "UPDATE users SET is_active = false",
                "no_device": "DELETE FROM user_mfa_backup_codes; DELETE FROM user_mfa_devices",
            }[change]
        )
        return admit(attempt)

    monkeypatch.setattr(throttle, "admit", meanwhile)
    assert reply(second(browser, code_at(clock))) == NOT_STARTED
    assert second_step.PENDING not in browser.session and browser.get(SESSION).status_code == 401
    assert "auth.login.succeeded" not in [row[0] for row in audit(migrator)]
    # Sin dispositivo el intento queda contado; si no se llegó a mirar el código, se devuelve.
    assert MfaCodeAttempt.objects.count() == (1 if change == "no_device" else 0)


def test_a_mark_opens_one_session_even_with_two_valid_codes_at_once(
    browser: Client,
    clock: SimpleNamespace,
    migrator: psycopg.Connection[Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ana = make_user(email=EMAIL)
    shown = enrolled(ana)
    halfway(browser, ana)
    twin, admit = Client(enforce_csrf_checks=True), throttle.admit
    twin.cookies = browser.cookies  # el mismo navegador: otra petición con la misma marca

    def meanwhile(attempt: dict[str, str]) -> Any:  # la otra llega después y acaba antes
        monkeypatch.setattr(throttle, "admit", admit)
        assert second(twin, shown[0]).status_code == 200
        return admit(attempt)

    monkeypatch.setattr(throttle, "admit", meanwhile)
    assert reply(second(browser, code_at(clock))) == NOT_STARTED  # y su código era válido
    assert Session.objects.count() == 1 and browser.get(SESSION).status_code == 200
    assert [row[7] for row in audit(migrator)] == ['{"method": "backup_code"}']
    assert MfaDevice.objects.get().last_step == 1 and not MfaCodeAttempt.objects.exists()
    assert counters(migrator) == {"id": 1, "par": 1, "ip": 1}  # solo el intento rechazado


def test_a_code_that_is_not_valid_keeps_the_mark_counts_and_is_audited(
    browser: Client, clock: SimpleNamespace, migrator: psycopg.Connection[Any]
) -> None:
    ana = make_user(email=EMAIL)
    enrolled(ana)
    halfway(browser, ana)
    migrator.execute("UPDATE login_throttles SET updated_at = now() - interval '9 minutes'")
    marked = browser.cookies["crm_session"].value
    for wrong in (code_at(clock, -2), "12345a", "U" * 16):
        answer = second(browser, wrong)
        assert (answer.status_code, answer.json()) == (400, WRONG), wrong  # sin decir por qué
    moved = "SELECT count(*) FROM login_throttles WHERE updated_at > now() - interval '1 minute'"
    assert migrator.execute(moved).fetchone() == (3,)  # un fallo adelanta el reloj de los tres
    assert (
        browser.cookies["crm_session"].value == marked and browser.get(SESSION).status_code == 401
    )
    assert second_step.PENDING in browser.session  # se puede probar otro
    rows = audit(migrator)
    assert len(rows) == 3 and rows[-1][:4] == ("auth.login.failed", "FAILED", "ANONYMOUS", None)
    assert rows[-1][4] == platform.identifier_hash(EMAIL)  # la huella, como en el acceso
    assert rows[-1][5:8] == ("user", ana.pk, '{"reason": "wrong_code"}')
    assert (
        counters(migrator) == {"id": 4, "par": 4, "ip": 4} and MfaCodeAttempt.objects.count() == 3
    )
    assert second(browser, code_at(clock)).status_code == 200
    assert counters(migrator) == {"id": 3, "ip": 3}  # devuelve el suyo y el de la contraseña
    assert MfaCodeAttempt.objects.count() == 3  # los fallos del tope por cuenta siguen contados


def test_both_limits_refuse_with_a_wait_and_the_cap_also_refuses_the_right_code(
    browser: Client,
    clock: SimpleNamespace,
    migrator: psycopg.Connection[Any],
    queued: list[Any],
    settings: Any,
) -> None:
    ana = make_user(email=EMAIL)
    enrolled(ana)
    halfway(browser, ana)
    for _ in range(code_limit.admit(ana.pk).last or 8):  # nueve contados contra la cuenta
        code_limit.admit(ana.pk)
    assert second(browser, "000000").status_code == 400  # el décimo: alcanza el tope
    rows = audit(migrator)
    assert [row[0] for row in rows] == ["auth.mfa.throttled", "auth.login.failed"]
    assert rows[0][1:7] == ("DENIED", "ANONYMOUS", None, None, "user", ana.pk)
    assert queued == [[str(ana.pk)]]  # y el aviso a la cuenta
    capped = second(browser, code_at(clock))  # ni con el correcto
    assert reply(capped) == (429, b'{"code":"RATE_LIMITED"}')
    assert 86000 < int(capped["Retry-After"]) <= 86400 and MfaDevice.objects.get().last_step == 1
    assert second_step.PENDING in browser.session and len(audit(migrator)) == 2
    migrator.execute("DELETE FROM user_mfa_code_attempts")
    limit = settings.LOGIN_THROTTLE["pair"][0]
    migrator.execute(
        "UPDATE login_throttles SET failures = %s WHERE key LIKE 'par:%%'", [limit - 1]
    )
    assert second(browser, "000000").status_code == 400  # empieza un bloqueo por dirección
    assert [row[0] for row in audit(migrator)][-2:] == ["auth.login.failed", "auth.login.throttled"]
    limited = second(browser, code_at(clock))
    assert reply(limited) == (429, b'{"code":"RATE_LIMITED"}') and int(limited["Retry-After"]) > 0
    assert MfaCodeAttempt.objects.count() == 1  # rechazado antes de contar contra la cuenta


def test_without_its_audit_row_there_is_no_session_and_the_code_is_not_spent(
    browser: Client,
    clock: SimpleNamespace,
    migrator: psycopg.Connection[Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ana = make_user(email=EMAIL)
    enrolled(ana)
    halfway(browser, ana)
    marked, record = browser.cookies["crm_session"].value, platform.record

    def unwritable(action: str, **how: Any) -> Any:
        if action == "auth.login.succeeded":
            raise ConnectionError("sin auditoría")
        return record(action, **how)

    monkeypatch.setattr(platform, "record", unwritable)
    browser.raise_request_exception = False
    assert second(browser, code_at(clock)).status_code == 500
    assert (
        browser.cookies["crm_session"].value == marked and browser.get(SESSION).status_code == 401
    )
    assert Session.objects.count() == 1 and second_step.PENDING in browser.session
    assert MfaDevice.objects.get().last_step == 1 and audit(migrator) == []
    assert counters(migrator) == {"id": 2, "par": 2, "ip": 2}  # nada devuelto
    assert MfaCodeAttempt.objects.count() == 1  # como en el acceso: sin fila, el intento cuenta
    monkeypatch.setattr(platform, "record", record)
    assert second(browser, code_at(clock)).status_code == 200  # el mismo código sigue valiendo


def test_a_mark_inside_an_open_session_of_that_account_still_gets_another_id(
    browser: Client, clock: SimpleNamespace
) -> None:
    ana = make_user(email=EMAIL)
    assert login(browser).status_code == 200  # nunca debería: la marca, en una sesión abierta
    enrolled(ana)
    halfway(browser, ana)
    opened = browser.cookies["crm_session"].value
    assert second(browser, code_at(clock)).status_code == 200
    assert browser.cookies["crm_session"].value != opened and Session.objects.count() == 1


@pytest.mark.parametrize("scope", ["identifier", "pair", "ip"])
def test_a_block_the_right_password_started_does_not_refuse_the_second_step(
    browser: Client, clock: SimpleNamespace, migrator: psycopg.Connection[Any], scope: str
) -> None:
    ana = make_user(email=EMAIL)
    enrolled(ana)
    seen = throttle.limits()[scope].failures - (scope != "identifier")  # la cuenta, ya caliente
    migrator.execute(
        "INSERT INTO login_throttles (key, failures, updated_at) VALUES (%s, %s, now())",
        [throttle.keys(EMAIL, "127.0.0.1")[scope], seen],
    )
    halfway(browser, ana)  # la contraseña correcta es el intento que llega al límite
    assert sum(counters(migrator).values()) == seen + 3  # lo contado sigue contado (ADR-023 §5)
    assert second(browser, code_at(clock)).status_code == 200  # y ese bloqueo no lo rechaza
    left = "SELECT failures, blocked_until FROM login_throttles WHERE failures > 0"
    assert migrator.execute(left).fetchall() == ([] if scope == "pair" else [(seen, None)])


def test_leaving_the_mark_ends_only_the_blocks_that_its_password_started(
    migrator: psycopg.Connection[Any],
) -> None:
    attempt = throttle.keys(EMAIL, "127.0.0.1")
    assert throttle.admit(attempt) == []  # una contraseña correcta que no llega a ningún límite
    migrator.execute("UPDATE login_throttles SET blocked_until = now() + interval '1 hour'")
    held = "SELECT count(*) FROM login_throttles WHERE blocked_until > now()"
    throttle.release(attempt, [])  # los empezó otro intento mientras se comprobaba la contraseña
    assert migrator.execute(held).fetchone() == (3,)
    throttle.release(attempt, [throttle.Blocked("ip", 30, 300)])  # solo el que empezó con ella
    assert migrator.execute(held).fetchone() == (2,) and throttle.blocked_for(attempt) > 0


def test_a_right_password_without_the_code_does_not_restart_a_counter(
    browser: Client, clock: SimpleNamespace, migrator: psycopg.Connection[Any]
) -> None:
    ana = make_user(email=EMAIL)
    enrolled(ana)
    migrator.execute(  # nueve intentos: el último fallo, hace 16 minutos; su bloqueo acaba ahora
        "INSERT INTO login_throttles (key, failures, updated_at, blocked_until) VALUES"
        " (%s, 9, now() - interval '16 minutes', now() - interval '1 second')",
        [throttle.keys(EMAIL, "127.0.0.1")["pair"]],
    )
    halfway(browser, ana)  # el bloqueo que empieza con ella acaba ahora, no «nunca empezó»
    assert second(browser, "000000").status_code == 400
    assert counters(migrator)["par"] == 11  # sin calma no vuelve a empezar (ADR-023 §5)

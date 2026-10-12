"""F2-100: `POST /api/v1/auth/mfa/totp/confirm/` confirma el alta de un dispositivo TOTP
(ADR-023 §2, §3, §4, §6 y §10). Middleware, CSRF, límite y auditoría de plataforma reales."""

import base64
import json
import logging
import re
import time
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from django.core import mail as outbox
from django.db import DatabaseError, connection, transaction
from django.test import Client, RequestFactory
from django.test.utils import CaptureQueriesContext

from apps.accounts import backup_codes, mfa, throttle, totp
from apps.accounts.models import ENROLLMENT_FAILURES, MfaBackupCode, MfaDevice, User
from apps.accounts.tasks import send_mfa_enabled
from apps.audit import platform
from core import sealed
from core.observability import reporting
from core.observability.logging import json_formatter
from tests import test_auth_api
from tests.factories import TEST_PASSWORD, make_user
from tests.test_auth_api import AGENT, EMAIL, SESSION, audit, login, reply, send
from tests.test_mfa_enrollment_start import KEYS, start

real_stack, browser = test_auth_api.real_stack, test_auth_api.browser
pytestmark = pytest.mark.usefixtures("tenant_db")
CONFIRM = "/api/v1/auth/mfa/totp/confirm/"
SHOWN = re.compile(r"[0-9A-HJKMNP-TV-Z]{4}(-[0-9A-HJKMNP-TV-Z]{4}){3}")
OLDER = "UPDATE user_mfa_devices SET created_at = now() - %s::interval"
NOT_STARTED = (409, b'{"code":"MFA_ENROLLMENT_NOT_STARTED"}')
INVALID = {"code": "invalid", "message": "El código no vale."}
WRONG = {"code": "VALIDATION_ERROR", "fields": {"code": [INVALID]}}


@pytest.fixture(autouse=True)
def queued(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """El aviso que se encola, sin broker, y si entonces la confirmación ya estaba guardada."""
    calls: list[dict[str, Any]] = []

    def enqueue(**how: Any) -> None:
        how.pop("connection")  # la propia del broker, como en el aviso de F2-93
        confirmed = MfaDevice.objects.exclude(confirmed_at=None).exists()
        calls.append({**how, "committed": confirmed and not connection.in_atomic_block})

    monkeypatch.setattr(send_mfa_enabled, "apply_async", enqueue)
    return calls


def begun(client: Client) -> bytes:
    """Entra y empieza un alta: el secreto del dispositivo sin confirmar."""
    assert login(client).status_code == 200
    return base64.b32decode(start(client).json()["secret"])


def now_code(secret: bytes, shift: int = 0) -> str:
    return totp.code(secret, totp.step_at(time.time()) + shift)


def confirm(client: Client, code: Any) -> Any:
    return send(client, "POST", CONFIRM, {"code": code})


def test_a_code_of_the_device_confirms_it_gives_backup_codes_once_and_closes_other_sessions(
    browser: Client,
    migrator: psycopg.Connection[Any],
    queued: list[dict[str, Any]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    ana = make_user(email=EMAIL)
    MfaBackupCode.objects.create(user=ana, code_hash="0" * 64)  # uno de antes: se sustituye
    other = Client(enforce_csrf_checks=True)  # otra sesión abierta, sin segundo factor
    other.get(test_auth_api.CSRF)
    assert login(other).status_code == 200
    secret, code = begun(browser), ""
    before = browser.cookies["crm_session"].value
    code = now_code(secret)
    with caplog.at_level(logging.DEBUG), CaptureQueriesContext(connection) as seen:
        answer = confirm(browser, code)
    assert answer.status_code == 200 and answer["Cache-Control"] == "no-store"
    codes = answer.json()["backup_codes"]
    assert len(codes) == len(set(codes)) == 10 and all(SHOWN.fullmatch(shown) for shown in codes)
    locks = [query["sql"] for query in seen if " FOR " in query["sql"]]
    assert ['"users"' in sql for sql in locks] == [
        True,
        False,
    ]  # la cuenta, y después el dispositivo
    assert "NO KEY UPDATE" in locks[0] and '"user_mfa_devices"' in locks[1]
    device = MfaDevice.objects.get()
    assert device.confirmed_at is not None and totp.code(secret, device.last_step or 0) == code
    stored = set(MfaBackupCode.objects.filter(user=ana).values_list("code_hash", flat=True))
    assert stored == {backup_codes.code_hash(shown) for shown in codes} and len(stored) == 10
    assert not MfaBackupCode.objects.exclude(used_at=None).exists()
    assert audit(migrator)[-1] == (
        "auth.mfa.enabled",
        "SUCCESS",
        "USER",
        ana.pk,
        None,
        "user",
        ana.pk,
        "{}",
        "127.0.0.1",
        AGENT,
    )
    # Las demás sesiones se abrieron sin segundo factor: fuera. Esta sigue, con otro ID.
    assert other.get(SESSION).status_code == 401 and browser.get(SESSION).status_code == 200
    assert browser.cookies["crm_session"].value != before
    session = browser.session
    assert session[mfa.MFA_AT] <= time.time() < session[mfa.MFA_AT] + 5
    assert session["epoch"] == User.objects.get(pk=ana.pk).session_epoch == ana.session_epoch + 1
    assert queued == [
        {"args": [str(ana.pk)], "expires": 24 * 60 * 60, "retry": False, "committed": True}
    ]  # el aviso, con el identificador de la cuenta y con el alta ya confirmada
    assert reply(confirm(browser, code)) == (409, b'{"code":"MFA_ALREADY_ENABLED"}')
    assert reply(start(browser)) == (409, b'{"code":"MFA_ALREADY_ENABLED"}') and len(queued) == 1
    assert login(other).status_code == 200  # el acceso todavía no pide el segundo paso
    logged = "\n".join(json_formatter().format(record) for record in caplog.records)
    kept = json.dumps([row[7] for row in audit(migrator)])
    for text in (code, secret.hex(), base64.b32encode(secret).decode(), *codes, *stored):
        assert text not in logged and text not in kept


def test_it_needs_a_session_csrf_and_a_code_of_the_pending_device(
    browser: Client, migrator: psycopg.Connection[Any], queued: list[dict[str, Any]]
) -> None:
    ana = make_user(email=EMAIL)
    assert reply(confirm(browser, "123456")) == (401, b'{"code":"NOT_AUTHENTICATED"}')
    secret = begun(browser)
    bare = Client(enforce_csrf_checks=True)
    bare.cookies = browser.cookies
    assert bare.post(CONFIRM, {"code": now_code(secret)}, "application/json").status_code == 403
    assert [send(browser, verb, CONFIRM, {}).status_code for verb in ("GET", "PUT", "DELETE")] == [
        405
    ] * 3
    rows = len(audit(migrator))
    for body in ({}, {"code": ""}, {"code": None}, {"code": "1" * 17}):
        answer = send(browser, "POST", CONFIRM, body)
        assert (answer.status_code, list(answer.json()["fields"])) == (400, ["code"])
    assert len(audit(migrator)) == rows  # un cuerpo que no cumple el contrato ni cuenta ni audita
    stale = now_code(secret, -2)  # de hace dos pasos: fuera de la ventana
    for wrong in (stale, "12345a", "１２３４５６", now_code(bytes(reversed(secret)))):
        answer = confirm(browser, wrong)
        assert (answer.status_code, answer.json()) == (400, WRONG), wrong  # sin decir por qué
    assert MfaDevice.objects.get().confirmed_at is None and not MfaBackupCode.objects.exists()
    assert MfaDevice.objects.get().failed_codes == 4  # cada uno cuenta, sea o no seis dígitos
    assert audit(migrator)[-1] == (
        "auth.mfa.enabled",
        "DENIED",
        "USER",
        ana.pk,
        None,
        "user",
        ana.pk,
        '{"reason": "code"}',
        "127.0.0.1",
        AGENT,
    )
    assert queued == [] and browser.get(SESSION).status_code == 200  # la sesión sigue
    assert confirm(browser, f" {now_code(secret)} ").status_code == 200  # recortado: vale


def test_a_wrong_code_counts_in_the_sign_in_limit_and_only_a_right_one_gives_it_back(
    browser: Client, migrator: psycopg.Connection[Any], settings: Any
) -> None:
    make_user(email=EMAIL)
    assert login(browser).status_code == 200
    limit = settings.LOGIN_THROTTLE["pair"][0]
    for _ in range(limit - 1):  # contraseñas incorrectas al empezar el alta, con la sesión
        assert start(browser, test_auth_api.WRONG).status_code == 400
    assert reply(confirm(browser, "123456")) == NOT_STARTED  # no prueba nada: no los borra
    assert sorted(dict(migrator.execute(KEYS).fetchall()).values()) == [limit] * 3  # y cuenta
    assert start(browser).status_code == 429  # una sesión sola no pone el contador a cero
    assert login(browser).status_code == 429  # tampoco el acceso desde esa dirección; sin fila:
    assert {row[0] for row in audit(migrator)} == {"auth.login.succeeded", mfa.STARTED}
    migrator.execute("DELETE FROM login_throttles")
    secret = base64.b32decode(start(browser).json()["secret"])
    migrator.execute("UPDATE login_throttles SET updated_at = now() - interval '5 minutes'")
    for _ in range(limit - 1):
        assert confirm(browser, now_code(secret, -2)).status_code == 400
    # El que llega al límite no es un código: el alta sigue en pie, a un fallo de agotarse.
    assert start(browser, test_auth_api.WRONG).status_code == 400
    assert sorted(dict(migrator.execute(KEYS).fetchall()).values()) == [limit] * 3
    moved = "SELECT count(*) FROM login_throttles WHERE updated_at > now() - interval '1 minute'"
    assert migrator.execute(moved).fetchone() == (3,)  # un fallo adelanta el reloj de los tres
    limited = confirm(browser, now_code(secret))  # ni con el bueno: está bloqueado
    assert reply(limited) == (429, b'{"code":"RATE_LIMITED"}') and int(limited["Retry-After"]) > 0
    waiting = MfaDevice.objects.get()  # bloqueada no confirma, ni suma al tope del alta
    assert (waiting.confirmed_at, waiting.failed_codes) == (None, limit - 1)
    migrator.execute("DELETE FROM login_throttles")
    secret = base64.b32decode(start(browser).json()["secret"])
    assert confirm(browser, now_code(secret, -2)).status_code == 400
    assert confirm(browser, now_code(secret)).status_code == 200  # el acierto devuelve lo suyo
    counted = dict(migrator.execute(KEYS).fetchall())
    assert sorted(counted.values()) == [1, 1] and not any(key.startswith("par:") for key in counted)


def test_an_enrolment_expires_ten_minutes_after_it_started(
    browser: Client, migrator: psycopg.Connection[Any]
) -> None:
    make_user(email=EMAIL)
    secret = begun(browser)
    migrator.execute(OLDER, ["10 minutes"])
    assert reply(confirm(browser, now_code(secret))) == NOT_STARTED  # caducó: hay que empezar otra
    assert MfaDevice.objects.get().confirmed_at is None and not MfaBackupCode.objects.exists()
    migrator.execute(OLDER, ["9 minutes 55 seconds"])
    assert confirm(browser, now_code(secret)).status_code == 200  # dentro del plazo


@pytest.mark.parametrize("change", ["epoch", "inactive", "logout"])
def test_a_revocation_that_arrives_during_the_request_confirms_nothing_and_ends_the_session(
    browser: Client, migrator: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    ana = make_user(email=EMAIL)
    secret = begun(browser)
    admit = throttle.admit

    def meanwhile(attempt: dict[str, str]) -> Any:
        # Con la petición ya pasada por el middleware: otra operación revoca o desactiva.
        if change == "epoch":
            migrator.execute("UPDATE users SET session_epoch = session_epoch + 1")
        elif change == "logout":  # otra petición cerró esta sesión: no revive con otro ID
            migrator.execute("DELETE FROM django_session")
        else:
            migrator.execute("UPDATE users SET is_active = false")
        return admit(attempt)

    monkeypatch.setattr(throttle, "admit", meanwhile)
    assert reply(confirm(browser, now_code(secret))) == (401, b'{"code":"NOT_AUTHENTICATED"}')
    monkeypatch.setattr(throttle, "admit", admit)
    assert migrator.execute("SELECT count(*) FROM django_session").fetchone() == (0,)  # cerrada
    assert MfaDevice.objects.get().confirmed_at is None and not MfaBackupCode.objects.exists()
    assert "auth.mfa.enabled" not in [row[0] for row in audit(migrator)]
    assert sorted(dict(migrator.execute(KEYS).fetchall()).values()) == [1, 1, 1]  # y contado
    assert browser.get(SESSION).status_code == 401  # y esa sesión ya no existe
    assert User.objects.get(pk=ana.pk).session_epoch == ana.session_epoch + (change == "epoch")


@pytest.mark.parametrize("part", [(platform, "record"), (throttle, "forgive")])
def test_without_its_audit_row_nothing_is_confirmed_and_no_session_changes(
    browser: Client, monkeypatch: pytest.MonkeyPatch, migrator: psycopg.Connection[Any], part: Any
) -> None:
    ana = make_user(email=EMAIL)
    other = Client(enforce_csrf_checks=True)
    other.get(test_auth_api.CSRF)
    assert login(other).status_code == 200
    secret = begun(browser)
    key, real = browser.cookies["crm_session"].value, getattr(*part)

    def broken(*args: Any, **kwargs: Any) -> Any:
        raise DatabaseError("sin partición")

    browser.raise_request_exception = False
    monkeypatch.setattr(part[0], part[1], broken)  # la fila, o devolver lo contado
    assert reply(confirm(browser, now_code(secret))) == (500, b'{"code":"INTERNAL_ERROR"}')
    assert MfaDevice.objects.get().confirmed_at is None and not MfaBackupCode.objects.exists()
    assert User.objects.get(pk=ana.pk).session_epoch == ana.session_epoch  # nadie revocado
    assert other.get(SESSION).status_code == 200 and browser.get(SESSION).status_code == 200
    assert browser.cookies["crm_session"].value == key and mfa.MFA_AT not in browser.session
    assert (
        confirm(browser, now_code(secret, -2)).status_code == 400
    )  # un rechazo sin auditar: igual
    assert MfaDevice.objects.get().failed_codes == 1  # y cuenta, con su fila o sin ella
    monkeypatch.setattr(part[0], part[1], real)
    with migrator.transaction():  # con la fila de la cuenta bloqueada por otro, espera: no confirma
        migrator.execute("SELECT 1 FROM users WHERE id = %s FOR NO KEY UPDATE", [ana.pk])
        with connection.cursor() as cursor:
            cursor.execute("SET lock_timeout = '200ms'")
        assert confirm(browser, now_code(secret)).status_code == 500
    assert MfaDevice.objects.get().confirmed_at is None
    with transaction.atomic(), pytest.raises(RuntimeError, match="transacción"):
        mfa.confirm_enrollment(RequestFactory().post(CONFIRM), code="123456")


def test_a_secret_that_cannot_be_opened_is_a_code_that_is_not_valid_and_is_reported(
    browser: Client, monkeypatch: pytest.MonkeyPatch, settings: Any
) -> None:
    make_user(email=EMAIL)
    secret = begun(browser)
    reports: list[tuple[str, Any]] = []
    spy = SimpleNamespace(
        capture_exception=lambda error, context=None: reports.append((repr(error), context))
    )
    monkeypatch.setattr(reporting, "reporter", lambda: spy)
    settings.SECRETS_KEKS = {2: bytes(range(32))}  # la clave con la que se cifró ya no está
    answer = confirm(browser, now_code(secret))
    assert (answer.status_code, answer.json()) == (400, WRONG)  # la respuesta no lo distingue
    assert reports == [("UnreadableSecrets(1)", {"action": "mfa.confirm"})]
    monkeypatch.setattr(reporting, "reporter", lambda: None)  # un reporte roto: la misma
    assert confirm(browser, now_code(secret)).status_code == 400
    assert MfaDevice.objects.get().confirmed_at is None and not MfaBackupCode.objects.exists()
    assert MfaDevice.objects.get().failed_codes == 2  # y cuenta para el tope del alta


def test_the_fifth_wrong_code_ends_the_enrolment_whatever_address_it_comes_from(
    browser: Client, migrator: psycopg.Connection[Any], caplog: pytest.LogCaptureFixture
) -> None:
    make_user(email=EMAIL)
    secret = begun(browser)
    caplog.set_level(logging.DEBUG)
    for failed in range(1, ENROLLMENT_FAILURES):
        browser.defaults["REMOTE_ADDR"] = f"198.51.100.{failed}"  # cada uno desde otra dirección
        assert confirm(browser, now_code(secret, -2)).status_code == 400
        assert MfaDevice.objects.get().failed_codes == failed
    blocks = "SELECT count(*) FROM login_throttles WHERE blocked_until IS NOT NULL"
    assert migrator.execute(blocks).fetchone() == (0,)  # el límite por dirección no vio nada
    browser.defaults["REMOTE_ADDR"] = "198.51.100.9"
    answer = confirm(browser, "12345a")  # el quinto: tampoco hace falta que sean seis dígitos
    assert (answer.status_code, answer.json()) == (400, WRONG)  # la respuesta de siempre
    assert not MfaDevice.objects.exists() and not MfaBackupCode.objects.exists()
    denied = [row[7] for row in audit(migrator) if row[:2] == ("auth.mfa.enabled", "DENIED")]
    assert denied == ['{"reason": "code"}'] * 4 + ['{"reason": "code_limit"}']
    logged = "\n".join(json_formatter().format(record) for record in caplog.records)
    for text in ("12345a", secret.hex(), base64.b32encode(secret).decode()):
        assert text not in logged  # ni el código que no vale ni el secreto
    assert reply(confirm(browser, now_code(secret))) == NOT_STARTED  # ni con el bueno
    assert browser.get(SESSION).status_code == 200  # la sesión sigue: puede empezar otra
    migrator.execute("DELETE FROM login_throttles")
    for _ in range(2):  # con la contraseña, y a cero: también sobre un alta que tenía un fallo
        secret = base64.b32decode(start(browser).json()["secret"])
        assert MfaDevice.objects.get().failed_codes == 0
        assert confirm(browser, now_code(secret, -2)).status_code == 400
    for _ in range(ENROLLMENT_FAILURES - 2):
        assert confirm(browser, now_code(secret, -2)).status_code == 400
    migrator.execute("DELETE FROM login_throttles")
    assert confirm(browser, now_code(secret)).status_code == 200  # cuatro la dejan en pie
    assert MfaDevice.objects.get().failed_codes == 0  # confirmada, sin fallos anotados


def test_a_failure_counted_meanwhile_is_read_with_the_device_locked(
    browser: Client, migrator: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    make_user(email=EMAIL)
    secret = begun(browser)
    assert confirm(browser, now_code(secret, -2)).status_code == 400
    admit = throttle.admit

    def meanwhile(attempt: dict[str, str]) -> Any:  # otra petición falla con esta en curso
        migrator.execute("UPDATE user_mfa_devices SET failed_codes = %s", [ENROLLMENT_FAILURES - 1])
        return admit(attempt)

    monkeypatch.setattr(throttle, "admit", meanwhile)
    with CaptureQueriesContext(connection) as queries:
        assert confirm(browser, now_code(secret, -2)).status_code == 400
    assert not MfaDevice.objects.exists()  # contó sobre lo que había al bloquear la fila
    locked = [q["sql"] for q in queries if "user_mfa_devices" in q["sql"] and "SELECT" in q["sql"]]
    assert locked and all(sql.rstrip().endswith("FOR UPDATE") for sql in locked)


def test_the_cap_of_an_account_leaves_the_enrolment_of_another_alone(browser: Client) -> None:
    luis = make_user(email="luis@example.com")
    sealed_secret = sealed.seal(mfa.PURPOSE, mfa.owner(luis.pk), bytes(20))
    theirs = MfaDevice.objects.create(user=luis, type="TOTP", secret=sealed_secret, failed_codes=1)
    make_user(email=EMAIL)
    secret = begun(browser)
    for left in (2, 2, 2, 2, 1):  # el quinto borra el alta de esta cuenta, y solo esa
        assert confirm(browser, now_code(secret, -2)).status_code == 400
        assert MfaDevice.objects.count() == left
    theirs.refresh_from_db()
    assert (theirs.failed_codes, theirs.confirmed_at) == (1, None)


def test_the_notice_is_best_effort_and_the_task_mails_the_account_without_any_code(
    browser: Client, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    ana = make_user(email=EMAIL)
    gone = make_user(email="baja@example.com", is_active=False)
    local = make_user(email='"ab"@example.com')  # `core.mail` no escribe a esa dirección
    secret = begun(browser)

    def refuse(**how: Any) -> None:
        raise ConnectionError("broker caído")

    monkeypatch.setattr(send_mfa_enabled, "apply_async", refuse)
    with caplog.at_level(logging.DEBUG):
        answer = confirm(browser, now_code(secret))  # sin aviso, el segundo factor ya está
    assert answer.status_code == 200 and MfaDevice.objects.get().confirmed_at is not None
    assert len([r for r in caplog.records if "sin encolar" in r.getMessage()]) == 1
    outbox.outbox.clear()
    assert send_mfa_enabled.apply(args=[str(ana.pk)]).get() is None
    (message,) = outbox.outbox
    assert message.to == [EMAIL] and message.subject == mfa.ENABLED_SUBJECT
    body = str(message.body)
    assert "Se dio de alta un segundo factor" in body and "se cerraron" in body
    assert "Cambia la contraseña con un enlace" in body  # qué hacer si no fue ella: echarlo
    assert "te llegue o no ese enlace" in body  # no siempre llega: personal de plataforma
    assert "cambiar la contraseña no quita" in body  # y avisar
    assert all(shown not in body for shown in answer.json()["backup_codes"]) and "http" not in body
    for unknown in (str(uuid4()), str(gone.pk), str(local.pk), "no-es-un-identificador", ""):
        assert mfa.notify_enabled(unknown) is False
    assert len(outbox.outbox) == 1 and (
        send_mfa_enabled.max_retries,
        TEST_PASSWORD not in body,
    ) == (0, True)

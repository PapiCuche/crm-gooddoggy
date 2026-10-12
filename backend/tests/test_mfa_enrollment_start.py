"""F2-97: `POST /api/v1/auth/mfa/totp/` empieza el alta de un dispositivo TOTP (ADR-023 §3,
§4 y §10). Middleware, CSRF, límite de intentos y auditoría de plataforma reales."""

import base64
import json
import logging
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlsplit

import psycopg
import pytest
from django.contrib.auth import authenticate
from django.db import DatabaseError, connection, transaction
from django.test import Client, RequestFactory
from django.utils import timezone

from apps.accounts import mfa, totp
from apps.accounts.models import MfaDevice
from apps.audit import platform
from core import sealed
from core.observability import reporting
from core.observability.logging import json_formatter
from tests import test_auth_api
from tests.factories import TEST_PASSWORD, make_user
from tests.test_auth_api import AGENT, EMAIL, audit, login, reply, send

real_stack, browser = test_auth_api.real_stack, test_auth_api.browser
pytestmark = pytest.mark.usefixtures("tenant_db")
START = "/api/v1/auth/mfa/totp/"
WRONG = TEST_PASSWORD + "x"
KEYS = "SELECT key, failures FROM login_throttles ORDER BY key"


def start(client: Client, password: Any = TEST_PASSWORD) -> Any:
    return send(client, "POST", START, {"password": password})


def opened(device: MfaDevice) -> bytes:
    return sealed.unseal("mfa.totp", f"user:{device.user_id}", device.secret)


def test_it_stores_a_sealed_unconfirmed_device_and_shows_its_secret_once(
    browser: Client, migrator: psycopg.Connection[Any], caplog: pytest.LogCaptureFixture
) -> None:
    ana = make_user(email=EMAIL)
    assert login(browser).status_code == 200
    with caplog.at_level(logging.DEBUG):
        answer = start(browser)
    assert answer.status_code == 201 and answer["Cache-Control"] == "no-store"
    body = answer.json()
    assert set(body) == {"secret", "uri", "expires_in"} and body["expires_in"] == 600
    secret = base64.b32decode(body["secret"])
    assert len(body["secret"]) == 32 and len(secret) == 20
    device = MfaDevice.objects.get()
    assert (device.user_id, device.type, device.confirmed_at, device.last_step) == (
        ana.pk,
        "TOTP",
        None,
        None,
    )  # sin confirmar: no protege ni exige nada
    assert opened(device) == secret and secret not in bytes(device.secret)  # cifrado, y es ese
    link = urlsplit(body["uri"])
    assert (link.scheme, link.netloc, link.path) == (
        "otpauth",
        "totp",
        "/Good%20Doggy%20CRM%3Aana%40example.com",
    )
    assert parse_qs(link.query)["secret"] == [body["secret"]]
    assert totp.accepted_step(secret, totp.code(secret, 5), now=150.0, last=None) == 5  # sirve
    assert audit(migrator)[-1] == (
        "auth.mfa.enrollment_started",
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
    again = start(browser).json()  # otra vez: sustituye la que quedó a medias
    replaced = MfaDevice.objects.get()
    assert again["secret"] != body["secret"] and replaced.pk != device.pk
    assert opened(replaced) == base64.b32decode(again["secret"])
    logged = "\n".join(json_formatter().format(record) for record in caplog.records)
    stored = json.dumps([row[7] for row in audit(migrator)])
    for text in (body["secret"], secret.hex(), bytes(device.secret).hex(), TEST_PASSWORD):
        assert text not in logged and text not in stored
    assert authenticate(username=EMAIL, password=TEST_PASSWORD) == ana  # nada más cambia
    assert browser.get(test_auth_api.SESSION).status_code == 200


def test_it_needs_a_session_csrf_and_the_current_password(
    browser: Client, migrator: psycopg.Connection[Any]
) -> None:
    ana = make_user(email=EMAIL)
    assert reply(start(browser)) == (401, b'{"code":"NOT_AUTHENTICATED"}')  # sin sesión
    assert login(browser).status_code == 200
    bare = Client(enforce_csrf_checks=True)
    bare.cookies = browser.cookies
    assert bare.post(START, {"password": TEST_PASSWORD}, "application/json").status_code == 403
    assert [send(browser, verb, START, {}).status_code for verb in ("GET", "PUT", "DELETE")] == [
        405
    ] * 3
    before = len(audit(migrator))
    for missing in ({}, {"password": ""}, {"password": None}, {"password": "x" * 1025}):
        answer = send(browser, "POST", START, missing)
        assert (answer.status_code, list(answer.json()["fields"])) == (400, ["password"])
    assert len(audit(migrator)) == before and not MfaDevice.objects.exists()  # ni cuenta ni audita
    wrong = start(browser, WRONG)  # la contraseña de otro momento, o de quien robó la sesión
    assert wrong.status_code == 400 and wrong.json() == {
        "code": "VALIDATION_ERROR",
        "fields": {"password": [{"code": "invalid", "message": "La contraseña no es correcta."}]},
    }
    assert not MfaDevice.objects.exists()
    assert audit(migrator)[-1] == (
        "auth.mfa.enrollment_started",
        "DENIED",
        "USER",
        ana.pk,
        None,
        "user",
        ana.pk,
        '{"reason": "password"}',
        "127.0.0.1",
        AGENT,
    )
    assert browser.get(test_auth_api.SESSION).status_code == 200  # la sesión sigue
    assert start(browser, " " + TEST_PASSWORD).status_code == 400  # tal cual: sin recortar


def test_wrong_passwords_count_in_the_sign_in_limit_of_that_address(
    browser: Client, migrator: psycopg.Connection[Any], settings: Any
) -> None:
    make_user(email=EMAIL)
    assert login(browser).status_code == 200
    limit = settings.LOGIN_THROTTLE["pair"][0]
    migrator.execute("UPDATE login_throttles SET updated_at = now() - interval '5 minutes'")
    for _ in range(limit):
        assert start(browser, WRONG).status_code == 400
    counted = dict(migrator.execute(KEYS).fetchall())
    assert sorted(counted.values()) == [limit] * 3  # la cuenta, la dirección y el par
    moved = "SELECT count(*) FROM login_throttles WHERE updated_at > now() - interval '1 minute'"
    assert migrator.execute(moved).fetchone() == (3,)  # un fallo adelanta el reloj de los tres
    limited = start(browser)  # ni con la buena: el par cuenta+dirección está bloqueado
    assert reply(limited) == (429, b'{"code":"RATE_LIMITED"}') and int(limited["Retry-After"]) > 0
    assert not MfaDevice.objects.exists()
    actions = [row[0] for row in audit(migrator)]
    assert actions.count("auth.mfa.enrollment_started") == limit
    assert "auth.login.throttled" not in actions  # el bloqueo no deja la fila del acceso (README)


def test_a_confirmed_device_is_not_replaced_even_if_its_secret_cannot_be_read(
    browser: Client, migrator: psycopg.Connection[Any], settings: Any
) -> None:
    ana = make_user(email=EMAIL)
    assert login(browser).status_code == 200
    assert start(browser).status_code == 201
    MfaDevice.objects.update(confirmed_at=timezone.now(), last_step=1)
    kept = bytes(MfaDevice.objects.get().secret)
    before = len(audit(migrator))
    settings.SECRETS_KEKS = {2: bytes(range(32))}  # la clave con la que se cifró ya no está
    with pytest.raises(sealed.SealError):
        opened(MfaDevice.objects.get())
    assert reply(start(browser)) == (409, b'{"code":"MFA_ALREADY_ENABLED"}')  # ADR-023 §3
    assert bytes(MfaDevice.objects.get().secret) == kept and len(audit(migrator)) == before
    assert MfaDevice.objects.filter(user=ana).count() == 1
    for _ in range(settings.LOGIN_THROTTLE["pair"][0]):  # la contraseña era la buena: el intento
        assert start(browser).status_code == 409  # se devuelve aunque el alta no empiece
    assert set(dict(migrator.execute(KEYS).fetchall()).values()) == {0}


def test_without_its_audit_row_or_without_keys_no_device_is_left(
    browser: Client, monkeypatch: pytest.MonkeyPatch, settings: Any
) -> None:
    make_user(email=EMAIL)
    assert login(browser).status_code == 200
    assert start(browser).status_code == 201
    kept = MfaDevice.objects.get().pk
    real, reports = platform.record, list[Any]()
    spy = SimpleNamespace(capture_exception=lambda error, context=None: reports.append(context))
    monkeypatch.setattr(reporting, "reporter", lambda: spy)

    def broken(*args: Any, **kwargs: Any) -> Any:
        raise DatabaseError("sin partición")

    browser.raise_request_exception = False
    monkeypatch.setattr(platform, "record", broken)
    assert reply(start(browser)) == (500, b'{"code":"INTERNAL_ERROR"}')
    assert MfaDevice.objects.get().pk == kept  # falla cerrado: ni borra la que había
    assert start(browser, WRONG).status_code == 400  # un rechazo sin auditar sigue siendo el mismo
    assert reports == [{"action": "auth.mfa.enrollment_started"}]  # pero no en silencio
    monkeypatch.setattr(platform, "record", real)
    settings.SECRETS_KEKS = {}
    assert reply(start(browser)) == (500, b'{"code":"INTERNAL_ERROR"}')  # sin clave no se guarda
    assert MfaDevice.objects.get().pk == kept
    request = RequestFactory().post(START)
    with transaction.atomic(), pytest.raises(RuntimeError, match="transacción"):
        mfa.start_enrollment(request, password=TEST_PASSWORD)
    assert "secret" not in repr(mfa.Enrollment("ABC", "otpauth://x", 600)).lower()


def test_each_account_starts_its_own_and_starting_again_leaves_one(
    browser: Client,
) -> None:
    ana, luis = make_user(email=EMAIL), make_user(email="luis@example.com")
    other = Client(enforce_csrf_checks=True)
    other.get(test_auth_api.CSRF)
    assert login(browser).status_code == 200 and login(other, "luis@example.com").status_code == 200
    mine, theirs = start(browser).json(), start(other).json()
    assert mine["secret"] != theirs["secret"] and "luis%40example.com" in theirs["uri"]
    assert {device.user_id for device in MfaDevice.objects.all()} == {ana.pk, luis.pk}
    for device in MfaDevice.objects.all():  # cada secreto, atado a su cuenta
        wrong_owner = luis.pk if device.user_id == ana.pk else ana.pk
        with pytest.raises(sealed.SealError):
            sealed.unseal("mfa.totp", f"user:{wrong_owner}", device.secret)
    for _ in range(3):
        assert start(browser).status_code == 201
    assert MfaDevice.objects.filter(user=ana).count() == 1  # siempre una sola alta a medias


def test_the_right_password_gives_back_what_it_counted(
    browser: Client, migrator: psycopg.Connection[Any], settings: Any
) -> None:
    make_user(email=EMAIL)
    assert login(browser).status_code == 200
    for _ in range(settings.LOGIN_THROTTLE["pair"][0] + 1):
        assert start(browser).status_code == 201  # como un acceso correcto: no se acumulan
    assert set(dict(migrator.execute(KEYS).fetchall()).values()) == {0}
    assert login(browser).status_code == 200  # empezar altas no bloquea el acceso
    assert start(browser, WRONG).status_code == 400 and start(browser).status_code == 201
    counted = dict(migrator.execute(KEYS).fetchall())  # el fallo sigue contado; el par se borra
    assert sorted(counted.values()) == [1, 1]
    full = [settings.LOGIN_THROTTLE["ip"][0]]  # la dirección, en su umbral: el siguiente bloquea
    migrator.execute("UPDATE login_throttles SET failures = %s WHERE key LIKE 'ip:%%'", full)
    assert start(browser).status_code == 201  # y devuelve también el bloqueo que empezó con él
    assert start(browser).status_code == 201


def test_a_start_waits_for_the_lock_of_its_account_row(
    browser: Client, migrator: psycopg.Connection[Any]
) -> None:
    ana = make_user(email=EMAIL)
    assert login(browser).status_code == 200
    browser.raise_request_exception = False
    with connection.cursor() as cursor:
        cursor.execute("SET lock_timeout = '200ms'")
    with migrator.transaction():  # otra transacción retiene la fila de la cuenta: otra alta
        migrator.execute("SELECT 1 FROM users WHERE id = %s FOR NO KEY UPDATE", [ana.pk])
        assert reply(start(browser)) == (500, b'{"code":"INTERNAL_ERROR"}')  # espera: no se cuela
    assert not MfaDevice.objects.exists()
    assert start(browser).status_code == 201  # libre la fila, empieza

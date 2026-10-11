"""F2-89: `POST /api/v1/auth/password-reset/confirm/` cambia la contraseña con un enlace de
recuperación (ADR-021 §5 a §7). Middleware, CSRF, límite y auditoría de plataforma reales."""

import json
import threading
from typing import Any

import psycopg
import pytest
from django.contrib.auth import authenticate
from django.core import mail as outbox
from django.db import DatabaseError, connection, transaction
from django.test import Client, RequestFactory
from django.test.utils import CaptureQueriesContext

from apps.accounts import recovery
from apps.accounts.models import PasswordReset, User
from apps.accounts.tasks import send_password_changed
from apps.audit import platform
from core.api.errors import ApiError
from tests import test_auth_api
from tests.factories import TEST_PASSWORD, make_user
from tests.test_auth_api import AGENT, SESSION, audit, login, reply, send
from tests.test_password_reset_issue import LINK

real_stack, browser = test_auth_api.real_stack, test_auth_api.browser
pytestmark = pytest.mark.usefixtures("tenant_db")
CHANGE = "/api/v1/auth/password-reset/confirm/"
EMAIL = "ana@example.com"
NEW = "otra-clave-larga-y-rara-73"
REFUSED = (
    400,
    {"code": "INVALID_RESET_LINK", "message": "El enlace no vale o ya caducó. Pide uno nuevo."},
)
AGE = (
    "UPDATE password_resets SET created_at = now() - %s::interval,"
    " expires_at = now() - %s::interval WHERE user_id = %s"
)
USERS = "SELECT password, session_epoch FROM users ORDER BY email"
STATE = "SELECT password, session_epoch FROM users WHERE email = %s"


@pytest.fixture(autouse=True)
def origin(settings: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    settings.APP_ORIGIN = "https://app.example.com"
    # El aviso del cambio (F2-93) no se encola aquí: no hay broker. Lo prueba su propio fichero.
    monkeypatch.setattr(send_password_changed, "apply_async", lambda **how: None)


def issued(email: str = EMAIL) -> str:
    """Emite un enlace de verdad y devuelve su secreto, el que iría en el correo."""
    assert recovery.issue_link(email) is True
    (secret,) = LINK.findall(str(outbox.outbox[-1].body))
    return str(secret)


def change(client: Client, token: Any, password: Any = NEW) -> Any:
    return send(client, "POST", CHANGE, {"token": token, "password": password})


def said(answer: Any) -> tuple[int, Any]:
    return answer.status_code, json.loads(answer.content) if answer.content else None


def test_the_latest_link_changes_the_password_once_closes_every_session_and_opens_none(
    browser: Client, migrator: psycopg.Connection[Any]
) -> None:
    ana = make_user(email=EMAIL)
    other = Client(enforce_csrf_checks=True)  # otra sesión abierta de la misma cuenta
    other.get(test_auth_api.CSRF)
    assert login(other).status_code == 200 and other.get(SESSION).status_code == 200
    secret, before = issued(), migrator.execute(STATE, [EMAIL]).fetchone()
    with CaptureQueriesContext(connection) as seen:
        done = change(browser, secret)
    assert reply(done) == (204, b"") and "crm_session" not in browser.cookies  # no inicia sesión
    locks = [query["sql"] for query in seen if " FOR " in query["sql"]]
    assert ['"users"' in sql for sql in locks] == [True, False]  # la cuenta, y después el enlace
    assert "NO KEY UPDATE" in locks[0] and '"password_resets"' in locks[1]
    after = migrator.execute(STATE, [EMAIL]).fetchone()
    assert before and after and after[0] != before[0] and after[0].startswith("argon2$argon2id$")
    assert after[1] == before[1] + 1  # la época: todas las sesiones de la cuenta, fuera
    assert other.get(SESSION).status_code == 401
    assert authenticate(username=EMAIL, password=NEW) == ana
    assert authenticate(username=EMAIL, password=TEST_PASSWORD) is None
    link = PasswordReset.objects.get()
    assert link.used_at is not None and link.used_at >= link.created_at
    assert audit(migrator)[-1] == (
        "auth.password.changed",
        "SUCCESS",
        "ANONYMOUS",
        None,
        None,
        "user",
        ana.pk,
        '{"method": "reset_link"}',
        "127.0.0.1",
        AGENT,
    )
    assert said(change(browser, secret, NEW + NEW)) == REFUSED  # un solo uso
    assert authenticate(username=EMAIL, password=NEW) == ana
    assert login(browser, EMAIL, NEW).status_code == 200  # y se entra por el acceso normal
    logged = json.dumps([row[7] for row in audit(migrator)])
    assert secret not in logged and NEW not in logged and link.token_hash not in logged


def test_a_link_that_is_not_valid_gets_one_answer_whatever_the_reason_and_changes_nothing(
    browser: Client, migrator: psycopg.Connection[Any], settings: Any
) -> None:
    settings.PASSWORD_RESET_THROTTLE = {**settings.PASSWORD_RESET_THROTTLE, "change": (50, 900)}
    ana, luis = make_user(email=EMAIL), make_user(email="luis@example.com")
    eva = make_user(email="eva@example.com")
    old, expired, off = issued(), issued("luis@example.com"), issued("eva@example.com")
    migrator.execute(AGE, ["3 minutes", "-57 minutes", ana.pk])
    latest = issued()  # uno más nuevo para ana: el anterior deja de valer
    migrator.execute(AGE, ["61 minutes", "1 second", luis.pk])
    User.objects.filter(pk=eva.pk).update(is_active=False)
    before = migrator.execute(USERS).fetchall()
    cases = [
        ("a" * 43, "unknown", None),  # con la forma de un secreto, pero de nadie
        ("corto", "malformed", None),
        (latest + "x", "malformed", None),
        (latest[:-1] + "!", "malformed", None),
        ("", None, None),  # ni llega: el cuerpo no es válido
        (old, "superseded", ana.pk),
        (expired, "expired", luis.pk),
        (off, "account", eva.pk),
    ]
    for token, reason, account in cases:
        answer = change(browser, token)
        if reason is None:
            assert (answer.status_code, list(answer.json()["fields"])) == (400, ["token"])
            continue
        assert said(answer) == REFUSED, reason  # la misma respuesta, sea cual sea la razón
        row = audit(migrator)[-1]
        assert row[:4] == ("auth.password.changed", "DENIED", "ANONYMOUS", None)
        assert row[5:8] == (
            "user" if account else None,
            account,
            json.dumps({"method": "reset_link", "reason": reason}),
        )
    User.objects.filter(pk=eva.pk).update(is_active=True, is_platform_staff=True)
    assert said(change(browser, off)) == REFUSED  # personal de plataforma: tampoco
    assert json.loads(audit(migrator)[-1][7])["reason"] == "account"
    assert migrator.execute(USERS).fetchall() == before
    assert not PasswordReset.objects.exclude(used_at=None).exists()
    assert change(browser, latest).status_code == 204  # y el último de ana sigue valiendo
    assert said(change(browser, latest)) == REFUSED
    used = json.dumps({"method": "reset_link", "reason": "used"})
    assert audit(migrator)[-1][5:8] == ("user", ana.pk, used)  # el usado también lleva su cuenta
    for token in ("a" * 43, "corto", old, expired, off, latest):  # ni con una contraseña débil
        assert said(change(browser, token, "corta")) == REFUSED  # se valida solo si el enlace vale


def test_a_password_the_validators_refuse_does_not_spend_the_link(
    browser: Client, migrator: psycopg.Connection[Any]
) -> None:
    make_user(email=EMAIL)
    secret, before = issued(), migrator.execute(STATE, [EMAIL]).fetchone()
    for weak, codes in (
        ("corta", ["password_too_short"]),
        ("1234567890123", ["password_too_common", "password_entirely_numeric"]),
        ("ana@example.com", ["password_too_similar"]),  # con la cuenta delante: se parece a ella
    ):
        answer = change(browser, secret, weak)
        assert (answer.status_code, answer.json()["code"]) == (400, "VALIDATION_ERROR"), weak
        assert [error["code"] for error in answer.json()["fields"]["password"]] == codes
    for missing in ({"token": secret}, {"password": NEW}, {}):
        assert send(browser, "POST", CHANGE, missing).status_code == 400
    assert migrator.execute(STATE, [EMAIL]).fetchone() == before
    assert PasswordReset.objects.get().used_at is None and audit(migrator) == []  # no pasó nada
    spaced = "  con espacios, tal cual  "
    assert change(browser, secret, spaced).status_code == 204  # sin recortar
    assert authenticate(username=EMAIL, password=spaced) is not None


def test_attempts_are_limited_by_network_address_with_their_own_counter(
    browser: Client, migrator: psycopg.Connection[Any], settings: Any
) -> None:
    settings.PASSWORD_RESET_THROTTLE = {**settings.PASSWORD_RESET_THROTTLE, "change": (3, 600)}
    make_user(email=EMAIL)
    secret = issued()
    assert change(browser, "").status_code == 400  # un cuerpo que no cumple el contrato no cuenta
    for token in ("b" * 43, "corto", "b" * 43):  # todo intento cuenta, también el mal formado
        assert said(change(browser, token)) == REFUSED
    limited = change(browser, secret)  # el cuarto, aunque el enlace valga
    assert reply(limited) == (429, b'{"code":"RATE_LIMITED"}')
    assert 590 < int(limited["Retry-After"]) <= 600
    assert len(audit(migrator)) == 3 and PasswordReset.objects.get().used_at is None
    keys = dict(migrator.execute("SELECT key, failures FROM login_throttles").fetchall())
    assert keys == {"rst:chg:ip:127.0.0.1": 3}  # ni el de pedir enlaces ni los del acceso
    migrator.execute("UPDATE login_throttles SET updated_at = now() - interval '601 seconds'")
    assert change(browser, secret).status_code == 204


def test_without_its_audit_row_the_password_does_not_change_and_a_refusal_stays_the_same(
    browser: Client, migrator: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    make_user(email=EMAIL)
    secret, before = issued(), migrator.execute(STATE, [EMAIL]).fetchone()

    def broken(*args: Any, **kwargs: Any) -> Any:
        raise DatabaseError("sin partición")

    browser.raise_request_exception = False
    monkeypatch.setattr(platform, "record", broken)
    assert reply(change(browser, secret)) == (500, b'{"code":"INTERNAL_ERROR"}')  # falla cerrado
    assert migrator.execute(STATE, [EMAIL]).fetchone() == before
    assert PasswordReset.objects.get().used_at is None  # ni el enlace se gastó
    assert said(change(browser, "c" * 43)) == REFUSED  # un rechazo sin auditar: la misma respuesta
    monkeypatch.undo()
    assert change(browser, secret).status_code == 204
    with transaction.atomic(), pytest.raises(RuntimeError, match="transacción"):
        recovery.change_password(RequestFactory().post(CHANGE), token=secret, password=NEW)


def test_two_changes_at_once_with_one_link_change_the_password_once() -> None:
    make_user(email=EMAIL)
    secret, start, results = issued(), threading.Barrier(2), list[str]()

    def run(password: str) -> None:
        try:
            start.wait(timeout=5)
            recovery.change_password(RequestFactory().post(CHANGE), token=secret, password=password)
            results.append(password)
        except ApiError as refused:
            results.append(str(refused.code))
        finally:
            connection.close()

    threads = [threading.Thread(target=run, args=(f"clave-simultánea-{n}-larga",)) for n in (1, 2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    won = [result for result in results if result != "INVALID_RESET_LINK"]
    assert len(results) == 2 and len(won) == 1  # una cambia; la otra, el enlace ya usado
    assert authenticate(username=EMAIL, password=won[0]) is not None
    assert User.objects.get(email=EMAIL).session_epoch == 1


def test_it_needs_csrf_but_no_session_and_only_takes_post() -> None:
    make_user(email=EMAIL)
    secret, bare = issued(), Client(enforce_csrf_checks=True)
    body = {"token": secret, "password": NEW}
    assert reply(bare.post(CHANGE, body, "application/json")) == (403, b'{"code":"CSRF_FAILED"}')
    assert bare.get(test_auth_api.CSRF).status_code == 204
    for method in ("GET", "PUT", "PATCH", "DELETE"):
        assert send(bare, method, CHANGE, body).status_code == 405
    assert PasswordReset.objects.get().used_at is None
    assert change(bare, secret).status_code == 204 and "crm_session" not in bare.cookies

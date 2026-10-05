"""F2-03A: iniciar sesión y sesión actual (ADR-003 §2–3, ADR-013 §5, ADR-014 §3). Middleware,
sesiones en BD y auditoría de plataforma reales."""

import io
import json
import logging
from collections.abc import Iterator
from datetime import timedelta
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import psycopg
import pytest
from django.contrib.sessions.backends.db import SessionStore
from django.contrib.sessions.models import Session
from django.db import DatabaseError
from django.test import Client
from django.utils import timezone

from apps.accounts.models import User
from apps.audit import platform
from apps.audit.platform import identifier_hash
from config.settings import base
from core.api.parsers import Utf8JSONParser
from core.observability import reporting
from tests.factories import TEST_PASSWORD, make_user
from tests.test_memberships import join

pytestmark = pytest.mark.usefixtures("tenant_db")
EMAIL = "ana@example.com"
LOGIN, SESSION, CSRF = "/api/v1/auth/login/", "/api/v1/auth/session/", "/api/v1/auth/csrf/"
INVALID = (401, b'{"code":"INVALID_CREDENTIALS"}')
NO_SESSION = (401, b'{"code":"NOT_AUTHENTICATED"}')
AGENT = "Mozilla/5.0 (pruebas)"
# La IP auditada es la del servidor ASGI: una cabecera del cliente nunca la cambia.
HEADERS = {"User-Agent": AGENT, "X-Forwarded-For": "203.0.113.9"}
WRONG = TEST_PASSWORD.upper()  # una contraseña que no es la de nadie


@pytest.fixture(autouse=True)
def real_stack(settings: Any) -> None:
    settings.ROOT_URLCONF = "config.urls"  # las rutas reales del proyecto
    settings.MIDDLEWARE = base.MIDDLEWARE
    settings.TENANCY_MEMBERSHIP_RESOLVER = base.TENANCY_MEMBERSHIP_RESOLVER


@pytest.fixture
def ana(orgs: dict[str, UUID]) -> User:
    user = make_user(email=EMAIL, first_name="Ana", last_name="López")
    join(orgs["A"], user)
    return user


@pytest.fixture
def browser() -> Iterator[Client]:
    """Un navegador: comprueba CSRF y ya pidió la cookie `csrftoken`."""
    client = Client(enforce_csrf_checks=True, headers=HEADERS)
    response = client.get(CSRF)
    assert response.status_code == 204
    token = response.cookies["csrftoken"]  # legible por JS y válida sobre HTTP
    assert not token["httponly"] and not token["secure"] and token["samesite"] == "Lax"
    assert token["path"] == "/" and not token["domain"]
    yield client


def send(client: Client, method: str, path: str, body: Any = None) -> Any:
    token = client.cookies["csrftoken"].value if "csrftoken" in client.cookies else ""
    return client.generic(
        method, path, json.dumps(body or {}), "application/json", headers={"X-CSRFToken": token}
    )


def login(client: Client, email: str = EMAIL, password: str = TEST_PASSWORD) -> Any:
    return send(client, "POST", LOGIN, {"email": email, "password": password})


def reply(response: Any) -> tuple[int, bytes]:
    return response.status_code, response.content


def audit(migrator: psycopg.Connection[Any]) -> list[tuple[Any, ...]]:
    return migrator.execute(
        "SELECT action, result, actor_type, actor_id, identifier_hash, entity_type, entity_id, "
        "metadata::text, host(ip), user_agent FROM platform_audit_logs ORDER BY occurred_at, id"
    ).fetchall()


def test_login_opens_a_rotated_session_with_a_safe_cookie(
    ana: User, browser: Client, migrator: psycopg.Connection[Any]
) -> None:
    store = SessionStore()
    store.create()  # una sesión anónima fijada en el navegador antes del login
    browser.cookies["crm_session"] = planted = str(store.session_key)
    csrf_before = browser.cookies["csrftoken"].value
    response = login(browser, email="  Ana@Example.com ")
    assert response.status_code == 200
    assert response.json() == {
        "user": {"id": str(ana.pk), "email": EMAIL, "first_name": "Ana", "last_name": "López"}
    }
    cookie = response.cookies["crm_session"]
    assert cookie["httponly"] and cookie["samesite"] == "Lax" and cookie["path"] == "/"
    assert not cookie["domain"] and not cookie["secure"] and int(cookie["max-age"]) == 43200
    assert cookie.value != planted  # el ID de sesión rota
    assert browser.cookies["csrftoken"].value != csrf_before  # y el token CSRF
    stale = Client()
    stale.cookies["crm_session"] = planted
    assert reply(stale.get(SESSION)) == NO_SESSION  # la clave anterior ya no sirve
    assert browser.get(SESSION).json()["user"]["id"] == str(ana.pk)
    assert Session.objects.count() == 1
    (row,) = audit(migrator)
    assert row[:7] == ("auth.login.succeeded", "SUCCESS", "USER", ana.pk, None, None, None)
    assert row[8:] == ("127.0.0.1", AGENT)
    whole = str(row) + str(Session.objects.get().get_decoded())
    assert TEST_PASSWORD not in whole and EMAIL not in whole
    # Otro login del mismo usuario sobre su sesión también emite un ID nuevo.
    first = browser.cookies["crm_session"].value
    assert login(browser).cookies["crm_session"].value != first
    stale.cookies["crm_session"] = first
    assert reply(stale.get(SESSION)) == NO_SESSION and Session.objects.count() == 1


def test_login_rejects_without_telling_why(ana: User, migrator: psycopg.Connection[Any]) -> None:
    inactive = make_user(email="baja@example.com", is_active=False)
    invited = make_user(email="sin-clave@example.com", password=None)  # contraseña inutilizable
    attempts = [
        (EMAIL, WRONG, "wrong_password", ana.pk),
        ("  Ana@Example.com ", WRONG, "wrong_password", ana.pk),  # se canoniza antes de auditar
        ("nadie@example.com", TEST_PASSWORD, "unknown_identifier", None),
        ("baja@example.com", TEST_PASSWORD, "inactive_user", inactive.pk),
        ("baja@example.com", WRONG, "inactive_user", inactive.pk),
        ("sin-clave@example.com", TEST_PASSWORD, "wrong_password", invited.pk),
        ("no-es-un-email", TEST_PASSWORD, "unknown_identifier", None),
    ]
    replies = set()
    for email, password, _reason, _account in attempts:
        client = Client(enforce_csrf_checks=True, headers=HEADERS)
        client.get(CSRF)
        response = login(client, email, password)
        headers = sorted((k, v) for k, v in response.headers.items() if k != "X-Request-ID")
        replies.add((reply(response), tuple(headers)))
        assert response.headers["WWW-Authenticate"] == "Session"
        assert not response.cookies and Session.objects.count() == 0  # ninguna cookie
    assert len(replies) == 1 and replies.pop()[0] == INVALID  # estado, cuerpo y cabeceras
    rows = audit(migrator)
    assert [json.loads(row[7])["reason"] for row in rows] == [a[2] for a in attempts]
    for row, (email, _password, _reason, account) in zip(rows, attempts, strict=True):
        assert row[:5] == ("auth.login.failed", "FAILED", "ANONYMOUS", None, identifier_hash(email))
        assert row[5:7] == (("user", account) if account else (None, None))
        assert row[8:] == ("127.0.0.1", AGENT)
    everything = str(rows)
    for secret in (TEST_PASSWORD, WRONG, "example.com", "no-es-un-email"):
        assert secret not in everything


def test_login_input_is_validated_and_needs_a_csrf_token(ana: User, browser: Client) -> None:
    missing = send(browser, "POST", LOGIN, {"email": EMAIL})
    assert missing.status_code == 400 and missing.json()["code"] == "VALIDATION_ERROR"
    assert "WWW-Authenticate" not in missing.headers  # solo los 401 lo llevan
    assert list(missing.json()["fields"]) == ["password"]
    assert send(browser, "POST", LOGIN, {"email": EMAIL, "password": "x" * 2000}).status_code == 400
    no_token = Client(enforce_csrf_checks=True)
    denied = no_token.post(LOGIN, {"email": EMAIL, "password": TEST_PASSWORD}, "application/json")
    assert reply(denied) == (403, b'{"code":"CSRF_FAILED"}')  # el login también exige CSRF
    assert Session.objects.count() == 0
    for method in ("GET", "PUT", "DELETE"):
        assert send(browser, method, LOGIN).status_code == 405
    make_user(email="espacio@example.com", password=TEST_PASSWORD + " ")
    assert reply(login(browser, "espacio@example.com", TEST_PASSWORD)) == INVALID  # no se recorta
    assert login(browser, "espacio@example.com", TEST_PASSWORD + " ").status_code == 200


@pytest.mark.parametrize("charset", ["zlib", "bz2", "rot13", "hex", "utf-16", "latin-1"])
def test_the_body_charset_never_picks_a_codec(ana: User, browser: Client, charset: str) -> None:
    """`charset=zlib` haría que el servidor descomprimiera el cuerpo antes de leerlo."""
    body = json.dumps({"email": EMAIL, "password": TEST_PASSWORD}).encode()

    def post(name: str) -> Any:
        token = {"X-CSRFToken": browser.cookies["csrftoken"].value}
        return browser.generic(
            "POST", LOGIN, body, f"application/json; charset={name}", headers=token
        )

    assert reply(post(charset)) == (415, b'{"code":"UNSUPPORTED_MEDIA_TYPE"}')
    assert Session.objects.count() == 0
    for known in ("UTF-8", "no-existe"):  # un charset desconocido se ignora: se lee como UTF-8
        assert post(known).status_code == 200


@pytest.mark.parametrize("opening, closing", [(b"[", b"]"), (b'{"a":', b"}"), (b"[", b"")])
def test_a_body_nested_too_deep_is_a_parse_error(
    ana: User, browser: Client, caplog: pytest.LogCaptureFixture, opening: bytes, closing: bytes
) -> None:
    """Sin sesión y con ella: un anidamiento que desbordaría al analizador, o a quien recorre
    lo que el analizador sí lee, es un 400, no un 500."""
    too_deep, invalid = (400, b'{"code":"PARSE_ERROR"}'), "VALIDATION_ERROR"

    def post(depth: int, field: bool = False) -> Any:
        token = {"X-CSRFToken": browser.cookies["csrftoken"].value}
        body = opening * depth + b"1" * bool(closing) + closing * depth
        if field:
            body = b'{"password":"x","email":' + body + b"}"
        return browser.generic("POST", LOGIN, body, "application/json", headers=token)

    caplog.set_level(logging.ERROR, logger="django.request")
    assert reply(post(200_000)) == too_deep  # desborda al analizador
    assert Session.objects.count() == 0
    if closing:
        assert post(100).json()["code"] == invalid  # el límite: se lee y se valida como siempre
        assert reply(post(101)) == too_deep
        assert post(99, field=True).json()["code"] == invalid  # el campo cuenta un nivel más
        for depth in (100, 2_000, 35_000, 70_000, 110_000):  # se leen, pero no se recorren
            assert reply(post(depth, field=True)) == too_deep, depth
    login(browser)
    assert reply(post(200_000)) == too_deep
    assert Session.objects.count() == 1  # la sesión que había sigue ahí
    assert not caplog.records  # nada que el servidor tenga que mirar


@pytest.mark.parametrize("error", [OSError, RuntimeError])
def test_only_the_overflow_becomes_an_unreadable_body(error: type[Exception]) -> None:
    class Unreadable(io.BytesIO):
        def read(self, size: int | None = -1) -> bytes:
            raise error("se cortó la conexión")

    with pytest.raises(error) as raised:
        Utf8JSONParser().parse(Unreadable(), "application/json", {"encoding": "utf-8"})
    assert type(raised.value) is error


def test_session_expires_twelve_hours_after_login(ana: User, browser: Client) -> None:
    login(browser)
    session = Session.objects.get()
    assert abs((session.expire_date - timezone.now()).total_seconds() - 12 * 3600) < 60
    assert browser.get(SESSION).status_code == 200
    Session.objects.update(expire_date=timezone.now() - timedelta(seconds=1))
    assert reply(browser.get(SESSION)) == NO_SESSION
    assert reply(browser.get("/api/v1/o/org-a/widgets/")) == NO_SESSION  # también en un tenant


def test_the_session_of_a_deactivated_user_stops_authenticating(ana: User, browser: Client) -> None:
    login(browser)
    User.objects.filter(pk=ana.pk).update(is_active=False)
    assert reply(browser.get(SESSION)) == NO_SESSION


def test_login_fails_closed_when_it_cannot_be_audited(
    ana: User, browser: Client, monkeypatch: pytest.MonkeyPatch, migrator: psycopg.Connection[Any]
) -> None:
    def broken(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("auditoría no disponible")

    reports: list[tuple[BaseException, Any]] = []
    spy = SimpleNamespace(
        capture_exception=lambda error, context=None: reports.append((error, context))
    )
    monkeypatch.setattr(reporting, "reporter", lambda: spy)
    monkeypatch.setattr(platform, "record", broken)
    quiet = Client(enforce_csrf_checks=True, raise_request_exception=False)
    quiet.get(CSRF)
    failed = login(quiet)
    assert reply(failed) == (500, b'{"code":"INTERNAL_ERROR"}')
    assert "crm_session" not in failed.cookies and Session.objects.count() == 0
    last = migrator.execute("SELECT last_login FROM users WHERE id = %s", [ana.pk]).fetchone()
    assert last == (None,)  # nada de lo que hace el login quedó escrito
    assert reply(login(quiet, password=WRONG)) == INVALID  # un rechazo no cambia
    assert [context for _, context in reports] == [{"action": "auth.login.failed"}]  # y se reporta
    monkeypatch.setattr(User.objects, "filter", broken)  # ni siquiera si falla su consulta
    assert reply(login(quiet, password=WRONG)) == INVALID and len(reports) == 2


def test_a_failed_relogin_keeps_the_previous_session(
    ana: User, browser: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    login(browser)
    before = browser.cookies["crm_session"].value

    def broken(*args: Any, **kwargs: Any) -> None:
        raise DatabaseError("sin base de datos")

    # Al crear la sesión nueva (antes de borrar la anterior) y al auditar el acceso.
    for target, step in ((SessionStore, "exists"), (SessionStore, "create"), (platform, "record")):
        monkeypatch.setattr(target, step, broken)
        browser.raise_request_exception = False
        failed = login(browser)
        monkeypatch.undo()
        assert reply(failed) == (500, b'{"code":"INTERNAL_ERROR"}')
        assert "crm_session" not in failed.cookies
        assert [row.session_key for row in Session.objects.all()] == [before]  # nada cambió
        assert browser.get(SESSION).status_code == 200  # la sesión anterior sigue siendo suya


def test_nothing_secret_reaches_the_logs(
    ana: User, browser: Client, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    login(browser, password=WRONG)
    login(browser)
    secrets = [browser.cookies["crm_session"].value, browser.cookies["csrftoken"].value]

    def broken(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("auditoría no disponible")

    monkeypatch.setattr(platform, "record", broken)  # también con las trazas de un fallo
    browser.raise_request_exception = False
    login(browser)
    login(browser, password=WRONG)
    assert "auth.login.failed sin auditar" in caplog.text
    for secret in (TEST_PASSWORD, WRONG, EMAIL, *secrets):
        assert secret not in caplog.text


@pytest.mark.parametrize(
    ("remote", "stored"),
    [
        ("203.0.113.7%x", None),
        ("fe80::1%a%b", None),
        ("no-es-una-ip", None),
        ("fe80::1%eth0", "fe80::1"),
    ],
)
def test_an_odd_remote_address_never_breaks_the_audit(
    ana: User, migrator: psycopg.Connection[Any], remote: str, stored: str | None
) -> None:
    client = Client(enforce_csrf_checks=True, REMOTE_ADDR=remote)
    client.get(CSRF)
    assert reply(login(client, password=WRONG)) == INVALID
    assert login(client).status_code == 200
    rows = audit(migrator)
    assert [row[0] for row in rows] == ["auth.login.failed", "auth.login.succeeded"]
    assert [row[8] for row in rows] == [stored, stored]

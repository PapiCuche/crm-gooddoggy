"""F2-88: `POST /api/v1/auth/password-reset/` pide un enlace de recuperación (ADR-021 §2 y §7).
Middleware, CSRF, límite de intentos y auditoría de plataforma reales; la tarea, sin ejecutar."""

from typing import Any

import psycopg
import pytest
from django.core import mail as outbox
from django.db import DatabaseError, connection, transaction
from django.test import Client
from django.test.utils import CaptureQueriesContext

from apps.accounts import recovery, throttle
from apps.accounts.models import PasswordReset, User
from apps.accounts.tasks import purge_login_throttles, send_password_reset
from apps.audit import platform
from apps.audit.platform import identifier_hash
from tests import test_auth_api
from tests.factories import TEST_PASSWORD, make_user
from tests.test_auth_api import AGENT, audit, login, reply, send

real_stack, browser = test_auth_api.real_stack, test_auth_api.browser
pytestmark = pytest.mark.usefixtures("tenant_db")
ASK = "/api/v1/auth/password-reset/"
EMAIL = "ana@example.com"
LIMITED = b'{"code":"RATE_LIMITED"}'
KEYS = "SELECT key, failures FROM login_throttles ORDER BY key"
AGE = "UPDATE login_throttles SET updated_at = now() - %s::interval WHERE left(key, 4) = 'rst:'"


@pytest.fixture
def queued(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Lo que se encola, sin broker: los argumentos de cada `apply_async`."""
    calls: list[dict[str, Any]] = []

    def enqueue(connection: Any, **how: Any) -> None:  # con una conexión propia, que no espera
        options = connection.transport_options
        assert (connection.connect_timeout, options["max_retries"]) == (recovery.BROKER_WAIT, 0)
        assert options["socket_connect_timeout"] == options["socket_timeout"] == 3
        assert not connection.connected  # perezosa: aquí nadie abrió nada
        calls.append(how)

    monkeypatch.setattr(send_password_reset, "apply_async", enqueue)
    return calls


def ask(client: Client, email: Any = EMAIL) -> Any:
    return send(client, "POST", ASK, {"email": email})


def test_every_address_gets_the_same_answer_the_same_audit_row_and_the_same_task(
    browser: Client, queued: list[dict[str, Any]], migrator: psycopg.Connection[Any]
) -> None:
    make_user(email=EMAIL)
    make_user(email="baja@example.com", is_active=False)
    User.objects.create_superuser(email="staff@example.com", password=TEST_PASSWORD)
    asked = [EMAIL, "nadie@example.com", "baja@example.com", "staff@example.com"]
    answers = []
    for email in asked:
        with CaptureQueriesContext(connection) as seen:
            answers.append(ask(browser, f"  {email.upper()} "))  # como llegue: se canoniza
        tables = " ".join(query["sql"] for query in seen)
        assert '"users"' not in tables and "password_resets" not in tables  # no busca la cuenta
    assert {reply(answer) for answer in answers} == {(204, b"")}
    assert len({tuple(sorted(answer.headers)) for answer in answers}) == 1  # mismas cabeceras
    assert queued == [
        {"args": [email], "argsrepr": "('[EMAIL]',)", "expires": 900, "retry": False}
        for email in asked  # la forma canónica, y enmascarada para lo que Celery enseña
    ]
    assert audit(migrator) == [
        ("auth.password.reset_requested", "SUCCESS", "ANONYMOUS", None, identifier_hash(email))
        + (None, None, "{}", "127.0.0.1", AGENT)  # sin cuenta: la ruta no la mira
        for email in asked
    ]
    assert not PasswordReset.objects.exists() and outbox.outbox == []  # eso es de la tarea


@pytest.mark.parametrize(
    "email", ["sin-arroba", "a@", "eñe@example.com", "", "   ", 7, None, [EMAIL]]
)
def test_what_is_not_an_address_is_a_400_that_counts_audits_and_queues_nothing(
    browser: Client, queued: list[dict[str, Any]], migrator: psycopg.Connection[Any], email: Any
) -> None:
    answer = ask(browser, email)
    assert (answer.status_code, answer.json()["code"]) == (400, "VALIDATION_ERROR")
    assert list(answer.json()["fields"]) == ["email"]
    assert queued == [] and audit(migrator) == [] and migrator.execute(KEYS).fetchall() == []


def test_the_limit_is_its_own_by_address_and_by_identifier_and_a_refusal_does_nothing(
    browser: Client, queued: list[dict[str, Any]], migrator: psycopg.Connection[Any], settings: Any
) -> None:
    settings.PASSWORD_RESET_THROTTLE = {"ip": (4, 3600), "identifier": (2, 600)}
    make_user(email=EMAIL)
    mark = f"rst:id:{identifier_hash(EMAIL)}"
    assert ask(browser).status_code == 204
    migrator.execute(AGE, ["100 seconds"])  # ventana fija: empezó con la primera petición
    assert ask(browser, "ANA@example.com").status_code == 204  # otra escritura, un identificador
    third = ask(browser)  # el tercero para un correo, exista o no: espera lo que queda de ventana
    assert reply(third) == (429, LIMITED) and 490 < int(third["Retry-After"]) <= 500
    for _ in range(2):
        assert ask(browser, "nadie@example.com").status_code == 204
    for email in ("nadie@example.com", "otra@example.com"):  # el quinto desde la red, sea cual sea
        fifth = ask(browser, email)
        assert reply(fifth) == (429, LIMITED) and 3490 < int(fifth["Retry-After"]) <= 3500
    assert len(queued) == 4 and len(audit(migrator)) == 4  # un rechazo ni encola ni audita
    counted = dict(migrator.execute(KEYS).fetchall())
    assert counted == {  # y no cuenta: los contadores se quedan en su tope
        "rst:ip:127.0.0.1": 4,
        mark: 2,
        f"rst:id:{identifier_hash('nadie@example.com')}": 2,
    }
    assert login(browser, EMAIL, "otra-clave-que-no-es").status_code == 401  # el acceso, aparte
    assert login(browser).status_code == 200  # y pedir enlaces no bloqueó a su dueña
    assert dict(migrator.execute(KEYS).fetchall()).items() >= counted.items()
    migrator.execute(AGE, ["601 seconds"])  # pasa la ventana del correo, no la de la red
    assert reply(ask(browser)) == (429, LIMITED)
    migrator.execute(AGE, ["3601 seconds"])
    assert ask(browser).status_code == 204
    assert dict(migrator.execute(KEYS).fetchall())[mark] == 1  # una ventana nueva


def test_without_its_audit_row_nothing_is_queued_and_a_broker_failure_is_the_same_for_anyone(
    browser: Client,
    queued: list[dict[str, Any]],
    migrator: psycopg.Connection[Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    make_user(email=EMAIL)
    real = platform.record

    def broken(*args: Any, **kwargs: Any) -> Any:
        raise DatabaseError("sin partición")

    browser.raise_request_exception = False
    monkeypatch.setattr(platform, "record", broken)
    failed = [reply(ask(browser, email)) for email in (EMAIL, "nadie@example.com")]
    assert failed == [(500, b'{"code":"INTERNAL_ERROR"}')] * 2 and queued == []  # falla cerrado
    monkeypatch.setattr(platform, "record", real)

    def refuse(**how: Any) -> None:
        raise ConnectionError(f"broker caído al encolar {how['args']}")

    monkeypatch.setattr(send_password_reset, "apply_async", refuse)
    failed = [reply(ask(browser, email)) for email in (EMAIL, "nadie@example.com")]
    assert failed == [(500, b'{"code":"INTERNAL_ERROR"}')] * 2
    assert [row[0] for row in audit(migrator)] == ["auth.password.reset_requested"] * 2
    with transaction.atomic(), pytest.raises(RuntimeError, match="transacción"):
        recovery.request_link(None, email=EMAIL)  # type: ignore[arg-type]


def test_it_needs_csrf_but_no_session_and_only_takes_post(
    queued: list[dict[str, Any]],
) -> None:
    bare = Client(enforce_csrf_checks=True)
    assert reply(bare.post(ASK, {"email": EMAIL}, "application/json")) == (
        403,
        b'{"code":"CSRF_FAILED"}',
    )
    assert bare.get(test_auth_api.CSRF).status_code == 204
    assert (
        ask(bare).status_code == 204 and "crm_session" not in bare.cookies
    )  # sin sesión, antes ni después
    for method in ("GET", "PUT", "PATCH", "DELETE"):
        assert send(bare, method, ASK, {"email": EMAIL}).status_code == 405
    assert len(queued) == 1


def test_its_counters_are_purged_by_their_own_window_not_by_the_login_one(
    browser: Client, queued: list[dict[str, Any]], migrator: psycopg.Connection[Any], settings: Any
) -> None:
    settings.PASSWORD_RESET_THROTTLE = {"ip": (20, 3600), "identifier": (5, 600)}  # purga: la mayor
    assert ask(browser).status_code == 204
    assert login(browser, "nadie@example.com", "una-clave-cualquiera").status_code == 401
    longest = max(limit.window for limit in throttle.limits().values())
    migrator.execute(
        "UPDATE login_throttles SET updated_at = now() - %s * interval '1 second'",
        [longest + 86_401],
    )
    migrator.execute(AGE, ["3599 seconds"])  # dentro de su ventana, y fuera de la del acceso
    purge_login_throttles.run()
    left = [key for key, _ in migrator.execute(KEYS).fetchall()]
    assert left == [f"rst:id:{identifier_hash(EMAIL)}", "rst:ip:127.0.0.1"]  # los del acceso, no
    migrator.execute(AGE, ["3601 seconds"])
    connection.cursor().execute("SET lock_timeout = '2s'")
    migrator.execute("BEGIN; SELECT 1 FROM login_throttles WHERE key LIKE 'rst:ip:%' FOR UPDATE")
    assert purge_login_throttles.run() == 1  # salta la fila que una petición tiene bloqueada
    migrator.execute("ROLLBACK")
    assert purge_login_throttles.run() == 1 and migrator.execute(KEYS).fetchall() == []

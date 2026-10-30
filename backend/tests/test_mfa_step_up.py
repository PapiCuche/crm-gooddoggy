"""F2-125: `POST /api/v1/auth/mfa/step-up/` y la comprobación de 15 minutos (ADR-023 §7, ADR-003
§4). Middleware, CSRF, motor de autorización, límites y auditoría reales."""

import time
from threading import Thread
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest
from django.contrib.sessions.backends.db import SessionStore
from django.contrib.sessions.models import Session
from django.db import connection
from django.test import Client
from django.urls import path
from rest_framework.response import Response
from rest_framework.views import APIView

import config.urls
from apps.accounts import code_limit, mfa, mfa_step_up
from apps.accounts.models import MfaCodeAttempt, MfaDevice
from apps.accounts.services import AUTH_AT, EPOCH, SEEN_AT
from apps.accounts.tasks import send_mfa_code_limit
from apps.audit import platform
from core.api import step_up
from core.api.errors import ApiError
from tests import test_auth_api, test_authorization, test_mfa_second_factor
from tests.factories import make_user
from tests.test_auth_api import SESSION, audit, login, reply, send
from tests.test_authorization import give
from tests.test_login_second_step import STEP_TWO
from tests.test_mfa_second_factor import SECRET, code_at, enrolled

world, real_stack = test_authorization.world, test_auth_api.real_stack
browser, clock = test_auth_api.browser, test_mfa_second_factor.clock
pytestmark = pytest.mark.usefixtures("tenant_db")
STEP_UP, STATUS, ORG = "/api/v1/auth/mfa/step-up/", "/api/v1/auth/mfa/", "/api/v1/o/org-a/"
MINUTES = 60


class Sensitive(APIView):
    """Una vista de prueba marcada con step-up: lee y escribe, las dos pidiéndolo."""

    required_permissions = {"GET": "users.view", "POST": "users.view"}
    step_up_methods: Any = {"GET", "POST"}

    def get(self, request: Any) -> Response:
        return Response({"ok": True})

    post = get


WritesOnly: Any = type(
    "WritesOnly", (Sensitive,), {"step_up_methods": {"post"}}
)  # minúsculas: vale
Misdeclared: Any = type(
    "Misdeclared", (Sensitive,), {"step_up_methods": "POST"}
)  # un texto: lo pide todo
Unmarked: Any = type("Unmarked", (Sensitive,), {"step_up_methods": None})
urlpatterns = [
    *config.urls.urlpatterns,
    *(
        path(ORG[1:] + f"{name}/", view.as_view())
        for name, view in (
            ("sensitive", Sensitive),
            ("writes", WritesOnly),
            ("misdeclared", Misdeclared),
            ("unmarked", Unmarked),
        )
    ),
]


@pytest.fixture(autouse=True)
def stack(real_stack: None, settings: Any, monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    settings.ROOT_URLCONF = __name__
    calls: list[Any] = []
    monkeypatch.setattr(send_mfa_code_limit, "apply_async", lambda **how: calls.append(how["args"]))
    return calls


def required(answer: Any) -> bool:
    body = {"code": "STEP_UP_REQUIRED", "message": step_up.MESSAGE}
    return bool(answer.status_code == 403 and answer.json() == body)


def inside(client: Client, shown: list[str]) -> None:
    """Entra por el acceso de verdad, con un código de respaldo (el paso TOTP sigue sin gastar)."""
    assert login(client).status_code == 202
    assert send(client, "POST", STEP_TWO, {"code": shown.pop()}).status_code == 200


def aged(client: Client, seconds: float) -> None:
    """La verificación de esta sesión, de hace `seconds`."""
    session = client.session
    session[mfa.MFA_AT] = int(time.time() - seconds)
    session.save()


def step(client: Client, code: Any) -> Any:
    return send(client, "POST", STEP_UP, {"code": code})


def ready(world: Any, shown: list[str] | None = None) -> tuple[Client, list[str]]:
    """Una sesión con MFA de `ana`, miembro de A con `users.view`, ya verificada en el acceso."""
    give(world.a, world.membership, {"users.view": None})
    shown = enrolled(world.ana) if shown is None else shown
    client = Client(enforce_csrf_checks=True)
    client.get(test_auth_api.CSRF)
    inside(client, shown)
    return client, shown


def test_a_session_verified_16_minutes_ago_needs_a_step_up_and_a_valid_code_renews_it(
    world: Any, clock: SimpleNamespace, migrator: psycopg.Connection[Any]
) -> None:
    browser, shown = ready(world)
    assert browser.get(ORG + "sensitive/").status_code == 200  # recién entrada
    aged(browser, 16 * MINUTES)
    assert required(browser.get(ORG + "sensitive/")) and required(
        send(browser, "POST", ORG + "sensitive/")
    )
    assert browser.get(STATUS).json()["step_up_expires_in"] is None
    before, code = browser.cookies["crm_session"].value, code_at(clock)
    migrator.execute("DELETE FROM platform_audit_logs")
    answer = step(browser, code)
    assert answer.status_code == 200 and answer.json() == {"step_up_expires_in": 900}
    assert browser.session[EPOCH] == world.ana.session_epoch  # la época y la vida siguen
    assert browser.cookies["crm_session"].value != before  # el ID de sesión rotó (ADR-003 §2)
    stale = Client()
    stale.cookies["crm_session"] = before
    assert stale.get(SESSION).status_code == 401  # y el anterior ya no sirve
    assert browser.get(ORG + "sensitive/").status_code == 200
    assert send(browser, "POST", ORG + "sensitive/").status_code == 200
    assert 898 <= browser.get(STATUS).json()["step_up_expires_in"] <= 900
    assert abs(browser.session[mfa.MFA_AT] - time.time()) < 60
    (row,) = audit(migrator)
    assert row[:4] == ("auth.mfa.step_up", "SUCCESS", "USER", world.ana.pk)
    assert row[5:8] == ("user", world.ana.pk, '{"method": "totp"}')
    assert (MfaDevice.objects.get().last_step or 0) > 1  # el código se gastó
    for secret in (code, SECRET.hex(), *shown):
        assert secret not in str(row)


def test_the_window_is_15_minutes_inclusive_and_a_mark_from_the_future_does_not_count(
    world: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    browser, _ = ready(world)
    mark = int(time.time())
    session = browser.session
    session[mfa.MFA_AT] = mark
    session.save()
    held = SimpleNamespace(now=float(mark))
    monkeypatch.setattr(step_up, "time", SimpleNamespace(time=lambda: held.now))
    for offset, ok in [
        (0, True),
        (899, True),
        (900, True),
        (900.5, False),
        (901, False),
        (-1, False),
    ]:
        held.now = mark + offset
        assert (browser.get(ORG + "sensitive/").status_code == 200) is ok, offset
    held.now = mark + 900
    assert browser.get(STATUS).json()["step_up_expires_in"] == 0  # el último segundo
    held.now = mark + 100.5
    assert browser.get(STATUS).json()["step_up_expires_in"] == 800  # hacia arriba
    held.now = mark + 899.5  # aún vale: no puede leerse 0
    assert browser.get(STATUS).json()["step_up_expires_in"] == 1


def test_the_check_reads_only_the_session_never_a_header_cookie_or_body(world: Any) -> None:
    browser, _ = ready(world)
    aged(browser, 16 * MINUTES)
    browser.cookies["mfa_at"] = str(int(time.time()))
    forged = {"X-Mfa-At": str(int(time.time()))}
    assert required(browser.get(ORG + "sensitive/", headers=forged))
    assert required(send(browser, "POST", ORG + "sensitive/", {"mfa_at": int(time.time())}))


@pytest.mark.parametrize("junk", [None, "123", 1.5, float(int(time.time())), True, "", [], {}])
def test_a_mark_that_is_not_an_integer_is_not_a_verification(world: Any, junk: Any) -> None:
    browser, _ = ready(world)
    session = browser.session
    session[mfa.MFA_AT] = junk
    session.save()
    assert required(browser.get(ORG + "sensitive/"))


def test_the_marks_of_a_session_are_read_with_the_helpers() -> None:
    held = {mfa.MFA_AT: 1000}
    assert step_up.age(held, now=1000.5) == 0.5 and step_up.age({}, now=1) is None
    assert step_up.age({mfa.MFA_AT: True}, now=2) is None and step_up.age(held, now=999) is None
    assert step_up.seconds_left(held, now=1000) == 900
    assert step_up.seconds_left(held, now=1899.5) == 1  # el último segundo no lee 0
    assert step_up.seconds_left(held, now=1900.5) is None
    assert step_up.seconds_left({mfa.MFA_AT: 1000.0}, now=1000) is None  # marca float
    left = step_up.seconds_left
    assert (left(held, now=1900), left(held, now=1901)) == (0, None)
    assert (left(held, 60, now=1060), left(held, 60, now=1061)) == (0, None)
    step_up.require_recent_mfa(SimpleNamespace(session=held), 2 * 10**9)  # no lanza
    with pytest.raises(ApiError) as refused:
        step_up.require_recent_mfa(SimpleNamespace(session={}))
    assert (refused.value.status_code, refused.value.code) == (403, "STEP_UP_REQUIRED")
    assert step_up.MAX_AGE == 15 * MINUTES


def test_an_account_without_a_second_factor_gets_the_same_code_and_cannot_verify(
    world: Any, migrator: psycopg.Connection[Any]
) -> None:
    give(world.a, world.membership, {"users.view": None})
    browser = Client(enforce_csrf_checks=True)
    browser.get(test_auth_api.CSRF)
    assert login(browser).status_code == 200  # sin factor, la contraseña abre la sesión
    answer = browser.get(ORG + "sensitive/")
    assert required(answer) and "actív" in answer.json()["message"]
    migrator.execute("DELETE FROM platform_audit_logs")
    assert reply(step(browser, "123456")) == (409, b'{"code":"MFA_NOT_ENABLED"}')
    assert not MfaCodeAttempt.objects.exists() and audit(migrator) == []
    enrolled(world.ana, confirmed=False)  # un alta a medias tampoco es un segundo factor
    assert reply(step(browser, "123456")) == (409, b'{"code":"MFA_NOT_ENABLED"}')
    assert required(browser.get(ORG + "sensitive/")) and not MfaCodeAttempt.objects.exists()


def test_the_permissions_come_first_and_the_declaration_is_per_method(
    world: Any, migrator: psycopg.Connection[Any]
) -> None:
    browser, _ = ready(world)
    aged(browser, 16 * MINUTES)
    assert browser.get(ORG + "unmarked/").status_code == 200  # sin declaración: nada que pedir
    assert browser.get(ORG + "writes/").status_code == 200  # solo pide la escritura
    assert required(send(browser, "POST", ORG + "writes/"))
    assert required(browser.get(ORG + "misdeclared/")) and required(
        send(browser, "POST", ORG + "misdeclared/")
    )
    assert browser.head(ORG + "sensitive/").status_code == 403  # HEAD pide lo que GET
    assert browser.head(ORG + "writes/").status_code == 200
    # Sin el permiso, quien no verificó no se entera de que hay un step-up.
    migrator.execute("DELETE FROM membership_roles")
    assert reply(browser.get(ORG + "sensitive/")) == (403, b'{"code":"PERMISSION_DENIED"}')
    assert send(browser, "POST", ORG + "sensitive/").status_code == 403
    assert browser.get("/api/v1/o/org-b/sensitive/").status_code == 404


def test_it_needs_a_session_csrf_and_a_code(world: Any, clock: SimpleNamespace) -> None:
    anonymous = Client(enforce_csrf_checks=True)
    anonymous.get(test_auth_api.CSRF)
    assert reply(step(anonymous, "123456")) == (401, b'{"code":"NOT_AUTHENTICATED"}')
    browser, _ = ready(world)
    bare = Client(enforce_csrf_checks=True)
    bare.cookies = browser.cookies
    assert bare.post(STEP_UP, {"code": code_at(clock)}, "application/json").status_code == 403
    assert [send(browser, verb, STEP_UP, {}).status_code for verb in ("GET", "PUT", "DELETE")] == [
        405
    ] * 3
    assert send(browser, "POST", STEP_UP, {}).json()["fields"] == {
        "code": [{"code": "required", "message": "Este campo es requerido."}]
    }
    assert send(browser, "POST", STEP_UP, {"code": "9" * 33}).status_code == 400
    assert not MfaCodeAttempt.objects.exists()  # un cuerpo roto no cuenta contra la cuenta


def test_a_wrong_code_is_the_same_answer_whatever_the_reason_and_counts(
    world: Any,
    clock: SimpleNamespace,
    migrator: psycopg.Connection[Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    browser, shown = ready(world)
    aged(browser, 16 * MINUTES)
    before = browser.cookies["crm_session"].value
    migrator.execute("DELETE FROM platform_audit_logs")
    spent = code_at(clock)
    assert step(browser, spent).status_code == 200
    aged(browser, 16 * MINUTES)
    wrong = [spent, code_at(clock, -2), code_at(clock, 2), "000000", "abcdef", "x" * 20]
    answers = {step(browser, code).content for code in wrong}
    assert len(answers) == 1 and b'"code":"invalid"' in answers.pop()
    assert step(browser, " ").status_code == 400  # en blanco: ni se mira, no cuenta
    assert MfaCodeAttempt.objects.count() == len(wrong)  # cada uno cuenta, también el repetido
    assert required(browser.get(ORG + "sensitive/"))  # nada quedó verificado
    rows = audit(migrator)
    assert [row[:2] for row in rows[1:]] == [("auth.mfa.step_up", "DENIED")] * len(wrong)
    assert {row[7] for row in rows[1:]} == {'{"reason": "code"}'}
    assert browser.cookies["crm_session"].value != before  # solo rotó el acierto, no los fallos
    for text in (*wrong[:3], spent, *shown):
        assert text not in str(rows) + caplog.text


def test_the_same_code_is_not_reusable_and_a_backup_code_is_spent_once(
    world: Any, clock: SimpleNamespace, migrator: psycopg.Connection[Any]
) -> None:
    browser, shown = ready(world)
    migrator.execute("DELETE FROM platform_audit_logs")
    typed = code_at(clock)
    assert step(browser, f"{typed[:3]} {typed[3:]}").status_code == 200  # `123 456` vale
    assert step(browser, typed).status_code == 400  # ni con otra sesión: el paso ya se gastó
    backup = shown[0]
    assert step(browser, backup).status_code == 200
    assert [step(browser, code).status_code for code in (backup, backup.lower())] == [400, 400]
    methods = [row[7] for row in audit(migrator) if row[1] == "SUCCESS"]
    assert methods == ['{"method": "totp"}', '{"method": "backup_code"}']
    enrolled(make_user(email="luis@example.com"), bytes(reversed(SECRET)))  # el de otra cuenta
    assert step(browser, code_at(clock, 0, bytes(reversed(SECRET)))).status_code == 400


def test_two_requests_with_the_same_code_do_not_both_verify(
    world: Any, clock: SimpleNamespace, migrator: psycopg.Connection[Any]
) -> None:
    give(world.a, world.membership, {"users.view": None})
    enrolled(world.ana)
    code, results = code_at(clock), []

    def request() -> Any:
        store = SessionStore()
        store.update({AUTH_AT: int(time.time()), SEEN_AT: int(time.time()), EPOCH: 0})
        store.create()
        return SimpleNamespace(
            user=world.ana, session=store, META={"REMOTE_ADDR": "127.0.0.1", "HTTP_USER_AGENT": "t"}
        )

    requests = [request(), request()]

    def present(one: Any) -> None:
        try:
            mfa_step_up.verify(one, code=code)
            results.append("verified")
        except mfa.WrongCode:
            results.append("wrong")
        finally:
            connection.close()

    threads = [Thread(target=present, args=(one,)) for one in requests]
    waiting = "SELECT count(*) FROM pg_locks WHERE NOT granted"
    with migrator.transaction():
        migrator.execute("SELECT 1 FROM users FOR UPDATE")
        for thread in threads:
            thread.start()
        deadline = time.monotonic() + 10
        while migrator.execute(waiting).fetchone() != (len(threads),):
            assert time.monotonic() < deadline  # no esperan: no hay bloqueo
            time.sleep(0.02)
    for thread in threads:
        thread.join(10)
    assert sorted(results) == ["verified", "wrong"]
    marked = [one.session.get(mfa.MFA_AT) for one in requests]
    assert sorted(mark is None for mark in marked) == [False, True]  # solo una quedó verificada


def test_the_cap_per_account_is_shared_and_refuses_the_right_code_too(
    world: Any,
    clock: SimpleNamespace,
    stack: list[Any],
    migrator: psycopg.Connection[Any],
) -> None:
    browser, shown = ready(world)
    aged(browser, 16 * MINUTES)
    for index in range(9):
        assert step(browser, f"{index:06}").status_code == 400
    assert MfaCodeAttempt.objects.count() == 9 and stack == []
    assert step(browser, "999999").status_code == 400  # el décimo alcanza el tope
    assert stack == [[str(world.ana.pk)]]  # y avisa a la cuenta
    assert "auth.mfa.throttled" in [row[0] for row in audit(migrator)]
    capped = step(browser, code_at(clock))
    assert reply(capped) == (429, b'{"code":"RATE_LIMITED"}') and int(capped["Retry-After"]) > 86000
    assert step(browser, shown[0]).status_code == 429  # ni uno de respaldo
    assert required(browser.get(ORG + "sensitive/"))
    # Es el mismo tope del segundo paso del acceso: ahí tampoco entra.
    again = Client(enforce_csrf_checks=True)
    again.get(test_auth_api.CSRF)
    assert login(again).status_code == 202
    assert send(again, "POST", STEP_TWO, {"code": code_at(clock)}).status_code == 429


@pytest.mark.parametrize("change", ["epoch", "logout", "inactive"])
def test_a_revocation_during_the_request_verifies_nothing_and_spends_nothing(
    world: Any,
    clock: SimpleNamespace,
    migrator: psycopg.Connection[Any],
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    browser, _ = ready(world)
    aged(browser, 16 * MINUTES)
    admit = code_limit.admit

    def meanwhile(user_id: Any) -> Any:  # con la petición ya pasada por el middleware
        migrator.execute(
            {
                "epoch": "UPDATE users SET session_epoch = session_epoch + 1",
                "logout": "DELETE FROM django_session",
                "inactive": "UPDATE users SET is_active = false",
            }[change]
        )
        return admit(user_id)

    monkeypatch.setattr(code_limit, "admit", meanwhile)
    migrator.execute("DELETE FROM platform_audit_logs")
    assert reply(step(browser, code_at(clock))) == (401, b'{"code":"NOT_AUTHENTICATED"}')
    assert MfaDevice.objects.get().last_step == 1  # el código no se gastó
    assert not MfaCodeAttempt.objects.exists()  # el intento se devolvió: no se llegó a mirar
    assert not Session.objects.exists()  # la sesión se cerró ya, sin esperar a la siguiente
    assert browser.get(SESSION).status_code == 401
    assert audit(migrator) == []


def test_a_factor_that_went_meanwhile_is_a_409_with_the_attempt_counted(
    world: Any,
    clock: SimpleNamespace,
    migrator: psycopg.Connection[Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    browser, _ = ready(world)
    admit = code_limit.admit

    def meanwhile(user_id: Any) -> Any:  # pasada ya la comprobación sin bloqueo
        migrator.execute("DELETE FROM user_mfa_devices")
        return admit(user_id)

    monkeypatch.setattr(code_limit, "admit", meanwhile)
    assert reply(step(browser, code_at(clock))) == (409, b'{"code":"MFA_NOT_ENABLED"}')
    assert MfaCodeAttempt.objects.count() == 1 and browser.get(SESSION).status_code == 200


def test_without_its_audit_row_nothing_is_verified_and_the_code_is_not_spent(
    world: Any, clock: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    browser, _ = ready(world)
    aged(browser, 16 * MINUTES)
    before, record = browser.cookies["crm_session"].value, platform.record

    def unwritable(action: str, **how: Any) -> Any:
        if action == "auth.mfa.step_up" and "result" not in how:
            raise ConnectionError("sin auditoría")
        return record(action, **how)

    monkeypatch.setattr(platform, "record", unwritable)
    browser.raise_request_exception = False
    assert step(browser, code_at(clock)).status_code == 500
    assert MfaDevice.objects.get().last_step == 1 and browser.cookies["crm_session"].value == before
    assert required(browser.get(ORG + "sensitive/"))
    monkeypatch.setattr(platform, "record", record)
    assert step(browser, code_at(clock)).status_code == 200  # el mismo código sigue valiendo


def test_a_rejection_that_cannot_be_audited_does_not_change_the_answer(
    world: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    browser, _ = ready(world)
    record = platform.record

    def unwritable(action: str, **how: Any) -> Any:
        if how.get("result") is not None:
            raise ConnectionError("sin auditoría")
        return record(action, **how)

    monkeypatch.setattr(platform, "record", unwritable)
    assert step(browser, "000000").status_code == 400

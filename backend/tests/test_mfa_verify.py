"""F2-104: `apps.accounts.second_factor.verify`, el procedimiento con el que una ruta comprueba
un código de un dispositivo confirmado (ADR-023 §5 y §10). PostgreSQL real, el rol `crm_app` y
la auditoría de plataforma real."""

import logging
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from django.core import mail as outbox
from django.db import DatabaseError, connection, transaction
from django.test.utils import CaptureQueriesContext

from apps.accounts import code_limit, mfa, second_factor
from apps.accounts.models import MfaBackupCode, MfaCodeAttempt, MfaDevice, User
from apps.accounts.tasks import send_mfa_code_limit
from apps.audit import platform
from core.observability.logging import json_formatter
from tests import test_mfa_second_factor
from tests.factories import TEST_PASSWORD, make_user
from tests.test_auth_api import audit
from tests.test_mfa_second_factor import code_at, enrolled

clock = test_mfa_second_factor.clock
pytestmark = pytest.mark.usefixtures("tenant_db")
EMAIL = "ana@example.com"
CLIENT = {"ip": "203.0.113.9", "user_agent": "pytest"}
LIMIT = 10


@pytest.fixture(autouse=True)
def queued(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """El aviso que se encola, sin broker, y si entonces había una transacción abierta."""
    calls: list[dict[str, Any]] = []

    def enqueue(**how: Any) -> None:
        how.pop("connection")  # la propia del broker
        calls.append({**how, "in_transaction": connection.in_atomic_block})

    monkeypatch.setattr(send_mfa_code_limit, "apply_async", enqueue)
    return calls


def check(user: User, presented: str, **route: Any) -> Any:
    how = {"actor": None, "client": CLIENT, "ready": lambda account: None, **route}
    how.setdefault("accepted", lambda account, method: method)
    return second_factor.verify(user.pk, presented, **how)


def counted(user: User) -> int:
    return MfaCodeAttempt.objects.filter(user=user).count()


def test_a_valid_code_runs_what_the_route_does_inside_the_transaction_and_is_not_counted(
    clock: SimpleNamespace, migrator: psycopg.Connection[Any], queued: list[dict[str, Any]]
) -> None:
    ana = make_user(email=EMAIL)
    shown = enrolled(ana)
    migrator.execute("UPDATE users SET session_epoch = 7")  # cambió desde que la ruta la leyó
    seen: list[Any] = []

    def ready(account: User) -> None:
        seen.append(("ready", account.session_epoch, connection.in_atomic_block, counted(ana)))

    def accepted(account: User, method: str) -> str:
        spent = (MfaDevice.objects.get().last_step or 0) > 1  # el código ya está gastado aquí
        seen.append(("accepted", account.pk, method, connection.in_atomic_block, spent))
        return "lo de la ruta"

    with CaptureQueriesContext(connection) as queries:
        assert check(ana, code_at(clock), ready=ready, accepted=accepted) == "lo de la ruta"
    assert seen == [("ready", 7, True, 1), ("accepted", ana.pk, "totp", True, True)]
    assert counted(ana) == 0  # devuelto con la transacción
    locks = [q["sql"] for q in queries if q["sql"].rstrip().endswith("UPDATE")]
    assert len(locks) == 3 and all(
        'FROM "users"' in sql or "FROM users" in sql for sql in locks[:2]
    )
    assert locks[1].rstrip().endswith("FOR NO KEY UPDATE")  # la cuenta, y después el dispositivo
    assert "user_mfa_devices" in locks[2] and locks[2].rstrip().endswith("FOR UPDATE")
    assert check(ana, shown[0]) == "backup_code" and counted(ana) == 0
    assert audit(migrator) == [] and queued == []  # nada que auditar ni que avisar aquí


def test_a_code_that_is_not_valid_runs_nothing_of_the_route_and_stays_counted(
    clock: SimpleNamespace, migrator: psycopg.Connection[Any], queued: list[dict[str, Any]]
) -> None:
    ana, luis = make_user(email=EMAIL), make_user(email="luis@example.com")
    enrolled(ana)
    ran: list[str] = []
    for wrong in (code_at(clock, -2), "12345a", "U" * 16):
        with pytest.raises(mfa.WrongCode):
            check(ana, wrong, accepted=lambda account, method: ran.append(method))
    assert ran == [] and counted(ana) == 3 and counted(luis) == 0
    assert audit(migrator) == [] and queued == []  # la fila de cada fallo es de la ruta
    with pytest.raises(second_factor.NoDevice):  # sin dispositivo: tal cual, y contado
        check(luis, "123456")
    assert counted(luis) == 1
    with transaction.atomic(), pytest.raises(RuntimeError, match="transacción"):
        check(ana, code_at(clock))  # el intento se desharía con ella
    with pytest.raises(User.DoesNotExist):
        second_factor.verify(
            uuid4(), "123456", actor=None, client=CLIENT, ready=print, accepted=print
        )
    assert counted(ana) == 3


def test_if_the_route_fails_after_the_code_nothing_is_spent_and_the_attempt_stays_counted(
    clock: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    ana = make_user(email=EMAIL)
    shown = enrolled(ana)

    def broken(account: User, method: str) -> None:
        raise DatabaseError("la fila de auditoría de la ruta no se pudo escribir")

    for presented in (code_at(clock), shown[0]):
        with pytest.raises(DatabaseError):
            check(ana, presented, accepted=broken)
    assert counted(ana) == 2  # como en el acceso: sin lo de la ruta, el intento no se devuelve
    assert MfaDevice.objects.get().last_step == 1  # y el código no quedó gastado
    assert not MfaBackupCode.objects.exclude(used_at=None).exists()
    with monkeypatch.context() as patch, pytest.raises(ZeroDivisionError):
        patch.setattr(code_limit, "give_back", lambda attempt: 1 / 0)
        check(ana, code_at(clock))  # devolverlo va en esa transacción: si falla, tampoco lo demás
    assert counted(ana) == 3 and MfaDevice.objects.get().last_step == 1
    assert check(ana, code_at(clock)) == "totp" and check(ana, shown[0]) == "backup_code"


def test_if_the_route_refuses_before_the_code_the_attempt_is_given_back(
    clock: SimpleNamespace,
    migrator: psycopg.Connection[Any],
    queued: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    ana = make_user(email=EMAIL)
    enrolled(ana)

    def refuse(account: User) -> None:
        raise PermissionError("la marca de acceso a medias ya no vale")

    with CaptureQueriesContext(connection) as queries, pytest.raises(PermissionError):
        check(ana, code_at(clock, -2), ready=refuse)
    assert counted(ana) == 0  # no se probó ningún código
    assert not any("user_mfa_devices" in query["sql"] for query in queries)  # ni se miró
    for _ in range(LIMIT - 1):
        code_limit.admit(ana.pk)
    with pytest.raises(PermissionError):
        check(ana, code_at(clock), ready=refuse)  # el que haría diez: devuelto, no alcanza el tope
    assert counted(ana) == LIMIT - 1 and audit(migrator) == [] and queued == []
    monkeypatch.setattr(code_limit, "give_back", lambda attempt: 1 / 0)
    with caplog.at_level(logging.ERROR), pytest.raises(PermissionError):
        check(ana, code_at(clock), ready=refuse)  # devolverlo falla: el error sigue siendo el suyo
    assert counted(ana) == LIMIT and "sin devolver" in caplog.text
    # Quedó contado y es el que alcanza el tope: no se alcanza en silencio.
    assert [row[0] for row in audit(migrator)] == ["auth.mfa.throttled"] and len(queued) == 1


@pytest.mark.parametrize("actor", [None, "account"])
def test_the_attempt_that_reaches_the_cap_is_audited_and_the_account_is_told(
    clock: SimpleNamespace,
    migrator: psycopg.Connection[Any],
    queued: list[dict[str, Any]],
    caplog: pytest.LogCaptureFixture,
    actor: str | None,
) -> None:
    ana = make_user(email=EMAIL)
    shown = enrolled(ana)
    who = ana.pk if actor else None
    caplog.set_level(logging.DEBUG)
    for _ in range(LIMIT - 1):
        with pytest.raises(mfa.WrongCode):
            check(ana, code_at(clock, -2), actor=who)
    assert audit(migrator) == [] and queued == []
    assert check(ana, code_at(clock), actor=who) == "totp"  # el décimo vale: ni fila ni aviso
    assert audit(migrator) == [] and queued == [] and counted(ana) == LIMIT - 1
    with pytest.raises(mfa.WrongCode):
        check(ana, "000000", actor=who)  # el décimo fallo: alcanza el tope
    assert audit(migrator) == [
        (
            "auth.mfa.throttled",
            "DENIED",
            "USER" if actor else "ANONYMOUS",  # en el segundo paso del acceso no hay sesión
            who,
            None,
            "user",
            ana.pk,
            '{"seconds": 86400, "attempts": 10}',
            "203.0.113.9",
            "pytest",
        )
    ]
    assert [(call["args"], call["in_transaction"]) for call in queued] == [([str(ana.pk)], False)]
    assert (queued[0]["expires"], queued[0]["retry"]) == (24 * 60 * 60, False)
    clock.at += test_mfa_second_factor.STEP * 3
    ran: list[str] = []
    for presented in (code_at(clock), shown[0]):  # ni con el correcto: no se mira ninguno
        with pytest.raises(second_factor.Capped) as capped:
            check(ana, presented, actor=who, accepted=lambda account, method: ran.append(method))
        assert 86390 <= capped.value.seconds <= 86400
    assert ran == [] and counted(ana) == LIMIT
    assert not MfaBackupCode.objects.exclude(used_at=None).exists()
    assert len(audit(migrator)) == 1 and len(queued) == 1  # un rechazo no vuelve a avisar
    logged = "\n".join(json_formatter().format(record) for record in caplog.records)
    assert "000000" not in logged and code_at(clock) not in logged and EMAIL not in logged


def test_the_row_and_the_notice_are_best_effort(
    clock: SimpleNamespace,
    migrator: psycopg.Connection[Any],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    ana = make_user(email=EMAIL)
    enrolled(ana)
    for _ in range(LIMIT - 1):
        code_limit.admit(ana.pk)

    def down(*args: Any, **how: Any) -> None:
        raise ConnectionError("caído")

    monkeypatch.setattr(platform, "record", down)
    monkeypatch.setattr(send_mfa_code_limit, "apply_async", down)
    with caplog.at_level(logging.ERROR), pytest.raises(mfa.WrongCode):
        check(ana, "000000")  # sin fila y sin aviso, la respuesta es la misma
    assert "auth.mfa.throttled sin auditar" in caplog.text and "sin encolar" in caplog.text
    assert audit(migrator) == [] and counted(ana) == LIMIT
    with pytest.raises(second_factor.Capped):
        check(ana, code_at(clock))


def test_the_task_mails_the_account_what_happened_without_any_code(settings: Any) -> None:
    ana = make_user(email=EMAIL)
    gone = make_user(email="baja@example.com", is_active=False)
    local = make_user(email='"ab"@example.com')  # `core.mail` no escribe a esa dirección
    assert send_mfa_code_limit.apply(args=[str(ana.pk)]).get() is None
    (message,) = outbox.outbox
    assert message.to == [EMAIL] and message.subject == second_factor.LIMIT_SUBJECT
    body = str(message.body)
    assert "Se fallaron demasiados códigos" in body and "como mucho 24 horas" in body
    assert "también los correctos" in body and "vuelve a aceptarlos sola" in body
    assert "Cambia\nla contraseña con un enlace de recuperación" in body  # si no fue ella
    assert "no adelanta el final de la espera" in body and "te llegue o no ese enlace" in body
    assert "http" not in body and TEST_PASSWORD not in body and "{" not in body
    for unknown in (str(uuid4()), str(gone.pk), str(local.pk), "no-es-un-identificador", ""):
        assert second_factor.notify_code_limit(unknown) is False
    settings.MFA_CODE_THROTTLE = (5, 48 * 60 * 60)  # la ventana que diga el ajuste
    assert second_factor.notify_code_limit(str(ana.pk)) is True
    assert "como mucho 48 horas" in str(outbox.outbox[1].body) and len(outbox.outbox) == 2
    settings.MFA_CODE_THROTTLE = (5, 24 * 60 * 60 + 1)  # no son horas justas: nunca de menos
    assert second_factor.notify_code_limit(str(ana.pk)) is True
    assert "como mucho 25 horas" in str(outbox.outbox[2].body)
    assert (send_mfa_code_limit.name, send_mfa_code_limit.max_retries) == (
        "accounts.send_mfa_code_limit",
        0,
    )

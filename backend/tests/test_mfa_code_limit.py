"""F2-102: `apps.accounts.code_limit`, el tope por cuenta de códigos fallidos del segundo factor
(ADR-023 §5). PostgreSQL real y el rol `crm_app`; el reloj, el de la base de datos."""

import logging
import time
from threading import Thread
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from django.apps import apps
from django.core.exceptions import ImproperlyConfigured
from django.db import connection, transaction
from django.db.models import ProtectedError
from django.db.utils import OperationalError

from apps.accounts import checks, code_limit
from apps.accounts.models import MfaCodeAttempt, User
from apps.accounts.tasks import purge_login_throttles
from tests.factories import make_user

pytestmark = pytest.mark.usefixtures("tenant_db")
LIMIT, WINDOW = 10, 24 * 60 * 60
OLDER = "UPDATE user_mfa_code_attempts SET created_at = created_at - %s::interval WHERE id = %s"
ROW_LOCK = "SELECT 1 FROM users WHERE id = %s FOR NO KEY UPDATE"


def counted(user: User) -> int:
    return MfaCodeAttempt.objects.filter(user=user).count()


def test_ten_attempts_are_admitted_and_the_next_waits_for_the_oldest_to_leave_the_window(
    migrator: psycopg.Connection[Any], settings: Any, caplog: pytest.LogCaptureFixture
) -> None:
    assert settings.MFA_CODE_THROTTLE == (LIMIT, WINDOW)  # ADR-023 §5
    ana, luis = make_user(email="ana@example.com"), make_user(email="luis@example.com")
    caplog.set_level(logging.DEBUG)
    attempts = [code_limit.admit(ana.pk) for _ in range(LIMIT)]
    assert [attempt.last for attempt in attempts] == [False] * (LIMIT - 1) + [True]
    assert len({attempt.id for attempt in attempts}) == LIMIT == counted(ana)
    migrator.execute(OLDER, ["23 hours 59 minutes", attempts[0].id])  # el más antiguo: 1 min
    migrator.execute(OLDER, ["2 hours", attempts[1].id])
    with pytest.raises(code_limit.Refused) as refused:
        code_limit.admit(ana.pk)  # el undécimo: ni se evalúa ni cuenta
    assert 55 <= refused.value.seconds <= 60 and counted(ana) == LIMIT
    assert code_limit.admit(luis.pk).last is False and counted(luis) == 1  # cada cuenta, el suyo
    migrator.execute(OLDER, ["1 minute", attempts[0].id])  # salió de la ventana: deja de contar
    again = code_limit.admit(ana.pk)  # el tope se levanta solo, intento a intento
    assert again.last is True and counted(ana) == LIMIT + 1
    with pytest.raises(code_limit.Refused) as refused:
        code_limit.admit(ana.pk)  # ahora espera al siguiente más antiguo
    assert 22 * 3600 - 5 <= refused.value.seconds <= 22 * 3600
    assert caplog.text == ""  # no registra nada


def test_an_attempt_given_back_does_not_count_and_only_its_own_row_goes() -> None:
    ana, luis = make_user(email="ana@example.com"), make_user(email="luis@example.com")
    theirs = code_limit.admit(luis.pk)
    for _ in range(LIMIT - 1):
        code_limit.admit(ana.pk)
    for _ in range(3):  # el décimo vale una y otra vez: no gasta el tope
        tenth = code_limit.admit(ana.pk)
        assert tenth.last is True
        with transaction.atomic():
            code_limit.give_back(tenth)
        assert counted(ana) == LIMIT - 1
    tenth = code_limit.admit(ana.pk)
    with pytest.raises(RuntimeError, match="deshecha"), transaction.atomic():
        code_limit.give_back(tenth)
        raise RuntimeError("deshecha")  # la transacción que aceptaba el código no termina
    assert counted(ana) == LIMIT  # sigue contado
    assert counted(luis) == 1 and MfaCodeAttempt.objects.filter(pk=theirs.id).exists()
    assert str(MfaCodeAttempt.objects.get(pk=theirs.id)) == str(theirs.id)  # nunca la cuenta


def test_it_counts_outside_any_transaction_and_only_for_an_account_that_exists() -> None:
    ana = make_user(email="ana@example.com")
    with transaction.atomic(), pytest.raises(RuntimeError, match="transacción"):
        code_limit.admit(ana.pk)  # se desharía con ella
    with pytest.raises(User.DoesNotExist):
        code_limit.admit(uuid4())
    assert not MfaCodeAttempt.objects.exists()


def test_two_simultaneous_attempts_with_nine_counted_are_not_both_admitted(
    migrator: psycopg.Connection[Any],
) -> None:
    ana = make_user(email="ana@example.com")
    for _ in range(LIMIT - 1):
        code_limit.admit(ana.pk)
    results: list[Any] = []

    def attempt() -> None:
        try:
            results.append(code_limit.admit(ana.pk))
        except code_limit.Refused as refused:
            results.append(refused)
        finally:
            connection.close()  # la conexión de este hilo

    threads = [Thread(target=attempt) for _ in range(2)]
    waiting = "SELECT count(*) FROM pg_locks WHERE NOT granted"
    with migrator.transaction():  # los dos llegan con la fila de la cuenta tomada por otro
        migrator.execute(ROW_LOCK, [ana.pk])
        for thread in threads:
            thread.start()
        deadline = time.monotonic() + 10
        while migrator.execute(waiting).fetchone() != (2,) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert migrator.execute(waiting).fetchone() == (2,)  # esperan los dos: uno a la vez
        assert results == [] and counted(ana) == LIMIT - 1
        released = migrator.execute("SELECT clock_timestamp()").fetchall()[0][0]
    for thread in threads:
        thread.join(10)
    assert sorted(type(result).__name__ for result in results) == ["Attempt", "Refused"]
    assert counted(ana) == LIMIT
    # La hora del intento es la de cuando se contó, tras la espera: no la del inicio de su
    # transacción (`now()`), que es anterior y lo sacaría antes de la ventana.
    assert MfaCodeAttempt.objects.filter(user=ana).latest("created_at").created_at >= released
    # Con el tope alcanzado también toma la fila antes de contar. Nada en el módulo pone plazo a
    # esa espera: aquí lo pone el `lock_timeout` de esta sesión.
    with connection.cursor() as cursor, migrator.transaction():
        migrator.execute(ROW_LOCK, [ana.pk])
        cursor.execute("SET lock_timeout = '200ms'")
        with pytest.raises(OperationalError):
            code_limit.admit(ana.pk)
        cursor.execute("SET lock_timeout = 0")


def test_the_purge_deletes_only_what_left_the_window(migrator: psycopg.Connection[Any]) -> None:
    ana = make_user(email="ana@example.com")
    old, edge, fresh = (code_limit.admit(ana.pk) for _ in range(3))
    migrator.execute(OLDER, ["24 hours 1 second", old.id])
    migrator.execute(OLDER, ["23 hours 59 minutes", edge.id])
    assert code_limit.purge() == 1 and code_limit.purge() == 0
    assert set(MfaCodeAttempt.objects.values_list("pk", flat=True)) == {edge.id, fresh.id}
    migrator.execute(OLDER, ["2 minutes", edge.id])
    assert purge_login_throttles() == 1  # en la tarea horaria que purga los contadores del acceso
    assert code_limit.admit(ana.pk).last is False and counted(ana) == 2  # sigue contando igual
    for left in MfaCodeAttempt.objects.values_list("pk", flat=True):
        migrator.execute(OLDER, ["25 hours", left])
    with pytest.raises(ProtectedError):  # ya no cuentan, pero hasta la purga la cuenta no se borra
        ana.delete()
    assert code_limit.purge() == 2 and counted(ana) == 0


@pytest.mark.parametrize(
    "value",
    [None, [10, 86400], (10,), (10, 86400, 1), ("10", 86400), (10.0, 86400), (True, 86400)]
    + [(0, 86400), (11, 86400), (10, 86399), (10, 32 * 86400)],  # solo puede endurecer ADR-023 §5
)
def test_a_setting_that_would_loosen_the_cap_does_not_start(settings: Any, value: Any) -> None:
    accounts = apps.get_app_config("accounts")
    assert checks.mfa_code_throttle() is None
    accounts.ready()
    for stricter in ((1, 86400), (5, 7 * 86400)):
        settings.MFA_CODE_THROTTLE = stricter
        assert checks.mfa_code_throttle() is None
    settings.MFA_CODE_THROTTLE = value
    assert checks.mfa_code_throttle()
    with pytest.raises(ImproperlyConfigured, match="MFA_CODE_THROTTLE"):
        accounts.ready()  # web, worker, beat y `manage.py`: ninguno arranca

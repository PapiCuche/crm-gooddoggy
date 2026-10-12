"""F2-103: `apps.accounts.second_factor.accept`, dar por bueno un código de un dispositivo
confirmado (ADR-023 §2, §3, §5 y §6). PostgreSQL real y el rol `crm_app`."""

import logging
import time
from datetime import timedelta
from threading import Thread
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest
from django.db import IntegrityError, connection, transaction
from django.db.utils import OperationalError
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.accounts import backup_codes, mfa, second_factor, totp
from apps.accounts.models import MfaBackupCode, MfaDevice, User
from core import sealed
from core.observability import reporting
from core.observability.logging import json_formatter
from tests.factories import make_user

pytestmark = pytest.mark.usefixtures("tenant_db")
SECRET = bytes(range(20))
STEP = timedelta(seconds=totp.PERIOD)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """La hora de `accept`, parada: los pasos de un test no dependen de cuándo corre."""
    held = SimpleNamespace(at=timezone.now())
    monkeypatch.setattr(timezone, "now", lambda: held.at)
    return held


def enrolled(user: User, secret: bytes = SECRET, *, confirmed: bool = True) -> list[str]:
    """Un dispositivo de `user` y sus códigos de respaldo, como los deja confirmar el alta."""
    box = sealed.seal(mfa.PURPOSE, mfa.owner(user.pk), secret)
    device = MfaDevice.objects.create(user=user, type=MfaDevice.Type.TOTP, secret=box)
    if confirmed:  # después de crearlo: con el reloj real, antes sería anterior a `created_at`
        MfaDevice.objects.filter(pk=device.pk).update(confirmed_at=timezone.now(), last_step=1)
    shown = backup_codes.new_codes()
    MfaBackupCode.objects.bulk_create(
        MfaBackupCode(user=user, code_hash=backup_codes.code_hash(code) or "") for code in shown
    )
    return shown


def code_at(clock: SimpleNamespace, shift: int = 0, secret: bytes = SECRET) -> str:
    return totp.code(secret, totp.step_at(clock.at.timestamp()) + shift)


def accept(user: User, presented: str) -> str | None:
    with transaction.atomic():
        return second_factor.accept(user, presented)


def test_a_totp_code_is_accepted_once_and_never_one_of_an_earlier_step(
    clock: SimpleNamespace, caplog: pytest.LogCaptureFixture
) -> None:
    ana, luis = make_user(email="ana@example.com"), make_user(email="luis@example.com")
    enrolled(ana)
    enrolled(luis, bytes(reversed(SECRET)))
    caplog.set_level(logging.DEBUG)
    now = totp.step_at(clock.at.timestamp())
    for wrong in (code_at(clock, -2), code_at(clock, 2), code_at(clock, 0, bytes(20))):
        assert accept(ana, wrong) is None  # fuera de la ventana, o de otro secreto
    assert accept(ana, code_at(clock, 0, bytes(reversed(SECRET)))) is None  # el de otra cuenta
    assert MfaDevice.objects.get(user=ana).last_step == 1  # nada aceptado todavía
    assert accept(ana, code_at(clock, -1)) == "totp" == second_factor.TOTP  # el paso anterior
    assert MfaDevice.objects.get(user=ana).last_step == now - 1
    assert accept(ana, code_at(clock, -1)) is None  # el mismo código no vale otra vez
    assert accept(ana, code_at(clock, 1)) == "totp"  # el siguiente, con el reloj adelantado
    assert MfaDevice.objects.get(user=ana).last_step == now + 1
    for spent in (code_at(clock, 0), code_at(clock, 1)):  # ni uno anterior al último, ni él
        assert accept(ana, spent) is None
    clock.at += STEP * 2  # dos pasos después vuelve a valer el suyo
    assert accept(ana, code_at(clock)) == "totp"
    assert MfaDevice.objects.get(user=ana).last_step == now + 2
    assert MfaDevice.objects.get(user=luis).last_step == 1  # el de otra cuenta, sin tocar
    assert not MfaBackupCode.objects.exclude(used_at=None).exists()
    logged = "\n".join(json_formatter().format(record) for record in caplog.records)
    assert code_at(clock) not in logged and SECRET.hex() not in logged


def test_a_backup_code_is_spent_once_however_it_is_typed_and_only_for_its_account(
    clock: SimpleNamespace, caplog: pytest.LogCaptureFixture
) -> None:
    ana, luis = make_user(email="ana@example.com"), make_user(email="luis@example.com")
    hers, his = enrolled(ana), enrolled(luis, bytes(reversed(SECRET)))
    caplog.set_level(logging.DEBUG)
    assert accept(ana, his[0]) is None  # el de otra cuenta no vale, ni se gasta
    assert accept(ana, hers[0]) == "backup_code" == second_factor.BACKUP_CODE
    spent = MfaBackupCode.objects.get(user=ana, used_at__isnull=False)
    assert (spent.code_hash, spent.used_at) == (backup_codes.code_hash(hers[0]), clock.at)
    assert accept(ana, hers[0]) is None  # gastado: no vuelve a valer
    assert accept(ana, hers[1].lower().replace("-", " ")) == "backup_code"  # como se teclee
    assert accept(ana, hers[2].replace("-", "")) == "backup_code"
    for wrong in ("", "12345", "1234567", "１２３４５６", hers[3][:-1], hers[3] + "0", "U" * 16):
        assert accept(ana, wrong) is None  # ni seis dígitos ni un código de respaldo
    assert MfaBackupCode.objects.filter(user=ana, used_at__isnull=False).count() == 3
    assert not MfaBackupCode.objects.filter(user=luis, used_at__isnull=False).exists()
    assert MfaDevice.objects.get(user=ana).last_step == 1  # un código de respaldo no mueve el paso
    logged = "\n".join(json_formatter().format(record) for record in caplog.records)
    assert not any(code in logged or code.replace("-", "") in logged for code in hers)


def test_without_a_confirmed_device_there_is_nothing_to_check(clock: SimpleNamespace) -> None:
    ana, luis = make_user(email="ana@example.com"), make_user(email="luis@example.com")
    shown = enrolled(ana, confirmed=False)  # un alta a medias no protege ni comprueba nada
    for user, presented in ((ana, code_at(clock)), (ana, shown[0]), (luis, "123456")):
        with pytest.raises(second_factor.NoDevice):
            accept(user, presented)
    assert not MfaBackupCode.objects.exclude(used_at=None).exists()
    with pytest.raises(RuntimeError, match="transacción"):
        second_factor.accept(ana, code_at(clock))  # gastar y aceptar van en una transacción


def test_a_secret_that_cannot_be_opened_is_a_code_that_is_not_valid_and_is_reported(
    clock: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, settings: Any
) -> None:
    ana = make_user(email="ana@example.com")
    shown = enrolled(ana)
    reports: list[tuple[str, Any]] = []
    spy = SimpleNamespace(
        capture_exception=lambda error, context=None: reports.append((repr(error), context))
    )
    monkeypatch.setattr(reporting, "reporter", lambda: spy)
    settings.SECRETS_KEKS = {2: bytes(range(32))}  # la clave con la que se cifró ya no está
    assert accept(ana, code_at(clock)) is None  # no se distingue de un código que no vale
    assert reports == [("UnreadableSecrets(1)", {"action": "mfa.accept"})]
    assert accept(ana, "１２３４５６") is None and len(reports) == 1  # no es TOTP: no abre nada
    monkeypatch.setattr(reporting, "reporter", lambda: None)  # un reporte roto: lo mismo
    assert accept(ana, code_at(clock)) is None
    assert MfaDevice.objects.get().last_step == 1
    assert accept(ana, shown[0]) == "backup_code"  # un código de respaldo no necesita el secreto
    assert len(reports) == 1


def test_the_code_is_spent_with_the_device_locked_and_only_if_the_transaction_ends(
    clock: SimpleNamespace, migrator: psycopg.Connection[Any]
) -> None:
    ana = make_user(email="ana@example.com")
    shown = enrolled(ana)
    for presented in (code_at(clock), shown[0]):
        with pytest.raises(RuntimeError, match="deshecha"), transaction.atomic():
            assert second_factor.accept(ana, presented)
            raise RuntimeError("deshecha")  # lo que aceptaba el código no termina
    assert MfaDevice.objects.get().last_step == 1  # nada gastado: los dos siguen valiendo
    assert not MfaBackupCode.objects.exclude(used_at=None).exists()
    with CaptureQueriesContext(connection) as queries:
        assert accept(ana, shown[0]) == "backup_code"
    first = next(query["sql"] for query in queries if "user_mfa" in query["sql"])
    assert "user_mfa_devices" in first and first.rstrip().endswith("FOR UPDATE")  # antes de mirar
    with connection.cursor() as cursor, migrator.transaction():
        migrator.execute("SELECT 1 FROM user_mfa_devices FOR UPDATE")  # otra lo tiene: espera
        cursor.execute("SET lock_timeout = '200ms'")
        for presented in (code_at(clock), shown[1]):
            with pytest.raises(OperationalError):
                accept(ana, presented)
        cursor.execute("SET lock_timeout = 0")
    assert accept(ana, code_at(clock)) == "totp" and accept(ana, shown[1]) == "backup_code"


def at_once(
    migrator: psycopg.Connection[Any],
    clock: SimpleNamespace,
    user: User,
    presented: list[str],
    meanwhile: timedelta = timedelta(0),
) -> list[str]:
    """Lo que `accept` responde a cada uno, presentados a la vez en transacciones distintas.
    Llegan con la fila del dispositivo tomada por otra: esperan todos, la hora avanza
    `meanwhile` y, al soltarla, pasan de uno en uno."""
    results: list[str] = []

    def present(code: str) -> None:
        try:
            results.append(str(accept(user, code)))
        finally:
            connection.close()  # la conexión de este hilo

    threads = [Thread(target=present, args=(code,)) for code in presented]
    waiting = "SELECT count(*) FROM pg_locks WHERE NOT granted"
    with migrator.transaction():
        migrator.execute("SELECT 1 FROM user_mfa_devices FOR UPDATE")
        for thread in threads:
            thread.start()
        deadline = time.monotonic() + 10
        while migrator.execute(waiting).fetchone() != (len(threads),):
            assert time.monotonic() < deadline  # no esperan: no hay bloqueo
            time.sleep(0.02)
        assert results == []
        clock.at += meanwhile
    for thread in threads:
        thread.join(10)
    return sorted(results)


def test_two_transactions_with_the_same_code_do_not_both_accept_it(
    clock: SimpleNamespace, migrator: psycopg.Connection[Any]
) -> None:
    ana = make_user(email="ana@example.com")
    shown = enrolled(ana)
    same = [code_at(clock), code_at(clock), shown[0], shown[0]]
    assert at_once(migrator, clock, ana, same) == ["None", "None", "backup_code", "totp"]
    assert MfaBackupCode.objects.filter(used_at__isnull=False).count() == 1
    # La hora de un código es la de cuando se mira, con la fila ya tomada: el de dos pasos
    # después no valía al llegar, y vale porque la espera lo metió en la ventana.
    assert at_once(migrator, clock, ana, [code_at(clock, 2)], STEP) == ["totp"]


def test_a_code_is_a_totp_code_only_if_it_is_six_ascii_digits_as_presented(
    clock: SimpleNamespace,
) -> None:
    ana = make_user(email="ana@example.com")
    enrolled(ana)
    digits = "1234-5678-9012-3456"  # un código de respaldo puede ser todo dígitos
    MfaBackupCode.objects.create(user=ana, code_hash=backup_codes.code_hash(digits) or "")
    code = code_at(clock)
    spaced = (f" {code}", f"{code} ", f"{code[:3]} {code[3:]}", f"{code[:3]}-{code[3:]}")
    for presented in (*spaced, f"{code}\n", f"{code}\x00", f"+{code[1:]}", digits[:7]):
        assert accept(ana, presented) is None  # ni recorta ni junta: no son seis dígitos
    assert MfaDevice.objects.get().last_step == 1
    assert accept(ana, digits.replace("-", "")) == "backup_code"  # dieciséis: de respaldo
    assert accept(ana, code) == "totp"


def test_a_backup_code_spent_before_it_was_created_is_refused_by_the_database(
    clock: SimpleNamespace,
) -> None:
    ana = make_user(email="ana@example.com")
    shown = enrolled(ana)
    clock.at -= timedelta(milliseconds=1)  # otro proceso, con el reloj atrasado
    with pytest.raises(IntegrityError, match="user_mfa_backup_codes_used_ck"):
        accept(ana, shown[0])  # falla cerrado: un 500, y el código no queda gastado
    clock.at += timedelta(milliseconds=1)
    assert accept(ana, shown[0]) == "backup_code"

"""F2-110: `POST /api/v1/auth/mfa/backup-codes/` regenera los códigos de respaldo (ADR-023 §6).
Middleware, CSRF, límites y auditoría de plataforma reales; se entra por el acceso de verdad."""

import logging
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest
from django.db import transaction
from django.test import Client

from apps.accounts import backup_codes, second_factor
from apps.accounts.models import MfaBackupCode, MfaCodeAttempt, MfaDevice
from apps.audit import platform
from core.observability.logging import json_formatter
from tests import test_auth_api, test_mfa_second_factor
from tests.factories import TEST_PASSWORD, make_user
from tests.test_auth_api import CSRF, EMAIL, SESSION, audit, login, reply, send
from tests.test_login_second_step import STEP_TWO, counters
from tests.test_mfa_disable import inside, refused
from tests.test_mfa_enrollment_confirm import SHOWN
from tests.test_mfa_second_factor import SECRET, STEP, code_at, enrolled

real_stack, browser = test_auth_api.real_stack, test_auth_api.browser
clock = test_mfa_second_factor.clock
pytestmark = pytest.mark.usefixtures("tenant_db")
CODES = "/api/v1/auth/mfa/backup-codes/"


def again(client: Client, code: Any, password: Any = TEST_PASSWORD) -> Any:
    return send(client, "POST", CODES, {"password": password, "code": code})


def kept(user: Any) -> set[str]:
    return set(MfaBackupCode.objects.filter(user=user).values_list("code_hash", flat=True))


def test_with_the_password_and_a_totp_code_ten_new_codes_replace_the_old_ones(
    browser: Client,
    clock: SimpleNamespace,
    migrator: psycopg.Connection[Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    ana, luis = make_user(email=EMAIL), make_user(email="luis@example.com")
    shown, theirs = enrolled(ana), enrolled(luis, bytes(reversed(SECRET)))
    other = Client(enforce_csrf_checks=True)
    other.get(CSRF)
    inside(browser, shown)  # gasta uno: quedan nueve, y uno gastado
    inside(other, shown)
    before, old = browser.cookies["crm_session"].value, kept(ana)
    migrator.execute("DELETE FROM platform_audit_logs")
    caplog.set_level(logging.DEBUG)
    typed = code_at(clock)
    answer = again(browser, f"{typed[:3]} {typed[3:]}")  # `123 456` vale, como en el acceso
    assert answer.status_code == 200 and answer["Cache-Control"] == "no-store"
    fresh = answer.json()["backup_codes"]
    assert list(answer.json()) == ["backup_codes"] and len(set(fresh)) == 10
    assert all(SHOWN.fullmatch(code) for code in fresh)
    new = {backup_codes.code_hash(code) for code in fresh}
    assert kept(ana) == new and not new & old  # solo los hashes de los nuevos, sin gastar
    assert not MfaBackupCode.objects.filter(user=ana, used_at__isnull=False).exists()
    assert len(kept(luis)) == len(theirs)  # los de otra cuenta, sin tocar
    (row,) = audit(migrator)
    assert row[:4] == ("auth.mfa.backup_codes_regenerated", "SUCCESS", "USER", ana.pk)
    assert row[5:8] == ("user", ana.pk, "{}")
    assert browser.cookies["crm_session"].value == before  # esta sesión no cambia
    assert other.get(SESSION).status_code == 200  # ni se cierran las demás
    assert browser.get("/api/v1/auth/mfa/").json()["backup_codes_left"] == 10
    assert "par" not in counters(migrator)  # el acierto devuelve lo que contó
    with transaction.atomic():  # ninguno de los anteriores vale ya
        assert [second_factor.accept(ana, code) for code in shown] == [None] * len(shown)
    third = Client(enforce_csrf_checks=True)
    third.get(CSRF)
    assert login(third).status_code == 202  # y uno de los nuevos sí
    assert send(third, "POST", STEP_TWO, {"code": fresh[0].lower()}).status_code == 200
    logged = "\n".join(json_formatter().format(record) for record in caplog.records)
    stored = str(audit(migrator))
    for text in (typed, TEST_PASSWORD, *fresh, *new):
        assert text not in logged and text not in stored


def test_a_backup_code_does_not_buy_ten_more_and_is_not_spent(
    browser: Client, clock: SimpleNamespace, migrator: psycopg.Connection[Any]
) -> None:
    ana = make_user(email=EMAIL)
    shown = enrolled(ana)
    inside(browser, shown)
    old = kept(ana)
    migrator.execute("DELETE FROM platform_audit_logs")
    assert refused(again(browser, shown[0])) == (400, {"code": "invalid"})
    assert kept(ana) == old  # los mismos códigos
    assert MfaBackupCode.objects.filter(user=ana, used_at__isnull=True).count() == 9  # sin gastar
    assert MfaCodeAttempt.objects.count() == 1  # y cuenta como un código que no vale
    assert counters(migrator)["par"] == 1  # también por dirección: lo devuelto se deshizo
    (row,) = audit(migrator)
    assert row[:2] == ("auth.mfa.backup_codes_regenerated", "DENIED")
    assert row[7] == '{"reason": "code"}'
    assert refused(again(browser, code_at(clock, -2))) == (400, {"code": "invalid"})
    assert refused(again(browser, code_at(clock), test_auth_api.WRONG)) == (
        400,
        {"password": "invalid"},
    )
    assert MfaCodeAttempt.objects.count() == 2 and MfaDevice.objects.get().last_step == 1
    assert [row[7] for row in audit(migrator)][1:] == [
        '{"reason": "code"}',
        '{"reason": "password"}',
    ]
    assert again(browser, code_at(clock)).status_code == 200  # con el del dispositivo, sí


def test_it_needs_a_session_csrf_and_a_confirmed_device(
    browser: Client, clock: SimpleNamespace
) -> None:
    ana = make_user(email=EMAIL)
    assert reply(again(browser, "123456")) == (401, b'{"code":"NOT_AUTHENTICATED"}')
    assert login(browser).status_code == 200  # todavía sin segundo factor
    assert reply(again(browser, "123456")) == (409, b'{"code":"MFA_NOT_ENABLED"}')
    bare = Client(enforce_csrf_checks=True)
    bare.cookies = browser.cookies
    body = {"password": TEST_PASSWORD, "code": "123456"}
    assert bare.post(CODES, body, "application/json").status_code == 403
    assert [send(browser, verb, CODES, {}).status_code for verb in ("GET", "PUT", "DELETE")] == [
        405
    ] * 3
    enrolled(ana)
    assert refused(send(browser, "POST", CODES, {})) == (
        400,
        {"password": "required", "code": "required"},
    )
    assert again(browser, code_at(clock)).status_code == 200


def test_without_its_audit_row_the_old_codes_stay_and_the_step_is_not_spent(
    browser: Client,
    clock: SimpleNamespace,
    migrator: psycopg.Connection[Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ana = make_user(email=EMAIL)
    shown = enrolled(ana)
    inside(browser, shown)
    old, record = kept(ana), platform.record

    def unwritable(action: str, **how: Any) -> Any:
        if action == "auth.mfa.backup_codes_regenerated" and "result" not in how:
            raise ConnectionError("sin auditoría")
        return record(action, **how)

    monkeypatch.setattr(platform, "record", unwritable)
    browser.raise_request_exception = False
    assert again(browser, code_at(clock)).status_code == 500
    assert kept(ana) == old and MfaDevice.objects.get().last_step == 1
    assert MfaCodeAttempt.objects.count() == 1  # como en el acceso: sin fila, el intento cuenta
    monkeypatch.setattr(platform, "record", record)
    assert again(browser, code_at(clock)).status_code == 200  # el mismo código sigue valiendo
    clock.at += STEP * 2
    assert again(browser, code_at(clock)).status_code == 200  # y se pueden pedir otra vez
    assert not kept(ana) & old and len(kept(ana)) == 10

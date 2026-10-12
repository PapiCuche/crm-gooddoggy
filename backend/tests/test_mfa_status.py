"""F2-108: `GET /api/v1/auth/mfa/`, el estado del segundo factor de la cuenta de la sesión
(ADR-023 §1, §4 y §6). Middleware, CSRF y sesión reales."""

import base64
import json
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

from apps.accounts import mfa
from apps.accounts.models import ENROLLMENT_FAILURES, MfaBackupCode, MfaDevice
from tests import test_auth_api, test_mfa_second_factor
from tests.factories import make_user
from tests.test_auth_api import EMAIL, NO_SESSION, login, reply, send
from tests.test_mfa_enrollment_confirm import CONFIRM, OLDER, now_code
from tests.test_mfa_enrollment_start import start
from tests.test_mfa_second_factor import enrolled

real_stack, browser = test_auth_api.real_stack, test_auth_api.browser
clock = test_mfa_second_factor.clock
pytestmark = pytest.mark.usefixtures("tenant_db")
STATUS = "/api/v1/auth/mfa/"
NONE = {"enabled": False, "enabled_at": None, "enrollment_expires_in": None, "backup_codes_left": 0}


def state(client: Client) -> dict[str, Any]:
    answer = client.get(STATUS)
    assert answer.status_code == 200 and answer["Cache-Control"] == "no-store"
    return dict(answer.json())


def test_it_follows_the_account_from_nothing_to_an_enrolment_to_a_confirmed_device(
    browser: Client, migrator: psycopg.Connection[Any]
) -> None:
    make_user(email=EMAIL)
    assert reply(browser.get(STATUS)) == (401, b'{"code":"NOT_AUTHENTICATED"}')
    assert login(browser).status_code == 200
    assert [send(browser, verb, STATUS, {}).status_code for verb in ("POST", "PUT", "DELETE")] == [
        405
    ] * 3
    assert state(browser) == NONE
    secret = base64.b32decode(start(browser).json()["secret"])
    pending = state(browser)
    assert 590 <= pending.pop("enrollment_expires_in") <= 600  # los diez minutos del alta
    assert pending == {"enabled": False, "enabled_at": None, "backup_codes_left": 0}
    migrator.execute(OLDER, ["9 minutes 30 seconds"])
    assert 25 <= state(browser)["enrollment_expires_in"] <= 30
    migrator.execute(OLDER, ["10 minutes"])
    assert state(browser) == NONE  # caducada: ya no se puede confirmar
    secret = base64.b32decode(start(browser).json()["secret"])
    for _ in range(ENROLLMENT_FAILURES):
        assert send(browser, "POST", CONFIRM, {"code": now_code(secret, -2)}).status_code == 400
    assert state(browser) == NONE  # agotada con cinco códigos que no valen (ADR-024)
    migrator.execute("DELETE FROM login_throttles")
    secret = base64.b32decode(start(browser).json()["secret"])
    shown = send(browser, "POST", CONFIRM, {"code": now_code(secret)}).json()["backup_codes"]
    with CaptureQueriesContext(connection) as queries:
        enabled = state(browser)
    assert not any(
        q["sql"].lstrip().upper().startswith(("UPDATE", "INSERT", "DELETE")) for q in queries
    )
    confirmed = MfaDevice.objects.get().confirmed_at
    assert confirmed is not None and enabled == {
        "enabled": True,
        "enabled_at": confirmed.isoformat().replace("+00:00", "Z"),
        "enrollment_expires_in": None,
        "backup_codes_left": 10,
    }
    whole = json.dumps(enabled)  # ni el secreto, ni un código, ni su hash
    hashes = MfaBackupCode.objects.values_list("code_hash", flat=True)
    for hidden in (base64.b32encode(secret).decode(), secret.hex(), *shown, *hashes):
        assert hidden not in whole
    migrator.execute(
        "UPDATE user_mfa_backup_codes SET used_at = now() WHERE id IN"
        " (SELECT id FROM user_mfa_backup_codes LIMIT 3)"
    )
    assert state(browser)["backup_codes_left"] == 7  # los gastados no cuentan


def test_it_only_tells_the_state_of_the_account_of_the_session(browser: Client) -> None:
    ana, luis = make_user(email=EMAIL), make_user(email="luis@example.com")
    enrolled(luis)  # Luis tiene segundo factor; Ana, no
    MfaBackupCode.objects.filter(user=luis).first().delete()  # type: ignore[union-attr]
    assert login(browser, "luis@example.com").status_code == 202  # a Luis le falta el segundo paso
    assert reply(browser.get(STATUS)) == NO_SESSION  # la marca de acceso a medias no es una sesión
    assert login(browser).status_code == 200
    assert state(browser) == NONE
    for asked in (f"?user={luis.pk}", f"?user_id={luis.pk}&email=luis@example.com"):
        assert browser.get(STATUS + asked).json() == NONE  # nada del cuerpo ni de la URL lo cambia
    assert browser.get(f"{STATUS}{luis.pk}/").status_code == 404
    assert MfaDevice.objects.filter(user=ana).count() == 0
    enrolled(ana)  # ahora las dos cuentas tienen códigos
    assert state(browser)["backup_codes_left"] == 10  # los suyos: ni los 9 de Luis ni los 19


def test_an_enrolment_counts_down_to_the_instant_it_can_no_longer_be_confirmed(
    clock: SimpleNamespace,
) -> None:
    ana = make_user(email=EMAIL)
    enrolled(ana, confirmed=False)  # deja códigos sin gastar: sin dispositivo confirmado, cero
    started = clock.at
    # Segundos enteros, hacia abajo: nunca más tiempo del que queda, y nunca cero. En el último
    # segundo ya dice que no, aunque `confirm_enrollment` la acepta hasta los 600 justos.
    for after, left in ((0, 600), (0.4, 599), (598.5, 1), (599, 1), (599.5, None), (600, None)):
        clock.at = started + timedelta(seconds=after)
        assert mfa.status(ana) == mfa.Status(False, None, left, 0), after

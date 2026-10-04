"""F2-03B: límite de intentos de acceso (ADR-003 §2, ADR-014 §3, D-F2-9). Stack real: middleware,
contadores en PostgreSQL con el rol `crm_app` y auditoría de plataforma."""

import json
import threading
from datetime import timedelta
from types import SimpleNamespace
from typing import Any
from unittest import mock

import psycopg
import pytest
from django.apps import apps
from django.core.exceptions import ImproperlyConfigured
from django.db import connection, transaction
from django.db.models.functions import Now
from django.test import Client

from apps.accounts import checks, services, throttle
from apps.accounts.models import LoginThrottle, User
from apps.accounts.tasks import purge_login_throttles
from apps.audit import platform
from apps.audit.platform import identifier_hash
from config.settings import base
from core.observability import reporting
from tests import test_auth_api
from tests.factories import TEST_PASSWORD
from tests.test_auth_api import CSRF, EMAIL, INVALID, WRONG, audit, login, reply

pytestmark = pytest.mark.usefixtures("tenant_db")
real_stack, ana = test_auth_api.real_stack, test_auth_api.ana
LIMITED = b'{"code":"RATE_LIMITED"}'
LIMITS = {  # pequeños: (intentos, ventana de calma, primer bloqueo, bloqueo máximo)
    "identifier": (5, 900, 120, 400),
    "pair": (3, 900, 60, 200),
    "ip": (7, 900, 300, 3600),
}
# La misma cuenta escrita de cuatro formas: mayúsculas, espacios y un dominio en ancho completo.
VARIANTS = (EMAIL, "ANA@Example.COM", " ana@example.com ", "ana@ｅｘａｍｐｌｅ.com")


@pytest.fixture(autouse=True)
def limits(settings: Any) -> None:
    settings.LOGIN_THROTTLE = LIMITS


def browser(ip: str = "198.51.100.1") -> Client:
    client = Client(enforce_csrf_checks=True, REMOTE_ADDR=ip)
    client.get(CSRF)
    return client


def later(seconds: int) -> None:
    """Pasa el tiempo: los contadores y sus bloqueos quedan esos segundos atrás."""
    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE login_throttles SET updated_at = updated_at - %(s)s * interval '1 s',"
            " blocked_until = blocked_until - %(s)s * interval '1 s'",
            {"s": seconds},
        )


def rows() -> dict[str, tuple[Any, ...]]:
    found = LoginThrottle.objects.values_list("key", "failures", "blocked_until", "updated_at")
    return {key: tuple(rest) for key, *rest in found}


def count(prefix: str) -> int:
    return int(LoginThrottle.objects.get(key__startswith=prefix).failures)


def fail(addresses: Any, email: str = EMAIL) -> list[Any]:
    """Un fallo desde cada dirección."""
    return [reply(login(browser(address), email, WRONG)) for address in addresses]


def together(calls: list[Any]) -> list[Any]:
    """Todas a la vez, cada una en su hilo y con su conexión. Devuelve resultado o error."""
    gate, results = threading.Barrier(len(calls)), list[Any]([None] * len(calls))

    def run(index: int) -> None:
        try:
            gate.wait(timeout=30)
            results[index] = calls[index]()
        except Exception as error:
            results[index] = error
        finally:
            connection.close()

    threads = [threading.Thread(target=run, args=(index,)) for index in range(len(calls))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return results


def test_after_n_failures_the_right_password_is_refused_until_the_wait_passes(
    ana: User, migrator: psycopg.Connection[Any]
) -> None:
    client = browser()
    for _ in range(3):
        assert reply(login(client, password=WRONG)) == INVALID  # el que llega al límite, también
    with mock.patch.object(services, "authenticate") as checked:
        refused = login(client)  # la contraseña correcta: se rechaza antes de comprobarla
    assert reply(refused) == (429, LIMITED) and refused.headers["Retry-After"] == "60"
    assert "crm_session" not in refused.cookies and not checked.called
    actions = audit(migrator)
    assert [row[0] for row in actions] == ["auth.login.failed"] * 3 + ["auth.login.throttled"]
    assert actions[3][1:5] == ("DENIED", "ANONYMOUS", None, identifier_hash(EMAIL))
    assert json.loads(actions[3][7]) == {"scope": "pair", "failures": 3, "seconds": 60}
    before = rows()
    assert reply(login(client)) == (429, LIMITED)
    assert rows() == before and len(audit(migrator)) == 4  # rechazado: ni cuenta ni audita
    address = LoginThrottle.objects.filter(key__startswith="ip:")
    address.update(blocked_until=Now() + timedelta(seconds=300))
    assert login(client).headers["Retry-After"] == "300"  # la espera más larga de sus claves
    address.update(blocked_until=None)
    later(61)
    assert login(client).status_code == 200  # pasada la espera, entra
    assert not LoginThrottle.objects.filter(key__startswith="par:").exists()  # y se reinicia
    assert (count("id:"), count("ip:")) == (3, 3)  # la cuenta y la dirección solo lo devuelven
    assert EMAIL not in str(sorted(rows()))
    assert [row[0] for row in audit(migrator)].count("auth.login.throttled") == 1  # ese no audita


def test_the_wait_reaches_its_ceiling_and_restarts_only_after_a_quiet_window(
    ana: User, settings: Any, migrator: psycopg.Connection[Any]
) -> None:
    defaults = [base.LOGIN_THROTTLE[scope] for scope in throttle.SCOPES]  # la tabla del README
    assert defaults == [(20, 900, 60, 3600), (5, 900, 60, 900), (30, 900, 300, 3600)]
    assert [throttle.Limit(*rule).block(10**6) for rule in defaults] == [3600, 900, 3600]
    quiet = {scope: (100, 900, 300, 3600) for scope in ("identifier", "ip")}
    settings.LOGIN_THROTTLE = {**quiet, "pair": LIMITS["pair"]}  # solo actúa `pair`
    client, waits = browser(), []
    fail(["198.51.100.1"] * 3)
    for _ in range(6):  # más de una ventana entera de ataque sostenido
        waits.append(int(login(client).headers["Retry-After"]))
        later(waits[-1] + 1)
        assert reply(login(client, password=WRONG)) == INVALID  # otro fallo al acabar la espera
    assert waits == [60, 120, 200, 200, 200, 200] and sum(waits) > 900  # no vuelve a empezar
    held = [json.loads(row[7])["seconds"] for row in audit(migrator) if "throttled" in row[0]]
    assert held[:6] == waits  # la auditoría lleva la espera real, también la escalada
    later(200 + 899)  # el último bloqueo terminó hace menos de una ventana: sigue contando
    assert reply(login(client, password=WRONG)) == INVALID and count("par:") == 10
    later(200 + 901)  # una ventana entera de calma
    assert reply(login(client, password=WRONG)) == INVALID and count("par:") == 1


def test_a_real_account_and_an_unknown_one_are_refused_the_same_way(ana: User) -> None:
    replies = set()
    for email in (EMAIL, "nadie@example.com"):
        client, seen = browser(), []
        for password in (WRONG, WRONG, WRONG, TEST_PASSWORD):
            response = login(client, email, password)
            headers = sorted((k, v) for k, v in response.headers.items() if k != "X-Request-ID")
            seen.append((reply(response), tuple(headers), len(response.cookies)))
        LoginThrottle.objects.all().delete()
        replies.add(tuple(seen))
    assert len(replies) == 1 and replies.pop()[3][0] == (429, LIMITED)


def test_other_addresses_never_lock_the_owner_out(
    ana: User, migrator: psycopg.Connection[Any]
) -> None:
    attackers = [f"203.0.113.{index}" for index in range(5)]
    assert fail(attackers) == [INVALID] * 5  # cinco direcciones: la cuenta queda caliente
    assert login(browser("198.51.100.7")).status_code == 200  # la dueña, desde la suya
    for _ in range(3):  # el ataque sigue en cuanto acaba cada espera: la cuenta no se enfría
        later(401)
        fail(attackers)
        later(1)
        assert login(browser("198.51.100.7")).status_code == 200
    heat = LoginThrottle.objects.get(key__startswith="id:")
    assert heat.failures >= 5 and heat.blocked_until is None  # la cuenta nunca se bloquea
    stranger = browser("192.0.2.44")  # una dirección nueva tiene un intento, acierte o no
    assert reply(login(stranger, password=WRONG)) == INVALID
    refused = login(stranger)
    assert reply(refused) == (429, LIMITED) and refused.headers["Retry-After"] == "120"
    blocks = [json.loads(row[7]) for row in audit(migrator) if "throttled" in row[0]]
    assert [held["seconds"] for held in blocks] == [120] + [240] * 5 + [400] * 10 + [120]
    assert blocks[-1]["scope"] == "identifier" and blocks[-1]["failures"] >= 5


def test_a_hot_account_makes_each_address_wait_longer_up_to_its_own_ceiling(ana: User) -> None:
    fail(f"203.0.113.{index}" for index in range(5))
    client, waits = browser("192.0.2.44"), []
    for _ in range(4):
        assert reply(login(client, password=WRONG)) == INVALID
        waits.append(int(login(client).headers["Retry-After"]))
        later(waits[-1] + 1)
    assert waits == [120, 240, 400, 400]  # la espera de `identifier`, no la de `pair`
    assert login(client).status_code == 200  # y acertar la borra
    assert not LoginThrottle.objects.filter(key__startswith="par:", key__endswith="2.44").exists()


def test_a_good_login_is_invisible_from_other_addresses(ana: User) -> None:
    def probe(email: str, owner_signs_in: bool) -> tuple[Any, ...]:
        LoginThrottle.objects.all().delete()
        fail((f"203.0.113.{index}" for index in range(4)), email)  # a un fallo de calentarse
        later(600)
        mark, primed = "id:" + identifier_hash(email), rows()
        if owner_signs_in:
            assert login(browser("198.51.100.7"), email).status_code == 200
        assert rows()[mark] == primed[mark]  # ni el contador de la cuenta ni su reloj se mueven
        later(301)  # los fallos cumplen la ventana de calma; un acceso a mitad no la acorta
        return (*fail(["192.0.2.1"] * 2, email), rows()[mark][0])

    idle = probe(EMAIL, owner_signs_in=False)
    assert idle == (INVALID, INVALID, 2)  # el contador volvió a empezar: nadie espera
    assert probe(EMAIL, owner_signs_in=True) == idle == probe("nadie@example.com", False)


def test_each_failure_keeps_the_window_open(ana: User) -> None:
    for _ in range(3):  # tres fallos en 20 minutos, nunca 15 de calma entre dos
        assert fail(["198.51.100.1"]) == [INVALID]
        later(600)
    assert {row[0] for row in rows().values()} == {3}  # siguen contando juntos
    later(361)  # ahora sí: una ventana entera desde el último fallo y desde su bloqueo de 60 s
    assert fail(["198.51.100.1"]) == [INVALID] and {row[0] for row in rows().values()} == {1}


def test_two_good_logins_at_once_leave_the_account_as_it_was(ana: User) -> None:
    fail(f"203.0.113.{index}" for index in range(4))
    later(600)
    first, second = (throttle.keys(EMAIL, f"198.51.100.{index}") for index in (7, 8))
    primed = rows()[first["identifier"]]
    started = [throttle.admit(first), throttle.admit(second)]  # las dos se evalúan a la vez
    assert rows()[first["identifier"]] == (
        primed[0] + 2,
        *primed[1:],
    )  # contadas, sin mover el reloj
    for attempt, mine in zip((first, second), started, strict=True):
        with transaction.atomic():
            throttle.forgive(attempt, mine)
    assert rows()[first["identifier"]] == primed


def test_pausing_does_not_reset_an_address_while_the_account_stays_hot(ana: User) -> None:
    fail(f"203.0.113.{index}" for index in range(5))
    client, pair = browser("192.0.2.44"), throttle.keys(EMAIL, "192.0.2.44")["pair"]
    assert reply(login(client, password=WRONG)) == INVALID  # su único intento: espera 120 s
    with connection.cursor() as cursor:  # esa dirección calla más de una ventana; las demás, no
        cursor.execute(
            "UPDATE login_throttles SET updated_at = updated_at - interval '2000 s',"
            " blocked_until = blocked_until - interval '2000 s' WHERE key = %s",
            [pair],
        )
    assert purge_login_throttles() == 0  # la purga tampoco lo borra: sigue donde estaba
    assert reply(login(client, password=WRONG)) == INVALID
    assert login(client).headers["Retry-After"] == "240"  # sigue donde estaba: no vuelve a 120
    later(2000)  # ahora calla toda la cuenta: deja de estar caliente y el contador sí empieza
    assert purge_login_throttles() == 7  # ya fría: todo menos los de `pair`, que aún esperan
    assert reply(login(client, password=WRONG)) == INVALID and rows()[pair][0] == 1


def test_one_address_trying_many_accounts_is_blocked_by_address(
    migrator: psycopg.Connection[Any],
) -> None:
    client = browser("203.0.113.9")
    for index in range(7):
        assert reply(login(client, f"cuenta{index}@example.com", WRONG)) == INVALID
    refused = login(client, "otra@example.com", WRONG)
    assert reply(refused) == (429, LIMITED) and refused.headers["Retry-After"] == "300"
    last = audit(migrator)[-1]
    assert (last[0], last[4], json.loads(last[7])["scope"]) == ("auth.login.throttled", None, "ip")
    assert reply(login(browser("203.0.113.10"), "otra@example.com", WRONG)) == INVALID  # otra, no


def test_a_good_login_gives_back_what_it_counted_on_the_address(ana: User) -> None:
    client = browser("203.0.113.9")
    for index in range(5):
        assert reply(login(client, f"cuenta{index}@example.com", WRONG)) == INVALID
    good = throttle.keys(EMAIL, "203.0.113.9")
    started = throttle.admit(good)  # un acceso correcto que aún se evalúa cuenta como intento
    assert reply(login(client, "cuenta5@example.com", WRONG)) == INVALID  # y otro llega al límite
    with transaction.atomic():
        throttle.forgive(good, started)  # y se lleva ese bloqueo: seis fallos no lo sostienen
    before = rows()["ip:203.0.113.9"]
    assert login(client).status_code == 200  # el séptimo intento de la dirección, correcto
    assert rows()["ip:203.0.113.9"] == before  # ni cuenta, ni bloquea, ni mueve su reloj
    assert before[:2] == (6, None)
    assert reply(login(client, "otra@example.com", WRONG)) == INVALID  # el séptimo fallo
    assert login(client, "otra@example.com", WRONG).headers["Retry-After"] == "300"
    later(301)
    assert login(client).status_code == 200  # con fallos recientes, acertar no quita la escalada…
    assert reply(login(client, "otra@example.com", WRONG)) == INVALID
    assert login(client, "otra@example.com", WRONG).headers["Retry-After"] == "600"  # …sigue


def test_one_account_or_one_network_is_one_key_however_it_is_written() -> None:
    same = {throttle.keys(email, None)["identifier"] for email in VARIANTS}
    assert same == {"id:" + identifier_hash(EMAIL)}
    assert throttle.keys("otra@example.com", None)["identifier"] not in same
    unknown = throttle.keys("sin arroba", None)  # sin dirección: todos comparten una
    assert unknown["pair"].endswith(":-") and unknown["ip"] == "ip:-"
    addresses = ("2001:db8:1:2::1", "2001:db8:1:2:ffff::9", "2001:db8:1:3::1")
    found = [throttle.keys(EMAIL, ip) for ip in addresses]
    assert [keys["ip"] for keys in found] == ["ip:2001:db8:1:2::/64"] * 2 + ["ip:2001:db8:1:3::/64"]
    mapped = throttle.keys(EMAIL, "::ffff:198.51.100.1")  # una IPv4 escrita como IPv6
    assert mapped["ip"] == "ip:198.51.100.1"


def test_other_spellings_of_the_account_do_not_buy_more_attempts(
    ana: User, migrator: psycopg.Connection[Any]
) -> None:
    client = browser()
    for email in VARIANTS[1:]:
        assert reply(login(client, email, WRONG)) == INVALID
    assert login(client, VARIANTS[0]).status_code == 429
    marks = {row[4] for row in audit(migrator)}  # los tres fallos y el bloqueo: una huella
    assert marks == {identifier_hash(EMAIL)}


def test_a_simultaneous_burst_tries_no_more_passwords_than_the_limit(ana: User) -> None:
    fail(["198.51.100.1"])
    later(1000)  # contadores de un fallo antiguo: la ráfaga los reinicia una sola vez
    attempt = throttle.keys(EMAIL, "198.51.100.1")
    results = together([lambda: throttle.admit(attempt)] * 12)
    admitted = [result for result in results if isinstance(result, list)]
    refused = [result for result in results if isinstance(result, throttle.Refused)]
    assert len(admitted) == 3 and len(refused) == 9  # el límite de `pair`
    assert sorted(len(started) for started in admitted) == [0, 0, 1]  # uno empieza el bloqueo
    assert all(0 < result.seconds <= 60 for result in refused)
    assert {row[0] for row in rows().values()} == {3}  # las tres claves; lo rechazado no cuenta


def test_a_burst_from_many_addresses_gets_one_attempt_each_once_the_account_is_hot(
    ana: User,
) -> None:
    fail(f"203.0.113.{index}" for index in range(4))  # a un fallo de calentarse
    attempts = [throttle.keys(EMAIL, f"192.0.2.{index}") for index in range(8)]
    results = together([lambda attempt=attempt: throttle.admit(attempt) for attempt in attempts])
    assert count("id:") == 12  # contado antes de evaluar, uno por intento
    assert [[held.scope for held in started] for started in results] == [["identifier"]] * 8
    for attempt in attempts:  # el siguiente de cada dirección ya espera
        with pytest.raises(throttle.Refused):
            throttle.admit(attempt)
    assert count("id:") == 12


def test_an_attempt_that_meets_a_block_just_started_counts_nowhere(
    ana: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    for index in range(7):  # la dirección llega a su límite y queda bloqueada
        login(browser("203.0.113.9"), f"cuenta{index}@example.com", WRONG)
    before, real, seen = rows(), throttle.blocked_for, [0]

    def late(attempt: dict[str, str]) -> int:
        """La primera consulta aún no ve el bloqueo: lo empezó otro intento a la vez."""
        seen[0] += 1
        return 0 if seen[0] == 1 else real(attempt)

    monkeypatch.setattr(throttle, "blocked_for", late)
    with pytest.raises(throttle.Refused) as refused:
        throttle.admit(throttle.keys(EMAIL, "203.0.113.9"))
    assert 290 < refused.value.seconds <= 300
    assert rows() == before  # tampoco la cuenta, que se contó antes de ver el bloqueo


def test_admitting_and_forgiving_at_once_never_deadlock(settings: Any) -> None:
    settings.LOGIN_THROTTLE = {scope: (10_000, 900, 60, 3600) for scope in LIMITS}
    attempt = throttle.keys(EMAIL, "198.51.100.1")  # tres filas compartidas

    def succeed() -> None:
        for _ in range(25):
            started = throttle.admit(attempt)
            with transaction.atomic():
                throttle.forgive(attempt, started)

    def fail_again() -> None:
        for _ in range(25):
            throttle.admit(attempt)
            throttle.failed(attempt)

    assert together([succeed, fail_again] * 4) == [None] * 8
    counted = rows()
    assert {counted[attempt[scope]][0] for scope in ("identifier", "ip")} == {100}


def test_a_block_that_cannot_be_audited_does_not_change_the_reply(
    ana: User, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    record, reports = platform.record, []

    def broken(action: str, **fields: Any) -> None:
        if action == "auth.login.throttled":
            raise RuntimeError("auditoría no disponible")
        record(action, **fields)

    spy = SimpleNamespace(capture_exception=lambda error, context=None: reports.append(context))
    monkeypatch.setattr(reporting, "reporter", lambda: spy)
    monkeypatch.setattr(platform, "record", broken)
    client = browser()
    assert fail(["198.51.100.1"] * 3) == [INVALID] * 3  # el que empieza el bloqueo, igual
    assert login(client).status_code == 429
    assert reports == [{"action": "auth.login.throttled"}]  # sin auditar, pero no en silencio
    assert reply(login(browser(""), "nadie@example.com", WRONG)) == INVALID
    assert [r.levelname for r in caplog.records if "sin dirección" in r.message] == ["WARNING"]
    assert EMAIL not in caplog.text and WRONG not in caplog.text


def test_the_purge_keeps_live_counters(ana: User, migrator: psycopg.Connection[Any]) -> None:
    job = base.CELERY_BEAT_SCHEDULE["accounts.purge_login_throttles"]
    assert (purge_login_throttles.name, purge_login_throttles.tenancy) == (job["task"], "platform")
    assert [len(job["schedule"].hour), len(job["schedule"].minute)] == [24, 1]  # una vez cada hora
    fail(["198.51.100.1"] * 3)  # deja un bloqueo de 60 s en la cuenta con esa dirección
    fail(["198.51.100.2"], "luis@example.com")  # y contadores sin bloqueo
    total = LoginThrottle.objects.count()
    assert total == 6 and purge_login_throttles() == 0  # todo es reciente: no se borra nada
    later(890)
    assert purge_login_throttles() == 0  # aún no hay una ventana entera de calma
    later(20)
    assert purge_login_throttles() == total - 2  # los de `pair` se conservan más
    later(86400 - 1000)  # un día (README), no `PAIR_KEPT`
    assert purge_login_throttles() == 0  # con la cuenta caliente al volver seguirían contando
    later(1000)
    assert purge_login_throttles() == 2 and not LoginThrottle.objects.exists()
    fail(["198.51.100.1"] * 3)
    with connection.cursor() as cursor:  # un bloqueo que aún corre, con su último intento viejo
        cursor.execute("UPDATE login_throttles SET updated_at = now() - interval '1 hour'")
        cursor.execute("SET lock_timeout = '2s'")
    migrator.execute("BEGIN; SELECT 1 FROM login_throttles WHERE key LIKE 'id:%' FOR UPDATE")
    assert purge_login_throttles() == 1  # salta la fila que un acceso tiene bloqueada
    migrator.execute("ROLLBACK")
    assert purge_login_throttles() == 1
    assert [*rows()] == [throttle.keys(EMAIL, "198.51.100.1")["pair"]]  # el bloqueado sigue


@pytest.mark.parametrize(
    "change",
    [
        {"ip": None},  # falta un contador
        {"identifier": None, "indentifier": (5, 900, 60, 200)},  # mal escrito
        {"pair": (3, 900, 60)},
        {"pair": [3, 900, 60, 200]},
        {"ip": (7, 900, 0, 3600)},  # nunca bloquearía
        {"ip": (7, 900, "300", 3600)},
        {"pair": (True, 900, 60, 200)},
        {"ip": (7, 10**13, 300, 3600)},  # desbordaría el intervalo en cada login
        {"otro": (3, 900, 60, 200)},  # sobra uno: nadie lo usaría
        {"ip": (7, 900, 4000, 3600)},  # el primer bloqueo, mayor que el máximo
        {"identifier": (5, 900, 30, 400)},  # la cuenta caliente esperaría menos que sin calentar
        {"identifier": (5, 900, 120, 150)},
        {"pair": (3, 59, 60, 200)},  # ventana de menos de 60 s (o intercambiada con los intentos)
        {"ip": (1001, 900, 300, 3600)},  # más de 1000 intentos: nunca bloquearía
    ],
)
def test_a_misconfigured_limit_does_not_start(settings: Any, change: dict[str, Any]) -> None:
    accounts = apps.get_app_config("accounts")
    assert checks.login_throttle() is None
    accounts.ready()  # con la configuración válida, la aplicación carga
    merged = {**LIMITS, **change}
    settings.LOGIN_THROTTLE = {scope: value for scope, value in merged.items() if value}
    assert checks.login_throttle()
    with pytest.raises(ImproperlyConfigured, match="LOGIN_THROTTLE"):
        accounts.ready()  # web, worker, beat y `manage.py`: ninguno arranca

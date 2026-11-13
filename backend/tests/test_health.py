import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from unittest import mock

import pytest
from django.test import Client

from config import env
from core import health
from core.storage.s3 import from_settings

CHECKS = {"database": "ok", "redis": "ok", "storage": "ok"}
BLACKHOLE = "192.0.2.1"  # TEST-NET-1 (RFC 5737): no responde; mide el tiempo límite


def failing(check: str) -> dict[str, Any]:
    return {"status": "fail", "checks": {**CHECKS, check: "fail"}}


@pytest.fixture(autouse=True)
def fresh() -> Iterator[None]:
    health.forget()  # el resultado se guarda unos segundos por proceso
    yield
    health.forget()


@pytest.fixture
def redis_up(settings: Any) -> Iterator[mock.MagicMock]:
    settings.CELERY_BROKER_URL = "redis://broker.internal:6379/0"
    settings.CHANNEL_LAYER_REDIS_URL = "redis://broker.internal:6379/0"
    with mock.patch("core.health.redis.Redis.from_url") as from_url:
        yield from_url


def test_live_ok_without_touching_any_dependency(client: Client) -> None:
    never = mock.Mock(side_effect=AssertionError("live no consulta dependencias"))
    with (
        mock.patch("core.health._database_ok", never),
        mock.patch("core.health._redis_ok", never),
        mock.patch("core.health._storage_ok", never),
    ):
        response = client.get("/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert "no-cache" in response["Cache-Control"]


@pytest.mark.django_db
def test_ready_ok_with_real_database_redis_answering_and_storage(
    client: Client, redis_up: mock.MagicMock
) -> None:
    response = client.get("/health/ready")
    assert (response.status_code, response.json()) == (200, {"status": "ok", "checks": CHECKS})
    redis_up.assert_called_once()  # la misma URL para broker y canales: un solo PING
    connect = redis_up.call_args.kwargs
    assert connect["socket_timeout"] == connect["socket_connect_timeout"] == health.TIMEOUT
    assert connect["retry"].get_retries() == 0  # redis 8 no reintenta hoy; que no empiece
    redis_up.return_value.ping.assert_called_once_with()
    redis_up.return_value.close.assert_called_once_with()


@pytest.mark.django_db
def test_ready_pings_the_broker_and_the_channel_layer_when_they_differ(
    client: Client, redis_up: mock.MagicMock, settings: Any
) -> None:
    settings.CHANNEL_LAYER_REDIS_URL = "redis://broker.internal:6379/1"
    assert client.get("/health/ready").status_code == 200
    urls = sorted(call.args[0] for call in redis_up.call_args_list)
    assert urls == ["redis://broker.internal:6379/0", "redis://broker.internal:6379/1"]
    redis_up.return_value.ping.side_effect = [None, ConnectionError("canales caído")]
    health.forget()
    assert client.get("/health/ready").json() == failing("redis")


def test_ready_fails_without_leaking_details(client: Client, redis_up: mock.MagicMock) -> None:
    with mock.patch("core.health.connection") as conn:
        conn.cursor.side_effect = RuntimeError("could not connect to host db.internal:5432")
        response = client.get("/health/ready")
    assert (response.status_code, response.json()) == (503, failing("database"))
    assert b"db.internal" not in response.content


@pytest.mark.django_db
@pytest.mark.parametrize(
    "broker",
    [
        f"redis://:s3cret@{BLACKHOLE}:6379/0",  # no contesta: tiempo límite, sin reintentos
        "redis://127.0.0.1:1/0",  # puerto cerrado
        "",  # sin configurar: sin Redis no hay outbox, ni Celery, ni canales
        "amqp://guest:s3cret@rabbit.internal//",  # no es Redis
    ],
)
def test_redis_down_is_a_bounded_503_that_names_only_the_check(
    client: Client, settings: Any, broker: str, caplog: pytest.LogCaptureFixture
) -> None:
    settings.CELERY_BROKER_URL, settings.CHANNEL_LAYER_REDIS_URL = broker, ""
    started = time.monotonic()
    response = client.get("/health/ready")
    assert time.monotonic() - started < 2.5
    assert (response.status_code, response.json()) == (503, failing("redis"))
    assert "readiness: Redis" in caplog.text
    for secret in ("s3cret", BLACKHOLE, "rabbit", "6379"):
        assert secret not in response.text and secret not in caplog.text


@pytest.mark.django_db
@pytest.mark.parametrize(
    "storage",
    [
        {"STORAGE_ENDPOINT_URL": f"http://{BLACKHOLE}:3900"},  # tiempo límite, sin reintentos
        {"STORAGE_ENDPOINT_URL": "http://127.0.0.1:1"},
        {"STORAGE_BUCKET": ""},  # sin configurar
        {"STORAGE_BACKEND": "ftp"},
    ],
)
def test_storage_down_is_a_bounded_503_without_details(
    client: Client,
    settings: Any,
    redis_up: mock.MagicMock,
    storage: dict[str, str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings.STORAGE_BACKEND, settings.STORAGE_BUCKET = "s3", "crm-private-bucket"
    settings.STORAGE_ACCESS_KEY_ID, settings.STORAGE_SECRET_ACCESS_KEY = "AKIDEXAMPLE", "s3cret"
    for name, value in storage.items():
        setattr(settings, name, value)
    started = time.monotonic()
    response = client.get("/health/ready")
    assert time.monotonic() - started < 2.5
    assert (response.status_code, response.json()) == (503, failing("storage"))
    assert "readiness: almacenamiento" in caplog.text
    for secret in ("s3cret", "AKIDEXAMPLE", "crm-private-bucket", BLACKHOLE, "3900", "ftp"):
        assert secret not in response.text and secret not in caplog.text


def test_the_storage_check_is_one_short_head_of_the_bucket(settings: Any) -> None:
    settings.STORAGE_BACKEND, settings.STORAGE_BUCKET = "s3", "crm-private-bucket"
    settings.STORAGE_ACCESS_KEY_ID, settings.STORAGE_SECRET_ACCESS_KEY = "AKIDEXAMPLE", "x"
    with mock.patch("core.storage.s3.boto3.client") as client:
        assert health._storage_ok() is True
    config = client.call_args.kwargs["config"]
    assert (config.connect_timeout, config.read_timeout) == (health.TIMEOUT, health.TIMEOUT)
    assert config.retries["total_max_attempts"] == 1
    calls = client.return_value.method_calls
    assert [c[0] for c in calls] == ["head_bucket"]  # nada que escriba
    assert calls[0].kwargs == {"Bucket": "crm-private-bucket"}


def test_the_upload_and_download_client_keeps_its_own_longer_limits(settings: Any) -> None:
    settings.STORAGE_BUCKET, settings.STORAGE_ACCESS_KEY_ID = "b", "AKIDEXAMPLE"
    settings.STORAGE_SECRET_ACCESS_KEY = "x" * 8
    config = from_settings().client.meta.config
    assert (config.connect_timeout, config.read_timeout) == (5, 30)
    assert config.retries["total_max_attempts"] == 4


def test_a_database_host_that_does_not_answer_fails_in_seconds() -> None:
    options = env.database("postgres://u:p@db:5432/crm")["OPTIONS"]
    assert options == {"connect_timeout": env.CONNECT_TIMEOUT} and env.CONNECT_TIMEOUT <= 3


def test_concurrent_probes_run_the_checks_once() -> None:
    calls, gate = [], threading.Event()

    def slow() -> bool:
        calls.append(1)
        gate.wait(2)
        return True

    with (
        mock.patch("core.health._database_ok", slow),
        mock.patch("core.health._redis_ok", lambda: True),
        mock.patch("core.health._storage_ok", lambda: True),
        ThreadPoolExecutor(8) as pool,
    ):
        probes = [pool.submit(health._checks) for _ in range(8)]
        time.sleep(0.2)
        gate.set()
        assert all(p.result() == CHECKS for p in probes)
    assert len(calls) == 1


def test_the_window_starts_when_the_checks_end() -> None:
    now, calls = [1000.0], []

    def slow() -> bool:
        calls.append(1)
        now[0] += 3.0  # comprobar tarda 3 s
        return True

    with (
        mock.patch("core.health.time.monotonic", lambda: now[0]),
        mock.patch("core.health._database_ok", slow),
        mock.patch("core.health._redis_ok", lambda: True),
        mock.patch("core.health._storage_ok", lambda: True),
    ):
        health._checks()
        now[0] += health.READY_TTL - 1  # 4 s tras terminar (7 s tras empezar): aún guardado
        health._checks()
    assert len(calls) == 1


@pytest.mark.django_db
def test_a_burst_runs_the_checks_once_per_window(client: Client, redis_up: mock.MagicMock) -> None:
    now = [1000.0]
    with mock.patch("core.health.time.monotonic", lambda: now[0]):
        for _ in range(5):
            assert client.get("/health/ready").status_code == 200
        assert redis_up.call_count == 1
        redis_up.return_value.ping.side_effect = ConnectionError
        now[0] += health.READY_TTL - 0.1
        assert client.get("/health/ready").status_code == 200  # aún el resultado guardado
        now[0] += 0.2
        assert client.get("/health/ready").status_code == 503
        assert redis_up.call_count == 2
        assert client.get("/health/ready").status_code == 503  # el fallo también se guarda
        assert redis_up.call_count == 2


@pytest.mark.parametrize("path", ["/health/live", "/health/ready"])
def test_health_rejects_unsafe_methods(client: Client, path: str) -> None:
    assert client.post(path).status_code == 405

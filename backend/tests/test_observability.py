"""Observabilidad (ADR-011): logs JSON redactados, request/correlation id, HTTP → Celery y Sentry.

Los secretos de prueba se construyen en ejecución (gitleaks sin allowlist). Ninguna red.
"""

import io
import json
import logging
import logging.config
from collections.abc import Iterator
from typing import Any
from uuid import UUID, uuid4

import pytest
import structlog
from celery.signals import before_task_publish
from django.conf import settings
from django.test import Client

from core.ids import new_id
from core.observability import reporting
from core.observability.celery import HEADER
from core.observability.context import bound, current_correlation_id, current_request_id
from core.observability.logging import json_formatter
from core.tenancy import middleware
from core.tenancy.context import ActorType, TenantContext
from core.tenancy.resolution import resolve_tenant
from core.tenancy.scope import tenant_scope
from tests import fakes
from tests.tenancy_app.tasks import FAILURES, PROBES, failing_probe, log_probe, outbox_probe

pytestmark = pytest.mark.usefixtures("tenant_db")
SECRETS = {
    "openai": "sk-" + "proj-" + "Ab1" * 16,
    "anthropic": "sk-" + "ant-api03-" + "Xy9" * 30,
    "meta": "EAA" + "Bw" + "Zq7" * 15,
    "bearer": "Bearer " + "tok" + "3n" * 10,
    "url": "postgres://crm_app:" + "S3cr3t" + "pw@db:5432/crm",
}


class Output:
    def __init__(self) -> None:
        self.stream = io.StringIO()

    @property
    def text(self) -> str:
        return self.stream.getvalue()

    def records(self) -> list[dict[str, Any]]:
        return [json.loads(line) for line in self.text.splitlines()]  # cada línea: JSON válido


@pytest.fixture
def out() -> Iterator[Output]:
    """El mismo formatter que el handler de stdout de LOGGING, sobre un buffer."""
    output = Output()
    handler = logging.StreamHandler(output.stream)
    handler.setFormatter(json_formatter())
    root = logging.getLogger()
    level, root.level = root.level, logging.INFO
    root.addHandler(handler)
    yield output
    root.removeHandler(handler)
    root.setLevel(level)


def test_logging_settings_write_json_to_stdout() -> None:
    logging_config: dict[str, Any] = settings.LOGGING
    console = logging_config["handlers"]["console"]
    assert (console["stream"], console["formatter"]) == ("ext://sys.stdout", "json")


def test_structlog_and_stdlib_logs_are_json_and_redacted(out: Output) -> None:
    for name, secret in SECRETS.items():
        structlog.get_logger("tests").info("login.failed", detail=f"reintento con {secret}")
        logging.getLogger("django.request").warning("error externo: %s", secret)
        logging.getLogger("tests").info("detalle", extra={"password": name + "-pw1"})
    kwargs = {"password": "hunter2-pw", "token": "t0k3n-x"}  # por nombre de clave
    structlog.get_logger("tests").info("login.failed", **kwargs)
    records = out.records()
    assert len(records) == 3 * len(SECRETS) + 1
    for secret in [*SECRETS.values(), "hunter2-pw", "t0k3n-x"]:
        assert secret.split(" ")[-1] not in out.text
    assert {"timestamp", "level", "logger", "event", "request_id", "correlation_id"} <= set(
        records[0]
    )


def test_exception_messages_are_redacted(out: Output) -> None:
    secret = SECRETS["anthropic"]
    try:
        raise RuntimeError(f"proveedor rechazó la clave {secret}")
    except RuntimeError:
        logging.getLogger("tests").error("proveedor.fallo", exc_info=True)
        structlog.get_logger("tests").exception("proveedor.fallo")
    assert secret not in out.text
    assert all("RuntimeError" in r["exception"] for r in out.records())  # tipo y traza técnica


def test_objects_positional_dicts_and_dict_reprs_are_redacted(out: Output) -> None:
    secret, pw = SECRETS["openai"], "hunter2-" + "pw9"
    structlog.get_logger("tests").error("fallo", error=RuntimeError(f"clave {secret}"))
    structlog.get_logger("tests").info("login %s", {"password": pw})
    logging.getLogger("tests").info("login %s", {"password": pw})
    logging.getLogger("tests").error("upstream: %s", ValueError({"password": pw}))
    assert secret not in out.text and pw not in out.text
    assert len(out.records()) == 4


def test_uvicorn_and_celery_trace_loggers_use_the_json_pipeline() -> None:
    config: dict[str, Any] = settings.LOGGING
    assert config["loggers"]["uvicorn"]["handlers"] == ["console"]
    assert config["loggers"]["uvicorn.error"] == {"handlers": [], "propagate": True}
    assert config["loggers"]["celery.app.trace"]["level"] == "WARNING"
    assert config["loggers"]["celery.pool"]["level"] == "INFO"


def test_the_uvicorn_access_log_stays_off() -> None:
    """Lleva la dirección del cliente y la query string. uvicorn lo emite si el logger tiene
    algún handler efectivo, propio o heredado: con la configuración del proyecto no lo tiene."""
    access = logging.getLogger("uvicorn.access")
    previous = access.handlers[:], access.propagate
    access.addHandler(logging.StreamHandler())  # lo que uvicorn instala antes de cargar Django
    try:
        # Solo la entrada de este logger: `dictConfig` entero es global (cierra y quita los
        # handlers del root, también los de pytest) y eso no se deshace desde aquí.
        config: dict[str, Any] = settings.LOGGING
        configurator = logging.config.DictConfigurator(config)
        configurator.configure_logger("uvicorn.access", config["loggers"]["uvicorn.access"])
        assert not access.hasHandlers() and not access.propagate
        assert logging.getLogger("uvicorn.error").hasHandlers()  # los errores sí se registran
    finally:
        access.handlers[:], access.propagate = previous


def test_http_request_ids_are_generated_and_do_not_leak(out: Output) -> None:
    client = Client(headers={"X-Request-ID": "attacker-controlled", "X-Correlation-ID": "evil"})
    first = client.get("/health/live?token=abc123secret")
    second = client.get("/health/live")
    ids = [first["X-Request-ID"], second["X-Request-ID"]]
    assert "attacker-controlled" not in ids and ids[0] != ids[1]
    assert [UUID(i).version for i in ids] == [7, 7]
    logs = [r for r in out.records() if r["event"] == "http.request.completed"]
    assert [(r["request_id"], r["correlation_id"]) for r in logs] == [(i, i) for i in ids]
    assert logs[0]["path"] == "/health/live" and "abc123secret" not in out.text  # sin query
    assert current_request_id() is None and current_correlation_id() is None  # sin fuga


def test_tenant_request_carries_the_correlation_into_the_tenant_context(
    orgs: dict[str, UUID], out: Output, monkeypatch: pytest.MonkeyPatch
) -> None:
    user, seen = uuid4(), []
    fakes.MEMBERS.add((user, orgs["A"]))
    real = resolve_tenant

    def spy(*args: Any) -> TenantContext:
        seen.append(real(*args))
        return seen[-1]

    monkeypatch.setattr(middleware, "resolve_tenant", spy)
    try:
        response = Client(headers={"X-Test-User": str(user)}).get("/api/v1/o/org-a/widgets/")
    finally:
        fakes.MEMBERS.clear()
    assert seen[0].correlation_id == response["X-Request-ID"]  # audit/outbox/Celery lo heredan
    log = next(r for r in out.records() if r["event"] == "http.request.completed")
    assert (log["organization_id"], log["user_id"], log["actor_type"]) == (
        str(orgs["A"]),
        str(user),
        "USER",
    )


def test_tenant_scope_logs_carry_tenant_and_actor(orgs: dict[str, UUID], out: Output) -> None:
    user = uuid4()
    with tenant_scope(TenantContext(orgs["A"], "test", user, ActorType.USER, user, "corr-t")):
        structlog.get_logger("tests").info("widget.updated")
    record = out.records()[0]
    assert (record["organization_id"], record["user_id"], record["actor_type"]) == (
        str(orgs["A"]),
        str(user),
        "USER",
    )
    assert record["correlation_id"] == "corr-t"


def test_correlation_travels_from_http_to_celery_by_header(
    orgs: dict[str, UUID], out: Output
) -> None:
    headers: dict[str, Any] = {}
    with bound(request_id="req-1", correlation_id="corr-http"):
        before_task_publish.send(sender="tenancy_app.log_probe", headers=headers, body=())
    assert headers == {HEADER: "corr-http"}  # el request_id no viaja al worker
    log_probe.apply(headers=headers).get()  # worker: sin contexto ambiente
    PROBES.clear()
    kwargs = {"event_id": str(new_id()), "organization_id": str(orgs["A"])}  # sin correlation_id
    outbox_probe.apply(kwargs=kwargs, headers=headers).get()
    assert PROBES[0][1] == "corr-http"  # tenant_task: cabecera → TenantContext.correlation_id
    outbox_probe.apply(kwargs={**kwargs, "correlation_id": "corr-kw"}, headers=headers).get()
    assert PROBES[1][1] == "corr-kw"  # el kwarg explícito (outbox) prevalece...
    probes = [r for r in out.records() if r["event"] == "probe.ran"]
    assert (probes[0]["correlation_id"], probes[0]["request_id"]) == ("corr-http", None)
    assert [r["correlation_id"] for r in probes[1:]] == [
        "corr-http",
        "corr-kw",
    ]  # ...también en logs


def test_celery_context_does_not_leak_between_tasks(out: Output) -> None:
    log_probe.apply(headers={HEADER: "corr-A"}).get()
    log_probe.apply().get()  # B sin correlación
    with bound(request_id="req-x", correlation_id="outer"):
        log_probe.apply(headers={HEADER: "corr-C"}).get()  # eager dentro de una petición
        assert current_correlation_id() == "outer"  # se restaura el contexto de la petición
    probes = [r["correlation_id"] for r in out.records() if r["event"] == "probe.ran"]
    assert probes == ["corr-A", None, "corr-C"]
    assert current_correlation_id() is None


def test_explicit_correlation_survives_until_task_failure(
    orgs: dict[str, UUID], out: Output
) -> None:
    FAILURES.clear()
    org = str(orgs["A"])
    failing_probe.apply(kwargs={"organization_id": org, "correlation_id": "corr-outbox"})
    failing_probe.apply(
        kwargs={"organization_id": org, "correlation_id": "corr-explicit"},
        headers={HEADER: "corr-header"},
    )  # el kwarg explícito prevalece sobre la cabecera
    log_probe.apply().get()  # siguiente tarea sin correlación: sin fuga desde el fallo
    failed = [r for r in out.records() if r["event"] == "celery.task.failed"]
    assert [r["correlation_id"] for r in failed] == ["corr-outbox", "corr-explicit"]
    assert all(r["exception_type"] == "RuntimeError" and "kwargs" not in r for r in failed)
    assert [f["correlation_id"] for f in FAILURES] == ["corr-outbox", "corr-explicit"]
    probe = next(r for r in out.records() if r["event"] == "probe.ran")
    assert probe["correlation_id"] is None and current_correlation_id() is None


def test_sentry_scrubber_removes_secrets_and_pii() -> None:
    s = SECRETS
    event: dict[str, Any] = {
        "request": {
            "url": "https://app.example.com/api/v1/o/acme/x",
            "data": {"body": "hola, mi tarjeta es 4111"},
            "cookies": {"sessionid": "sess-" + "c00k1e"},
            "query_string": "token=qs-" + "s3cr3t",
            "headers": {"Authorization": s["bearer"], "Cookie": "sid=ck-" + "v4lue",
                        "X-Hub-Signature-256": "sha256=h00k", "X-CSRFToken": "csrf-" + "t0k",
                        "User-Agent": "ua"},
        },
        "user": {"id": "u-1", "email": "ana@example.com", "username": "ana",
                 "ip_address": "1.2.3.4"},
        "exception": {"values": [{"type": "RuntimeError", "value": f"fallo {s['openai']}",
                                  "stacktrace": {"frames": [{"vars": {"key": s["meta"]}}]}}]},
        "breadcrumbs": {"values": [{"message": f"llamando con {s['anthropic']}"}]},
        "extra": {"nested": {"password": "pw-" + "extra", "dsn": s["url"]}},
        "contexts": {"crm": {"api_key": "ak-" + "ctx"}},
    }  # fmt: skip
    with bound(request_id="req-s", correlation_id="corr-s"):
        scrubbed = reporting.scrub_sentry_event(event)
    text = json.dumps(scrubbed)
    for leaked in [*s.values(), "4111", "c00k1e", "s3cr3t", "v4lue", "ana@example.com", '"ana"',
                   "1.2.3.4", "pw-extra", "ak-ctx", "tok3n", "h00k", "csrf-t0k"]:  # fmt: skip
        assert leaked not in text, leaked
    assert scrubbed["user"] == {"id": "u-1"} and scrubbed["request"]["headers"] == {
        "User-Agent": "ua"
    }
    assert scrubbed["tags"] == {"request_id": "req-s", "correlation_id": "corr-s"}


def test_reporter_is_noop_without_dsn_and_sentry_is_hardened(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr("sentry_sdk.init", lambda **kw: calls.append(kw))
    reporting.reporter.cache_clear()
    try:
        assert isinstance(reporting.reporter(), reporting.NoopReporter) and calls == []
        reporting.reporter.cache_clear()
        monkeypatch.setattr(settings, "SENTRY_DSN", "https://public@sentry.example.invalid/1")
        assert isinstance(reporting.reporter(), reporting.SentryReporter)
    finally:
        reporting.reporter.cache_clear()
    options = calls[0]
    assert (options["send_default_pii"], options["traces_sample_rate"]) == (False, 0.0)
    assert options["before_send"] is reporting.scrub_sentry_event
    assert (options["include_local_variables"], options["max_request_body_size"]) == (
        False,
        "never",
    )
    assert not options["auto_enabling_integrations"] and "profiles_sample_rate" not in options

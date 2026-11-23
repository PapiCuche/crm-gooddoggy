import ast
import base64
import importlib
import secrets
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from django.core.exceptions import ImproperlyConfigured

GOOD_KEY = "k" * 60
MIGRATOR_SECRET = secrets.token_hex(16)  # aleatorio por ejecución
BASE_ENV: dict[str, str | None] = {
    "DJANGO_SECRET_KEY": GOOD_KEY,
    "DJANGO_ALLOWED_HOSTS": "app.example.com",
    "FORWARDED_ALLOW_IPS": "10.0.0.2",
    "DATABASE_URL": "postgres://crm_app:secret@db:5432/crm",
    "DATABASE_MIGRATOR_URL": None,
    "CRM_MIGRATOR_PASSWORD": None,
    "UVICORN_FORWARDED_ALLOW_IPS": None,
    "STORAGE_ENDPOINT_URL": None,
    "STORAGE_PUBLIC_ENDPOINT_URL": None,
    "MAIL_BACKEND": None,
    "EMAIL_HOST": None,
    "EMAIL_PORT": None,
    "EMAIL_HOST_USER": None,
    "EMAIL_HOST_PASSWORD": None,
    "EMAIL_USE_TLS": None,
    "EMAIL_USE_SSL": None,
    "MAIL_FROM": None,
    "APP_ORIGIN": None,
    "DJANGO_TEST_ACCOUNTS_ORGANIZATION": None,
    "SANDBOX_INBOUND_ENABLED": None,
    "SECRETS_KEKS": "1:" + base64.urlsafe_b64encode(bytes(range(32))).decode(),  # ADR-022 §6
    "META_ENABLED": None,
    "AI_ENABLED": None,
    "META_APP_SECRETS": None,
    "META_WEBHOOK_VERIFY_TOKEN": None,
    "MFA_ENFORCEMENT": None,
    "META_GRAPH_SIMULATOR_URL": None,
    "CHANNEL_HEALTH_INTERVAL_MINUTES": None,
}
MAIL_SECRET = secrets.token_hex(16)  # aleatorio por ejecución
MAIL_ENV = {
    "EMAIL_HOST": "smtp.example.com",
    "EMAIL_HOST_USER": "apikey",
    "EMAIL_HOST_PASSWORD": MAIL_SECRET,
    "APP_ORIGIN": "https://app.example.com",
    "EMAIL_USE_TLS": "true",
    "MAIL_FROM": "no-reply@example.com",
}


@pytest.fixture(autouse=True)
def forget_loaded_settings() -> Iterator[None]:
    """Cada carga deja `base` en `sys.modules` con las variables de su caso (el canal de Meta
    encendido, por ejemplo): quien importe `local` o `test` después heredaría ese estado."""
    yield
    for name in ("production", "local", "migrate", "test", "base"):
        sys.modules.pop(f"config.settings.{name}", None)


def load_production(monkeypatch: pytest.MonkeyPatch, **overrides: str | None) -> object:
    for key, value in {**BASE_ENV, **overrides}.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)
    for name in ("config.settings.production", "config.settings.base"):
        sys.modules.pop(name, None)
    return importlib.import_module("config.settings.production")


def test_production_loads_with_valid_env(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = load_production(monkeypatch)
    assert settings.DEBUG is False  # type: ignore[attr-defined]
    assert settings.SECURE_SSL_REDIRECT is True  # type: ignore[attr-defined]
    assert settings.DATABASES["default"]["HOST"] == "db"  # type: ignore[attr-defined]
    # ADR-014 §2: la cookie de sesión de producción no se relaja.
    assert settings.SESSION_COOKIE_NAME == "__Host-crm_session"  # type: ignore[attr-defined]
    for flag in ("SESSION_COOKIE_SECURE", "SESSION_COOKIE_HTTPONLY", "CSRF_COOKIE_SECURE"):
        assert getattr(settings, flag) is True
    assert settings.SESSION_COOKIE_SAMESITE == settings.CSRF_COOKIE_SAMESITE == "Lax"  # type: ignore[attr-defined]
    # `__Host-` exige Path=/ y ningún Domain; el cliente tiene que poder leer `csrftoken`.
    assert getattr(settings, "SESSION_COOKIE_PATH", "/") == "/"
    assert getattr(settings, "SESSION_COOKIE_DOMAIN", None) is None
    assert settings.CSRF_COOKIE_HTTPONLY is False  # type: ignore[attr-defined]
    assert getattr(settings, "CSRF_COOKIE_DOMAIN", None) is None
    # D-F2-2: sesiones en base de datos (revocar es borrar una fila) y 12 horas.
    assert settings.SESSION_ENGINE.endswith(".db")  # type: ignore[attr-defined]
    assert settings.SESSION_COOKIE_AGE == 12 * 60 * 60  # type: ignore[attr-defined]
    # F2-03D: el proxy de confianza es explícito; una red también vale.
    assert settings.FORWARDED_ALLOW_IPS == ["10.0.0.2"]  # type: ignore[attr-defined]
    networks = load_production(monkeypatch, FORWARDED_ALLOW_IPS="10.0.0.0/8, fd00::2, fd00::/16")
    assert len(networks.FORWARDED_ALLOW_IPS) == 3  # type: ignore[attr-defined]
    with pytest.raises(ImproperlyConfigured, match="FORWARDED_ALLOW_IPS"):
        load_production(monkeypatch, FORWARDED_ALLOW_IPS=None)


@pytest.mark.parametrize(
    "overrides",
    [
        {"DJANGO_SECRET_KEY": None},
        {"DJANGO_SECRET_KEY": "django-insecure-" + "x" * 60},
        {"DJANGO_SECRET_KEY": "too-short"},
        {"DJANGO_ALLOWED_HOSTS": None},
        {"DJANGO_ALLOWED_HOSTS": ""},
        {"DJANGO_ALLOWED_HOSTS": " , "},
        {"DATABASE_URL": None},
        {"DATABASE_URL": "mysql://u:p@h/db"},
        {"DJANGO_DEBUG": "true"},
        {"DJANGO_DEBUG": "maybe"},
        {"FORWARDED_ALLOW_IPS": None},  # sin proxy declarado, todos los clientes son el proxy
        {"FORWARDED_ALLOW_IPS": " , "},
        {"FORWARDED_ALLOW_IPS": "*"},  # cualquiera elegiría su dirección
        {"FORWARDED_ALLOW_IPS": "10.0.0.2, *"},
        {"FORWARDED_ALLOW_IPS": "*, 10.0.0.2"},
        {"FORWARDED_ALLOW_IPS": "0.0.0.0/0"},  # lo mismo que `*`
        {"FORWARDED_ALLOW_IPS": "0.0.0.0/1,128.0.0.0/1"},
        {"FORWARDED_ALLOW_IPS": "10.0.0.2, ::/0"},
        {"FORWARDED_ALLOW_IPS": "proxy.internal"},  # uvicorn no confiaría en nadie, sin avisar
        {"FORWARDED_ALLOW_IPS": "10.0.0.2/24"},  # bits de host: uvicorn tampoco la entiende
        {"FORWARDED_ALLOW_IPS": "10.0.0.2 10.0.0.3"},
        {"FORWARDED_ALLOW_IPS": '"10.0.0.2"'},
        {"FORWARDED_ALLOW_IPS": "10.0.0.0/7"},  # más ancha que /8
        {"FORWARDED_ALLOW_IPS": "fc00::/15"},  # más ancha que /16
        {"FORWARDED_ALLOW_IPS": "::ffff:10.0.0.2"},  # nunca casaría con el proxy IPv4
        {"UVICORN_FORWARDED_ALLOW_IPS": "10.0.0.2"},  # uvicorn la prefiere a la validada
    ],
)
def test_production_refuses_insecure_or_missing_config(
    monkeypatch: pytest.MonkeyPatch, overrides: dict[str, str | None]
) -> None:
    with pytest.raises(ImproperlyConfigured):
        load_production(monkeypatch, **overrides)


def test_production_adds_loopback_after_validating_configured_hosts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = load_production(monkeypatch, DJANGO_ALLOWED_HOSTS="app.example.com,localhost")
    hosts = settings.ALLOWED_HOSTS  # type: ignore[attr-defined]
    assert hosts[0] == "app.example.com"
    assert {"127.0.0.1", "localhost", "[::1]"} <= set(hosts)
    assert len(hosts) == len(set(hosts))  # sin duplicados


def test_production_refuses_test_accounts(monkeypatch: pytest.MonkeyPatch) -> None:
    """ADR-026 §1: con accesos de prueba, production no arranca."""
    with pytest.raises(ImproperlyConfigured, match="DJANGO_TEST_ACCOUNTS_ORGANIZATION"):
        load_production(monkeypatch, DJANGO_TEST_ACCOUNTS_ORGANIZATION="pruebas")
    disabled = load_production(monkeypatch, DJANGO_TEST_ACCOUNTS_ORGANIZATION=None)
    assert disabled.TEST_ACCOUNTS_ORGANIZATION == ""  # type: ignore[attr-defined]


@pytest.mark.parametrize("value", ["true", "1", "maybe"])
def test_production_refuses_the_sandbox_inbound_and_has_it_off(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """F1-14: la entrada Sandbox inyecta mensajes falsos; production no arranca con ella."""
    with pytest.raises(ImproperlyConfigured):
        load_production(monkeypatch, SANDBOX_INBOUND_ENABLED=value)
    for off in (None, "false"):
        settings = load_production(monkeypatch, SANDBOX_INBOUND_ENABLED=off)
        assert settings.SANDBOX_INBOUND_ENABLED is False  # type: ignore[attr-defined]
    assert load_local(monkeypatch).SANDBOX_INBOUND_ENABLED is True
    assert load_local(monkeypatch, SANDBOX_INBOUND_ENABLED="false").SANDBOX_INBOUND_ENABLED is False


@pytest.mark.parametrize(("value", "on"), [(None, False), ("false", False), ("true", True)])
def test_ai_is_off_unless_turned_on(monkeypatch: Any, value: Any, on: bool) -> None:
    """F8-06 (ADR-034 §5): apagado por omisión; un valor que no es booleano no arranca."""
    assert load_production(monkeypatch, AI_ENABLED=value).AI_ENABLED is on  # type: ignore[attr-defined]
    with pytest.raises(ImproperlyConfigured):
        load_production(monkeypatch, AI_ENABLED="ture")


@pytest.mark.parametrize("value", ["strict", "Enforce", "", "off"])
def test_mfa_enforcement_is_warn_by_default_and_anything_else_than_its_two_values_stops_it(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """ADR-023 §9 y ADR-038: un valor que no es `warn` ni `enforce` no arranca ningún proceso."""
    assert load_production(monkeypatch).MFA_ENFORCEMENT == "warn"  # type: ignore[attr-defined]
    on = load_production(monkeypatch, MFA_ENFORCEMENT="enforce")
    assert on.MFA_ENFORCEMENT == "enforce"  # type: ignore[attr-defined]
    with pytest.raises(ImproperlyConfigured, match="MFA_ENFORCEMENT"):
        load_production(monkeypatch, MFA_ENFORCEMENT=value)
    with pytest.raises(ImproperlyConfigured, match="MFA_ENFORCEMENT"):
        load_local(monkeypatch, MFA_ENFORCEMENT=value)


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("DATABASE_MIGRATOR_URL", f"postgres://crm_migrator:{MIGRATOR_SECRET}@db:5432/crm"),
        ("CRM_MIGRATOR_PASSWORD", MIGRATOR_SECRET),
    ],
)
def test_production_refuses_migrator_credentials(
    monkeypatch: pytest.MonkeyPatch, variable: str, value: str
) -> None:
    with pytest.raises(ImproperlyConfigured) as excinfo:
        load_production(monkeypatch, **{variable: value})
    message = str(excinfo.value)
    assert variable in message
    assert MIGRATOR_SECRET not in message


@pytest.mark.parametrize(
    "endpoint", ["http://s.example.com", "https://u:" + "p" + "@s.example.com"]
)
def test_production_storage_endpoint_requires_tls(
    monkeypatch: pytest.MonkeyPatch, endpoint: str
) -> None:
    for valid in ("https://storage.example.com", ""):  # vacío = AWS por defecto
        load_production(monkeypatch, STORAGE_ENDPOINT_URL=valid)
    with pytest.raises(ImproperlyConfigured):
        load_production(monkeypatch, STORAGE_ENDPOINT_URL=endpoint)


@pytest.mark.parametrize(
    ("public", "changes"),
    [
        ("http://media.example.com", {}),  # sin TLS
        ("https://app.example.com", {}),  # el host de la aplicación (ALLOWED_HOSTS)
        ("https://APP.example.com.", {}),  # mayúsculas y punto final
        ("https://media.example.com", {"DJANGO_ALLOWED_HOSTS": "MEDIA.example.com"}),
        ("https://media.example.com", {"DJANGO_ALLOWED_HOSTS": ".example.com"}),
        ("https://media.example.com", {"DJANGO_ALLOWED_HOSTS": "*"}),  # con `*`, ninguno
        ("https://crm.example.org", {"APP_ORIGIN": "https://crm.example.org"}),
        ("https://crm.example.org:443", {"APP_ORIGIN": "https://crm.example.org"}),
        ("https://crm.example.org:8443", {"APP_ORIGIN": "https://crm.example.org"}),  # puerto
        ("https://crm.example.org", {"APP_ORIGIN": "https://CRM.example.org"}),
        ("https://crm.example.org", {"APP_ORIGIN": "https://crm.example.org."}),
        ("https://u:" + "p" + "@media.example.com", {}),  # credenciales
        ("https://media.example.com/bucket", {}),  # no es un origen
        ("", {"STORAGE_ENDPOINT_URL": "https://app.example.com"}),  # vacío: firma el interno
    ],
)
def test_production_signs_browser_urls_only_for_another_https_host(
    monkeypatch: pytest.MonkeyPatch, public: str, changes: dict[str, str]
) -> None:
    for valid in ("https://media.example.com", ""):  # vacío = el endpoint interno (o AWS)
        loaded = load_production(monkeypatch, STORAGE_PUBLIC_ENDPOINT_URL=valid)
        assert loaded.STORAGE_PUBLIC_ENDPOINT_URL == valid  # type: ignore[attr-defined]
    with pytest.raises(ImproperlyConfigured):
        load_production(monkeypatch, STORAGE_PUBLIC_ENDPOINT_URL=public, **changes)


def test_production_mail_is_optional_but_never_half_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR-019 §6: sin servidor arranca (enviar fallará al intentarlo); con servidor, completo."""
    bare = load_production(monkeypatch)
    assert (bare.EMAIL_HOST, bare.MAIL_BACKEND, bare.EMAIL_TIMEOUT) == ("", "smtp", 10)  # type: ignore[attr-defined]
    full = load_production(monkeypatch, **MAIL_ENV, EMAIL_PORT="2525")
    # Lo que no pasa por `core.mail` (el aviso a ADMINS de Django) no envía nada, ni con servidor.
    dummy = "django.core.mail.backends.dummy.EmailBackend"
    assert bare.EMAIL_BACKEND == dummy  # type: ignore[attr-defined]
    assert full.EMAIL_BACKEND == dummy  # type: ignore[attr-defined]
    assert (full.EMAIL_HOST, full.EMAIL_PORT) == ("smtp.example.com", 2525)  # type: ignore[attr-defined]
    assert (full.EMAIL_USE_TLS, full.EMAIL_USE_SSL) == (True, False)  # type: ignore[attr-defined]
    assert full.DEFAULT_FROM_EMAIL == "no-reply@example.com"  # type: ignore[attr-defined]
    implicit = {**MAIL_ENV, "EMAIL_USE_TLS": "false", "EMAIL_USE_SSL": "true"}
    assert load_production(monkeypatch, **implicit).EMAIL_PORT == 587  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    "overrides",
    [
        {"EMAIL_USE_TLS": "false"},  # sin cifrado
        {"EMAIL_USE_SSL": "true"},  # los dos a la vez
        {"MAIL_FROM": None},
        {"EMAIL_HOST_USER": None},
        {"EMAIL_HOST_PASSWORD": None},
        {"MAIL_BACKEND": "memory"},
        {"EMAIL_PORT": "25a"},
        {"EMAIL_PORT": "-1"},
    ],
)
def test_production_refuses_an_unsafe_mail_server(
    monkeypatch: pytest.MonkeyPatch, overrides: dict[str, str | None]
) -> None:
    with pytest.raises(ImproperlyConfigured) as error:
        load_production(monkeypatch, **{**MAIL_ENV, **overrides})
    assert MAIL_SECRET not in str(error.value)  # el error nombra variables, nunca su valor


@pytest.mark.parametrize(
    "value",
    [
        "http://app.example.com",  # un enlace que cambia una contraseña no viaja por http
        None,  # ni se compone sin origen
        "https://app.example.com/",
        "https://app.example.com/crm",
        "https://app.example.com?x=1",
        "https://app.example.com#x",
        f"https://ana:{MAIL_SECRET}@app.example.com",
        "https://app.example.com:99999",
        "https://app.example.com:0",
        "https://",
        "app.example.com",
        "ftp://app.example.com",
        "https://app.example.com:",
        "https://app .example.com",
        "https://app.example.com\\crm",  # un navegador lee la barra invertida como una ruta
        "https://app.example.com\x7f",
        "https://app\u2028.example.com",
        "https://áé.example.com",  # un dominio internacionalizado se escribe en su forma IDNA
        "https://" + "a" * 254,
        "https://[::::]",  # entre corchetes solo cabe una IPv6
    ],
)
def test_production_with_mail_needs_an_https_origin_for_its_links(
    monkeypatch: pytest.MonkeyPatch, value: str | None
) -> None:
    """ADR-021 §8: el origen de los enlaces llega del despliegue, bien formado y por https."""
    with pytest.raises(ImproperlyConfigured, match="APP_ORIGIN") as error:
        load_production(monkeypatch, **{**MAIL_ENV, "APP_ORIGIN": value})
    assert MAIL_SECRET not in str(error.value)  # el error nombra la variable, nunca su valor


def test_the_origin_is_optional_without_mail_and_kept_as_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert load_production(monkeypatch).APP_ORIGIN == ""  # type: ignore[attr-defined]
    local = load_production(monkeypatch, APP_ORIGIN=" http://localhost:8080 ")
    assert local.APP_ORIGIN == "http://localhost:8080"  # type: ignore[attr-defined]
    with pytest.raises(ImproperlyConfigured, match="APP_ORIGIN"):  # solo http o https
        load_production(monkeypatch, APP_ORIGIN="ftp://localhost:8080")
    for given in ("https://crm.example.pe:8443", "https://[2001:db8::1]:8443"):
        full = load_production(monkeypatch, **{**MAIL_ENV, "APP_ORIGIN": given})
        assert full.APP_ORIGIN == given  # type: ignore[attr-defined]


def load_migrate(monkeypatch: pytest.MonkeyPatch, **env: str | None) -> object:
    for key, value in env.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)
    for name in ("config.settings.migrate", "config.settings.base"):
        sys.modules.pop(name, None)
    return importlib.import_module("config.settings.migrate")


def test_migrate_settings_use_migrator_url_without_runtime_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = load_migrate(
        monkeypatch,
        DATABASE_URL=None,
        DATABASE_MIGRATOR_URL="postgres://crm_migrator:x@db:5432/crm",
        DJANGO_SECRET_KEY=None,
    )
    assert settings.DATABASES["default"]["USER"] == "crm_migrator"  # type: ignore[attr-defined]


def test_migrate_settings_never_enable_test_accounts(monkeypatch: pytest.MonkeyPatch) -> None:
    """ADR-026 §1: el job de migraciones ignora los accesos de prueba aunque los reciba."""
    settings = load_migrate(
        monkeypatch,
        DATABASE_URL=None,
        DATABASE_MIGRATOR_URL="postgres://crm_migrator:x@db:5432/crm",
        DJANGO_SECRET_KEY=None,
        DJANGO_TEST_ACCOUNTS_ORGANIZATION="pruebas",
    )
    assert settings.TEST_ACCOUNTS_ORGANIZATION == ""  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    "env",
    [
        {
            "DATABASE_URL": "postgres://crm_app:x@db:5432/crm",
            "DATABASE_MIGRATOR_URL": "postgres://m:x@db/crm",
        },
        {"DATABASE_URL": None, "DATABASE_MIGRATOR_URL": None},
    ],
)
def test_migrate_settings_refuse_runtime_url_or_missing_migrator(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str | None]
) -> None:
    with pytest.raises(ImproperlyConfigured):
        load_migrate(monkeypatch, **env)


META_SECRET, META_NEXT = secrets.token_hex(16), secrets.token_hex(16)  # aleatorios por ejecución
META_VERIFY = secrets.token_urlsafe(24)
META_ENV: dict[str, str | None] = {
    "META_ENABLED": "true",
    "META_APP_SECRETS": f"{META_SECRET},{META_NEXT}",
    "META_WEBHOOK_VERIFY_TOKEN": META_VERIFY,
    "CELERY_BROKER_URL": "redis://redis:6379/0",  # el limitador de envíos (F7-05c) cuenta ahí
}


def load_local(monkeypatch: pytest.MonkeyPatch, **env: str | None) -> Any:
    for key, value in {**BASE_ENV, **env}.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)
    for name in ("config.settings.local", "config.settings.base"):
        sys.modules.pop(name, None)
    return importlib.import_module("config.settings.local")


@pytest.mark.parametrize("loader", [load_production, load_local])
def test_the_whatsapp_channel_is_off_by_default_and_then_requires_nothing(
    monkeypatch: pytest.MonkeyPatch, loader: Callable[..., Any]
) -> None:
    settings = loader(monkeypatch)
    assert settings.META_ENABLED is False and settings.META_APP_SECRETS == []
    assert "graph.facebook.com" not in settings.HTTP_ALLOWED_HOSTS
    assert (
        settings.HTTP_ALLOWED_HOSTS.count("api.anthropic.com") == 1
    )  # F8-03b: el host fijo de la IA
    stray = loader(monkeypatch, META_APP_SECRETS="x", META_ENABLED="false")
    assert stray.META_ENABLED is False  # un valor suelto con el canal apagado no se valida


@pytest.mark.parametrize("loader", [load_production, load_local])
def test_the_whatsapp_channel_on_loads_with_valid_settings_and_allows_the_graph_host(
    monkeypatch: pytest.MonkeyPatch, loader: Callable[..., Any]
) -> None:
    settings = loader(
        monkeypatch, **META_ENV, HTTP_ALLOWED_HOSTS="graph.facebook.com,api.example.com"
    )
    assert settings.META_APP_SECRETS == [META_SECRET, META_NEXT]
    assert settings.META_GRAPH_VERSION == "v23.0"
    assert settings.HTTP_ALLOWED_HOSTS.count("graph.facebook.com") == 1
    added = loader(monkeypatch, **META_ENV, HTTP_ALLOWED_HOSTS="api.example.com")
    assert added.HTTP_ALLOWED_HOSTS == [
        "api.example.com",
        "graph.facebook.com",
        "api.anthropic.com",
    ]


BAD_META: dict[str, dict[str, str | None]] = {
    "no secrets": {"META_APP_SECRETS": None},
    "short secret": {"META_APP_SECRETS": "corto"},
    "symbols": {"META_APP_SECRETS": "a" * 15 + "-" + "b" * 16},
    "duplicates": {"META_APP_SECRETS": f"{META_SECRET},{META_SECRET}"},
    "four secrets": {"META_APP_SECRETS": ",".join(secrets.token_hex(16) for _ in range(4))},
    "no verify token": {"META_WEBHOOK_VERIFY_TOKEN": None},
    "short verify token": {"META_WEBHOOK_VERIFY_TOKEN": "corto"},
    "verify token with spaces": {"META_WEBHOOK_VERIFY_TOKEN": "con espacios " * 3},
    "verify token is a secret": {"META_WEBHOOK_VERIFY_TOKEN": META_SECRET},
}


@pytest.mark.parametrize("loader", [load_production, load_local])
@pytest.mark.parametrize("name", sorted(BAD_META))
def test_the_whatsapp_channel_on_refuses_to_start_with_bad_settings_and_names_no_value(
    monkeypatch: pytest.MonkeyPatch, loader: Callable[..., Any], name: str
) -> None:
    with pytest.raises(ImproperlyConfigured) as raised:
        loader(monkeypatch, **META_ENV | BAD_META[name])
    message = str(raised.value)
    assert "META_" in message and META_SECRET not in message and META_VERIFY not in message


def test_the_migrations_job_never_needs_the_whatsapp_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = load_migrate(
        monkeypatch,
        DATABASE_URL=None,
        DATABASE_MIGRATOR_URL="postgres://crm_migrator:x@db:5432/crm",
        DJANGO_SECRET_KEY=None,
        META_ENABLED="true",
        META_APP_SECRETS=None,
        META_WEBHOOK_VERIFY_TOKEN=None,
    )
    assert settings.DATABASES["default"]["USER"] == "crm_migrator"  # type: ignore[attr-defined]


@pytest.mark.parametrize("value", ["0", "00", "-1"])
def test_the_channel_health_interval_is_at_least_one_minute_or_nothing_starts(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """F7b-06j: con 0, «página suscrita» no valdría nunca y cada pasada lo comprobaría todo."""
    assert load_production(monkeypatch).CHANNEL_HEALTH_INTERVAL_MINUTES == 15  # type: ignore[attr-defined]
    one = load_local(monkeypatch, CHANNEL_HEALTH_INTERVAL_MINUTES="1")
    assert one.CHANNEL_HEALTH_INTERVAL_MINUTES == 1
    with pytest.raises(ImproperlyConfigured, match="CHANNEL_HEALTH_INTERVAL_MINUTES"):
        load_production(monkeypatch, CHANNEL_HEALTH_INTERVAL_MINUTES=value)
    with pytest.raises(ImproperlyConfigured, match="CHANNEL_HEALTH_INTERVAL_MINUTES"):
        load_local(monkeypatch, CHANNEL_HEALTH_INTERVAL_MINUTES=value)


def test_the_channel_limit_window_is_shorter_than_what_the_login_purge_keeps() -> None:
    """La purga de los contadores de acceso borra lo que lleva más de su ventana de calma sin
    moverse; los contadores `chn:` (F7-02e) viven en esa tabla y su ventana tiene que caber."""
    from config.settings import base

    kept = max(window for _, window, _, _ in base.LOGIN_THROTTLE.values())
    assert base.CHANNEL_CHECK_THROTTLE[1] < kept and base.MEDIA_UPLOAD_THROTTLE[1] < kept
    assert base.AI_ACCOUNT_THROTTLE[1] < kept  # los contadores `aik:` (F8-03d)
    assert base.AI_COPILOT_THROTTLE[1] < kept  # y los `aic:` (F8-08h)
    assert base.AI_COPILOT_ORG_THROTTLE[1] < kept  # y los `aio:` (F8-08m)
    assert base.AI_EXCLUSION_THROTTLE[1] < kept  # y los `aix:` (F8-13c)


@pytest.mark.parametrize("throttle", [(0, 600), (61, 600), (10, 59), (10, 841), (10, 3600)])
def test_production_refuses_an_ai_account_limit_it_cannot_enforce(
    throttle: tuple[int, int],
) -> None:
    """La prueba de conexión de IA no se repite en bucle contra el proveedor (F8-03d)."""
    from config import env
    from config.settings import base

    env.check_ai_throttle(base.AI_ACCOUNT_THROTTLE)
    with pytest.raises(ImproperlyConfigured, match="AI_ACCOUNT_THROTTLE"):
        env.check_ai_throttle(throttle)


@pytest.mark.parametrize("throttle", [(0, 600), (61, 600), (20, 59), (20, 841), (20, 3600)])
def test_production_refuses_a_copilot_limit_it_cannot_enforce(
    monkeypatch: pytest.MonkeyPatch, throttle: tuple[int, int]
) -> None:
    """Una persona no gasta sola el presupuesto de la organización (F8-08h)."""
    from config import env
    from config.settings import base

    checked: list[tuple[int, int]] = []
    monkeypatch.setattr(env, "check_ai_copilot_throttle", checked.append)
    load_production(monkeypatch)
    assert checked == [base.AI_COPILOT_THROTTLE]  # production lo comprueba al arrancar
    monkeypatch.undo()
    env.check_ai_copilot_throttle(base.AI_COPILOT_THROTTLE)
    with pytest.raises(ImproperlyConfigured, match="AI_COPILOT_THROTTLE"):
        env.check_ai_copilot_throttle(throttle)


@pytest.mark.parametrize("throttle", [(0, 600), (19, 600), (601, 600), (120, 59), (120, 841)])
def test_production_refuses_an_organization_copilot_limit_it_cannot_enforce(
    monkeypatch: pytest.MonkeyPatch, throttle: tuple[int, int]
) -> None:
    """Una organización no agota el servidor ni el proveedor (F8-08m); nunca por debajo del tope
    de una persona."""
    from config import env
    from config.settings import base

    checked: list[tuple[tuple[int, int], tuple[int, int]]] = []
    monkeypatch.setattr(env, "check_ai_copilot_org_throttle", lambda *rules: checked.append(rules))
    load_production(monkeypatch)
    assert checked == [(base.AI_COPILOT_ORG_THROTTLE, base.AI_COPILOT_THROTTLE)]  # al arrancar
    monkeypatch.undo()
    env.check_ai_copilot_org_throttle(base.AI_COPILOT_ORG_THROTTLE, base.AI_COPILOT_THROTTLE)
    env.check_ai_copilot_org_throttle((20, 600), (20, 600))  # el mismo que el de una persona
    with pytest.raises(ImproperlyConfigured, match="AI_COPILOT_ORG_THROTTLE"):
        env.check_ai_copilot_org_throttle(throttle, base.AI_COPILOT_THROTTLE)


@pytest.mark.parametrize(
    ("throttle", "seconds"),
    [((0, 600), 3), ((61, 600), 3), ((30, 59), 3), ((30, 841), 3), ((30, 600), 0.0004),
     ((30, 600), 0), ((30, 600), 0.09), ((30, 600), 10.5), ((30, 600), float("nan")),
     ((30, 600), True), ((30, 600), "3"), ((30, 600), None), ((True, 600), 3), ((1.5, 600), 3),
     ((30, 600.0), 3), ([30, 600], 3), ((30,), 3), ((30, 600, 1), 3), (None, 3), ("30/600", 3)],
)  # fmt: skip
def test_production_refuses_an_exclusion_limit_or_a_send_check_it_cannot_enforce(
    monkeypatch: pytest.MonkeyPatch, throttle: tuple[int, int], seconds: Any
) -> None:
    """F8-13e: el tope de excluir conversaciones y el plazo de la comprobación de antes de cada
    envío (`0` o menos de 1 ms sería «sin tope») se comprueban al arrancar en production."""
    from config import env
    from config.settings import base

    checked: list[Any] = []
    monkeypatch.setattr(env, "check_ai_exclusion_throttle", checked.append)
    monkeypatch.setattr(env, "check_ai_send_check_seconds", checked.append)
    load_production(monkeypatch)
    assert checked == [base.AI_EXCLUSION_THROTTLE, base.AI_SEND_CHECK_SECONDS]
    monkeypatch.undo()
    for limit, window, good in ((1, 60, 0.1), (60, 840, 10), (30, 600, 3), (30, 600, 0.5)):
        env.check_ai_exclusion_throttle((limit, window))
        env.check_ai_send_check_seconds(good)
    good = type(throttle) is tuple and throttle == (30, 600) and type(throttle[1]) is int
    name = "AI_SEND_CHECK_SECONDS" if good else "AI_EXCLUSION_THROTTLE"
    with pytest.raises(ImproperlyConfigured, match=f"^{name}:"):
        env.check_ai_exclusion_throttle(throttle)
        env.check_ai_send_check_seconds(seconds)


@pytest.mark.parametrize("throttle", [(0, 600), (61, 600), (5, 59), (5, 841), (5, 3600)])
def test_the_whatsapp_channel_on_refuses_a_limit_it_cannot_enforce(
    throttle: tuple[int, int],
) -> None:
    """Encender el canal exige un tope de peticiones razonable (F7-02e)."""
    from config import env

    good = (META_SECRET, META_VERIFY, "v23.0")
    env.check_meta([good[0]], good[1], good[2], (5, 600))
    with pytest.raises(ImproperlyConfigured, match="CHANNEL_CHECK_THROTTLE"):
        env.check_meta([good[0]], good[1], good[2], throttle)


@pytest.mark.parametrize(("send", "broker"), [((0, 1), "r"), ((61, 1), "r"), ((20, 0), "r"),
                                              ((20, 61), "r"), ((20, 1), "")])  # fmt: skip
def test_the_whatsapp_channel_on_needs_a_send_limit_and_a_broker_for_it(
    send: tuple[int, int], broker: str
) -> None:
    from config import env

    with pytest.raises(ImproperlyConfigured, match="CHANNEL_SEND_LIMIT"):
        env.check_meta([META_SECRET], META_VERIFY, "v23.0", (5, 600), send, broker)


SIMULATOR = "http://meta-sim:8090"
MIGRATE_ENV: dict[str, str | None] = {
    "DATABASE_URL": None, "DATABASE_MIGRATOR_URL": "postgres://crm_migrator:x@db:5432/crm",
    "DJANGO_SECRET_KEY": None,
}  # fmt: skip


def load_test(monkeypatch: pytest.MonkeyPatch, **env: str | None) -> Any:
    for key, value in env.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)
    for name in ("config.settings.test", "config.settings.base"):
        sys.modules.pop(name, None)
    return importlib.import_module("config.settings.test")


@pytest.mark.parametrize("value", [SIMULATOR, ""])
@pytest.mark.parametrize(
    "loader",
    [load_production, lambda m, **e: load_migrate(m, **MIGRATE_ENV, **e), load_test],
    ids=["production", "migrate", "test"],
)
def test_only_the_local_settings_start_with_the_graph_simulator(
    monkeypatch: pytest.MonkeyPatch, loader: Callable[..., Any], value: str
) -> None:
    """ADR-041: production, el job de migraciones y las pruebas no arrancan con la variable puesta,
    aunque venga vacía; sin ella, su valor es vacío (`core.http` no la usa)."""
    with pytest.raises(ImproperlyConfigured, match="META_GRAPH_SIMULATOR_URL") as raised:
        loader(monkeypatch, META_GRAPH_SIMULATOR_URL=value)
    assert SIMULATOR not in str(raised.value)
    assert loader(monkeypatch, META_GRAPH_SIMULATOR_URL=None).META_GRAPH_SIMULATOR_URL == ""
    assert load_local(monkeypatch, META_GRAPH_SIMULATOR_URL=value).META_GRAPH_SIMULATOR_URL == value


@pytest.mark.parametrize(
    "value",
    [
        "https://meta-sim:8090", "http://meta-sim", "http://meta-sim:8090/", "http://meta-sim:0",
        "http://meta-sim:8090/v23.0", "http://user:pw@meta-sim:8090", "http://META-SIM:8090",
        "http://meta-sim:8090?x=1", "http://meta-sim:99999", "ftp://meta-sim:8090",
        "http://user@meta-sim:8090", "http://meta-sim:8090#x",
    ],
)  # fmt: skip
def test_the_local_settings_refuse_a_simulator_that_is_not_a_bare_http_origin(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    with pytest.raises(ImproperlyConfigured, match="META_GRAPH_SIMULATOR_URL"):
        load_local(monkeypatch, META_GRAPH_SIMULATOR_URL=value)


def _imports_local(path: Path, root: Path) -> bool:
    """¿El módulo importa `config.settings.local`? Con `ast`: `import`, `from … import` (también
    relativo y entre paréntesis) e `import_module`/`__import__` con el nombre literal."""
    package = ".".join(path.relative_to(root).with_suffix("").parts[:-1])
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            parts = package.split(".")
            base = ".".join(parts[: len(parts) - node.level + 1]) if node.level else ""
            module = ".".join(p for p in (base, node.module or "") if p)
            names = [module, *(f"{module}.{alias.name}" for alias in node.names)]
        elif isinstance(node, ast.Call) and _called(node) in ("import_module", "__import__"):
            names = [str(a.value) for a in node.args[:1] if isinstance(a, ast.Constant)]
        else:
            continue
        if any(n == LOCAL or n.startswith(f"{LOCAL}.") for n in names):
            return True
    return False


LOCAL = "config.settings.local"


def _called(node: ast.Call) -> str:
    return str(getattr(node.func, "attr", None) or getattr(node.func, "id", ""))


@pytest.mark.parametrize(
    ("where", "code"),
    [
        ("config/settings/x.py", "from .local import *"),
        ("config/settings/x.py", "from . import local"),
        ("config/x.py", "from .settings import local"),
        ("apps/x/y.py", "from config.settings import (\n    base,\n    local,\n)"),
        ("core/x.py", "import config.settings.local as s"),
        ("core/x.py", "importlib.import_module('config.settings.local')"),
        ("core/x.py", "__import__('config.settings.local')"),
    ],
)
def test_the_import_check_sees_every_form(tmp_path: Path, where: str, code: str) -> None:
    path = tmp_path / where
    path.parent.mkdir(parents=True)
    path.write_text(code + "\n")
    assert _imports_local(path, tmp_path)
    path.write_text(code.replace("local", "base") + "\n")
    assert not _imports_local(path, tmp_path)


def test_only_the_local_settings_read_the_simulator_and_nothing_imports_them() -> None:
    """ADR-041: en `config/settings/` solo `base` (vacío, sin leer el entorno) y `local` nombran
    la variable; `env.simulator` solo en `local`; el código de la aplicación nunca importa
    `config.settings.local` (solo por su nombre, en `DJANGO_SETTINGS_MODULE`)."""
    root = Path(__file__).resolve().parents[1]
    base_lines = [
        line.strip() for line in (root / "config/settings/base.py").read_text().splitlines()
        if "META_GRAPH_SIMULATOR_URL" in line and not line.strip().startswith("#")
    ]  # fmt: skip
    assert base_lines == ['META_GRAPH_SIMULATOR_URL = ""']
    named = sorted(
        path.name for path in (root / "config/settings").glob("*.py")
        if "META_GRAPH_SIMULATOR_URL" in path.read_text()
    )  # fmt: skip
    assert named == ["base.py", "local.py"]
    sources = [
        path for folder in ("config", "core", "apps") for path in (root / folder).rglob("*.py")
    ]
    assert [str(p.relative_to(root)) for p in sources if "env.simulator(" in p.read_text()] == [
        "config/settings/local.py"
    ]
    assert [str(p.relative_to(root)) for p in sources if _imports_local(p, root)] == []

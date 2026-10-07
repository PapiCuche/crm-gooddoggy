"""Settings comunes. Cada entorno (local, test, production) los importa y ajusta."""

from pathlib import Path

from celery.schedules import crontab

from config import env

BASE_DIR = Path(__file__).resolve().parents[2]

SECRET_KEY = env.required("DJANGO_SECRET_KEY")
DEBUG = False
ALLOWED_HOSTS: list[str] = env.csv_list("DJANGO_ALLOWED_HOSTS")

INSTALLED_APPS = [
    "django.contrib.contenttypes",  # lo exige django.contrib.auth
    "django.contrib.auth",
    "django.contrib.sessions",
    "rest_framework",
    "drf_spectacular",
    "core",
    "apps.organizations",
    "apps.accounts",
    "apps.access",
    "apps.audit",
    "apps.files",
    "apps.provisioning",
    "apps.members",
]

MIDDLEWARE = [
    "core.observability.middleware.RequestContextMiddleware",  # request/correlation id (F1-07)
    "core.api.middleware.ApiEnvelopeMiddleware",  # errores y CSP de /api/ (ADR-014): por fuera
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",  # request.user (F2-01)
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "apps.accounts.middleware.SessionLifetimeMiddleware",  # caducidad absoluta y por inactividad
    "core.api.middleware.ApiCsrfMiddleware",  # CSRF de /api/ antes de resolver el tenant
    "core.tenancy.middleware.TenantResolutionMiddleware",  # siempre tras la autenticación
]

ROOT_URLCONF = "config.urls"
# El backend solo sirve la API y las sondas: una ruta sin su barra final es un 404 del
# contrato, nunca una redirección en HTML.
APPEND_SLASH = False
# Identidad (F2-01): usuario global con email canónico y Argon2id (ADR-003 §2). Un usuario
# autenticado no accede a ningún tenant sin membresía (TENANCY_MEMBERSHIP_RESOLVER).
AUTH_USER_MODEL = "accounts.User"
PASSWORD_HASHERS = ["django.contrib.auth.hashers.Argon2PasswordHasher"]
_VALIDATORS = "django.contrib.auth.password_validation."
AUTH_PASSWORD_VALIDATORS: list[dict[str, object]] = [
    {"NAME": _VALIDATORS + "UserAttributeSimilarityValidator"},
    {"NAME": _VALIDATORS + "MinimumLengthValidator", "OPTIONS": {"min_length": 12}},
    {"NAME": _VALIDATORS + "CommonPasswordValidator"},
    {"NAME": _VALIDATORS + "NumericPasswordValidator"},
]
# Sesión (ADR-003 §2, D-F2-2): filas en `django_session`, cookie HttpOnly y SameSite=Lax. El
# nombre con prefijo `__Host-` y `Secure` los pone production.py: sobre HTTP no son válidos.
SESSION_ENGINE = "django.contrib.sessions.backends.db"
SESSION_COOKIE_NAME = "crm_session"
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_AGE = 12 * 60 * 60  # inactividad
SESSION_ABSOLUTE_AGE = 7 * 24 * 60 * 60  # desde el inicio de sesión, haya o no actividad
SESSION_REFRESH_INTERVAL = 5 * 60  # cada cuánto se renueva la caducidad por inactividad
CSRF_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_HTTPONLY = False  # legible por JS: el cliente la copia en X-CSRFToken (ADR-003 §3)
# Límite de intentos de acceso (F2-03B): por contador, (intentos, ventana de calma, primer
# bloqueo, bloqueo máximo), en segundos. Lo valida `apps.accounts.checks`. Valores
# conservadores, pendientes de confirmación del PO (D-F2-8).
LOGIN_THROTTLE = {
    "identifier": (20, 15 * 60, 60, 60 * 60),  # la cuenta: no rechaza, endurece `pair` (D-F2-9)
    "pair": (5, 15 * 60, 60, 15 * 60),  # una cuenta desde una dirección
    "ip": (30, 15 * 60, 5 * 60, 60 * 60),  # muchas cuentas desde una dirección
}
ASGI_APPLICATION = "config.asgi.application"

DATABASES = {"default": env.database(env.required("DATABASE_URL"))}
# ADR-002: rol de runtime (sin BYPASSRLS, no propietario). Recibe los GRANT de las migraciones.
DB_APP_ROLE = env.optional("DB_APP_ROLE", "crm_app")
# Propietario explícito de tablas y funciones SECURITY DEFINER (ADR-002 §3.3).
DB_MIGRATOR_ROLE = env.optional("DB_MIGRATOR_ROLE", "crm_migrator")
# Verificación del rol conectado en cada conexión nueva (activa en production).
ENFORCE_RUNTIME_DB_ROLE = False
# Tenancy (F1-04): inyección para que core no importe módulos superiores.
TENANCY_ORGANIZATION_SELECTOR = "apps.organizations.selectors.organization_by_slug"
TENANCY_MEMBERSHIP_RESOLVER = "apps.organizations.selectors.active_membership"  # F2-02
# Celery (F1-10): broker Redis por entorno (compose: redis://redis:6379/0).
CELERY_BROKER_URL = env.optional("CELERY_BROKER_URL", "")
# Outbox (F1-06): el publisher corre cada segundo en beat.
CELERY_BEAT_SCHEDULE = {
    "core.publish_outbox": {"task": "core.publish_outbox", "schedule": 1.0},
    # D-F2-2: una vez al día, a hora fija (UTC). Un intervalo de 24 horas se cuenta desde que
    # beat arranca: si el contenedor se recrea a diario, la purga no se ejecutaría nunca.
    "accounts.purge_expired_sessions": {
        "task": "accounts.purge_expired_sessions",
        "schedule": crontab(hour=3, minute=17),
    },
    "accounts.purge_login_throttles": {  # F2-03B: cada hora, también a minuto fijo
        "task": "accounts.purge_login_throttles",
        "schedule": crontab(minute=7),
    },
}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LANGUAGE_CODE = "es-pe"
LANGUAGES = [("es", "Español"), ("en", "English")]
LOCALE_PATHS = [BASE_DIR / "locale"]
USE_I18N = True
TIME_ZONE = "UTC"  # se guarda en UTC; la zona de cada organización se aplica al mostrar
USE_TZ = True

# ADR-011: JSON en stdout, redactado (core.observability.logging). Celery no reemplaza el root.
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"json": {"()": "core.observability.logging.json_formatter"}},
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "json",
            "stream": "ext://sys.stdout",
        }
    },
    "root": {"handlers": ["console"], "level": env.optional("DJANGO_LOG_LEVEL", "INFO")},
    "loggers": {  # uvicorn configura handlers de texto antes que Django: se reemplazan
        "uvicorn": {"handlers": ["console"], "propagate": False},
        "uvicorn.error": {"handlers": [], "propagate": True},
        # El log de acceso lleva la dirección del cliente y la query string, y el redactor solo
        # tapa patrones de secreto: sin handlers y sin propagar, uvicorn no lo emite (lo decide
        # con `hasHandlers()`). `--no-access-log` solo no bastaba: propagarlo aquí lo volvía a
        # encender. Queda `http.request.completed`.
        "uvicorn.access": {"handlers": [], "propagate": False},
        "celery.app.trace": {"level": "WARNING"},  # "succeeded: <repr(resultado)>"
    },
}
# API (F1-08A): DRF solo JSON. Contrato OpenAPI con drf-spectacular, versionado en
# backend/openapi/schema.yaml (fuente de verdad para orval). Convenciones: ADR-014 (F2-12).
REST_FRAMEWORK = {
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    "DEFAULT_PARSER_CLASSES": ["core.api.parsers.Utf8JSONParser"],  # el charset no elige códec
    "URL_FORMAT_OVERRIDE": None,  # solo JSON: `?format=` no existe
    # Sesión de Django; el CSRF lo exige ApiCsrfMiddleware para todo /api/.
    "DEFAULT_AUTHENTICATION_CLASSES": ["core.api.authentication.SessionAuthentication"],
    "EXCEPTION_HANDLER": "core.api.errors.exception_handler",  # cuerpo de error único
    # F2-05B: denegación por defecto. Las vistas de plataforma declaran las suyas
    # (core.api.permissions).
    "DEFAULT_PERMISSION_CLASSES": ["apps.access.permissions.HasPermission"],
    "DEFAULT_FILTER_BACKENDS": ["apps.access.permissions.ScopeFilter"],
    # ADR-016: las vistas genéricas de lista se paginan por defecto, después de filtrar por
    # tenant y alcance.
    "DEFAULT_PAGINATION_CLASS": "core.api.pagination.CursorPagination",
}
# Copia de `OrganizationMembership.Status` (los módulos de L2 no se importan): un test las compara.
MEMBERSHIP_STATUSES = ["INVITED", "ACTIVE", "SUSPENDED", "DEACTIVATED"]
SPECTACULAR_SETTINGS = {
    "TITLE": "Good Doggy CRM API",
    "DESCRIPTION": "Contrato de la API del backend. Fuente para el cliente TypeScript (orval).",
    "VERSION": "0.1.0",
    "SERVE_INCLUDE_SCHEMA": False,  # el propio /api/schema/ no forma parte del contrato
    "COMPONENT_SPLIT_REQUEST": True,  # tipos separados de petición/respuesta para orval
    "SCHEMA_PATH_PREFIX": r"/api/v[0-9]+",
    "SERVE_PERMISSIONS": ["core.api.permissions.Public"],  # ruta de plataforma (ADR-014 §4)
    # Un nombre por enumeración: sin esto la primera `status` del contrato sería `StatusEnum`.
    "ENUM_NAME_OVERRIDES": {
        "MembershipStatusEnum": MEMBERSHIP_STATUSES,
        "ActiveOrSuspendedEnum": ["ACTIVE", "SUSPENDED"],  # los dos que la API cambia (F2-19)
        "ScopesEnum": "apps.access.catalog.Scope",  # un alcance: `scopes` (F2-11), `scope` (F2-22)
        # `Team.Strategy` (F2-50): otra lista con el mismo nombre de campo no le cambia el nombre.
        "AssignmentStrategyEnum": "apps.organizations.models.Team.Strategy",
        "TeamRoleEnum": "apps.organizations.models.TeamMember.Role",  # F2-55, por lo mismo
        # La auditoría (F2-73): nombres propios, para que otro `result` no se los quede.
        "AuditActorTypeEnum": "apps.audit.models.ACTOR_TYPES",
        "AuditResultEnum": "apps.audit.services.Result",
    },
}
# Object storage S3-compatible (ADR-008): credenciales solo por entorno.
STORAGE_BACKEND = env.optional("STORAGE_BACKEND", "s3")  # s3 | memory (tests)
STORAGE_ENDPOINT_URL = env.optional("STORAGE_ENDPOINT_URL", "")  # vacío = AWS S3
STORAGE_REGION = env.optional("STORAGE_REGION", "us-east-1")
STORAGE_BUCKET = env.optional("STORAGE_BUCKET", "")
STORAGE_ACCESS_KEY_ID = env.optional("STORAGE_ACCESS_KEY_ID", "")
STORAGE_SECRET_ACCESS_KEY = env.optional("STORAGE_SECRET_ACCESS_KEY", "")
STORAGE_ADDRESSING_STYLE = env.optional("STORAGE_ADDRESSING_STYLE", "path")
# HTTP saliente (security-boundaries B9): allowlist exacta de hosts; vacía = nada permitido.
HTTP_ALLOWED_HOSTS: list[str] = env.csv_list("HTTP_ALLOWED_HOSTS")
# Correo saliente (ADR-019): SMTP por entorno, sin proveedor fijado. Solo `core.mail` lo usa.
# Sin EMAIL_HOST no se envía nada: `core.mail.send` falla con un error de configuración.
MAIL_BACKEND = env.optional("MAIL_BACKEND", "smtp")  # smtp | memory (tests)
EMAIL_HOST = env.optional("EMAIL_HOST", "")
EMAIL_PORT = env.integer("EMAIL_PORT", 587)
EMAIL_HOST_USER = env.optional("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = env.optional("EMAIL_HOST_PASSWORD", "")
EMAIL_USE_TLS = env.boolean("EMAIL_USE_TLS", False)  # STARTTLS (587)
EMAIL_USE_SSL = env.boolean("EMAIL_USE_SSL", False)  # TLS implícito (465)
EMAIL_TIMEOUT = 10  # segundos: una tarea no espera indefinidamente al servidor de correo
DEFAULT_FROM_EMAIL = env.optional("MAIL_FROM", "")
# Reporte de errores (ADR-011 §3): sin SENTRY_DSN, NoopReporter (sin red).
SENTRY_DSN = env.optional("SENTRY_DSN", "")
SENTRY_ENVIRONMENT = env.optional("SENTRY_ENVIRONMENT", "")

"""Settings comunes. Cada entorno (local, test, production) los importa y ajusta."""

import re
from pathlib import Path
from typing import Any

from celery.schedules import crontab
from django.core.exceptions import ImproperlyConfigured

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
    "apps.catalog",
    "apps.inventory",
    "apps.imports",
    "apps.contacts",
    "apps.pricing",
    "apps.deals",
    "apps.conversations",
    "apps.tasks",
    "apps.quotes",
    "apps.ai_gateway",  # F8-03a (ADR-034 §2): L1, no importa módulos de negocio
    "apps.ai_agents",  # F8-07a (ADR-034 §2): L4, el copiloto y su Tool Registry
    "apps.automations",  # F11-02 (ADR-049): L4, automatizaciones internas de seguimiento
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
    "apps.conversations.throttle.ChannelThrottleMiddleware",  # F7-02e: tope por persona
    "apps.quotes.api.limit.QuoteFloorLimitMiddleware",  # F5-09e2: el oráculo del costo
    "apps.ai_gateway.throttle.AiAccountThrottleMiddleware",  # F8-03d: tope por persona
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
# Recuperación de contraseña (ADR-021 §2 y §5): intentos admitidos por ventana, en segundos.
# Todos rechazan. Son propios de este flujo: no tocan los contadores del acceso.
PASSWORD_RESET_THROTTLE = {
    "ip": (20, 60 * 60),  # una dirección de red, para cualquier correo
    "identifier": (5, 60 * 60),  # un correo, desde cualquier dirección
    "change": (10, 15 * 60),  # presentar un enlace, desde una dirección de red (ADR-021 §5)
}
# Enlaces de invitación presentados desde una dirección de red (ADR-020 §4, F2-130a): intentos por
# ventana, en segundos. Pendiente de confirmación del PO.
INVITATION_LINK_THROTTLE = (10, 15 * 60)
# Segundo factor (ADR-023 §5): códigos fallidos por cuenta, vengan de donde vengan, y la ventana
# en segundos. Alcanzado el tope, se rechaza también el código correcto.
MFA_CODE_THROTTLE = (10, 24 * 60 * 60)
# Rutas de cuentas de WhatsApp (F7-02e): peticiones de una persona por ventana (segundos); conectar
# y rotar preguntan a Meta en cada una. Pendiente de confirmación del PO.
CHANNEL_CHECK_THROTTLE = (5, 10 * 60)
# Rutas de cuentas del proveedor de IA (F8-03d): escrituras (POST y DELETE: guardar, rotar, borrar
# y probar) de una persona por ventana (segundos); la prueba de conexión llama al proveedor. GET y
# HEAD no cuentan. Confirmado por el propietario el 2026-11-17 (F8-03e, #767).
AI_ACCOUNT_THROTTLE = (10, 10 * 60)
# Pedir una sugerencia al copiloto (F8-08h, ADR-034 §7): peticiones de una persona por ventana
# (segundos), con su propio contador; el presupuesto de la organización sigue siendo el tope duro.
# PENDIENTE DEL PROPIETARIO (#771): el valor propuesto por el programa; se cambia por PR.
AI_COPILOT_THROTTLE = (20, 10 * 60)
# Y por organización (F8-08m, #1047): pedidos de sugerir y resumir de toda la organización por
# ventana (segundos), con su propio contador, contados tras el permiso; que una organización no
# agote el servidor ni el proveedor aunque cada persona esté bajo su tope. PROVISIONAL: el valor
# propuesto por el programa (seis personas a tope), pendiente de confirmación del propietario; se
# cambia por PR. El presupuesto sigue siendo el tope duro.
AI_COPILOT_ORG_THROTTLE = (120, 10 * 60)
# F8-13c: excluir una conversación de la IA (el PUT), por persona y ventana (segundos): puede
# esperar a una llamada en curso. PROVISIONAL: el valor propuesto por el programa; se cambia por PR.
AI_EXCLUSION_THROTTLE = (30, 10 * 60)
# Subidas de medios (F7-09b2): peticiones de una persona por ventana (< su calma). Pendiente del PO.
MEDIA_UPLOAD_THROTTLE = (30, 10 * 60)
# Rutas que miden el suelo de costo de una cotización (F5-09e2): peticiones por persona y
# cotización, y por persona en todas, por ventana (segundos). Pendiente de confirmación del PO.
QUOTE_FLOOR_THROTTLE = {"quote": (6, 60 * 60), "user": (30, 24 * 60 * 60)}
# Mensajes entrantes (F7-04): contactos nuevos por minuto y por cuenta; pasado el tope, el mensaje
# se reintenta (no se pierde) y se alerta. Pendiente de confirmación del PO.
INBOUND_NEW_CONTACTS_PER_MINUTE = 30
ASGI_APPLICATION = "config.asgi.application"

# F1-DB-01: `config/asgi.py` marca su proceso antes de cargar los ajustes (`env.database`).
DATABASES = {
    "default": env.database(
        env.required("DATABASE_URL"), asgi=env.boolean("CRM_ASGI_PROCESS", False)
    )
}
# ADR-002: rol de runtime (sin BYPASSRLS, no propietario). Recibe los GRANT de las migraciones.
DB_APP_ROLE = env.optional("DB_APP_ROLE", "crm_app")
# Propietario explícito de tablas y funciones SECURITY DEFINER (ADR-002 §3.3).
DB_MIGRATOR_ROLE = env.optional("DB_MIGRATOR_ROLE", "crm_migrator")
# Verificación del rol conectado en cada conexión nueva (activa en production).
ENFORCE_RUNTIME_DB_ROLE = False
# Tenancy (F1-04): inyección para que core no importe módulos superiores.
TENANCY_ORGANIZATION_SELECTOR = "apps.organizations.selectors.organization_by_slug"
TENANCY_MEMBERSHIP_RESOLVER = "apps.organizations.selectors.active_membership"  # F2-02
# Segundo factor por membresía (ADR-023 §9, ADR-038): con `enforce`, una membresía que lo exige no
# entra a su organización sin dispositivo confirmado; con `warn` (por defecto) entra y su `me/` lo
# dice. `enforce` espera al comando de operador de ADR-023 §11 en todo entorno con cuentas reales.
MFA_ENFORCEMENT = env.optional("MFA_ENFORCEMENT", "warn")
if MFA_ENFORCEMENT not in ("warn", "enforce"):
    raise ImproperlyConfigured("MFA_ENFORCEMENT debe ser warn o enforce")
TENANCY_SECOND_FACTOR_POLICY = "apps.members.second_factor.membership_second_factor"
# F1-14: la entrada del simulador Sandbox (inyecta mensajes de clientes). Apagada por omisión;
# `local` y `test` la encienden y `production` no arranca con ella.
SANDBOX_INBOUND_ENABLED = env.boolean("SANDBOX_INBOUND_ENABLED", False)
# Celery (F1-10): broker Redis por entorno (compose: redis://redis:6379/0).
CELERY_BROKER_URL = env.optional("CELERY_BROKER_URL", "")
# Capa de canales (F6-07a): Redis; sin URL, el WebSocket del Inbox no abre (1013). Test: memoria.
CHANNEL_LAYER_REDIS_URL = env.optional("CHANNEL_LAYER_REDIS_URL", CELERY_BROKER_URL)
_HOST = {"address": CHANNEL_LAYER_REDIS_URL, "socket_connect_timeout": 5, "socket_timeout": 15}
_REDIS = {"hosts": [_HOST], "expiry": 30, "capacity": 200}
CHANNEL_LAYERS = (
    {"default": {"BACKEND": "channels_redis.core.RedisChannelLayer", "CONFIG": _REDIS}}
    if CHANNEL_LAYER_REDIS_URL
    else {}
)
# WebSocket del Inbox: tope por persona y proceso, trama máxima, inactividad y revisión (segundos).
INBOX_WS_MAX_PER_USER = 8
INBOX_WS_MAX_FRAME = 1024
INBOX_WS_MAX_FRAMES = 30  # tramas del cliente por minuto y conexión
INBOX_WS_IDLE_SECONDS = 90.0
INBOX_WS_RECHECK_SECONDS = 60.0
INBOX_WS_TICK_SECONDS = 5.0
# F6-04b: minutos que un mensaje saliente puede seguir en cola antes de que el barredor lo recoja.
INBOX_SWEEP_AFTER_MINUTES = env.integer("INBOX_SWEEP_AFTER_MINUTES", 5)
# ...y a partir de cuántos el barredor lo da por fallido (un tope a los reintentos sin fin).
INBOX_GIVE_UP_AFTER_MINUTES = env.integer("INBOX_GIVE_UP_AFTER_MINUTES", 120)
# F7-05c: minutos que un mensaje puede seguir `SENDING` (resultado ambiguo o proceso caído) antes
# de quedar `FAILED` con `PROVIDER_UNKNOWN`; Meta no deduplica, así que no se reenvía.
INBOX_SENDING_DEADLINE_MINUTES = env.integer("INBOX_SENDING_DEADLINE_MINUTES", 15)
# F7-06b: cada cuánto se comprueba la salud de cada cuenta de WhatsApp (calidad, nivel, token).
CHANNEL_HEALTH_INTERVAL_MINUTES = env.integer("CHANNEL_HEALTH_INTERVAL_MINUTES", 15)
if CHANNEL_HEALTH_INTERVAL_MINUTES < 1:  # F7b-06j: con 0, «página suscrita» no valdría nunca
    raise ImproperlyConfigured("CHANNEL_HEALTH_INTERVAL_MINUTES debe ser al menos 1")
MEDIA_PER_ORG = 2  # F7-09a1b: descargas de medios a la vez por organización (a confirmar)
# F7-15 (ADR-008): `host:puerto` de `clamd` (ClamAV). Vacío: nada recibe veredicto, ningún medio
# entrante se abre (tras los reintentos queda `ERROR`) y ningún envío de un archivo se guarda
# (F7-15b: 503 `MEDIA_SCAN_UNAVAILABLE`).
CLAMD_ADDRESS = env.optional("CLAMD_ADDRESS", "")
# F7-09a1c: las descargas de medios (hasta 120 s cada una) van en su propia cola, de baja
# concurrencia (`worker-media`), para no ocupar los workers de la salida de mensajes.
CELERY_TASK_ROUTES = {
    "conversations.fetch_media": {"queue": "media"},
    "conversations.scan_media": {"queue": "media"},  # F7-15: el antivirus, tras la descarga
}
# F7-09a1d: Redis vuelve a entregar una tarea reservada y sin confirmar pasado este tiempo, y una
# con `countdown` (o `eta`) espera reservada sin confirmar, con `acks_late` o sin él: ningún
# `countdown` puede llegar a tanto (es global: medios, `MAX_COUNTDOWN` = 1800 s; las demás, 600 s
# como mucho; una prueba lo comprueba). Es el valor por omisión, explícito.
CELERY_BROKER_TRANSPORT_OPTIONS = {"visibility_timeout": 3600}
# Outbox (F1-06): el publisher corre cada segundo en beat.
CELERY_BEAT_SCHEDULE = {
    "core.publish_outbox": {"task": "core.publish_outbox", "schedule": 1.0},
    # F6-04b: cada minuto vuelve a encolar los mensajes del Inbox que llevan demasiado en cola.
    "conversations.sweep_queued": {"task": "conversations.sweep_queued", "schedule": 60.0},
    "conversations.sweep_sending": {"task": "conversations.sweep_sending", "schedule": 60.0},
    # F7-09a1d: cada minuto vuelve a encolar los adjuntos de medios que ninguna tarea atiende.
    "conversations.sweep_media": {
        "task": "conversations.sweep_media",
        "schedule": 60.0,
        "options": {"expires": 60},
    },
    "conversations.healthcheck_accounts": {
        "task": "conversations.healthcheck_accounts",
        "schedule": 300.0,  # F7-06b: cada 5 minutos revisa las cuentas a las que les toca
    },
    # F3-34: cada 5 minutos encola, por organización activa, el vencimiento de sus reservas.
    "inventory.expire_reservations_sweep": {
        "task": "inventory.expire_reservations_sweep",
        "schedule": 300.0,
        "options": {"expires": 300},  # sin workers no se acumulan barridos viejos
    },
    # F5-09c3: cada 5 minutos encola, por organización activa, el vencimiento de sus cotizaciones.
    "quotes.expire_sweep": {
        "task": "quotes.expire_sweep",
        "schedule": 300.0,
        "options": {"expires": 300},
    },
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
    # F2-98 (ADR-022 §5): tras una rotación de clave, vuelve a cifrar con la vigente. A diario:
    # sin rotación no encuentra nada que hacer.
    "accounts.reseal_mfa_secrets": {
        "task": "accounts.reseal_mfa_secrets",
        "schedule": crontab(hour=3, minute=47),
    },
    # F7-03c: recoge lo que la ruta del webhook no pudo avisar, y reintenta lo fallido.
    "conversations.process_webhook_ingress": {
        "task": "conversations.process_webhook_ingress",
        "schedule": 15.0,
        # Si los workers se paran no se acumulan ~5760 tareas al día para despacharse de golpe.
        "options": {"expires": 15},
    },
    # F7-11a: métricas y alertas de la ingesta de Meta, por ventanas de 5 minutos ya cerradas.
    "conversations.meta_alerts": {
        "task": "conversations.meta_alerts",
        "schedule": 60.0,
        "options": {"expires": 60},
    },
    # F7b-12: cada hora, los avisos de caducidad de los tokens de página; una vez al día, además,
    # lee en Meta las caducidades que no se saben (hasta 50 organizaciones y 20 tokens de cada una).
    "conversations.token_expiry": {
        "task": "conversations.token_expiry",
        "schedule": crontab(minute=23),
        "options": {"expires": 3600},
    },
    "conversations.token_expiry_backfill": {
        "task": "conversations.token_expiry",
        "schedule": crontab(hour=5, minute=13),
        "kwargs": {"backfill": True},
        "options": {"expires": 3600},
    },
    # F7-03c: purga a 90 días, el tope de la ingesta (ADR-029 §3).
    "conversations.purge_webhook_ingress": {
        "task": "conversations.purge_webhook_ingress",
        "schedule": crontab(hour=4, minute=17),
    },
    # F7-02b (ADR-022 §5): lo mismo para los tokens de las cuentas de canal, a otra hora.
    "conversations.reseal_credentials": {
        "task": "conversations.reseal_credentials",
        "schedule": crontab(hour=3, minute=57),
    },
    # F8-03a (ADR-022 §5): y las claves de los proveedores de IA, a otra hora.
    "ai_gateway.reseal_provider_keys": {
        "task": "ai_gateway.reseal_provider_keys",
        "schedule": crontab(hour=4, minute=7),
    },
    # F8-08j (D-F8-05): a diario, el texto de los borradores del copiloto de más de 30 días.
    "ai_agents.purge_suggestion_text": {
        "task": "ai_agents.purge_suggestion_text",
        "schedule": crontab(hour=4, minute=37),
    },
    # F8-05b5 (ADR-034 §5): cada 5 minutos cierra como gastadas las reservas de IA huérfanas.
    "ai_gateway.close_orphan_reservations": {
        "task": "ai_gateway.close_orphan_reservations",
        "schedule": 300.0,
        "options": {"expires": 300},
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
        # Su línea DEBUG («TaskPool: Apply») vuelca los argumentos de cada tarea: nunca se emite.
        "celery.pool": {"level": "INFO"},
        # Su línea DEBUG («GET <ruta>?<query>») lleva la query de la URL firmada de un medio.
        "urllib3": {"level": "WARNING"},
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
    "PREPROCESSING_HOOKS": ["apps.conversations.api.urls.hide_sandbox"],  # F1-14
    # Un nombre por enumeración: sin esto la primera `status` del contrato sería `StatusEnum`.
    "ENUM_NAME_OVERRIDES": {
        "MembershipStatusEnum": MEMBERSHIP_STATUSES,
        "PromotionStatusEnum": "apps.pricing.models.PromotionStatus",  # F3-48d
        "ActiveOrSuspendedEnum": ["ACTIVE", "SUSPENDED"],  # los dos que la API cambia (F2-19)
        "ScopesEnum": "apps.access.catalog.Scope",  # un alcance: `scopes` (F2-11), `scope` (F2-22)
        # `Team.Strategy` (F2-50): otra lista con el mismo nombre de campo no le cambia el nombre.
        "AssignmentStrategyEnum": "apps.organizations.models.Team.Strategy",
        "TeamRoleEnum": "apps.organizations.models.TeamMember.Role",  # F2-55, por lo mismo
        # La auditoría (F2-73): nombres propios, para que otro `result` no se los quede.
        "AuditActorTypeEnum": "apps.audit.models.ACTOR_TYPES",
        "ActorTypeEnum": "core.tenancy.context.ActorType",  # quién hizo un movimiento (F3-36b)
        "AuditResultEnum": "apps.audit.services.Result",
        "InvitationStatusEnum": "apps.organizations.models.UserInvitation.Status",  # F2-80
        "MessagingPolicyModeEnum": ["FREE_FORM", "BLOCKED", "HUMAN_AGENT_TAG"],  # F7-05a, F7b-05a
        # F7b-06a: la cuenta de una red social; la lista entera conserva su nombre.
        "SocialPlatformEnum": ["MESSENGER", "INSTAGRAM"],
        "PlatformEnum": "apps.conversations.models.Platform",
        "RevokedEnum": ["REVOKED"],  # el único estado que la API pone a una invitación (F2-82)
        # El catálogo (F3-06, F3-06b): el estado de un producto y el de una variante son listas
        # distintas con valores en común.
        "ConversationStatusEnum": "apps.conversations.models.Status",  # F6-02
        "HumanRequestReasonEnum": "apps.conversations.models.HumanRequest",  # F8-08n
        "AiProviderEnum": "apps.ai_gateway.models.Provider",  # F8-03c
        "AiProviderAccountStatusEnum": "apps.ai_gateway.models.AccountStatus",
        "QuickReplyVariableEnum": "apps.conversations.quick_replies.VARIABLES",  # F6-08a2
        "StageTypeEnum": "apps.deals.models.StageType",  # F5-04: `stage_type`, `status` y `type`
        "TaskTypeEnum": "apps.tasks.models.TaskType",  # F5-08b: `type` de una tarea
        "QuoteStatusEnum": "apps.quotes.models.QuoteStatus",  # F5-09d: `status` de una cotización
        "QuoteDiscountTypeEnum": "apps.quotes.models.DiscountType",  # F5-09d2: el de la línea
        "DiscountTypeEnum": "apps.pricing.models.DiscountType",  # el de las promociones, como antes
        "ProductStatusEnum": "apps.catalog.models.Product.Status",
        "VariantStatusEnum": "apps.catalog.models.ProductVariant.Status",
        "WarehouseTypeEnum": "apps.inventory.models.Warehouse.Type",  # F3-08
        "MovementTypeEnum": "apps.inventory.models.InventoryMovement.Type",  # F3-09
        "ImportStatusEnum": "apps.imports.models.DataImport.Status",  # F3-20b
        "ImportRowActionEnum": "apps.imports.models.DataImportRow.Action",
        "ContactSourceEnum": "apps.contacts.models.Source",  # F4-02
        "ContactDocumentTypeEnum": "apps.contacts.models.DocumentType",
        "TagAppliesToEnum": "apps.contacts.models.TagTarget",  # F4-11
        "PriceListKindEnum": "apps.pricing.models.Kind",  # F3-40b
        "PriceListPaymentConditionEnum": "apps.pricing.models.PaymentCondition",
        "PricePermissionEnum": "apps.pricing.models.READ_PERMISSIONS",
        "PriceListCurrencyEnum": "apps.pricing.price_lists.CURRENCIES",
        "PriceResolutionReasonEnum": "apps.pricing.services.Reason",  # F3-44
        "PipelineKindEnum": "apps.deals.models.Kind",  # F5-02b: el de lectura y el de escritura
        "ChannelAccountStatusEnum": "apps.conversations.models.AccountStatus",  # F7-07a
        "AutomationModeEnum": "apps.automations.models.Mode",  # F11-07: el `mode` de una regla
        "AutomationRuleKeyEnum": "apps.automations.params.KEYS",
        "AutomationRunStatusEnum": "apps.automations.models.RunStatus",  # F11-07b
    },
}
# Object storage S3-compatible (ADR-008): credenciales solo por entorno.
STORAGE_BACKEND = env.optional("STORAGE_BACKEND", "s3")  # s3 | memory (tests)
STORAGE_ENDPOINT_URL = env.optional("STORAGE_ENDPOINT_URL", "")  # vacío = AWS S3
# El origen con el que se firman las URLs que abre el navegador (`presign_*`); vacío = el de arriba.
STORAGE_PUBLIC_ENDPOINT_URL = env.origin("STORAGE_PUBLIC_ENDPOINT_URL")
STORAGE_REGION = env.optional("STORAGE_REGION", "us-east-1")
STORAGE_BUCKET = env.optional("STORAGE_BUCKET", "")
STORAGE_ACCESS_KEY_ID = env.optional("STORAGE_ACCESS_KEY_ID", "")
STORAGE_SECRET_ACCESS_KEY = env.optional("STORAGE_SECRET_ACCESS_KEY", "")
STORAGE_ADDRESSING_STYLE = env.optional("STORAGE_ADDRESSING_STYLE", "path")
# HTTP saliente (security-boundaries B9): allowlist exacta de hosts; vacía = nada permitido.
HTTP_ALLOWED_HOSTS: list[str] = env.csv_list("HTTP_ALLOWED_HOSTS")
# Canal de WhatsApp (ADR-029, Proposed): una app de Meta por despliegue. Apagado por omisión, y
# apagado no exige nada (los despliegues actuales, el stack local y CI no tienen estos valores).
# La validación al arrancar con el canal encendido la hacen `production` y `local`: el job de
# migraciones no recibe estos secretos (ADR-002 §1.1).
META_ENABLED = env.boolean("META_ENABLED", False)
# Los app secrets de Meta: el vigente primero y, durante una rotación, el anterior (ADR-029).
META_APP_SECRETS: list[str] = env.csv_list("META_APP_SECRETS")
META_WEBHOOK_VERIFY_TOKEN = env.optional("META_WEBHOOK_VERIFY_TOKEN", "")
# Versión de la Graph API, fijada en el código (D-F7-08): subirla es un cambio revisado, con los
# fixtures del simulador. A confirmar con la versión vigente de Meta y su fecha de retirada.
META_GRAPH_VERSION = "v23.0"
META_GRAPH_HOST = "graph.facebook.com"
# ADR-041: el origen del simulador local de Graph (`http://meta-sim:8090`). Vacío en todo
# despliegue: solo `local` lo lee del entorno; production, migrate y test no arrancan con ella.
META_GRAPH_SIMULATOR_URL = ""
# F7b-05a (ADR-035 §7, D-F7b-08): Meta aprobó la función Human Agent en la app. Solo entonces una
# persona responde por Instagram o Messenger con la ventana de 24 h cerrada, hasta 168 h después.
# Apagado por omisión: el propietario lo enciende **después** de la aprobación, nunca antes.
META_HUMAN_AGENT_APPROVED = env.boolean("META_HUMAN_AGENT_APPROVED", False)
# F7b-05c (ADR-035 §6): el id de la app de Meta del despliegue, para reconocer nuestros ecos de
# Instagram y Messenger. Vacío por omisión: entonces un eco con `app_id` nunca se toma por nuestro.
META_APP_ID = env.optional("META_APP_ID", "")
# F7-05c: envíos por cuenta y ventana en segundos (muy por debajo del tope de Meta); Redis, el del
# broker. Validado al arrancar con el canal encendido (`env.check_meta`).
CHANNEL_SEND_LIMIT = (20, 1)
# F7-09a1: hosts de los que se bajan medios (a confirmar con Meta). **No** entran en
# `HTTP_ALLOWED_HOSTS`: `request` (con el token de la cuenta) no llega a ellos; solo `download`.
META_MEDIA_HOSTS = ("lookaside.fbsbx.com",)
if META_ENABLED and META_GRAPH_HOST not in HTTP_ALLOWED_HOSTS:
    HTTP_ALLOWED_HOSTS.append(META_GRAPH_HOST)
# F8-03b (ADR-034 §3): el host fijo de cada proveedor de IA; nunca una URL que dé el usuario. Solo
# `apps.ai_gateway` lo llama, con la clave sellada de la organización.
AI_PROVIDER_HOSTS = {"ANTHROPIC": "api.anthropic.com"}
# F8-06 (ADR-034 §5): el interruptor global de IA. Apagado por omisión; un valor que no es booleano
# no arranca. La aplicación no lo cambia (pide reiniciar): con él apagado no se llama a ningún
# modelo ni se prueba ninguna clave, aunque una organización tenga su interruptor encendido.
AI_ENABLED = env.boolean("AI_ENABLED", False)
# F11-02 (ADR-049 §7, E3): el interruptor global de las automatizaciones internas. Apagado por
# omisión; encenderlo en producción es una operación del propietario, no un cambio de variable.
AUTOMATIONS_ENABLED = env.boolean("AUTOMATIONS_ENABLED", False)
HTTP_ALLOWED_HOSTS.extend(h for h in AI_PROVIDER_HOSTS.values() if h not in HTTP_ALLOWED_HOSTS)
# F8-05 (ADR-034 §3, D-F8-03): los modelos permitidos y su precio en USD por millón de tokens, de la
# página oficial de precios de Anthropic (consultada el día `since`). Sin precio no se llama.
# Cambiarlo es un PR. `long`: el tramo de prompt largo (más de `above` tokens), si lo hay.
AI_MODELS: dict[str, dict[str, dict[str, Any]]] = {"ANTHROPIC": {"claude-sonnet-5-5": {
    "since": "2026-11-17", "input": "2", "cache_write_5m": "2.50", "cache_write_1h": "4",
    "cache_read": "0.10", "output": "10",
}}}  # fmt: skip
# Los parámetros por proveedor; `model`: con el que nace una cuenta (cada cuenta elige el suyo de
# `AI_MODELS`, F8-05b6). `seconds`: plazo total.
AI_PROVIDER_PARAMS: dict[str, dict[str, Any]] = {"ANTHROPIC": {
    "model": "claude-sonnet-5-5", "max_tokens": 2048, "effort": "medium", "seconds": 60,
}}  # fmt: skip
# F8-13c: cuánto espera excluir una conversación a que terminen sus llamadas en curso (el plazo de
# una llamada y un margen); si no, 409 `AI_CALL_IN_PROGRESS` y no cambia nada.
AI_EXCLUSION_WAIT_SECONDS = max(p["seconds"] for p in AI_PROVIDER_PARAMS.values()) + 5
# F8-13d: el `statement_timeout` de la comprobación de justo antes de cada envío; agotado, no sale.
AI_SEND_CHECK_SECONDS = 3
AI_TOOL_TOKENS = 1000  # margen de la reserva por los tokens que añade la API al usar tools
# F8-05b7: los tokens visuales de una imagen como mucho (el tramo de alta resolución) y los límites
# de un turno con tools; lo que los pasa ni se reserva (`AI_TURN_INVALID`).
AI_IMAGE_TOKENS = 4784
AI_TURN_LIMITS = {"turns": 100, "blocks": 400, "thinking": 100, "images": 20,
                  "image_chars": 2_000_000, "body_bytes": 8_000_000}  # fmt: skip
# F8-07d (decisión del propietario, #770): «pocas unidades» es de 1 a este número; `get_stock`
# falla cerrado si no es un entero >= 1.
AI_STOCK_FEW_UNITS = 3
# F8-08d (ADR-034 §7): cuántos mensajes recientes visibles para el cliente lee el copiloto; un
# valor que no sea un entero de 1 a 50 no arranca.
AI_COPILOT_CONTEXT_MESSAGES = 20
# F8-08g (ADR-034 §6, §7): tool calls por invocación del copiloto (entero de 1 a 10) y su plazo
# total en segundos (entero de 20 a 55: por debajo del tiempo límite del proxy de producción,
# decisión del orquestador en #771). Un valor fuera de rango no arranca.
AI_COPILOT_TOOL_CALLS = 5
AI_COPILOT_DEADLINE = 50
# F8-08e (ADR-034 §9): segundos que vale la evidencia de precio y stock para el Output Guard; un
# valor fijo en configuración, entero de 1 a 600 (10 minutos como mucho), o el guard falla cerrado.
AI_EVIDENCE_TTL = 600
# F8-08f (ADR-034 §8): con traspaso, la sugerencia es siempre el texto de traspaso, nunca el del
# modelo. Este es el inicial, decidido por el propietario el 2026-11-21 (#771): el de toda
# organización que no guardó el suyo (F8-08l, `…/ai/handoff-reply/`). Nunca promete atención
# inmediata; con una cifra, un plazo, una prisa, un enlace o la marca, no arranca.
AI_HANDOFF_REPLY = (
    "Entiendo. Voy a solicitar que una persona de nuestro equipo continúe contigo para ayudarte "
    "directamente. Tu conversación quedará disponible para que pueda revisar el contexto y no "
    "tengas que repetir toda la información"
)
# F8-08j (D-F8-05, ADR-034 §13): días que se guarda el texto de una sugerencia o un resumen; después
# la purga diaria lo pone a NULL (los hallazgos y los ids quedan).
AI_SUGGESTION_TEXT_DAYS = 30
# F8-08k (D-F8-05): el job de migraciones quita el detalle de la IA de más de 12 meses completos.
# Apagado hasta que el propietario lo confirme con datos reales en producción; encenderlo es un PR.
AI_RECORDS_RETENTION = False
# Correo saliente (ADR-019): SMTP por entorno, sin proveedor fijado. Solo `core.mail` lo usa.
# El backend por defecto de Django no envía nada: lo que no pasa por `core.mail` (el aviso a
# `ADMINS` de `AdminEmailHandler`, `django.core.mail.send_mail`) no abre una conexión SMTP.
EMAIL_BACKEND = "django.core.mail.backends.dummy.EmailBackend"
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
# El origen público de la aplicación (ADR-021 §8): con él se componen los enlaces que van en un
# correo. Llega del despliegue, nunca de una petición. Sin él no se emite ningún enlace.
APP_ORIGIN = env.origin("APP_ORIGIN")
# Claves maestras del cifrado de secretos en reposo (ADR-022), por versión; cifra la más alta.
# Llegan del entorno de web, worker, ws y beat, nunca de la base de datos. Sin ellas no se cifra
# ni se descifra nada (`core.sealed` falla cerrado); `local` y `test` derivan una de desarrollo.
SECRETS_KEKS = env.keys("SECRETS_KEKS")
# Accesos de prueba (ADR-026): el slug de la organización de pruebas; vacío los desactiva y es
# el valor por defecto. `production` no arranca con él.
TEST_ACCOUNTS_ORGANIZATION = env.optional("DJANGO_TEST_ACCOUNTS_ORGANIZATION", "").strip()
if TEST_ACCOUNTS_ORGANIZATION and not re.fullmatch(
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", TEST_ACCOUNTS_ORGANIZATION
):
    raise ImproperlyConfigured("DJANGO_TEST_ACCOUNTS_ORGANIZATION no es un slug válido")
# F7-11a (ADR-011): umbrales por ventana de 5 minutos de las alertas `meta.alert.<tipo>` de la
# ingesta de Meta; cada uno se sube con `META_ALERT_<TIPO>` (p. ej. `META_ALERT_OUTBOUND_FAILED=5`).
# Por omisión: una ráfaga de 20 firmas inválidas, 5 intentos fallidos de la ingesta, cualquier
# `DEAD`, la cola parada 10 minutos, cualquier envío fallido (sin los rechazos de la política) y
# cualquier token marcado inválido. Pendientes de confirmación del propietario.
META_ALERTS = {
    name: env.integer(f"META_ALERT_{name.upper()}", default)
    for name, default in (
        ("signature_invalid", 20), ("ingress_failed", 5), ("ingress_dead", 1),
        ("ingress_lag_seconds", 600), ("outbound_failed", 1), ("token_invalid_count", 1),
    )
}  # fmt: skip
# Códigos de un envío `FAILED` que son decisiones de la política o del cliente, no una avería del
# canal: se cuentan en las métricas, pero no disparan `meta.alert.outbound_failed`.
META_ALERT_QUIET_CODES = (
    "CONVERSATION_CLOSED", "CONTACT_BLOCKED", "WINDOW_CLOSED", "CONSENT_REVOKED",
    "MEDIA_NOT_SCANNED", "RECIPIENT_UNREACHABLE",
)  # fmt: skip
# Reporte de errores (ADR-011 §3): sin SENTRY_DSN, NoopReporter (sin red).
SENTRY_DSN = env.optional("SENTRY_DSN", "")
SENTRY_ENVIRONMENT = env.optional("SENTRY_ENVIRONMENT", "")

"""Producción. Sin valores por defecto inseguros: falla al arrancar si falta algo crítico."""

import ipaddress
import os
from urllib.parse import urlsplit

from django.core.exceptions import ImproperlyConfigured

from config import env
from config.settings.base import *  # noqa: F403
from config.settings.base import (
    ALLOWED_HOSTS,
    APP_ORIGIN,
    DEFAULT_FROM_EMAIL,
    EMAIL_HOST,
    EMAIL_HOST_PASSWORD,
    EMAIL_HOST_USER,
    EMAIL_USE_SSL,
    EMAIL_USE_TLS,
    MAIL_BACKEND,
    SECRET_KEY,
)

# ADR-002 §1.1: la credencial del migrador (BYPASSRLS) es exclusiva del job de migraciones.
# web/worker/ws/beat se niegan a arrancar si la reciben. Solo se nombra la variable, nunca su valor.
FORBIDDEN_RUNTIME_VARIABLES = ("DATABASE_MIGRATOR_URL", "CRM_MIGRATOR_PASSWORD")
_leaked = [name for name in FORBIDDEN_RUNTIME_VARIABLES if name in os.environ]
if _leaked:
    raise ImproperlyConfigured(
        "El runtime recibió credenciales de migración prohibidas: " + ", ".join(_leaked)
    )

DEBUG = False
ENFORCE_RUNTIME_DB_ROLE = True  # ADR-002 §1.1: ni superusuario, ni BYPASSRLS, ni propietario
if env.boolean("DJANGO_DEBUG", False):
    raise ImproperlyConfigured("DEBUG no puede activarse en production")
if not ALLOWED_HOSTS:
    raise ImproperlyConfigured("DJANGO_ALLOWED_HOSTS es obligatorio en production")
if SECRET_KEY.startswith("django-insecure") or len(SECRET_KEY) < 50:
    raise ImproperlyConfigured(
        "DJANGO_SECRET_KEY de production debe ser aleatoria y de 50+ caracteres"
    )

# ADR-008: storage con TLS. Vacío = AWS por defecto; explícito = https y sin credenciales.
_storage = urlsplit(env.optional("STORAGE_ENDPOINT_URL", ""))
if _storage.geturl() and (_storage.scheme != "https" or _storage.username or _storage.password):
    raise ImproperlyConfigured(
        "STORAGE_ENDPOINT_URL de production debe ser https y sin credenciales"
    )

# ADR-019: sin servidor de correo, enviar falla al intentarlo. Con servidor, va cifrado
# (STARTTLS o TLS implícito, no los dos), con remitente y con credenciales.
if EMAIL_HOST and not (
    EMAIL_USE_TLS != EMAIL_USE_SSL
    and DEFAULT_FROM_EMAIL
    and EMAIL_HOST_USER
    and EMAIL_HOST_PASSWORD
):
    raise ImproperlyConfigured(
        "El correo de production (EMAIL_HOST) exige EMAIL_USE_TLS o EMAIL_USE_SSL (uno de los"
        " dos), MAIL_FROM, EMAIL_HOST_USER y EMAIL_HOST_PASSWORD"
    )
# ADR-021 §8: un enlace que cambia una contraseña no viaja por http.
if EMAIL_HOST and not APP_ORIGIN.startswith("https://"):
    raise ImproperlyConfigured("Con correo configurado, APP_ORIGIN de production debe ser https")
if MAIL_BACKEND != "smtp":
    raise ImproperlyConfigured("MAIL_BACKEND de production debe ser smtp")

# Loopback para las sondas locales (HEALTHCHECK del contenedor): se añade DESPUÉS de validar,
# así una DJANGO_ALLOWED_HOSTS vacía sigue fallando y el operador no necesita conocer la sonda.
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "[::1]")
ALLOWED_HOSTS = list(dict.fromkeys([*ALLOWED_HOSTS, *LOOPBACK_HOSTS]))

# La dirección del cliente (auditoría de plataforma, límite de intentos) es la que resuelve
# uvicorn, que solo acepta `X-Forwarded-For` de estas direcciones o redes. Sin declararlas,
# todos los clientes tendrían la del proxy; con `*` o una red que lo abarque todo, cada cliente
# elegiría la suya. uvicorn ignora en silencio lo que no es una IP o una red: aquí no arranca.
FORWARDED_ALLOW_IPS = env.csv_list("FORWARDED_ALLOW_IPS")
_WIDEST = {4: 8, 6: 16}  # prefijo mínimo: una red de proxies nunca es media Internet
_MAPPED = ipaddress.ip_network("::ffff:0:0/96")  # IPv4 escrita como IPv6: uvicorn no la casa


def _is_proxy(item: str) -> bool:
    try:
        network = ipaddress.ip_network(item)  # estricto, como uvicorn: `10.0.0.2/24` no vale
    except ValueError:
        return False
    mapped = network.version == 6 and network.overlaps(_MAPPED)
    return network.prefixlen >= _WIDEST[network.version] and not mapped


if not FORWARDED_ALLOW_IPS or not all(map(_is_proxy, FORWARDED_ALLOW_IPS)):
    raise ImproperlyConfigured(
        "FORWARDED_ALLOW_IPS es obligatorio en production: direcciones IP o redes CIDR de los"
        " proxies (10.0.3.2, 10.0.5.0/24), sin bits de host ni nombres, no más anchas que /8"
        " (IPv4) o /16 (IPv6); nunca *"
    )
if "UVICORN_FORWARDED_ALLOW_IPS" in os.environ:  # uvicorn la prefiere a la que se validó
    raise ImproperlyConfigured("UVICORN_FORWARDED_ALLOW_IPS no se admite: usar FORWARDED_ALLOW_IPS")

# Detrás del reverse proxy (ADR-003): el proxy termina TLS y envía X-Forwarded-Proto.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_SSL_REDIRECT = True
SECURE_REDIRECT_EXEMPT = [r"^health/"]  # sondas internas del orquestador
SECURE_HSTS_SECONDS = 31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "strict-origin-when-cross-origin"
SESSION_COOKIE_SECURE = True
SESSION_COOKIE_NAME = "__Host-crm_session"  # solo válido con Secure, Path=/ y sin Domain
CSRF_COOKIE_SECURE = True
X_FRAME_OPTIONS = "DENY"
CSRF_TRUSTED_ORIGINS = env.csv_list("DJANGO_CSRF_TRUSTED_ORIGINS")

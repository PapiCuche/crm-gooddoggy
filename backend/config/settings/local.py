"""Desarrollo local. Nunca se usa en producción."""

import hashlib
import os

os.environ.setdefault("DJANGO_SECRET_KEY", "django-insecure-local-only-not-for-production")
os.environ.setdefault("DATABASE_URL", "postgres://crm_app:change-me-local-app@localhost:5432/crm")

from config import env  # noqa: E402
from config.settings.base import *  # noqa: E402,F403

DEBUG = True
# Compose (F1-10): sondas internas por nombre de servicio (p. ej. `backend`).
ALLOWED_HOSTS = ["localhost", "127.0.0.1", *env.csv_list("DJANGO_ALLOWED_HOSTS")]
# Compose lo activa: el stack local también prueba que el runtime es crm_app (ADR-002 §1.1).
ENFORCE_RUNTIME_DB_ROLE = env.boolean("ENFORCE_RUNTIME_DB_ROLE", False)
# `next dev` hace de proxy de /api y cambia la cabecera Host: sin esto, la comprobación de
# Origin del CSRF rechazaría toda mutación hecha desde el navegador en desarrollo.
CSRF_TRUSTED_ORIGINS = [
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    *env.csv_list("DJANGO_CSRF_TRUSTED_ORIGINS"),
]

# Sin `SECRETS_KEKS`, una clave de desarrollo derivada de la SECRET_KEY local (ADR-022 §6): el
# cifrado de secretos funciona sin configurar nada. Nunca en production, que no hereda de aquí.
SECRETS_KEKS = env.keys("SECRETS_KEKS") or {
    1: hashlib.sha256(b"dev-kek:" + SECRET_KEY.encode()).digest()  # noqa: F405
}

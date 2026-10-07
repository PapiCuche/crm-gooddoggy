"""Tests (pytest). La BD se toma de DATABASE_URL (en CI: servicio postgres:18.6)."""

import os

os.environ.setdefault("DJANGO_SECRET_KEY", "django-insecure-test-only-not-for-production")
os.environ.setdefault("DATABASE_URL", "postgres://crm_app:change-me-local-app@localhost:5432/crm")

from config.settings.base import *  # noqa: E402,F403

ALLOWED_HOSTS = ["testserver"]
INSTALLED_APPS = [*INSTALLED_APPS, "tests.tenancy_app"]  # noqa: F405 — solo tests (RLS)
# Migraciones de la BD de test: como crm_migrator (tests/conftest.py). Los tests corren como
# el rol de DATABASE_URL, que debe ser crm_app (ni superusuario ni BYPASSRLS).
MIGRATOR_DATABASE_URL = os.environ.get("DATABASE_MIGRATOR_URL", "")
# F1-04: rutas de tenant de prueba, autenticación y membresías simuladas (tests/fakes.py).
ROOT_URLCONF = "tests.urls"
MIDDLEWARE = [*MIDDLEWARE[:-1], "tests.fakes.FakeAuthMiddleware", MIDDLEWARE[-1]]  # noqa: F405
TENANCY_MEMBERSHIP_RESOLVER = "tests.fakes.membership"
STORAGE_BACKEND = "memory"  # la suite de contrato usa Garage vía STORAGE_* (tests/test_storage.py)
MAIL_BACKEND = "memory"  # los correos quedan en `django.core.mail.outbox`
DEFAULT_FROM_EMAIL = "no-reply@crm.test"

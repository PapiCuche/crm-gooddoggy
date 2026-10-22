"""Job de migraciones (ADR-002 §1.1): conecta como `crm_migrator` con DATABASE_MIGRATOR_URL.

No recibe secretos de runtime: si ve DATABASE_URL se niega a arrancar. La SECRET_KEY es
efímera porque el job no firma nada.
"""

import os
import secrets

from django.core.exceptions import ImproperlyConfigured

from config import env

if "DATABASE_URL" in os.environ:
    raise ImproperlyConfigured("El job de migraciones no debe recibir DATABASE_URL (runtime)")
os.environ["DATABASE_URL"] = env.required("DATABASE_MIGRATOR_URL")  # solo dentro de este proceso
os.environ.setdefault("DJANGO_SECRET_KEY", secrets.token_urlsafe(64))

from config.settings.base import *  # noqa: E402,F403

# El job no cifra ni lee secretos (ADR-002 §1.1): aunque le den las claves, no las guarda.
SECRETS_KEKS = {}
# Ni accesos de prueba (ADR-026 §1): es un job de producción y conecta con BYPASSRLS.
TEST_ACCOUNTS_ORGANIZATION = ""

from django.apps import AppConfig
from django.conf import settings
from django.db.backends.signals import connection_created


class CoreConfig(AppConfig):
    name = "core"
    verbose_name = "Core (kernel)"

    def ready(self) -> None:
        from core.observability import logging, reporting
        from core.tenancy import checks  # noqa: F401 — registra la de la política de segundo factor

        logging.configure()
        reporting.reporter()  # Noop sin SENTRY_DSN; si hay DSN, inicializa Sentry al arrancar
        if settings.ENFORCE_RUNTIME_DB_ROLE:
            from core.db.guards import enforce_runtime_role

            connection_created.connect(enforce_runtime_role, dispatch_uid="enforce_runtime_role")

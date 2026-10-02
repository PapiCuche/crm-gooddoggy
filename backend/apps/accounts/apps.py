from django.apps import AppConfig
from django.core.exceptions import ImproperlyConfigured


class AccountsConfig(AppConfig):
    name = "apps.accounts"
    label = "accounts"

    def ready(self) -> None:
        from apps.accounts import checks  # noqa: PLC0415 — sus modelos ya están cargados

        if problem := checks.login_throttle():  # ningún proceso arranca con un límite roto
            raise ImproperlyConfigured(f"LOGIN_THROTTLE: {problem}")

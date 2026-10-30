from django.apps import AppConfig


class DealsConfig(AppConfig):
    name = "apps.deals"
    label = "deals"

    def ready(self) -> None:
        from apps.deals.seed import ensure_default_pipeline
        from apps.provisioning.seeds import register_seed

        register_seed(ensure_default_pipeline)  # las organizaciones nuevas nacen con su pipeline

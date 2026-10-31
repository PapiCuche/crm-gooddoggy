from django.apps import AppConfig


class DealsConfig(AppConfig):
    name = "apps.deals"
    label = "deals"

    def ready(self) -> None:
        from apps.access.scopes import Everyone, register
        from apps.deals.models import LostReason, Pipeline, PipelineStage
        from apps.deals.seed import ensure_default_pipeline
        from apps.provisioning.seeds import register_seed

        register_seed(ensure_default_pipeline)  # las organizaciones nuevas nacen con su pipeline
        # Configuración de la organización (F5-02): con `opportunities.view`, cualquier alcance
        # la ve toda. El alcance decidirá qué oportunidades se ven (F5-03), no esto.
        for model in (Pipeline, PipelineStage, LostReason):
            register(model, Everyone())

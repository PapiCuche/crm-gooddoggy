from django.apps import AppConfig


class QuotesConfig(AppConfig):
    name = "apps.quotes"
    label = "quotes"

    def ready(self) -> None:
        from apps.access.scopes import register
        from apps.deals.scopes import AssignedTo
        from apps.provisioning.seeds import register_seed
        from apps.quotes.models import Quote
        from apps.quotes.seed import ensure_template_policies

        # Por quien creó la cotización (F5-09d): `TEAM`, la de sus compañeros de equipo
        register(Quote, AssignedTo("created_by_user_id"))
        register_seed(
            ensure_template_policies
        )  # F5-09b4: las organizaciones nuevas nacen con ellas

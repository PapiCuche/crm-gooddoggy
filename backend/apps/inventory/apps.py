from django.apps import AppConfig


class InventoryConfig(AppConfig):
    name = "apps.inventory"
    label = "inventory"

    def ready(self) -> None:
        from apps.catalog.events import variants_retiring
        from apps.inventory.guards import refuse_with_stock

        variants_retiring.connect(refuse_with_stock, dispatch_uid="inventory.refuse_with_stock")

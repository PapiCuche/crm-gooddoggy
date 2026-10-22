from django.urls import path

from apps.inventory.api import stock, views

urlpatterns = [
    path("warehouses/", views.WarehousesView.as_view(), name="warehouses"),
    path("warehouses/<uuid:warehouse_id>/", views.WarehouseView.as_view(), name="warehouse"),
    path("inventory/levels/", stock.LevelsView.as_view(), name="inventory-levels"),
    path("inventory/movements/", stock.MovementsView.as_view(), name="inventory-movements"),
]

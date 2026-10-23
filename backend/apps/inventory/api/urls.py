from django.urls import path

from apps.inventory.api import imports, stock, views

urlpatterns = [
    path("warehouses/", views.WarehousesView.as_view(), name="warehouses"),
    path("warehouses/<uuid:warehouse_id>/", views.WarehouseView.as_view(), name="warehouse"),
    path("inventory/levels/", stock.LevelsView.as_view(), name="inventory-levels"),
    path("inventory/movements/", stock.MovementsView.as_view(), name="inventory-movements"),
    path("inventory/imports/", imports.StockImportsView.as_view(), name="stock-imports"),
    path("inventory/imports/template/", imports.TemplateView.as_view(), name="stock-template"),
    path(
        "inventory/imports/<uuid:import_id>/rows/",
        imports.StockImportRowsView.as_view(),
        name="stock-import-rows",
    ),
]

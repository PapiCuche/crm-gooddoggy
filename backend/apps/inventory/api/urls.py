from django.urls import path

from apps.inventory.api import views

urlpatterns = [
    path("warehouses/", views.WarehousesView.as_view(), name="warehouses"),
    path("warehouses/<uuid:warehouse_id>/", views.WarehouseView.as_view(), name="warehouse"),
]

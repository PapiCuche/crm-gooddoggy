from django.urls import path

from apps.pricing.api import views

urlpatterns = [
    path("pricing/price-lists/", views.PriceListsView.as_view(), name="price-lists"),
    path(
        "pricing/price-lists/<uuid:price_list_id>/",
        views.PriceListView.as_view(),
        name="price-list",
    ),
]

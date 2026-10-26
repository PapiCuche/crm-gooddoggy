from django.urls import path

from apps.pricing.api import prices, views

urlpatterns = [
    path("pricing/price-lists/", views.PriceListsView.as_view(), name="price-lists"),
    path(
        "pricing/price-lists/<uuid:price_list_id>/",
        views.PriceListView.as_view(),
        name="price-list",
    ),
    path(
        "pricing/price-lists/<uuid:price_list_id>/prices/",
        prices.ListPricesView.as_view(),
        name="price-list-prices",
    ),
]

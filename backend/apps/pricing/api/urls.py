from django.urls import path

from apps.pricing.api import costs, prices, promotions, views

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
    path(
        "pricing/variants/<uuid:variant_id>/price-history/",
        prices.PriceHistoryView.as_view(),
        name="variant-price-history",
    ),
    path(
        "pricing/variants/<uuid:variant_id>/costs/",
        costs.CostsView.as_view(),
        name="variant-costs",
    ),
    path("pricing/promotions/", promotions.PromotionsView.as_view(), name="promotions"),
    path(
        "pricing/promotions/<uuid:promotion_id>/",
        promotions.PromotionView.as_view(),
        name="promotion",
    ),
    path(
        "pricing/promotions/<uuid:promotion_id>/publish/",
        promotions.PromotionPublishView.as_view(),
        name="promotion-publish",
    ),
    path(
        "pricing/promotions/<uuid:promotion_id>/cancel/",
        promotions.PromotionCancelView.as_view(),
        name="promotion-cancel",
    ),
]

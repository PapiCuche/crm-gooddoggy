from django.urls import path

from apps.quotes.api import policies, quotes

urlpatterns = [
    path("quotes/", quotes.QuotesView.as_view(), name="quotes"),
    path("quotes/<uuid:quote_id>/", quotes.QuoteView.as_view(), name="quote"),
    path(
        "quotes/<uuid:quote_id>/request-approval/",
        quotes.QuoteRequestApprovalView.as_view(),
        name="quote-request-approval",
    ),
    path(
        "quotes/<uuid:quote_id>/items/<uuid:line_id>/price/",
        quotes.QuotePriceView.as_view(),
        name="quote-price",
    ),
    path(
        "quotes/discount-policies/",
        policies.DiscountPoliciesView.as_view(),
        name="discount-policies",
    ),
]

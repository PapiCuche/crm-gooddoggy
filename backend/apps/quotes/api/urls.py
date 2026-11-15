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
        "quotes/<uuid:quote_id>/approve/",
        quotes.QuoteApproveView.as_view(),
        name="quote-approve",
    ),
    path(
        "quotes/<uuid:quote_id>/reject/",
        quotes.QuoteRejectView.as_view(),
        name="quote-reject",
    ),
    path(
        "quotes/<uuid:quote_id>/withdraw-request/",
        quotes.QuoteWithdrawRequestView.as_view(),
        name="quote-withdraw-request",
    ),
    path(
        "quotes/<uuid:quote_id>/cancel/",
        quotes.QuoteCancelView.as_view(),
        name="quote-cancel",
    ),
    path(
        "quotes/<uuid:quote_id>/issue/",
        quotes.QuoteIssueView.as_view(),
        name="quote-issue",
    ),
    path(
        "quotes/<uuid:quote_id>/accept/",
        quotes.QuoteAcceptView.as_view(),
        name="quote-accept",
    ),
    path(
        "quotes/<uuid:quote_id>/decline/",
        quotes.QuoteDeclineView.as_view(),
        name="quote-decline",
    ),
    path(
        "quotes/<uuid:quote_id>/revise/",
        quotes.QuoteReviseView.as_view(),
        name="quote-revise",
    ),
    path(
        "quotes/<uuid:quote_id>/items/<uuid:line_id>/price/",
        quotes.QuotePriceView.as_view(),
        name="quote-price",
    ),
    path(
        "quotes/<uuid:quote_id>/items/<uuid:line_id>/discount/",
        quotes.QuoteDiscountView.as_view(),
        name="quote-discount",
    ),
    path(
        "quotes/discount-policies/",
        policies.DiscountPoliciesView.as_view(),
        name="discount-policies",
    ),
]

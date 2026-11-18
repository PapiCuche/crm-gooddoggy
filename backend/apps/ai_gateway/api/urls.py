from django.urls import path

from apps.ai_gateway.api import budget, views

urlpatterns = [
    path("ai/budget/", budget.BudgetView.as_view(), name="ai-budget"),
    path("ai/provider-accounts/", views.AccountsView.as_view(), name="ai-provider-accounts"),
    path(
        "ai/provider-accounts/<uuid:account_id>/",
        views.AccountView.as_view(),
        name="ai-provider-account",
    ),
    path(
        "ai/provider-accounts/<uuid:account_id>/key/",
        views.AccountKeyView.as_view(),
        name="ai-provider-account-key",
    ),
    path(
        "ai/provider-accounts/<uuid:account_id>/test/",
        views.AccountTestView.as_view(),
        name="ai-provider-account-test",
    ),
]

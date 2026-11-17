from django.urls import path

from apps.ai_gateway.api import views

urlpatterns = [
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
]

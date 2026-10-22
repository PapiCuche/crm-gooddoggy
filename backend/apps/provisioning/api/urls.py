from django.urls import path

from apps.provisioning.api import views

urlpatterns = [
    path("", views.TestAccountsView.as_view(), name="auth-test-accounts"),
]

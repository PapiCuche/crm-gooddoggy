"""Rutas de tenant de `organizations` (`/api/v1/o/<slug>/…`)."""

from django.urls import path

from apps.organizations.api import views

urlpatterns = [
    path("branches/", views.BranchesView.as_view(), name="branches"),
]

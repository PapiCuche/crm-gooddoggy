"""Rutas de tenant de `audit` (`/api/v1/o/<slug>/…`)."""

from django.urls import path

from apps.audit.api import views

urlpatterns = [path("audit/", views.AuditLogView.as_view(), name="audit")]

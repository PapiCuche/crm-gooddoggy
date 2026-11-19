"""Rutas de plataforma de `members` (`/api/v1/invitations/…`): sin sesión de la organización."""

from django.urls import path

from apps.members.api import views

urlpatterns = [
    path("preview/", views.InvitationPreviewView.as_view(), name="invitation-preview"),
    path("accept/", views.InvitationAcceptView.as_view(), name="invitation-accept"),
]

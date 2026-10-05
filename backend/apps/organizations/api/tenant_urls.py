"""Rutas de tenant de `organizations` (`/api/v1/o/<slug>/…`)."""

from django.urls import path

from apps.organizations.api import views

urlpatterns = [
    path("branches/", views.BranchesView.as_view(), name="branches"),
    path("branches/<uuid:branch_id>/", views.BranchView.as_view(), name="branch"),
    path("teams/", views.TeamsView.as_view(), name="teams"),
    path("teams/<uuid:team_id>/", views.TeamView.as_view(), name="team"),
]

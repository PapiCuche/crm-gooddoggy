from django.urls import path

from apps.automations.api import views

urlpatterns = [
    path("automations/", views.AutomationsView.as_view(), name="automations"),
    path("automations/rules/<str:key>/", views.RuleView.as_view(), name="automations-rule"),
    path("automations/pause/", views.PauseView.as_view(), name="automations-pause"),
    path("automations/resume/", views.ResumeView.as_view(), name="automations-resume"),
    path("automations/runs/", views.RunsView.as_view(), name="automations-runs"),
]

from django.urls import path

from apps.access.api import views

urlpatterns = [
    path("me/", views.SelfContextView.as_view(), name="me-context"),
    path("members/", views.MembersView.as_view(), name="members"),
    path("roles/", views.RolesView.as_view(), name="roles"),
]

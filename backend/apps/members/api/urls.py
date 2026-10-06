from django.urls import path

from apps.members.api import views

urlpatterns = [
    path(
        "members/<uuid:membership_id>/status/",
        views.MemberStatusView.as_view(),
        name="member-status",
    ),
    path(
        "members/<uuid:membership_id>/branch/",
        views.MemberBranchView.as_view(),
        name="member-branch",
    ),
]

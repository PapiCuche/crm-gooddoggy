from django.urls import path

from apps.members.api import views

urlpatterns = [
    path("invitations/", views.InvitationsView.as_view(), name="invitations"),
    path(
        "invitations/<uuid:invitation_id>/status/",
        views.InvitationStatusView.as_view(),
        name="invitation-status",
    ),
    path(
        "invitations/<uuid:invitation_id>/resend/",
        views.InvitationResendView.as_view(),
        name="invitation-resend",
    ),
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
    path(
        "teams/<uuid:team_id>/members/<uuid:membership_id>/",
        views.TeamMemberView.as_view(),
        name="team-member",
    ),
]

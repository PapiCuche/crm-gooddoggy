from django.urls import path

from apps.accounts.api import views

urlpatterns = [
    path("csrf/", views.CsrfView.as_view(), name="auth-csrf"),
    path("login/mfa/", views.LoginMfaView.as_view(), name="auth-login-mfa"),
    path("login/", views.LoginView.as_view(), name="auth-login"),
    path("logout/", views.LogoutView.as_view(), name="auth-logout"),
    path("mfa/backup-codes/", views.MfaBackupCodesView.as_view(), name="auth-mfa-backup-codes"),
    path("mfa/disable/", views.MfaDisableView.as_view(), name="auth-mfa-disable"),
    path("mfa/step-up/", views.MfaStepUpView.as_view(), name="auth-mfa-step-up"),
    path("mfa/totp/confirm/", views.MfaTotpConfirmView.as_view(), name="auth-mfa-totp-confirm"),
    path("mfa/totp/", views.MfaTotpView.as_view(), name="auth-mfa-totp"),
    path("mfa/", views.MfaView.as_view(), name="auth-mfa"),
    path(
        "password-reset/confirm/",
        views.PasswordResetConfirmView.as_view(),
        name="auth-password-reset-confirm",
    ),
    path(
        "password-reset/",
        views.PasswordResetRequestView.as_view(),
        name="auth-password-reset-request",
    ),
    path("session/", views.SessionView.as_view(), name="auth-session"),
]

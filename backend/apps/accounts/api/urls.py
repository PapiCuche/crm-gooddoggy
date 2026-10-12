from django.urls import path

from apps.accounts.api import views

urlpatterns = [
    path("csrf/", views.CsrfView.as_view(), name="auth-csrf"),
    path("login/", views.LoginView.as_view(), name="auth-login"),
    path("logout/", views.LogoutView.as_view(), name="auth-logout"),
    path("mfa/totp/confirm/", views.MfaTotpConfirmView.as_view(), name="auth-mfa-totp-confirm"),
    path("mfa/totp/", views.MfaTotpView.as_view(), name="auth-mfa-totp"),
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

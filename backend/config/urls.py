from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView

from core import health

urlpatterns = [
    path("health/live", health.live, name="health-live"),
    path("health/ready", health.ready, name="health-ready"),
    path("api/schema/", SpectacularAPIView.as_view(), name="api-schema"),  # contrato OpenAPI
    # Rutas de plataforma (ADR-014 §4): sin tenant. Cada una figura en `PLATFORM` de la
    # auditoría del URLconf (tests/test_access_api.py).
    path("api/v1/auth/", include("apps.accounts.api.urls")),
    path("api/v1/me/", include("apps.organizations.api.urls")),
    # Rutas de tenant: `TenantResolutionMiddleware` resuelve el slug antes de la vista.
    path("api/v1/o/<slug:org_slug>/", include("apps.access.api.urls")),
    path("api/v1/o/<slug:org_slug>/", include("apps.members.api.urls")),
]

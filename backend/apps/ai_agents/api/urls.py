from django.urls import path

from apps.ai_agents.api import views

urlpatterns = [
    path(  # sin el scope de la petición: `UNSCOPED` en `core.tenancy.middleware` (F8-08h)
        "conversations/<uuid:conversation_id>/copilot/suggest/",
        views.SuggestView.as_view(),
        name="copilot-suggest",
    ),
    path(  # F8-08i: también en `UNSCOPED`; una ruta nueva aquí no entra sola
        "conversations/<uuid:conversation_id>/copilot/summary/",
        views.SummaryView.as_view(),
        name="copilot-summary",
    ),
]

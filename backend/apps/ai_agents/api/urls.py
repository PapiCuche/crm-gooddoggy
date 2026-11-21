from django.urls import path

from apps.ai_agents.api import views

urlpatterns = [
    path(  # sin el scope de la petición: `UNSCOPED` en `core.tenancy.middleware` (F8-08h)
        "conversations/<uuid:conversation_id>/copilot/suggest/",
        views.SuggestView.as_view(),
        name="copilot-suggest",
    ),
]

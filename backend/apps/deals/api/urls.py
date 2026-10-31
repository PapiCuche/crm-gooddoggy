from django.urls import path

from apps.deals.api import pipelines

urlpatterns = [
    path("pipelines/", pipelines.PipelinesView.as_view(), name="pipelines"),
    path("lost-reasons/", pipelines.LostReasonsView.as_view(), name="lost-reasons"),
]

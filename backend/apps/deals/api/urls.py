from django.urls import path

from apps.deals.api import pipelines

urlpatterns = [
    path("pipelines/", pipelines.PipelinesView.as_view(), name="pipelines"),
    path("pipelines/<uuid:pipeline_id>/", pipelines.PipelineView.as_view(), name="pipeline"),
    path(
        "pipelines/<uuid:pipeline_id>/stages/<uuid:stage_id>/",
        pipelines.StageView.as_view(),
        name="pipeline-stage",
    ),
    path("lost-reasons/", pipelines.LostReasonsView.as_view(), name="lost-reasons"),
    path(
        "lost-reasons/<uuid:lost_reason_id>/",
        pipelines.LostReasonView.as_view(),
        name="lost-reason",
    ),
]

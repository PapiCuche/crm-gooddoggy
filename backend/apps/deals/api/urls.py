from django.urls import path

from apps.deals.api import opportunities, pipelines

urlpatterns = [
    path("pipelines/", pipelines.PipelinesView.as_view(), name="pipelines"),
    path("pipelines/<uuid:pipeline_id>/", pipelines.PipelineView.as_view(), name="pipeline"),
    path(
        "pipelines/<uuid:pipeline_id>/stages/",
        pipelines.StagesView.as_view(),
        name="pipeline-stages",
    ),
    path(
        "pipelines/<uuid:pipeline_id>/stages/order/",
        pipelines.StageOrderView.as_view(),
        name="pipeline-stage-order",
    ),
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
    path("opportunities/", opportunities.OpportunitiesView.as_view(), name="opportunities"),
]

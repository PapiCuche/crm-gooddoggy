from django.urls import path

from apps.tasks.api import tasks

urlpatterns = [
    path("tasks/", tasks.TasksView.as_view(), name="tasks"),
    path("tasks/<uuid:task_id>/", tasks.TaskView.as_view(), name="task"),
    *(
        path(f"tasks/<uuid:task_id>/{name}/", view.as_view(), name=f"task-{name}")
        for name, view in (
            ("assign", tasks.TaskAssignView),
            ("complete", tasks.TaskCompleteView),
            ("cancel", tasks.TaskCancelView),
            ("reopen", tasks.TaskReopenView),
        )
    ),
]

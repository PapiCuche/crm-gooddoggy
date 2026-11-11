from django.urls import path

from apps.tasks.api import tasks

urlpatterns = [
    path("tasks/", tasks.TasksView.as_view(), name="tasks"),
    path("tasks/<uuid:task_id>/", tasks.TaskView.as_view(), name="task"),
]

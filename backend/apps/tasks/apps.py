from django.apps import AppConfig


class TasksConfig(AppConfig):
    name = "apps.tasks"
    label = "tasks"

    def ready(self) -> None:
        from apps.access.scopes import register
        from apps.deals.scopes import AssignedTo
        from apps.tasks.models import Task

        register(Task, AssignedTo())  # por responsable; `TEAM`, sus compañeros de equipo (F5-08b)

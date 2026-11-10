"""Tareas y seguimientos (Fase 5, E10; modelo de datos §E.6). Tabla tenant-owned con FORCE RLS y
FKs compuestas (contacto, oportunidad y personas, en la migración). Nada se borra: la tarea se
marca borrada y `crm_app` no tiene `DELETE` ni `TRUNCATE`."""

from django.db import models

from apps.deals.models import Priority
from core.db.models import TenantModel, uuid7_primary_key


class TaskType(models.TextChoices):
    CALL = "CALL"
    MESSAGE = "MESSAGE"
    MEETING = "MEETING"
    FOLLOW_UP = "FOLLOW_UP"
    QUOTE = "QUOTE"
    COLLECTION = "COLLECTION"
    DELIVERY = "DELIVERY"
    OTHER = "OTHER"


class TaskStatus(models.TextChoices):
    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    DONE = "DONE"
    CANCELLED = "CANCELLED"


class TaskSource(models.TextChoices):
    MANUAL = "MANUAL"
    AUTOMATION = "AUTOMATION"
    AI = "AI"


class Task(TenantModel):
    """Algo por hacer, con responsable y fecha. `due_at` es un instante (`is_all_day` dice que
    importa el día, no la hora: el cliente lo muestra sin hora). Lo completado lleva su fecha y su
    autor (CHECK); el motivo solo lo cancelado. Las personas (`*_user_id`) son miembros de la
    organización y el contacto y la oportunidad, de la misma: FKs compuestas en la migración."""

    Type, Status, Source, Priority = TaskType, TaskStatus, TaskSource, Priority

    id = uuid7_primary_key()
    type = models.CharField(max_length=12, choices=TaskType.choices, default=TaskType.FOLLOW_UP)
    title = models.CharField(max_length=200)
    description = models.CharField(max_length=5000, default="", blank=True)
    assigned_user_id = models.UUIDField(null=True, blank=True)
    contact = models.ForeignKey(
        "contacts.Contact",
        models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        db_constraint=False,
        db_index=False,
    )
    opportunity = models.ForeignKey(
        "deals.Opportunity",
        models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        db_constraint=False,
        db_index=False,
    )
    due_at = models.DateTimeField(null=True, blank=True)
    is_all_day = models.BooleanField(default=False)
    reminder_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=12, choices=TaskStatus.choices, default=TaskStatus.PENDING)
    priority = models.CharField(max_length=8, choices=Priority.choices, default=Priority.MEDIUM)
    completed_at = models.DateTimeField(null=True, blank=True)
    completed_by_user_id = models.UUIDField(null=True, blank=True)
    cancel_reason = models.CharField(max_length=500, null=True, blank=True)  # noqa: DJ001
    source = models.CharField(max_length=12, choices=TaskSource.choices, default=TaskSource.MANUAL)
    created_by_user_id = models.UUIDField(null=True, blank=True)
    updated_by_user_id = models.UUIDField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    deleted_at = models.DateTimeField(null=True, blank=True)
    deleted_by_user_id = models.UUIDField(null=True, blank=True)
    version = models.PositiveIntegerField(default=1)

    class Meta:
        db_table = "tasks"
        indexes = [
            models.Index(  # «Mis tareas»: las de una persona por estado y vencimiento
                fields=["organization_id", "assigned_user_id", "status", "due_at"],
                condition=models.Q(deleted_at__isnull=True),
                name="tasks_org_owner_idx",
            ),
            models.Index(
                fields=["organization_id", "contact"],
                condition=models.Q(deleted_at__isnull=True),
                name="tasks_org_contact_idx",
            ),
            models.Index(
                fields=["organization_id", "opportunity"],
                condition=models.Q(deleted_at__isnull=True),
                name="tasks_org_opportunity_idx",
            ),
        ]
        constraints = [
            models.CheckConstraint(condition=models.Q(title__regex=r"\S"), name="tasks_title_ck"),
            models.CheckConstraint(
                condition=models.Q(type__in=TaskType.values), name="tasks_type_ck"
            ),
            models.CheckConstraint(
                condition=models.Q(status__in=TaskStatus.values), name="tasks_status_ck"
            ),
            models.CheckConstraint(
                condition=models.Q(priority__in=Priority.values), name="tasks_priority_ck"
            ),
            models.CheckConstraint(
                condition=models.Q(source__in=TaskSource.values), name="tasks_source_ck"
            ),
            models.CheckConstraint(  # lo completado lleva su fecha; el motivo, solo lo cancelado
                condition=(
                    models.Q(status="DONE", completed_at__isnull=False, cancel_reason__isnull=True)
                    | models.Q(
                        status="CANCELLED",
                        completed_at__isnull=True,
                        completed_by_user_id__isnull=True,
                    )
                    | models.Q(
                        status__in=["PENDING", "IN_PROGRESS"],
                        completed_at__isnull=True,
                        completed_by_user_id__isnull=True,
                        cancel_reason__isnull=True,
                    )
                ),
                name="tasks_closed_ck",
            ),
            models.CheckConstraint(condition=models.Q(version__gte=1), name="tasks_version_ck"),
        ]

    def __str__(self) -> str:
        return str(self.pk)

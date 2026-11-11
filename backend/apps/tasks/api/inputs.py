"""Lo que entra por las rutas de escritura de tareas (F5-08b): serializadores estrictos (el tipo
exacto de cada campo, ninguna clave desconocida) con las mismas reglas que los comandos
(`commands._text` y `_moment`), para que un dato inválido sea un 400 en su campo. Una descripción
con un tabulador u otro carácter no imprimible es un 400 en `description`; el espacio interior de
cada línea se colapsa. Las fechas entran y salen en ISO 8601 con zona horaria. El responsable no se
envía al crear (es quien crea; se cambia con `…/assign/`), ni la organización."""

import datetime as dt
from typing import Any

from django.utils.dateparse import parse_datetime
from rest_framework import serializers

from apps.deals.api.inputs import Checked, version_required
from apps.tasks import commands
from apps.tasks.models import Task
from core.api.strict import Choice, Closed, Flag, Id, Whole

OPEN = [(s, s) for s in (Task.Status.PENDING, Task.Status.IN_PROGRESS)]
INSTANT = "ISO 8601 con zona horaria, como 2026-11-20T09:00:00-05:00."
VERSION = "La que se leyó; si no, 409."


def _title(value: str) -> str:
    text = commands._text(value, "título", commands.TITLE_MAX)
    if not text:
        raise ValueError("título: no puede estar vacío")
    return text


def _description(value: str) -> str:
    return commands._text(value, "descripción", commands.DESCRIPTION_MAX, lines=True)


def _instant(value: str) -> dt.datetime:
    moment = parse_datetime(value)
    if moment is None:
        raise ValueError(f"fecha y hora {INSTANT[:-1]}")
    return commands._moment(moment, "fecha y hora")


def _reason(value: str) -> str | None:
    return commands._text(value, "motivo", commands.REASON_MAX) or None


class TaskFields(Closed, serializers.Serializer[Any]):
    """Lo que se envía de una tarea al crearla o cambiarla."""

    type = Choice(choices=Task.Type.choices, required=False)
    description = Checked(
        _description, max_length=commands.DESCRIPTION_MAX, required=False, allow_blank=True
    )
    due_at = Checked(_instant, allow_null=True, required=False, help_text=INSTANT)
    is_all_day = Flag(
        required=False,
        help_text="Importa el día, no la hora: el cliente muestra `due_at` sin hora y envía el "
        "instante de las 00:00 locales del día elegido; el servidor no reinterpreta zonas.",
    )
    reminder_at = Checked(
        _instant,
        allow_null=True,
        required=False,
        help_text=f"{INSTANT} No se compara con `due_at`.",
    )
    priority = Choice(choices=Task.Priority.choices, required=False)


class TaskCreateSerializer(TaskFields):
    title = Checked(_title, max_length=commands.TITLE_MAX, help_text="Una línea de texto libre.")
    contact_id = Id(required=False, help_text="Un contacto vivo que quien crea puede ver.")
    opportunity_id = Id(
        required=False, help_text="Una oportunidad viva del contacto que quien crea puede ver."
    )


@version_required
class TaskUpdateSerializer(TaskFields):
    title = Checked(_title, max_length=commands.TITLE_MAX, required=False)
    status = Choice(choices=OPEN, required=False, help_text="`PENDING` o `IN_PROGRESS`.")
    version = Whole(min_value=1, max_value=2**31 - 1, help_text=VERSION)


class TaskVersionSerializer(Closed, serializers.Serializer[Any]):
    version = Whole(min_value=1, max_value=2**31 - 1, help_text=VERSION)


class TaskAssignSerializer(TaskVersionSerializer):
    assigned_user_id = Id(help_text="El responsable: un miembro activo de la organización.")


class TaskCancelSerializer(TaskVersionSerializer):
    reason = Checked(_reason, max_length=commands.REASON_MAX, allow_null=True, required=False)

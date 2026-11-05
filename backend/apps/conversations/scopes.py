"""Alcance de las conversaciones (F6-01; ADR-003 §5, 03 §H): propias y sin asignar.

- `OWN` (y `BRANCH`, que la conversación aún no tiene): las asignadas a quien consulta y las que
  nadie tiene (sin equipo ni persona), que cualquiera con el permiso puede ver y tomar.
- `TEAM`: además, las del equipo al que pertenece quien consulta (`ectx.team_ids`).
- `ORGANIZATION`: todas (lo resuelve el motor).

El listado (`q`) y la comprobación por objeto (`allows`) salen de las mismas condiciones.
"""

from dataclasses import dataclass

from django.db.models import Model, Q

from apps.access.catalog import Scope
from apps.access.selectors import ExecutionContext


@dataclass(frozen=True, slots=True)
class Visible:
    def q(self, scope: Scope, ectx: ExecutionContext) -> Q:
        user = ectx.tenant.user_id
        # sin usuario no hay «propias»: `assigned_user_id = NULL` sería `IS NULL`
        mine = Q(pk__in=()) if user is None else Q(assigned_user_id=user)
        found = mine | Q(assigned_user_id__isnull=True, assigned_team_id__isnull=True)
        if scope == Scope.TEAM and ectx.team_ids:
            found |= Q(assigned_team_id__in=ectx.team_ids)
        return found

    def allows(self, scope: Scope, ectx: ExecutionContext, obj: Model) -> bool:
        user, team = getattr(obj, "assigned_user_id", None), getattr(obj, "assigned_team_id", None)
        if user is None and team is None:
            return True
        if user is not None and user == ectx.tenant.user_id:
            return True
        return scope == Scope.TEAM and team is not None and team in ectx.team_ids

"""Alcance de las oportunidades (F5-04; ADR-003 §5, 03 §H): se decide por `assigned_user_id`.

- `OWN`: las asignadas a quien consulta.
- `TEAM`: además, las asignadas a un miembro de alguno de sus equipos (D-F4-01, salida (a), que
  los contactos aún no aplican: la oportunidad no lleva `team_id`). Cuentan todas las
  pertenencias, también las inactivas, como `ExecutionContext.team_ids`.
- `BRANCH`: no hay sucursal en la oportunidad todavía; vale lo mismo que `OWN`.
- `ORGANIZATION`: todas, también las que no tienen responsable (lo resuelve el motor).

El listado (`q`) y la comprobación por objeto (`allows`) salen de la misma declaración de
compañeros de equipo, así que no pueden divergir.
"""

from dataclasses import dataclass
from uuid import UUID

from django.db.models import Model, Q, QuerySet

from apps.access.catalog import Scope
from apps.access.selectors import ExecutionContext
from apps.organizations.models import TeamMember


def teammates(ectx: ExecutionContext) -> QuerySet[TeamMember]:
    """Las pertenencias a los equipos de quien consulta, de las que sale el usuario de cada
    miembro (`membership__user_id`). Sin equipos, vacío."""
    return TeamMember.objects.filter(team_id__in=ectx.team_ids)


@dataclass(frozen=True, slots=True)
class AssignedTo:
    def q(self, scope: Scope, ectx: ExecutionContext) -> Q:
        own = Q(assigned_user_id=ectx.tenant.user_id)  # sin usuario: `= NULL` no casa nunca
        if scope != Scope.TEAM or not ectx.team_ids:
            return own
        return own | Q(assigned_user_id__in=teammates(ectx).values("membership__user_id"))

    def allows(self, scope: Scope, ectx: ExecutionContext, obj: Model) -> bool:
        assigned: UUID | None = getattr(obj, "assigned_user_id", None)
        if assigned is None:  # nadie la tiene: solo `ORGANIZATION`
            return False
        if assigned == ectx.tenant.user_id:
            return True
        if scope != Scope.TEAM or not ectx.team_ids:
            return False
        return teammates(ectx).filter(membership__user_id=assigned).exists()

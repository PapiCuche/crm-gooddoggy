"""El texto de traspaso vigente de una organización (F8-08l, #1046): el guardado si aún pasa los
detectores; si no, o sin guardado, el inicial (`AI_HANDOFF_REPLY`). Solo lee, sin permiso ni
auditoría: lo usan `replies` y, desde el arranque (F8-08l2, #1058), la solicitud de atención
humana (`conversations.handoff`), que no cierra nada por enviarlo."""

from dataclasses import dataclass
from datetime import datetime

from apps.ai_agents import detectors
from apps.ai_agents.models import AIHandoffReply
from core.observability.logging import get_logger
from core.tenancy.context import TenantContext
from core.tenancy.scope import require_scope

logger = get_logger(__name__)


@dataclass(frozen=True)
class Reply:
    text: str
    initial: bool  # el del propietario: sin texto guardado, o el guardado ya no vale
    changed_at: datetime | None


def effective(alias: str) -> Reply:
    """El que propone el copiloto, validado al usarlo."""
    row: AIHandoffReply | None = AIHandoffReply.objects.using(alias).first()
    if row is None:
        return Reply(detectors.handoff_reply(), True, None)
    if problems := detectors.reply_problems(row.text):
        logger.warning("ai.handoff_reply_invalid", problems=list(problems))
        return Reply(detectors.handoff_reply(), True, row.changed_at)
    return Reply(row.text, False, row.changed_at)


def in_scope(ctx: TenantContext) -> str:
    """El de la organización de `ctx`, en su `tenant_scope` activo (bajo su RLS)."""
    return effective(require_scope(ctx)).text

"""El barrido de «Conversación sin respuesta», paso 1 (F11-05a; ADR-049 §3, §5-§7), como
`SYSTEM` bajo RLS: cierra las ejecuciones cuyo episodio terminó, reintenta las fallidas o sin
destinatario y reclama las que esperan desde hace `first_after_minutes`, en horario. Cada sujeto va
en su transacción, relee su fila y vuelve a comprobar interruptores y regla (una pausa la detiene).
`episode_at` = el mayor entre `awaiting_reply_since` y el último `closed_at` del sujeto: con
`automation_runs_open_uq`, los casos a) y b) de F11-04 no abren otra ejecución y una hora que vuelve
a un episodio cerrado no choca con su clave. `SIMULATE` nunca crea tareas; `ON` se salta (F11-05b,
#1216). Horario en `America/Lima`: `Organization.timezone` (ADR-049 §5) aún no existe. Logs con
códigos y cifras, sin texto del cliente ni ids de sujetos."""

import datetime as dt
import time
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.db import connections

from apps.access.catalog import Scope
from apps.access.scopes import policy_for
from apps.access.selectors import AccessDenied, holders, member_context
from apps.automations import params as schema
from apps.automations.commands import runnable
from apps.automations.models import AutomationRule, Mode
from apps.conversations.models import Conversation
from apps.organizations.models import TeamMember
from core.observability.logging import get_logger
from core.tenancy.context import ActorType, TenantContext
from core.tenancy.scope import require_scope, tenant_scope

logger = get_logger(__name__)
KEY = schema.CONVERSATION_UNANSWERED
MAX_PER_PASS, RUN_SECONDS = 100, 60.0  # por pasada, organización y regla (§6)
ORG_TIMEZONE = "America/Lima"  # ADR-049 §5, por omisión; ver el docstring
USER = "membership__user_id"
LOCK = "SELECT pg_try_advisory_xact_lock(hashtextextended(%s, 0))"
RETRYABLE, OPEN = ["SKIPPED", "FAILED"], ["DONE", "SKIPPED", "FAILED", "DEAD"]
CANDIDATES = (  # esperan desde antes del corte, sin ejecución abierta en este modo
    "SELECT c.id, greatest(c.awaiting_reply_since, (SELECT max(r.closed_at) FROM automation_runs r"
    " WHERE r.organization_id = c.organization_id AND r.rule_id = %(rule)s"
    " AND r.subject_id = c.id AND r.step = 1)) FROM conversations c"
    " WHERE c.organization_id = %(org)s AND c.awaiting_reply_since IS NOT NULL"
    " AND c.awaiting_reply_since <= %(cutoff)s AND NOT EXISTS (SELECT 1 FROM automation_runs r"
    " WHERE r.organization_id = c.organization_id AND r.rule_id = %(rule)s"
    " AND r.subject_id = c.id AND r.step = 1 AND (r.mode = %(mode)s AND r.status <> 'CLOSED'"
    " OR r.closed_at > %(cutoff)s)) ORDER BY c.awaiting_reply_since, c.id LIMIT %(limit)s"
)
PENDING = (  # abiertas cuyo episodio terminó (cualquier modo) o que toca reintentar (este modo)
    "SELECT r.id, c.id, c.awaiting_reply_since IS NULL FROM automation_runs r JOIN conversations c"
    " ON c.organization_id = r.organization_id AND c.id = r.subject_id"
    " WHERE r.organization_id = %(org)s AND r.rule_id = %(rule)s AND r.step = 1"
    " AND r.status <> 'CLOSED' AND (c.awaiting_reply_since IS NULL OR (%(hours)s"
    " AND r.mode = %(mode)s AND (r.status = 'SKIPPED' OR (r.status = 'FAILED'"
    " AND r.updated_at <= %(now)s - make_interval(mins => (2 ^ r.attempts)::int)))))"
    " ORDER BY r.id LIMIT %(limit)s"
)
OPEN_PER_USER = (
    "SELECT u, count(*) FROM automation_runs r, unnest(r.recipients) u WHERE r.organization_id ="
    " %(org)s AND r.rule_id = %(rule)s AND r.mode = %(mode)s AND r.status = 'DONE' GROUP BY u"
)
LAST_CLOSED = (
    "SELECT max(closed_at) FROM automation_runs WHERE organization_id = %(org)s"
    " AND rule_id = %(rule)s AND subject_id = %(subject)s AND step = 1"
)
CLAIM = (
    "INSERT INTO automation_runs (organization_id, rule_id, rule_version, rule_key, subject_type,"
    " subject_id, episode_at, step, mode, status, reason, recipients, attempts, updated_at)"
    " VALUES (%(org)s, %(rule)s, %(version)s, %(key)s, 'conversation', %(subject)s, %(episode)s,"
    " 1, %(mode)s, %(status)s, %(reason)s, %(recipients)s, %(attempts)s, %(now)s)"
    " ON CONFLICT DO NOTHING RETURNING id"
)
LOCK_RUN = (
    "SELECT status, recipients, mode FROM automation_runs WHERE id = %s AND status = ANY(%s)"
    " FOR UPDATE SKIP LOCKED"
)
RETRY = (
    "UPDATE automation_runs SET status = %(status)s, reason = %(reason)s, recipients ="
    " %(recipients)s, rule_version = %(version)s, updated_at = %(now)s WHERE id = %(id)s"
)
CLOSE = (
    "UPDATE automation_runs SET status = 'CLOSED', reason = 'RESOLVED', closed_at = %(now)s,"
    " updated_at = %(now)s WHERE id = %(id)s"
)
FAIL = (
    "UPDATE automation_runs SET attempts = attempts + 1, status = CASE WHEN attempts + 1 >= 3"
    " THEN 'DEAD' ELSE 'FAILED' END, reason = %(reason)s, updated_at = %(now)s"  # §6
    " WHERE id = %(id)s AND status IN ('FAILED', 'SKIPPED') RETURNING status"
)
STORM = (
    "SELECT count(*) FROM automation_runs WHERE organization_id = %(org)s AND rule_id = %(rule)s"
    " AND created_at >= now() - interval '170 seconds'"  # unas tres pasadas de 60 s
)


class Stopped(Exception):  # noqa: N818 — pausa, interruptor global o la regla cambió
    """La pasada se detiene en el siguiente sujeto."""


@dataclass(frozen=True)
class Pass:
    ctx: TenantContext
    rule: UUID
    mode: str
    version: int
    params: dict[str, Any]
    now: dt.datetime  # el reloj de la base al empezar la pasada

    def sql(self, **more: Any) -> dict[str, Any]:
        cutoff = self.now - dt.timedelta(minutes=self.params["first_after_minutes"])
        return {"org": self.ctx.organization_id, "rule": self.rule, "mode": self.mode,
                "version": self.version, "key": KEY, "now": self.now, "cutoff": cutoff,
                **more}  # fmt: skip


def _sql(ctx: TenantContext, sql: str, args: Any) -> list[tuple[Any, ...]]:
    with connections[require_scope(ctx)].cursor() as cursor:
        cursor.execute(sql, args)
        return cursor.fetchall() if cursor.description else []


def clock(ctx: TenantContext) -> dt.datetime:
    """`now()` de la base, una vez por pasada (las pruebas lo mueven)."""
    return _sql(ctx, "SELECT now()", [])[0][0]  # type: ignore[no-any-return]


def in_hours(now: dt.datetime, zone: str, params: dict[str, Any]) -> bool:
    """¿`now` está en horario, en la hora local de `zone` (con DST)? Una zona desconocida, no."""
    try:
        local = now.astimezone(ZoneInfo(zone))
    except ZoneInfoNotFoundError, ValueError:
        logger.error("automations.bad_timezone")
        return False
    hhmm = f"{local:%H:%M}"
    return local.isoweekday() in params["days"] and params["opens_at"] <= hhmm < params["closes_at"]


def _sees(ctx: TenantContext, user: UUID, conversation: Conversation) -> bool:
    """`can()` de `conversations.view` para ese miembro activo, con su alcance (OWN, TEAM)."""
    try:
        member = member_context(ctx, user)
    except AccessDenied:
        return False
    scopes = member.permissions.get("conversations.view") or frozenset()
    if Scope.ORGANIZATION in scopes:
        return True
    acting = replace(ctx, user_id=user, actor_type=ActorType.USER, actor_id=user)
    policy = policy_for(Conversation)
    return any(policy.allows(s, replace(member, tenant=acting), conversation) for s in scopes if s)


def recipients(ctx: TenantContext, conversation: Conversation, limit: int) -> list[UUID]:
    """D-F11-04, E1: asignada, si no supervisores del equipo, si no `conversations.assign`; solo
    quien ve la conversación, por id, hasta `limit`."""

    def seeing(users: list[UUID]) -> list[UUID]:
        found: list[UUID] = []
        for user in users:
            found += [user] if len(found) < limit and _sees(ctx, user, conversation) else []
        return found

    if conversation.assigned_user_id and (mine := seeing([conversation.assigned_user_id])):
        return mine
    if conversation.assigned_team_id:
        team = TeamMember.objects.using(require_scope(ctx)).filter(
            team_id=conversation.assigned_team_id, team_role="SUPERVISOR", is_active=True
        )
        if found := seeing(list(team.order_by(USER).values_list(USER, flat=True))):
            return found
    return seeing([user for user, _ in holders(ctx, "conversations.assign")])


@contextmanager
def _subject(run: Pass) -> Iterator[TenantContext]:
    """La transacción de un sujeto: `lock_timeout` y, otra vez, los interruptores y la regla."""
    with tenant_scope(run.ctx) as tenant:
        _sql(tenant, "SELECT set_config('lock_timeout', '5s', true)", [])
        if not any((r.pk, r.mode, r.version) == (run.rule, run.mode, run.version)
                   for r in runnable(tenant)):  # fmt: skip
            raise Stopped
        yield tenant


def close(run: Pass, subject: UUID, run_id: UUID | None, counts: Counter[UUID]) -> str:
    """Cierra la ejecución si la conversación, releída, ya no espera; libera el tope por persona."""
    with _subject(run) as tenant:
        locked = _sql(tenant, LOCK_RUN, [run_id, OPEN])
        found = Conversation.objects.using(require_scope(tenant)).filter(pk=subject)
        if not locked or found.filter(awaiting_reply_since__isnull=False).exists():
            return "skipped"
        _sql(tenant, CLOSE, run.sql(id=run_id))
    if (locked[0][0], locked[0][2]) == ("DONE", run.mode):
        counts.subtract(locked[0][1])
    return "closed"


def claim(run: Pass, subject: UUID, run_id: UUID | None, counts: Counter[UUID]) -> str:
    """Reclama o reintenta el aviso de una conversación que sigue esperando (sin tareas)."""
    with _subject(run) as tenant:
        if run_id is not None and not _sql(tenant, LOCK_RUN, [run_id, RETRYABLE]):
            return "skipped"
        rows = Conversation.objects.using(require_scope(tenant)).filter(pk=subject)
        conversation = rows.first()  # la fila de ahora, no la de la selección
        if conversation is None or (waiting := conversation.awaiting_reply_since) is None:
            return "skipped"
        last = _sql(tenant, LAST_CLOSED, run.sql(subject=subject))[0][0]
        episode = max(waiting, last or waiting)
        if run_id is None and episode > run.sql()["cutoff"]:
            return "skipped"
        chosen = recipients(tenant, conversation, run.params["max_recipients"])
        kept = [user for user in chosen if counts[user] < run.params["max_open_per_user"]]
        reason = None if kept else "CAPPED" if chosen else "NO_RECIPIENT"
        args = run.sql(subject=subject, episode=episode, status="SKIPPED" if reason else "DONE",
                       reason=reason, recipients=kept, attempts=0, id=run_id)  # fmt: skip
        if run_id is not None:
            _sql(tenant, RETRY, args)
        elif not _sql(tenant, CLAIM, args):
            return "skipped"  # otra pasada ya la tiene (o una ejecución abierta)
        counts.update(kept)
        return (reason or "simulated").lower()


def fail(run: Pass, subject: UUID, run_id: UUID | None, episode: Any, error: Exception) -> str:
    """Anota el fallo en otra transacción: `FAILED` con `attempts + 1`, y `DEAD` al tercero."""
    cause = error.__cause__ or error
    reason = "LOCK_TIMEOUT" if getattr(cause, "sqlstate", None) == "55P03" else "DB_ERROR"
    org, kind = str(run.ctx.organization_id), type(error).__name__
    logger.warning("automations.subject_failed", organization_id=org, rule=KEY, error=kind)
    try:
        with tenant_scope(run.ctx) as tenant:
            if run_id is None:
                _sql(tenant, CLAIM, run.sql(subject=subject, episode=episode, status="FAILED",
                                            reason=reason, recipients=[], attempts=1))  # fmt: skip
                return "failed"
            done = _sql(tenant, FAIL, run.sql(reason=reason, id=run_id))
    except Exception:  # noqa: BLE001 — el siguiente barrido lo vuelve a ver
        logger.error("automations.failure_not_recorded", organization_id=org, rule=KEY)
        return "failed"
    if done and done[0][0] == "DEAD":
        logger.error("automations.run_dead", organization_id=org, rule=KEY, reason=reason)
        return "dead"
    return "failed"


def run_pass(ctx: TenantContext, rule: AutomationRule, now: dt.datetime) -> Counter[str]:
    """Una pasada de la regla en `SIMULATE`: cierres, reintentos y avisos nuevos, con topes."""
    params = schema.validate(KEY, {**schema.defaults(KEY), **rule.params})
    run = Pass(ctx, rule.pk, rule.mode, rule.version, params, now)
    with tenant_scope(ctx) as tenant:
        lock = f"automations:{ctx.organization_id}:{KEY}"
        if not _sql(tenant, LOCK, [lock])[0][0]:  # otra pasada selecciona ahora
            return Counter(skipped_locked=1)
        hours = in_hours(now, ORG_TIMEZONE, params)
        pending = _sql(tenant, PENDING, run.sql(hours=hours, limit=MAX_PER_PASS))
        budget = MAX_PER_PASS - sum(1 for *_, ended in pending if not ended)
        fresh = _sql(tenant, CANDIDATES, run.sql(limit=budget + 1)) if hours and budget > 0 else []
        counts = (
            Counter(dict(_sql(tenant, OPEN_PER_USER, run.sql()))) if pending or fresh else Counter()
        )
    pending.sort(key=lambda row: not row[2])  # primero los cierres: liberan el tope por persona
    work = [(r, s, None, ended) for r, s, ended in pending] + [
        (None, s, episode, False) for s, episode in fresh[:budget]
    ]
    done: Counter[str] = Counter()
    started = time.monotonic()
    for run_id, subject, episode, ended in work:
        if time.monotonic() - started > RUN_SECONDS:
            done["timeout"] += 1
            break
        try:
            outcome = (close if ended else claim)(run, subject, run_id, counts)
        except Stopped:
            done["stopped"] += 1
            break
        except Exception as error:  # noqa: BLE001 — un sujeto no detiene el resto
            outcome = fail(run, subject, run_id, episode, error)
        done[outcome] += 1
    if hours and len(fresh) > budget:
        done["capped_pass"] += 1
        with tenant_scope(ctx) as tenant:
            made = _sql(tenant, STORM, run.sql())[0][0]
        if made >= 3 * MAX_PER_PASS:  # tres pasadas seguidas en el tope; nadie la apaga sola
            logger.error("automations.storm", organization_id=str(ctx.organization_id), rule=KEY,
                         runs=made)  # fmt: skip
    return done


def evaluate(ctx: TenantContext) -> Counter[str]:
    """Las reglas que se pueden ejecutar ahora en la organización de `ctx`, una pasada cada una."""
    with tenant_scope(ctx) as tenant:
        rules = [rule for rule in runnable(tenant) if rule.key == KEY]
        now = clock(tenant)
    done: Counter[str] = Counter()
    for rule in rules:
        if rule.mode != Mode.SIMULATE:
            done["on_pending"] += 1  # crear tareas en `ON` es de F11-05b (#1216)
            continue
        done.update(run_pass(ctx, rule, now))
    logger.info("automations.swept", organization_id=str(ctx.organization_id), rule=KEY, **done)
    return done

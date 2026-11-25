"""La caducidad del token de una página de Messenger o Instagram y sus avisos (F7b-12; ADR-035 §3).

`debug_token` da `expires_at` (`0`, nunca) y `data_access_expires_at`, que solo cuenta en Instagram
(una lectura de la lista de Meta de permisos que no caducan). `expires_at` guarda la más temprana;
`expiry_checked_at` `NULL`, no se sabe. Se lee al conectar, al rotar y, si falta, a diario.
`sweep` (cada hora) avisa (correo a `channels.manage` y alerta) a 10 y 3 días, una vez por umbral
y token: un `UPDATE` condicional reclama el umbral con su auditoría; el correo sale tras confirmar
y no se reintenta. Nada lleva el token, su final ni el app secret.
F7b-12c: solo entra lo que tiene un aviso pendiente o (a diario) una caducidad que se puede
preguntar; cada pregunta se reclama antes (`expiry_attempt_at`) y no se repite en `WAIT`, y una
pasada no pasa de `PASS_SECONDS`.
"""

import datetime as dt
from collections import Counter
from time import monotonic
from typing import Any, NamedTuple
from uuid import UUID

from django.conf import settings
from django.db import connection
from django.db.models import F, Q
from django.db.models.functions import Now

from apps.access.selectors import holders
from apps.audit.services import Entity, record
from apps.conversations import credentials, graph
from apps.conversations.models import SOCIAL, WARN_DAYS, ChannelAccount, Credential, Platform
from apps.organizations.models import Organization
from core import mail, sealed
from core.observability import reporting
from core.observability.logging import get_logger
from core.tenancy.context import ActorType, TenantContext
from core.tenancy.resolution import ACCESSIBLE_STATUSES
from core.tenancy.scope import require_scope, tenant_scope

logger = get_logger(__name__)
CHECKED = ("PENDING", "CONNECTED", "DEGRADED")  # las que se comprueban (ADR-029 §12)
MAX_ORGS, BATCH = 50, 20  # organizaciones por pasada y credenciales por organización
WAIT = dt.timedelta(hours=23)  # entre dos preguntas a Meta por una credencial (la de la 0065)
PASS_SECONDS = 120  # lo máximo que una pasada retiene un worker, como `HEALTH_PASS_SECONDS`
LOUD = ("no_app", "app_token_rejected", "malformed", "other_app")  # de la configuración: aviso
EVENT, ACTION = "channel.alert.token_expiring", "channel_account.token_expiring"
LINK_PATH = "/o/{slug}/canales"
SUBJECT = "El token de un canal del CRM caduca en menos de {days} días"
EXPIRED = "El token de un canal del CRM caducó"
BODY = (
    "Hola:\n\nEl token de la cuenta de {platform} «{account}» de «{organization}» {news} el "
    "{date} (UTC).\n\nSin un token válido, el CRM deja de enviar por esa cuenta. Cambia el token "
    "en:\n\n{link}\n\nRecibes este correo porque gestionas los canales de esta organización. "
    "Cada aviso se envía una sola vez por token.\n"
)


class Known(NamedTuple):
    """Una caducidad leída en Meta: `at` `None`, no caduca."""

    at: dt.datetime | None


def read(platform: str, token: str, **ids: str) -> Known | None:
    """La caducidad que cuenta para una cuenta de `platform` con `token`; `None`, no se sabe.
    `ids` (organización y cuenta, si ya existen) van al registro."""
    seen = graph.token_expiry(token)
    if isinstance(seen, str):  # solo el motivo y los ids; los de configuración, como aviso
        level = "warning" if seen in LOUD else "info"
        getattr(logger, level)("channel.token_expiry_unknown", reason=seen, **ids)
        return None
    counted = [seen.expires_at]
    if platform == Platform.INSTAGRAM:
        counted.append(seen.data_access_expires_at)
    return Known(min((at for at in counted if at is not None), default=None))


def warning_due() -> Q:
    """Un aviso pendiente, exacto: sin aviso y a 10 días, o avisada a 10 y a 3 días (la `0065`)."""
    first, last = max(WARN_DAYS), min(WARN_DAYS)
    return Q(expiry_warned_days__isnull=True, expires_at__lte=Now() + dt.timedelta(days=first)) | Q(
        expiry_warned_days=first, expires_at__lte=Now() + dt.timedelta(days=last)
    )


def askable() -> Q:
    """Una caducidad sin leer que se puede preguntar: sin intento en `WAIT` (futuro: ninguno)."""
    waited = Q(expiry_attempt_at__isnull=True) | Q(expiry_attempt_at__lte=Now() - WAIT)
    return Q(waited | Q(expiry_attempt_at__gt=Now()), expiry_checked_at__isnull=True)


def _targets(ctx: TenantContext, wanted: Q, *order: Any) -> list[tuple[ChannelAccount, Credential]]:
    """Las credenciales vivas que cumplen `wanted` de cuentas sociales vivas que se comprueban de
    una organización accesible, en el orden `order` (el turno)."""
    alias = require_scope(ctx)
    orgs = Organization.objects.using(alias).filter(status__in=ACCESSIBLE_STATUSES)
    if not orgs.filter(pk=ctx.organization_id).exists():
        return []
    found = ChannelAccount.objects.using(alias).filter(
        platform__in=SOCIAL, deleted_at__isnull=True, status__in=CHECKED,
        credential_id__isnull=False,
    )  # fmt: skip
    accounts = {a.credential_id: a for a in found}
    live = Credential.objects.using(alias).filter(wanted, pk__in=accounts, revoked_at__isnull=True)
    rows = live.order_by(*order, "pk")[:BATCH]
    return [(accounts[c.pk], c) for c in rows if accounts[c.pk].pk == c.owner_id]


def _backfill(organization_id: UUID, done: Counter[str], deadline: float) -> None:
    """Lee en Meta la caducidad que no se sabe, el intento más viejo primero. Cada pregunta se
    reclama antes (confirmada): si Meta no contesta o el worker cae, no se repite en `WAIT`; otra
    pasada a la vez no la reclama. El token se abre solo durante la llamada."""
    ctx = TenantContext(organization_id, "celery")
    with tenant_scope(ctx):
        oldest = F("expiry_attempt_at").asc(nulls_first=True)
        rows = _targets(ctx, askable(), oldest, "created_at")
    for account, credential in rows:
        if monotonic() > deadline:
            done["OUT_OF_TIME"] = 1
            return
        with tenant_scope(ctx):
            claim = Credential.objects.using(require_scope(ctx)).filter(
                askable(), pk=credential.pk, revoked_at__isnull=True
            )
            if not claim.update(expiry_attempt_at=Now()):
                continue  # otra pasada la reclamó, o ya se leyó
        try:
            token = credentials.open_token(credential)
        except sealed.SealError:
            done["UNREADABLE"] += 1
            continue
        try:
            ids = {"organization_id": str(organization_id), "channel_account_id": str(account.pk)}
            known = read(account.platform, token, **ids)
        finally:
            del token
        if known is None:
            done["UNKNOWN"] += 1
            continue
        with tenant_scope(ctx):
            fresh = Credential.objects.using(require_scope(ctx)).filter(
                pk=credential.pk, revoked_at__isnull=True, expiry_checked_at__isnull=True
            )
            done["READ"] += fresh.update(expiry_checked_at=Now(), expires_at=known.at)


def _warn(organization_id: UUID) -> list[dict[str, Any]]:
    """Reclama y audita los avisos que tocan en la organización; devuelve lo que se enviará."""
    ctx = TenantContext(organization_id, "celery", actor_type=ActorType.SYSTEM)
    claimed: list[dict[str, Any]] = []
    with tenant_scope(ctx):
        alias = require_scope(ctx)
        rows = _targets(ctx, warning_due(), "expires_at", "created_at")  # la más urgente primero
        org = Organization.objects.using(alias).filter(
            pk=organization_id, status__in=ACCESSIBLE_STATUSES
        ).values_list("slug", "name").first()  # fmt: skip
        if not rows or org is None:
            return []
        with connection.cursor() as cursor:  # el reloj de la base, el de los umbrales
            cursor.execute("SELECT statement_timestamp()")
            now = cursor.fetchone()[0]
        for account, credential in rows:
            for days in sorted(WARN_DAYS):  # el más urgente primero: uno solo por pasada
                pending = Q(expiry_warned_days__isnull=True) | Q(expiry_warned_days__gt=days)
                claim = Credential.objects.using(alias).filter(
                    pending, pk=credential.pk, revoked_at__isnull=True,
                    expires_at__lte=Now() + dt.timedelta(days=days),
                )  # fmt: skip
                if claim.update(expiry_warned_days=days):
                    break
            else:
                continue  # otro barrido ya lo reclamó
            expired = credential.expires_at is not None and credential.expires_at <= now
            ids = {"credential_id": str(credential.pk), "days": days, "expired": expired}
            entity = Entity("channel_account", account.pk, account.name)
            record(ctx, ACTION, entity, metadata=ids, actor=(ActorType.SYSTEM, None))
            claimed.append({
                "account_id": account.pk, "platform": account.platform, "account": account.name,
                "at": credential.expires_at, **ids,
            })  # fmt: skip
        to = [email for _, email in holders(ctx, "channels.manage") if mail.deliverable(email)]
    for warning in claimed:
        warning |= {"slug": org[0], "organization": org[1], "to": to}
    return claimed


def _send(organization_id: UUID, warning: dict[str, Any]) -> None:
    """La alerta de operaciones (solo ids y días) y el correo a quien gestiona los canales."""
    context = {
        "organization_id": str(organization_id), "channel_account_id": str(warning["account_id"]),
        "platform": warning["platform"], "days": warning["days"], "expired": warning["expired"],
    }  # fmt: skip
    logger.error(EVENT, **context)
    try:
        reporting.reporter().capture_message(EVENT, context, fingerprint=[EVENT])
    except Exception:  # noqa: BLE001 — un reporte roto no frena el correo
        logger.warning("channel.alert.report_failed", alert="token_expiring")
    if not settings.APP_ORIGIN or not mail.configured():
        logger.warning("channel.token_expiry_unsent", recipients=len(warning["to"]), **context)
        return
    expired = warning["expired"]
    body = BODY.format(
        platform=warning["platform"].capitalize(), account=warning["account"],
        organization=warning["organization"], news="caducó" if expired else "caduca",
        date=warning["at"].strftime("%d/%m/%Y %H:%M"),
        link=settings.APP_ORIGIN + LINK_PATH.format(slug=warning["slug"]),
    )  # fmt: skip
    subject = EXPIRED if expired else SUBJECT.format(days=warning["days"])
    for to in warning["to"]:
        try:
            mail.send(mail.Message(to=to, subject=subject, body=body, purpose="channel_token"))
        except mail.MailError, ValueError:  # solo el tipo; las demás siguen
            logger.warning("channel.token_expiry_mail_failed", **context)


def sweep(*, backfill: bool = False) -> dict[str, int]:
    """Con `backfill`, primero las caducidades que no se saben; después, los avisos. Las
    organizaciones, de `credentials_expiry_due` (SECURITY DEFINER, solo ids), el turno más viejo
    primero. Un fallo en una organización no detiene las demás y solo se registra su tipo. Pasados
    `PASS_SECONDS` no empieza otra organización ni otra pregunta (`OUT_OF_TIME`): sigue la próxima
    pasada. Nunca corta entre reclamar un aviso y enviarlo."""
    if not settings.META_ENABLED:
        return {}
    with connection.cursor() as cursor:
        cursor.execute("SELECT public.credentials_expiry_due(%s, %s)", [backfill, MAX_ORGS])
        organizations = [UUID(str(row[0])) for row in cursor.fetchall()]
    done: Counter[str] = Counter()
    deadline = monotonic() + PASS_SECONDS
    for organization_id in organizations:
        if monotonic() > deadline:
            done["OUT_OF_TIME"] = 1
            break
        try:
            if backfill:
                _backfill(organization_id, done, deadline)
            for warning in _warn(organization_id):
                _send(organization_id, warning)
                done["WARNED"] += 1
        except Exception as error:  # noqa: BLE001 — nada del error: puede llevar la URL
            logger.error("channel.token_expiry_failed", error=type(error).__name__)
            done["ERROR"] += 1
    logger.info("channel.token_expiry_swept", backfill=backfill, **done)
    return dict(done)

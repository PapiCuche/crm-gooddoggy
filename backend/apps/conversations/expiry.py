"""La caducidad del token de una página de Messenger o Instagram y sus avisos (F7b-12; ADR-035 §3).

Meta la da en `debug_token` (`graph.token_expiry`): `expires_at` (`0`, nunca) y
`data_access_expires_at`, que solo cuenta en Instagram (los permisos de Messenger están en la lista
de Meta de los que no caducan por el acceso a datos; los de Instagram, no: una lectura de la
documentación). `credentials.expires_at` es la más temprana; `expiry_checked_at` `NULL`, no se sabe.
Se lee al conectar y al rotar; si no se pudo, `sweep(backfill=True)` (a diario) lo reintenta.

`sweep` (cada hora) avisa por correo a quien tiene `channels.manage` y a operaciones (como las
alertas de la ingesta) a 10 y a 3 días, una vez por umbral y token: un `UPDATE` condicional reclama
el umbral (de dos barridos a la vez, gana uno) con su auditoría en la misma transacción; el correo
sale tras confirmar y no se reintenta. Un token ya caducado recibe solo el último, con `expired`.
Rotar crea otra credencial, sin avisos. Nada lleva el token, su final ni el app secret.
"""

import datetime as dt
from collections import Counter
from typing import Any, NamedTuple
from uuid import UUID

from django.conf import settings
from django.db import connection
from django.db.models import Q
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


def read(platform: str, token: str) -> Known | None:
    """La caducidad que cuenta para una cuenta de `platform` con `token`; `None`, no se sabe."""
    seen = graph.token_expiry(token)
    if isinstance(seen, str):  # solo el motivo; los de configuración, como aviso
        level = "warning" if seen in ("no_app", "app_token_rejected", "malformed") else "info"
        getattr(logger, level)("channel.token_expiry_unknown", reason=seen)
        return None
    counted = [seen.expires_at]
    if platform == Platform.INSTAGRAM:
        counted.append(seen.data_access_expires_at)
    return Known(min((at for at in counted if at is not None), default=None))


def _targets(alias: str, wanted: Q) -> list[tuple[ChannelAccount, Credential]]:
    """Las credenciales vivas que cumplen `wanted` de las cuentas sociales vivas que se
    comprueban, con su cuenta, en el scope de `alias`."""
    found = ChannelAccount.objects.using(alias).filter(
        platform__in=SOCIAL, deleted_at__isnull=True, status__in=CHECKED,
        credential_id__isnull=False,
    )  # fmt: skip
    accounts = {a.credential_id: a for a in found}
    live = Credential.objects.using(alias).filter(wanted, pk__in=accounts, revoked_at__isnull=True)
    rows = live.order_by("expires_at", "created_at")[:BATCH]  # la más urgente primero
    return [(accounts[c.pk], c) for c in rows if accounts[c.pk].pk == c.owner_id]


def _backfill(organization_id: UUID, done: Counter[str]) -> None:
    """Lee en Meta la caducidad que no se sabe; el token se abre solo durante la llamada."""
    ctx = TenantContext(organization_id, "celery")
    with tenant_scope(ctx):
        rows = _targets(require_scope(ctx), Q(expiry_checked_at__isnull=True))
    for account, credential in rows:
        try:
            token = credentials.open_token(credential)
        except sealed.SealError:
            done["UNREADABLE"] += 1
            continue
        try:
            known = read(account.platform, token)
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
        due = Q(expires_at__lte=Now() + dt.timedelta(days=max(WARN_DAYS)))
        due &= Q(expiry_warned_days__isnull=True) | Q(expiry_warned_days__gt=min(WARN_DAYS))
        rows = _targets(alias, due)
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
    organizaciones, de `credentials_expiry_due` (SECURITY DEFINER, solo ids). Un fallo en una
    organización no detiene las demás y solo se registra su tipo."""
    if not settings.META_ENABLED:
        return {}
    with connection.cursor() as cursor:
        cursor.execute("SELECT public.credentials_expiry_due(%s, %s)", [backfill, MAX_ORGS])
        organizations = [UUID(str(row[0])) for row in cursor.fetchall()]
    done: Counter[str] = Counter()
    for organization_id in organizations:
        try:
            if backfill:
                _backfill(organization_id, done)
            for warning in _warn(organization_id):
                _send(organization_id, warning)
                done["WARNED"] += 1
        except Exception as error:  # noqa: BLE001 — nada del error: puede llevar la URL
            logger.error("channel.token_expiry_failed", error=type(error).__name__)
            done["ERROR"] += 1
    logger.info("channel.token_expiry_swept", backfill=backfill, **done)
    return dict(done)

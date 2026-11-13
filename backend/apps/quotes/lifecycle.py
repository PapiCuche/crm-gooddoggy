"""Emitir una cotización (F5-09c2; 04 §L.2). Solo `apps.quotes.api` lo importará. Orden: el permiso
(`quotes.send`, antes de mirar lo que se envía), la cotización bloqueada y a su **alcance**
(`quotes.view` y `quotes.send`: una que no existe, de otra organización o fuera del alcance es la
misma `DoesNotExist`), la versión (`StaleVersion`), el estado (`NotIssuable`) y la validez.

**Emitir** pasa `APPROVED`, o un `DRAFT` que quien emite puede enviar sin aprobación, a `SENT`.
Antes de tocar el costo, quien emite ha de ver al contacto (`contacts.view` con su alcance): si no
existe, está borrado o no lo ve es el mismo `BadReference("contact_id")`, y solo se bloquea si lo
ve. Los precios manuales vuelven a pasar por el suelo de costo (`PriceRefused`, sin cifras). Desde
`DRAFT` se vuelve a medir con quien emite: un autor humano, sin precio manual y dentro de **su**
límite; si no, `NeedsApproval` sin escribir nada. Lo de la IA (sin autor) solo sale de `APPROVED`
con la última solicitud aprobada. Congela la fotografía de la empresa (nombre), la del cliente
(nombre y documento, nada más), las condiciones y la garantía (texto de quien emite) y la validez,
que ha de ser futura. Audita `quote.issued` y emite el evento `quote.issued` (solo ids), en la
misma transacción."""

from typing import Any
from uuid import UUID

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.access.selectors import can
from apps.audit.services import record
from apps.contacts.models import Contact
from apps.organizations.models import Organization
from apps.quotes.commands import (
    BadReference,
    StaleVersion,
    _entity,
    _event,
    _integer,
    _moment,
    _reachable,
    _reference,
    _require,
    _stamp,
    _text,
    _uuid,
    _within,
)
from apps.quotes.discounts import _floor, _lines, _verdict
from apps.quotes.models import ApprovalStatus, Quote, QuoteApproval, QuoteStatus
from core.tenancy.context import TenantContext
from core.tenancy.scope import require_scope

TERMS_MAX, WARRANTY_MAX = 2000, 500


class NotIssuable(Exception):
    """Solo se emite una cotización aprobada o un borrador."""


class NeedsApproval(Exception):
    """Para salir, esta cotización necesita antes una aprobación."""


def _contact(alias: str, ectx: Any, contact_id: UUID) -> Contact:
    """El contacto vivo que quien emite ve, bloqueado. Se lee sin bloquear y solo si lo ve se
    bloquea (y se vuelve a mirar): quien no lo ve no espera ni distingue si existe."""
    live = Contact.objects.using(alias).filter(pk=contact_id, deleted_at__isnull=True)
    seen = live.first()
    if seen is not None and can(ectx, "contacts.view", seen):
        found: Contact | None = live.select_for_update(no_key=True).first()
        if found is not None and can(ectx, "contacts.view", found):
            return found
    raise BadReference("contact_id")  # el mismo error: no existe, borrado o no lo ve


def _approved(alias: str, quote: Quote) -> bool:
    latest = QuoteApproval.objects.using(alias).filter(quote_id=quote.pk)
    found = latest.order_by("-created_at", "-id").values_list("status", flat=True).first()
    return found == ApprovalStatus.APPROVED


def issue_quote(
    ctx: TenantContext, *, quote_id: UUID, version: int, valid_until: Any = None,
    terms: str | None = None, warranty: str | None = None,
) -> Quote:  # fmt: skip
    """Emite la cotización (`SENT`) con `quotes.send`: fotografías, `sent_at` y validez (la dada o
    la que ya tiene; futura, o `ValueError`). `NotAllowed`, `DoesNotExist`, `StaleVersion`,
    `NotIssuable`, `BadReference`, `PriceRefused`, `NeedsApproval`; si falla, nada se escribe."""
    alias = require_scope(ctx)
    ectx = _require(ctx, "quotes.send")  # lo primero: sin permiso, ni se mira lo que se envía
    quote_id = _uuid(quote_id, "cotización")
    _integer(version, 1, 2**31 - 1, "versión")
    until = None if valid_until is None else _moment(valid_until)
    words = [None if text is None else _text(text, what, top) or None
             for text, what, top in ((terms, "condiciones", TERMS_MAX),
                                     (warranty, "garantía", WARRANTY_MAX))]  # fmt: skip
    try:
        with transaction.atomic(using=alias):
            quote = _reachable(alias, quote_id, _within(ectx, "quotes.view", "quotes.send"))
            if quote.version != version:
                raise StaleVersion(quote_id)
            if quote.status not in (QuoteStatus.DRAFT, QuoteStatus.APPROVED):
                raise NotIssuable(quote_id)
            now, until = timezone.now(), until or quote.valid_until
            if until is None or until <= now:
                raise ValueError("validez: una fecha futura")
            contact = _contact(alias, ectx, quote.contact_id)
            lines = _lines(alias, quote)
            _floor(ctx, quote, lines)
            if quote.status == QuoteStatus.DRAFT:  # se vuelve a medir, con quien emite
                if _verdict(alias, ectx, quote, lines)[3] is not None:
                    raise NeedsApproval(quote_id)
            elif quote.created_by_user_id is None and not _approved(alias, quote):
                raise NeedsApproval(quote_id)
            company = Organization.objects.using(alias).get(pk=ctx.organization_id)
            before, was = quote.status, quote.valid_until
            _stamp(ctx, alias, quote, {
                "status": QuoteStatus.SENT, "sent_at": now, "valid_until": until,
                "company_snapshot": {"name": company.name},
                "customer_snapshot": {
                    "display_name": contact.display_name, "document_type": contact.document_type,
                    "document_number": contact.document_number,
                },
                "terms_snapshot": words[0], "warranty_snapshot": words[1],
            })  # fmt: skip
            record(ctx, "quote.issued", _entity(quote), {
                "status": [before, QuoteStatus.SENT], "valid_until": [was, until],
                "sent_at": [None, now], "snapshots": [None, "[congeladas]"],
                **{k: [None, "[texto]"] for k, v in zip(("terms", "warranty"), words, strict=True)
                   if v},
            })  # fmt: skip
            _event(ctx, quote, "quote.issued")
    except IntegrityError as error:
        raise _reference(error) from None
    return quote

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
misma transacción.

**Revisar** (F5-09c4) nunca edita lo emitido: con `quotes.create`, `quotes.update` y `quotes.cancel`
(y, si ya salió, `quotes.send`) sobre la cotización bloqueada, una `APPROVED`, `SENT` o `VIEWED`
pasa a `CANCELLED` y nace la revisión siguiente del mismo número, en `DRAFT`, revalorada por el
`PricingService` en su lista a la hora de ahora, sin descuentos, precios manuales, aprobaciones,
fotografías ni validez: para salir vuelve a pasar por enviar o emitir (que miden con quien actúa;
la de la IA, que sigue sin autor, pide aprobación). Un borrador no se revisa: se sigue editando."""

from typing import Any
from uuid import UUID

from django.db import IntegrityError, connections, transaction
from django.utils import timezone

from apps.access.selectors import can
from apps.audit.services import record
from apps.contacts.models import Contact
from apps.organizations.models import Organization
from apps.quotes.commands import (
    TAX_RATE,
    BadReference,
    NotAllowed,
    StaleVersion,
    _entity,
    _event,
    _integer,
    _links,
    _moment,
    _reachable,
    _reference,
    _require,
    _shown,
    _stamp,
    _text,
    _totals,
    _uuid,
    _within,
)
from apps.quotes.commands import _lines as _priced
from apps.quotes.discounts import _floor, _lines, _verdict
from apps.quotes.models import ApprovalStatus, Quote, QuoteApproval, QuoteItem, QuoteStatus
from core.tenancy.context import TenantContext
from core.tenancy.scope import require_scope

TERMS_MAX, WARRANTY_MAX = 2000, 500
REVISION_MAX = 32767  # la columna es `smallint`
REVISABLE = (QuoteStatus.APPROVED, QuoteStatus.SENT, QuoteStatus.VIEWED)


class NotIssuable(Exception):
    """Solo se emite una cotización aprobada o un borrador."""


class NeedsApproval(Exception):
    """Para salir, esta cotización necesita antes una aprobación."""


class NotRevisable(Exception):
    """Solo se revisa una cotización aprobada o emitida que sigue abierta."""


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


def revise_quote(ctx: TenantContext, *, quote_id: UUID, version: int) -> Quote:
    """La revisión siguiente (`DRAFT`) de una `APPROVED`, `SENT` o `VIEWED`, que queda `CANCELLED`
    en la misma transacción; un borrador se devuelve tal cual, sin escribir. `NotAllowed`,
    `DoesNotExist`, `StaleVersion`, `NotRevisable`, `BadReference`, `ValueError`; audita
    `quote.cancelled` y `quote.revised` (con el total de antes y el de ahora) y sus eventos."""
    alias = require_scope(ctx)
    _require(ctx, "quotes.create")  # lo primero: sin permiso, ni se mira lo que se envía
    ectx = _require(ctx, "quotes.update")
    quote_id = _uuid(quote_id, "cotización")
    _integer(version, 1, 2**31 - 1, "versión")
    try:
        with transaction.atomic(using=alias):
            old = _reachable(alias, quote_id, _within(ectx, "quotes.view", "quotes.update"))
            if old.version != version:
                raise StaleVersion(quote_id)
            if old.status == QuoteStatus.DRAFT:
                return old  # se sigue editando: ninguna fila nueva
            kept = QuoteItem.objects.using(alias).filter(quote_id=old.pk).order_by("sort_order")
            wanted = list(kept.values_list("variant_id", "quantity"))
            if old.status not in REVISABLE or old.revision >= REVISION_MAX or not wanted:
                raise NotRevisable(quote_id)
            if old.status != QuoteStatus.APPROVED and not can(ectx, "quotes.send", old):
                raise NotAllowed("quotes.send")  # ya la tiene el cliente
            if not can(ectx, "quotes.cancel", old):  # revisar cancela la anterior
                raise NotAllowed("quotes.cancel")
            _links(alias, old.contact_id, old.opportunity_id)
            rows, price = _priced(alias, ctx, wanted, old.price_list_id)
            totals = _totals(rows, TAX_RATE)
            with connections[alias].cursor() as cursor:  # el reloj de la transacción: la base
                cursor.execute("SELECT now()")  # exige que la anterior se cancele en ella
                now = cursor.fetchone()[0]
            before = old.status
            _stamp(ctx, alias, old, {"status": QuoteStatus.CANCELLED, "cancelled_at": now})
            new: Quote = Quote.objects.using(alias).create(
                number=old.number, revision=old.revision + 1, contact_id=old.contact_id,
                opportunity_id=old.opportunity_id, price_list_id=old.price_list_id,
                currency=price.currency, tax_rate=TAX_RATE,
                created_by_user_id=old.created_by_user_id,  # su alcance; la IA sigue sin autor
                updated_by_user_id=ctx.user_id, notes_to_customer=old.notes_to_customer,
                internal_notes=old.internal_notes, **totals,
            )  # fmt: skip
            QuoteItem.objects.using(alias).bulk_create(
                QuoteItem(organization_id=ctx.organization_id, quote=new, **row) for row in rows
            )
            record(ctx, "quote.cancelled", _entity(old), {"status": [before, old.status]},
                   {"revised_to": str(new.pk)})  # fmt: skip
            record(ctx, "quote.revised", _entity(new), {
                "status": [None, new.status], "revision": [old.revision, new.revision],
                "total": [old.total, new.total], "items": [None, _shown(rows)],
                **{f: [None, "[texto]"] for f in ("notes_to_customer", "internal_notes")
                   if getattr(new, f)},
            }, {"previous_quote_id": str(old.pk), "total_rose": new.total > old.total})  # fmt: skip
            _event(ctx, old, "quote.cancelled")
            _event(ctx, new, "quote.revised", previous_quote_id=old.pk)
    except IntegrityError as error:
        raise _reference(error) from None
    return new

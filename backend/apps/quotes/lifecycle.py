"""Emitir una cotización (F5-09c; 04 §L.2). Solo `apps.quotes.api` lo importará. Orden: el permiso
(`quotes.send`), la cotización bloqueada (`FOR NO KEY UPDATE`), su **alcance** (`quotes.view` y
`quotes.send`: una que no existe, de otra organización o fuera del alcance es la misma
`DoesNotExist`), la versión (`StaleVersion`) y el estado (`WrongStatus`). Escribe la cotización, su
auditoría (solo ids, estados y `[texto]`) y su evento del outbox en la misma transacción.

**Emitir** pasa `APPROVED`, o un `DRAFT` que quien emite puede enviar sin aprobación, a `SENT`:
congela la fotografía de la empresa y la del cliente (lo mínimo del documento: nombre y documento;
quien emite ha de poder ver al contacto, y si no lo ve o no existe es el mismo `BadReference`,
antes de tocar el costo), las condiciones y la garantía (texto de quien emite: no hay configuración
por organización) y la validez, que ha de estar en el futuro. Vuelve a pasar los precios manuales
por el suelo de costo (`PriceRefused`) y, desde `DRAFT`, vuelve a medir el límite con quien emite:
la base deja pasar `DRAFT` a `SENT` y un `DRAFT` devuelto con un descuento sobre el límite no sale
(`NeedsApproval`, sin escribir nada), ni el de la IA (`created_by_user_id` nulo) sin una solicitud
aprobada."""

from typing import Any
from uuid import UUID

from django.db import transaction
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


class WrongStatus(Exception):
    """La cotización no está en un estado desde el que se pueda hacer esto."""


class NeedsApproval(Exception):
    """Para salir, esta cotización necesita antes una aprobación."""


def issue_quote(
    ctx: TenantContext, *, quote_id: UUID, version: int, valid_until: Any = None,
    terms: str | None = None, warranty: str | None = None,
) -> Quote:  # fmt: skip
    """Emite la cotización (`SENT`) con `quotes.send`: fotografías, `sent_at` y validez (la dada o
    la que ya tiene; en el futuro, o `ValueError`). Audita `quote.issued`; evento `quote.issued`."""
    until = None if valid_until is None else _moment(valid_until)
    given = ((terms, "condiciones", TERMS_MAX), (warranty, "garantía", WARRANTY_MAX))
    words = [None if text is None else _text(text, what, top) or None for text, what, top in given]
    alias, quote_id = require_scope(ctx), _uuid(quote_id, "cotización")
    _integer(version, 1, 2**31 - 1, "versión")
    with transaction.atomic(using=alias):
        ectx = _require(ctx, "quotes.send")
        quote = _reachable(alias, quote_id, _within(ectx, "quotes.view", "quotes.send"))
        if quote.version != version:
            raise StaleVersion(quote_id)
        if quote.status not in (QuoteStatus.DRAFT, QuoteStatus.APPROVED):
            raise WrongStatus(quote_id)
        now, until = timezone.now(), until or quote.valid_until
        if until is None or until <= now:
            raise ValueError("validez: una fecha futura")
        found = Contact.objects.using(alias).filter(pk=quote.contact_id, deleted_at__isnull=True)
        seen = found.first()  # sin bloquear: el que no lo ve no espera ni distingue
        if seen is None or not can(ectx, "contacts.view", seen):  # la fotografía copia sus datos
            raise BadReference("contact_id")  # el mismo error: no existe, borrado o no visible
        contact = found.select_for_update(no_key=True).first()
        if contact is None:
            raise BadReference("contact_id")
        lines = _lines(alias, quote)
        _floor(ctx, quote, lines)
        if quote.status == QuoteStatus.DRAFT:  # la base lo deja pasar: aquí se vuelve a medir
            if quote.created_by_user_id is None or _verdict(alias, ectx, quote, lines)[3]:
                raise NeedsApproval(quote.pk)
        elif quote.created_by_user_id is None and not (
            QuoteApproval.objects.using(alias)
            .filter(quote_id=quote.pk, status=ApprovalStatus.APPROVED).exists()
        ):  # fmt: skip
            raise NeedsApproval(quote.pk)
        company = Organization.objects.using(alias).get(pk=ctx.organization_id)
        before, was = quote.status, quote.valid_until
        _stamp(ctx, alias, quote, {
            "status": QuoteStatus.SENT, "sent_at": now, "valid_until": until,
            "company_snapshot": {"name": company.name},
            "customer_snapshot": {
                "contact_id": str(contact.pk), "display_name": contact.display_name,
                "document_type": contact.document_type, "document_number": contact.document_number,
            },
            "terms_snapshot": words[0], "warranty_snapshot": words[1],
        })  # fmt: skip
        record(ctx, "quote.issued", _entity(quote), {
            "status": [before, QuoteStatus.SENT], "valid_until": [was, until],
            "sent_at": [None, now], "snapshots": [None, "[congeladas]"],
        })  # fmt: skip
        _event(ctx, quote, "quote.issued")
    return quote

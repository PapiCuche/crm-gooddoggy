"""F5-09e4: emitir por la API (`…/quotes/{id}/issue/`) y la fotografía del cliente en el detalle,
con los roles reales (ana Owner sin límite, vera Vendedora `OWN` con `quotes.override_price`, sam
Supervisor `TEAM` sin política, bea sin rol, luis de B), la sesión y `crm_app`. Las negativas
primero: permiso antes de leer, el mismo 404, el límite y el suelo medidos otra vez al emitir, la
IA solo con una aprobación, carreras con hilos, cuerpos hostiles y que el costo nunca sale."""

import logging
from datetime import timedelta
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from django.conf import settings as django_settings
from django.utils import timezone

from tests.factories import make_user
from tests.test_authorization import give
from tests.test_member_branch import DENIED
from tests.test_memberships import join
from tests.test_pricing_engine import item, row
from tests.test_quote_discounts import cost
from tests.test_quotes_decisions_api import act
from tests.test_quotes_discount_api import discount
from tests.test_quotes_pricing_api import REFUSED, SENTINEL, draft, lab, price, refusals, submit
from tests.test_quotes_read_api import QUOTES, board, crm, pipelines, real_stack
from tests.test_quotes_write_api import DB, ROWS, desk, fail, threaded, waiting
from tests.test_self_context import NOT_FOUND, reply, signed
from tests.test_team_writes import send

__all__ = ["board", "crm", "desk", "lab", "pipelines", "real_stack"]  # las fixtures
pytestmark = pytest.mark.usefixtures("tenant_db")
STATE = "SELECT status, version FROM quotes WHERE id = %s"
ISSUED = "SELECT count(*) FROM audit_logs WHERE action = 'quote.issued'"
EVENTS = "SELECT count(*) FROM outbox_events WHERE event_type = 'quote.issued'"
DONE = "SELECT count(*) FROM {} WHERE {} IN ('quote.issued', 'quote.cancelled')"
DOCUMENT = "12345678"
SEES = {"quotes.view": "ORGANIZATION", "contacts.view": "ORGANIZATION"}
COST = (SENTINEL, "3494.4", "4865.6", "4211.1", "3568.7", "4969.1")  # sin IGV y con él: nunca


def soon(days: int = 30) -> str:
    return (timezone.now() + timedelta(days=days)).isoformat()


def issue(client: Any, quote: Any, version: int = 1, **extra: Any) -> Any:
    body = {"version": version, "valid_until": soon(), **extra}
    return send(client, "post", f"{QUOTES}{quote}/issue/", body)


def member(lab: SimpleNamespace, grants: dict[str, Any]) -> Any:
    user = make_user()
    give(lab.a, join(lab.a, user).pk, grants)
    return user


def test_the_permission_comes_before_the_body_and_anything_out_of_reach_is_one_404(
    lab: SimpleNamespace, migrator: DB
) -> None:
    mine, theirs = draft(lab)[0], draft(lab, "ana")[0]
    before = migrator.execute(ROWS).fetchone()
    for target in (mine, uuid4()):  # sin rol: 403, exista o no, y sin mirar el cuerpo
        assert reply(issue(signed(lab.bea), target, bogus=1)) == DENIED
    view_only = member(lab, SEES)
    send_only = member(lab, {"quotes.send": "ORGANIZATION", "contacts.view": "ORGANIZATION"})
    for who in (view_only, send_only):  # uno no da el otro
        assert reply(issue(signed(who), mine, bogus=1)) == DENIED
    crossed = member(lab, {"quotes.view": "ORGANIZATION", "quotes.send": "OWN"})
    assert reply(issue(signed(lab.luis), mine)) == NOT_FOUND  # no es de su organización
    for who, target in ((lab.vera, theirs), (lab.sam, theirs), (crossed, mine),
                        (lab.vera, uuid4()), (lab.vera, lab.quotes["b"])):  # fmt: skip
        assert reply(issue(signed(who), target, version=9)) == NOT_FOUND, (who, target)
    assert fail(issue(signed(lab.vera), mine, version=9))[:2] == (409, "STALE_VERSION")
    assert migrator.execute(ROWS).fetchone() == before and lab.asked == []
    keys = "SELECT count(*) FROM login_throttles WHERE key LIKE %s"  # emitir cuenta, antes de todo
    assert migrator.execute(keys, [f"qfl:usr:{lab.bea.pk}%"]).fetchone() == (3,)


def test_issuing_freezes_the_customer_and_only_who_sees_the_contact_reads_it(
    lab: SimpleNamespace, migrator: DB
) -> None:
    quote = draft(lab)[0]
    migrator.execute("UPDATE contacts SET document_type = 'DNI', document_number = %s"
                     " WHERE id = %s", [DOCUMENT, lab.ids["owned"]])  # fmt: skip
    vera = signed(lab.vera)
    assert vera.get(f"{QUOTES}{quote}/").json()["customer_snapshot"] is None  # aún no se emitió
    done = issue(vera, quote, terms="Pago 50 % al pedir", warranty="12 meses")
    body = done.json()
    assert (done.status_code, body["status"], body["version"]) == (200, "SENT", 2)
    said = (body["terms_snapshot"], body["warranty_snapshot"], body["sent_at"] is not None)
    assert said == ("Pago 50 % al pedir", "12 meses", True)  # F5-09i: la pantalla los muestra
    frozen = {"display_name": "De vera", "document_type": "DNI", "document_number": DOCUMENT}
    assert body["customer_snapshot"] == frozen and vera.get(f"{QUOTES}{quote}/").json() == body
    assert signed(lab.ana).get(f"{QUOTES}{quote}/").json()["customer_snapshot"] == frozen
    blind = member(lab, {"quotes.view": "ORGANIZATION", "contacts.view": "OWN"})  # no es suyo
    for who in (blind, member(lab, {"quotes.view": "ORGANIZATION"})):
        seen = signed(who).get(f"{QUOTES}{quote}/")
        assert (seen.json()["customer_snapshot"], seen.json()["contact"]["display_name"]) == (
            None, None,
        )  # fmt: skip
        assert DOCUMENT not in seen.content.decode()
    shown = str(migrator.execute("SELECT t::text FROM audit_logs t").fetchall())
    assert DOCUMENT not in shown and "Pago" not in shown
    assert fail(issue(vera, quote, version=2))[:2] == (409, "NOT_ISSUABLE")  # no dos veces
    assert migrator.execute(ISSUED).fetchone() == migrator.execute(EVENTS).fetchone() == (1,)


def test_who_issues_must_see_the_contact_and_give_a_future_validity(
    lab: SimpleNamespace, migrator: DB
) -> None:
    quote = draft(lab)[0]  # de vera, con su contacto
    blind = member(lab, {"quotes.view": "ORGANIZATION", "quotes.send": "ORGANIZATION"})
    sent = issue(signed(blind), quote)
    assert fail(sent) == (400, "VALIDATION_ERROR", ["_"])  # el 400 de crear: no dice por qué
    assert "El contacto o la oportunidad" in sent.content.decode()
    plain = send(signed(lab.vera), "post", f"{QUOTES}{quote}/issue/", {"version": 1})
    assert fail(plain) == (400, "VALIDATION_ERROR", ["_"])  # el borrador no tiene validez
    assert migrator.execute(STATE, [quote]).fetchone() == ("DRAFT", 1)


def test_a_draft_left_over_the_limit_is_measured_again_with_who_issues_it(
    lab: SimpleNamespace, migrator: DB
) -> None:
    quote, [line] = draft(lab)
    vera = signed(lab.vera)  # sin política: 0 %
    assert discount(vera, quote, line, value="5.00").json()["status"] == "PENDING_APPROVAL"
    assert act(vera, quote, "withdraw-request").json()["status"] == "DRAFT"  # con el descuento
    sender = member(lab, {**SEES, "quotes.send": "ORGANIZATION"})  # sin política: 0 %
    for who in (lab.vera, sender):  # ni la autora ni otra persona sin límite
        assert fail(issue(signed(who), quote, version=3))[:2] == (409, "NEEDS_APPROVAL")
    assert migrator.execute(STATE, [quote]).fetchone() == ("DRAFT", 3)
    assert migrator.execute(ISSUED).fetchone() == (0,)
    done = issue(signed(lab.ana), quote, version=3)  # la Owner, sin límite
    assert (done.status_code, done.json()["status"]) == (200, "SENT")


def test_the_quote_of_the_ai_goes_out_only_after_a_person_approved_it(
    lab: SimpleNamespace, migrator: DB
) -> None:
    ai, ana = lab.quotes["nobody"], signed(lab.ana)
    assert fail(issue(ana, ai))[:2] == (409, "NEEDS_APPROVAL")  # ni la Owner, sin solicitud
    assert submit(ana, ai).json()["status"] == "PENDING_APPROVAL"
    assert fail(issue(ana, ai, version=2))[:2] == (409, "NOT_ISSUABLE")  # espera la decisión
    give(lab.a, lab.m["bea"], {"quotes.view": "ORGANIZATION", "quotes.approve": None})
    assert act(signed(lab.bea), ai, "approve").json()["status"] == "APPROVED"
    done = issue(ana, ai, version=3, terms="", warranty=" ")  # en blanco: sin texto
    assert (done.status_code, done.json()["status"]) == (200, "SENT")
    assert migrator.execute(ISSUED).fetchone() == (1,)


def test_a_cost_that_rose_before_issuing_is_the_same_422_audited_counted_and_capped(
    lab: SimpleNamespace, migrator: DB, settings: Any
) -> None:
    later = timezone.now() + timedelta(days=3)
    cost(lab, lab.case, "80", ends_at=later)
    quote, [_, line] = draft(lab, "vera", lab.phone, lab.case)
    assert price(signed(lab.vera), quote, line, "95.00").status_code == 200
    ana = signed(lab.ana)
    assert act(ana, quote, "approve").json()["status"] == "APPROVED"
    cost(lab, lab.case, "99", starts_at=later)  # programado: el precio ya no lo cubre
    sent = [issue(ana, quote, version=3), issue(signed(lab.vera), quote, version=3)]
    assert {reply(s) for s in sent} == {REFUSED}
    assert migrator.execute(STATE, [quote]).fetchone() == ("APPROVED", 3)
    assert [r[:3] for r in refusals(migrator)] == [
        (quote, "DENIED", {"route": "issue", "line_id": None})
    ] * 2
    settings.QUOTE_FLOOR_THROTTLE = {"quote": (3, 3600), "user": (30, 86400)}
    assert reply(issue(ana, quote, version=3)) == REFUSED  # ana: aprobar y dos emitir; luego, tope
    assert issue(ana, quote, version=3).status_code == 429
    assert migrator.execute(ISSUED).fetchone() == (0,)


def test_the_cost_never_leaves_issuing_nor_its_refusal(
    lab: SimpleNamespace, migrator: DB, caplog: pytest.LogCaptureFixture
) -> None:
    [(listed,)] = migrator.execute("SELECT DISTINCT price_list_id FROM product_prices").fetchall()
    later, late = timezone.now() + timedelta(days=3), item(lab.a, "LATE")
    row(lab.a, (late, listed), "5000", timezone.now() - timedelta(9))
    cost(lab, late, SENTINEL, ends_at=later)
    ana, quotes = signed(lab.ana), []
    with caplog.at_level(logging.DEBUG):
        for value in ("4300.00", "4130.00"):  # a mano, sobre el costo de hoy
            quote, [line] = draft(lab, "vera", late)
            assert price(signed(lab.vera), quote, line, value).status_code == 200
            assert act(ana, quote, "approve").json()["status"] == "APPROVED"
            quotes.append(quote)
        cost(lab, late, "4211.11", starts_at=later)
        sold, failed = quotes
        sent = [issue(ana, sold, version=3, terms="Contado"), issue(ana, failed, version=3)]
    assert [s.status_code for s in sent] == [200, 422]  # el costo programado: solo la segunda
    shown = [s.content.decode() + str(sorted(s.headers.items())) for s in sent]
    shown += [str(migrator.execute(sql).fetchall()) for sql in (
        "SELECT t::text FROM audit_logs t", "SELECT t::text FROM outbox_events t")]  # fmt: skip
    schema = django_settings.BASE_DIR / "openapi" / "schema.yaml"
    shown += [caplog.text, schema.read_text()]
    assert not [(v, s[:80]) for v in COST for s in shown if v in s]


@pytest.mark.parametrize(
    "extra",
    [{"status": "SENT"}, {"organization_id": str(uuid4())}, {"version": "1"}, {"version": True},
     {"customer_snapshot": {"display_name": "X"}}, {"company_snapshot": {}},
     {"sent_at": soon()}, {"valid_until": 1893456000}, {"valid_until": "2030-01-01T00:00:00"},
     {"valid_until": (timezone.now() - timedelta(days=1)).isoformat()},
     {"terms": "x" * 2001}, {"warranty": 12}],
)  # fmt: skip
def test_a_hostile_body_is_a_400_on_its_field_and_writes_nothing(
    lab: SimpleNamespace, migrator: DB, extra: dict[str, Any]
) -> None:
    quote, before = draft(lab)[0], migrator.execute(ROWS).fetchone()
    assert fail(issue(signed(lab.vera), quote, **extra)) == (400, "VALIDATION_ERROR", list(extra))
    assert migrator.execute(ROWS).fetchone() == before and lab.asked == []


@pytest.mark.parametrize("rival", ["issue", "cancel"])
def test_two_issues_or_an_issue_and_a_cancel_at_once_leave_one(
    lab: SimpleNamespace, migrator: DB, rival: str
) -> None:
    quote, got, verbs = draft(lab)[0], list[Any](), iter(("issue", rival))

    def call(verb: str) -> Any:
        ana = signed(lab.ana)
        return issue(ana, quote) if verb == "issue" else act(ana, quote, verb, version=1)

    with migrator.transaction():  # los dos esperan la misma fila; luego, uno gana
        migrator.execute("SELECT 1 FROM quotes WHERE id = %s FOR UPDATE", [quote])
        workers = threaded(2, lambda: got.append(fail(call(next(verbs)))))
        waiting(migrator, 2)
    for worker in workers:
        worker.join(timeout=15)
    assert sorted(got, key=str) == [(200, None, []), (409, "STALE_VERSION", [])]
    for found in (DONE.format("audit_logs", "action"), DONE.format("outbox_events", "event_type")):
        assert migrator.execute(found).fetchone() == (1,), found  # una auditoría y un evento


def test_a_draft_that_kept_a_manual_price_needs_approval_before_the_floor_is_measured(
    lab: SimpleNamespace, migrator: DB
) -> None:
    quote, [line] = draft(lab)
    vera = signed(lab.vera)
    assert price(vera, quote, line, "4200.00").json()["status"] == "PENDING_APPROVAL"
    assert act(vera, quote, "withdraw-request").json()["status"] == "DRAFT"  # con el precio
    asked = len(lab.asked)
    for who in (lab.vera, lab.ana):  # ni la Owner: el precio manual pide aprobación, sin medirlo
        assert fail(issue(signed(who), quote, version=3))[:2] == (409, "NEEDS_APPROVAL")
    assert len(lab.asked) == asked and refusals(migrator) == []
    assert migrator.execute(STATE, [quote]).fetchone() == ("DRAFT", 3)


def test_who_cannot_see_the_contact_gets_the_400_before_the_floor_is_measured(
    lab: SimpleNamespace, migrator: DB
) -> None:
    later = timezone.now() + timedelta(days=3)
    cost(lab, lab.case, "80", ends_at=later)
    quote, [_, line] = draft(lab, "vera", lab.phone, lab.case)
    assert price(signed(lab.vera), quote, line, "95.00").status_code == 200
    assert act(signed(lab.ana), quote, "approve").json()["status"] == "APPROVED"
    cost(lab, lab.case, "99", starts_at=later)  # el precio ya no lo cubre
    blind = member(lab, {"quotes.view": "ORGANIZATION", "quotes.send": "ORGANIZATION"})
    asked = len(lab.asked)
    assert fail(issue(signed(blind), quote, version=3)) == (400, "VALIDATION_ERROR", ["_"])
    assert len(lab.asked) == asked and refusals(migrator) == []  # ni medido ni auditado
    assert migrator.execute(STATE, [quote]).fetchone() == ("APPROVED", 3)


@pytest.mark.parametrize(
    "value", ["2030-02-30T10:00:00-05:00", "2030-01-01T25:00:00Z", "2030-01-01T10:00:00+25:00"]
)
def test_an_impossible_validity_has_the_fixed_message_never_the_parsers(
    lab: SimpleNamespace, value: str
) -> None:
    sent = issue(signed(lab.vera), draft(lab)[0], valid_until=value)
    assert fail(sent) == (400, "VALIDATION_ERROR", ["valid_until"])
    fixed = "Fecha y hora ISO 8601 con zona horaria, como 2026-11-20T23:59:00-05:00."
    assert sent.json()["fields"]["valid_until"] == [{"code": "invalid", "message": fixed}]

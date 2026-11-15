"""F5-09e5: aceptar, declinar y revisar por la API (`…/quotes/{id}/accept/`, `…/decline/` y
`…/revise/`), con los roles reales (ana Owner, vera Vendedora `OWN`, sam Supervisor `TEAM`, bea sin
rol, luis de B), la sesión y `crm_app`. Las negativas primero: permiso antes de leer, el mismo 404,
la validez pasada aunque la tarea no corrió, de dónde se revisa, los vínculos que ve quien revisa,
carreras con hilos, cuerpos hostiles y que el costo nunca sale. Ninguna mide el suelo ni cuenta."""

import json
import logging
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from django.conf import settings as django_settings

from apps.quotes import answers
from apps.quotes.api.errors import LINKS
from core.tenancy.scope import tenant_scope
from tests.test_member_branch import DENIED
from tests.test_memberships import ctx
from tests.test_opportunities_tables import make
from tests.test_quotes_decisions_api import act
from tests.test_quotes_issue_api import COST, SEES, STATE, issue, member, soon
from tests.test_quotes_pricing_api import draft, lab, price, submit
from tests.test_quotes_read_api import QUOTES, board, crm, pipelines, real_stack
from tests.test_quotes_write_api import DB, ROWS, create, desk, fail, lines, threaded, waiting
from tests.test_self_context import NOT_FOUND, reply, signed
from tests.test_team_writes import send

__all__ = ["board", "crm", "desk", "lab", "pipelines", "real_stack"]  # las fixtures
pytestmark = pytest.mark.usefixtures("tenant_db")
ORG = "ORGANIZATION"
VERBS = ("accept", "decline", "revise")
REVISER = {"quotes.view": ORG, "quotes.create": None, "quotes.update": ORG, "quotes.cancel": ORG,
           "quotes.send": ORG, "contacts.view": ORG, "prices.view": None}  # fmt: skip
DONE = "SELECT count(*) FROM {} WHERE {} IN ('quote.accepted', 'quote.declined', 'quote.revised')"
COUNTED = "SELECT count(*), coalesce(sum(failures), 0) FROM login_throttles WHERE key LIKE 'qfl:%'"


def answer(client: Any, quote: Any, verb: str, version: int = 2, **extra: Any) -> Any:
    body = {"version": version, **({"reason": "Encontró otro precio"} if verb == "decline" else {})}
    return send(client, "post", f"{QUOTES}{quote}/{verb}/", body | extra)


def sold(lab: SimpleNamespace, who: str = "vera", quote: str | None = None) -> str:
    """Una cotización emitida (`SENT`, versión 2) de `who`."""
    quote = quote or draft(lab, who)[0]
    assert issue(signed(getattr(lab, who)), quote).status_code == 200
    return quote


def lapse(migrator: DB, quote: str) -> None:
    """La validez pasó hace un segundo según la base (el migrador no pasa por el disparador)."""
    migrator.execute("UPDATE quotes SET sent_at = now() - interval '2 days', valid_until = now()"
                     " - interval '1 second' WHERE id = %s", [quote])  # fmt: skip


def links_400() -> bytes:
    shape = {"code": "VALIDATION_ERROR", "fields": {"_": [{"code": "invalid", "message": LINKS}]}}
    return json.dumps(shape, ensure_ascii=False, separators=(",", ":")).encode()


def done(migrator: DB) -> tuple[Any, Any]:
    return (migrator.execute(DONE.format("audit_logs", "action")).fetchone(),
            migrator.execute(DONE.format("outbox_events", "event_type")).fetchone())  # fmt: skip


def test_the_permission_comes_before_the_body_and_the_row_and_nothing_is_counted(
    lab: SimpleNamespace, migrator: DB
) -> None:
    mine = sold(lab)
    partial = [member(lab, SEES), member(lab, {"quotes.send": ORG, "contacts.view": ORG})]
    unrevising = [
        member(lab, {k: v for k, v in REVISER.items() if k != missing})
        for missing in ("quotes.view", "quotes.create", "quotes.update", "quotes.cancel")
    ]
    before, counted = migrator.execute(ROWS).fetchone(), migrator.execute(COUNTED).fetchone()
    for verb in VERBS:  # sin rol: 403, exista o no, y sin mirar el cuerpo
        for target in (mine, uuid4()):
            assert reply(answer(signed(lab.bea), target, verb, bogus=1)) == DENIED, verb
    for who in partial:  # `quotes.view` y `quotes.send`: uno no da el otro
        for verb in ("accept", "decline"):
            assert reply(answer(signed(who), mine, verb, bogus=1)) == DENIED
    for who in unrevising:  # revisar: los cuatro, antes de leer
        assert reply(answer(signed(who), mine, "revise", bogus=1)) == DENIED
    assert migrator.execute(ROWS).fetchone() == before and lab.asked == []
    assert migrator.execute(COUNTED).fetchone() == counted  # ninguna mide el suelo: no cuentan


@pytest.mark.parametrize("verb", VERBS)
def test_out_of_scope_other_org_and_absent_are_one_404_even_stale(
    lab: SimpleNamespace, migrator: DB, verb: str
) -> None:
    mine, theirs = sold(lab), sold(lab, "ana")
    own = {"accept": "quotes.send", "decline": "quotes.send", "revise": "quotes.update"}[verb]
    crossed = member(lab, {**REVISER, own: "OWN"})  # ve toda, actúa solo en las suyas
    before = migrator.execute(ROWS).fetchone()
    for who, target in ((lab.vera, theirs), (lab.sam, theirs), (crossed, mine), (lab.luis, mine),
                        (lab.vera, uuid4()), (lab.vera, lab.quotes["b"])):  # fmt: skip
        assert reply(answer(signed(who), target, verb, version=9)) == NOT_FOUND, (who, target)
    assert fail(answer(signed(lab.vera), mine, verb, version=9))[:2] == (409, "STALE_VERSION")
    assert migrator.execute(ROWS).fetchone() == before
    assert migrator.execute(STATE, [mine]).fetchone() == ("SENT", 2)


def test_the_customer_answers_once_with_one_audit_and_one_event(
    lab: SimpleNamespace, migrator: DB
) -> None:
    accepted, declined, vera = sold(lab), sold(lab), signed(lab.vera)
    unsent = draft(lab)[0]
    assert fail(answer(vera, unsent, "accept", version=1))[:2] == (409, "NOT_ANSWERABLE")
    yes = answer(vera, accepted, "accept")
    assert (yes.status_code, yes.json()["status"], yes.json()["version"]) == (200, "ACCEPTED", 3)
    assert vera.get(f"{QUOTES}{accepted}/").json() == yes.json()  # el mismo detalle
    no = answer(vera, declined, "decline", reason="Compró en otra tienda")
    assert (no.status_code, no.json()["status"], no.json()["version"]) == (200, "REJECTED", 3)
    said = [(r["accepted_at"] is None, r["rejected_at"] is None, r["rejection_reason"])
            for r in (yes.json(), no.json())]  # fmt: skip
    assert said == [(False, True, None), (True, False, "Compró en otra tienda")]  # F5-09i
    for quote in (accepted, declined):  # no se responde dos veces
        for verb in ("accept", "decline"):
            assert fail(answer(vera, quote, verb, version=3))[:2] == (409, "NOT_ANSWERABLE")
    assert done(migrator) == ((2,), (2,))
    assert "otra tienda" not in str(migrator.execute("SELECT t::text FROM audit_logs t").fetchall())


def test_a_lapsed_validity_is_409_expired_even_before_the_task_marks_it(
    lab: SimpleNamespace, migrator: DB
) -> None:
    quote = sold(lab)
    lapse(migrator, quote)
    for verb in ("accept", "decline"):
        assert fail(answer(signed(lab.vera), quote, verb))[:2] == (409, "QUOTE_EXPIRED")
    assert migrator.execute(STATE, [quote]).fetchone() == ("SENT", 2)
    assert done(migrator) == ((0,), (0,))


def test_revising_answers_the_new_revision_and_never_edits_what_was_issued(
    lab: SimpleNamespace, migrator: DB
) -> None:
    vera, quote = signed(lab.vera), sold(lab)
    before = vera.get(f"{QUOTES}{quote}/").json()
    made = answer(vera, quote, "revise")
    new = made.json()
    assert made.status_code == 200 and new["id"] != quote
    assert (new["number"], new["revision"], new["status"], new["version"]) == (
        before["number"], 2, "DRAFT", 1)  # fmt: skip
    assert (new["valid_until"], new["customer_snapshot"]) == (None, None)
    assert vera.get(f"{QUOTES}{new['id']}/").json() == new
    old = vera.get(f"{QUOTES}{quote}/").json()
    assert (old["status"], old["version"]) == ("CANCELLED", 3)
    kept = ("items", "total", "valid_until", "customer_snapshot")
    assert [old[k] for k in kept] == [before[k] for k in kept]  # lo emitido no se tocó
    plain = draft(lab)[0]  # un borrador se devuelve tal cual: se sigue editando
    assert answer(vera, plain, "revise", version=1).json() == vera.get(f"{QUOTES}{plain}/").json()
    approved = draft(lab)[0]
    assert submit(vera, approved).json()["status"] == "APPROVED"
    assert answer(vera, approved, "revise").json()["revision"] == 2
    ended = [sold(lab) for _ in range(3)]
    assert answer(vera, ended[0], "accept").status_code == 200
    assert answer(vera, ended[1], "decline").status_code == 200
    lapse(migrator, ended[2])
    with tenant_scope(ctx(lab.a)) as system:
        assert answers.expire_quote(system, UUID(ended[2]))
    for target in (*ended, quote):  # aceptada, rechazada, vencida y la cancelada: terminales
        assert fail(answer(vera, target, "revise", version=3))[:2] == (409, "NOT_REVISABLE")
    revised = "SELECT count(*) FROM audit_logs WHERE action = 'quote.revised'"
    assert migrator.execute(revised).fetchone() == (2,)  # la emitida y la aprobada


def test_revising_needs_send_once_issued_and_sees_the_links_as_when_creating(
    lab: SimpleNamespace, migrator: DB
) -> None:
    vera, ids = signed(lab.vera), lab.ids
    opp = make(lab.a, contact_id=ids["owned"], assigned_user_id=lab.vera.pk)
    linked = sold(lab, quote=create(vera, {"opportunity_id": str(opp.pk),
                                           "items": lines(lab.phone)}).json()["id"])  # fmt: skip
    alone, approved = sold(lab), draft(lab)[0]
    assert submit(vera, approved).json()["status"] == "APPROVED"
    unsent = member(lab, {k: v for k, v in REVISER.items() if k != "quotes.send"})
    blind = member(lab, {**REVISER, "contacts.view": "OWN"})  # el contacto es de vera
    before = migrator.execute(ROWS).fetchone()
    assert reply(answer(signed(unsent), alone, "revise")) == DENIED  # ya la tiene el cliente
    for who, target in ((blind, alone), (member(lab, REVISER), linked)):  # sin ver la oportunidad
        assert reply(answer(signed(who), target, "revise")) == (400, links_400())
    assert migrator.execute(ROWS).fetchone() == before
    assert answer(signed(unsent), approved, "revise").status_code == 200  # aún no salió
    seer = member(lab, {**REVISER, "opportunities.view": ORG})
    assert answer(signed(seer), linked, "revise").json()["opportunity_id"] == str(opp.pk)


@pytest.mark.parametrize(
    ("verb", "extra", "field"),
    [("accept", {"status": "ACCEPTED"}, "status"), ("accept", {"reason": "x"}, "reason"),
     ("accept", {"organization_id": str(uuid4())}, "organization_id"),
     ("accept", {"accepted_at": soon()}, "accepted_at"), ("decline", {"version": "2"}, "version"),
     ("decline", {"version": True}, "version"), ("decline", {"reason": " "}, "reason"),
     ("decline", {"reason": "x" * 501}, "reason"), ("decline", {"reason": 5}, "reason"),
     ("decline", {"reason": None}, "reason"), ("revise", {"items": []}, "items"),
     ("revise", {"revision": 5}, "revision"), ("revise", {"version": 2.0}, "version")],
)  # fmt: skip
def test_a_hostile_body_is_a_400_on_its_field_and_writes_nothing(
    lab: SimpleNamespace, migrator: DB, verb: str, extra: dict[str, Any], field: str
) -> None:
    quote = sold(lab)
    before = migrator.execute(ROWS).fetchone()
    assert fail(answer(signed(lab.vera), quote, verb, **extra)) == (400, "VALIDATION_ERROR",
                                                                     [field])  # fmt: skip
    assert migrator.execute(ROWS).fetchone() == before


def test_a_decline_without_a_reason_is_a_400_and_up_to_500_characters_is_accepted(
    lab: SimpleNamespace,
) -> None:
    quote, vera = sold(lab), signed(lab.vera)
    sent = send(vera, "post", f"{QUOTES}{quote}/decline/", {"version": 2})
    assert fail(sent) == (400, "VALIDATION_ERROR", ["reason"])
    assert answer(vera, quote, "decline", reason="x" * 500).status_code == 200


RACES = [("accept", "accept"), ("accept", "decline"), ("accept", "revise"), ("revise", "revise")]


@pytest.mark.parametrize("pair", RACES)
def test_two_answers_at_once_from_one_version_leave_one(
    lab: SimpleNamespace, migrator: DB, pair: tuple[str, str]
) -> None:
    quote, got, verbs = sold(lab), list[Any](), iter(pair)
    with migrator.transaction():  # los dos esperan la misma fila; luego, uno gana
        migrator.execute("SELECT 1 FROM quotes WHERE id = %s FOR UPDATE", [quote])
        workers = threaded(2, lambda: got.append(fail(answer(signed(lab.ana), quote,
                                                             next(verbs)))))  # fmt: skip
        waiting(migrator, 2)
    for worker in workers:
        worker.join(timeout=15)
    assert sorted(got, key=str) == [(200, None, []), (409, "STALE_VERSION", [])]
    assert done(migrator) == ((1,), (1,))  # una auditoría y un evento


def test_the_cost_never_leaves_answering_nor_revising(
    lab: SimpleNamespace, migrator: DB, caplog: pytest.LogCaptureFixture
) -> None:
    ana, vera, sent = signed(lab.ana), signed(lab.vera), []
    with caplog.at_level(logging.DEBUG):
        for verb in VERBS:  # a mano, sobre el costo; aprobada por ana y emitida
            quote, [line] = draft(lab)
            assert price(vera, quote, line, "4300.00").status_code == 200
            assert act(ana, quote, "approve").json()["status"] == "APPROVED"
            assert issue(ana, quote, version=3).status_code == 200
            sent.append(answer(vera, quote, verb, version=4))
    assert [s.status_code for s in sent] == [200, 200, 200]
    shown = [s.content.decode() + str(sorted(s.headers.items())) for s in sent]
    shown += [str(migrator.execute(sql).fetchall()) for sql in (
        "SELECT t::text FROM audit_logs t", "SELECT t::text FROM outbox_events t")]  # fmt: skip
    schema = django_settings.BASE_DIR / "openapi" / "schema.yaml"
    shown += [caplog.text, schema.read_text()]
    assert not [(v, s[:80]) for v in COST for s in shown if v in s]

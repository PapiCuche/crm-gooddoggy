"""F5-09e3: aprobar, rechazar, retirar la solicitud y cancelar por la API, con los roles reales (ana
Owner sin límite, vera Vendedora `OWN` con `quotes.override_price`, sam Supervisor `TEAM` sin
política, bea sin rol, luis de B), la sesión y `crm_app`. Las negativas primero: permiso antes de
leer, el mismo 404, quién decide, el suelo y el tope al aprobar, carreras con hilos y reversión."""

import json
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from django.utils import timezone

from apps.quotes import decisions
from apps.quotes.models import DiscountPolicy
from core.tenancy.scope import tenant_scope
from tests.factories import make_user
from tests.test_authorization import give
from tests.test_member_branch import DENIED
from tests.test_memberships import ctx, join
from tests.test_quote_discounts import cost
from tests.test_quotes_pricing_api import REFUSED, SENTINEL, draft, lab, price, refusals, submit
from tests.test_quotes_read_api import QUOTES, board, crm, pipelines, real_stack
from tests.test_quotes_write_api import DB, ROWS, desk, fail, threaded, waiting
from tests.test_self_context import NOT_FOUND, reply, signed
from tests.test_team_writes import send

__all__ = ["board", "crm", "desk", "lab", "pipelines", "real_stack"]  # las fixtures
pytestmark = pytest.mark.usefixtures("tenant_db")
VERBS = ("approve", "reject", "withdraw-request", "cancel")
BODY = {"approve": {}, "reject": {"reason": "Margen"}, "withdraw-request": {}, "cancel": {}}
DECIDED = ("quote.approved", "quote.rejected", "quote.approval_withdrawn", "quote.cancelled")
STATE = "SELECT q.status, q.version, a.status FROM quotes q LEFT JOIN quote_approvals a ON " \
        "a.quote_id = q.id WHERE q.id = %s ORDER BY a.created_at DESC LIMIT 1"  # fmt: skip


def act(client: Any, quote: Any, verb: str, version: int = 2, **extra: Any) -> Any:
    body = {"version": version, **BODY[verb], **extra}
    return send(client, "post", f"{QUOTES}{quote}/{verb}/", body)


def pending(lab: SimpleNamespace, value: str = "4200.00") -> str:
    """Una cotización de vera con un precio manual que cubre el costo: espera aprobación (v. 2)."""
    quote, [line] = draft(lab)
    assert price(signed(lab.vera), quote, line, value).json()["status"] == "PENDING_APPROVAL"
    return quote


def decided(migrator: DB) -> list[str]:
    found = "SELECT action FROM audit_logs WHERE action = ANY(%s) ORDER BY occurred_at"
    return [a for (a,) in migrator.execute(found, [list(DECIDED)]).fetchall()]


def test_the_permission_comes_before_the_body_and_the_row_on_every_route(
    lab: SimpleNamespace, migrator: DB
) -> None:
    quote, before = pending(lab), migrator.execute(ROWS).fetchone()
    for verb in VERBS:
        for target in (quote, uuid4()):  # sin rol: 403, exista o no, y sin mirar el cuerpo
            assert reply(act(signed(lab.bea), target, verb, bogus=1)) == DENIED, verb
        assert reply(act(signed(lab.luis), quote, verb)) == NOT_FOUND  # no es de su organización
    for verb in ("approve", "reject"):  # vera ve y cambia la suya, pero no aprueba
        assert reply(act(signed(lab.vera), quote, verb, bogus=1)) == DENIED
    grants: list[tuple[dict[str, str | None], tuple[str, ...]]] = [
        ({"quotes.approve": None}, ("approve", "reject")),
        ({"quotes.update": "ORGANIZATION"}, ("withdraw-request",)),
        ({"quotes.cancel": "ORGANIZATION"}, ("cancel",)),
    ]
    for grant, verbs in grants:
        give(lab.a, lab.m["bea"], grant)  # el de la acción sin `quotes.view`: tampoco
        for verb in verbs:
            assert reply(act(signed(lab.bea), quote, verb, bogus=1)) == DENIED
    viewer = make_user()  # ve todas, nada más: ninguna acción
    give(lab.a, join(lab.a, viewer).pk, {"quotes.view": "ORGANIZATION"})
    for verb in VERBS:
        assert reply(act(signed(viewer), quote, verb, bogus=1)) == DENIED
    assert migrator.execute(ROWS).fetchone() == before
    assert migrator.execute(STATE, [quote]).fetchone() == ("PENDING_APPROVAL", 2, "PENDING")


def test_out_of_scope_other_org_and_absent_are_one_404_even_stale_and_never_counted_as_403(
    lab: SimpleNamespace, migrator: DB
) -> None:
    quote, theirs = pending(lab), lab.quotes["ana"]
    give(
        lab.a,
        lab.m["bea"],
        {
            "quotes.approve": None,
            "quotes.view": "OWN",
            "quotes.update": "ORGANIZATION",
            "quotes.cancel": "ORGANIZATION",
        },
    )
    for who, target in ((lab.sam, theirs), (lab.vera, theirs), (lab.bea, quote)):
        for verb in VERBS:  # sam aprueba solo su equipo; bea lo ve `OWN` aunque cambie todas
            for at in (target, uuid4(), lab.quotes["b"]):
                if who is lab.vera and verb in ("approve", "reject"):
                    continue  # sin `quotes.approve`: 403 antes (prueba anterior)
                assert reply(act(signed(who), at, verb, version=9)) == NOT_FOUND, (who, verb)
    assert fail(act(signed(lab.ana), quote, "approve", version=9))[:2] == (409, "STALE_VERSION")
    assert migrator.execute(STATE, [quote]).fetchone() == ("PENDING_APPROVAL", 2, "PENDING")
    assert decided(migrator) == []


def test_approving_reaches_approved_once_and_the_refusals_have_no_figures_and_one_shape(
    lab: SimpleNamespace, migrator: DB
) -> None:
    one, two, sam, ana = pending(lab), pending(lab, "4300.00"), signed(lab.sam), signed(lab.ana)
    beyond = {act(sam, q, "approve").content for q in (one, two)}  # sin política: 0 %
    own = set()
    for value in ("4250.00", "4350.00"):  # quien pidió no aprueba (tampoco en una org de uno)
        mine, [line] = draft(lab, "ana")
        assert price(ana, mine, line, value).json()["status"] == "PENDING_APPROVAL"
        own.add(act(ana, mine, "approve").content)
    done = act(ana, one, "approve", comment="Visto")
    assert (done.status_code, done.json()["status"], done.json()["version"]) == (200, "APPROVED", 3)
    assert SENTINEL not in done.content.decode()
    assert act(ana, two, "approve").json()["status"] == "APPROVED"
    later = {act(ana, one, "approve", version=3).content,
             act(ana, lab.quotes["ana"], "approve", version=1).content}  # fmt: skip
    for quote in (one, two):
        assert act(ana, quote, "cancel", version=3).json()["status"] == "CANCELLED"
    final = {act(ana, quote, "cancel", version=4).content for quote in (one, two)}
    for bodies, code in ((beyond, "BEYOND_LIMIT"), (own, "SELF_APPROVAL_FORBIDDEN"),
                         (later, "NOT_PENDING"), (final, "QUOTE_TERMINAL")):  # fmt: skip
        [body] = bodies  # los mismos bytes para cada cotización
        assert json.loads(body)["code"] == code and not any(c.isdigit() for c in body.decode())
    assert decided(migrator) == ["quote.approved"] * 2 + ["quote.cancelled"] * 2


def test_rejecting_needs_a_reason_and_withdrawing_is_for_the_requester_or_an_approver(
    lab: SimpleNamespace, migrator: DB
) -> None:
    quote, sam = pending(lab), signed(lab.sam)
    for bad in ({"reason": " "}, {"reason": None}, {"reason": "x" * 201}):
        assert fail(act(sam, quote, "reject", **bad)) == (400, "VALIDATION_ERROR", ["reason"])
    assert fail(send(sam, "post", f"{QUOTES}{quote}/reject/", {"version": 2}))[2] == ["reason"]
    done = act(sam, quote, "reject", reason="Margen DNI 12345678")  # el equipo; no mide su límite
    assert (done.status_code, done.json()["status"]) == (200, "DRAFT")
    found = "SELECT changes::text FROM audit_logs WHERE action = 'quote.rejected'"
    [(changes,)] = migrator.execute(found).fetchall()
    assert "12345678" not in changes and "[texto]" in changes
    again = pending(lab)
    assert act(signed(lab.vera), again, "withdraw-request").json()["status"] == "DRAFT"  # suya
    assert decided(migrator) == ["quote.rejected", "quote.approval_withdrawn"]


def test_the_request_of_the_ai_is_decided_by_a_person_who_did_not_submit_it(
    lab: SimpleNamespace, migrator: DB
) -> None:
    ai = lab.quotes["nobody"]  # sin autor: la de la IA
    assert submit(signed(lab.ana), ai).json()["status"] == "PENDING_APPROVAL"
    give(lab.a, lab.m["bea"], {"quotes.view": "ORGANIZATION", "quotes.update": "ORGANIZATION",
                               "quotes.cancel": "ORGANIZATION"})  # fmt: skip
    for verb in ("withdraw-request", "cancel"):  # ni retirarla ni cancelarla sin poder aprobar
        assert reply(act(signed(lab.bea), ai, verb, version=9)) == DENIED  # antes de la versión
    assert fail(act(signed(lab.ana), ai, "approve"))[1] == "SELF_APPROVAL_FORBIDDEN"  # la envió
    give(lab.a, lab.m["bea"], {"quotes.approve": None})
    assert act(signed(lab.bea), ai, "approve").json()["status"] == "APPROVED"
    assert decided(migrator) == ["quote.approved"]


def test_cancelling_a_pending_quote_closes_its_request_with_one_audit_and_one_event(
    lab: SimpleNamespace, migrator: DB
) -> None:
    quote = pending(lab)
    give(lab.a, lab.m["bea"], {"quotes.view": "ORGANIZATION", "quotes.cancel": "ORGANIZATION"})
    assert reply(act(signed(lab.bea), quote, "cancel")) == DENIED  # ni pidió ni aprueba
    done = act(signed(lab.vera), quote, "cancel", reason="Ya no")  # quien la pidió
    assert (done.json()["status"], done.json()["cancel_reason"]) == ("CANCELLED", "Ya no")
    assert migrator.execute(STATE, [quote]).fetchone() == ("CANCELLED", 3, "CANCELLED")
    plain = act(signed(lab.bea), lab.quotes["vera"], "cancel", version=1)  # un borrador: sí
    assert plain.json()["status"] == "CANCELLED"
    events = "SELECT count(*) FROM outbox_events WHERE event_type = 'quote.cancelled'"
    assert migrator.execute(events).fetchone() == (2,)
    assert decided(migrator) == ["quote.cancelled"] * 2


@pytest.mark.parametrize("verb", VERBS)
@pytest.mark.parametrize(
    "extra",
    [{"status": "APPROVED"}, {"organization_id": str(uuid4())}, {"version": "2"},
     {"version": True}, {"decided_by_user_id": str(uuid4())}, {"quote_id": str(uuid4())}],
)  # fmt: skip
def test_a_hostile_body_is_a_400_on_its_field_and_writes_nothing(
    lab: SimpleNamespace, migrator: DB, verb: str, extra: dict[str, Any]
) -> None:
    quote = pending(lab)
    who = signed(lab.vera if verb in ("withdraw-request", "cancel") else lab.ana)
    before = migrator.execute(ROWS).fetchone()
    assert fail(act(who, quote, verb, **extra)) == (400, "VALIDATION_ERROR", list(extra))
    assert migrator.execute(ROWS).fetchone() == before


def test_a_cost_that_rose_refuses_the_approval_audited_counted_and_capped(
    lab: SimpleNamespace, migrator: DB, settings: Any
) -> None:
    later = timezone.now() + timedelta(days=3)
    cost(lab, lab.case, "80", ends_at=later)
    quote, [_, line] = draft(lab, "vera", lab.phone, lab.case)
    assert price(signed(lab.vera), quote, line, "95.00").status_code == 200
    cost(lab, lab.case, "99", starts_at=later)  # programado: el precio ya no lo cubre
    ana = signed(lab.ana)
    assert reply(act(ana, quote, "approve")) == REFUSED
    assert migrator.execute(STATE, [quote]).fetchone() == ("PENDING_APPROVAL", 2, "PENDING")
    assert [r[:3] for r in refusals(migrator)] == [
        (quote, "DENIED", {"route": "approve", "line_id": None})
    ]
    settings.QUOTE_FLOOR_THROTTLE = {"quote": (2, 3600), "user": (30, 86400)}
    assert reply(act(ana, quote, "approve")) == REFUSED  # la segunda; la tercera, el tope
    assert act(ana, quote, "approve").status_code == 429
    for verb in ("reject", "withdraw-request", "cancel"):  # no miden el suelo: no cuentan
        act(ana, uuid4(), verb)
    keys = "SELECT key FROM login_throttles WHERE key LIKE %s"
    assert len(migrator.execute(keys, [f"qfl:usr:{lab.ana.pk}%"]).fetchall()) == 2


RACES = [("approve", "approve"), ("approve", "reject"), ("approve", "withdraw-request"),
         ("approve", "cancel"), ("cancel", "cancel")]  # fmt: skip


@pytest.mark.parametrize("pair", RACES)
def test_two_decisions_at_once_from_one_version_leave_one(
    lab: SimpleNamespace, migrator: DB, pair: tuple[str, str]
) -> None:
    quote, got, verbs = pending(lab), list[Any](), iter(pair)
    with migrator.transaction():  # los dos esperan la misma fila; luego, uno gana
        migrator.execute("SELECT 1 FROM quotes WHERE id = %s FOR UPDATE", [quote])
        workers = threaded(2, lambda: got.append(fail(act(signed(lab.ana), quote, next(verbs)))))
        waiting(migrator, 2)
    for worker in workers:
        worker.join(timeout=15)
    assert sorted(got, key=str) == [(200, None, []), (409, "STALE_VERSION", [])]
    assert len(decided(migrator)) == 1
    events = "SELECT count(*) FROM outbox_events WHERE event_type = 'quote.cancelled'"
    assert migrator.execute(events).fetchone() == (int("cancel" in decided(migrator)[0]),)


def test_a_failure_after_the_decision_rolls_the_request_back(
    lab: SimpleNamespace, migrator: DB, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("audit down")

    quote = pending(lab)
    monkeypatch.setattr(decisions, "record", broken)
    ana = signed(lab.ana)
    ana.raise_request_exception = False
    assert act(ana, quote, "approve").status_code == act(ana, quote, "reject").status_code == 500
    assert migrator.execute(STATE, [quote]).fetchone() == ("PENDING_APPROVAL", 2, "PENDING")
    assert decided(migrator) == []


def test_an_approver_within_a_policy_approves_the_team_quote(lab: SimpleNamespace) -> None:
    quote = pending(lab)  # 4200 de 4399: 4,53 %
    role = give(lab.a, lab.m["sam"], {})
    with tenant_scope(ctx(lab.a)):
        DiscountPolicy.objects.create(role_id=role.pk, max_discount_percent=Decimal(5))
    assert act(signed(lab.sam), quote, "approve").json()["status"] == "APPROVED"

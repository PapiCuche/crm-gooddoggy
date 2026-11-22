"""F8-12b (#1068): el aviso de los eventos del presupuesto de IA (ADR-045). Su handler del outbox
encola la tarea con solo ids; la tarea audita `ai.budget.alerted` una vez por aviso y mes y escribe
a cada miembro activo con `ai.manage` de la organización del evento, sin importes. PostgreSQL real
como `crm_app`, eventos de `ai_budget_reserve` y el correo en memoria (`locmem`)."""

from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from django.core import mail as sent
from django.db import OperationalError

from apps.ai_gateway import alerts, tasks
from apps.organizations.models import OrganizationMembership
from core import mail
from core.outbox import emit, handlers_for
from core.outbox.publisher import publish_outbox
from core.tenancy.context import TenantContext
from core.tenancy.scope import tenant_scope
from tests.factories import make_user
from tests.test_ai_budget_ledger import RESERVE, scalar
from tests.test_ai_gateway import budget
from tests.test_authorization import give
from tests.test_contacts_api import crm, real_stack
from tests.test_invitation_accept import held
from tests.test_memberships import ctx, join

__all__ = ["crm", "real_stack"]  # los fixtures, para pytest
pytestmark = pytest.mark.usefixtures("tenant_db")
DB = psycopg.Connection[Any]
THRESHOLD, EXCEEDED = alerts.THRESHOLD, alerts.EXCEEDED
CAP, EIGHTY = "123.456789", "98.765432"  # ningún correo lleva el tope ni lo gastado
ORIGIN = "https://app.example.com"
EVENT = "SELECT id FROM outbox_events WHERE organization_id = %s AND event_type = %s"
AUDIT = (
    "SELECT organization_id, actor_type, actor_id, entity_type, entity_id, metadata"
    " FROM audit_logs WHERE action = 'ai.budget.alerted' ORDER BY occurred_at, id"
)
CAP_ID = "SELECT id FROM ai_budgets WHERE organization_id = %s"
MONTH = "SELECT ai_budget_month(now())::text"


@pytest.fixture(autouse=True)
def origin(settings: Any) -> None:
    settings.APP_ORIGIN = ORIGIN


@pytest.fixture
def team(crm: SimpleNamespace, migrator: DB) -> SimpleNamespace:
    """En A: ana (Owner) e ines tienen `ai.manage`; vera (vendedora) no; olga lo tiene con la
    membresía suspendida y pia con la cuenta desactivada. En B: luis lo tiene. Topes en las dos."""
    ines, olga, pia = (make_user(email=f"{n}@example.com") for n in ("ines", "olga", "pia"))
    for user in (ines, olga, pia):
        give(crm.a, join(crm.a, user).pk, {"ai.manage": None})
    give(crm.b, crm.m_luis, {"ai.manage": None})
    migrator.execute("UPDATE organization_memberships SET status = 'SUSPENDED' WHERE user_id = %s",
                     [olga.pk])  # fmt: skip
    migrator.execute("UPDATE users SET is_active = false WHERE id = %s", [pia.pk])
    budget(migrator, crm.a, CAP)
    budget(migrator, crm.b, CAP)
    return SimpleNamespace(crm=crm, ines=ines, olga=olga)


def reserve(org: UUID, user: Any, amount: str) -> Any:
    with tenant_scope(ctx(org, user)):
        return scalar(RESERVE, [amount, "SUGGEST"])


def reach(org: UUID, user: Any) -> None:
    """El 80 % del tope y después el corte del mes: los dos eventos, de `ai_budget_reserve`."""
    reserve(org, user, EIGHTY)
    assert reserve(org, user, CAP) is None


def event(migrator: DB, org: UUID, kind: str) -> UUID:
    ((found,),) = migrator.execute(EVENT, [org, kind]).fetchall()
    return UUID(str(found))


def forge(
    org: UUID, kind: str, aggregate: Any, payload: Any, aggregate_type: str = "ai_budget"
) -> UUID:
    """Un evento que escribe el runtime (`crm_app` puede: ADR-045), no `ai_budget_reserve`."""
    with tenant_scope(ctx(org)) as tenant:
        return emit(tenant, kind, aggregate_type=aggregate_type, aggregate_id=aggregate,
                    payload=payload)  # fmt: skip


def value(migrator: DB, sql: str, params: list[Any] | None = None) -> Any:
    row = migrator.execute(sql, params).fetchone()
    assert row is not None
    return row[0]


def audit(migrator: DB) -> list[Any]:
    return migrator.execute(AUDIT).fetchall()


def to(outbox: list[Any]) -> list[str]:
    return [address for message in outbox for address in message.to]


def send(org: UUID, event_id: UUID) -> Any:
    kwargs = {"organization_id": str(org), "event_id": str(event_id)}
    return tasks.send_budget_alert.apply(kwargs=kwargs).get()


def test_the_outbox_handler_queues_one_send_with_ids_only(
    team: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, migrator: DB
) -> None:
    crm = team.crm
    reach(crm.a, crm.ana)
    for kind in (THRESHOLD, EXCEEDED):
        assert handlers_for(kind) == (tasks.budget_alert_event,)
    published: list[Any] = []
    queued: list[Any] = []
    monkeypatch.setattr(tasks.budget_alert_event, "apply_async", lambda **kw: published.append(kw))
    monkeypatch.setattr(tasks.send_budget_alert, "apply_async", lambda **kw: queued.append(kw))
    migrator.execute("DELETE FROM outbox_events WHERE event_type NOT LIKE 'ai.budget.%%'")
    assert publish_outbox() == 2  # los dos avisos salen del outbox
    for handled in published:
        assert tasks.budget_alert_event.apply(**handled).successful()
    ids = [{"organization_id": str(crm.a), "event_id": str(event(migrator, crm.a, kind))}
           for kind in (THRESHOLD, EXCEEDED)]  # fmt: skip
    assert queued == [{"kwargs": one} for one in ids]
    assert sent.outbox == [] and audit(migrator) == []  # el handler no avisa en su transacción
    other = forge(crm.a, "contact.created", crm.a, {})  # otro evento: el handler no encola nada
    assert tasks.budget_alert_event.apply(
        kwargs={"event_id": str(other), "organization_id": str(crm.a)}
    ).successful()
    assert len(queued) == 2


def test_each_alert_is_audited_once_and_mailed_to_each_ai_manager_only(
    team: SimpleNamespace, migrator: DB
) -> None:
    crm = team.crm
    reach(crm.a, crm.ana)
    reach(crm.b, crm.luis)
    month = value(migrator, MONTH)
    cap_a = value(migrator, CAP_ID, [crm.a])
    assert send(crm.a, event(migrator, crm.a, THRESHOLD)) == "sent"
    assert sorted(to(sent.outbox)) == ["a@example.com", "ines@example.com"]
    first = sent.outbox[0]
    assert first.subject == "El gasto de IA llegó al 80 % del tope del mes"
    assert "«Org A»" in first.body and "80 % del tope mensual de " in first.body
    assert f"\n{ORIGIN}/o/org-a/ia\n" in first.body
    assert "123" not in first.body and "98" not in first.body  # sin tope ni gasto
    assert all(message.body == first.body for message in sent.outbox)
    one = (crm.a, "SYSTEM", None, "ai_budget", cap_a,
           {"event": THRESHOLD, "month": month, "recipients": 2})  # fmt: skip
    assert audit(migrator) == [one]
    sent.outbox.clear()
    assert send(crm.a, event(migrator, crm.a, EXCEEDED)) == "sent"
    assert sent.outbox[0].subject == "El gasto de IA llegó al tope del mes"
    assert "llegó al tope mensual de " in sent.outbox[0].body
    assert len(sent.outbox) == 2 and len(audit(migrator)) == 2
    sent.outbox.clear()
    assert send(crm.b, event(migrator, crm.b, THRESHOLD)) == "sent"  # B: solo luis
    assert to(sent.outbox) == ["l@example.com"] and "/o/org-b/ia" in sent.outbox[0].body
    assert [row[0] for row in audit(migrator)] == [crm.a, crm.a, crm.b]


def test_another_delivery_or_a_forged_copy_alerts_nobody_again(
    team: SimpleNamespace, migrator: DB
) -> None:
    crm = team.crm
    reach(crm.a, crm.ana)
    threshold = event(migrator, crm.a, THRESHOLD)
    assert send(crm.a, threshold) == "sent"
    assert send(crm.a, threshold) == "duplicate"  # otra entrega del mismo evento
    cap_a = value(migrator, CAP_ID, [crm.a])
    month = value(migrator, MONTH)
    copy = forge(crm.a, THRESHOLD, cap_a, {"month": month})  # el runtime puede escribirlo
    assert send(crm.a, copy) == "duplicate"
    assert len(sent.outbox) == 2 and len(audit(migrator)) == 1
    assert send(crm.a, event(migrator, crm.a, EXCEEDED)) == "sent"  # el otro aviso, aparte


def test_a_concurrent_delivery_waits_for_the_first_and_does_not_repeat(
    team: SimpleNamespace, migrator: DB
) -> None:
    crm = team.crm
    reach(crm.a, crm.ana)
    threshold = event(migrator, crm.a, THRESHOLD)
    first, second = (
        (lambda tenant: alerts._record(tenant, threshold)),
        (lambda: send(crm.a, threshold)),
    )
    out = held(crm.a, first, second)
    assert out["first"][0] == THRESHOLD and out["second"] == "duplicate"
    assert len(audit(migrator)) == 1 and sent.outbox == []  # la primera no llegó a enviar


def test_an_event_without_its_mark_or_out_of_shape_or_of_another_org_alerts_nobody(
    team: SimpleNamespace, migrator: DB
) -> None:
    crm = team.crm
    cap_a = value(migrator, CAP_ID, [crm.a])
    cap_b = value(migrator, CAP_ID, [crm.b])
    month = value(migrator, MONTH)
    early = forge(crm.a, THRESHOLD, cap_a, {"month": month})
    assert send(crm.a, early) == "unmarked"
    reach(crm.b, crm.luis)  # B tiene las marcas del mes; A no
    assert send(crm.a, forge(crm.a, EXCEEDED, cap_a, {"month": month})) == "unmarked"
    foreign = forge(crm.a, THRESHOLD, cap_b, {"month": month})  # el tope de B, en un evento de A
    assert send(crm.a, foreign) == "budget"
    reserve(crm.a, crm.ana, EIGHTY)  # A, solo la del 80 %
    (real,) = {row[0] for row in migrator.execute(EVENT, [crm.a, THRESHOLD]).fetchall()} - {
        early,
        foreign,
    }
    assert send(crm.a, forge(crm.a, EXCEEDED, cap_a, {"month": month})) == "unmarked"
    for payload in ({}, {"month": month[:8] + "02"}, {"month": 1}, {"month": "2026-13-01"}):
        assert send(crm.a, forge(crm.a, THRESHOLD, cap_a, payload)) == "payload"
    assert send(crm.a, forge(crm.a, THRESHOLD, cap_a, {"month": month}, "budget")) == "event"
    assert send(crm.a, forge(crm.a, "ai.budget.other", cap_a, {"month": month})) == "event"
    assert send(crm.a, event(migrator, crm.b, THRESHOLD)) == "event"  # de B, pedido para A
    assert send(crm.a, uuid4()) == "event"
    assert sent.outbox == [] and audit(migrator) == []
    migrator.execute("UPDATE organizations SET status = 'SUSPENDED' WHERE id = %s", [crm.a])
    assert send(crm.a, real) == "organization"
    assert sent.outbox == [] and audit(migrator) == []


def test_a_failed_delivery_does_not_stop_the_others_and_is_not_retried(
    team: SimpleNamespace, migrator: DB, monkeypatch: pytest.MonkeyPatch
) -> None:
    crm = team.crm
    reach(crm.a, crm.ana)
    rosa = make_user(email="rosa@example.com")  # con `ai.manage` y una dirección imposible
    give(crm.a, join(crm.a, rosa).pk, {"ai.manage": None})
    migrator.execute("UPDATE users SET email = 'rosa@' WHERE id = %s", [rosa.pk])
    real = mail.send

    def flaky(message: mail.Message) -> None:
        if message.to == "a@example.com":
            raise mail.MailError("SMTPDataError")
        real(message)

    monkeypatch.setattr(mail, "send", flaky)
    assert send(crm.a, event(migrator, crm.a, THRESHOLD)) == "sent"
    assert to(sent.outbox) == ["ines@example.com"]
    assert audit(migrator)[0][5]["recipients"] == 2  # sin la dirección imposible
    assert send(crm.a, event(migrator, crm.a, THRESHOLD)) == "duplicate"


def test_without_mail_the_alert_is_still_audited_and_a_db_failure_records_nothing(
    team: SimpleNamespace, migrator: DB, settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    crm = team.crm
    reach(crm.a, crm.ana)
    settings.DEFAULT_FROM_EMAIL = ""
    assert send(crm.a, event(migrator, crm.a, THRESHOLD)) == "unsent"
    assert sent.outbox == [] and len(audit(migrator)) == 1

    def broken(*args: Any) -> None:
        raise OperationalError("la base")

    monkeypatch.setattr(alerts, "_record", broken)
    with pytest.raises(alerts.NotRecorded):  # se reintenta: nada quedó
        alerts.send(crm.a, event(migrator, crm.a, EXCEEDED))
    assert len(audit(migrator)) == 1


def test_holders_are_the_active_members_with_the_permission_of_the_scope_org(
    team: SimpleNamespace,
) -> None:
    from apps.access.selectors import holders

    crm = team.crm
    with tenant_scope(TenantContext(crm.a, "test")) as tenant:
        found = holders(tenant, "ai.manage")
        assert {email for _, email in found} == {"a@example.com", "ines@example.com"}
        assert [user for user, _ in found] == sorted(user for user, _ in found)
        statuses = OrganizationMembership.objects.values_list("status", flat=True)
        assert "SUSPENDED" in statuses  # olga sigue siendo de A, suspendida
    with tenant_scope(TenantContext(crm.b, "test")) as tenant:
        assert holders(tenant, "ai.manage") == [(crm.luis.pk, "l@example.com")]

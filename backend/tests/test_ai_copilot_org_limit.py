"""F8-08m (#1047; ADR-034 §5, §7): el tope por organización de sugerir y resumir, junto al de cada
persona, de punta a punta con middleware, sesión, CSRF, motor de autorización y PostgreSQL con
`crm_app`. La organización es la de la sesión; se cuenta tras el permiso y antes del runtime; un
anónimo, un 403, un 404 o el tope por persona no lo gastan; el presupuesto sigue siendo el tope
final. Datos ficticios."""

from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from django.test import Client

from tests.factories import make_user
from tests.test_ai_copilot_context import conversation
from tests.test_ai_copilot_runtime import PLAIN, desk
from tests.test_ai_copilot_suggest_api import DB, SPENT, URL
from tests.test_ai_copilot_suggest_api import ask as suggest
from tests.test_ai_copilot_summary_api import ask as summary
from tests.test_ai_copilot_summary_api import said
from tests.test_ai_gateway import world
from tests.test_authorization import give
from tests.test_memberships import join
from tests.test_self_context import real_stack
from tests.test_self_context import signed as plain
from tests.test_team_writes import send

__all__ = ["desk", "real_stack", "world"]  # los fixtures, para pytest
pytestmark = pytest.mark.usefixtures("tenant_db", "real_stack")
BOTH = {"ai.use": None, "conversations.view": "ORGANIZATION"}
LIMITED = (429, {"code": "AI_ORG_RATE_LIMITED"})
PEOPLE = "SELECT key, failures FROM login_throttles WHERE key LIKE 'aic:%'"
ORGS = "SELECT key, failures FROM login_throttles WHERE key LIKE 'aio:%'"


def member(org: UUID, email: str, grants: dict[str, str | None] = BOTH) -> Any:
    user = make_user(email=email)
    give(org, join(org, user).pk, grants)
    return user


def counted(migrator: DB, sql: str = ORGS) -> dict[str, int]:
    return dict(migrator.execute(sql).fetchall())


def test_the_organization_limit_trips_while_each_person_is_under_theirs(
    desk: SimpleNamespace, migrator: DB, settings: Any
) -> None:
    assert settings.AI_COPILOT_ORG_THROTTLE == (120, 600)  # provisional, pendiente del propietario
    settings.AI_COPILOT_ORG_THROTTLE = (3, 600)
    bob, leo = member(desk.a, "bob@example.com"), member(desk.a, "leo@example.com")
    desk.fake.replies = [PLAIN, said("Pide un collar."), PLAIN]
    served = [suggest(desk), summary(desk, bob), suggest(desk, leo)]
    assert [a.status_code for a in served] == [201] * 3
    calls, spent = len(desk.fake.calls), migrator.execute(SPENT).fetchone()
    limited = [summary(desk), suggest(desk, bob)]
    assert [(a.status_code, a.json()) for a in limited] == [LIMITED] * 2
    assert all(0 < int(a["Retry-After"]) <= 600 for a in limited)
    # ni llamada, ni reserva, ni run, ni borrador: el tope corta antes del runtime
    assert len(desk.fake.calls) == calls and migrator.execute(SPENT).fetchone() == spent
    people = {f"aic:usr:{desk.ana.pk}": 2, f"aic:usr:{bob.pk}": 2, f"aic:usr:{leo.pk}": 1}
    assert counted(migrator, PEOPLE) == people  # cada persona, lejos de sus 20
    assert counted(migrator) == {f"aio:org:{desk.a}": 5}


def test_another_organization_has_its_own_counter_and_the_body_never_picks_one(
    desk: SimpleNamespace, migrator: DB, settings: Any
) -> None:
    settings.AI_COPILOT_ORG_THROTTLE = (1, 600)
    desk.fake.replies = [PLAIN]
    assert suggest(desk).status_code == 201
    url, body = URL.format("org-a", desk.convo), {"organization_id": str(desk.b)}
    again = send(plain(desk.ana), "post", url, body)
    assert (again.status_code, again.json()) == LIMITED
    bob = member(desk.b, "bob@example.com")
    theirs = conversation(desk.b, {"display_name": "Bea"})
    other = summary(desk, bob, theirs, org="org-b")  # B no tiene cuenta: llega al runtime
    assert (other.status_code, other.json()) == (409, {"code": "AI_NOT_CONFIGURED"})
    assert counted(migrator) == {f"aio:org:{desk.a}": 2, f"aio:org:{desk.b}": 1}


def test_anonymous_refused_and_out_of_scope_requests_spend_nothing_of_the_organization(
    desk: SimpleNamespace, migrator: DB
) -> None:
    eva = member(desk.a, "eva@example.com", {"conversations.view": "ORGANIZATION"})
    zoe = member(desk.a, "zoe@example.com", {"ai.use": None})
    leo = member(desk.a, "leo@example.com", {"ai.use": None, "conversations.view": "OWN"})
    bob = member(desk.b, "bob@example.com")
    hidden = conversation(desk.a, {"display_name": "Otro"}, assigned_user_id=desk.ana.pk)
    anonymous = send(Client(enforce_csrf_checks=True), "post", URL.format("org-a", desk.convo), {})
    answers = [anonymous, suggest(desk, eva), summary(desk, zoe), summary(desk, leo, hidden),
               suggest(desk, convo=uuid4()), suggest(desk, bob)]  # fmt: skip
    assert [a.status_code for a in answers] == [401, 403, 403, 404, 404, 404]
    assert desk.fake.calls == [] and migrator.execute(SPENT).fetchone() == (0, 0, 0)
    assert sum(counted(migrator).values()) == 0  # el 404 del runtime devuelve el suyo
    assert counted(migrator, PEOPLE)[f"aic:usr:{eva.pk}"] == 1  # el de la persona, sí


def test_a_person_over_their_own_limit_never_reaches_the_organization_counter(
    desk: SimpleNamespace, migrator: DB, settings: Any
) -> None:
    settings.AI_COPILOT_THROTTLE = (1, 600)
    desk.fake.replies = [PLAIN]
    assert suggest(desk).status_code == 201
    assert (summary(desk).status_code, summary(desk).json()) == (429, {"code": "RATE_LIMITED"})
    assert counted(migrator) == {f"aio:org:{desk.a}": 1}


def test_the_budget_is_still_the_final_cap_under_both_limits(
    desk: SimpleNamespace, migrator: DB
) -> None:
    migrator.execute("UPDATE ai_budgets SET daily_usd = 0.01")  # menos que una llamada
    answers = [suggest(desk), summary(desk)]
    assert [(a.status_code, a.json()) for a in answers] == [
        (409, {"code": "AI_BUDGET_EXHAUSTED"})
    ] * 2
    assert desk.fake.calls == []
    assert counted(migrator) == {f"aio:org:{desk.a}": 2}  # llegaron al runtime: cuentan

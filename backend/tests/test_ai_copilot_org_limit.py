"""F8-08m (#1047; ADR-034 §5, §7): el tope por organización de sugerir y resumir, junto al de cada
persona, de punta a punta con middleware, sesión, CSRF, motor de autorización y PostgreSQL con
`crm_app`. La organización es la de la sesión; se cuenta tras el permiso y antes del runtime; un
anónimo, un 403, un 404 o el tope por persona no lo gastan; el presupuesto sigue siendo el tope
final. Datos ficticios."""

import ast
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from django.test import Client

from apps.ai_agents import copilot
from apps.ai_gateway import throttle
from tests.factories import make_user
from tests.test_ai_copilot_context import conversation
from tests.test_ai_copilot_insert_api import URL as INSERT
from tests.test_ai_copilot_runtime import PLAIN, desk
from tests.test_ai_copilot_suggest_api import DB, SPENT, URL
from tests.test_ai_copilot_suggest_api import ask as suggest
from tests.test_ai_copilot_summary_api import ask as summary
from tests.test_ai_copilot_summary_api import said
from tests.test_ai_gateway import world
from tests.test_ai_tools_hardening import BACKEND
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


def test_a_runtime_404_gives_back_exactly_its_own_request(
    desk: SimpleNamespace, migrator: DB
) -> None:
    desk.fake.replies = [PLAIN]
    assert suggest(desk).status_code == 201
    assert suggest(desk, convo=uuid4()).status_code == 404
    assert counted(migrator) == {f"aio:org:{desk.a}": 1}  # el servido sigue contado


def test_a_request_given_back_never_comes_off_a_newer_window(
    desk: SimpleNamespace, migrator: DB, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = copilot.suggest
    aged = "UPDATE login_throttles SET updated_at = updated_at - interval '601 seconds'"

    def late(ctx: Any, conversation_id: UUID) -> Any:
        """Mientras esta sigue en el runtime, su ventana vence y otra petición abre una nueva; el
        runtime la rechaza después (F8-13: la ruta ya la admitió antes de contar)."""
        migrator.execute(f"{aged} WHERE key LIKE 'aio:%'")
        assert throttle.organization_wait(ctx.organization_id)[0] == 0
        return real(ctx, uuid4())

    monkeypatch.setattr(copilot, "suggest", late)
    assert suggest(desk).status_code == 404
    assert counted(migrator) == {f"aio:org:{desk.a}": 1}  # la nueva ventana conserva su pedido


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


def test_the_insert_route_never_touches_the_organization_counter(
    desk: SimpleNamespace, migrator: DB, settings: Any
) -> None:
    """Insertar (F8-08j) no llama al modelo: ni cuenta ni lo corta el tope de la organización."""
    settings.AI_COPILOT_ORG_THROTTLE = (1, 600)
    desk.fake.replies = [PLAIN]
    made = suggest(desk).json()["id"]
    assert (suggest(desk).status_code, suggest(desk).json()) == LIMITED  # ya en su tope
    url, ana = INSERT.format("org-a", desk.convo, made), plain(desk.ana)
    assert [send(ana, "post", url, {}).status_code for _ in range(3)] == [200] * 3
    assert counted(migrator) == {f"aio:org:{desk.a}": 3}  # solo los tres de sugerir


def test_the_counter_module_imports_nothing_from_apps() -> None:
    """La vista del copiloto lo importa por una excepción del contrato de su ruta (pyproject.toml):
    sin otro módulo de `apps`, no llega al gateway, la clave ni el interruptor. `core` ya no importa
    `apps` (su contrato), así que basta con sus imports directos."""
    tree = ast.parse((BACKEND / "apps/ai_gateway/throttle.py").read_text())
    relative = [node for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.level]
    found = {node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    found |= {a.name for node in ast.walk(tree) if isinstance(node, ast.Import) for a in node.names}
    assert found and not relative and not [n for n in found if n.split(".")[0] == "apps"]

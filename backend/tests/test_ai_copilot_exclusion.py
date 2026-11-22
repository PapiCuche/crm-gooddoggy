"""F8-13 (#1071): una conversación excluida de la IA nunca llega al proveedor, ni por la API ni
llamando al runtime directamente: ni su contexto se lee, ni se reserva presupuesto, ni cuenta en los
topes; la marca puesta a mitad corta la llamada siguiente. Con la API falsa (`tests.ai_sim`) que
cuenta llamadas, middleware, sesión y PostgreSQL con `crm_app`. Datos ficticios."""

from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock
from uuid import UUID

import pytest
from django.db import OperationalError
from django.utils import timezone

from apps.ai_agents import context, copilot, registry
from apps.conversations import selectors
from apps.conversations.models import Conversation
from core.tenancy.scope import tenant_scope
from tests.test_ai_copilot_runtime import GOOD, PLAIN, SEARCH, desk
from tests.test_ai_copilot_suggest_api import COUNTED, DB, SPENT
from tests.test_ai_copilot_suggest_api import ask as suggest
from tests.test_ai_copilot_summary_api import ask as summary
from tests.test_ai_copilot_summary_api import said
from tests.test_ai_gateway import world
from tests.test_memberships import ctx
from tests.test_self_context import real_stack

__all__ = ["desk", "real_stack", "world"]  # los fixtures, para pytest
pytestmark = pytest.mark.usefixtures("tenant_db", "real_stack")
EXCLUDED = (409, {"code": "AI_EXCLUDED"})
ASKS = {"suggest": suggest, "summary": summary}


def mark(w: SimpleNamespace, on: bool = True) -> None:
    with tenant_scope(ctx(w.a)):
        Conversation.objects.filter(pk=w.convo).update(
            ai_excluded_at=timezone.now() if on else None,
            ai_excluded_by_user_id=w.ana.pk if on else None,
        )


@pytest.fixture
def reads(desk: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Cada lectura del contexto (la llamada sigue igual)."""
    seen: list[str] = []
    for owner, name in ((context, "build"), (context, "customer_since_reply"),
                        (selectors, "messages")):  # fmt: skip
        real = getattr(owner, name)

        def spy(*args: Any, _real: Any = real, _name: str = name, **kwargs: Any) -> Any:
            seen.append(_name)
            return _real(*args, **kwargs)

        monkeypatch.setattr(owner, name, spy)
    return seen


def spent_nothing(migrator: DB) -> bool:
    counted = [failures for _, failures in migrator.execute(COUNTED).fetchall()]
    return migrator.execute(SPENT).fetchone() == (0, 0, 0) and not any(counted)


@pytest.mark.parametrize("route", ASKS)
def test_an_excluded_conversation_reads_calls_reserves_and_counts_nothing(
    desk: SimpleNamespace, migrator: DB, reads: list[str], route: str
) -> None:
    mark(desk)
    answers = [ASKS[route](desk) for _ in range(3)]
    assert [(a.status_code, a.json()) for a in answers] == [EXCLUDED] * 3
    assert desk.fake.calls == [] and reads == [] and spent_nothing(migrator)
    # antes del tope de la organización (ni lo cuenta); el de la persona, devuelto cada vez
    assert migrator.execute(COUNTED).fetchall() == [(f"aic:usr:{desk.ana.pk}", 0)]
    mark(desk, on=False)  # incluida otra vez, el copiloto vuelve a responder
    desk.fake.replies = [PLAIN if route == "suggest" else said("Pide un collar.")]
    assert ASKS[route](desk).status_code == 201 and "build" in reads


@pytest.mark.parametrize("run", [copilot.suggest, copilot.summarize])
def test_the_runtime_called_directly_refuses_it_before_anything(
    desk: SimpleNamespace, migrator: DB, reads: list[str], run: Any
) -> None:
    mark(desk)
    with pytest.raises(copilot.CopilotError) as caught:
        run(ctx(desk.a, desk.ana), desk.convo)
    assert (caught.value.code, caught.value.run_id) == ("AI_EXCLUDED", None)
    assert desk.fake.calls == [] and reads == [] and spent_nothing(migrator)


def test_excluded_after_the_route_admitted_it_is_refused_before_its_context(
    desk: SimpleNamespace, migrator: DB, reads: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    admit = copilot.admit

    def admitted(ctx_: Any, conversation_id: UUID) -> None:
        admit(ctx_, conversation_id)
        mark(desk)  # alguien la excluye justo después

    monkeypatch.setattr(copilot, "admit", admitted)
    answer = suggest(desk)
    assert (answer.status_code, answer.json()) == EXCLUDED
    assert desk.fake.calls == [] and reads == [] and spent_nothing(migrator)  # ambos devueltos


def test_excluded_between_two_calls_of_one_run_stops_the_next_call(
    desk: SimpleNamespace, migrator: DB, monkeypatch: pytest.MonkeyPatch
) -> None:
    invoke = registry.invoke_read_only

    def tool(*args: Any) -> Any:
        mark(desk)  # mientras corre la tool de la primera respuesta
        return invoke(*args)

    monkeypatch.setattr(registry, "invoke_read_only", tool)
    desk.fake.replies = [SEARCH, GOOD]
    answer = suggest(desk)
    assert (answer.status_code, answer.json()) == EXCLUDED
    assert len(desk.fake.calls) == 1  # la segunda nunca sale
    runs = "SELECT outcome, cardinality(reservation_ids) FROM ai_runs"
    assert migrator.execute(runs).fetchall() == [("AI_EXCLUDED", 1)]  # la pagada, con su run
    assert migrator.execute(SPENT).fetchone() == (1, 1, 0)  # sin borrador
    counted = dict(migrator.execute(COUNTED).fetchall())
    assert counted == {f"aic:usr:{desk.ana.pk}": 1, f"aio:org:{desk.a}": 1}  # sí consumió


def test_a_mark_that_cannot_be_read_calls_nothing(
    desk: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    broken = Mock()
    broken.objects.filter.side_effect = OperationalError("sin lectura")
    monkeypatch.setattr(copilot, "Conversation", broken)
    desk.fake.replies = [PLAIN]
    for run in (copilot.suggest, copilot.summarize):
        with pytest.raises(OperationalError):
            run(ctx(desk.a, desk.ana), desk.convo)
    assert desk.fake.calls == []

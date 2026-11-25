"""F8-13 (#1071): una conversación excluida de la IA nunca llega al proveedor, ni por la API ni
llamando al runtime directamente: ni su contexto se lee, ni se reserva presupuesto, ni cuenta en los
topes salvo el de la persona (F8-13c); la marca puesta a mitad corta la llamada siguiente, la que
está en curso (excluir espera a que termine y ninguna empieza después) y el reintento que espera su
`retry-after` (F8-13c). Con la API falsa
(`tests.ai_sim`) que cuenta llamadas, middleware, sesión y PostgreSQL con `crm_app`; la
concurrencia, con hilos y conexiones reales. Datos ficticios."""

import ast
import pathlib
import threading
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock
from uuid import UUID, uuid4

import pytest
from django.conf import settings as settings_module
from django.db import OperationalError, connection
from django.utils import timezone

from apps.ai_agents import context, copilot, registry
from apps.ai_gateway import gateway
from apps.audit.services import record as audit_record
from apps.conversations import attention, selectors
from apps.conversations.models import Conversation
from core.tenancy.scope import tenant_scope
from tests.factories import make_user
from tests.test_ai_copilot_runtime import GOOD, PLAIN, SEARCH, desk
from tests.test_ai_copilot_suggest_api import COUNTED, DB, SPENT
from tests.test_ai_copilot_suggest_api import ask as suggest
from tests.test_ai_copilot_summary_api import ask as summary
from tests.test_ai_copilot_summary_api import said
from tests.test_ai_gateway import world
from tests.test_ai_kill_switch import ahead, later
from tests.test_authorization import give
from tests.test_conversations_api import BASE
from tests.test_memberships import ctx, join
from tests.test_self_context import real_stack, signed
from tests.test_team_writes import send

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


def spent_nothing(migrator: DB, person: int = 0) -> bool:
    """Ni reserva, ni run, ni borrador; el tope de la organización, sin contar; el de la persona,
    `person` (F8-13c: una exclusión cuenta en el de la persona)."""
    counted = dict(migrator.execute(COUNTED).fetchall())
    mine = {key: n for key, n in counted.items() if key.startswith("aic:usr:")}
    others = [n for key, n in counted.items() if not key.startswith("aic:usr:")]
    return (migrator.execute(SPENT).fetchone() == (0, 0, 0) and not any(others)
            and sum(mine.values()) == person)  # fmt: skip


RUNS = "SELECT outcome, cardinality(reservation_ids) FROM ai_runs"
LEDGER = "SELECT outcome, cost_usd FROM ai_llm_calls"
USAGE = "SELECT spent_usd, reserved_usd FROM ai_budget_usage"


@pytest.mark.parametrize("route", ASKS)
def test_an_excluded_conversation_reads_calls_and_reserves_nothing_and_counts_for_the_person(
    desk: SimpleNamespace, migrator: DB, reads: list[str], route: str
) -> None:
    mark(desk)
    answers = [ASKS[route](desk) for _ in range(3)]
    assert [(a.status_code, a.json()) for a in answers] == [EXCLUDED] * 3
    assert desk.fake.calls == [] and reads == [] and spent_nothing(migrator, person=3)
    # antes del tope de la organización (ni lo cuenta); el de la persona cuenta cada vez (F8-13c)
    assert migrator.execute(COUNTED).fetchall() == [(f"aic:usr:{desk.ana.pk}", 3)]
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
    # la organización, devuelta; la persona, contada (F8-13c)
    assert desk.fake.calls == [] and reads == [] and spent_nothing(migrator, person=1)


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


def test_excluded_after_the_context_and_before_the_first_call_reserves_and_calls_nothing(
    desk: SimpleNamespace, migrator: DB, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepare = copilot._prepare

    def prepared(*args: Any) -> Any:
        built = prepare(*args)
        mark(desk)  # ya leído el contexto, antes de la primera reserva
        return built

    monkeypatch.setattr(copilot, "_prepare", prepared)
    desk.fake.replies = [PLAIN]
    answer = suggest(desk)
    assert (answer.status_code, answer.json()) == EXCLUDED and desk.fake.calls == []
    assert migrator.execute(RUNS).fetchall() == [("AI_EXCLUDED", 0)]  # el run, sin reservas
    assert migrator.execute(SPENT).fetchone() == (0, 1, 0)
    counted = dict(migrator.execute(COUNTED).fetchall())  # con su run: los dos cuentan
    assert counted == {f"aic:usr:{desk.ana.pk}": 1, f"aio:org:{desk.a}": 1}


BROKEN = attention.INCLUDED.replace("c.id = %s", "c.nope = %s")  # un error real de PostgreSQL


def test_a_conversation_that_cannot_be_read_counts_as_excluded_and_a_lost_lock_is_an_error(
    desk: SimpleNamespace,
) -> None:
    """Sin fila visible (inexistente o de otra organización, por RLS) o con la marca: `False`.
    Incluida pero sin el candado en esta sesión (nunca tomado, soltado o de otra sesión):
    `CallLockLost` (F8-13d: un fallo, no una exclusión). Nunca `True` sin el candado."""
    mine, other = ctx(desk.a, desk.ana), ctx(desk.b, desk.ana)
    with attention.ai_call(mine, desk.convo, 5), tenant_scope(mine):
        assert attention.ai_included(mine, desk.convo) is True
        assert attention.ai_included(mine, uuid4()) is False
    with tenant_scope(mine), pytest.raises(attention.CallLockLost):
        attention.ai_included(mine, desk.convo)  # ya soltado
    with attention.ai_call(mine, uuid4(), 5), tenant_scope(mine):  # el de otra conversación
        with pytest.raises(attention.CallLockLost):
            attention.ai_included(mine, desk.convo)
    with attention.ai_call(other, desk.convo, 5), tenant_scope(other):
        assert attention.ai_included(other, desk.convo) is False
    held, done = threading.Event(), threading.Event()

    def hold() -> None:  # otra sesión tiene el candado: no vale para esta
        with attention.ai_call(mine, desk.convo, 5):
            held.set()
            done.wait(10)

    holder = later(hold)
    assert held.wait(10)
    with tenant_scope(mine), pytest.raises(attention.CallLockLost):
        attention.ai_included(mine, desk.convo)
    done.set()
    holder.join(10)
    mark(desk)
    with tenant_scope(mine):  # con la marca, sin candado: excluida (es cierto)
        assert attention.ai_included(mine, desk.convo) is False


def test_the_organization_switch_is_checked_before_the_exclusion_lock(
    desk: SimpleNamespace, migrator: DB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """El interruptor sigue primero en la transacción de la reserva: apagada, ni mira la marca."""
    seen: list[None] = []
    monkeypatch.setattr(attention, "ai_included", ahead(lambda: seen.append(None),
                                                        attention.ai_included))  # fmt: skip
    migrator.execute("UPDATE ai_switches SET enabled = false WHERE organization_id = %s", [desk.a])
    with pytest.raises(copilot.CopilotError) as caught:
        copilot.suggest(ctx(desk.a, desk.ana), desk.convo)
    assert caught.value.code == "AI_DISABLED" and seen == [] and desk.fake.calls == []


def test_a_database_error_in_the_check_before_reserving_calls_and_reserves_nothing(
    desk: SimpleNamespace, migrator: DB, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(attention, "INCLUDED", BROKEN)  # un error real de PostgreSQL
    desk.fake.replies = [PLAIN]
    with pytest.raises(copilot.CopilotError) as caught:
        copilot.suggest(ctx(desk.a, desk.ana), desk.convo)
    assert caught.value.code == "INTERNAL_ERROR" and caught.value.run_id is not None
    assert desk.fake.calls == [] and migrator.execute(RUNS).fetchall() == [("INTERNAL_ERROR", 0)]
    assert migrator.execute(SPENT).fetchone() == (0, 1, 0)  # ninguna reserva


def test_a_retry_whose_check_passes_is_still_sent(desk: SimpleNamespace, migrator: DB) -> None:
    desk.fake.script, desk.fake.replies = ["rate_limit"], [PLAIN]
    copilot.suggest(ctx(desk.a, desk.ana), desk.convo)
    assert len(desk.fake.calls) == 2 and migrator.execute(RUNS).fetchall() == [("OK", 1)]


@pytest.mark.parametrize(("during", "outcome"), [
    ("excluded", "AI_EXCLUDED"), ("ai.use", "ACCESS_DENIED"), ("broken", "INTERNAL_ERROR"),
])  # fmt: skip
def test_nothing_is_resent_after_a_retry_wait_once_it_is_withheld(
    desk: SimpleNamespace, migrator: DB, monkeypatch: pytest.MonkeyPatch, during: str, outcome: str
) -> None:
    """Un 429; en su espera, la marca, sin `ai.use` o la comprobación rota: no se reenvía nada."""

    drop = "DELETE FROM role_permissions WHERE permission_code = 'ai.use'"

    def wait(_: float) -> None:
        {
            "excluded": lambda: mark(desk),
            "ai.use": lambda: migrator.execute(drop),
            "broken": lambda: monkeypatch.setattr(attention, "INCLUDED", BROKEN),
        }[during]()  # type: ignore[no-untyped-call]

    monkeypatch.setattr(time, "sleep", wait)
    desk.fake.script, desk.fake.replies = ["rate_limit"], [PLAIN]
    with pytest.raises(copilot.CopilotError) as caught:
        copilot.suggest(ctx(desk.a, desk.ana), desk.convo)
    assert caught.value.code == outcome and len(desk.fake.calls) == 1
    assert migrator.execute(RUNS).fetchall() == [(outcome, 1)]  # con su reserva
    assert migrator.execute(LEDGER).fetchall() == [("PROVIDER_UNAVAILABLE", 0)]  # liberada
    assert migrator.execute(USAGE).fetchone() == (0, 0)


def exclude(desk: SimpleNamespace) -> None:
    """El comando del `PUT …/ai-exclusion/`, con su propio `lock_timeout`: un bloqueo mutuo o una
    espera sin fin fallan en segundos, no cuelgan la prueba."""
    with tenant_scope(ctx(desk.a, desk.ana)) as tenant, connection.cursor() as cursor:
        cursor.execute("SET LOCAL lock_timeout = '10s'")
        version = Conversation.objects.get(pk=desk.convo).version
        attention.set_ai_excluded(tenant, conversation_id=desk.convo, excluded=True,
                                  version=version)  # fmt: skip


def excluded_at(migrator: DB, desk: SimpleNamespace) -> Any:
    sql = "SELECT ai_excluded_at FROM conversations WHERE id = %s"
    return migrator.execute(sql, [desk.convo]).fetchone()[0]  # type: ignore[index]


def put(desk: SimpleNamespace) -> Any:
    """El `PUT …/ai-exclusion/` real (middleware, sesión, transacción de la petición)."""
    version = migrator_version(desk)
    return send(signed(desk.ana), "put", f"{BASE}{desk.convo}/ai-exclusion/",
                {"excluded": True, "version": version})  # fmt: skip


def migrator_version(desk: SimpleNamespace) -> int:
    with tenant_scope(ctx(desk.a)):
        return int(Conversation.objects.get(pk=desk.convo).version)


def in_flight(desk: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """El proveedor falso espera a `gate`; `seen`: cuándo empieza y vuelve cada llamada."""
    w = SimpleNamespace(inside=threading.Event(), gate=threading.Event(), seen=list[Any]())
    real = desk.fake.handle

    def handle(*args: Any) -> Any:
        w.seen.append(("start", time.monotonic()))
        w.inside.set()
        w.gate.wait(15)
        try:
            return real(*args)
        finally:
            w.seen.append(("back", time.monotonic()))

    monkeypatch.setattr(desk.fake, "handle", handle)
    return w


def asked(desk: SimpleNamespace, outcome: list[str]) -> None:
    try:
        copilot.suggest(ctx(desk.a, desk.ana), desk.convo)
        outcome.append("OK")
    except copilot.CopilotError as stop:
        outcome.append(stop.code)


def test_an_exclusion_waits_for_the_call_in_flight_and_no_call_starts_after_its_200(
    desk: SimpleNamespace, migrator: DB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hilos y conexiones reales: la primera llamada está en el proveedor cuando llega el `PUT`;
    el `PUT` responde solo después de que vuelve, y la segunda llamada del run (tras la tool) ya
    no empieza: ninguna llamada empieza después del 200."""
    w, outcome, answered = in_flight(desk, monkeypatch), list[str](), list[Any]()
    desk.fake.replies = [SEARCH, GOOD]
    asking = later(lambda: asked(desk, outcome))
    assert w.inside.wait(10)
    putting = later(lambda: answered.append((put(desk), time.monotonic())))
    putting.join(1)
    assert putting.is_alive() and excluded_at(migrator, desk) is None  # espera a la llamada
    w.gate.set()
    for thread in (asking, putting):
        thread.join(20)
        assert not thread.is_alive()
    [(answer, at)] = answered
    assert answer.status_code == 200 and outcome == ["AI_EXCLUDED"]
    assert [kind for kind, _ in w.seen] == ["start", "back"]  # una sola llamada
    assert all(t < at for _, t in w.seen)  # empezó y volvió antes del 200
    assert migrator.execute(RUNS).fetchall() == [("AI_EXCLUDED", 1)]


def test_two_calls_on_the_same_conversation_do_not_wait_for_each_other(
    desk: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """El candado de la llamada es compartido: solo excluir espera."""
    w, outcome = in_flight(desk, monkeypatch), list[str]()
    desk.fake.replies = [PLAIN]
    callers = [later(lambda: asked(desk, outcome)) for _ in range(2)]
    both = any(len(w.seen) == 2 or threading.Event().wait(0.05) for _ in range(200))
    w.gate.set()
    for thread in callers:
        thread.join(20)
    assert both and outcome == ["OK", "OK"]  # las dos en el proveedor a la vez


def test_an_exclusion_that_cannot_wait_long_enough_changes_nothing(
    desk: SimpleNamespace, migrator: DB, monkeypatch: pytest.MonkeyPatch, settings: Any
) -> None:
    settings.AI_EXCLUSION_WAIT_SECONDS = 1
    w, outcome = in_flight(desk, monkeypatch), list[str]()
    desk.fake.replies = [PLAIN]
    version = migrator_version(desk)
    asking = later(lambda: asked(desk, outcome))
    assert w.inside.wait(10)
    answer = put(desk)  # la llamada sigue en el proveedor más de 1 s
    assert (answer.status_code, answer.json()["code"]) == (409, "AI_CALL_IN_PROGRESS")
    assert excluded_at(migrator, desk) is None and migrator_version(desk) == version
    audited = "SELECT count(*) FROM audit_logs WHERE action = 'conversation.ai_excluded'"
    assert migrator.execute(audited).fetchone() == (0,)
    w.gate.set()
    asking.join(20)
    assert outcome == ["OK"]
    assert put(desk).status_code == 200  # ya sin llamadas en curso


def test_who_cannot_see_the_conversation_never_waits_for_its_calls(
    desk: SimpleNamespace, migrator: DB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """El alcance, antes del candado: fuera de él, el 404 enseguida (sin esperar ni delatarla)."""
    assign = "UPDATE conversations SET assigned_user_id = %s, assigned_at = now() WHERE id = %s"
    migrator.execute(assign, [desk.ana.pk, desk.convo])
    eva = make_user(email="eva@example.com")
    give(desk.a, join(desk.a, eva).pk, {"conversations.view": "OWN"})
    w, outcome = in_flight(desk, monkeypatch), list[str]()
    desk.fake.replies = [PLAIN]
    asking = later(lambda: asked(desk, outcome))
    assert w.inside.wait(10)
    started = time.monotonic()
    answer = send(signed(eva), "put", f"{BASE}{desk.convo}/ai-exclusion/",
                  {"excluded": True, "version": migrator_version(desk)})  # fmt: skip
    assert answer.status_code == 404 and time.monotonic() - started < 5
    w.gate.set()
    asking.join(20)
    assert outcome == ["OK"] and excluded_at(migrator, desk) is None


def test_excluding_has_its_own_per_person_limit(
    desk: SimpleNamespace, migrator: DB, settings: Any
) -> None:
    settings.AI_EXCLUSION_THROTTLE = (2, 600)
    assert [put(desk).status_code for _ in range(3)] == [200, 200, 429]  # la 2.ª: misma marca
    assert dict(migrator.execute(COUNTED).fetchall())[f"aix:usr:{desk.ana.pk}"] == 3


WAITING = "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted"


def test_only_one_exclusion_waits_per_conversation_the_next_is_refused_at_once(
    desk: SimpleNamespace, migrator: DB, monkeypatch: pytest.MonkeyPatch
) -> None:
    w, outcome, answered = in_flight(desk, monkeypatch), list[str](), list[Any]()
    desk.fake.replies = [PLAIN]
    asking = later(lambda: asked(desk, outcome))
    assert w.inside.wait(10)
    waiting = later(lambda: answered.append(put(desk)))
    assert any(migrator.execute(WAITING).fetchone() == (1,) or threading.Event().wait(0.05)
               for _ in range(200))  # fmt: skip
    started, second = time.monotonic(), put(desk)
    assert (second.status_code, second.json()["code"]) == (409, "AI_CALL_IN_PROGRESS")
    assert time.monotonic() - started < 5  # sin esperar ni retener otra conexión
    w.gate.set()
    for thread in (asking, waiting):
        thread.join(20)
    assert outcome == ["OK"] and [a.status_code for a in answered] == [200]


def test_a_call_waits_for_an_exclusion_no_longer_than_its_run_and_the_row_wait_is_bounded(
    desk: SimpleNamespace, migrator: DB, monkeypatch: pytest.MonkeyPatch, settings: Any
) -> None:
    inside, gate = threading.Event(), threading.Event()

    def pause() -> None:
        inside.set()
        gate.wait(10)

    monkeypatch.setattr(attention, "record", ahead(pause, audit_record))
    monkeypatch.setattr(copilot, "CALL_SECONDS", 0)
    monkeypatch.setattr(context, "caps", lambda: (8, 1.5))  # el plazo del run: 1,5 s
    stopping = later(lambda: exclude(desk))
    assert inside.wait(10)
    started = time.monotonic()
    with pytest.raises(copilot.CopilotError) as caught:
        copilot.suggest(ctx(desk.a, desk.ana), desk.convo)
    assert caught.value.code == "AI_DEADLINE" and time.monotonic() - started < 5
    assert desk.fake.calls == [] and migrator.execute(RUNS).fetchall() == [("AI_DEADLINE", 0)]
    gate.set()
    stopping.join(10)


AUDITED = "SELECT count(*) FROM audit_logs WHERE action = 'conversation.ai_excluded'"


def test_the_row_gets_only_what_is_left_of_the_wait_and_a_busy_row_is_a_retryable_409(
    desk: SimpleNamespace, migrator: DB, settings: Any
) -> None:
    """F8-13d: tras esperar 1,5 s a la llamada, la fila espera solo lo que queda de los 2 s
    (nunca dos esperas enteras); si sigue bloqueada, 409 `CONVERSATION_BUSY` y nada cambia. Los
    1,5 s cuentan desde que la petición pide el candado, y cada espera se mide en su sentencia:
    con la máquina cargada, el resto de la petición tarda segundos que no son espera (F1-TEST-01).
    """
    settings.AI_EXCLUSION_WAIT_SECONDS = 2
    held, asked, waits = threading.Event(), threading.Event(), list[tuple[str, float]]()

    def timed(execute: Any, sql: str, params: Any, many: bool, context: Any) -> Any:
        kind = "call" if sql == attention._CALL_EXCLUSIVE else "row" if "FOR NO KEY" in sql else ""
        if kind == "call":
            asked.set()
        began = time.monotonic()
        try:
            return execute(sql, params, many, context)
        finally:
            if kind:
                waits.append((kind, time.monotonic() - began))

    def hold() -> None:
        with attention.ai_call(ctx(desk.a, desk.ana), desk.convo, 5):
            held.set()
            asked.wait(10)  # desde que la petición pide el candado
            threading.Event().wait(1.5)  # `desk` sustituye `time.sleep`

    version = migrator_version(desk)
    with migrator.transaction():
        migrator.execute("SELECT 1 FROM conversations WHERE id = %s FOR UPDATE", [desk.convo])
        holder = later(hold)
        assert held.wait(10)
        started = time.monotonic()
        with connection.execute_wrapper(timed):
            answer = put(desk)
        took = time.monotonic() - started
    holder.join(10)
    assert (answer.status_code, answer.json()["code"]) == (409, "CONVERSATION_BUSY")
    assert [kind for kind, _ in waits] == ["call", "row"]  # una vez cada una, en este orden
    (_, call), (_, row) = waits
    assert 1.4 < took and call + row < 2.8  # una espera de 2 s, no 1,5 + 2
    assert excluded_at(migrator, desk) is None and migrator_version(desk) == version
    assert migrator.execute(AUDITED).fetchone() == (0,)
    assert put(desk).status_code == 200  # reintentar, ya libre


def test_a_stale_version_is_refused_before_waiting_and_takes_no_waiting_slot(
    desk: SimpleNamespace, migrator: DB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F8-13d: con una llamada en curso, una versión vieja es 409 enseguida (sin esperar ni ocupar
    el único hueco de espera)."""
    w, outcome = in_flight(desk, monkeypatch), list[str]()
    desk.fake.replies = [PLAIN]
    asking = later(lambda: asked(desk, outcome))
    assert w.inside.wait(10)
    started = time.monotonic()
    answer = send(signed(desk.ana), "put", f"{BASE}{desk.convo}/ai-exclusion/",
                  {"excluded": True, "version": migrator_version(desk) + 1})  # fmt: skip
    assert (answer.status_code, answer.json()["code"]) == (409, "CONVERSATION_VERSION_CONFLICT")
    assert time.monotonic() - started < 1 and migrator.execute(WAITING).fetchone() == (0,)
    w.gate.set()
    asking.join(20)
    assert outcome == ["OK"] and excluded_at(migrator, desk) is None


def test_a_call_lock_whose_short_transaction_fails_after_taking_it_closes_the_connection(
    desk: SimpleNamespace, migrator: DB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F8-13d: el candado es de sesión; si su transacción corta falla después de tomarlo, la
    conexión se cierra (la base lo suelta) y no queda retenido."""
    real = attention._locked

    def broken(*args: Any) -> None:
        real(*args)
        raise OperationalError("tras tomarlo")

    monkeypatch.setattr(attention, "_locked", broken)
    with pytest.raises(OperationalError), attention.ai_call(ctx(desk.a, desk.ana), desk.convo, 5):
        pytest.fail("no entra")
    assert connection.connection is None
    assert migrator.execute(
        "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory'"
    ).fetchone() == (0,)


def test_every_gateway_call_in_the_apps_passes_its_check() -> None:
    """`admit` es opcional (las pruebas del gateway no lo pasan); nada de `apps` lo omite."""
    found = []
    for path in (pathlib.Path(settings_module.BASE_DIR) / "apps").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "complete" and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "gateway"):  # fmt: skip
                found.append({k.arg for k in node.keywords})
    assert found and all("admit" in keywords for keywords in found)


def test_a_call_that_fails_midway_releases_its_lock(
    desk: SimpleNamespace, migrator: DB, monkeypatch: pytest.MonkeyPatch, settings: Any
) -> None:
    settings.AI_EXCLUSION_WAIT_SECONDS = 1

    def broken(*_: Any) -> Any:
        raise RuntimeError("roto")

    monkeypatch.setattr(gateway, "_reserve", broken)
    with pytest.raises(copilot.CopilotError) as caught:
        copilot.suggest(ctx(desk.a, desk.ana), desk.convo)
    assert caught.value.code == "INTERNAL_ERROR"
    held = "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory'"
    assert migrator.execute(held).fetchone() == (0,)
    started = time.monotonic()
    assert put(desk).status_code == 200 and time.monotonic() - started < 1


def test_a_reservation_waits_for_an_exclusion_in_flight_and_then_sends_nothing(
    desk: SimpleNamespace, migrator: DB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """El comando ya tiene el candado exclusivo y la fila (antes de confirmar): la llamada espera
    su candado antes de reservar, ve la marca confirmada y no reserva ni llama."""
    inside, gate, outcome = threading.Event(), threading.Event(), list[str]()

    def pause() -> None:
        inside.set()
        gate.wait(10)

    monkeypatch.setattr(attention, "record", ahead(pause, audit_record))
    stopping = later(lambda: exclude(desk))
    assert inside.wait(10)
    desk.fake.replies = [PLAIN]

    def ask() -> None:
        try:
            copilot.suggest(ctx(desk.a, desk.ana), desk.convo)
        except copilot.CopilotError as stop:
            outcome.append(stop.code)

    asking = later(ask)
    asking.join(0.5)
    assert asking.is_alive()  # leyó el contexto y espera el candado de la llamada
    gate.set()
    for thread in (stopping, asking):
        thread.join(15)
        assert not thread.is_alive()
    assert outcome == ["AI_EXCLUDED"] and desk.fake.calls == []
    assert migrator.execute(RUNS).fetchall() == [("AI_EXCLUDED", 0)]
    assert migrator.execute("SELECT count(*) FROM ai_budget_reservations").fetchone() == (0,)

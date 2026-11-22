"""F8-08l2 (#1058; ADR-034 §8, ADR-010, ADR-011): la solicitud de atención humana reconoce el texto
de traspaso vigente de la organización de la conversación (leído bajo su RLS), no solo el inicial:
enviarlo no la cierra y, en el copiloto, no cuenta como «una persona ya respondió». Uno anterior,
cambiado después, sí cuenta. Pruebas D4, D8 y D9 de la revisión de #1052. Datos ficticios."""

import pathlib
import tomllib
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest
from django.core.exceptions import ImproperlyConfigured

from apps.ai_agents import copilot, detectors, handoff_text
from apps.conversations import handoff
from tests.test_ai_copilot_context import say
from tests.test_ai_copilot_runtime import PLAIN, desk, run
from tests.test_ai_gateway import world
from tests.test_ai_handoff_reply import MINE, OWNER, STORE
from tests.test_conversations_api import crm, inbox, real_stack, stamp
from tests.test_conversations_human_request import HANDOFF, ask, state, trail
from tests.test_conversations_send_api import post
from tests.test_self_context import signed

__all__ = ["crm", "desk", "inbox", "real_stack", "world"]  # los fixtures, para pytest
pytestmark = pytest.mark.usefixtures("tenant_db")
DB = psycopg.Connection[Any]
NEW = "Gracias por escribirnos. Una persona del equipo seguirá esta conversación contigo."
STALE = "Te atendemos en 5 minutos"  # la base lo aceptó, pero ya no pasa: se propone el inicial
CHANGE = "UPDATE ai_handoff_replies SET text = %s WHERE organization_id = %s"
OPEN = ("AGENT",)


def replied(inbox: SimpleNamespace, migrator: DB, convo: Any, body: str) -> bool:
    """Envía `body` como persona; `True` si cerró la solicitud (y entonces la vuelve a pedir)."""
    sent = post(signed(inbox.ana), convo, body=body)
    assert sent.status_code == 202, sent.content
    if state(inbox.a, convo)[:1] == OPEN:
        return False
    closed = {"cause": "REPLIED", "message_id": sent.json()["id"]}
    assert state(inbox.a, convo) == (None, None, None) and trail(migrator)[-1][3] == closed
    assert ask(signed(inbox.ana), convo).status_code == 200
    return True


def test_d4_the_organization_text_sent_keeps_the_request_and_anything_else_closes_it(
    inbox: SimpleNamespace, migrator: DB
) -> None:
    assert detectors.reply_problems(NEW) == () and detectors.reply_problems(STALE) != ()
    migrator.execute(STORE, [inbox.a, MINE])
    migrator.execute(STORE, [inbox.b, NEW])  # el de B no es el de A (RLS de la conversación)
    convo = inbox.convs["nobody"]
    ask(signed(inbox.ana), convo)
    assert not replied(inbox, migrator, convo, "  " + MINE.replace(" ", "\n ", 1) + "\n")
    # el inicial, uno más corto o uno que lo contiene (no se busca dentro) y el de B: atienden
    for other in (OWNER, MINE[:-1], f"{MINE} ¿Algo más?", f"Hola. {MINE}", NEW):
        assert replied(inbox, migrator, convo, other), other


def test_d8_a_text_changed_afterwards_is_a_real_reply_and_a_stale_one_gives_way_to_the_initial(
    inbox: SimpleNamespace, migrator: DB
) -> None:
    migrator.execute(STORE, [inbox.a, MINE])
    convo = inbox.convs["nobody"]
    ask(signed(inbox.ana), convo)
    assert not replied(inbox, migrator, convo, MINE)
    migrator.execute(CHANGE, [NEW, inbox.a])
    assert replied(inbox, migrator, convo, MINE)  # el anterior, enviado tras el cambio, sí
    assert not replied(inbox, migrator, convo, NEW)
    migrator.execute(CHANGE, [STALE, inbox.a])  # el copiloto propone el inicial: ese no atiende
    assert not replied(inbox, migrator, convo, OWNER)
    assert replied(inbox, migrator, convo, STALE)


def test_d9_a_late_hook_records_after_only_the_handoff_text_and_never_after_a_real_reply(
    desk: SimpleNamespace, migrator: DB, monkeypatch: pytest.MonkeyPatch
) -> None:
    migrator.execute(STORE, [desk.a, MINE])
    prepare, ana = copilot._prepare, desk.ana.pk
    person = {"direction": "OUTBOUND", "sender_type": "USER", "sender_user_id": ana,
              "delivery_status": "SENT"}  # fmt: skip
    note = {"direction": "INTERNAL", "sender_type": "USER", "sender_user_id": ana,
            "message_type": "NOTE"}  # fmt: skip
    system = {"direction": "OUTBOUND", "sender_type": "SYSTEM", "delivery_status": "SENT"}

    def meanwhile(*sent: tuple[str, dict[str, Any]], change: str | None = None) -> Any:
        """Lo que pasa en la conversación mientras el modelo trabaja; luego, el hook."""

        def step(*args: Any) -> Any:
            found = prepare(*args)
            for body, over in sent:
                say(desk, body, **over)
            if change:
                migrator.execute(CHANGE, [change, desk.a])
            return found

        say(desk, HANDOFF)
        monkeypatch.setattr(copilot, "_prepare", step)
        assert run(desk, PLAIN).handoff_reason == "ASKED_FOR_PERSON"
        return state(desk.a, desk.convo)[0]

    assert meanwhile((f" {MINE} ", person), ("Ya te atiendo", person)) is None  # una real
    assert meanwhile((MINE, person), (MINE, person)) == "ASKED_FOR_PERSON"  # solo el traspaso
    assert len(trail(migrator)) == 1
    stamp(desk.a, desk.convo, human_requested_at=None, human_request_reason=None,
          human_requested_by_user_id=None)  # fmt: skip
    assert meanwhile(("Nota", note), ("Confirmación", system)) == "ASKED_FOR_PERSON"  # #1052
    stamp(desk.a, desk.convo, human_requested_at=None, human_request_reason=None,
          human_requested_by_user_id=None)  # fmt: skip
    assert meanwhile((MINE, person), change=NEW) is None  # D8: cambiado antes del hook, cuenta


def test_only_ai_agents_serves_the_text_and_only_the_request_reads_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = tomllib.loads((pathlib.Path(__file__).parents[1] / "pyproject.toml").read_text())
    contracts = config["tool"]["importlinter"]["contracts"]
    [guard] = [c for c in contracts if c.get("protected_modules") == [handoff.__name__]]
    allowed = ["apps.conversations.attention", "apps.ai_agents.apps"]
    assert (guard["type"], guard["allowed_importers"]) == ("protected", allowed)
    assert handoff._SOURCE == [handoff_text.in_scope]
    handoff.serve(handoff_text.in_scope)  # `ready` otra vez: lo mismo
    with pytest.raises(ImproperlyConfigured):
        handoff.serve(lambda ctx: OWNER)  # nadie más lo reemplaza
    assert handoff._SOURCE == [handoff_text.in_scope]
    monkeypatch.setattr(handoff, "_SOURCE", [])
    with pytest.raises(ImproperlyConfigured):  # sin quien lo sirva, nunca otro texto
        handoff.text(None)  # type: ignore[arg-type]

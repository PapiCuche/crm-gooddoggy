"""F8-05b7: turnos con tools y los bloques del propio modelo por el gateway, contra la API falsa
(`tests.ai_sim`, sin red). Un turno inválido se rechaza antes del scope, la clave, la reserva y la
llamada, solo con su código; la reserva acota los bloques de razonamiento reenviados y las
imágenes; el costo cobrado sigue siendo el real; nada de la conversación sale a logs ni errores."""

import logging
from dataclasses import dataclass, replace
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from django.conf import settings
from django.test import override_settings

from apps.ai_gateway import gateway, keys, prices, turns
from apps.ai_gateway.gateway import GatewayError
from apps.ai_gateway.llm import (
    Image,
    LLMRequest,
    Outcome,
    RedactedThinking,
    Text,
    Thinking,
    Tool,
    ToolCall,
    ToolResult,
    Turn,
    Usage,
)  # fmt: skip
from tests.test_ai_gateway import PARAMS, SCHEMA, budget, ledger, world
from tests.test_memberships import ctx

pytestmark = pytest.mark.usefixtures("tenant_db")
__all__ = ["world"]  # el fixture, para pytest
SEEN, SIG, RED = "pensamiento-que-no-sale", "firma-que-no-sale-0001", "redactado-que-no-sale"
INPUT, RESULT, PNG = "entrada-que-no-sale", "resultado-que-no-sale", "iVBORw0KGgoAAAANSUhEUg=="
TOOLS = (Tool("search_product", "Busca un producto", SCHEMA),)
LIMITS, MAX = settings.AI_TURN_LIMITS, PARAMS["max_tokens"]
CALLED = (ToolCall("toolu_1", "search_product", {"q": INPUT}),
          ToolCall("toolu_2", "search_product", {"q": "b"}))  # fmt: skip
ANSWERS = (ToolResult("toolu_1", (Text(RESULT), Image("image/png", PNG))),
           ToolResult("toolu_2", (), True), Text("¿y ahora?"))  # fmt: skip
LOOP = (Turn("user", "hola"),
        Turn("assistant", (Thinking(SEEN, SIG), RedactedThinking(RED), Text("busco"), *CALLED)),
        Turn("user", ANSWERS))  # fmt: skip
SECRETS = (SEEN, SIG, RED, INPUT, RESULT, PNG, "toolu_1", "search_product")
CONTEXT = {"request_id", "correlation_id", "organization_id", "user_id", "actor_type", "level",
           "logger", "timestamp"}  # fmt: skip
REPLY = [{"type": "thinking", "thinking": "", "signature": SIG}, {"type": "redacted_thinking",
         "data": RED}, {"type": "text", "text": "busco"}, {"type": "tool_use", "id": "toolu_7",
         "name": "search_product", "input": {"q": INPUT}}]  # fmt: skip


def ask(*conversation: Turn, tools: tuple[Tool, ...] = TOOLS) -> LLMRequest:
    return LLMRequest("SUGGEST", "s", conversation or LOOP, tools)


def at(index: int, *content: Any) -> tuple[Turn, ...]:
    """`LOOP` con el contenido del turno `index` cambiado."""
    return tuple(replace(t, content=content) if i == index else t for i, t in enumerate(LOOP))


@dataclass(frozen=True)
class Audio:  # un tipo de bloque que el gateway no conoce
    data: str


class Shout(Text):  # una subclase no pasa por su base
    pass


class Pair(tuple[Any, ...]):  # una subclase de tupla no es el contenido de un turno
    pass


DEEP: list[Any] = []
for _ in range(70):
    DEEP = [DEEP]
BAD_INPUTS: dict[str, Any] = {  # entradas de tool que no son JSON válido o no se codifican
    "decimal": {"q": Decimal("1.5")}, "set": {"q": {1, 2}}, "bytes": {"q": b"x"},
    "tuple_key": {("a",): 1}, "int_key": {1: "x"}, "tuple": {"q": (1, 2)},
    "nan": {"q": float("nan")}, "inf": {"q": float("inf")}, "minus_inf": {"q": float("-inf")},
    "surrogate": {"q": "\ud800"}, "surrogate_key": {"\ud800": 1}, "nested": {"q": [{"r": b"x"}]},
    "deep": {"q": DEEP},  # 70 listas anidadas: más de 64 niveles
}  # fmt: skip


CASES: dict[str, tuple[str, tuple[Turn, ...]]] = {  # caso -> (regla, conversación)
    "result_without_call": ("pairing", (Turn("user", (ToolResult("toolu_1"),)),)),
    "missing_result": ("pairing", at(2, ANSWERS[0])),
    "extra_result": ("pairing", at(2, *ANSWERS[:2], ToolResult("toolu_9"))),
    "repeated_result": ("pairing", at(2, ANSWERS[0], ANSWERS[0], ANSWERS[1])),
    "result_turn_later": (
        "pairing",
        (*LOOP[:2], Turn("user", "x"), Turn("assistant", "y"), Turn("user", ANSWERS)),
    ),  # fmt: skip
    "repeated_call_in_turn": ("duplicate_id", at(1, CALLED[0], CALLED[0])),
    "repeated_call_later": (
        "duplicate_id",
        (*LOOP, Turn("assistant", (CALLED[0],)), Turn("user", (ToolResult("toolu_1"),))),
    ),  # fmt: skip
    "text_before_result": ("order", at(2, Text("x"), *ANSWERS[:2])),
    "unknown_block": ("block_type", at(0, Audio("x"))),
    "subclass": ("block_type", at(0, Shout("x"))),
    "thinking_from_user": ("block_type", at(0, Thinking(SEEN, SIG))),
    "result_from_assistant": (
        "block_type",
        (Turn("user", "x"), Turn("assistant", ANSWERS[:1]), Turn("user", "y")),
    ),  # fmt: skip
    "nested_thinking": (
        "block_type",
        at(2, ToolResult("toolu_1", (Thinking(SEEN, SIG),)), ANSWERS[1]),  # type: ignore[arg-type]
    ),  # fmt: skip
    "input_not_object": (
        "field",
        at(
            1,
            ToolCall("toolu_1", "search_product", [INPUT]),  # type: ignore[arg-type]
            CALLED[1],
        ),
    ),  # fmt: skip
    "is_error_not_bool": ("field", at(2, ANSWERS[0], ToolResult("toolu_2", (), 1))),  # type: ignore[arg-type]
    "text_not_str": ("field", at(0, Text(7))),  # type: ignore[arg-type]
    "empty_text": ("field", at(0, Text(""))),
    "empty_signature": ("field", at(1, Thinking(SEEN, ""), *CALLED)),
    "empty_redacted": ("field", at(1, RedactedThinking(""), *CALLED)),
    "bad_id": (
        "field",
        (
            Turn("user", "x"),
            Turn("assistant", (ToolCall("toolu 1", "search_product", {}),)),
            Turn("user", (ToolResult("toolu 1"),)),
        ),
    ),  # fmt: skip
    "content_list": ("field", (Turn("user", ["x"]),)),  # type: ignore[arg-type]
    "undeclared_tool": (
        "unknown_tool",
        (
            *LOOP[:1],
            Turn("assistant", (ToolCall("toolu_1", "delete_all", {}),)),
            Turn("user", (ToolResult("toolu_1"),)),
        ),
    ),  # fmt: skip
    "svg": ("image", at(0, Image("image/svg+xml", PNG))),
    "not_base64": ("image", at(0, Image("image/png", "no es base64!"))),
    "assistant_last": ("last_turn", LOOP[:2]),
    "two_users": ("role", (Turn("user", "x"), Turn("user", "y"), Turn("assistant", "z"))),
    "system_role": ("role", (Turn("system", "x"),)),
    "assistant_first": ("role", (Turn("assistant", "x"), Turn("user", "y"))),
    "nothing": ("limit", ()),
    "empty_user": ("field", (Turn("user", ()),)),
    "empty_assistant": ("field", (Turn("user", "x"), Turn("assistant", ()), Turn("user", "y"))),
    "blank_text": ("field", at(0, Text(" \n\t"))),
    "blank_plain": ("field", (Turn("user", "  "),)),
    "tuple_subclass": ("field", (Turn("user", Pair((Text("x"),))),)),
    "nested_svg": (
        "image",
        at(2, ToolResult("toolu_1", (Image("image/svg+xml", PNG),)), ANSWERS[1]),
    ),  # fmt: skip
    "nested_not_base64": (
        "image",
        at(2, ToolResult("toolu_1", (Image("image/png", "@@@@"),)), ANSWERS[1]),
    ),  # fmt: skip
    "base64_length": ("image", at(0, Image("image/png", PNG[:-1]))),
    "base64_padding": ("image", at(0, Image("image/png", "iVBO=AAA"))),
    "empty_image": ("image", at(0, Image("image/png", ""))),
    "surrogate_text": ("field", at(0, Text("a\ud800"))),
    "surrogate_plain": ("field", (Turn("user", "a\ud800"),)),
    "surrogate_thinking": ("field", at(1, Thinking("\udfff", SIG), *CALLED)),
    **{
        f"input_{name}": (
            "field",
            (
                Turn("user", "x"),
                Turn("assistant", (ToolCall("toolu_1", "search_product", value),)),
                Turn("user", (ToolResult("toolu_1"),)),
            ),
        )
        for name, value in BAD_INPUTS.items()
    },  # fmt: skip
}


@pytest.fixture
def spies(world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Lo que el gateway llegó a hacer: abrir el scope, leer el modelo, la clave o reservar."""
    ran: list[str] = []

    def spy(module: Any, name: str) -> None:
        real = getattr(module, name)

        def wrapper(*args: Any) -> Any:
            ran.append(name)
            return real(*args)

        monkeypatch.setattr(module, name, wrapper)

    for name in ("tenant_scope", "_model", "_reserve"):
        spy(gateway, name)
    spy(keys, "open_key")
    return ran


def rejected(w: SimpleNamespace, request: LLMRequest, caplog: pytest.LogCaptureFixture) -> str:
    """El código del rechazo; nada del turno en el error ni en los logs. Devuelve la regla."""
    with caplog.at_level(logging.DEBUG), pytest.raises(GatewayError) as caught:
        gateway.complete(ctx(w.a, w.ana), request)
    assert caught.value.outcome == Outcome.AI_TURN_INVALID
    assert str(caught.value) == "AI_TURN_INVALID" and caught.value.__suppress_context__
    logged = caplog.text + str([r.__dict__ for r in caplog.records])
    assert not any(s in logged for s in SECRETS)
    [record] = [r for r in caplog.records if "ai.turn_rejected" in r.getMessage()]
    fields: dict[str, Any] = record.msg  # type: ignore[assignment]
    assert set(fields) - CONTEXT == {"event", "rule"}  # solo el código de la regla
    return str(fields["rule"])


@pytest.mark.parametrize("case", CASES)
def test_an_invalid_turn_is_refused_before_scope_key_reserve_and_call(
    world: SimpleNamespace, migrator: Any, spies: list[str], case: str,
    caplog: pytest.LogCaptureFixture,
) -> None:  # fmt: skip
    budget(migrator, world.a, "1")
    rule, conversation = CASES[case]
    assert rejected(world, ask(*conversation) if conversation else LLMRequest(
        "SUGGEST", "s", (), TOOLS), caplog) == rule  # fmt: skip
    assert spies == [] and world.fake.calls == [] and ledger(migrator) == ([], [])


def many(kind: str, n: int) -> tuple[Turn, ...]:
    """Una conversación válida con `n` de `kind` (turnos, bloques, razonamiento o imágenes)."""
    if kind == "turns":
        return tuple(Turn("user" if i % 2 == 0 else "assistant", "x") for i in range(n))
    if kind in ("thinking", "redacted"):  # los dos cuentan en el mismo límite
        own = Thinking("", SIG) if kind == "thinking" else RedactedThinking(RED)
        return (Turn("user", "x"), Turn("assistant", (own,) * n), Turn("user", "y"))
    block = Image("image/png", PNG) if kind == "images" else Text("x")
    return (Turn("user", (block,) * n),)


@pytest.mark.parametrize("kind", ["turns", "blocks", "thinking", "redacted", "images"])
def test_each_count_limit_holds_at_the_limit_and_refuses_one_more(
    world: SimpleNamespace, migrator: Any, spies: list[str], kind: str,
    caplog: pytest.LogCaptureFixture,
) -> None:  # fmt: skip
    budget(migrator, world.a, "100")
    assert LIMITS == {"turns": 100, "blocks": 400, "thinking": 100, "images": 20,
                      "image_chars": 2_000_000, "body_bytes": 8_000_000}  # fmt: skip
    limit = 5 if kind == "turns" else 4  # los turnos, impares: el último es de quien pide
    with override_settings(AI_TURN_LIMITS={**LIMITS, kind.replace("redacted", "thinking"): limit}):
        gateway.complete(ctx(world.a, world.ana), ask(*many(kind, limit)))
        assert len(world.fake.calls) == 1
        world.fake.calls.clear()
        spies.clear()
        over = many(kind, limit + 1)
        assert rejected(world, ask(*over), caplog) == "limit"
    assert spies == [] and world.fake.calls == [] and len(ledger(migrator)[1]) == 1


def test_image_size_and_body_size_hold_at_the_limit_and_refuse_one_more(
    world: SimpleNamespace, migrator: Any, spies: list[str], caplog: pytest.LogCaptureFixture
) -> None:
    budget(migrator, world.a, "100")
    picture = (Turn("user", (Image("image/png", PNG),)),)
    with override_settings(AI_TURN_LIMITS={**LIMITS, "image_chars": len(PNG)}):
        gateway.complete(ctx(world.a, world.ana), ask(*picture))
    with override_settings(AI_TURN_LIMITS={**LIMITS, "image_chars": len(PNG) - 1}):
        assert rejected(world, ask(*picture), caplog) == "image"
    size = len(gateway.ADAPTERS["ANTHROPIC"].encode(ask(), PARAMS))
    with override_settings(AI_TURN_LIMITS={**LIMITS, "body_bytes": size}):
        gateway.complete(ctx(world.a, world.ana), ask())
    spies.clear()
    caplog.clear()
    with override_settings(AI_TURN_LIMITS={**LIMITS, "body_bytes": size - 1}):
        assert rejected(world, ask(), caplog) == "limit"
    # el cuerpo se mide ya codificado: se leyó el modelo, pero no la clave, la reserva o la llamada
    assert "open_key" not in spies and "_reserve" not in spies
    assert len(world.fake.calls) == 2 and len(ledger(migrator)[1]) == 2


def bound(request: LLMRequest, max_tokens: int = MAX) -> Decimal:
    """Lo que debe reservarse: bytes del cuerpo, el margen de tools, `max(max_tokens, bytes)` por
    bloque de razonamiento y la cota de cada imagen."""
    body = gateway.ADAPTERS["ANTHROPIC"].encode(request, {**PARAMS, "max_tokens": max_tokens})
    blocks = [b for t in request.turns if not isinstance(t.content, str) for b in t.content]
    blocks += [c for b in blocks if isinstance(b, ToolResult) for c in b.content]
    extra = sum(max(max_tokens, len((b.data if isinstance(b, RedactedThinking) else b.thinking)
                                    .encode())) for b in blocks
                if isinstance(b, Thinking | RedactedThinking))  # fmt: skip
    extra += 4784 * sum(isinstance(b, Image) for b in blocks)
    return prices.ceiling("ANTHROPIC", PARAMS["model"], len(body) + 1000 + extra, max_tokens)


def test_the_reservation_bounds_resent_thinking_and_images_and_charges_the_real_cost(
    world: SimpleNamespace, migrator: Any, caplog: pytest.LogCaptureFixture
) -> None:
    budget(migrator, world.a, "1")
    request = ask()
    tokens = (bound(request) * 1_000_000 - MAX * 10) / 4  # entrada al precio más alto (1 h)
    world.fake.usage = {"input_tokens": 0, "output_tokens": MAX,
                        "cache_creation": {"ephemeral_1h_input_tokens": int(tokens)},
                        "cache_creation_input_tokens": int(tokens)}  # fmt: skip
    with caplog.at_level(logging.DEBUG):
        reply = gateway.complete(ctx(world.a, world.ana), request)
    [(spent, reserved)], [row] = ledger(migrator)
    assert row[1] == bound(request) == prices.cost("ANTHROPIC", PARAMS["model"], reply.usage)
    assert (row[0], row[2], spent, reserved) == ("OK", row[1], row[1], 0)  # justo la cota
    assert "ai.reservation_exceeded" not in caplog.text
    assert not any(s in caplog.text + str([r.__dict__ for r in caplog.records]) for s in SECRETS)
    world.fake.usage = {"input_tokens": 100, "output_tokens": 10}  # lo real, muy por debajo
    gateway.complete(ctx(world.a, world.ana), request)
    [(spent2, _)], rows = ledger(migrator)
    cost = prices.cost("ANTHROPIC", PARAMS["model"], Usage(100, 10))
    assert rows[1][2] == cost < rows[1][1] and spent2 == spent + cost


def test_a_cap_without_the_thinking_and_image_terms_refuses(
    world: SimpleNamespace, migrator: Any
) -> None:
    body = gateway.ADAPTERS["ANTHROPIC"].encode(ask(), PARAMS)
    plain = prices.ceiling("ANTHROPIC", PARAMS["model"], len(body) + 1000, MAX)
    budget(migrator, world.a, str(plain))
    with pytest.raises(GatewayError) as caught:
        gateway.complete(ctx(world.a, world.ana), ask())
    assert caught.value.outcome == Outcome.AI_BUDGET_EXHAUSTED and world.fake.calls == []


LONG = "é" * 1500  # 3000 bytes, 1500 caracteres: cuenta en bytes


@pytest.mark.parametrize("max_tokens", [MAX, 100])
def test_a_thinking_block_bound_is_the_max_of_max_tokens_and_its_bytes(max_tokens: int) -> None:
    long, short = Thinking(LONG, SIG), Thinking("", SIG)
    assert turns.surplus((long,), max_tokens) == 3000  # lo visible pasa de max_tokens
    assert turns.surplus((RedactedThinking("x" * 2500),), max_tokens) == max(2500, max_tokens)
    assert turns.surplus((short,), max_tokens) == max_tokens  # max_tokens es el piso
    assert turns.surplus((long, short, Image("image/png", PNG), Text("x")), 4000) == 4000 * 2 + 4784
    with pytest.raises(turns.Rejected):  # un tipo sin cota no vale 0: falla cerrado
        turns.surplus((Audio("x"),), max_tokens)  # type: ignore[arg-type]


def test_lowering_max_tokens_does_not_lower_a_resent_block_bound(
    world: SimpleNamespace, migrator: Any
) -> None:
    budget(migrator, world.a, "1")
    request = ask(Turn("user", "x"), Turn("assistant", (Thinking(LONG, SIG), Text("y"))),
                  Turn("user", "z"))  # fmt: skip
    with override_settings(AI_PROVIDER_PARAMS={"ANTHROPIC": {**PARAMS, "max_tokens": 100}}):
        gateway.complete(ctx(world.a, world.ana), request)
    body = gateway.ADAPTERS["ANTHROPIC"].encode(request, {**PARAMS, "max_tokens": 100})
    held = prices.ceiling("ANTHROPIC", PARAMS["model"], len(body) + 1000 + 3000, 100)
    assert ledger(migrator)[1][0][1] == held == bound(request, 100)  # 3000 bytes, no 100


def test_the_model_blocks_go_back_unchanged(world: SimpleNamespace, migrator: Any) -> None:
    budget(migrator, world.a, "1")
    world.fake.content = REPLY
    reply = gateway.complete(ctx(world.a, world.ana), ask(Turn("user", "hola")))
    assert reply.blocks == (Thinking("", SIG), RedactedThinking(RED), Text("busco"),
                            ToolCall("toolu_7", "search_product", {"q": INPUT}))  # fmt: skip
    assert (reply.text, reply.tool_calls) == ("busco", reply.blocks[3:])
    answer = ToolResult("toolu_7", (Text(RESULT), Image("image/webp", PNG)), True)
    following = ask(Turn("user", "hola"), Turn("assistant", reply.blocks), Turn("user", (answer,)))
    gateway.complete(ctx(world.a, world.ana), following)
    sent = world.fake.calls[-1]["body"]["messages"]
    assert sent[1] == {"role": "assistant", "content": REPLY}  # byte a byte, en su orden
    picture = {"type": "base64", "media_type": "image/webp", "data": PNG}
    parts = [{"type": "text", "text": RESULT}, {"type": "image", "source": picture}]
    assert sent[2]["content"] == [{"type": "tool_result", "tool_use_id": "toolu_7",
                                   "is_error": True, "content": parts}]  # fmt: skip
    assert sent[0] == {"role": "user", "content": "hola"}  # el texto simple sigue igual


BROKEN = {  # respuestas que no se pueden reenviar intactas
    "unknown": [{"type": "server_tool_use", "id": "s", "name": "x", "input": {}}],
    "signature": [{"type": "thinking", "thinking": "", "signature": 7}],
    "redacted": [{"type": "redacted_thinking"}],
    "input": [{"type": "tool_use", "id": "toolu_1", "name": "x", "input": [INPUT]}],
}


@pytest.mark.parametrize("name", BROKEN)
def test_an_unreadable_reply_block_counts_the_reservation_whole(
    world: SimpleNamespace, migrator: Any, name: str, caplog: pytest.LogCaptureFixture,
    cases: dict[str, Any] = BROKEN,
) -> None:  # fmt: skip
    budget(migrator, world.a, "1")
    world.fake.content = cases[name]
    with caplog.at_level(logging.DEBUG), pytest.raises(GatewayError) as caught:
        gateway.complete(ctx(world.a, world.ana), ask(Turn("user", "hola")))
    assert caught.value.outcome == Outcome.PROVIDER_UNAVAILABLE and INPUT not in caplog.text
    [(spent, reserved)], [row] = ledger(migrator)
    assert (row[0], spent, reserved) == ("PROVIDER_UNCERTAIN", row[1], 0)


def test_an_unencodable_system_prompt_is_refused_before_key_and_reserve(
    world: SimpleNamespace, migrator: Any, spies: list[str], caplog: pytest.LogCaptureFixture
) -> None:
    budget(migrator, world.a, "1")
    request = LLMRequest("SUGGEST", "s\ud800", (Turn("user", "x"),), TOOLS)
    assert rejected(world, request, caplog) == "field"  # 400 antes: un 500 dentro del scope
    assert "open_key" not in spies and "_reserve" not in spies
    assert world.fake.calls == [] and ledger(migrator) == ([], [])


def test_a_conversation_goes_on_after_a_tool_round(world: SimpleNamespace, migrator: Any) -> None:
    budget(migrator, world.a, "1")
    later = (*LOOP, Turn("assistant", "listo"), Turn("user", "gracias"))
    gateway.complete(ctx(world.a, world.ana), ask(*later))
    sent = world.fake.calls[-1]["body"]["messages"]
    assert [m["role"] for m in sent] == ["user", "assistant", "user", "assistant", "user"]
    assert [b["type"] for b in sent[2]["content"]] == ["tool_result", "tool_result", "text"]


def test_an_empty_reply_is_charged_and_returns_no_blocks(
    world: SimpleNamespace, migrator: Any
) -> None:
    budget(migrator, world.a, "1")
    world.fake.content = []
    reply = gateway.complete(ctx(world.a, world.ana), ask(Turn("user", "hola")))
    assert (reply.blocks, reply.text, reply.tool_calls) == ((), "", ())
    [(spent, reserved)], [row] = ledger(migrator)
    assert (row[0], spent, reserved) == ("OK", row[2], 0) and 0 < row[2] < row[1]


ANSWERED = ToolResult("toolu_1")


def levels(n: int, kind: type) -> dict[str, Any]:
    """Una entrada de tool con `n` contenedores anidados (el primero, el objeto de la entrada)."""
    value: Any = 1.5  # un número finito es JSON
    for _ in range(n - 1):
        value = {"k": value} if kind is dict else [value]
    return {"q": value}


USE = {"type": "tool_use", "id": "toolu_7", "name": "search_product", "input": {"q": INPUT}}
UNSENDABLE = {  # F8-07d: se leen, pero con una llamada a una tool no se podrían reenviar
    "blank_text": [{"type": "text", "text": " \n"}, USE],
    "empty_text": [USE, {"type": "text", "text": ""}],
    "blank_signature": [{"type": "thinking", "thinking": "x", "signature": " "}, USE],
    "blank_redacted": [{"type": "redacted_thinking", "data": ""}, USE],
    "deep_input": [{**USE, "input": levels(65, dict)}],
    "deep_list": [{**USE, "input": levels(65, list)}],
    "bad_id": [{**USE, "id": "toolu 7"}],
    "repeated_id": [USE, {**USE, "input": {"q": "b"}}],
    "unknown_tool": [{**USE, "name": "delete_everything"}],
}


@pytest.mark.parametrize("name", UNSENDABLE)
def test_a_tool_reply_that_could_not_be_resent_counts_the_reservation_whole(
    world: SimpleNamespace, migrator: Any, name: str, caplog: pytest.LogCaptureFixture
) -> None:
    test_an_unreadable_reply_block_counts_the_reservation_whole(
        world, migrator, name, caplog, UNSENDABLE
    )


@pytest.mark.parametrize("deep", [64, 1])
def test_a_resendable_tool_reply_is_kept_whole_and_goes_back_unchanged(
    world: SimpleNamespace, migrator: Any, deep: int
) -> None:
    budget(migrator, world.a, "1")
    world.fake.content = [{"type": "thinking", "thinking": "", "signature": SIG},
                          {**USE, "input": levels(deep, dict)}]  # fmt: skip
    reply = gateway.complete(ctx(world.a, world.ana), ask(Turn("user", "hola")))
    assert [type(b) for b in reply.blocks] == [Thinking, ToolCall] and reply.tool_calls
    answer = Turn("user", (ToolResult("toolu_7", (Text("r"),)),))
    again = ask(Turn("user", "hola"), Turn("assistant", reply.blocks), answer)
    gateway.complete(ctx(world.a, world.ana), again)
    assert [row[0] for row in ledger(migrator)[1]] == ["OK", "OK"]


def test_a_final_reply_without_tool_calls_is_not_checked_as_resendable(
    world: SimpleNamespace, migrator: Any
) -> None:
    budget(migrator, world.a, "1")
    world.fake.content = [{"type": "text", "text": " "}, {"type": "text", "text": "hola"}]
    reply = gateway.complete(ctx(world.a, world.ana), ask(Turn("user", "hola")))
    assert reply.blocks == (Text(" "), Text("hola")) and reply.text == " hola"  # ninguno se quita


@pytest.mark.parametrize("kind", [dict, list])
def test_a_tool_input_passes_at_64_levels_and_is_refused_at_65(kind: type) -> None:
    def deep(n: int) -> LLMRequest:
        call = ToolCall("toolu_1", "search_product", levels(n, kind))
        return ask(Turn("user", "x"), Turn("assistant", (call,)), Turn("user", (ANSWERED,)))

    assert len(turns.check(deep(64))) == 3
    with pytest.raises(turns.Rejected, match="^field$"):
        turns.check(deep(65))


BAD_TOOLS = {"list_name": Tool(["x"], "d", SCHEMA), "spaced_name": Tool("a b", "d", SCHEMA),  # type: ignore[arg-type]
             "int_description": Tool("t", 7, SCHEMA), "list_schema": Tool("t", "d", [SCHEMA]),  # type: ignore[arg-type]
             "surrogate_description": Tool("t", "\ud800", SCHEMA)}  # fmt: skip


@pytest.mark.parametrize("name", BAD_TOOLS)
def test_a_malformed_tool_definition_is_refused_before_scope_and_reserve(
    world: SimpleNamespace, migrator: Any, spies: list[str], name: str,
    caplog: pytest.LogCaptureFixture,
) -> None:  # fmt: skip
    budget(migrator, world.a, "1")
    assert rejected(world, ask(Turn("user", "x"), tools=(BAD_TOOLS[name],)), caplog) == "field"
    assert spies == [] and world.fake.calls == [] and ledger(migrator) == ([], [])


def test_a_tool_schema_that_is_not_json_is_refused_before_key_and_reserve(
    world: SimpleNamespace, migrator: Any, spies: list[str], caplog: pytest.LogCaptureFixture
) -> None:
    budget(migrator, world.a, "1")
    odd = (Tool("search_product", "d", {"type": "object", "maximum": float("nan")}),)
    assert rejected(world, ask(Turn("user", "x"), tools=odd), caplog) == "field"  # sin NaN en JSON
    assert "open_key" not in spies and "_reserve" not in spies
    assert world.fake.calls == [] and ledger(migrator) == ([], [])

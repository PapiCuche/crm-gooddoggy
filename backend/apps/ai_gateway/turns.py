"""Los turnos con tools y los bloques del propio modelo (F8-05b7; ADR-034 §5): se validan antes
de abrir el scope, la clave, la reserva o la llamada, y dan la parte de la cota que sus bytes no
acotan. `Rejected` lleva solo el código de la regla: nunca un texto, un id, un nombre ni un tamaño
del turno.

Las reglas son las de la Messages API: a cada `tool_use` de un turno del asistente le contesta un
`tool_result` en el turno siguiente de quien pide, y solo ahí, con los resultados primero; un
`thinking` o `redacted_thinking` vuelve sin cambios; el último turno es de quien pide."""

import re
from collections.abc import Callable
from typing import Any

from django.conf import settings

from apps.ai_gateway.llm import (
    Block,
    Image,
    LLMRequest,
    RedactedThinking,
    Text,
    Thinking,
    ToolCall,
    ToolResult,
)  # fmt: skip

ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
BASE64 = re.compile(r"[A-Za-z0-9+/]+={0,2}")
MEDIA = frozenset({"image/jpeg", "image/png", "image/gif", "image/webp"})
ALLOWED: dict[str, tuple[type, ...]] = {
    "user": (Text, Image, ToolResult),
    "assistant": (Text, Thinking, RedactedThinking, ToolCall),
}
FIELDS: dict[type, tuple[tuple[str, type], ...]] = {  # el tipo exacto de cada campo
    Text: (("text", str),),
    Image: (("media_type", str), ("data", str)),
    Thinking: (("thinking", str), ("signature", str)),
    RedactedThinking: (("data", str),),
    ToolCall: (("id", str), ("name", str), ("input", dict)),
    ToolResult: (("tool_use_id", str), ("content", tuple), ("is_error", bool)),
}
# Lo que un bloque añade a sus bytes en la cota (un token nunca es menos de un byte). Un bloque de
# razonamiento se cobra como entrada con el razonamiento entero, cifrado en la firma: como mucho el
# `max_tokens` de la respuesta que lo trajo, y nunca menos que los bytes de su texto. Una imagen,
# hasta `AI_IMAGE_TOKENS` tokens visuales, sean cuantos sean sus bytes. Un tipo sin entrada no
# vale 0: falla cerrado.
BOUND: dict[type, Callable[[Any, int], int]] = {
    Text: lambda block, top: 0,
    ToolCall: lambda block, top: 0,
    ToolResult: lambda block, top: 0,
    Image: lambda block, top: int(settings.AI_IMAGE_TOKENS),
    Thinking: lambda block, top: max(top, len(block.thinking.encode())),
    RedactedThinking: lambda block, top: max(top, len(block.data.encode())),
}


class Rejected(Exception):  # noqa: N818 — solo el código de la regla
    def __init__(self, rule: str) -> None:
        super().__init__(rule)
        self.rule = rule


def _fields(block: Any) -> None:
    if any(type(getattr(block, name)) is not kind for name, kind in FIELDS[type(block)]):
        raise Rejected("field")
    empty = {Text: "text", Thinking: "signature", RedactedThinking: "data"}.get(type(block))
    ids = [block.id] if type(block) is ToolCall else []
    ids += [block.tool_use_id] if type(block) is ToolResult else []
    if (empty and not getattr(block, empty)) or not all(ID.fullmatch(i) for i in ids):
        raise Rejected("field")
    if type(block) is Image:
        size = len(block.data) <= settings.AI_TURN_LIMITS["image_chars"]
        if block.media_type not in MEDIA or not BASE64.fullmatch(block.data) or not size:
            raise Rejected("image")


def _blocks(role: str, content: object) -> tuple[Block, ...]:
    """Los bloques de un turno, con sus campos comprobados; el texto simple es un bloque."""
    blocks = (Text(content),) if type(content) is str else content
    if type(blocks) is not tuple:
        raise Rejected("field")
    for block in blocks:
        if type(block) not in ALLOWED[role]:
            raise Rejected("block_type")
        _fields(block)
        for inner in block.content if type(block) is ToolResult else ():
            if type(inner) not in (Text, Image):
                raise Rejected("block_type")
            _fields(inner)
    return blocks


def check(request: LLMRequest) -> tuple[Block, ...]:
    """Todos los bloques de `request` (también los de cada resultado), validados. `Rejected`."""
    limits, conversation = settings.AI_TURN_LIMITS, request.turns
    if not 0 < len(conversation) <= limits["turns"]:
        raise Rejected("limit")
    tools = {tool.name for tool in request.tools}
    flat: list[Block] = []
    seen: set[str] = set()
    pending: set[str] = set()  # los `tool_use` del turno anterior, que este tiene que contestar
    for index, turn in enumerate(conversation):
        if turn.role != ("user", "assistant")[index % 2]:
            raise Rejected("role")
        blocks = _blocks(turn.role, turn.content)
        if turn.role == "user":
            first = [type(block) is ToolResult for block in blocks]
            if first != sorted(first, reverse=True):
                raise Rejected("order")
            answered = [block.tool_use_id for block in blocks if type(block) is ToolResult]
            if len(answered) != len(set(answered)) or set(answered) != pending:
                raise Rejected("pairing")
            pending = set()
        else:
            for call in (block for block in blocks if type(block) is ToolCall):
                if call.id in seen:
                    raise Rejected("duplicate_id")
                if call.name not in tools:
                    raise Rejected("unknown_tool")
                seen.add(call.id)
                pending.add(call.id)
        flat += blocks
        flat += [inner for block in blocks if type(block) is ToolResult for inner in block.content]
    if len(conversation) % 2 == 0:
        raise Rejected("last_turn")
    kinds = [type(block) for block in flat]
    counts = {"blocks": len(kinds), "images": kinds.count(Image),
              "thinking": kinds.count(Thinking) + kinds.count(RedactedThinking)}  # fmt: skip
    if any(count > limits[name] for name, count in counts.items()):
        raise Rejected("limit")
    return tuple(flat)


def surplus(blocks: tuple[Block, ...], max_tokens: int) -> int:
    """Los tokens que `blocks` pueden costar por encima de sus bytes. `Rejected` si uno no se
    puede acotar: no se reserva ni se llama."""
    try:
        return sum(BOUND[type(block)](block, max_tokens) for block in blocks)
    except KeyError:
        raise Rejected("unbounded") from None

"""Los turnos con tools y los bloques del propio modelo (F8-05b7; ADR-034 §5): se validan antes
de abrir el scope, la clave, la reserva o la llamada, y dan la parte de la cota que sus bytes no
acotan. `Rejected` lleva solo el código de la regla: nunca un texto, un id, un nombre ni un tamaño
del turno.

Las reglas son las de la Messages API: a cada `tool_use` de un turno del asistente le contesta un
`tool_result` en el turno siguiente de quien pide, y solo ahí, con los resultados primero; un
`thinking` o `redacted_thinking` vuelve sin cambios; el último turno es de quien pide. No se exige
que el razonamiento vaya primero en un turno del asistente: con el razonamiento intercalado de
`thinking: adaptive` («Adaptive mode relaxes this: no assistant turn needs to start with one», guía
de thinking de Anthropic) llega también entre llamadas a tools, y exigirlo rechazaría respuestas
válidas. Todo texto tiene que poder codificarse en UTF-8 y la entrada de una tool, en JSON."""

import math
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
BASE64 = re.compile(r"(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?")
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


def _utf8(text: str) -> None:
    try:
        text.encode()
    except UnicodeEncodeError:  # un sustituto suelto: no se puede enviar
        raise Rejected("field") from None


def _json(value: object, depth: int = 0) -> None:
    """La entrada de una tool: solo tipos JSON, claves de texto, números finitos y textos UTF-8."""
    if depth > 64:
        raise Rejected("field")
    if type(value) is dict:
        for key, item in value.items():
            if type(key) is not str:
                raise Rejected("field")
            _utf8(key)
            _json(item, depth + 1)
    elif type(value) is list:
        for item in value:
            _json(item, depth + 1)
    elif type(value) is str:
        _utf8(value)
    elif type(value) is float and not math.isfinite(value):
        raise Rejected("field")
    elif type(value) not in (int, float, bool, type(None)):
        raise Rejected("field")


def _fields(block: Any) -> None:
    if any(type(getattr(block, name)) is not kind for name, kind in FIELDS[type(block)]):
        raise Rejected("field")
    for name, kind in FIELDS[type(block)]:
        if kind is str:
            _utf8(getattr(block, name))
    empty = {Text: "text", Thinking: "signature", RedactedThinking: "data"}.get(type(block))
    ids = [block.id] if type(block) is ToolCall else []
    ids += [block.tool_use_id] if type(block) is ToolResult else []
    if (empty and not getattr(block, empty).strip()) or not all(ID.fullmatch(i) for i in ids):
        raise Rejected("field")
    if type(block) is ToolCall:
        _json(block.input)
    if type(block) is Image:
        size = 0 < len(block.data) <= settings.AI_TURN_LIMITS["image_chars"]
        if block.media_type not in MEDIA or not size or not BASE64.fullmatch(block.data):
            raise Rejected("image")


def _blocks(role: str, content: object) -> tuple[Block, ...]:
    """Los bloques de un turno, con sus campos comprobados; el texto simple es un bloque."""
    blocks = (Text(content),) if type(content) is str else content
    if type(blocks) is not tuple or not blocks:  # un turno vacío no se envía
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


def reply(blocks: tuple[Block, ...]) -> None:
    """Una respuesta con llamadas a tools (F8-07d), la única que se reenvía: las reglas de un turno
    del asistente y sin ids repetidos. `Rejected`; nunca quita un bloque."""
    _blocks("assistant", blocks)
    ids = [block.id for block in blocks if type(block) is ToolCall]
    if len(ids) != len(set(ids)):
        raise Rejected("duplicate_id")


def check(request: LLMRequest) -> tuple[Block, ...]:
    """Todos los bloques de `request` (también los de cada resultado), validados. `Rejected`."""
    limits, conversation = settings.AI_TURN_LIMITS, request.turns
    if not 0 < len(conversation) <= limits["turns"]:
        raise Rejected("limit")
    for tool in request.tools:  # las del código, pero un nombre que no es texto sería un 500
        shape = (type(tool.name), type(tool.description), type(tool.schema))
        if shape != (str, str, dict) or not ID.fullmatch(tool.name):
            raise Rejected("field")
        _utf8(tool.description)
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

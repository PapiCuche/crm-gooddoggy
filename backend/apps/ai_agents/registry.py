"""El Tool Registry del copiloto (F8-07a; ADR-005 Parte C, ADR-034 §6, §11).

Las tools se registran en código y se comprueban **al cargar** (si no, Django no arranca): solo
`READ` y `PUBLIC_SAFE`, sin argumentos de identidad, con una salida cerrada y sin claves de
costos, márgenes, listas internas o ubicaciones, y con un permiso no sensible. Tras arrancar, el
registro se cierra. `invoke` corre con el `ExecutionContext` de **la persona** en el
`tenant_scope` de su organización: valida la entrada sin devolverla, exige el permiso, comprueba
la forma de la salida y devuelve el sobre `{ok, data | error, meta}` para el modelo y la evidencia,
que se queda en el servidor (con sus `facts`, F8-07d: lo exacto que el modelo no ve, como el stock).
El log lleva solo la tool, el código y la duración."""

import json
import re
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any
from uuid import UUID

from django.db import connections

from apps.access.selectors import ExecutionContext, definition, has_permission
from apps.ai_agents.fields import Field, Invalid
from apps.ai_gateway import llm
from core.ids import new_id
from core.observability.logging import get_logger
from core.tenancy.context import ActorType
from core.tenancy.scope import require_scope

logger = get_logger(__name__)

MAX_BYTES = 8192  # el sobre en UTF-8; si no cabe, `RESULT_TOO_LARGE` (nunca un JSON cortado)
MAX_FIELDS = 4
NAME = re.compile(r"[a-z][a-z0-9_]{2,40}")
CAMEL = re.compile(r"([a-z0-9])([A-Z])")
# Se buscan en la clave normalizada (minúsculas, camelCase partido, sin separadores): `orgId`,
# `OrganizationID` y `tenantid` son identidad; `unitCost`, `BarCode` y `SKU`, salida prohibida.
WHO = ("org", "tenant", "contact", "conversation", "customer", "user", "member", "actor",
       "branch", "account", "phone", "email")  # fmt: skip
SECRET = ("cost", "margin", "internal", "onhand", "reserved", "warehouse", "location", "note",
          "supplier", "sku", "barcode", "purchase", "stock", "description")  # fmt: skip
CODES = frozenset({"ean", "gtin", "upc"})  # cortos: solo como palabra entera
LEAVES = (str, int, float, bool, type(None))  # una hoja abierta (dict, list…) dejaría pasar todo


class Risk(StrEnum):
    READ = "READ"
    WRITE_LOW = "WRITE_LOW"
    WRITE_HIGH = "WRITE_HIGH"


class Audience(StrEnum):
    PUBLIC_SAFE = "PUBLIC_SAFE"
    INTERNAL_ONLY = "INTERNAL_ONLY"


class ToolRejected(Exception):  # noqa: N818 — al cargar: la tool no entra en el registro
    pass


class NotFound(Exception):  # noqa: N818 — lo ausente, lo ajeno y lo no publicado, igual
    pass


def _no_facts() -> Mapping[str, int | None]:
    return MappingProxyType({})


@dataclass(frozen=True)
class Found:
    data: dict[str, Any]
    subjects: tuple[UUID, ...]  # lo que respalda la evidencia (productos, variantes…)
    facts: Mapping[str, int | None] = field(default_factory=_no_facts, repr=False)


@dataclass(frozen=True)
class Evidence:  # en el servidor: no se serializa hacia el modelo
    evidence_id: UUID
    kind: str
    tool: str
    as_of: datetime
    subjects: tuple[UUID, ...]
    # Lo exacto para el vendedor (F8-07d: el stock): de solo lectura, fuera del sobre, del `meta`,
    # del log y del `repr`.
    facts: Mapping[str, int | None] = field(default_factory=_no_facts, repr=False)


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    fields: Mapping[str, Field]
    output: Mapping[str, Any]  # la forma exacta: un tipo, un dict de formas o `[forma]`
    risk: Risk
    audience: Audience
    permission: str
    kind: str  # el tipo de evidencia: CATALOG, PRICE, STOCK
    handler: Callable[[ExecutionContext, dict[str, Any]], Found] = field(repr=False)
    # Antes de leer nada (F8-07d): una configuración inválida falla cerrado, sin consultas
    precheck: Callable[[], object] = field(default=lambda: None, repr=False)


@dataclass(frozen=True)
class Invocation:
    ok: bool
    text: str  # el sobre en JSON, lo único que ve el modelo
    evidence: Evidence | None
    code: str = "OK"

    def result(self, tool_use_id: str) -> llm.ToolResult:
        """El `tool_result` del turno siguiente (formato neutro de #968)."""
        return llm.ToolResult(tool_use_id, (llm.Text(self.text),), is_error=not self.ok)


_TOOLS: dict[str, ToolSpec] = {}
TOOLS = MappingProxyType(_TOOLS)  # solo lectura; `register` la llena hasta `freeze()`
_open = True


def _words(key: str) -> list[str]:
    return [w for w in re.split(r"[^a-z0-9]+", CAMEL.sub(r"\1_\2", key).lower()) if w]


def _identity(name: str) -> bool:
    return any(who in "".join(_words(name)) for who in WHO)


def _secret(key: str) -> bool:
    """La descripción corta es pública; la larga, no."""
    joined = re.sub(r"(^|_)short_description(?=_|$)", "_", "_".join(_words(key)))
    return bool(CODES & set(_words(key))) or any(w in joined.replace("_", "") for w in SECRET)


def _keys(shape: object) -> Iterator[str]:
    """Las claves de una forma, a cualquier profundidad; `ToolRejected` si no es cerrada."""
    if isinstance(shape, Mapping):
        for key, inner in shape.items():
            yield key if type(key) is str else ""
            yield from _keys(inner)
    elif type(shape) is list and len(shape) == 1:
        yield from _keys(shape[0])
    elif not (shape in LEAVES or type(shape) is tuple and shape and set(shape) <= set(LEAVES)):
        raise ToolRejected("salida")


def check(spec: ToolSpec) -> None:
    """`ToolRejected` si la tool no puede estar en el registro de este corte."""
    if spec.risk is not Risk.READ or spec.audience is not Audience.PUBLIC_SAFE:
        raise ToolRejected("solo READ y PUBLIC_SAFE")
    if not NAME.fullmatch(spec.name) or spec.name in TOOLS:
        raise ToolRejected("nombre")
    if not 0 < len(spec.fields) <= MAX_FIELDS or any(_identity(n) for n in spec.fields):
        raise ToolRejected("argumentos")
    if any(not key or _secret(key) for key in _keys(spec.output)):
        raise ToolRejected("salida")
    try:
        sensitive = definition(spec.permission).is_sensitive
    except LookupError:
        raise ToolRejected("permiso") from None
    if sensitive:
        raise ToolRejected("permiso")


def register(spec: ToolSpec) -> ToolSpec:
    if not _open:  # tras arrancar no entra ninguna
        raise ToolRejected("registro cerrado")
    check(spec)
    _TOOLS[spec.name] = spec
    return spec


def freeze() -> None:
    global _open
    _open = False


def declared(spec: ToolSpec) -> llm.Tool:
    """La tool como la ve el modelo: esquema estricto, todos los campos obligatorios."""
    schema = {"type": "object", "additionalProperties": False, "required": list(spec.fields),
              "properties": {name: f.schema() for name, f in spec.fields.items()}}  # fmt: skip
    return llm.Tool(spec.name, spec.description, schema)


def _clean(spec: ToolSpec, raw: object) -> dict[str, Any]:
    if type(raw) is not dict or set(raw) != set(spec.fields):
        raise Invalid
    return {name: spec.fields[name].clean(raw[name]) for name in spec.fields}


def _conforms(value: object, shape: object) -> bool:
    if isinstance(shape, Mapping):
        if type(value) is not dict or set(value) != set(shape):
            return False
        return all(_conforms(value[key], inner) for key, inner in shape.items())
    if isinstance(shape, list):
        return type(value) is list and all(_conforms(item, shape[0]) for item in value)
    return type(value) in (shape if isinstance(shape, tuple) else (shape,))


def _dump(envelope: dict[str, Any]) -> str:
    return json.dumps(envelope, ensure_ascii=False, separators=(",", ":"), sort_keys=True,
                      allow_nan=False)  # fmt: skip


def _failed(code: str) -> Invocation:
    return Invocation(False, _dump({"ok": False, "error": {"code": code}}), None, code)


def _now(ectx: ExecutionContext) -> datetime:
    with connections[require_scope(ectx.tenant)].cursor() as cursor:
        cursor.execute("SELECT clock_timestamp()")
        now: datetime = cursor.fetchone()[0]
    return now


def _run(spec: ToolSpec | None, ectx: ExecutionContext, raw: object) -> Invocation:
    if spec is None or spec.risk is not Risk.READ or spec.audience is not Audience.PUBLIC_SAFE:
        return _failed("UNKNOWN_TOOL")
    spec.precheck()
    try:
        args = _clean(spec, raw)
    except Invalid:
        return _failed("INVALID_INPUT")
    person = ectx.tenant.actor_type == ActorType.USER and ectx.tenant.user_id is not None
    if not (person and has_permission(ectx, spec.permission)):  # la persona, nunca SYSTEM
        return _failed("FORBIDDEN")
    as_of = _now(ectx)
    try:
        found = spec.handler(ectx, args)
    except NotFound:
        return _failed("NOT_FOUND")
    if not _conforms(found.data, spec.output):  # un fallo nuestro: no sale nada
        raise RuntimeError(f"{spec.name}: la salida no tiene la forma declarada")
    facts = MappingProxyType(dict(found.facts))  # una copia: el handler ya no la cambia
    evidence = Evidence(new_id(), spec.kind, spec.name, as_of, found.subjects, facts)
    meta = {"evidence_id": str(evidence.evidence_id), "kind": spec.kind,
            "as_of": as_of.isoformat()}  # fmt: skip
    text = _dump({"ok": True, "data": found.data, "meta": meta})
    if len(text.encode()) > MAX_BYTES:
        return _failed("RESULT_TOO_LARGE")
    return Invocation(True, text, evidence)


def invoke(ectx: ExecutionContext, call: llm.ToolCall) -> Invocation:
    """Una llamada del modelo a una tool, en el `tenant_scope` de `ectx`."""
    spec, start = TOOLS.get(call.name), time.monotonic()
    done = _run(spec, ectx, call.input)
    logger.info("ai.tool_invoked", tool=spec.name if spec else "unknown", outcome=done.code,
                ms=round((time.monotonic() - start) * 1000))  # fmt: skip
    return done

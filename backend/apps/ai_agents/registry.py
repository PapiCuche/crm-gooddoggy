"""El Tool Registry del copiloto (F8-07a; ADR-005 Parte C, ADR-034 §6, §11).

Las tools se registran en código y se comprueban **al cargar** (si no, Django no arranca): solo
`READ` y `PUBLIC_SAFE`, sin argumentos de identidad, con una salida cerrada y sin claves de
costos, márgenes, listas internas o ubicaciones, y con un permiso no sensible. Tras arrancar, el
registro se cierra y cada llamada vuelve a comprobar su tool (F8-07e). `invoke` corre con el
`ExecutionContext` de **la persona** en el `tenant_scope` de su organización: valida la entrada
sin devolverla, exige el permiso, comprueba la forma de la salida y devuelve el sobre
`{ok, data | error, meta}` para el modelo y la evidencia, que se queda en el servidor (con sus
`facts`, F8-07d: lo exacto que el modelo no ve, como el stock). El log lleva solo la tool, el
código y la duración.

F8-08c: el copiloto llama `invoke_read_only`, que abre para cada llamada su propio `tenant_scope`
corto con `SET TRANSACTION READ ONLY`: PostgreSQL rechaza cualquier escritura de datos, `nextval`
o un objeto grande, se escriba como se escriba el SQL. `invoke` falla si su transacción no es de
solo lectura. La evidencia guarda además lo que vio el modelo (`data`), para que el Output Guard
copie de ahí el valor de una claim."""

import json
import re
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any
from uuid import UUID

from django.db import connections, transaction

from apps.access.selectors import (
    AccessDenied,
    ExecutionContext,
    definition,
    execution_context,
    has_permission,
)
from apps.ai_agents.fields import Field, Invalid
from apps.ai_gateway import llm
from core.ids import new_id
from core.observability.logging import get_logger
from core.tenancy.context import ActorType, TenantContext, TenantContextError, current
from core.tenancy.scope import require_scope, tenant_scope

logger = get_logger(__name__)

MAX_BYTES = 8192  # el sobre en UTF-8; si no cabe, `RESULT_TOO_LARGE` (nunca un JSON cortado)
MAX_FIELDS = 4
NAME = re.compile(r"[a-z][a-z0-9_]{2,40}")
KEY = re.compile(r"[A-Za-z0-9_-]+")  # F8-07e: en ASCII, o `ѕku` (cirílica) y `ｓｋｕ` se colarían
CAMEL = re.compile(r"([a-z0-9])([A-Z])")
# Se buscan en la clave normalizada (minúsculas, camelCase partido, sin separadores): `orgId`,
# `OrganizationID` y `tenantid` son identidad; `unitCost`, `BarCode` y `SKU`, salida prohibida.
# También en español y sus sinónimos (F8-07e).
WHO = ("org", "tenant", "contact", "convers", "customer", "client", "user", "usuario", "member",
       "actor", "branch", "sucursal", "account", "phone", "telefono", "email", "correo", "owner",
       "seller", "vendedor", "person", "company", "empresa", "buyer", "comprador", "lead", "deal",
       "negocio", "opportunit", "oportunidad", "message", "mensaje", "chat", "thread", "agent",
       "staff", "waid", "psid", "uid")  # fmt: skip
IDS = frozenset({"dni", "rut"})  # cortos: solo como palabra entera (`fruta` y `rutina`, no)
SECRET = ("cost", "margin", "margen", "intern", "onhand", "reserv", "warehouse", "almacen",
          "bodega", "location", "ubicacion", "note", "nota", "comment", "comentario",
          "observacion", "supplier", "proveedor", "vendor", "sku", "barcode", "barra", "gtin",
          "isbn", "mpn", "purchase", "compra", "stock", "existencia", "inventor", "quantit", "qty",
          "cantidad", "descrip")  # fmt: skip
CODES = ("ean", "upc")  # cortos: al principio de una palabra (`ean13`); `gtin`, en cualquier sitio
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


def _no_data() -> Mapping[str, Any]:
    return MappingProxyType({})


def _frozen(value: Any) -> Any:
    """Una copia de solo lectura a cualquier profundidad: un dict, un `MappingProxyType`; una
    lista, una tupla. Las hojas (`str`, `int`…) ya lo son."""
    if isinstance(value, dict):
        return MappingProxyType({key: _frozen(inner) for key, inner in value.items()})
    if isinstance(value, list):
        return tuple(_frozen(inner) for inner in value)
    return value


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
    # del log y del `repr`; fuera de la igualdad y del hash (la evidencia sigue siendo hashable).
    facts: Mapping[str, int | None] = field(
        default_factory=_no_facts, repr=False, compare=False, hash=False
    )
    # Lo que vio el modelo (el `data` del sobre, F8-08c): de ahí copia el Output Guard el valor de
    # una claim, nunca del texto del modelo. Igual de cerrado que `facts`.
    data: Mapping[str, Any] = field(default_factory=_no_data, repr=False, compare=False, hash=False)


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
    # Con el `as_of` de la evidencia (F8-08c): lo que depende del instante se lee en ese instante
    handler: Callable[[ExecutionContext, dict[str, Any], datetime], Found] = field(repr=False)
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
    words = _words(name)
    return bool(IDS & set(words)) or any(who in "".join(words) for who in WHO)


def _secret(key: str) -> bool:
    """La descripción corta es pública (la clave entera, nada más); la larga, no."""
    words = _words(key)
    if words == ["short", "description"]:
        return False
    return any(w.startswith(CODES) for w in words) or any(w in "".join(words) for w in SECRET)


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


def _vet(spec: ToolSpec) -> None:
    """Lo de `check` salvo el nombre repetido. También en cada llamada (F8-07e): una forma que
    cambió tras el registro (`ROW` es un dict) no llega a ejecutarse."""
    if spec.risk is not Risk.READ or spec.audience is not Audience.PUBLIC_SAFE:
        raise ToolRejected("solo READ y PUBLIC_SAFE")
    if not NAME.fullmatch(spec.name):
        raise ToolRejected("nombre")
    if not 0 < len(spec.fields) <= MAX_FIELDS:
        raise ToolRejected("argumentos")
    if any(not KEY.fullmatch(name) or _identity(name) for name in spec.fields):
        raise ToolRejected("argumentos")
    if any(not KEY.fullmatch(key) or _secret(key) for key in _keys(spec.output)):
        raise ToolRejected("salida")
    try:
        sensitive = definition(spec.permission).is_sensitive
    except LookupError:
        raise ToolRejected("permiso") from None
    if sensitive:
        raise ToolRejected("permiso")


def check(spec: ToolSpec) -> None:
    """`ToolRejected` si la tool no puede estar en el registro de este corte."""
    if spec.name in TOOLS:
        raise ToolRejected("nombre")
    _vet(spec)


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


def fits(data: dict[str, Any], kind: str) -> bool:
    """¿Cabe `data` en el sobre de `MAX_BYTES`, con el `meta` más largo? Para que una tool corte
    filas en lugar de fallar con `RESULT_TOO_LARGE` (F8-07e)."""
    meta = {"evidence_id": str(UUID(int=0)), "kind": kind, "as_of": "0" * 32}  # +00:00 con µs
    return len(_dump({"ok": True, "data": data, "meta": meta}).encode()) <= MAX_BYTES


def _failed(code: str) -> Invocation:
    return Invocation(False, _dump({"ok": False, "error": {"code": code}}), None, code)


def _now(ectx: ExecutionContext) -> datetime:
    """El `as_of`, si la transacción es de solo lectura; si no, un fallo nuestro (F8-08c)."""
    with connections[require_scope(ectx.tenant)].cursor() as cursor:
        cursor.execute("SELECT clock_timestamp(), current_setting('transaction_read_only')")
        now, mode = cursor.fetchone()
    if mode != "on":
        raise RuntimeError("una tool solo corre en una transacción de solo lectura")
    stamp: datetime = now
    return stamp


def _person(ectx: ExecutionContext) -> bool:
    return ectx.tenant.actor_type == ActorType.USER and ectx.tenant.user_id is not None


def _run(spec: ToolSpec | None, ectx: ExecutionContext | None, raw: object) -> Invocation:
    try:
        if spec is None:
            raise ToolRejected("desconocida")
        _vet(spec)
    except ToolRejected:
        return _failed("UNKNOWN_TOOL")
    spec.precheck()
    try:
        args = _clean(spec, raw)
    except Invalid:
        return _failed("INVALID_INPUT")
    # la persona con su membresía activa, nunca SYSTEM
    if ectx is None or not _person(ectx) or not has_permission(ectx, spec.permission):
        return _failed("FORBIDDEN")
    as_of = _now(ectx)
    try:
        found: Found | None = spec.handler(ectx, args, as_of)
    except NotFound:
        found = None
    _now(ectx)  # sigue de solo lectura: un `COMMIT` en la tool habría abierto otra transacción
    if found is None:
        return _failed("NOT_FOUND")
    if not _conforms(found.data, spec.output):  # un fallo nuestro: no sale nada
        raise RuntimeError(f"{spec.name}: la salida no tiene la forma declarada")
    facts = MappingProxyType(dict(found.facts))  # una copia: el handler ya no la cambia
    evidence = Evidence(
        new_id(), spec.kind, spec.name, as_of, found.subjects, facts, _frozen(found.data)
    )
    meta = {"evidence_id": str(evidence.evidence_id), "kind": spec.kind,
            "as_of": as_of.isoformat()}  # fmt: skip
    text = _dump({"ok": True, "data": found.data, "meta": meta})
    if len(text.encode()) > MAX_BYTES:
        return _failed("RESULT_TOO_LARGE")
    return Invocation(True, text, evidence)


def invoke(ectx: ExecutionContext | None, call: llm.ToolCall) -> Invocation:
    """Una llamada del modelo a una tool, en el `tenant_scope` de `ectx`, que tiene que ser de solo
    lectura. Sin `ectx` (sin membresía activa), `FORBIDDEN`."""
    spec, start = TOOLS.get(call.name), time.monotonic()
    done = _run(spec, ectx, call.input)
    logger.info("ai.tool_invoked", tool=spec.name if spec else "unknown", outcome=done.code,
                ms=round((time.monotonic() - start) * 1000))  # fmt: skip
    return done


@contextmanager
def read_only_scope(tenant: TenantContext) -> Iterator[ExecutionContext | None]:
    """Un `tenant_scope` propio de `tenant`, de solo lectura antes de leer nada más (pasar a solo
    lectura tras las `set_config` del scope se admite; volver a lectura y escritura, no), con los
    permisos de la persona leídos en él; `None` si no tiene membresía activa (o es SYSTEM). Nunca
    dentro de otro scope: se quedaría en esa transacción y la dejaría de solo lectura."""
    if current() is not None:
        raise TenantContextError("las tools abren su propio tenant_scope de solo lectura")
    with tenant_scope(tenant):
        alias = require_scope(tenant)
        try:
            with connections[alias].cursor() as cursor:
                cursor.execute("SET TRANSACTION READ ONLY")
            try:
                ectx: ExecutionContext | None = execution_context(tenant)
            except AccessDenied:
                ectx = None
            yield ectx
        finally:  # nunca confirma: lo que un `COMMIT` en la tool o un `on_commit` dejó, se deshace
            transaction.set_rollback(True, using=alias)


def invoke_read_only(tenant: TenantContext, call: llm.ToolCall) -> Invocation:
    """`invoke` en su propio `tenant_scope` corto de solo lectura (F8-08c), fuera de cualquier
    otra transacción: lo que el copiloto usa entre dos llamadas al modelo (ADR-034 §7)."""
    with read_only_scope(tenant) as ectx:
        return invoke(ectx, call)

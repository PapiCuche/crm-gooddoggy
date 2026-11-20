"""El Output Guard del copiloto, parte I (F8-08e; ADR-034 §9, ADR-005 Parte D).

Ninguna cifra la escribe el modelo: su respuesta es una plantilla con placeholders `{{c1}}` y
claims `{id, kind, evidence_ref, path}` (`REPLY_SCHEMA`, estricto, para `output_config.format`).
`render` valida cada claim contra la evidencia de **esta** invocación (`tool_use_id → Evidence`,
`None` si la tool no dio `ok`): la tool da ese `kind`; el `path` existe en lo que vio el modelo
(`Evidence.data`) y es el de su `kind`; la evidencia de precio y stock tiene como mucho
`AI_EVIDENCE_TTL`; tras el nombre de un producto, sus claims hablan de él y de una sola variante.
El valor se copia de la evidencia y se formatea aquí, nunca de la plantilla. Un placeholder sin
claim válida queda como `VERIFY_MARK`, con un hallazgo (su tipo y la posición de la marca, nunca
lo que propuso el modelo) y un problema para el reintento (solo ids, códigos y nombres de tools).
`amount` es el precio actual; `regular_amount` solo es el «antes» con promoción (#1012). Del stock
se copia la franja con su texto fijo, nunca `facts`. Puro: sin base de datos y sin logs.

Los detectores fuera de los placeholders y el traspaso son de F8-08f; el bucle y el reintento, de
F8-08g. `context` (F8-08d) toma de aquí `REPLY_SCHEMA`."""

import re
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from apps.ai_agents.registry import Evidence, ToolSpec
from apps.ai_agents.tools.catalog import GET_PRODUCT_VARIANTS, SEARCH_PRODUCT
from apps.ai_agents.tools.price import GET_PRICE
from apps.ai_agents.tools.stock import AVAILABLE, FEW_UNITS, GET_STOCK, OUT_OF_STOCK
from core.text import VERIFY_MARK

MAX_TTL = 600  # ADR-034 §9: precio y stock, 10 minutos como mucho
MAX_CLAIMS, MAX_TEMPLATE = 40, 4000  # y como mucho MAX_CLAIMS placeholders: hallazgos acotados
CLAIM_ID = re.compile(r"c[0-9]{1,3}")
PLACEHOLDER = re.compile(r"\{\{([^{}]*)\}\}")  # todo `{{…}}` es un placeholder: o claim o marca
N = r"(?:0|[1-9][0-9]{0,2})"  # un índice de lista
MONEY = re.compile(r"[0-9]+(?:\.[0-9]+)?")
CURRENCY = re.compile(r"[A-Z]{3}")
SYMBOLS = {"PEN": "S/", "USD": "US$"}  # otra moneda va con su código ISO 4217
# NOT_TRACKED (no lleva stock) no tiene franja que afirmar: queda como marca
BANDS = {AVAILABLE: "disponible", FEW_UNITS: "disponible, con pocas unidades",
         OUT_OF_STOCK: "agotado"}  # fmt: skip
PROMOTED = ("REGULAR_PRICE", "PROMOTION")  # solo valen si la fila tiene promoción


@dataclass(frozen=True)
class Kind:
    tools: tuple[ToolSpec, ...]  # la primera es la que se pide en el reintento
    path: re.Pattern[str]

    @property
    def fresh(self) -> bool:  # precio y stock caducan; el catálogo, no
        return self.tools[0].kind != "CATALOG"


KINDS = {
    "PRODUCT_NAME": Kind((SEARCH_PRODUCT, GET_PRODUCT_VARIANTS),
                         re.compile(rf"products\.{N}\.name|name|variants\.{N}\.name")),
    "PRICE": Kind((GET_PRICE,), re.compile(rf"prices\.{N}\.amount")),
    "REGULAR_PRICE": Kind((GET_PRICE,), re.compile(rf"prices\.{N}\.regular_amount")),
    "PROMOTION": Kind((GET_PRICE,), re.compile(rf"prices\.{N}\.promotion")),
    "PROMOTION_TERMS": Kind((GET_PRICE,), re.compile(rf"promotions\.{N}\.terms")),
    "STOCK": Kind((GET_STOCK,), re.compile(r"availability")),
}  # fmt: skip
FIELDS = ("id", "kind", "evidence_ref", "path")
# ADR-005 Parte D: estricto (sin campos extra) y solo con lo que admite `output_config.format`
REPLY_SCHEMA: dict[str, Any] = {
    "type": "object", "additionalProperties": False,
    "required": ["reply_template", "claims", "wants_handoff"],
    "properties": {
        "reply_template": {"type": "string", "description": "La respuesta. Cada nombre, precio, "
                           "promoción o disponibilidad va como {{id}} de su claim, nunca escrito."},
        "claims": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": list(FIELDS),
            "properties": {
                "id": {"type": "string", "description": "c1, c2…"},
                "kind": {"type": "string", "enum": list(KINDS)},
                "evidence_ref": {"type": "string",
                                 "description": "El id del tool_use cuyo resultado lo respalda"},
                "path": {"type": "string",
                         "description": "El campo de `data` de ese resultado: prices.0.amount"},
            },
        }},
        "wants_handoff": {"type": "boolean"},
    },
}  # fmt: skip


class Malformed(ValueError):  # no cumple el esquema o pasa los topes: no se renderiza nada
    FEEDBACK = "la respuesta no cumple el esquema de salida; responde con reply_template y claims"


@dataclass(frozen=True)
class Claim:
    id: str
    kind: str
    evidence_ref: str
    path: str


@dataclass(frozen=True)
class Finding:
    type: str
    start: int  # la posición de su `VERIFY_MARK` en el texto renderizado
    end: int


@dataclass(frozen=True)
class Problem:
    code: str
    claim: str | None = None  # un id con la forma `c<n>`, nunca otro texto del modelo
    tool: str | None = None

    def text(self) -> str:
        if self.code == "CLAIM_MISSING":
            if self.claim is None:
                return "un placeholder no tiene claim con un id válido (c1, c2…)"
            return f"el placeholder {{{{{self.claim}}}}} no tiene claim"
        if self.code == "CLAIM_ID":
            return "una claim tiene un id inválido; usa c1, c2…"
        if self.code == "CLAIM_DUPLICATE":
            return f"la claim {self.claim} está repetida"
        if self.code == "CLAIM_UNUSED":
            return f"la claim {self.claim} no tiene placeholder"
        tool = f"llama a `{self.tool}`"
        return f"la claim {self.claim} no tiene evidencia válida ({self.code}); {tool}"


@dataclass(frozen=True)
class Rendered:
    text: str
    findings: tuple[Finding, ...]
    problems: tuple[Problem, ...]
    wants_handoff: bool
    expires_at: datetime | None  # cuándo caduca la primera evidencia de precio o stock citada

    @property
    def ok(self) -> bool:
        return not self.problems

    def feedback(self) -> str:
        """El motivo estructurado del reintento (ADR-005 Parte D)."""
        return "\n".join(problem.text() for problem in self.problems)


@dataclass(frozen=True)
class _Cited:
    value: str
    subject: str | None  # lo que nombra una claim de nombre (producto o variante)
    variant: str | None  # la variante de una claim de precio o stock
    subjects: frozenset[str]
    expires: datetime | None


def evidence_ttl() -> timedelta:
    """`AI_EVIDENCE_TTL`; `ImproperlyConfigured` si no es un entero de 1 a `MAX_TTL`."""
    ttl = settings.AI_EVIDENCE_TTL
    if type(ttl) is not int or not 1 <= ttl <= MAX_TTL:
        raise ImproperlyConfigured("AI_EVIDENCE_TTL")
    return timedelta(seconds=ttl)


def parse(raw: object) -> tuple[str, tuple[Claim, ...], bool]:
    """La respuesta como la exige `REPLY_SCHEMA` (también en el servidor), o `Malformed`."""
    if type(raw) is not dict or set(raw) != set(REPLY_SCHEMA["required"]):
        raise Malformed
    template, claims, handoff = raw["reply_template"], raw["claims"], raw["wants_handoff"]
    if type(template) is not str or type(claims) is not list or type(handoff) is not bool:
        raise Malformed
    if len(template) > MAX_TEMPLATE or len(claims) > MAX_CLAIMS:
        raise Malformed
    if len(PLACEHOLDER.findall(template)) > MAX_CLAIMS:
        raise Malformed
    for claim in claims:
        if type(claim) is not dict or set(claim) != set(FIELDS):
            raise Malformed
        if any(type(value) is not str for value in claim.values()) or claim["kind"] not in KINDS:
            raise Malformed
    return template, tuple(Claim(**claim) for claim in claims), handoff


def _at(data: object, path: str) -> list[Any] | None:
    """Los nodos de `path` en `data`, de la raíz a la hoja; `None` si no existe."""
    nodes = [data]
    for step in path.split("."):
        node = nodes[-1]
        if isinstance(node, Mapping) and step in node:
            nodes.append(node[step])
        elif isinstance(node, list | tuple) and re.fullmatch(N, step) and int(step) < len(node):
            nodes.append(node[int(step)])
        else:
            return None
    return nodes


def _money(currency: object, amount: str) -> str | None:
    if type(currency) is not str or not CURRENCY.fullmatch(currency) or not MONEY.fullmatch(amount):
        return None
    return f"{SYMBOLS.get(currency, currency)} {Decimal(amount):,f}"


def _value(kind: str, row: Mapping[str, Any], leaf: str) -> str | None:
    if kind in ("PRICE", "REGULAR_PRICE"):
        return _money(row.get("currency"), leaf)
    if kind == "STOCK":
        return BANDS.get(leaf)
    return leaf


def _cite(claim: Claim, found: Evidence | None, now: datetime, ttl: timedelta) -> _Cited | str:
    """El valor de la evidencia para `claim`, o el tipo del hallazgo."""
    kind = KINDS[claim.kind]
    if found is None:
        return "EVIDENCE_MISSING"
    if found.tool not in {tool.name for tool in kind.tools} or found.kind != kind.tools[0].kind:
        return "KIND_MISMATCH"
    nodes = _at(found.data, claim.path)
    if nodes is None:
        return "PATH_MISSING"
    if not kind.path.fullmatch(claim.path):
        regular = claim.kind == "PRICE" and KINDS["REGULAR_PRICE"].path.fullmatch(claim.path)
        return "PRICE_NOT_CURRENT" if regular else "KIND_MISMATCH"
    expires = found.as_of + ttl if kind.fresh else None
    if expires is not None and now > expires:
        return "EVIDENCE_STALE"
    row, leaf = nodes[-2], nodes[-1]
    if claim.kind in PROMOTED and row.get("promotion") is None:
        return "NO_PROMOTION"
    value = _value(claim.kind, row, leaf) if type(leaf) is str and leaf.strip() else None
    if value is None:
        return "VALUE_MISSING"
    subject = row.get("variant_id", row.get("product_id"))
    variant = found.data.get("variant_id")
    subjects = frozenset(map(str, found.subjects))
    return _Cited(value, subject, variant, subjects, expires)


def render(raw: object, evidence: Mapping[str, Evidence | None], now: datetime) -> Rendered:
    """El texto con los valores copiados de `evidence`, o `Malformed`."""
    ttl = evidence_ttl()
    template, claims, handoff = parse(raw)
    problems: list[Problem] = []
    counts = Counter(item.id for item in claims)
    by_id: dict[str, Claim] = {}
    for item in claims:
        if not CLAIM_ID.fullmatch(item.id):
            problems.append(Problem("CLAIM_ID"))
        elif counts[item.id] > 1:
            problems.append(Problem("CLAIM_DUPLICATE", item.id))
        else:
            by_id[item.id] = item
    out: list[str] = []
    findings: list[Finding] = []
    used: set[str] = set()
    anchor: str | None = None  # lo que nombró la última claim de nombre
    variant: str | None = None  # la variante de las claims que la siguen
    expires: datetime | None = None
    pos = 0
    for match in PLACEHOLDER.finditer(template):
        out.append(template[pos : match.start()])
        pos, key = match.end(), match.group(1)
        cited: _Cited | str
        if (claim := by_id.get(key)) is None:
            cited = "CLAIM_MISSING"
        else:
            used.add(key)
            cited = _cite(claim, evidence.get(claim.evidence_ref), now, ttl)
            if claim.kind == "PRODUCT_NAME":  # empieza otro producto, válido o no
                anchor = cited.subject if isinstance(cited, _Cited) else None
                variant = None
            elif isinstance(cited, _Cited):
                if (anchor is not None and anchor not in cited.subjects) or (
                    variant is not None and cited.variant != variant
                ):
                    cited = "SUBJECT_MISMATCH"
                else:
                    variant = cited.variant
        if isinstance(cited, _Cited):
            out.append(cited.value)
            if cited.expires is not None:
                expires = cited.expires if expires is None else min(expires, cited.expires)
        else:
            start = sum(map(len, out))
            out.append(VERIFY_MARK)
            findings.append(Finding(cited, start, start + len(VERIFY_MARK)))
            if claim is None:  # el id solo si tiene la forma `c<n>`
                problems.append(Problem(cited, key if CLAIM_ID.fullmatch(key) else None))
            else:
                problems.append(Problem(cited, claim.id, KINDS[claim.kind].tools[0].name))
    out.append(template[pos:])
    problems += [Problem("CLAIM_UNUSED", key) for key in by_id if key not in used]
    unique = tuple(dict.fromkeys(problems))
    return Rendered("".join(out), tuple(findings), unique, handoff, expires)

"""El Output Guard del copiloto, parte I (F8-08e; ADR-034 §9, ADR-005 Parte D).

La respuesta del modelo es una plantilla con placeholders `{{c1}}` y claims (`reply_schema()`).
`render` valida cada claim contra la evidencia de **esta** invocación (`tool_use_id → Evidence`,
`None` si no dio `ok`): tool y `kind`, `path` del `kind` en `Evidence.data`, evidencia ni del futuro
ni caducada (`AI_EVIDENCE_TTL`); y por grupos (un nombre abre uno; lo anterior al primero es suyo):
un producto y una variante, el «antes» y la promoción de la fila del precio actual, las condiciones
de la fila citada más cercana; y los precios de una variante, de una sola consulta. Copia el valor
de la evidencia, nunca de la plantilla, con las etiquetas del servidor («precio regular», y el
nombre de la lista si se citan varias de una variante); si no, `VERIFY_MARK` con un hallazgo
(tipo y posición, nunca el texto del modelo) y un problema para el reintento. Las posiciones son
índices de `str` (code points, no UTF-16: F8-09 las convierte); los detectores de F8-08f miran
solo lo que no está en `inserted` (valores, etiquetas y marcas). Puro: sin base de datos ni logs."""

import copy
import re
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from apps.ai_agents.registry import Evidence, ToolSpec
from apps.ai_agents.tools.catalog import GET_PRODUCT_VARIANTS, SEARCH_PRODUCT
from apps.ai_agents.tools.price import CUT, GET_PRICE
from apps.ai_agents.tools.stock import AVAILABLE, FEW_UNITS, GET_STOCK, NOT_TRACKED, OUT_OF_STOCK
from apps.conversations.models import BODY_MAX
from core.text import VERIFY_MARK

MAX_TTL = 600  # ADR-034 §9: precio y stock, 10 minutos como mucho
MAX_CLAIMS, MAX_TEMPLATE = 40, 4000  # y como mucho MAX_CLAIMS placeholders: hallazgos acotados
MAX_FIELD = 64  # cada campo de una claim (un `tool_use_id` tiene como mucho 64)
CLAIM_ID = re.compile(r"c[0-9]{1,3}")
UNSTORABLE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff]")  # NUL, C0 y sustitutos
PLACEHOLDER = re.compile(r"\{\{([^{}]*)\}\}")  # todo `{{…}}` es un placeholder: o claim o marca
N = r"(?:0|[1-9][0-9]{0,2})"  # un índice de lista
MONEY = re.compile(r"[0-9]+(?:\.[0-9]+)?")
CURRENCY = re.compile(r"[A-Z]{3}")
SYMBOLS = {"PEN": "S/", "USD": "US$"}  # otra moneda va con su código ISO 4217
BANDS = {AVAILABLE: "disponible", FEW_UNITS: "disponible, con pocas unidades",
         OUT_OF_STOCK: "agotado"}  # fmt: skip
PROMOTED = ("REGULAR_PRICE", "PROMOTION")  # solo con promoción y de la fila del precio actual
ROWS = ("PRICE", *PROMOTED)  # las que citan una fila de `prices`
REGULAR = "precio regular"  # lo escribe el servidor: el «antes» nunca pasa por lo que se paga
# Códigos que solo cambian el texto del reintento; el hallazgo va con su tipo
FINDING = {"NOT_TRACKED": "VALUE_MISSING", "TRUNCATED": "VALUE_MISSING",
           "PRICE_TWICE": "SUBJECT_MISMATCH"}  # fmt: skip
TEXTS = {
    "CLAIM_ID": "una claim tiene un id inválido; usa c1, c2…",
    "CLAIM_DUPLICATE": "la claim {claim} está repetida",
    "CLAIM_UNUSED": "la claim {claim} no tiene placeholder",
    "SUBJECT_MISMATCH": "la claim {claim} no es del producto, variante o fila de su grupo",
    "NOT_TRACKED": "la claim {claim} no lleva stock: no afirmes disponibilidad",
    "TRUNCATED": "las condiciones de la claim {claim} llegaron recortadas: no las cites",
    "PRICE_TWICE": "la claim {claim} es de otra consulta de precio de una variante ya citada",
    "TOO_LONG": f"la respuesta renderizada pasa de {BODY_MAX} caracteres: acórtala",
}


@dataclass(frozen=True)
class Kind:
    tools: tuple[ToolSpec, ...]  # la primera es la que se pide en el reintento
    path: re.Pattern[str]


KINDS = {
    "PRODUCT_NAME": Kind((SEARCH_PRODUCT, GET_PRODUCT_VARIANTS),
                         re.compile(rf"products\.{N}\.name|name|variants\.{N}\.name")),
    "PRICE": Kind((GET_PRICE,), re.compile(rf"prices\.{N}\.amount")),
    "REGULAR_PRICE": Kind((GET_PRICE,), re.compile(rf"prices\.{N}\.regular_amount")),
    "PROMOTION": Kind((GET_PRICE,), re.compile(rf"prices\.{N}\.promotion")),
    "PROMOTION_TERMS": Kind((GET_PRICE,), re.compile(rf"promotions\.{N}\.terms")),
    "STOCK": Kind((GET_STOCK,), re.compile(r"availability")),
}  # fmt: skip
TOP = ("reply_template", "claims", "wants_handoff")
FIELDS = ("id", "kind", "evidence_ref", "path")
# ADR-005 Parte D: estricto (sin campos extra) y solo con lo que admite `output_config.format`
_SCHEMA: dict[str, Any] = {
    "type": "object", "additionalProperties": False, "required": list(TOP),
    "properties": {
        "reply_template": {"type": "string", "description": "La respuesta. Cada nombre, precio, "
                           "promoción o disponibilidad va como {{id}} de su claim, nunca escrito. "
                           f"El servidor antepone «{REGULAR}» a REGULAR_PRICE y, si citas varias "
                           "listas de una variante, su nombre a cada PRICE: no los escribas tú."},
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


def reply_schema() -> dict[str, Any]:
    """Una copia del esquema: quien la cambie (el contexto, el gateway) no cambia el guard."""
    return copy.deepcopy(_SCHEMA)


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
    start: int  # la posición de su `VERIFY_MARK` en el texto renderizado (code points)
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
        if self.code in TEXTS:
            return TEXTS[self.code].format(claim=self.claim)
        tool = f"llama a `{self.tool}`"
        return f"la claim {self.claim} no tiene evidencia válida ({self.code}); {tool}"


@dataclass(frozen=True)
class Rendered:
    text: str
    findings: tuple[Finding, ...]
    problems: tuple[Problem, ...]
    wants_handoff: bool
    expires_at: datetime | None  # cuándo caduca la primera evidencia de precio o stock citada
    inserted: tuple[tuple[int, int], ...] = ()  # lo que puso el servidor: valores y marcas

    @property
    def ok(self) -> bool:
        return not self.problems

    def feedback(self) -> str:
        """El motivo estructurado del reintento (ADR-005 Parte D)."""
        return "\n".join(problem.text() for problem in self.problems)


@dataclass(frozen=True)
class _Cited:
    value: str
    product: str | None  # el de una claim de nombre
    variant: str | None  # la variante nombrada, o la de la evidencia de precio o stock
    subjects: frozenset[str]
    expires: datetime | None
    row: tuple[str, int] | None = None  # (evidence_ref, índice) de una fila de `prices`
    promotion: str | None = None  # el `promotion_id` de esa fila o de unas condiciones
    price_list: str | None = None  # el nombre de la lista de esa fila, si lo tiene


def evidence_ttl() -> timedelta:
    """`AI_EVIDENCE_TTL`; `ImproperlyConfigured` si no es un entero de 1 a `MAX_TTL`."""
    ttl = settings.AI_EVIDENCE_TTL
    if type(ttl) is not int or not 1 <= ttl <= MAX_TTL:
        raise ImproperlyConfigured("AI_EVIDENCE_TTL")
    return timedelta(seconds=ttl)


def parse(raw: object) -> tuple[str, tuple[Claim, ...], bool]:
    """La respuesta como la exige el esquema (también en el servidor), o `Malformed`."""
    if type(raw) is not dict or set(raw) != set(TOP):
        raise Malformed
    template, claims, handoff = raw["reply_template"], raw["claims"], raw["wants_handoff"]
    if type(template) is not str or type(claims) is not list or type(handoff) is not bool:
        raise Malformed
    if max(len(claims), len(PLACEHOLDER.findall(template))) > MAX_CLAIMS:
        raise Malformed
    # F8-08g: vacía, con controles o con sustitutos sueltos, la base no la guardaría
    if len(template) > MAX_TEMPLATE or not template.strip() or UNSTORABLE.search(template):
        raise Malformed
    for claim in claims:
        if type(claim) is not dict or set(claim) != set(FIELDS):
            raise Malformed
        if any(type(v) is not str or len(v) > MAX_FIELD for v in claim.values()) or (
            claim["kind"] not in KINDS  # ya es un `str`: se puede buscar
        ):
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
        money, tax = _money(row.get("currency"), leaf), row.get("tax_included")
        if money is None or type(tax) is not bool:
            return None
        money = money if tax else f"{money} + IGV"
        return f"{REGULAR} {money}" if kind == "REGULAR_PRICE" else money
    if kind == "STOCK":
        return BANDS.get(leaf)
    return leaf


def _cite(claim: Claim, found: Evidence | None, now: datetime, ttl: timedelta) -> _Cited | str:
    """El valor de la evidencia para `claim`, o el código del problema."""
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
    expires = found.as_of + ttl if found.kind != "CATALOG" else None  # el catálogo no caduca
    if found.as_of > now or (expires is not None and now > expires):  # ni del futuro
        return "EVIDENCE_STALE"
    row, leaf = nodes[-2], nodes[-1]
    if claim.kind in PROMOTED and row.get("promotion") is None:
        return "NO_PROMOTION"
    if claim.kind == "STOCK" and leaf == NOT_TRACKED:
        return "NOT_TRACKED"
    if claim.kind == "PROMOTION_TERMS" and type(leaf) is str and leaf.endswith(CUT.lstrip()):
        return "TRUNCATED"  # recortadas por `get_price` (a cero: solo «[…]»): nunca como enteras
    value = _value(claim.kind, row, leaf) if type(leaf) is str and leaf.strip() else None
    if value is None:
        return "VALUE_MISSING"
    if claim.kind == "PRODUCT_NAME":
        product = row.get("product_id", found.data.get("product_id"))
        if type(product) is not str:  # sin sujeto no se ancla nada: falla la claim
            return "VALUE_MISSING"
        return _Cited(value, product, row.get("variant_id"), frozenset(), None)
    at = (claim.evidence_ref, int(claim.path.split(".")[1])) if claim.kind in ROWS else None
    subjects, name = frozenset(map(str, found.subjects)), row.get("price_list")
    name = name.strip() if type(name) is str and name.strip() else None
    return _Cited(value, None, found.data.get("variant_id"), subjects, expires, at,
                  row.get("promotion_id"), name)  # fmt: skip


def _groups(kinds: list[str], cited: list[_Cited | str]) -> list[tuple[Any, list[int]]]:
    """`[((producto, variante), índices sin nombre)]`: cada nombre abre un grupo, salvo otro del
    mismo producto sin variante; lo anterior al primer nombre es de su grupo."""
    groups: list[tuple[Any, list[int]]] = [((None, None), [])]
    named = False
    for at, kind in enumerate(kinds):
        if kind != "PRODUCT_NAME":
            groups[-1][1].append(at)
            continue
        found = cited[at]
        head = (found.product, found.variant) if isinstance(found, _Cited) else (None, None)
        if not named:
            groups[-1] = (head, groups[-1][1])
        elif head[1] is not None or head[0] is None or head[0] != groups[-1][0][0]:
            groups.append((head, []))
        named = True
    return groups


def _near(at: int, among: list[int]) -> int | None:
    """El índice de `among` más cercano a `at`: el anterior antes que el siguiente."""
    near = [p for p in among if p < at][-1:] or [p for p in among if p > at][:1]
    return near[0] if near else None


def _coherent(kinds: list[str], cited: list[_Cited | str]) -> None:
    """Cambia por `SUBJECT_MISMATCH` lo que no es del sujeto de su grupo (ADR-005 Parte D), y por
    `PRICE_TWICE` el precio de una variante ya citada que viene de otra llamada a `get_price`:
    «antes X, ahora Y» tras un cambio de precio sería un descuento que no existe. Varias listas de
    una misma llamada sí (D-F8-06); cómo se redactan lo miran los detectores (F8-08f)."""

    def valid(at: int | None, *wanted: str) -> _Cited | None:
        if at is None:  # sin fila con la que emparejar
            return None
        found = cited[at]
        return found if isinstance(found, _Cited) and kinds[at] in wanted else None

    current: dict[str | None, str | None] = {}  # variante → la llamada de su primer precio
    for (product, variant), members in _groups(kinds, cited):
        for at in members:
            if (found := valid(at, *KINDS)) is None:
                continue
            if (product is not None and product not in found.subjects) or (
                variant is not None and found.variant != variant
            ):
                cited[at] = "SUBJECT_MISMATCH"
            else:
                variant = found.variant
        for at in members:  # en todo el texto, no solo en el grupo: un nombre no lo reinicia
            if (found := valid(at, "PRICE")) is None:
                continue
            call = found.row[0] if found.row else None  # el `tool_use_id` de su `get_price`
            if current.setdefault(found.variant, call) != call:
                cited[at] = "PRICE_TWICE"
        prices = [at for at in members if valid(at, "PRICE")]
        for at in members:
            if (found := valid(at, *PROMOTED)) is None:
                continue
            pair = valid(_near(at, prices), "PRICE")
            alone = pair is None and kinds[at] == "REGULAR_PRICE"  # un «antes» sin «ahora»
            if alone or (pair is not None and pair.row != found.row):
                cited[at] = "SUBJECT_MISMATCH"
        rows = [at for at in members if valid(at, *ROWS)]
        for at in members:  # las condiciones, de la fila citada más cercana y de su promoción
            if (found := valid(at, "PROMOTION_TERMS")) is None:
                continue
            pair = valid(_near(at, rows), *ROWS)
            if pair is None or found.promotion is None or pair.promotion != found.promotion:
                cited[at] = "SUBJECT_MISMATCH"
    quoted = [(at, price) for at in range(len(cited)) if (price := valid(at, "PRICE"))]
    lists: dict[str | None, set[tuple[str, int] | None]] = {}  # variante → filas de sus precios
    for _, price in quoted:
        lists.setdefault(price.variant, set()).add(price.row)
    for at, price in quoted:  # varias listas: cada precio con su nombre, nunca el del modelo
        if len(lists[price.variant]) > 1 and (label := price.price_list):
            cited[at] = replace(price, value=f"{label}: {price.value}")
        elif len(lists[price.variant]) > 1:  # sin nombre no se distinguen: no se insertan
            cited[at] = "VALUE_MISSING"


def render(raw: object, evidence: Mapping[str, Evidence | None], now: datetime) -> Rendered:
    """El texto con los valores copiados de `evidence`, o `Malformed`. `now` con zona horaria."""
    ttl = evidence_ttl()
    if now.utcoffset() is None:
        raise ValueError("render: `now` sin zona horaria")
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
    matches = list(PLACEHOLDER.finditer(template))
    placed = [by_id.get(match.group(1)) for match in matches]
    cited: list[_Cited | str] = [  # primero todas; después, los grupos
        "CLAIM_MISSING" if c is None else _cite(c, evidence.get(c.evidence_ref), now, ttl)
        for c in placed
    ]
    _coherent([c.kind if c else "" for c in placed], cited)
    out: list[str] = []
    findings: list[Finding] = []
    inserted: list[tuple[int, int]] = []
    expires: datetime | None = None
    pos = 0
    for match, claim, found in zip(matches, placed, cited, strict=True):
        out.append(template[pos : match.start()])
        pos, start = match.end(), sum(map(len, out))
        piece = found.value if isinstance(found, _Cited) else VERIFY_MARK
        out.append(piece)
        inserted.append((start, start + len(piece)))
        if isinstance(found, _Cited):
            if found.expires is not None:
                expires = found.expires if expires is None else min(expires, found.expires)
            continue
        findings.append(Finding(FINDING.get(found, found), start, start + len(piece)))
        if claim is None:  # el id solo si tiene la forma `c<n>`
            key = match.group(1)
            problems.append(Problem(found, key if CLAIM_ID.fullmatch(key) else None))
        else:
            problems.append(Problem(found, claim.id, KINDS[claim.kind].tools[0].name))
    out.append(template[pos:])
    used = {claim.id for claim in placed if claim is not None}
    problems += [Problem("CLAIM_UNUSED", key) for key in by_id if key not in used]
    text = "".join(out)
    if len(text) > BODY_MAX:  # nunca se corta en silencio: no sale más que la marca
        text, expires = VERIFY_MARK, None
        findings, inserted = [Finding("TOO_LONG", 0, len(VERIFY_MARK))], [(0, len(VERIFY_MARK))]
        problems.append(Problem("TOO_LONG"))
    unique = tuple(dict.fromkeys(problems))
    return Rendered(text, tuple(findings), unique, handoff, expires, tuple(inserted))

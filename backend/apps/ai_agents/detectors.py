"""El Output Guard del copiloto, parte II (F8-08f; ADR-034 §7, §8, §9; ADR-005 Parte D).

Detectores deterministas sobre el texto **del modelo**: los tramos de `Rendered.text` fuera de
`inserted` (lo que puso el servidor: un precio, una promoción con su porcentaje o sus fechas no se
marca). Ningún número se acepta por ser igual a un valor de la evidencia: el modelo escribiría el
precio de otro producto. Comparan en una clave por carácter (NFKD sin marcas `Mn` ni formato `Cf`,
`casefold`, todo número Unicode como `0`, todo símbolo de moneda como `$` y las letras y llaves
parecidas de `core.confusables` por su prototipo) y cortan el texto original por posiciones.
Marcan de más a propósito: un falso positivo cuesta una marca; un falso negativo, una cifra, un
término o un enlace sin evidencia.

- `substitute` (sugerencia): cada tramo pasa a `VERIFY_MARK`; el hallazgo da tipo y posición de su
  marca, nunca el tramo. `mark` (resumen, §7): los mismos tramos, con su posición, sin sustituir.
- `handoff` (§8): palabras clave en los últimos mensajes del cliente **o** `wants_handoff`, más una
  señal de enfado. Con traspaso, `suggestion` devuelve el texto fijo `AI_HANDOFF_REPLY`, nunca el
  del modelo. No asigna nada.

Posiciones en code points (F8-09 las convierte para JavaScript). Puro: sin base de datos ni logs."""

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, replace

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from apps.ai_agents.guard import Finding, Problem, Rendered
from apps.conversations.models import BODY_MAX
from core.confusables import PROTOTYPES
from core.text import UNSEEN, VERIFY_MARK, has_verify_mark

MAX_FINDINGS = 100  # el CHECK de `ai_suggestions.findings`
_LOOK = {char: shape for shape, chars in PROTOTYPES.items() for char in chars}
_DROP = ("Mn", "Me", "Cf", "Cn", "Co", "Cs")  # lo que no se ve o no está asignado
TLD = (
    r"(?:com|net|org|info|biz|pe|io|co|me|ly|app|shop|store|online|site|link|xyz|lat|es|ar|mx"
    r"|cl|us|uk|de|br|gg|tk|ru|cn|top|club|page|dev|ai|tv|cc|to|be|gl|gd|vip|live|in|ws)\b"
)
DOT = r"[.。·]"
HIDDEN_DOT = rf"[(\[{{<]\s*(?:{DOT}|punto|dot)\s*[)\]}}>]"
WORD = r"(?<![\w-])[\w-]++"  # desde su inicio y posesivo: lineal con el texto
# El tipo de un tramo, de mayor a menor prioridad: dos tramos unidos se quedan con el primero.
PATTERNS: dict[str, str] = {
    "LINK": "|".join([
        r"\b[a-z][a-z0-9+.-]*+:\s*/\s*/\S*", r"\bh(?:tt|xx)ps?\b", r"\bwww\b",
        # tienda.pe, bit.ly/x, wa.me/51
        rf"(?<!{DOT}){WORD}(?:{DOT}(?![a-z]{{2}}){WORD})*+{DOT}[a-z]{{2}}[\w-]*+(?:[/?#]\S*)?",
        rf"{WORD}(?:\s*{HIDDEN_DOT}\s*{WORD})+",  # tienda[.]pe, tienda(punto)pe
        rf"(?:{WORD}\s*+)?(?:{DOT}|\bpunto\b|\bdot\b)\s*{TLD}",  # tienda . pe, .com tras un valor
        r"(?<![\w.+-])[\w.+-]*+@[\w.-]+|\barroba\b",  # correos y cuentas
    ]),
    # Un placeholder que no se renderizó (`｛｛c1｝｝`), llaves sueltas (`{{{c1}}}`) y la marca
    "MARKUP": r"\{++[^{}\s]{0,64}\}++|[{}]|[^\w\s]\s*verificar\s*[^\w\s]",
    "DISCOUNT": "|".join([
        r"[%‰]", r"\bpor\s*ciento\b", r"\bd?s?cto\w*", r"\bdesc\.", r"\bdescuent\w*",
        r"\brebaj\w*", r"\bofert\w*", r"\bpromo\w*", r"\bliquidaci\w*", r"\bremate\w*",
        r"\bantes\b", r"\bahora\b", r"\bahorr\w*", r"\b0+\s*x\s*0+\b", r"\b(?:black|cyber)\b",
    ]),
    "AVAILABILITY": (r"\bstock\w*|\bagot\w*|\bqued(?:a|an|aria|arian|o)\b|\bunidad(?:es)?\b"
                     r"|\bdisponib\w*|\bexistencias?\b|\brepo(?:n|sici)\w*|\binventario\b"),
    "TERMS": "|".join([
        r"\bcuotas?\b", r"\bcontra\s*-?\s*entrega\b", r"\bgaranti\w*", r"\bdevoluci\w*",
        r"\bdevolv\w*", r"\bdevuelv\w*", r"\bcambi(?:o|os|ar|amos)\b", r"\breembols\w*",
        r"\bgratis\b", r"\bgratuit\w*", r"\bsin\s+costo\b", r"\brecargo\w*",
        r"\bpag(?:o|os|a|as|ar|amos|an)\b", r"\b(?:yape|plin|efectivo|igv)\b", r"\btarjeta\w*",
        r"\btransferenc\w*", r"\bdeposit\w*", r"\bcredito\w*", r"\bfinanci\w*",
        r"\binteres(?:es)?\b", r"\bcomision\w*", r"\bimpuesto\w*", r"\bfactura\w*",
        r"\brecojo\b", r"\brecog\w*", r"\bretir(?:o|ar)\b",
    ]),
    "DELIVERY": "|".join([
        r"\bllega\w*", r"\bllegu\w*", r"\bentreg\w*", r"\benvi[oa]\w*", r"\bdespach\w*",
        r"\bdelivery\b", r"\bcourier\b", r"\bexpress\b", r"\bmotorizad\w*", r"\bmanana\b",
        r"\bhoy\b", r"\b(?:lunes|martes|miercoles|jueves|viernes|sabados?|domingos?)\b",
        r"\bsemanas?\b", r"\bhoras?\b", r"(?<!buenos )\bdias?\b", r"\bhabiles\b", r"\bmes(?:es)?\b",
        r"\bplazos?\b", r"\bdemor\w*", r"\btarda\w*", r"\b(?:esta|la)\s+tarde\b", r"\binmediat\w*",
    ]),
    "FIGURE": "|".join([  # importes, monedas y números con forma de precio o de cantidad
        r"0++(?:[.,' ]?+0++)*+", r"\$", r"\bs\s*[/⁄∕⧸]\.?",
        r"\b(?:pen|usd|eur|soles?|sol|dolar\w*|centimos?|lucas?)\b",
        r"\b(?:dos|tres|cuatro|cinco|seis|siete|ocho|nueve|diez|once|doce|trece|catorce|quince"
        r"|veinte|treinta|cuarenta|cincuenta|sesenta|setenta|ochenta|noventa|cien|ciento|mil"
        r"|miles|docenas?)\b|\bdieci\w+|\bveinti\w+|\b\w+cient[oa]s\b|\bmillon\w*",
    ]),
}  # fmt: skip
_COMPILED = {kind: re.compile(pattern) for kind, pattern in PATTERNS.items()}
_RANK = {kind: rank for rank, kind in enumerate(PATTERNS)}
JOIN = re.compile(r"[\s.,:;/\u3002\u00b7-]*")  # entre dos tramos (en la clave): una sola marca

# §8: quien pide una persona, y la señal de enfado (palabras, mayúsculas sostenidas, «!!!»)
PERSON = re.compile(
    r"\basesor\w*|\bpersonas?\b|\bhuman[oa]s?\b|\bhablar\s+con\s+alguien\b|\balguien\s+real\b"
    r"|\bencargad[oa]s?\b|\bsupervisor\w*|\bgerente\w*|\badministrador\w*|\boperador\w*"
    r"|\bvendedor\w*|\b(?:ro)?bots?\b|\bmaquina\b|\batencion\s+al\s+cliente\b"
    r"|\bllam(?:en|enme|ame|eme)\b"
)
UPSET = re.compile(
    r"\bestaf\w*|\breclam\w*|\bdenunci\w*|\bindecopi\b|\bpesim\w*|\bhorribl\w*|\bverguenza\b"
    r"|\bsinverguenz\w*|\bladron\w*|\brater[oa]s?\b|\bmentiros\w*|\bbasura\b|\bporqueria\b|\bmierda\b"
    r"|\bcarajo\b|\bidiota\w*|\bimbecil\w*|\binutil\w*|\bincompetent\w*|\bhart[oa]s?\b"
    r"|\bfraude\w*|\brob(?:o|aron|ando)\b|\babus(?:o|ivo)s?\b|\bmolest[oa]s?\b|\benojad\w*"
    r"|\bfurios\w*|\bindignad\w*|\bno\s+(?:me\s+)?respond\w*|\bnadie\s+(?:me\s+)?respond\w*"
    r"|(?:[!¡]\s*){3,}"
)
SHOUT = 10  # letras en palabras enteras en mayúsculas en un mensaje: «NO ME RESPONDEN»
CAPS = re.compile(r"\b[^\W\d_]{2,}\b")
REASONS = ("ASKED_FOR_PERSON", "UPSET", "MODEL")


@dataclass(frozen=True)
class Handoff:
    requested: bool
    reason: str | None = None  # uno de `REASONS`


def _fold(text: str) -> tuple[str, list[int]]:
    """La clave con la que se compara y, para cada carácter suyo, su índice en `text`."""
    out: list[str] = []
    origin: list[int] = []
    for at, char in enumerate(text):
        kind = unicodedata.category(char)
        if kind[0] == "N":
            part = "0"
        elif kind == "Sc":
            part = "$"
        elif char in UNSEEN or kind in _DROP:
            part = ""
        else:
            plain = unicodedata.normalize("NFKD", _LOOK.get(char, char))
            part = "".join(c for c in plain if unicodedata.category(c) not in _DROP).casefold()
        out.append(part)
        origin += [at] * len(part)
    return "".join(out), origin


def _spans(text: str) -> list[tuple[str, int, int]]:
    """Los tramos de `text` con algo que detectar, `(tipo, inicio, fin)`, ordenados y unidos: los
    que se tocan o solo los separan blancos y signos (`S/ 30`) son una sola marca."""
    key, origin = _fold(text)
    found: list[tuple[str, int, int]] = []
    for kind, pattern in _COMPILED.items():
        for match in pattern.finditer(key):
            if match.end() > match.start():  # lo quitado que sigue (una tilde suelta) va con él
                end = origin[match.end()] if match.end() < len(origin) else len(text)
                found.append((kind, origin[match.start()], end))
    merged: list[tuple[str, int, int]] = []
    for kind, start, end in sorted(found, key=lambda span: (span[1], -span[2])):
        if merged and JOIN.fullmatch(_fold(text[merged[-1][2] : max(start, merged[-1][2])])[0]):
            last, first, stop = merged[-1]
            best = last if _RANK[last] <= _RANK[kind] else kind
            merged[-1] = (best, first, max(stop, end))
        else:
            merged.append((kind, start, end))
    # La red: si queda la marca entre dos tramos (letras que la clave no pliega), el trozo entero
    pos, kept = 0, []
    for span in [*merged, ("", len(text), len(text))]:
        if has_verify_mark(text[pos : span[1]]):
            kept.append(("MARKUP", pos, span[1]))
        if span[0]:
            kept.append(span)
        pos = span[2]
    return kept


def mark(text: str) -> tuple[Finding, ...]:
    """El resumen (§7): cada tramo marcado «sin verificar», con su posición, sin sustituirlo.
    Pasados `MAX_FINDINGS`, el último cubre el resto."""
    found = [Finding(kind, start, end) for kind, start, end in _spans(text)]
    if len(found) > MAX_FINDINGS:
        found[MAX_FINDINGS - 1 :] = [replace(found[MAX_FINDINGS - 1], end=found[-1].end)]
    return tuple(found)


def substitute(rendered: Rendered) -> Rendered:
    """La sugerencia: cada tramo del texto del modelo (fuera de `inserted`) por `VERIFY_MARK`, con
    un hallazgo de tipo y posición; los hallazgos y tramos del servidor se mueven con el texto. Si
    el resultado pasa de `BODY_MAX` o de `MAX_FINDINGS`, queda solo la marca (`TOO_LONG`)."""
    text, out = rendered.text, ""
    by_span = {(f.start, f.end): f for f in rendered.findings}
    findings: list[Finding] = []
    inserted: list[tuple[int, int]] = []
    pos = 0
    for start, end in [*rendered.inserted, (len(text), len(text))]:
        segment, cursor = text[pos:start], 0
        for kind, first, stop in _spans(segment):
            out += segment[cursor:first]
            findings.append(Finding(kind, len(out), len(out) + len(VERIFY_MARK)))
            inserted.append((len(out), len(out) + len(VERIFY_MARK)))
            out, cursor = out + VERIFY_MARK, stop
        out += segment[cursor:]
        if end > start:
            if (old := by_span.get((start, end))) is not None:
                findings.append(Finding(old.type, len(out), len(out) + end - start))
            inserted.append((len(out), len(out) + end - start))
            out += text[start:end]
        pos = end
    if len(out) > BODY_MAX or len(findings) > MAX_FINDINGS:
        problems = tuple(dict.fromkeys((*rendered.problems, Problem("TOO_LONG"))))
        whole = (0, len(VERIFY_MARK))
        return replace(rendered, text=VERIFY_MARK, findings=(Finding("TOO_LONG", *whole),),
                       problems=problems, expires_at=None, inserted=(whole,))  # fmt: skip
    return replace(rendered, text=out, findings=tuple(findings), inserted=tuple(inserted))


def _shouts(text: str) -> bool:
    return sum(len(word) for word in CAPS.findall(text) if word.isupper()) >= SHOUT


def handoff(customer: Sequence[str], wants_handoff: bool) -> Handoff:
    """§8: ¿pide el cliente una persona en sus últimos mensajes (`customer`, los que elige quien
    llama) **o** la pide el modelo, o está molesto? El motivo, en ese orden de prioridad."""
    keys = [_fold(text)[0] for text in customer]
    if any(PERSON.search(key) for key in keys):
        return Handoff(True, "ASKED_FOR_PERSON")
    if any(UPSET.search(key) for key in keys) or any(map(_shouts, customer)):
        return Handoff(True, "UPSET")
    if wants_handoff is True:
        return Handoff(True, "MODEL")
    return Handoff(False)


def handoff_reply() -> str:
    """`AI_HANDOFF_REPLY`; `ImproperlyConfigured` si no es un texto de 1 a `BODY_MAX` caracteres
    sin nada que detectar (ni cifras, ni plazos, ni enlaces, ni la marca)."""
    reply = settings.AI_HANDOFF_REPLY
    if type(reply) is not str or not reply.strip() or len(reply) > BODY_MAX or _spans(reply):
        raise ImproperlyConfigured("AI_HANDOFF_REPLY")
    return reply


def suggestion(rendered: Rendered, customer: Sequence[str]) -> tuple[Rendered, Handoff]:
    """La sugerencia que se guarda: con traspaso, el texto fijo (sin hallazgos ni problemas: no hay
    nada que reintentar); si no, `substitute`."""
    found = handoff(customer, rendered.wants_handoff)
    if not found.requested:
        return substitute(rendered), found
    text = handoff_reply()
    fixed = Rendered(text, (), (), rendered.wants_handoff, None, ((0, len(text)),))
    return fixed, found

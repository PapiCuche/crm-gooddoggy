"""El Output Guard del copiloto, parte II (F8-08f; ADR-034 §7, §8, §9; ADR-005 Parte D).

Detectores deterministas sobre el texto **del modelo**: los tramos de `Rendered.text` fuera de
`inserted` (lo que puso el servidor: un precio, una promoción con su porcentaje o sus fechas no se
marca). Ningún número se acepta por ser igual a un valor de la evidencia: el modelo escribiría el
precio de otro producto. Buscan en el texto entero (un `.pet` del modelo pegado a un nombre del
servidor es un enlace) y marcan solo lo que escribió el modelo; lo que el modelo escribe entre dos
precios del servidor se marca siempre (dos precios actuales como un descuento). Comparan en una
clave por carácter (NFKD sin marcas `Mn` ni formato `Cf`, `casefold`, todo número Unicode como `#`,
que no es letra, todo símbolo de moneda como `$` y las letras y llaves parecidas de
`core.confusables` por su prototipo) y cortan el texto original por posiciones.
Marcan de más a propósito: un falso positivo cuesta una marca; un falso negativo, una cifra, un
término o un enlace sin evidencia.

- `substitute` (sugerencia): cada tramo pasa a `VERIFY_MARK`; el hallazgo da tipo y posición de su
  marca, nunca el tramo. `mark` (resumen, §7): los mismos tramos, con su posición, sin sustituir.
- `handoff` (§8): palabras clave en los últimos mensajes del cliente **o** `wants_handoff`, más una
  señal de enfado. Con traspaso, `suggestion` devuelve el texto de traspaso (el de la organización,
  F8-08l; si no, el inicial `AI_HANDOFF_REPLY`), nunca el del modelo. No asigna nada.
- `reply_problems` (F8-08l): por qué un texto de traspaso no vale, al guardarlo y al usarlo.

Posiciones en code points (F8-09 las convierte para JavaScript). Puro: sin base de datos ni logs."""

import re
import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from apps.ai_agents.guard import Finding, Problem, Rendered
from apps.ai_agents.models import HANDOFF_MAX
from apps.conversations.models import BODY_MAX
from apps.conversations.text import clean_body
from core.confusables import PROTOTYPES
from core.text import UNSEEN, VERIFY_MARK, has_verify_mark

MAX_FINDINGS = 100  # el CHECK de `ai_suggestions.findings`
_LOOK = {char: shape for shape, chars in PROTOTYPES.items() for char in chars}
_DROP = ("Mn", "Me", "Cf", "Cn", "Co", "Cs")  # lo que no se ve o no está asignado
# Tras un punto con espacio detrás (el de una frase), solo estos dominios; sin las palabras
# españolas de dos letras («Hola. Es…», «… Me alegra»), que serían una frase.
TLD = (
    r"(?:com|net|org|info|biz|pe|io|co|ly|app|shop|store|tienda|online|site|web|website|link|xyz"
    r"|lat|mx|cl|us|uk|br|gg|tk|ru|cn|top|club|page|dev|ai|tv|cc|be|gl|gd|vip|live|ws|pet|pets"
    r"|dog|vet|cat|blog|click|fun|bio|pro|eu|fr|nl|ca|au|jp|ec|py|bo)\b"
)
DOT = "[.。·•∙⋅・‧]"
SPACED = "[.。∙⋅・‧]"  # con blancos alrededor, sin `·` ni `•`: son viñetas de una lista
OPEN = "[(\\[{<「『【〔〖〈《⟨⟦❨❪❬❴]"
CLOSE = "[)\\]}>」』】〕〗〉》⟩⟧❩❫❭❵]"
HIDDEN_DOT = rf"{OPEN}\s*(?:{DOT}|punto|dot)\s*{CLOSE}"
WORD = r"(?<![\w#-])[\w#-]++"  # desde su inicio y posesivo: lineal con el texto
NUM = "#"  # cada número de la clave: no es letra, así que `\b` vale tras él («5disponibles»)
# El tipo de un tramo, de mayor a menor prioridad: dos tramos unidos se quedan con el primero.
PATTERNS: dict[str, str] = {
    "LINK": "|".join([
        r"\b[a-z][a-z0-9+.-]*+:\s*/\s*/\S*", r"\bh(?:tt|xx)ps?\b", r"\bwww\b",
        # tienda.pe, rojo.pet, bit.ly/x, wa.me/51: sin espacios, cualquier dominio
        rf"(?<!{DOT}){WORD}(?:{DOT}(?![a-z]{{2}}){WORD})*+{DOT}[a-z]{{2}}[\w#-]*+(?:[/?]\S*)?",
        rf"(?:{WORD}\s*+)?(?:{HIDDEN_DOT}\s*+{WORD})+",  # tienda[.]pe, tienda「.」pe
        rf"{WORD}\s+{SPACED}\s*[a-z]{{2,63}}\b",  # tienda . pet, tienda .dog
        rf"(?:{WORD}\s*+)?(?:{SPACED}|\bpunto\b|\bdot\b)\s*{TLD}",  # tienda. pe, tienda punto com
        r"(?<![\w.+-])[\w.+-]*+@[\w.-]+|\barroba\b",  # correos y cuentas
    ]),
    # Un placeholder que no se renderizó (`｛｛c1｝｝`), llaves sueltas (`{{{c1}}}`) y la marca
    "MARKUP": r"\{++[^{}\s]{0,64}\}++|[{}]|[^\w\s]\s*verificar\s*[^\w\s]",
    "DISCOUNT": "|".join([
        r"[%‰~]", r"-+>|=+>|[←-⇿➔-➿⟰-⟿⤀-⥿]",
        r"\bpor\s*ciento\b", r"\bd?s?cto\w*", r"\bdesc(?:tos?)?\b", r"\bdto\w*", r"\bdescuent\w*",
        r"\brebaj\w*", r"\bofert\w*", r"\bpromo\w*", r"\bliquidaci\w*", r"\bremate\w*",
        r"\bantes\b", r"\bahora\b", r"\bahorr\w*", r"#++\s*x\s*#++", r"\b(?:black|cyber|off)\b",
        r"\bcostab\w*", r"\b(?:era|estaba)\b", r"\bbaj\w*", r"\bpaso\s+de\b", r"\bnormal\w*",
        r"\bregular\w*", r"\bfinal\w*", r"\ben\s+(?:vez|lugar)\s+de\b", r"\bpara\s+ti\b",
        r"\bprecio\s+(?:de\s+lista|especial)\b", r"\bmas\s+barat\w*", r"\bmitad\b",
        r"\bregal\w*",
    ]),
    "AVAILABILITY": "|".join([
        r"\bstock\w*", r"\bagot\w*", r"\bqued(?:a|an|aria|arian|o)\b", r"\bunidad(?:es)?\b",
        r"\bdisponib\w*", r"\bexistencias?\b", r"\brepo(?:n|sici)\w*", r"\binventario\b",
        r"\b(?:se\s+)?acab(?:o|a|aron|amos)\b", r"\b(?:si|no)\s+(?:hay|tenemos|contamos)\b",
    ]),
    "TERMS": "|".join([
        r"\bcuotas?\b", r"\bcontra\s*-?\s*entrega\b", r"\bgaranti\w*", r"\bdevoluci\w*",
        r"\bdevolv\w*", r"\bdevuelv\w*", r"\bcambi(?:o|os|ar|amos)\b", r"\breembols\w*",
        r"\bgratis\b", r"\bgratuit\w*", r"\bsin\s+cargo\b", r"\bcostos?\b", r"\brecargo\w*",
        r"\bpag(?:o|os|a|as|ar|amos|an)\b", r"\b(?:yape|plin|efectivo|igv)\b", r"\btarjeta\w*",
        r"\btransferenc\w*", r"\bdeposit\w*", r"\bcredito\w*", r"\bfinanci\w*",
        r"\binteres(?:es)?\b", r"\bcomision\w*", r"\bimpuesto\w*", r"\bfactura\w*",
        r"\brecojo\b", r"\brecog\w*", r"\bretir(?:o|ar)\b", r"\bcancel\w*", r"\bal\s+recibir\b",
        r"\b(?:visa|mastercard|amex|diners|aceptamos)\b",
    ]),
    "DELIVERY": "|".join([
        r"\bllega\w*", r"\bllegu\w*", r"\bentreg\w*", r"\benvi[oa]\w*", r"\bdespach\w*",
        r"\bdelivery\b", r"\bcourier\b", r"\bexpress\b", r"\bmotorizad\w*", r"\bmanana\b",
        r"\bhoy\b", r"\b(?:lunes|martes|miercoles|jueves|viernes|sabados?|domingos?)\b",
        r"\bsemanas?\b", r"\bhoras?\b", r"(?<!buenos )\bdias?\b", r"\bhabiles\b", r"\bmes(?:es)?\b",
        r"\bplazos?\b", r"\bdemor\w*", r"\btarda\w*", r"\b(?:esta|la)\s+tarde\b", r"\binmediat\w*",
        r"\bal\s+toque\b", r"\bahorit\w*", r"\bmand(?:o|a|an|amos|aremos|ariamos)\b",
        r"\ben\s+camino\b", r"\bsal(?:e|io|ieron|dra|dria)\b", r"\bconfirmad\w*", r"\blist[oa]s?\b",
    ]),
    "FIGURE": "|".join([  # importes, monedas y números con forma de precio o de cantidad
        rf"{NUM}++(?:[.,' ]?+{NUM}++)*+", r"\$", r"\bs\s*[/⁄∕⧸]\.?",
        r"\b(?:pen|usd|eur|soles?|sol|dolar\w*|centimos?|lucas?)\b",
        r"\b(?:cero|dos|tres|cuatro|cinco|seis|siete|ocho|nueve|diez|once|doce|trece|catorce"
        r"|quince|cien|ciento|mil|miles|millar\w*|docenas?)\b|\bdieci\w+|\bveint[ei]\w*"
        r"|\b(?:treint|cuarent|cincuent|sesent|setent|ochent|novent)[ai]\w*|\b\w*cient[oa]s\b"
        r"|\bquinient[oa]s\b|\bmillon\w*",
    ]),
}  # fmt: skip
_COMPILED = {kind: re.compile(pattern) for kind, pattern in PATTERNS.items()}
_RANK = {kind: rank for rank, kind in enumerate(PATTERNS)}
JOIN = re.compile(r"[\s.,:;/。·-]*")  # entre dos tramos (en la clave): una sola marca
# Un precio que copió el servidor, con su etiqueta si la lleva («precio regular», «Lista: …»);
# entre dos, lo que escribe el modelo es un descuento salvo un separador de lista (en la clave)
PRICE = re.compile(
    r"(?:precio regular |[^\n]{1,80}?: )?(?:S/|US\$|[A-Z]{3}) [0-9][0-9,]*(?:\.[0-9]+)?(?: \+ IGV)?"
)
SEPARATOR = re.compile(r"[\s,;/|\u00b7\u2022-]*(?:\b[yoeu]\b[\s,;/|\u00b7\u2022-]*)?")

# §8: quien pide una persona, y la señal de enfado (frases sin doble sentido y mayúsculas)
PERSON = re.compile(
    r"\basesor\w*|\b(?:con|a)\s+(?:una?\s+)?(?:persona|human[oa])s?\b"  # «persona», solo pedida
    r"|\b(?:persona|human[oa])\s+(?:real|de\s+verdad)\b|\bquiero\s+(?:una?\s+)?(?:persona|human[oa])\b"
    r"|\balguien\s+(?:real|que\s+me\s+atienda)\b"
    r"|\b(?:hablar|pasa\w*|comunica\w*|contacta\w*)\s+con\s+alguien\b|\batienda\s+alguien\b"
    r"|\bencargad[oa]s?\b|\bsupervisor\w*|\bgerente\w*|\badministrador\w*|\boperador\w*"
    r"|\bvendedor\w*|\b(?:con|a)\s+(?:un|una|el|la|tu|su)\s+(?:agente|jef[ea]|ejecutiv[oa])s?\b"
    r"|\b(?:ro)?bots?\b"
    r"|\b(?:con|eres|es)\s+una\s+maquina\b|\batencion\s+al\s+cliente\b|\bllam(?:en|enme|ame|eme)\b"
)
UPSET = re.compile(
    r"\bestaf\w*|\breclamo\w*|\breclamaci\w*|\bdenunci\w*|\bindecopi\b|\bpesim\w*|\bhorribl\w*"
    r"|\bque\s+verguenza\b|\bsinverguenz\w*|\bladron\w*|\brater[oa]s\b|\bmentiros\w*|\bmierda\b"
    r"|\buna\s+(?:basura|porqueria|burla)\b|\bcarajo\b|\bidiota\w*|\bimbecil\w*|\binutiles\b"
    r"|\bincompetent\w*|\bestoy\s+(?:muy\s+)?(?:hart|molest|enojad|indignad|furios)\w*"
    r"|\bcansad[oa]s?\s+de\s+(?:esperar|ustedes)\b"
    r"|\bfraude\w*|\bme\s+robaron\b|\bes\s+un\s+(?:robo|abuso)\b|\bno\s+me\s+respond\w*"
    r"|\bnadie\s+(?:me\s+)?respond\w*"
)
# Gritar: «!!!» con casi todo en mayúsculas («QUIERO MI PEDIDO YA!!!»). Solo mayúsculas no basta
# (mucha gente escribe así: «TIENEN STOCK?»), ni «!!!» solo («Hola!!!»).
CAPS_RATIO, CAPS_BANG = 0.8, 6
# Gritar de alegría no es enfado («GRACIAS!!!», «ME ENCANTÓ!!!»)
HAPPY = re.compile(
    r"\bgracias\b|\blind[oa]s?\b|\bperfect\w*|\bencant\w*|\bexcelente\w*|\bgenial\w*|\bfeliz\w*"
    r"|\bbuenaz\w*|\blo\s+quiero\b|\bsi\b"
)
REASONS = ("ASKED_FOR_PERSON", "UPSET", "MODEL")
# F8-08l (#1046): el texto de traspaso de una organización. Nunca promete atención inmediata
# (decisión del propietario, #771): además de los detectores (`inmediat…`, `ahorita`, `hoy`,
# `horas`…), estas prisas, sobre la clave y solo en ese texto.
SOON = re.compile(
    r"\benseguida\b|\bpronto\b|\bbrevedad\b|\ben\s+breve\b|\bya\s+mismo\b|\bminutos?\b"
    r"|\bsegundos?\b|\bal\s+(?:instante|momento|tiro)\b|\brapid\w*|\bvolando\b|\bpriorid\w*"
    r"|\b(?:un|unos|pocos)\s+(?:momentos?|instantes?)\b|\burgen\w*"
)
# La única frase que los detectores marcan y el texto de traspaso admite: la del propietario sobre
# la conversación («Tu conversación quedará disponible…», #771). `disponib…` es de AVAILABILITY
# (el stock de un producto); aquí no habla de stock. Solo esa frase y solo en ese texto: el del
# modelo se sigue marcando igual. Lo que vaya pegado a ella («… disponible 2 días») sí se detecta.
KEPT = re.compile(r"\bconversacion\s+quedara\s+disponible\b")


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
            part = NUM
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


Hit = tuple[str, int, int]  # (tipo, inicio, fin)


def _spans(text: str) -> list[tuple[int, int, list[Hit]]]:
    """Los tramos de `text` con algo que detectar, ordenados y unidos (los que se tocan o solo los
    separan blancos y signos, `S/ 30`, son una sola marca), cada uno con sus coincidencias."""
    key, origin = _fold(text)
    found: list[Hit] = []
    for kind, pattern in _COMPILED.items():
        for match in pattern.finditer(key):
            if match.end() > match.start():  # lo quitado que sigue (una tilde suelta) va con él
                end = origin[match.end()] if match.end() < len(origin) else len(text)
                found.append((kind, origin[match.start()], end))
    merged: list[tuple[int, int, list[Hit]]] = []
    for hit in sorted(found, key=lambda span: (span[1], -span[2])):
        _, start, end = hit
        if merged and JOIN.fullmatch(_fold(text[merged[-1][1] : max(start, merged[-1][1])])[0]):
            first, stop, members = merged[-1]
            merged[-1] = (first, max(stop, end), [*members, hit])
        else:
            merged.append((start, end, [hit]))
    return merged


def _best(found: Sequence[Hit], start: int, end: int) -> str:
    """El tipo de mayor prioridad de lo que se detectó entre `start` y `end`."""
    kinds = [kind for kind, first, stop in found if max(first, start) < min(stop, end)]
    return min(kinds, key=_RANK.__getitem__, default="")


def _parts(text: str, owned: Sequence[tuple[int, int]]) -> list[tuple[str, int, int]]:
    """Lo que hay que marcar dentro de `owned` (lo que escribió el modelo): cada tramo de `text`
    entero recortado a cada trozo suyo, sin blancos en los bordes y con el tipo de mayor prioridad
    de lo que detectó ahí (un trozo sin nada detectado, o de solo blancos, no se marca), más
    lo que se detecta en el trozo solo («gratis» pegado a un nombre del servidor) y, como red, lo
    que aún lleve la marca entre dos tramos (letras que la clave no pliega)."""
    merged, kept = _spans(text), []
    for low, high in owned:
        pieces: list[Hit] = []
        for start, end, found in merged:
            start, end = max(start, low), min(end, high)
            kind, piece = _best(found, start, end), text[start:end]
            lead, trail = len(piece) - len(piece.lstrip()), len(piece) - len(piece.rstrip())
            if kind and piece.strip():  # sin los blancos de los bordes
                pieces.append((kind, start + lead, end - trail))
        for start, end, found in _spans(text[low:high]):
            pieces.append((_best(found, start, end), start + low, end + low))
        union: list[Hit] = []
        for kind, start, end in sorted(pieces, key=lambda hit: (hit[1], -hit[2])):
            if union and start < union[-1][2]:
                last, first, stop = union[-1]
                union[-1] = (min(last, kind, key=_RANK.__getitem__), first, max(stop, end))
            else:
                union.append((kind, start, end))
        pos = low
        for kind, start, end in [*union, ("", high, high)]:
            if has_verify_mark(text[pos:start]):
                kept.append(("MARKUP", pos, start))
            if kind:
                kept.append((kind, start, end))
            pos = end
    return kept


def mark(text: str) -> tuple[Finding, ...]:
    """El resumen (§7): cada tramo marcado «sin verificar», con su posición, sin sustituirlo.
    Pasados `MAX_FINDINGS`, el último cubre el resto."""
    found = [Finding(kind, start, end) for kind, start, end in _parts(text, [(0, len(text))])]
    if len(found) > MAX_FINDINGS:
        found[MAX_FINDINGS - 1 :] = [replace(found[MAX_FINDINGS - 1], end=found[-1].end)]
    return tuple(found)


def _owned(text: str, inserted: Sequence[tuple[int, int]]) -> list[tuple[int, int]]:
    """Los trozos de `text` fuera de `inserted`: los que escribió el modelo."""
    owned, pos = [], 0
    for start, end in sorted(inserted):
        if start > pos:
            owned.append((pos, start))
        pos = max(pos, end)
    return [*owned, (pos, len(text))] if pos < len(text) else owned


def substitute(rendered: Rendered) -> Rendered:
    """La sugerencia: cada tramo que escribió el modelo por `VERIFY_MARK`, con un hallazgo de tipo
    y posición; los hallazgos y tramos del servidor se mueven con el texto. Si el resultado pasa de
    `BODY_MAX` o de `MAX_FINDINGS`, queda solo la marca (`TOO_LONG`)."""
    text, server = rendered.text, sorted(rendered.inserted)
    between = [(left[1], right[0]) for left, right in zip(server, server[1:], strict=False)
               if left[1] < right[0] and PRICE.fullmatch(text[left[0] : left[1]])
               and PRICE.fullmatch(text[right[0] : right[1]])
               and not SEPARATOR.fullmatch(_fold(text[left[1] : right[0]])[0])]  # fmt: skip
    parts = [part for part in _parts(text, _owned(text, server))
             if not any(low <= part[1] < high for low, high in between)]  # fmt: skip
    parts += [("DISCOUNT", low, high) for low, high in between]
    by_span = {(f.start, f.end): f for f in rendered.findings}
    pieces = sorted([(start, end, "") for start, end in server] + [(s, e, k) for k, s, e in parts])
    out, pos = "", 0
    findings: list[Finding] = []
    inserted: list[tuple[int, int]] = []
    for start, end, kind in pieces:
        out += text[pos:start]
        piece = VERIFY_MARK if kind else text[start:end]
        old = None if kind else by_span.get((start, end))
        if kind or old is not None:
            findings.append(Finding(old.type if old else kind, len(out), len(out) + len(piece)))
        inserted.append((len(out), len(out) + len(piece)))
        out, pos = out + piece, end
    out += text[pos:]
    if len(out) > BODY_MAX or len(findings) > MAX_FINDINGS:
        problems = tuple(dict.fromkeys((*rendered.problems, Problem("TOO_LONG"))))
        whole = (0, len(VERIFY_MARK))
        return replace(rendered, text=VERIFY_MARK, findings=(Finding("TOO_LONG", *whole),),
                       problems=problems, expires_at=None, inserted=(whole,))  # fmt: skip
    return replace(rendered, text=out, findings=tuple(findings), inserted=tuple(inserted))


def _shouts(text: str) -> bool:
    letters = [char for char in text if char.isalpha()]
    bangs = sum(char in "!\u00a1" for char in _fold(text)[0]) >= 3
    upper = sum(char.isupper() for char in letters)
    happy = HAPPY.search(_fold(text)[0]) is not None
    return bangs and not happy and len(letters) >= CAPS_BANG and upper >= CAPS_RATIO * len(letters)


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


def reply_problems(reply: object) -> tuple[str, ...]:
    """Por qué `reply` no vale como texto de traspaso; vacío si vale. `TOO_LONG`: más de
    `HANDOFF_MAX`; `BODY`: no es un cuerpo que `clean_body` acepta tal cual (sin controles de
    dirección ni caracteres ocultos); si no, los tipos de lo que detectan los detectores (cifras,
    plazos, promesas, enlaces, la marca), salvo `KEPT`, y `SOON` (una prisa). Solo códigos."""
    if not isinstance(reply, str):
        return ("BODY",)
    if len(reply) > HANDOFF_MAX:
        return ("TOO_LONG",)
    try:
        if clean_body(reply) != reply:
            return ("BODY",)
    except ValueError:
        return ("BODY",)
    key, origin = _fold(reply)
    owned, pos = [], 0
    for kept in KEPT.finditer(key):
        owned.append((pos, origin[kept.start()]))
        pos = origin[kept.end() - 1] + 1
    found = [kind for kind, _, _ in _parts(reply, [*owned, (pos, len(reply))])]
    return tuple(dict.fromkeys(found + (["SOON"] if SOON.search(key) else [])))


def handoff_reply() -> str:
    """El texto de traspaso inicial, `AI_HANDOFF_REPLY` (el de una organización sin el suyo);
    `ImproperlyConfigured` si no vale (`reply_problems`). Al arrancar y en cada uso."""
    reply = settings.AI_HANDOFF_REPLY
    if not isinstance(reply, str) or reply_problems(reply):
        raise ImproperlyConfigured("AI_HANDOFF_REPLY")
    return reply


def suggestion(
    rendered: Rendered, customer: Sequence[str], reply: Callable[[], str] = handoff_reply
) -> tuple[Rendered, Handoff]:
    """La sugerencia que se guarda: con traspaso, el texto de traspaso que da `reply` (sin
    hallazgos ni problemas: no hay nada que reintentar; solo entonces se pide); si no,
    `substitute`."""
    found = handoff(customer, rendered.wants_handoff)
    if not found.requested:
        return substitute(rendered), found
    text = reply()
    fixed = Rendered(text, (), (), rendered.wants_handoff, None, ((0, len(text)),))
    return fixed, found

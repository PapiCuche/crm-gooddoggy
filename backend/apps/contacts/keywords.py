"""Palabras de baja y de alta de la organización (F7-08b; ADR-010 §2.7, D-F7-05).

Un mensaje del cliente cuenta solo si **todo** su texto, normalizado (`normalize`: sin tildes ni
mayúsculas, un espacio entre palabras, sin signos ni emojis en los extremos), es una palabra
configurada: «STOP» o « baja! » cuentan; «quiero darme de baja» o «stop please», no. Sin filas
valen `DEFAULTS` (D-F7-05, pendiente del propietario). Nada del cliente se registra ni se audita.
"""

import re
import unicodedata

from django.db import connections, transaction

from apps.access.selectors import AccessDenied, Denied, execution_context, has_permission
from apps.audit.services import Entity, record
from apps.contacts.models import ConsentKeyword, KeywordAction
from core.tenancy.context import TenantContext
from core.tenancy.scope import require_scope

OPT_OUT, OPT_IN = KeywordAction.OPT_OUT.value, KeywordAction.OPT_IN.value
DEFAULTS = {"stop": OPT_OUT, "baja": OPT_OUT, "cancelar": OPT_OUT, "alta": OPT_IN}
KEYWORD = re.compile(r"[a-z0-9]+(?: [a-z0-9]+){0,2}")  # la forma normalizada (y el CHECK)
KEYWORD_MAX = 30
KEYWORD_MIN = 4  # «si», «no», «ok» o «ya» son respuestas, no una baja
COMMON = frozenset(  # respuestas corrientes (normalizadas) que nunca pueden ser una palabra
    {
        "si", "no", "ok", "okay", "vale", "dale", "ya", "yes", "yep", "hola", "hello", "hi", "hey",
        "buenas", "bueno", "buena", "bien", "claro", "listo", "perfecto", "genial", "entiendo",
        "gracias", "muchas gracias", "no gracias", "thanks", "thank you", "no thanks",
        "de acuerdo", "por favor", "please", "buenos dias", "buenas tardes", "buenas noches",
    }
)  # fmt: skip
PER_ACTION = 10  # cuántas palabras de cada acción
TEXT_MAX = 120  # un mensaje más largo nunca es una palabra de baja o de alta
EDGES = re.compile(r"^[\W_]+|[\W_]+$")  # signos, símbolos y emojis de los extremos
PERMISSION = "channels.manage"  # quien configura el canal configura sus palabras (a confirmar)


def normalize(text: str) -> str:
    """La forma con la que se compara: NFKD sin marcas (tildes, diéresis, la virgulilla de la ñ),
    `casefold`, espacios unificados y sin lo que no es letra ni cifra en los extremos."""
    plain = "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))
    return EDGES.sub("", " ".join(plain.casefold().split()))


def action_for(ctx: TenantContext, text: str) -> str | None:
    """`OPT_OUT` u `OPT_IN` si el texto entero es una palabra de la organización; si no, `None`."""
    alias = require_scope(ctx)
    if len(text) > TEXT_MAX:  # ni se normaliza: con tanto relleno ya no es solo la palabra
        return None
    word = normalize(text)
    if not KEYWORD.fullmatch(word) or len(word) > KEYWORD_MAX:
        return None
    configured = dict(ConsentKeyword.objects.using(alias).values_list("keyword", "action"))
    return (configured or DEFAULTS).get(word)


def keywords(ctx: TenantContext) -> dict[str, str]:
    """Las palabras vigentes de la organización (las por defecto si no configuró ninguna)."""
    alias = require_scope(ctx)
    found = ConsentKeyword.objects.using(alias).order_by("keyword").values_list("keyword", "action")
    return dict(found) or dict(DEFAULTS)


def _wanted(opt_out: object, opt_in: object) -> dict[str, str]:
    wanted: dict[str, str] = {}
    for action, words in ((OPT_OUT, opt_out), (OPT_IN, opt_in)):
        if not isinstance(words, list) or len(words) > PER_ACTION:
            raise ValueError("lista de palabras no admitida")
        for raw in words:
            word = normalize(raw) if isinstance(raw, str) else ""
            if not KEYWORD.fullmatch(word) or len(word) > KEYWORD_MAX or word in wanted:
                raise ValueError("palabra no admitida o repetida")
            if len(word) < KEYWORD_MIN or word in COMMON:  # una respuesta normal daría de baja
                raise ValueError("palabra demasiado corta o demasiado común para una baja o alta")
            wanted[word] = action
    if OPT_OUT not in wanted.values():  # dejar al cliente sin forma de darse de baja, nunca
        raise ValueError("hace falta al menos una palabra de baja")
    return wanted


def set_keywords(ctx: TenantContext, *, opt_out: list[str], opt_in: list[str]) -> dict[str, str]:
    """Reemplaza las palabras de la organización (se guardan normalizadas). Pide `channels.manage`
    antes de leer nada (`AccessDenied`). `ValueError` (sin escribir nada) si una lista no es una
    lista de hasta 10, si una palabra no es de una a tres palabras de letras o cifras de hasta 30
    caracteres (y al menos 4: «si» o «ok» no), si es una respuesta corriente («gracias», «hola», «no
    gracias»…), si se repite (también entre baja y alta) o si no queda ninguna de baja. Audita el
    antes y el después (`consent_keywords.updated`); las mismas palabras no escriben nada."""
    alias = require_scope(ctx)
    if not has_permission(execution_context(ctx), PERMISSION):
        raise AccessDenied(Denied.PERMISSION)
    wanted = _wanted(opt_out, opt_in)
    with transaction.atomic(using=alias):
        with connections[alias].cursor() as cursor:  # dos cambios a la vez: uno tras otro
            key = f"consent-keywords:{ctx.organization_id}"
            cursor.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", [key])
        before = keywords(ctx)
        if before == wanted:
            return wanted
        rows = ConsentKeyword.objects.using(alias)
        rows.all().delete()
        for word, action in wanted.items():
            rows.create(keyword=word, action=action, created_by_user_id=ctx.user_id)
        org = Entity("organization", ctx.organization_id)
        record(ctx, "consent_keywords.updated", org, {"keywords": [before, wanted]})
    return wanted

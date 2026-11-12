"""Redactor compartido (ADR-011; 04 §N): por nombre de clave y por patrón en cualquier texto.

`redact()` devuelve una estructura nueva (no muta la entrada). Auditoría hoy; logs en F1-07.
"""

import re
from collections.abc import Callable, Mapping
from typing import Any

from django.utils.functional import Promise

REDACTED = "[REDACTED]"
MAX_DEPTH = 8
_SECRET_SEGMENTS = frozenset(
    {"password", "passwords", "passwd", "pwd", "passphrase", "secret", "secrets", "token",
     "apikey", "authorization", "cookie", "cookies", "sessionid", "ciphertext", "credential",
     "credentials", "dsn", "kek", "csrf", "csrftoken", "csrfmiddlewaretoken", "xcsrftoken",
     "sessionkey", "sessiontoken"}
)  # fmt: skip
_SECRET_FRAGMENTS = (
    "api_key", "private_key", "access_key", "session_key", "crm_session", "hub_signature",
)  # fmt: skip
# Toda clave `*session_id` se trata como credencial, aunque termine en `_id`: para correlacionar
# hay que usar otro nombre (p. ej., `conversation_id`).
_SESSION_IDS = ("session_id", "session_ids")
_NOT_SECRET_SUFFIXES = ("_at", "_limit", "_count", "_id")  # fechas, límites, contadores y FKs
_CONTENT_KEYS = frozenset({"body", "content", "text", "transcript", "caption"})
_CONTENT_SUFFIXES = ("_body", "_content", "_transcript", "_text", "_preview", "_caption")
PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # OpenAI (sk-, sk-proj-, sk-svcacct-, sk-admin-) y Anthropic (sk-ant-…).
    (re.compile(r"(?<![A-Za-z0-9])sk-(?:ant-|proj-|svcacct-|admin-)?[A-Za-z0-9_-]{16,}"), REDACTED),
    (re.compile(r"(?<![A-Za-z0-9])EAA[A-Za-z0-9]{20,}"), REDACTED),  # tokens de Meta
    (re.compile(r"(?i)\bsha256=[0-9a-f]{64}\b"), REDACTED),  # firma X-Hub-Signature-256 de Meta
    (re.compile(r"(?i)(?<=[?&])(?:oh|oe|hash|mid)=[^&\s\"']+"), REDACTED),  # URL firmada de Meta
    (
        re.compile(
            r"(?i)(\bbearer|authorization[\"']?\s*[:=]\s*[\"']?basic)\s+[A-Za-z0-9._~+/=-]{8,}"
        ),
        rf"\1 {REDACTED}",
    ),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}"), REDACTED),
    (re.compile(r"(?<=://)[^/\s:@]*:[^/\s@]+@"), f"{REDACTED}@"),  # [usuario]:clave@ en URLs
    (  # client_secret=, hub.verify_token=, DB_PASSWORD=… (input_tokens= no)
        re.compile(
            # El lookbehind incluye `_.-`: la búsqueda no reintenta dentro de una misma palabra
            # (sin él, una cadena larga de puntos cuesta tiempo cuadrático).
            r"(?i)(?<![a-z0-9_.-])([a-z0-9_.-]*?(?:api[_-]?key|token|secret|passw(?:or)?d))"
            r"=[^&\s\"']+"
        ),
        rf"\1={REDACTED}",
    ),
    (  # session_key=…, sessionid: …, crm_session=…, X-CSRFToken: … dentro de un texto. Nombres
        # exactos y prefijo acotado: coste lineal, y `csrf_failure_count=3` no se toca.
        re.compile(
            r"(?i)(?<![a-z0-9_.-])([a-z0-9_.-]{0,40}?(?:session[_-]?(?:id|key)|crm_session|"
            r"csrf(?:middleware)?[_-]?token)[\"']?\s*[=:]\s*[\"']?)[^&\s\"';,}]+"
        ),
        rf"\1{REDACTED}",
    ),
    (  # repr/JSON de un dict dentro de un texto: 'password': 'x', "api_key": "y"
        re.compile(
            r"(?i)(['\"][a-z0-9_.-]*?(?:api[_-]?key|token|secret|passw(?:or)?d)['\"]\s*:\s*)"
            r"(['\"])[^'\"]*\2"
        ),
        rf"\1\2{REDACTED}\2",
    ),
)


def _normalize(key: str) -> str:
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", key).lower().replace("-", "_").replace(".", "_")


def is_sensitive_key(key: str) -> bool:
    name = _normalize(key)
    if name.endswith(_SESSION_IDS):
        return True
    if name.endswith(_NOT_SECRET_SUFFIXES):
        return False
    segments = {s.rstrip("0123456789") for s in name.split("_")}  # password1, new_password2
    return bool(_SECRET_SEGMENTS & segments) or any(f in name for f in _SECRET_FRAGMENTS)


EMAIL_MASK = "[EMAIL]"
# Cualquier cosa con forma de dirección, en cualquier alfabeto, con `@` o `%40`. Anclada por la
# izquierda: una cadena larga sin `@` se recorre una sola vez.
_LOCAL = r"[^\s<>()\[\]{},;:\"=@]"
EMAIL = re.compile(
    rf"(?i)(?:(?<!{_LOCAL}){_LOCAL}+?|\"[^\"\n]{{1,64}}\"|(?<=\[REDACTED\]))"
    rf"(?:@|%40)(?:\[[^\]\s]{{0,64}}\]|[^\s<>()\[\]{{}},;:\"'@]+)"
)


def mask_emails(text: str) -> str:
    """Un email no es un secreto y `redact` lo deja pasar; esto lo tapa donde no debe quedar:
    la auditoría de plataforma (ADR-013 §4) y el texto libre que un operador manda al log."""
    return EMAIL.sub(EMAIL_MASK, text)


def redact_text(text: str) -> str:
    for pattern, replacement in PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def _each(value: Any, fn: Callable[[Any], Any]) -> Any:
    """Aplica `fn` conservando la forma [antes, después] y los `None`."""
    if isinstance(value, list | tuple):
        return [None if item is None else fn(item) for item in value]
    return None if value is None else fn(value)


def _by_key(key: str, value: Any, depth: int) -> Any:
    name = _normalize(key)
    if name.endswith("last_four"):  # 04 §N.1: solo last_four antes y después
        return _each(value, lambda v: v if isinstance(v, str) and len(v) <= 4 else REDACTED)
    if is_sensitive_key(key) or name in _CONTENT_KEYS or name.endswith(_CONTENT_SUFFIXES):
        return _each(value, lambda v: REDACTED)
    return redact(value, _depth=depth + 1)


def redact(value: Any, *, _depth: int = 0) -> Any:
    if _depth > MAX_DEPTH:
        return "[TRUNCATED]"
    if isinstance(value, Promise):  # cadenas lazy de Django: el encoder JSON haría str()
        value = str(value)
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, bytes | bytearray | memoryview):
        return "[BINARY]"
    if isinstance(value, Mapping):
        return {str(k): _by_key(str(k), v, _depth) for k, v in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [redact(item, _depth=_depth + 1) for item in value]
    return value

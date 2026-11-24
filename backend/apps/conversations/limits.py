"""Lo que cada canal acepta en una respuesta (ADR-035 §7; F7b-16): una sola tabla para el
adaptador de Meta (`meta_page`, al despachar), el comando que encola (`record_outbound`, antes de
crear la fila), la ruta de medios y el compositor (`ChannelRefSerializer`). Pura.

Instagram: «Message text must be UTF-8 and be a 1000 bytes or less» (Messaging API, [S8] de
ADR-035); Messenger: 2000 caracteres (SIN VERIFICAR 6 de ADR-035). Medios salientes de Messenger e
Instagram: después (ADR-035 §11). El resto de canales, el tope de la API (`BODY_MAX`) y con medios.
"""

from apps.conversations.models import BODY_MAX

CHARACTERS, BYTES = "CHARACTERS", "BYTES"  # puntos de código o bytes UTF-8
TEXT_LIMITS = {"MESSENGER": (CHARACTERS, 2000), "INSTAGRAM": (BYTES, 1000)}
NO_MEDIA = frozenset(TEXT_LIMITS)


def text_limit(platform: str) -> tuple[str, int]:
    return TEXT_LIMITS.get(platform, (CHARACTERS, BODY_MAX))


def fits(platform: str, text: str) -> bool:
    """¿Cabe el texto en el canal? Messenger cuenta caracteres; Instagram, bytes UTF-8."""
    unit, limit = text_limit(platform)
    return (len(text.encode()) if unit == BYTES else len(text)) <= limit


def takes_media(platform: str) -> bool:
    return platform not in NO_MEDIA

"""La forma de los slugs del catálogo (marcas, categorías) y cómo se forman a partir de un nombre.
Puro: lo comparten los comandos del módulo."""

import re
import unicodedata

SLUG = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")  # la misma forma que imponen las tablas
SLUG_MAX = 63


class NoSlug(ValueError):
    """El nombre no deja formar un slug: hay que enviarlo."""


def clean_slug(value: str) -> str:
    """El slug como se guarda: minúsculas ASCII, cifras y guiones entre ellas."""
    slug = value.strip()
    if not (len(slug) <= SLUG_MAX and SLUG.fullmatch(slug)):
        raise ValueError(f"minúsculas sin acento, cifras y guiones entre ellas, hasta {SLUG_MAX}")
    return slug


def slug_from(value: str) -> str:
    """Un slug a partir del nombre: sin acentos, en minúsculas, con guiones. `NoSlug` si el
    nombre no deja ninguna letra o cifra ASCII (por ejemplo, solo caracteres no latinos)."""
    ascii_text = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")[:SLUG_MAX].strip("-")
    if not slug:
        raise NoSlug("no se puede formar a partir del nombre: envía uno")
    return slug

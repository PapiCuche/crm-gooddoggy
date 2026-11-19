"""F2-131: la regla de texto de `core.text` frente a lo que no se ve. Los selectores de variación
solo valen tras una base que pueden cambiar (uno por base); ZWNJ y ZWJ, solo dentro de una
palabra. Se comprueba en cada uso de `core.text` que los nombres corrientes siguen valiendo."""

from collections.abc import Callable
from functools import partial
from typing import Any

import pytest
from django.core.exceptions import ValidationError

from apps.accounts.services import _person_name
from apps.catalog.brands import brand_name
from apps.catalog.categories import category_name
from apps.catalog.products import product_name, short_description
from apps.contacts.commands import _clean as contact_fields
from apps.contacts.tags import tag_name
from apps.conversations.profiles import untrusted
from apps.conversations.quick_replies import RULES as QUICK_REPLY
from apps.deals.api.inputs import _name as pipeline_name
from apps.deals.api.opportunity_inputs import _title as opportunity_title
from apps.inventory.stock import reason as stock_reason
from apps.inventory.warehouses import warehouse_name
from apps.organizations.branches import branch_name
from apps.organizations.teams import CLEAN as TEAM
from apps.pricing.price_lists import price_list_name
from apps.pricing.text import change_reason
from apps.quotes.commands import _text as quote_text
from apps.tasks.commands import _text as task_text
from core.text import line, name, optional_name

HIDDEN = "Ana" + "\ufe01" * 40  # 40 selectores: datos que no se ven
REFUSED = (
    HIDDEN,
    "Ana\ufe0f",  # VS16 tras una letra latina
    "Ana \ufe0f",  # tras un espacio
    "\ufe0fAna",  # al principio
    "\u2764\ufe0f\ufe0f",  # dos tras la misma base
    "\u2764\ufe0f\U000e0100",  # de rangos distintos, también dos
    "Ana\U000e0100",  # selector ideográfico tras una letra latina
    "\u2764\U000e0100",  # ni tras un símbolo
    "Ana\u180b",  # selector mongol tras una letra latina
    "Ana\u180f",
    "Ana\U000e01ef",  # el último selector ideográfico
    "\u8279\u180b",  # ni tras un ideograma
    "\u2800\ufe0f",  # tras el braille vacío, que no se ve
    "\u3164\ufe00",  # ni tras un relleno hangul
    "\u3164\U000e0100",
    "Own\u034fer",  # CGJ, siempre
    "Ana\u17b4",  # las vocales inherentes jemer, siempre
    "Ana\u17b5",
    "Ana" + "\u3164" * 40,  # rellenos hangul: datos que no se ven aunque haya letras
    "Ana\u1160",
    "Ana\u115f",
    "Ana\uffa0",
    "Ana\U00013441",  # blancos jeroglíficos
    "Ana\U00013442",
    "Ana\U0001d159",  # la cabeza de nota nula
    "\u0915\U00016fe4",  # el relleno kitán
    "Ana\u200dMar\u00eda",  # entre letras latinas, ZWNJ y ZWJ no hacen falta
    "\u0391\u200c\u0392",  # ni griegas
    "\u0410\u200d\u0411",  # ni cirílicas
    "\u4e00\u200c\u4e01",  # ni ideogramas
    "\uac00\u200d\uac01",  # ni hangul
    "\u3042\u200c\u3044",  # ni kana
    "\u30a2\u200d\u30a4",
    "\u0628\u200cA",  # ni junto a una latina
    "Ana " + "\u2764\ufe0f" * 9,  # más de 8 selectores y ZWNJ/ZWJ
    "\u0628\u200c" * 9 + "\u0628",  # 9 ZWNJ
    "\u0628\u200c" * 5 + "\u0628 \u2764\ufe0f" * 4,  # 5 ZWNJ y 4 selectores
    "\u200cAna",  # ZWNJ o ZWJ al principio, al final, repetidos o junto a lo que no es letra
    "Ana\u200d",
    "An\u200c\u200ca",
    "An\u200c\u200da",
    "Ana \u200cMaría",
    "Ana\u200c María",
    "\U0001f468\u200d\U0001f469\u200d\U0001f467",  # entre emojis: como hasta ahora
    "1\u200c2",
    "a\u200c\u0301b",  # tras la letra viene una marca, no otra letra
    "\u3164\u200ca",  # un relleno no es una letra
    "a\u200c\u3164",
)
ACCEPTED = (
    "Ana María",
    "Ñahui Peña",
    "José Pérez-Ríos",
    "O'Brien",
    "D’Angelo",
    "Café \u2764\ufe0f",  # un emoji con su VS16
    "1\ufe0f\u20e3 Sucursal",  # un keycap
    "Mesa \u2194\ufe0e",  # VS15: forma de texto
    "\u203c\ufe0f Urgente",
    "\u845b\U000e0100\u98fe",  # un ideograma con su selector (IVS): nombres japoneses
    "\u8279\ufe00",  # una variante estándar de ideograma
    "\u1820\u180b\u1821\u180f",  # mongol, con sus selectores
    "\u062d\u0633\u06cc\u0646\u200c\u0632\u0627\u062f\u0647",  # persa: ZWNJ entre letras
    "\u0915\u094d\u200d\u0937",  # devanagari: ZWJ tras el virama
    "\u0dc1\u0dca\u200d\u0dbb\u0dd3",  # cingalés: Sri
    "\u0928\u094d\u200c\u0928",  # ZWNJ tras el virama
    "\u2139\ufe0f Info",  # U+2139: una letra para Unicode, pero también un emoji
    "Ana " + "\u2764\ufe0f" * 8,  # 8 selectores y ZWNJ/ZWJ: el tope
    "\u0628\u200c" * 8 + "\u0628",
)


@pytest.mark.parametrize("value", REFUSED)
def test_hidden_selectors_marks_and_stray_joiners_are_refused(value: str) -> None:
    for check in (partial(line, limit=200), partial(name, limit=200)):
        with pytest.raises(ValueError) as error:
            check(value)
        assert "Ana" not in str(error.value) and "\ufe0f" not in str(error.value)  # sin eco


@pytest.mark.parametrize("value", ACCEPTED)
def test_names_of_other_scripts_and_a_single_selector_after_its_base_are_kept(value: str) -> None:
    assert line(value, 200) == name(value, 200) == value


def test_the_rules_hold_after_nfc_and_across_the_lines_of_a_note() -> None:
    assert line("\u0627\u0653\u200c\u0628", 50) == "\u0622\u200c\u0628"  # NFC no mueve el ZWNJ
    with pytest.raises(ValueError):
        line("\u0628\u3000\u200c\u0628", 50)  # un espacio ideográfico es un espacio
    note = "Hola \u2764\ufe0f\nadiós"
    assert task_text(note, "nota", 99, lines=True) == note
    with pytest.raises(ValueError):
        task_text("Hola\n" + HIDDEN, "nota", 99, lines=True)  # en cualquier línea


def test_an_optional_name_is_empty_or_has_something_to_read() -> None:
    assert optional_name("", 9) == optional_name("  ", 9) == ""
    assert optional_name(" de la  Cruz ", 50) == "de la Cruz"
    for blank in ("\u3164", "\u2800", "\u0301\u0301", "\U0001f436", "- .", "\u2764\ufe0f"):
        with pytest.raises(ValueError):
            optional_name(blank, 50)


def _contact(field: str) -> Callable[[str], Any]:
    return lambda value: contact_fields({field: value})[field]


def _person(value: str) -> str:
    try:
        return _person_name(value, required=False)
    except ValidationError as error:
        raise ValueError("nombre") from error


FAMILIES: dict[str, Callable[[str], Any]] = {
    "sucursal": branch_name,
    "equipo": TEAM["name"],
    "equipo, descripción": TEAM["description"],
    "contacto": _contact("display_name"),
    "contacto, nombre": _contact("first_name"),
    "contacto, apellido": _contact("last_name"),
    "etiqueta": tag_name,
    "marca": brand_name,
    "categoría": category_name,
    "producto": product_name,
    "producto, descripción": short_description,
    "embudo": pipeline_name,
    "oportunidad": opportunity_title,
    "almacén": warehouse_name,
    "motivo de stock": stock_reason,
    "lista de precios": price_list_name,
    "motivo de precio": change_reason,
    "respuesta rápida": QUICK_REPLY["name"],
    "tarea": lambda value: task_text(value, "nota", 500, lines=True),
    "cotización": lambda value: quote_text(value, "nota", 500),
    "invitación, apellido": _person,
}
COMMON = (
    "Ana María",
    "Ñahui Peña",
    "José Pérez-Ríos",
    "O'Brien",
    "Café \u2764\ufe0f",
    "1\ufe0f\u20e3 Caja",
    "\u845b\U000e0100\u98fe",
    "\u062d\u0633\u06cc\u0646\u200c\u0632",
)


@pytest.mark.parametrize("family", FAMILIES)
def test_every_user_of_core_text_keeps_common_names_and_refuses_hidden_data(family: str) -> None:
    clean = FAMILIES[family]
    for value in COMMON:
        assert clean(value) == value, (family, value)
    for value in (HIDDEN, "Own\u034fer", "\u200cAna"):
        with pytest.raises(ValueError):
            clean(value)


def test_an_instagram_profile_name_with_hidden_data_is_dropped_not_raised() -> None:
    assert untrusted(HIDDEN, 100) == untrusted("Ana\ufe0f", 100) == ""
    assert untrusted("Ana" + "\u3164" * 40, 100) == ""
    assert untrusted("Ana \U0001f468\u200d\U0001f469", 100) == "Ana \U0001f468\U0001f469"
    assert untrusted("Ana \u2764\ufe0f", 100) == "Ana \u2764\ufe0f"

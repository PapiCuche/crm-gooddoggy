"""F2-99: generar y reconocer un código de respaldo del segundo factor (ADR-023 §6). Sin base
de datos."""

import hashlib
import logging
import re
import secrets

import pytest

from apps.accounts import backup_codes
from apps.accounts.backup_codes import ALPHABET, code_hash, new_codes, normalized

SHOWN = re.compile(r"[0-9A-HJKMNP-TV-Z]{4}(-[0-9A-HJKMNP-TV-Z]{4}){3}")
CODE = "7K3M-9QXA-0B1C-ZZ2Y"  # uno con la forma de los que se enseñan
PLAIN = CODE.replace("-", "")


def test_it_makes_ten_distinct_codes_of_80_random_bits_each(
    caplog: pytest.LogCaptureFixture,
) -> None:
    assert len(ALPHABET) == len(set(ALPHABET)) == 32 and not set("ILOU") & set(ALPHABET)
    assert (backup_codes.COUNT, backup_codes.LENGTH) == (10, 16)  # 16 símbolos de 5 bits: 80
    with caplog.at_level(logging.DEBUG):
        first, second = new_codes(), new_codes()
    assert len(first) == len(set(first)) == 10 and not set(first) & set(second)
    assert all(SHOWN.fullmatch(code) for code in first + second)
    seen = set("".join(first + second)) - {"-"}
    assert seen <= set(ALPHABET) and len(seen) > 20  # del alfabeto entero, no de unos pocos
    assert all(normalized(code) == code.replace("-", "") for code in first)  # se reconocen
    assert caplog.records == []  # el módulo no registra nada


def test_two_equal_draws_do_not_shorten_the_set(monkeypatch: pytest.MonkeyPatch) -> None:
    draws = iter("0" * 16 + "0" * 16 + "".join(ALPHABET[n % 32] * 16 for n in range(1, 10)))
    monkeypatch.setattr(secrets, "choice", lambda alphabet: next(draws))
    codes = new_codes()
    assert len(set(codes)) == 10 and sorted(codes)[0] == "0000-0000-0000-0000"


@pytest.mark.parametrize(
    "typed",
    [
        CODE,
        PLAIN,
        CODE.lower(),
        PLAIN.lower(),
        "7k3m 9qxa 0b1c zz2y",
        " 7K3M - 9QXA - 0B1C - ZZ2Y ",
        "7K3M-9QXA-OBIC-ZZ2Y",  # O por 0, I por 1
        "7k3m-9qxa-oblc-zz2y",  # y L por 1
        "7-K-3-M-9-Q-X-A-0-B-1-C-Z-Z-2-Y",
    ],
)
def test_what_a_person_types_gives_the_hash_of_the_code_that_was_made(typed: str) -> None:
    assert normalized(typed) == PLAIN
    assert code_hash(typed) == hashlib.sha256(PLAIN.encode()).hexdigest()
    assert re.fullmatch(r"[0-9a-f]{64}", code_hash(typed) or "")  # lo que exige la tabla


@pytest.mark.parametrize(
    "typed",
    [
        "",
        PLAIN[:-1],
        PLAIN + "0",
        PLAIN[:-1] + "U",  # la U no está en el alfabeto
        PLAIN[:-1] + "_",
        PLAIN[:-1] + "\n",
        PLAIN + "\n",
        "\t" + PLAIN,
        PLAIN[:-1] + "ı",  # una i sin punto, que en mayúsculas sería una I
        PLAIN[:-2] + "ß",  # dos letras al pasar a mayúsculas
        PLAIN[:-1] + "Ａ",  # una A de ancho completo
        PLAIN[:-1] + "٣",  # un dígito de otra escritura
        "123456",  # un código TOTP no es uno de respaldo
        "-" * 16,
    ],
)
def test_what_is_not_sixteen_symbols_of_the_alphabet_has_no_hash(typed: str) -> None:
    assert normalized(typed) is None and code_hash(typed) is None


def test_two_different_codes_never_share_a_hash() -> None:
    # La normalización solo junta lo que no está en el alfabeto con su dígito: dos códigos que
    # se generan distintos no pueden acabar iguales.
    for symbol in ALPHABET:
        assert normalized(symbol * 16) == symbol * 16
    hashes = {code_hash(symbol * 16) for symbol in ALPHABET}
    assert len(hashes) == 32 and None not in hashes
    assert code_hash("1" * 16) == code_hash("I" * 16) == code_hash("l" * 16)
    assert code_hash("0" * 16) == code_hash("o" * 16) != code_hash("1" * 16)

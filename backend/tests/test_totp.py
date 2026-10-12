"""F2-96: calcular y comprobar un código TOTP (ADR-023 §2), contra los vectores de RFC 4226
(apéndice D) y RFC 6238 (apéndice B). Sin base de datos."""

import base64
import hmac
import logging
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

import pytest

from apps.accounts import totp
from apps.accounts.totp import accepted_step, as_text, code, new_secret, step_at, uri

RFC_KEY = b"1234567890" * 2  # el secreto de prueba de los dos RFC, en ASCII
HOTP = "755224 287082 359152 969429 338314 254676 287922 162583 399871 520489".split()
# RFC 6238, apéndice B, modo SHA-1: instante y código de 8 dígitos; con 6, sus seis últimos.
TOTP = {
    59: "94287082",
    1111111109: "07081804",
    1111111111: "14050471",
    1234567890: "89005924",
    2000000000: "69279037",
    20000000000: "65353130",
}
NOW = 1_800_000_015.0  # a mitad de un paso
STEP = int(NOW // 30)


def test_the_codes_are_those_of_the_rfc_vectors() -> None:
    assert [code(RFC_KEY, counter) for counter in range(10)] == HOTP
    for seconds, eight in TOTP.items():
        assert code(RFC_KEY, step_at(seconds)) == eight[-6:], seconds
    assert (step_at(0), step_at(29.999), step_at(30), step_at(59)) == (0, 0, 1, 1)
    assert code(RFC_KEY, 2**40) != code(RFC_KEY, 2**40 + 1)  # el contador entero, no 32 bits
    assert all(len(code(key, STEP)) == 6 for key in (b"\x00" * 20, b"\xff" * 20, RFC_KEY))
    assert code(RFC_KEY, 37037036) == "081804"  # conserva el cero de la izquierda


def test_it_takes_the_current_step_and_its_two_neighbours_and_nothing_else() -> None:
    def check(shift: int) -> int | None:
        return accepted_step(RFC_KEY, code(RFC_KEY, STEP + shift), now=NOW, last=None)

    assert [check(shift) for shift in (-2, -1, 0, 1, 2)] == [None, STEP - 1, STEP, STEP + 1, None]
    edge = STEP * 30.0  # justo al empezar el paso, y justo antes de acabar
    assert accepted_step(RFC_KEY, code(RFC_KEY, STEP - 1), now=edge, last=None) == STEP - 1
    assert accepted_step(RFC_KEY, code(RFC_KEY, STEP - 1), now=edge + 59.999, last=None) is None
    assert accepted_step(RFC_KEY, code(RFC_KEY, STEP), now=NOW, last=None) == STEP
    other = bytes(reversed(RFC_KEY))  # el código de otro secreto no vale
    assert code(other, STEP) != code(RFC_KEY, STEP)
    assert accepted_step(RFC_KEY, code(other, STEP), now=NOW, last=None) is None


def test_a_step_is_accepted_once_and_nothing_at_or_before_it_afterwards() -> None:
    codes = {shift: code(RFC_KEY, STEP + shift) for shift in (-1, 0, 1)}

    def check(shift: int, last: int | None) -> int | None:
        return accepted_step(RFC_KEY, codes[shift], now=NOW, last=last)

    assert check(0, None) == check(0, STEP - 1) == STEP
    assert check(0, STEP) is None  # el mismo código, otra vez
    assert check(-1, STEP) is None  # ni el del paso anterior a uno ya usado
    assert check(1, STEP) == STEP + 1  # el siguiente sí, una vez
    assert check(1, STEP + 1) is None and check(0, STEP + 5) is None
    assert check(-1, STEP - 2) == STEP - 1 and check(-1, STEP - 1) is None
    assert accepted_step(RFC_KEY, code(RFC_KEY, 0), now=0.0, last=None) == 0  # sin pasos negativos
    assert accepted_step(RFC_KEY, code(RFC_KEY, 1), now=0.0, last=None) == 1
    assert accepted_step(RFC_KEY, code(RFC_KEY, 0), now=-31.0, last=None) is None


@pytest.mark.parametrize(
    "presented",
    [
        "",
        "12345",
        "1234567",
        "12345a",
        " 23456",
        "12345\n",
        "１２３４５６",
        "١٢٣٤٥٦",
        "-12345",
        "1e5000",
    ],
)
def test_what_is_not_six_ascii_digits_is_never_a_code(
    presented: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert accepted_step(RFC_KEY, presented, now=NOW, last=None) is None
    # Ni aunque el código de ese paso, por la razón que fuera, coincidiera con lo presentado.
    monkeypatch.setattr(totp, "code", lambda secret, step: presented)
    assert accepted_step(RFC_KEY, presented, now=NOW, last=None) is None


def test_it_compares_every_step_of_the_window_in_constant_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[bytes, bytes]] = []
    real = hmac.compare_digest

    def spy(left: Any, right: Any) -> bool:
        seen.append((left, right))
        return real(left, right)

    monkeypatch.setattr(hmac, "compare_digest", spy)  # el módulo lo llama por su nombre
    first = code(RFC_KEY, STEP - 1)  # coincide con el primero de la ventana: no sale antes
    assert accepted_step(RFC_KEY, first, now=NOW, last=None) == STEP - 1
    assert [left for left, _ in seen] == [code(RFC_KEY, STEP + n).encode() for n in (-1, 0, 1)]
    assert {right for _, right in seen} == {first.encode()}
    seen.clear()
    assert accepted_step(RFC_KEY, "no", now=NOW, last=None) is None
    assert len(seen) == 3  # lo que no es un código hace las mismas comparaciones


def test_a_new_secret_is_160_random_bits_shown_as_base32_and_as_an_otpauth_uri(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.DEBUG):
        first, second = new_secret(), new_secret()
        assert len(first) == len(second) == 20 and first != second
        shown = as_text(first)
        assert len(shown) == 32 and "=" not in shown and base64.b32decode(shown) == first
        assert as_text(b"\x00" * 7) == "AAAAAAAAAAAA"  # sin relleno, tampoco cuando lo llevaría
        link = uri(first, issuer="Good Doggy CRM", account="ana+x@example.com")
    parts = urlsplit(link)
    assert (parts.scheme, parts.netloc) == ("otpauth", "totp")
    assert parts.path == "/Good%20Doggy%20CRM%3Aana%2Bx%40example.com"  # todo escapado
    assert unquote(parts.path) == "/Good Doggy CRM:ana+x@example.com"
    assert parse_qs(parts.query, strict_parsing=True) == {
        "secret": [shown],
        "issuer": ["Good Doggy CRM"],
        "algorithm": ["SHA1"],
        "digits": ["6"],
        "period": ["30"],
    }
    assert "+" not in parts.query  # los espacios, como %20: no toda aplicación lee `+`
    odd = uri(first, issuer="A&B=C?/#", account="x y&secret=Z")  # nada se cuela como parámetro
    assert parse_qs(urlsplit(odd).query)["secret"] == [shown]
    assert unquote(urlsplit(odd).path) == "/A&B=C?/#:x y&secret=Z"
    assert caplog.records == []  # el módulo no registra nada

"""F3-42: `core.money`, sin base de datos. Un importe se lee sin redondear y nunca de un float."""

from decimal import Decimal

import pytest

from core.money import amount_text, currency, parse_amount


@pytest.mark.parametrize(
    ("value", "read"),
    [
        ("4399", "4399.00"),
        (" 4399.9 ", "4399.90"),
        ("0.01", "0.01"),
        ("999999999999.99", "999999999999.99"),  # NUMERIC(14,2)
        (Decimal("4399.90"), "4399.90"),
        (Decimal("1E+3"), "1000.00"),
    ],
)
def test_an_amount_is_read_as_written(value: str | Decimal, read: str) -> None:
    assert amount_text(parse_amount(value)) == read


@pytest.mark.parametrize(
    "value",
    ["4399.999", "-1", "+1", "1e3", "1,5", "١٢", "1000000000000", ".5", "5.", "", "NaN"]
    + [Decimal("4399.999"), Decimal("NaN"), Decimal("Infinity"), Decimal("-1")],
)
def test_an_amount_is_never_rounded_or_guessed(value: str | Decimal) -> None:
    with pytest.raises(ValueError):
        parse_amount(value)


def test_a_float_is_refused() -> None:
    for value in (4399.9, 4399, True):
        with pytest.raises(TypeError):
            parse_amount(value)  # type: ignore[arg-type]


def test_the_currency_of_an_amount() -> None:
    assert currency("PEN") == "PEN"
    for wrong in ("pen", "PE", "PENN", "S/", "ÑAN"):
        with pytest.raises(ValueError):
            currency(wrong)

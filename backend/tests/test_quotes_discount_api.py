"""F5-09e2b: el descuento de una línea por la API (`…/items/{line_id}/discount/`), con los roles
reales (ana Owner, vera Vendedora `OWN`, sam Supervisor `TEAM`, bea sin rol, luis de B), la sesión y
`crm_app`: permisos antes de leer, el mismo 404, el límite de 3 % al céntimo, entradas hostiles, la
versión, una carrera y que la ruta nunca llega al suelo de costo ni cuenta en su tope."""

from decimal import Decimal as D
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from apps.quotes.models import DiscountPolicy
from core.tenancy.scope import tenant_scope
from tests.factories import make_user
from tests.test_authorization import give
from tests.test_member_branch import DENIED
from tests.test_memberships import ctx, join
from tests.test_quotes_pricing_api import SENTINEL, draft, lab, submit
from tests.test_quotes_read_api import QUOTES, board, crm, pipelines, real_stack
from tests.test_quotes_write_api import DB, ROWS, desk, fail, threaded, waiting
from tests.test_self_context import NOT_FOUND, reply, signed
from tests.test_team_writes import send

__all__ = ["board", "crm", "desk", "lab", "pipelines", "real_stack"]  # las fixtures
pytestmark = pytest.mark.usefixtures("tenant_db")
GONE = object()  # quita la clave del cuerpo
DISCOUNTED = "SELECT count(*) FROM audit_logs WHERE action = 'quote.line_discounted'"
COUNTED = "SELECT count(*) FROM login_throttles WHERE key LIKE 'qfl:%'"


def discount(client: Any, quote: Any, line: Any, kind: str = "PERCENT", value: Any = "3.00",
             version: int = 1, **extra: Any) -> Any:  # fmt: skip
    body = {"version": version, "discount_type": kind, "discount_value": value, **extra}
    sent = {k: v for k, v in body.items() if v is not GONE}
    return send(client, "post", f"{QUOTES}{quote}/items/{line}/discount/", sent)


def test_the_permissions_come_before_the_body_and_anything_out_of_reach_is_one_404(
    lab: SimpleNamespace, migrator: DB
) -> None:
    mine, [line] = draft(lab)
    theirs, [their_line] = draft(lab, "ana")
    before = migrator.execute(ROWS).fetchone()
    for target in (mine, uuid4()):  # sin rol: 403, exista o no, y sin mirar el cuerpo
        assert reply(discount(signed(lab.bea), target, line, bogus=1)) == DENIED
    viewer = make_user()  # ve y descuenta, pero no cambia: ni descuento ni pedir aprobación
    give(lab.a, join(lab.a, viewer).pk, {"quotes.view": "ORGANIZATION", "quotes.discount": None})
    assert reply(discount(signed(viewer), mine, line, bogus=1)) == DENIED
    asked = f"{QUOTES}{mine}/request-approval/"
    assert reply(send(signed(viewer), "post", asked, {"version": 1, "bogus": 1})) == DENIED
    give(lab.a, lab.m["bea"], {"quotes.view": "ORGANIZATION", "quotes.update": "ORGANIZATION"})
    assert reply(discount(signed(lab.bea), mine, line, bogus=1)) == DENIED  # sin quotes.discount
    vera = signed(lab.vera)
    for quote, at, version in ((theirs, their_line, 7), (uuid4(), line, 7),
                               (lab.quotes["b"], line, 7), (mine, their_line, 1),
                               (mine, uuid4(), 1)):  # fmt: skip
        assert reply(discount(vera, quote, at, version=version)) == NOT_FOUND
    assert reply(discount(signed(lab.luis), mine, line)) == NOT_FOUND  # de otra organización
    assert fail(discount(vera, mine, line, version=7))[:2] == (409, "STALE_VERSION")  # la ve
    assert migrator.execute(ROWS).fetchone() == before and lab.asked == []


def test_three_percent_fits_and_is_approved_on_submit_and_a_cent_more_never_is(
    lab: SimpleNamespace, migrator: DB
) -> None:
    role = give(lab.a, lab.m["vera"], {})
    with tenant_scope(ctx(lab.a)):
        DiscountPolicy.objects.create(role_id=role.pk, max_discount_percent=D(3))
    vera = signed(lab.vera)
    fits, [phone] = draft(lab)
    done = discount(vera, fits, phone, "PERCENT", "3.00")  # 131,97 de 4399
    assert (done.status_code, done.json()["status"], done.json()["version"]) == (200, "DRAFT", 2)
    assert [done.json()["items"][0][k] for k in ("final_unit_price", "discount_amount")] == [
        "4267.03", "131.97",
    ]  # fmt: skip
    assert SENTINEL not in done.content.decode()
    assert submit(vera, fits, 2).json()["status"] == "APPROVED"
    for kind, value, variant in (("PERCENT", "3.01", lab.phone), ("AMOUNT", "3.02", lab.case)):
        over, [line] = draft(lab, "vera", variant)  # 3,02 de 100,50 es 3,0049 %: sube a 3,01
        assert discount(vera, over, line, kind, value).json()["status"] == "PENDING_APPROVAL"
        assert submit(vera, over, 2).json()["code"] == "QUOTE_NOT_DRAFT"  # nunca APPROVED
    edge, [case] = draft(lab, "vera", lab.case)  # 3,01 de 100,50 es 2,995 %: cabe
    assert discount(vera, edge, case, "AMOUNT", "3.01").json()["status"] == "DRAFT"
    assert migrator.execute(DISCOUNTED).fetchone() == (4,) and lab.asked == []


@pytest.mark.parametrize(
    ("over", "field"),
    [({"discount_value": 3.0}, "discount_value"), ({"discount_value": 3}, "discount_value"),
     ({"discount_value": "NaN"}, "discount_value"), ({"discount_value": "1e3"}, "discount_value"),
     ({"discount_value": "-1"}, "discount_value"), ({"discount_value": "1.001"}, "discount_value"),
     ({"discount_value": "1000000000000"}, "discount_value"),
     ({"discount_value": True}, "discount_value"), ({"discount_value": None}, "discount_value"),
     ({"discount_value": GONE}, "discount_value"), ({"discount_value": "100.01"}, "discount_value"),
     ({"discount_type": "NONE"}, "discount_value"), ({"discount_type": "percent"}, "discount_type"),
     ({"discount_type": GONE}, "discount_type"), ({"version": "1"}, "version"),
     ({"status": "APPROVED"}, "status"), ({"organization_id": str(uuid4())}, "organization_id"),
     ({"final_unit_price": "1.00"}, "final_unit_price"), ({"line_id": str(uuid4())}, "line_id"),
     ({"manual_price_override": False}, "manual_price_override"),
     ({"discount_type": "AMOUNT", "discount_value": "4399.00"}, "_"),
     ({"discount_type": "PERCENT", "discount_value": "100.00"}, "_")],
)  # fmt: skip
def test_a_hostile_value_or_field_is_a_400_and_writes_nothing(
    lab: SimpleNamespace, migrator: DB, over: dict[str, Any], field: str
) -> None:
    quote, [line] = draft(lab)
    before = migrator.execute(ROWS).fetchone()
    assert fail(discount(signed(lab.vera), quote, line, **over)) == (
        400,
        "VALIDATION_ERROR",
        [field],
    )
    assert migrator.execute(ROWS).fetchone() == before


def test_none_takes_a_manual_price_back_and_the_route_is_never_counted_nor_measured(
    lab: SimpleNamespace, migrator: DB, settings: Any
) -> None:
    settings.QUOTE_FLOOR_THROTTLE = {"quote": (1, 3600), "user": (1, 86400)}
    quote, [line] = draft(lab, "ana")
    ana, version = signed(lab.ana), 1
    for kind, value in (("AMOUNT", "99.00"), ("PERCENT", "10"), ("NONE", GONE)):
        done = discount(ana, quote, line, kind, value, version)  # Owner: sin límite, en borrador
        assert (done.status_code, done.json()["status"]) == (200, "DRAFT")
        version = done.json()["version"]
    assert done.json()["items"][0]["final_unit_price"] == "4399.00" and version == 4
    price = f"{QUOTES}{quote}/items/{line}/price/"
    for method in ("get", "put", "options"):  # una ruta del suelo cuenta solo con POST
        send(ana, method, price, {})
    assert migrator.execute(COUNTED).fetchone() == (0,) and lab.asked == []
    assert migrator.execute(DISCOUNTED).fetchone() == (3,)


def test_two_discounts_at_once_from_one_version_leave_one_and_one_audit_row(
    lab: SimpleNamespace, migrator: DB
) -> None:
    quote, [line] = draft(lab)
    got, values = list[Any](), iter(("1.00", "2.00"))
    with migrator.transaction():  # los dos esperan la misma fila; luego, uno gana
        migrator.execute("SELECT 1 FROM quotes WHERE id = %s FOR UPDATE", [quote])
        workers = threaded(
            2, lambda: got.append(fail(discount(signed(lab.vera), quote, line, value=next(values))))
        )
        waiting(migrator, 2)
    for worker in workers:
        worker.join(timeout=15)
    assert sorted(got, key=str) == [(200, None, []), (409, "STALE_VERSION", [])]
    assert migrator.execute(DISCOUNTED).fetchone() == (1,)

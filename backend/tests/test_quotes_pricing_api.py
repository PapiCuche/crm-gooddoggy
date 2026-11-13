"""F5-09e2: precio manual y pedir aprobación por la API con el tope del oráculo del costo, con los
roles reales, la sesión y `crm_app`: permiso, el mismo 404, la negativa auditada y el tope."""

import ast
import inspect
import logging
import pathlib
import re
from datetime import timedelta
from decimal import Decimal as D
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.test import Client
from django.utils import timezone

from apps.accounts.tasks import purge_login_throttles
from apps.pricing import floor
from apps.quotes import discounts
from apps.quotes.api import limit, urls
from apps.quotes.models import Quote, QuoteApproval
from config import env
from config.settings import base
from core.tenancy.scope import tenant_scope
from tests.test_authorization import give
from tests.test_member_branch import DENIED
from tests.test_memberships import ctx
from tests.test_pricing_engine import item, row
from tests.test_quote_discounts import cost
from tests.test_quotes_read_api import QUOTES, board, crm, pipelines, real_stack
from tests.test_quotes_write_api import DB, ROWS, create, desk, lines, threaded
from tests.test_self_context import NOT_FOUND, reply, signed
from tests.test_team_writes import send

__all__ = ["board", "crm", "desk", "pipelines", "real_stack"]  # las fixtures
pytestmark = pytest.mark.usefixtures("tenant_db")
SENTINEL = "4123.45"  # el costo que nadie debe ver
REFUSED = (422, b'{"code":"PRICE_NOT_ALLOWED","message":"Ese precio no se admite."}')


def price(client: Any, quote: Any, line: Any, value: Any = "4000.00", version: int = 1,
          org: str = "org-a", **extra: Any) -> Any:  # fmt: skip
    body = {"version": version, "price": value, "reason": "Cliente frecuente", **extra}
    return send(client, "post", f"{QUOTES.replace('org-a', org)}{quote}/items/{line}/price/", body)


def submit(client: Any, quote: Any, version: int = 1) -> Any:
    return send(client, "post", f"{QUOTES}{quote}/request-approval/", {"version": version})


def draft(desk: SimpleNamespace, who: str = "vera", *variants: Any) -> tuple[str, list[str]]:
    contact = desk.ids[{"vera": "owned", "ana": "plain", "sam": "sam"}[who]]
    sent = {"contact_id": str(contact), "items": lines(*(variants or (desk.phone,)))}
    made = create(signed(getattr(desk, who)), sent).json()
    return made["id"], [x["id"] for x in made["items"]]


def withdraw(desk: SimpleNamespace, quote: str, version: int) -> None:
    """Como retirar la solicitud (F5-09b3): de vuelta a borrador, con otra versión."""
    with tenant_scope(ctx(desk.a)):
        QuoteApproval.objects.filter(quote_id=quote, status="PENDING").update(
            status="CANCELLED", decided_at=timezone.now()
        )
        Quote.objects.filter(pk=quote).update(status="DRAFT", version=version)


def refusals(migrator: DB) -> list[Any]:
    found = "SELECT entity_id::text, result, metadata, changes::text FROM audit_logs "
    return migrator.execute(found + "WHERE action = 'quote.price_refused'").fetchall()


@pytest.fixture
def lab(desk: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """vera (Vendedora `OWN`) con `quotes.override_price` y sin ver costos; el iPhone con su costo
    y cada respuesta del suelo contada (`asked`)."""
    give(desk.a, desk.m["vera"], {"quotes.override_price": None})
    cost(desk, desk.phone, SENTINEL)
    asked: list[int] = []

    def counted(*args: Any) -> bool:
        asked.append(1)
        return floor.covers_cost(*args)

    monkeypatch.setattr(discounts, "covers_cost", counted)
    return SimpleNamespace(**vars(desk), asked=asked)


def test_permission_first_then_absent_other_org_out_of_scope_and_a_foreign_line_are_one_404(
    lab: SimpleNamespace, migrator: DB
) -> None:
    mine, [line] = draft(lab)
    for target in (mine, uuid4()):  # sin rol: 403, exista o no, y sin mirar el cuerpo
        assert reply(price(signed(lab.bea), target, line, extra=1)) == DENIED
        assert reply(submit(signed(lab.bea), target)) == DENIED
    assert reply(price(signed(lab.sam), mine, line, extra=1)) == DENIED  # sin override_price
    theirs, [their_line] = draft(lab, "ana")
    vera, before = signed(lab.vera), migrator.execute(ROWS).fetchone()
    for quote, at, version in ((theirs, their_line, 7), (uuid4(), line, 7),
                               (lab.quotes["b"], line, 7), (mine, their_line, 1),
                               (mine, uuid4(), 1)):  # fmt: skip
        assert reply(price(vera, quote, at, "1.00", version)) == NOT_FOUND
    for quote in (theirs, uuid4(), lab.quotes["b"], lab.quotes["sam"]):
        assert reply(submit(vera, quote, version=7)) == NOT_FOUND
    assert price(vera, mine, line, version=7).json()["code"] == "STALE_VERSION"  # la ve: versión
    assert migrator.execute(ROWS).fetchone() == before and refusals(migrator) == []
    assert lab.asked == []  # nada ajeno llegó al suelo


@pytest.mark.parametrize(
    ("over", "field"),
    [({"price": 4000.0}, "price"), ({"price": 4000}, "price"), ({"price": "NaN"}, "price"),
     ({"price": "1e3"}, "price"), ({"price": "-1"}, "price"), ({"price": "1.001"}, "price"),
     ({"price": "1000000000000"}, "price"), ({"price": True}, "price"), ({"reason": " "}, "reason"),
     ({"version": "1"}, "version"), ({"status": "APPROVED"}, "status"),
     ({"organization_id": str(uuid4())}, "organization_id"),
     ({"manual_price_override": False}, "manual_price_override")],
)  # fmt: skip
def test_a_hostile_amount_or_field_is_a_400_on_its_field_and_writes_nothing(
    lab: SimpleNamespace, migrator: DB, over: dict[str, Any], field: str
) -> None:
    quote, [line] = draft(lab)
    before, body = migrator.execute(ROWS).fetchone(), {"version": 1, "price": "1.00", "reason": "x"}
    sent = send(signed(lab.vera), "post", f"{QUOTES}{quote}/items/{line}/price/", body | over)
    assert (sent.status_code, list(sent.json()["fields"])) == (400, [field])
    assert migrator.execute(ROWS).fetchone() == before and lab.asked == []


def test_a_price_over_the_cost_asks_for_approval_and_a_plain_draft_is_approved_on_submit(
    lab: SimpleNamespace,
) -> None:
    quote, [line] = draft(lab)
    done = price(signed(lab.vera), quote, line, "4200.00")
    shown = (done.status_code, done.json()["status"], done.json()["version"])
    assert shown == (200, "PENDING_APPROVAL", 2) and SENTINEL not in done.content.decode()
    assert submit(signed(lab.vera), quote, 2).json()["code"] == "QUOTE_NOT_DRAFT"
    assert submit(signed(lab.vera), draft(lab)[0]).json()["status"] == "APPROVED"


def test_every_refusal_is_the_same_422_audited_after_the_rollback_with_ids_only(
    lab: SimpleNamespace, migrator: DB, caplog: pytest.LogCaptureFixture
) -> None:
    [(listed,)] = migrator.execute("SELECT DISTINCT price_list_id FROM product_prices").fetchall()
    later = timezone.now() + timedelta(days=3)
    bare, late = item(lab.a, "BARE"), item(lab.a, "LATE")  # sin costo; el costo sube después
    for variant in (bare, late):
        row(lab.a, (variant, listed), "500", timezone.now() - timedelta(9))
    cost(lab, lab.case, "1.00", currency="USD")  # en otra moneda
    cost(lab, late, "300", ends_at=later)
    quote, [phone, case, plain] = draft(lab, "vera", lab.phone, lab.case, bare)
    resend, [line] = draft(lab, "vera", late)
    vera = signed(lab.vera)
    with caplog.at_level(logging.DEBUG):
        sent = [price(vera, quote, phone, "4123.44"), price(vera, quote, case, "99.00"),
                price(vera, quote, plain, "49.00")]  # fmt: skip
        assert price(vera, resend, line, "400.00").status_code == 200
        withdraw(lab, resend, 3)
        cost(lab, late, "450", starts_at=later)  # programado: el precio ya no lo cubre
        sent.append(submit(vera, resend, 3))
    assert {reply(s) for s in sent} == {REFUSED}
    assert len({tuple(h for h in s.headers.items() if h[0] != "X-Request-ID") for s in sent}) == 1
    audited = refusals(migrator)
    wanted = [(quote, "price", x) for x in (phone, case, plain)] + [
        (resend, "request-approval", None)
    ]
    assert audited == [(q, "DENIED", {"route": r, "line_id": x}, "{}") for q, r, x in wanted]
    shown = str(migrator.execute("SELECT t::text FROM audit_logs t").fetchall()) + caplog.text
    assert "4123." not in shown  # con el punto: un uuid puede llevar 4123
    assert migrator.execute("SELECT version FROM quotes WHERE id = %s", [quote]).fetchone() == (1,)


def test_an_audit_that_fails_leaves_the_same_refusal_and_a_line_without_values(
    lab: SimpleNamespace, migrator: DB, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:  # fmt: skip
    def broken(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError(SENTINEL)

    monkeypatch.setattr(limit, "record", broken)
    quote, [line] = draft(lab)
    with caplog.at_level(logging.INFO):
        assert reply(price(signed(lab.vera), quote, line, "1.00")) == REFUSED
    assert "RuntimeError" in caplog.text and SENTINEL not in caplog.text
    assert refusals(migrator) == []


def test_the_cap_cuts_a_cost_bisection_and_the_429_says_nothing_about_the_quote(
    lab: SimpleNamespace, migrator: DB, settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings.QUOTE_FLOOR_THROTTLE = {"quote": (4, 3600), "user": (7, 86400)}
    vera, low, high = signed(lab.vera), D("0.01"), D("4398.99")
    quote, [line] = draft(lab)
    version, codes = 1, []
    for _ in range(9):  # búsqueda binaria; retira la solicitud tras cada sí
        guess = ((low + high) / 2).quantize(D("0.01"))
        sent = price(vera, quote, line, str(guess), version)
        codes.append(sent.status_code)
        if sent.status_code == 200:
            high, version = guess, version + 2
            withdraw(lab, quote, version)
        elif sent.status_code == 422:
            low = guess
    assert codes[4:] == [429] * 5 and len(lab.asked) == 4
    assert high - low >= D("4398.98") / 2**4  # el intervalo nunca llega al céntimo
    other, [at] = draft(lab)  # otra cotización: el tope por persona ya se alcanzó
    fixed = f"'{timezone.now().isoformat()}'::timestamptz"  # un solo instante: la espera no oscila
    monkeypatch.setattr(limit, "_COUNT", limit._COUNT.replace("clock_timestamp()", fixed))
    sam, [sam_line] = draft(lab, "sam")
    limited = [price(vera, other, at, "1.00"), submit(vera, other), price(vera, uuid4(), at),
               submit(vera, sam), price(vera, lab.quotes["b"], at), price(vera, quote, line),
               price(vera, sam, sam_line, org="org-b")]  # fmt: skip
    assert {(r.status_code, r.content, r["Retry-After"]) for r in limited} == {
        (429, b'{"code":"RATE_LIMITED"}', limited[0]["Retry-After"])
    }
    assert int(limited[0]["Retry-After"]) % 60 == 0 and len(lab.asked) == 4
    assert reply(price(signed(lab.ana), other, at, "1.00")) == REFUSED  # otra persona, no


def test_a_burst_at_once_is_evaluated_at_most_the_cap_and_a_variant_path_shares_the_key(
    lab: SimpleNamespace, settings: Any
) -> None:
    settings.QUOTE_FLOOR_THROTTLE = {"quote": (3, 3600), "user": (100, 86400)}
    quote, [line] = draft(lab)
    got: list[int] = []
    for worker in threaded(8, lambda: got.append(price(signed(lab.vera), quote, line).status_code)):
        worker.join(timeout=30)
    assert sorted(got) == [422] * 3 + [429] * 5 and len(lab.asked) == 3
    path = f"{QUOTES}{quote}/items/{line}/price/"
    for variant in (path.replace(quote, quote.upper()), path.replace("-", "%2D"),
                    path.replace("quotes/", "quotes//"), path[:-1], path + "/"):  # fmt: skip
        sent = send(signed(lab.vera), "post", variant, {"version": 1, "price": "1", "reason": "x"})
        assert sent.status_code in (301, 404, 429), variant
    assert len(lab.asked) == 3


def test_the_purge_keeps_a_counter_inside_its_window_and_nobody_anonymous_counts(
    lab: SimpleNamespace, migrator: DB
) -> None:
    quote, [line] = draft(lab)
    assert reply(price(signed(lab.bea), quote, line)) == DENIED  # cuenta: solo contra ella
    assert price(Client(), quote, line).status_code == 401  # sin sesión: no cuenta
    keys = "SELECT key FROM login_throttles WHERE key LIKE 'qfl:%' ORDER BY key"
    assert len(migrator.execute(keys).fetchall()) == 2  # bea: ella misma; el anónimo, nada
    migrator.execute("UPDATE login_throttles SET updated_at = now() - interval '20 minutes'")
    purge_login_throttles()
    assert len(migrator.execute(keys).fetchall()) == 2  # la purga del acceso no lo borra
    migrator.execute("UPDATE login_throttles SET updated_at = now() - interval '25 hours'")
    purge_login_throttles()
    assert migrator.execute(keys).fetchall() == []


def test_every_quote_route_that_reaches_the_cost_floor_is_counted() -> None:
    """Toda función de `apps.quotes` que llega a `covers_cost` o `_floor` (también por otra) y que
    llama una vista de su API está en una ruta que `limit.PATH` cuenta."""
    calls: dict[str, set[str]] = {}
    for source in pathlib.Path(discounts.__file__).parent.glob("*.py"):
        for node in ast.walk(ast.parse(source.read_text())):
            if isinstance(node, ast.FunctionDef):
                names = {getattr(n, "attr", getattr(n, "id", "")) for n in ast.walk(node)}
                calls.setdefault(node.name, set()).update(names)
    reach: set[str] = set()
    while (grown := {n for n, used in calls.items() if used & (reach | {"covers_cost"})}) != reach:
        reach = grown
    assert {"_floor", "set_manual_price", "submit_quote", "approve_quote", "issue_quote"} <= reach
    checked = []
    for pattern in urls.urlpatterns:
        for method in ("post", "put", "patch", "delete"):
            if handler := getattr(pattern.callback.view_class, method, None):
                seen = inspect.getclosurevars(handler)
                used = {
                    *seen.unbound,
                    *(getattr(v, "__name__", "") for v in seen.nonlocals.values()),
                }
                path = re.sub(r"<uuid:\w+>", str(uuid4()), f"/api/v1/o/org-a/{pattern.pattern}")
                assert not used & reach or limit.PATH.match(path), path
                checked += [path] if used & reach else []
    assert len(checked) == 2  # el precio y pedir aprobación


def test_the_floor_limit_refuses_a_setting_it_cannot_enforce() -> None:
    env.check_floor_throttle(base.QUOTE_FLOOR_THROTTLE)  # el de omisión, 6/h y 30/día, vale
    assert base.QUOTE_FLOOR_THROTTLE == {"quote": (6, 3600), "user": (30, 86400)}
    day, bad = 86400, ((0, 3600), (31, 3600), (6, 59))  # cero, sobre el de la persona, < 60 s
    for quote, user in [*((q, (30, day)) for q in bad), ((6, 3600), (101, day)),
                        ((6, 3600), (30, 1800)), ((6, 3600), (30, 8 * day))]:  # fmt: skip
        with pytest.raises(ImproperlyConfigured, match="QUOTE_FLOOR_THROTTLE"):
            env.check_floor_throttle({"quote": quote, "user": user})

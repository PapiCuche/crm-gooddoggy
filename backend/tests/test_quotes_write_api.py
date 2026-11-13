"""F5-09e (corte A): crear y cambiar cotizaciones por la API (`…/quotes/`), con las plantillas de
rol reales (ana Owner, vera Vendedora `OWN`, sam Supervisor `TEAM`, bea sin rol, luis de B), el
middleware, la sesión y PostgreSQL con `crm_app`. Las negativas primero: permiso antes de leer,
alcance (el mismo 404), otra organización, versión, cuerpo cerrado, el mismo 400 para un vínculo
que no se ve, sin esperas por lo ajeno, carreras con hilos, reversión y errores sin cifras."""

import json
import logging
import threading
from datetime import timedelta
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from django.db import DataError, connection
from django.test import Client
from django.utils import timezone

from apps.quotes import answers, commands, decisions, discounts, lifecycle
from apps.quotes.api.errors import CONFLICTS, LINKS, domain
from apps.quotes.models import Quote
from core.api.errors import ApiError
from core.tenancy.scope import tenant_scope
from tests.test_authorization import give
from tests.test_member_branch import DENIED
from tests.test_memberships import ctx
from tests.test_opportunities_tables import make
from tests.test_prices import price_list
from tests.test_pricing_engine import item, row
from tests.test_quotes_read_api import QUOTES, UNAUTHENTICATED, board, crm, pipelines, real_stack
from tests.test_self_context import NOT_FOUND, reply, signed
from tests.test_team_writes import send

__all__ = ["board", "crm", "pipelines", "real_stack"]  # las fixtures, para pytest
pytestmark = pytest.mark.usefixtures("tenant_db")
DB = psycopg.Connection[Any]
ROWS = "SELECT (SELECT count(*) FROM quotes), (SELECT count(*) FROM audit_logs)"
NOTE = {"version": 1, "internal_notes": "x"}


@pytest.fixture
def desk(board: SimpleNamespace, migrator: DB) -> SimpleNamespace:
    """Sobre `board`: la lista predeterminada de A con dos variantes con precio, y la numeración
    después de las de `board` (escritas a mano)."""
    migrator.execute("INSERT INTO org_sequences VALUES (%s, 'COT', 900000)", [board.a])
    listed, since = price_list(board.a, "DESK", is_default=True), timezone.now() - timedelta(9)
    phone, case = item(board.a, "IP16"), item(board.a, "CASE")
    row(board.a, (phone, listed), "4399", since)
    row(board.a, (case, listed), "100.50", since)
    return SimpleNamespace(**vars(board), phone=phone, case=case)


def lines(*variants: Any) -> list[dict[str, Any]]:
    return [{"variant_id": str(v), "quantity": 1} for v in variants]


def create(client: Client, body: Any) -> Any:
    return send(client, "post", QUOTES, body)


def patch(client: Client, quote: Any, body: Any) -> Any:
    return send(client, "patch", f"{QUOTES}{quote}/", body)


def threaded(count: int, call: Any) -> list[threading.Thread]:
    def go() -> None:
        try:
            call()
        finally:
            connection.close()

    workers = [threading.Thread(target=go) for _ in range(count)]
    for worker in workers:
        worker.start()
    return workers


def fail(response: Any) -> tuple[int, str | None, list[str]]:
    sent = response.json()
    return response.status_code, sent.get("code"), sorted(sent.get("fields", {}))


def test_creating_answers_the_detail_audits_and_prices_the_lines(
    desk: SimpleNamespace, migrator: DB
) -> None:
    made = create(signed(desk.vera), {
        "contact_id": str(desk.ids["owned"]), "notes_to_customer": "Entrega DNI 12345678",
        "items": [{"variant_id": str(desk.phone), "quantity": 2}, *lines(desk.case)],
        "valid_until": "2026-12-31T23:59:00-05:00",
    })  # fmt: skip
    assert made.status_code == 201, made.content
    body = made.json()
    assert (body["status"], body["version"], body["total"]) == ("DRAFT", 1, "8898.50")
    assert (body["created_by_user_id"], body["contact"]["display_name"]) == (
        str(desk.vera.pk), "De vera",
    )  # fmt: skip
    assert [x["line_total"] for x in body["items"]] == ["8798.00", "100.50"]
    assert signed(desk.vera).get(f"{QUOTES}{body['id']}/").json() == body  # el mismo detalle
    found = migrator.execute("SELECT action, changes::text FROM audit_logs").fetchall()
    assert [a for a, _ in found] == ["quote.created"] and "12345678" not in found[0][1]
    opp = make(desk.a, contact_id=desk.ids["owned"], assigned_user_id=desk.vera.pk)
    alone = create(signed(desk.vera), {"opportunity_id": str(opp.pk), "items": lines(desk.case)})
    assert alone.json()["contact"]["id"] == str(desk.ids["owned"])  # el de la oportunidad


def test_a_link_the_creator_cannot_see_is_one_identical_400_and_writes_nothing(
    desk: SimpleNamespace, migrator: DB
) -> None:
    vera, before, ids = signed(desk.vera), migrator.execute(ROWS).fetchone(), desk.ids
    mine = {"contact_id": ids["owned"], "assigned_user_id": desk.vera.pk}
    opps = [make(desk.a, contact_id=ids["plain"]),  # ni suya ni de un contacto suyo
            make(desk.a, **{**mine, "contact_id": ids["plain"]}),  # suya, su contacto no lo ve
            make(desk.a, **mine, deleted_at=timezone.now()), make(desk.b)]  # fmt: skip
    bad = [
        *({"contact_id": str(c)} for c in (ids["plain"], ids["sam"], ids["b"], ids["deleted"])),
        *({"opportunity_id": str(o.pk)} for o in opps),
        {"contact_id": str(uuid4())}, {"opportunity_id": str(uuid4())},
        {"opportunity_id": str(make(desk.a, **mine).pk), "contact_id": str(ids["created"])},
    ]  # fmt: skip
    sent = {create(vera, {**b, "items": lines(desk.phone)}).content for b in bad}
    shape = {"code": "VALIDATION_ERROR", "fields": {"_": [{"code": "invalid", "message": LINKS}]}}
    assert sent == {json.dumps(shape, ensure_ascii=False, separators=(",", ":")).encode()}
    assert migrator.execute(ROWS).fetchone() == before  # un solo cuerpo, nada escrito
    for case in ({"price_list_id": str(uuid4())}, {"items": lines(uuid4())}):
        wanted = {"contact_id": str(ids["owned"]), "items": lines(desk.phone), **case}
        assert fail(create(vera, wanted))[2] == [next(iter(case)).replace("items", "variant_id")]


def test_the_permission_comes_before_the_body_and_the_row(
    desk: SimpleNamespace, migrator: DB
) -> None:
    for target in (desk.quotes["vera"], uuid4(), desk.quotes["b"]):
        assert reply(patch(signed(desk.bea), target, {"bogus": 1})) == DENIED
    assert reply(create(signed(desk.bea), {"bogus": 1})) == DENIED  # ni se mira el cuerpo
    give(desk.a, desk.m["bea"], {"quotes.create": None, "quotes.update": "ORGANIZATION"})
    assert reply(create(signed(desk.bea), {"bogus": 1})) == DENIED  # crear sin ver no basta
    assert reply(patch(signed(desk.bea), desk.quotes["vera"], NOTE)) == DENIED  # cambiar tampoco
    assert reply(create(Client(), NOTE)) == UNAUTHENTICATED
    assert reply(patch(signed(desk.luis), desk.quotes["vera"], NOTE)) == NOT_FOUND  # no es de A
    assert migrator.execute("SELECT count(*) FROM audit_logs").fetchone() == (0,)


@pytest.mark.parametrize(
    ("who", "reach"),
    [("vera", {"vera"}), ("sam", {"vera", "sam"}), ("ana", {"vera", "sam", "ana", "nobody"})],
)
def test_out_of_scope_other_org_and_absent_are_the_same_404_even_with_a_stale_version(
    desk: SimpleNamespace, migrator: DB, who: str, reach: set[str]
) -> None:
    client, stale = signed(getattr(desk, who)), {**NOTE, "version": 7}
    for key, quote in desk.quotes.items():
        if key not in reach:
            assert reply(patch(client, quote, stale)) == NOT_FOUND, key
            continue
        assert fail(patch(client, quote, stale))[:2] == (409, "STALE_VERSION")  # la ve: versión
        assert patch(client, quote, NOTE).json()["version"] == 2
    assert reply(patch(client, uuid4(), NOTE)) == NOT_FOUND
    changed = migrator.execute("SELECT count(*) FROM quotes WHERE version = 2").fetchone()
    assert changed == (len(reach),)


def test_the_view_scope_and_the_update_scope_are_crossed(desk: SimpleNamespace) -> None:
    give(desk.a, desk.m["bea"], {"quotes.view": "ORGANIZATION", "quotes.update": "OWN"})
    assert reply(patch(signed(desk.bea), desk.quotes["ana"], NOTE)) == NOT_FOUND  # la ve, no más
    give(desk.a, desk.m["vera"], {"quotes.update": "ORGANIZATION"})
    assert reply(patch(signed(desk.vera), desk.quotes["ana"], NOTE)) == NOT_FOUND  # ve `OWN`


@pytest.mark.parametrize(
    "extra",
    [{"organization_id": str(uuid4())}, {"status": "APPROVED"}, {"number": "COT-999999"},
     {"total": "1.00"}, {"revision": 2}, {"created_by_user_id": str(uuid4())}, {"currency": "USD"},
     {"cancelled_at": "2026-01-01T00:00:00Z"}],
)  # fmt: skip
def test_nothing_the_server_owns_comes_from_the_body(
    desk: SimpleNamespace, migrator: DB, extra: dict[str, Any]
) -> None:
    before, ana, key = migrator.execute(ROWS).fetchone(), signed(desk.ana), next(iter(extra))
    sent = {"contact_id": str(desk.ids["plain"]), "items": lines(desk.phone)}
    assert fail(create(ana, {**sent, **extra})) == (400, "VALIDATION_ERROR", [key])
    assert fail(patch(ana, desk.quotes["ana"], {**NOTE, **extra}))[2] == [key]
    priced = [{"variant_id": str(desk.phone), "quantity": 1, "unit_price": "1.00"}]
    assert fail(create(ana, {**sent, "items": priced}))[2] == ["items"]  # el precio, tampoco
    assert migrator.execute(ROWS).fetchone() == before


def _line(quantity: Any) -> dict[str, Any]:
    return {"items": [{"variant_id": "PHONE", "quantity": quantity}]}


@pytest.mark.parametrize(
    ("bad", "field"),
    [(_line(1.0), "items"), (_line("1"), "items"), (_line(True), "items"), (_line(0), "items"),
     (_line(1_000_001), "items"), (_line(None), "items"), ({"items": []}, "items"),
     ({"items": "x"}, "items"), ({"items": _line(1)["items"] * 2}, "items"),
     ({"items": [{"variant_id": str(uuid4()), "quantity": 1} for _ in range(51)]}, "items"),
     ({"valid_until": "2026-12-31T23:59:00"}, "valid_until"),
     ({"valid_until": "1999-12-31T23:59:00Z"}, "valid_until"),
     ({"notes_to_customer": 5}, "notes_to_customer"), ({"version": "1"}, "version"),
     ({"version": True}, "version"), ({"version": 0}, "version")],
)  # fmt: skip
def test_a_hostile_body_is_a_400_on_its_field_and_writes_nothing(
    desk: SimpleNamespace, migrator: DB, bad: dict[str, Any], field: str
) -> None:
    before = migrator.execute(ROWS).fetchone()
    body = json.loads(json.dumps({"version": 1, **bad}).replace("PHONE", str(desk.phone)))
    assert fail(patch(signed(desk.ana), desk.quotes["ana"], body)) == (
        400, "VALIDATION_ERROR", [field],
    )  # fmt: skip
    assert migrator.execute(ROWS).fetchone() == before


def test_only_a_draft_is_changed_and_the_lines_are_priced_again(desk: SimpleNamespace) -> None:
    ana = signed(desk.ana)
    made = create(ana, {"contact_id": str(desk.ids["plain"]), "items": lines(desk.phone)}).json()
    swapped = patch(ana, made["id"], {"version": 1, "items": lines(desk.case, desk.phone)}).json()
    assert ([x["sku"] for x in swapped["items"]], swapped["total"]) == (["CASE", "IP16"], "4499.50")
    with tenant_scope(ctx(desk.a, desk.ana)) as tenant:  # el Owner, sin límite: `APPROVED`
        discounts.submit_quote(tenant, quote_id=UUID(made["id"]), version=2)
    assert fail(patch(ana, made["id"], {"version": 3, "notes_to_customer": "x"}))[:2] == (
        409, "QUOTE_NOT_DRAFT",
    )  # fmt: skip


def test_a_row_out_of_scope_is_not_waited_for(desk: SimpleNamespace, migrator: DB) -> None:
    """Quien no alcanza la cotización (ni el contacto, al crear) no espera el bloqueo de otro: el
    tiempo no cuenta si existe."""
    got: list[Any] = []

    def go() -> None:
        vera, sent = signed(desk.vera), {"contact_id": str(desk.ids["plain"])}
        got.append(reply(patch(vera, desk.quotes["ana"], NOTE)))
        got.append(create(vera, {**sent, "items": lines(desk.phone)}).status_code)

    with migrator.transaction():
        migrator.execute("SELECT 1 FROM quotes WHERE id = %s FOR UPDATE", [desk.quotes["ana"]])
        migrator.execute("SELECT 1 FROM contacts WHERE id = %s FOR UPDATE", [desk.ids["plain"]])
        [worker] = threaded(1, go)
        worker.join(timeout=15)
        assert not worker.is_alive() and got == [NOT_FOUND, 400]


def test_two_changes_at_once_from_one_version_leave_one(
    desk: SimpleNamespace, migrator: DB
) -> None:
    quote, got = desk.quotes["ana"], list[Any]()
    with migrator.transaction():  # los dos esperan la misma fila; luego, uno gana
        migrator.execute("SELECT 1 FROM quotes WHERE id = %s FOR UPDATE", [quote])
        workers = threaded(2, lambda: got.append(fail(patch(signed(desk.ana), quote, NOTE))[:2]))
        threading.Event().wait(1)
    for worker in workers:
        worker.join(timeout=15)
    assert sorted(got, key=str) == [(200, None), (409, "STALE_VERSION")]
    assert migrator.execute(
        "SELECT count(*) FROM audit_logs WHERE action = 'quote.updated'"
    ).fetchone() == (1,)


def test_a_failure_after_the_write_rolls_the_request_back(
    desk: SimpleNamespace, migrator: DB, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("audit down")

    monkeypatch.setattr(commands, "record", broken)
    ana, before = signed(desk.ana), migrator.execute(ROWS).fetchone()
    ana.raise_request_exception = False
    sent = {"contact_id": str(desk.ids["plain"]), "items": lines(desk.phone)}
    assert create(ana, sent).status_code == patch(ana, desk.quotes["ana"], NOTE).status_code == 500
    assert migrator.execute(ROWS).fetchone() == before
    assert migrator.execute("SELECT max(version) FROM quotes").fetchone() == (1,)


def test_a_data_error_is_a_bare_500_and_the_log_has_no_value(
    desk: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    class Cause(Exception):
        sqlstate = "22P02"

    def hostile(*args: Any, **kwargs: Any) -> None:
        raise DataError('invalid input syntax for type uuid: "DNI 12345678"') from Cause()

    monkeypatch.setattr(commands, "update_quote", hostile)
    with caplog.at_level(logging.INFO):
        sent = patch(signed(desk.ana), desk.quotes["ana"], NOTE)
    assert reply(sent) == (500, b'{"code":"INTERNAL_ERROR"}')
    assert "22P02" in caplog.text and "12345678" not in caplog.text


@pytest.mark.parametrize(("error", "code"), [(kind(), code) for kind, code, _ in CONFLICTS])
def test_every_domain_error_has_its_code_and_no_figures(error: Exception, code: str) -> None:
    with pytest.raises(ApiError) as raised, domain():
        raise error
    assert (raised.value.status_code, raised.value.code) == (409, code)
    assert not any(ch.isdigit() for ch in str(raised.value.message))


def test_the_mapper_covers_every_command_exception_and_hides_their_text() -> None:
    mapped = {kind for kind, _, _ in CONFLICTS}
    special = (commands.BadReference, commands.NotAllowed, discounts.PriceRefused)
    for module in (commands, decisions, discounts, lifecycle, answers):
        for value in vars(module).values():
            if isinstance(value, type) and value.__module__ == module.__name__:
                assert not issubclass(value, Exception) or value in (*mapped, *special), value
    for error, status in ((ValueError("812.40"), 400), (Quote.DoesNotExist(), 404),
                          (commands.NotAllowed("quotes.update"), 403),
                          (discounts.PriceRefused(), 422)):  # fmt: skip
        with pytest.raises(Exception) as raised, domain():
            raise error
        assert raised.value.status_code == status  # type: ignore[attr-defined]
        assert "812" not in str(raised.value.detail)  # type: ignore[attr-defined]

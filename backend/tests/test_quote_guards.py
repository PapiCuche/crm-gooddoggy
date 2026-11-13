"""F5-09b: `discount_policies` y `quote_approvals` (RLS, FKs compuestas, CHECK, solo lo que escribe
`crm_app`) y las protecciones en la base de la cotización: identidad, importes y líneas congelados
al salir del borrador, el grafo de estados y la solicitud de aprobación abierta. PostgreSQL real."""

from decimal import Decimal as D
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest
from django.core.management import call_command
from django.db import DatabaseError, IntegrityError, ProgrammingError, connection, transaction
from django.utils import timezone

from apps.access.models import Role
from apps.quotes.models import DiscountPolicy, Quote, QuoteApproval, QuoteItem
from core.tenancy.context import current
from core.tenancy.scope import tenant_scope
from tests import test_opportunities_tables as tables
from tests.conftest import migrator_conninfo
from tests.factories import make_user
from tests.test_memberships import ctx, join
from tests.test_pipelines_tables import as_migrator
from tests.test_prices import price_list
from tests.test_quote_tables import issued, line, quote

world = tables.world
pytestmark = pytest.mark.usefixtures("tenant_db")
type DB = psycopg.Connection[Any]
_n = iter(range(1, 10_000))
LEGAL = {
    "DRAFT": {"PENDING_APPROVAL", "APPROVED", "SENT", "CANCELLED"},
    "PENDING_APPROVAL": {"DRAFT", "APPROVED", "CANCELLED"},
    "APPROVED": {"SENT", "CANCELLED"},
    "SENT": {"VIEWED", "ACCEPTED", "REJECTED", "EXPIRED", "CANCELLED"},
    "VIEWED": {"ACCEPTED", "REJECTED", "EXPIRED", "CANCELLED"},
}
ALL = {
    "DRAFT",
    "PENDING_APPROVAL",
    "APPROVED",
    "SENT",
    "VIEWED",
    "ACCEPTED",
    "REJECTED",
    "EXPIRED",
    "CANCELLED",
}
CHANGE = "UPDATE quotes SET {} WHERE id = %s"


def role(org: Any, code: str = "") -> Any:
    with tenant_scope(ctx(org)):
        return Role.objects.create(code=code or f"r{next(_n)}", name="Rol").pk


def policy(world: SimpleNamespace, **over: Any) -> DiscountPolicy:
    fields: dict[str, Any] = {"role_id": role(world.a), "max_discount_percent": D("10")}
    with tenant_scope(ctx(world.a)):
        made: DiscountPolicy = DiscountPolicy.objects.create(**{**fields, **over})
    return made


def approval(world: SimpleNamespace, made: Quote, **over: Any) -> QuoteApproval:
    fields: dict[str, Any] = {
        "quote_id": made.pk,
        "reason": "DISCOUNT_OVER_LIMIT",
        "requested_by_user_id": world.ana.pk,
        "requested_discount_percent": D("12"),
        "limit_percent_at_request": D("10"),
        "submitted_by_user_id": world.ana.pk,  # quien la envía es quien actúa (el disparador)
    }
    with tenant_scope(ctx(world.a)), connection.cursor() as cursor:  # quien actúa: Ana
        cursor.execute("SELECT set_config('app.user_id', %s, true)", [str(world.ana.pk)])
        created: QuoteApproval = QuoteApproval.objects.create(**{**fields, **over})
    return created


def go(world: SimpleNamespace, made: Quote, *states: str) -> None:
    """Lleva la cotización por esos estados, como el runtime (`crm_app`), con sus disparadores. La
    respuesta del cliente la registra una persona (Ana) con su fecha; para vencerla, antes el
    migrador deja su validez en el pasado (F5-09c3)."""
    for state in states:
        at = timezone.now()  # la respuesta, con su fecha
        said = {"rejected_at": at, "rejection_reason": "No"}
        extra = {"SENT": issued(), "ACCEPTED": {"accepted_at": at}, "REJECTED": said}.get(state, {})
        if state == "EXPIRED":
            with psycopg.connect(migrator_conninfo(), autocommit=True) as owner:
                owner.execute("UPDATE quotes SET sent_at = now() - interval '2 days', valid_until"
                              " = now() - interval '1 day' WHERE id = %s"
                              " AND status IN ('SENT', 'VIEWED')", [made.pk])  # fmt: skip
        if current() is not None:  # dentro de `fails`: en su transacción, que deshace lo que haga
            Quote.objects.filter(pk=made.pk).update(status=state, **extra)
            continue
        with tenant_scope(ctx(world.a, world.ana if state in ("ACCEPTED", "REJECTED") else None)):
            Quote.objects.filter(pk=made.pk).update(status=state, **extra)


def fails(world: SimpleNamespace, match: str, work: Any) -> None:
    with tenant_scope(ctx(world.a)), pytest.raises(IntegrityError, match=match):
        with transaction.atomic():
            work()


def decide(world: SimpleNamespace, found: QuoteApproval, **fields: Any) -> None:
    with tenant_scope(ctx(world.a)):
        QuoteApproval.objects.filter(pk=found.pk).update(**fields)


def test_a_quote_is_born_a_draft_even_by_sql(world: SimpleNamespace) -> None:
    mine = quote(world)
    insert = (
        "INSERT INTO quotes (id, organization_id, number, revision, contact_id, price_list_id,"
        " status, currency, tax_mode, tax_rate, subtotal, discount_total, tax_total, total,"
        " version, created_at, updated_at, notes_to_customer, internal_notes) VALUES"
        " (gen_random_uuid(), %s, 'COT-900001', 1, %s, %s, %s, 'PEN', 'INCLUDED', 0.18, 0, 0, 0,"
        " 0, 1, now(), now(), '', '')"
    )
    for state in ("APPROVED", "SENT", "PENDING_APPROVAL", "CANCELLED"):
        with tenant_scope(ctx(world.a)), connection.cursor() as cursor:
            with pytest.raises(IntegrityError, match="born a DRAFT"), transaction.atomic():
                cursor.execute(insert, [world.a, mine.contact_id, mine.price_list_id, state])


def test_a_temporary_table_cannot_stand_in_for_the_quote_the_guards_read(
    world: SimpleNamespace,
) -> None:
    """`crm_app` crea tablas temporales: sin `pg_temp` al final del `search_path`, una `quotes` o
    una `quote_approvals` falsas harían creer a las guardas que la cotización es un borrador o que
    hay una solicitud abierta o aprobada."""
    sent, row = quote(world), None
    row = line(world, sent)
    go(world, sent, "SENT")
    with tenant_scope(ctx(world.a)), connection.cursor() as cursor:
        cursor.execute(
            "CREATE TEMP TABLE quotes (organization_id uuid, id uuid, status text) ON COMMIT DROP"
        )
        cursor.execute("INSERT INTO pg_temp.quotes VALUES (%s, %s, 'DRAFT')", [world.a, sent.pk])
        with pytest.raises(IntegrityError, match="lines change only in DRAFT"):
            cursor.execute("UPDATE quote_items SET sku_snapshot = 'X' WHERE id = %s", [row.pk])
    draft = quote(world, number="COT-900002")
    with tenant_scope(ctx(world.a)), connection.cursor() as cursor:
        cursor.execute(
            "CREATE TEMP TABLE quote_approvals (organization_id uuid, quote_id uuid, status text)"
            " ON COMMIT DROP"
        )
        cursor.execute(
            "INSERT INTO pg_temp.quote_approvals VALUES (%s, %s, 'PENDING'), (%s, %s, 'APPROVED')",
            [world.a, draft.pk, world.a, draft.pk],
        )
        with pytest.raises(IntegrityError, match="needs an open request"):
            cursor.execute(
                "UPDATE quotes SET status = 'PENDING_APPROVAL' WHERE id = %s", [draft.pk]
            )


def test_a_policy_for_a_price_list_needs_its_currency(world: SimpleNamespace) -> None:
    listed = price_list(world.a, "SINMONEDA")
    fails(
        world,
        "discount_policies_list_currency_ck",
        lambda: policy(world, applies_to_price_list_id=listed, currency=None),
    )  # la FK compuesta no mira un `NULL`: lo impone el `CHECK`


@pytest.fixture
def boss(world: SimpleNamespace) -> Any:
    """Otra persona de A: la que decide lo que pidió `ana`."""
    user = make_user()
    join(world.a, user)
    return user


def test_the_new_tables_are_isolated_by_row_level_security_even_by_sql(
    world: SimpleNamespace,
) -> None:
    made = quote(world)
    policy(world)
    approval(world, made)
    for table in ("discount_policies", "quote_approvals"):
        count = f"SELECT count(*) FROM {table}"  # noqa: S608
        for org, expected in ((world.a, 1), (world.b, 0)):
            with tenant_scope(ctx(org)), connection.cursor() as cursor:
                cursor.execute(count)
                assert cursor.fetchone() == (expected,)
    with tenant_scope(ctx(world.b)), connection.cursor() as cursor:
        cursor.execute("UPDATE quote_approvals SET comment = 'otra'")
        assert cursor.rowcount == 0  # ni las cambia
    for statement in (
        "TRUNCATE discount_policies",
        "DELETE FROM quote_approvals",
        "TRUNCATE quote_approvals",
    ):
        with tenant_scope(ctx(world.a)), connection.cursor() as cursor:
            with pytest.raises(ProgrammingError, match="permission denied"):
                cursor.execute(statement)


def test_a_policy_keeps_its_references_in_the_organization_and_is_unique(
    world: SimpleNamespace,
) -> None:
    mine, listed = policy(world), price_list(world.a, "VIP")
    foreign_role = role(world.b)
    fails(world, "discount_policies_role_fk", lambda: policy(world, role_id=foreign_role))
    other = price_list(world.b, "AJENA")
    fails(
        world,
        "discount_policies_price_list_fk",
        lambda: policy(world, applies_to_price_list_id=other, currency="PEN"),
    )
    fails(
        world,
        "discount_policies_price_list_fk",  # la moneda es la de la lista
        lambda: policy(world, applies_to_price_list_id=listed, currency="USD"),
    )
    fails(world, "discount_policies_all_lists_uq", lambda: policy(world, role_id=mine.role_id))
    assert policy(world, role_id=mine.role_id, applies_to_price_list_id=listed, currency="PEN")
    fails(
        world,
        "discount_policies_list_uq",
        lambda: policy(
            world, role_id=mine.role_id, applies_to_price_list_id=listed, currency="PEN"
        ),
    )


@pytest.mark.parametrize(
    ("fields", "constraint"),
    [
        ({"max_discount_percent": D("100.01")}, "discount_policies_percent_ck"),
        ({"max_discount_percent": D("-1")}, "discount_policies_percent_ck"),
        ({"max_discount_amount": D("50")}, "discount_policies_amount_ck"),  # sin moneda
        ({"max_discount_amount": D("-1"), "currency": "PEN"}, "discount_policies_amount_ck"),
        ({"version": 0}, "discount_policies_version_ck"),
    ],
)
def test_the_database_refuses_a_bad_policy(
    world: SimpleNamespace, fields: dict[str, Any], constraint: str
) -> None:
    fails(world, constraint, lambda: policy(world, **fields))
    assert policy(
        world,
        max_discount_percent=D("100"),
        max_discount_amount=D("0"),
        currency="PEN",
        role_id=role(world.a),
    )


def test_an_approval_keeps_its_references_and_there_is_one_open_per_quote(
    world: SimpleNamespace, boss: Any
) -> None:
    made = quote(world)
    fails(
        world,
        "quote_approvals_requested_by_fk",
        lambda: approval(world, made, requested_by_user_id=world.luis.pk),
    )
    foreign = quote(world, "b")  # una cotización de otra organización
    fails(world, "quote_approvals_quote_fk", lambda: approval(world, foreign))
    first = approval(world, made)
    fails(world, "quote_approvals_one_pending_uq", lambda: approval(world, made))
    decide(
        world,
        first,
        status="REJECTED",
        decided_by_user_id=boss.pk,
        decided_at=timezone.now(),
        comment="Muy bajo",
    )
    assert approval(world, made).status == "PENDING"  # decidida la anterior, otra se abre


@pytest.mark.parametrize(
    ("fields", "constraint"),
    [
        ({"reason": "OTHER"}, "quote_approvals_reason_ck"),
        ({"status": "SOON"}, "quote_approvals_status_ck|starts open"),
        ({"reason": "AI_CREATED"}, "quote_approvals_requester_ck"),  # la IA no tiene quien pida
        ({"requested_by_user_id": None}, "quote_approvals_requester_ck"),  # una persona sí
        ({"requested_discount_percent": D("101")}, "quote_approvals_percent_ck"),
        ({"limit_percent_at_request": D("-1")}, "quote_approvals_limit_ck"),
        ({"version": 0}, "quote_approvals_version_ck"),
    ],
)
def test_the_database_refuses_a_bad_approval(
    world: SimpleNamespace, fields: dict[str, Any], constraint: str
) -> None:
    made = quote(world)
    fails(world, constraint, lambda: approval(world, made, **fields))
    assert approval(world, made, reason="AI_CREATED", requested_by_user_id=None).reason  # la IA


def test_a_decision_needs_its_author_and_date_and_the_approver_is_not_the_requester(
    world: SimpleNamespace, boss: Any
) -> None:
    now = timezone.now()
    found = approval(world, quote(world, number=f"COT-9{next(_n):05d}"))
    for state in ("APPROVED", "REJECTED"):  # sin quien ni cuándo
        fails(world, "quote_approvals_decided_ck", lambda s=state: decide(world, found, status=s))
    fails(  # quien pidió no se aprueba a sí mismo
        world,
        "quote_approvals_separation_ck",
        lambda: decide(
            world, found, status="APPROVED", decided_at=now, decided_by_user_id=world.ana.pk
        ),
    )
    ok = approval(world, quote(world, number="COT-900100"))
    fails(
        world,
        "quote_approvals_separation_ck",  # rechazar sin motivo
        lambda: decide(world, ok, status="REJECTED", decided_at=now, decided_by_user_id=boss.pk),
    )
    decide(world, ok, status="APPROVED", decided_at=now, decided_by_user_id=boss.pk)
    cancelled = approval(world, quote(world, number="COT-900101"))
    decide(world, cancelled, status="CANCELLED", decided_at=now)  # el sistema la retira
    ai = approval(
        world, quote(world, number="COT-900102"), reason="AI_CREATED", requested_by_user_id=None
    )
    decide(world, ai, status="APPROVED", decided_at=now, decided_by_user_id=boss.pk)


def test_a_decision_is_final_and_what_was_requested_never_changes(
    world: SimpleNamespace, boss: Any
) -> None:
    open_ = approval(world, quote(world))
    for change in (
        {"reason": "MANUAL_PRICE"},
        {"requested_discount_percent": D("1")},
        {"limit_percent_at_request": D("99")},
        {"requested_by_user_id": boss.pk},
        {"quote_id": quote(world, number="COT-900200").pk},
    ):
        fails(world, "request never changes", lambda c=change: decide(world, open_, **c))
    decide(world, open_, status="APPROVED", decided_by_user_id=boss.pk, decided_at=timezone.now())
    fails(world, "decision is final", lambda: decide(world, open_, comment="otra cosa"))
    fails(world, "decision is final", lambda: decide(world, open_, status="REJECTED"))


def test_an_approval_starts_open_on_a_quote_that_is_not_approved_yet(
    world: SimpleNamespace, boss: Any
) -> None:
    done = approval(world, quote(world))
    now = timezone.now()
    fails(
        world,
        "starts open",
        lambda: approval(
            world, quote(world, number="COT-900300"), status="CANCELLED", decided_at=now
        ),
    )
    sent = quote(world, number="COT-900301")
    go(world, sent, "SENT")
    fails(world, "starts open", lambda: approval(world, sent))  # una ya enviada no se aprueba
    assert done.status == "PENDING"


def test_the_identity_of_a_quote_never_changes_for_the_runtime_role(world: SimpleNamespace) -> None:
    made = quote(world)
    other = {
        "number": "COT-777777",
        "revision": 2,
        "contact_id": tables.contact(world.a).pk,
        "price_list_id": price_list(world.a, "OTRA"),
        "created_by_user_id": None,
        "created_at": timezone.now(),
    }
    for field, value in other.items():
        fails(
            world,
            "identity columns never change",
            lambda f=field, v=value: Quote.objects.filter(pk=made.pk).update(**{f: v}),
        )
    with tenant_scope(ctx(world.a)):  # lo demás de un borrador sí cambia
        assert Quote.objects.filter(pk=made.pk).update(notes_to_customer="x", total=D("0")) == 1


CHANGES = {
    "currency": "USD",
    "tax_mode": "EXCLUDED",
    "tax_rate": D("0.19"),
    "subtotal": D("1"),
    "discount_total": D("1"),
    "tax_total": D("1"),
    "total": D("1"),
}


PATHS = {
    "DRAFT": [],
    "PENDING_APPROVAL": ["PENDING_APPROVAL"],
    "APPROVED": ["APPROVED"],
    "SENT": ["SENT"],
    "VIEWED": ["SENT", "VIEWED"],
    "ACCEPTED": ["SENT", "ACCEPTED"],
    "REJECTED": ["SENT", "REJECTED"],
    "EXPIRED": ["SENT", "EXPIRED"],
}


def reach(world: SimpleNamespace, state: str) -> Quote:
    """Una cotización con importes, en ese estado, llegada por un camino legal."""
    made = quote(
        world,
        number=f"COT-8{next(_n):05d}",
        subtotal=D("84.75"),
        tax_total=D("15.25"),
        total=D("100"),
        discount_total=D("20"),
    )
    if state == "CANCELLED":
        with tenant_scope(ctx(world.a)):
            Quote.objects.filter(pk=made.pk).update(status=state, cancelled_at=timezone.now())
        return made
    if state == "PENDING_APPROVAL":
        approval(world, made)
    go(world, made, *PATHS[state])
    return made


@pytest.mark.parametrize("state", ["DRAFT", "PENDING_APPROVAL", "APPROVED", "SENT"])
def test_the_amounts_change_only_while_the_quote_is_a_draft(
    world: SimpleNamespace, state: str
) -> None:
    made = reach(world, state)
    with tenant_scope(ctx(world.a)):  # lo que no es dinero cambia siempre
        assert Quote.objects.filter(pk=made.pk).update(internal_notes="visto") == 1
    if state == "DRAFT":
        with tenant_scope(ctx(world.a)):
            assert (
                Quote.objects.filter(pk=made.pk).update(
                    total=D("1"), subtotal=D("1"), tax_total=D("0")
                )
                == 1
            )
        return
    for field, value in CHANGES.items():
        fails(
            world,
            "amounts are frozen",
            lambda f=field, v=value: Quote.objects.filter(pk=made.pk).update(**{f: v}),
        )


@pytest.mark.parametrize("old", sorted(ALL))
def test_the_status_graph_is_enforced_by_the_database(world: SimpleNamespace, old: str) -> None:
    for new in sorted(ALL - LEGAL.get(old, set()) - {old}):
        made = reach(world, old)
        fails(world, "status change is not allowed", lambda m=made, n=new: go(world, m, n))


def test_the_legal_paths_and_the_open_request_go_together(world: SimpleNamespace) -> None:
    boss, now = make_user(), timezone.now()
    join(world.a, boss)
    made = quote(world)
    fails(world, "needs an open request", lambda: go(world, made, "PENDING_APPROVAL"))
    asked = approval(world, made)
    go(world, made, "PENDING_APPROVAL")
    for target in ("APPROVED", "DRAFT", "CANCELLED"):  # con la solicitud abierta, ninguno
        fails(world, "decide the open request first", lambda t=target: go(world, made, t))
    fails(world, "status change is not allowed", lambda: go(world, made, "SENT"))
    decide(
        world, asked, status="REJECTED", decided_by_user_id=boss.pk, decided_at=now, comment="Baja"
    )
    fails(
        world, "needs the latest request approved", lambda: go(world, made, "APPROVED")
    )  # rechazada
    go(world, made, "DRAFT")  # el rechazo la devuelve al borrador
    again = approval(world, made)
    go(world, made, "PENDING_APPROVAL")
    decide(world, again, status="APPROVED", decided_by_user_id=boss.pk, decided_at=now)
    go(world, made, "APPROVED", "SENT", "VIEWED", "ACCEPTED")
    direct = quote(world, number="COT-900400")  # dentro del límite: sin solicitud
    go(world, direct, "APPROVED", "SENT")


def test_an_old_approval_does_not_approve_a_later_request(
    world: SimpleNamespace, boss: Any
) -> None:
    made, now = quote(world), timezone.now()
    first = approval(world, made)
    go(world, made, "PENDING_APPROVAL")
    decide(world, first, status="APPROVED", decided_by_user_id=boss.pk, decided_at=now)
    go(world, made, "DRAFT")  # se edita después de aprobada: hace falta otra solicitud
    second = approval(world, made)
    go(world, made, "PENDING_APPROVAL")
    decide(world, second, status="CANCELLED", decided_at=now)  # el sistema la retira
    fails(world, "latest request approved", lambda: go(world, made, "APPROVED"))  # la vieja no vale


def test_a_draft_with_an_open_request_cannot_skip_to_approved_or_sent(
    world: SimpleNamespace,
) -> None:
    made = quote(world)
    approval(world, made)
    for target in ("APPROVED", "SENT"):
        fails(world, "decide the open request first", lambda t=target: go(world, made, t))


def test_the_lines_change_only_while_the_quote_is_a_draft(world: SimpleNamespace) -> None:
    made = quote(world)
    row = line(world, made)
    spare = line(world, made, sort_order=2)
    with tenant_scope(ctx(world.a)):  # en borrador: cambian y se reemplazan
        assert QuoteItem.objects.filter(pk=spare.pk).update(quantity=3, line_total=D("300")) == 1
        QuoteItem.objects.filter(pk=spare.pk).delete()
    go(world, made, "APPROVED")
    fails(world, "lines change only in DRAFT", lambda: line(world, made, sort_order=3))
    fails(
        world,
        "lines change only in DRAFT",
        lambda: QuoteItem.objects.filter(pk=row.pk).update(quantity=1, line_total=D("100")),
    )
    fails(world, "lines change only in DRAFT", lambda: QuoteItem.objects.filter(pk=row.pk).delete())
    other = reach(world, "PENDING_APPROVAL")
    fails(world, "lines change only in DRAFT", lambda: line(world, other, sort_order=9))


def test_a_manual_price_has_its_reason_and_nothing_else_does(world: SimpleNamespace) -> None:
    made = quote(world)
    row = line(world, made)
    for fields in (
        {"manual_price_override": True},
        {"manual_price_override": True, "override_reason": "  "},
        {"override_reason": "Sin marca"},
    ):
        fails(
            world,
            "quote_items_override_ck",
            lambda f=fields: QuoteItem.objects.filter(pk=row.pk).update(**f),
        )
    with tenant_scope(ctx(world.a)):
        manual = {
            "manual_price_override": True,
            "override_reason": "Cliente mayorista",
            "final_unit_price": D("90"),
            "line_total": D("180"),
        }
        assert QuoteItem.objects.filter(pk=row.pk).update(**manual) == 1


def test_the_guards_do_not_stop_the_migrator_who_maintains_the_database(
    world: SimpleNamespace, migrator: DB
) -> None:
    """Vigilan al rol del runtime: quien migra (o borra una organización) no pasa por ellos."""
    made = quote(world, number="COT-900500")
    row = line(world, made)
    go(world, made, "APPROVED")
    migrator.execute(
        "UPDATE quote_items SET quantity = 7, line_total = 700 WHERE id = %s", [row.pk]
    )
    migrator.execute(
        "UPDATE quotes SET total = 5, subtotal = 4, tax_total = 1 WHERE id = %s", [made.pk]
    )
    assert migrator.execute("SELECT total FROM quotes WHERE id = %s", [made.pk]).fetchone() == (5,)
    with pytest.raises(
        DatabaseError, match="permission denied|must be owner"
    ):  # y crm_app no los quita
        with tenant_scope(ctx(world.a)), connection.cursor() as cursor:
            cursor.execute("ALTER TABLE quotes DISABLE TRIGGER quotes_guard")


def test_the_second_migration_goes_back_and_forward_with_its_triggers(
    world: SimpleNamespace, migrator: DB
) -> None:
    tables_now = (
        "SELECT to_regclass('public.quote_approvals') IS NOT NULL,"
        " to_regclass('public.discount_policies') IS NOT NULL,"
        " (SELECT count(*) FROM pg_trigger WHERE tgname LIKE 'quote%_guard')"
    )
    assert migrator.execute(tables_now).fetchone() == (True, True, 3)
    with as_migrator():
        call_command("migrate", "quotes", "0001", verbosity=0)
    assert migrator.execute(tables_now).fetchone() == (False, False, 0)
    with as_migrator():
        call_command("migrate", verbosity=0)
    assert migrator.execute(tables_now).fetchone() == (True, True, 3)

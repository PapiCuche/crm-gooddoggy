"""F5-03: las tablas `opportunities`, `opportunity_items` y `opportunity_stage_history` y el
número `OPP-…`, con PostgreSQL real y el rol `crm_app`. Los comandos llegan con F5-03b y F5-03c."""

import threading
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from django.db import DatabaseError, IntegrityError, connection
from django.db.migrations.loader import MigrationLoader
from django.utils import timezone

from apps.catalog.models import Product, ProductVariant
from apps.contacts.models import Contact
from apps.deals.models import (
    LostReason,
    Opportunity,
    OpportunityItem,
    OpportunityStageHistory,
    Pipeline,
    PipelineStage,
)
from apps.deals.numbers import next_opportunity_number
from apps.deals.seed import ensure_default_pipeline
from core.tenancy.scope import tenant_scope
from tests.factories import make_user
from tests.test_memberships import ctx, join

pytestmark = pytest.mark.usefixtures("tenant_db")
TABLES = ("opportunities", "opportunity_items", "opportunity_stage_history")


@pytest.fixture
def world(orgs: dict[str, Any]) -> SimpleNamespace:
    """`ana` es miembro de A y `luis` solo de B; cada organización, con su pipeline minorista."""
    ana, luis = make_user(), make_user()
    join(orgs["A"], ana)
    join(orgs["B"], luis)
    for org in ("A", "B"):
        with tenant_scope(ctx(orgs[org])) as tenant:
            ensure_default_pipeline(tenant)  # también siembra los motivos (`PRICE`…)
    return SimpleNamespace(a=orgs["A"], b=orgs["B"], ana=ana, luis=luis)


def stage(org: UUID, code: str = "nuevo") -> Any:
    with tenant_scope(ctx(org)):
        return PipelineStage.objects.get(code=code, pipeline__code="retail")


def contact(org: UUID) -> Any:
    with tenant_scope(ctx(org)):
        return Contact.objects.create(display_name="Cliente")


def variant(org: UUID, sku: str = "IP16") -> Any:
    with tenant_scope(ctx(org)):
        product = Product.objects.create(name=sku, slug=sku.lower(), product_type="PHYSICAL")
        return ProductVariant.objects.create(product=product, sku=sku, name=sku)


def reason(org: UUID) -> Any:
    with tenant_scope(ctx(org)):
        return LostReason.objects.get(code="PRICE")


def fields(org: UUID, **over: Any) -> dict[str, Any]:
    """Las columnas mínimas de una oportunidad abierta en «Nuevo» (sin número: lo pone el test)."""
    nuevo, now = stage(org), timezone.now()
    return {
        "contact_id": contact(org).pk, "pipeline_id": nuevo.pipeline_id, "stage_id": nuevo.pk,
        "title": "iPhone 16", "currency": "PEN", "probability": 10, "last_activity_at": now,
        "stage_entered_at": now, **over,
    }  # fmt: skip


def make(org: UUID, **over: Any) -> Any:
    with tenant_scope(ctx(org)) as tenant:
        number = over.pop("number", None) or next_opportunity_number(tenant)
        return Opportunity.objects.create(number=number, **fields(org, **over))


def refused(org: UUID, constraint: str, make_row: Any) -> None:
    """La base rechaza lo que `make_row` escribe, nombrando la restricción."""
    with tenant_scope(ctx(org)), pytest.raises(IntegrityError, match=constraint):
        make_row()


def sql(org: UUID, statement: str, params: list[Any] | None = None) -> list[Any]:
    with tenant_scope(ctx(org)), connection.cursor() as cursor:
        cursor.execute(statement, params)
        return cursor.fetchall() if cursor.description else []


def test_what_a_creates_never_exists_for_b(world: SimpleNamespace) -> None:
    opp = make(world.a)
    nuevo = stage(world.a)
    with tenant_scope(ctx(world.a)):
        OpportunityItem.objects.create(opportunity=opp, variant=variant(world.a), quantity=1)
        OpportunityStageHistory.objects.create(
            opportunity=opp, to_stage=nuevo, changed_at=timezone.now()
        )
    with tenant_scope(ctx(world.b)):
        assert not Opportunity.objects.exists() and not OpportunityItem.objects.exists()
        assert not OpportunityStageHistory.objects.exists()
    for table in TABLES:  # RLS, no solo el manager
        assert len(sql(world.a, f"SELECT 1 FROM {table}")) == 1  # noqa: S608
        assert sql(world.b, f"SELECT 1 FROM {table}") == []  # noqa: S608
    with pytest.raises(DatabaseError):  # por SQL, como `crm_app`, tampoco se escribe en la ajena
        sql(
            world.b,
            "INSERT INTO opportunity_stage_history (id, organization_id, opportunity_id, "
            "to_stage_id, changed_at) VALUES (%s, %s, %s, %s, now())",
            [uuid4(), world.a, opp.pk, nuevo.pk],
        )
    assert sql(world.b, "UPDATE opportunities SET title = 'x'") == []  # y no ve nada que cambiar


def test_composite_foreign_keys_keep_every_reference_in_the_organization(
    world: SimpleNamespace,
) -> None:
    mine, theirs = make(world.a), stage(world.b)
    other = {"contact_id": contact(world.b).pk}
    their_reason, their_sku = reason(world.b).pk, variant(world.b).pk
    lost_stage = stage(world.a, "perdido").pk
    spare_sku = variant(world.b, "OTRO").pk  # los objetos de B, creados fuera del tenant A
    refused(world.a, "opportunities_contact_fk", lambda: make(world.a, **other))
    refused(
        world.a,  # el pipeline y la etapa de B, con su estado (la FK de la etapa también cae)
        "opportunities_(pipeline|stage)_fk",
        lambda: make(world.a, pipeline_id=theirs.pipeline_id, stage_id=theirs.pk),
    )
    refused(
        world.a,
        "opportunities_lost_reason_fk",
        lambda: make(
            world.a,
            status="LOST",
            stage_id=lost_stage,
            probability=0,
            lost_at=timezone.now(),
            lost_reason_id=their_reason,
        ),
    )
    for who in ("assigned", "created_by", "updated_by", "deleted_by"):  # `luis` no es de A
        refused(
            world.a,
            f"opportunities_{who}_fk",
            lambda w=who: make(world.a, **{f"{w}_user_id": world.luis.pk}),
        )
    assert make(world.a, assigned_user_id=world.ana.pk).assigned_user_id == world.ana.pk
    refused(
        world.a,
        "opportunity_items_variant_fk",
        lambda: OpportunityItem.objects.create(opportunity=mine, variant_id=their_sku, quantity=1),
    )
    refused(
        world.b,  # la oportunidad de A, desde B
        "opportunity_items_opportunity_fk",
        lambda: OpportunityItem.objects.create(opportunity=mine, variant_id=spare_sku, quantity=1),
    )
    now = timezone.now()
    refused(
        world.a,
        "opp_history_to_stage_fk",
        lambda: OpportunityStageHistory.objects.create(
            opportunity=mine, to_stage=theirs, changed_at=now
        ),
    )
    refused(
        world.a,
        "opp_history_from_stage_fk",
        lambda: OpportunityStageHistory.objects.create(
            opportunity=mine,
            from_stage=theirs,
            to_stage=stage(world.a),
            changed_at=now,
            seconds_in_previous_stage=1,
        ),  # fmt: skip
    )
    refused(
        world.a,
        "opp_history_changed_by_fk",
        lambda: OpportunityStageHistory.objects.create(
            opportunity=mine,
            to_stage=stage(world.a),
            changed_at=timezone.now(),
            changed_by_user_id=world.luis.pk,
        ),  # fmt: skip
    )


def test_the_stage_belongs_to_the_pipeline_and_the_status_is_its_type(
    world: SimpleNamespace,
) -> None:
    with tenant_scope(ctx(world.a)):
        other = Pipeline.objects.create(name="Otro", code="otro", kind="OTHER", currency="PEN")
        elsewhere = PipelineStage.objects.create(
            pipeline=other, name="Nuevo", code="nuevo", stage_type="OPEN",
            default_probability=10, color="#112233",
        )  # fmt: skip
    refused(  # la etapa existe, pero no es de ese pipeline
        world.a, "opportunities_stage_fk", lambda: make(world.a, stage_id=elsewhere.pk)
    )
    ganado = stage(world.a, "ganado")
    lifecycle = {"probability": 100, "won_at": timezone.now()}
    refused(  # abierta en una etapa WON
        world.a, "opportunities_stage_fk", lambda: make(world.a, stage_id=ganado.pk)
    )
    refused(  # ganada en una etapa OPEN
        world.a, "opportunities_stage_fk", lambda: make(world.a, status="WON", **lifecycle)
    )
    won = make(world.a, stage_id=ganado.pk, status="WON", **lifecycle)
    assert won.status == "WON"
    with tenant_scope(ctx(world.a)), pytest.raises(IntegrityError, match="opportunities_stage_fk"):
        PipelineStage.objects.filter(pk=ganado.pk).update(stage_type="OPEN")  # en uso


@pytest.mark.parametrize(
    ("constraint", "over"),
    [
        ("opportunities_status_ck", {"won_at": "now"}),  # abierta con fecha de ganada
        ("opportunities_status_ck", {"lost_at": "now"}),
        ("opportunities_status_ck", {"lost_reason_note": "x"}),
        ("opportunities_status_ck", {"status": "WON", "stage": "ganado", "probability": 100}),
        ("opportunities_status_ck", {"status": "LOST", "stage": "perdido", "probability": 0}),
        (  # perdida sin motivo
            "opportunities_status_ck",
            {"status": "LOST", "stage": "perdido", "probability": 0, "lost_at": "now"},
        ),
        (  # perdida y ganada
            "opportunities_status_ck",
            {"status": "LOST", "stage": "perdido", "probability": 0, "lost_at": "now",
             "won_at": "now", "reason": True},
        ),
        (  # ganada con motivo de pérdida
            "opportunities_status_ck",
            {"status": "WON", "stage": "ganado", "probability": 100, "won_at": "now",
             "reason": True},
        ),
        ("opportunities_probability_ck", {"probability": 101}),
        (
            "opportunities_probability_ck",
            {"status": "WON", "stage": "ganado", "probability": 90, "won_at": "now"},
        ),
        (
            "opportunities_probability_ck",
            {"status": "LOST", "stage": "perdido", "probability": 5, "lost_at": "now",
             "reason": True},
        ),
    ],
)  # fmt: skip
def test_the_status_fixes_the_dates_the_reason_and_the_probability(
    world: SimpleNamespace, constraint: str, over: dict[str, Any]
) -> None:
    values = dict(over)
    if "stage" in values:
        chosen = stage(world.a, values.pop("stage"))
        values["stage_id"] = chosen.pk
    if values.pop("reason", False):
        values["lost_reason_id"] = reason(world.a).pk
    for key in ("won_at", "lost_at"):
        if values.get(key) == "now":
            values[key] = timezone.now()
    refused(world.a, constraint, lambda: make(world.a, **values))


def test_closed_opportunities_are_valid_when_every_piece_agrees(world: SimpleNamespace) -> None:
    now = timezone.now()
    won = make(world.a, status="WON", stage_id=stage(world.a, "ganado").pk, probability=100,
               won_at=now)  # fmt: skip
    lost = make(
        world.a, status="LOST", stage_id=stage(world.a, "perdido").pk, probability=0, lost_at=now,
        lost_reason_id=reason(world.a).pk, lost_reason_note="Caro",
    )  # fmt: skip
    assert (won.status, lost.status, lost.lost_reason_note) == ("WON", "LOST", "Caro")


@pytest.mark.parametrize(
    ("constraint", "over"),
    [
        ("opportunities_number_ck", {"number": "OPP-12"}),
        ("opportunities_number_ck", {"number": "opp-000012"}),
        ("opportunities_title_ck", {"title": "   "}),
        ("opportunities_title_ck", {"title": ""}),
        ("opportunities_amount_ck", {"amount": -1}),
        ("opportunities_currency_ck", {"currency": "pen"}),
        ("opportunities_priority_ck", {"priority": "ASAP"}),
        ("opportunities_source_ck", {"source": "RADIO"}),
        ("opportunities_version_ck", {"version": 0}),
    ],
)
def test_the_database_refuses_a_bad_opportunity(
    world: SimpleNamespace, constraint: str, over: dict[str, Any]
) -> None:
    refused(world.a, constraint, lambda: make(world.a, **over))


def test_the_number_is_unique_per_organization(world: SimpleNamespace) -> None:
    make(world.a, number="OPP-000001")
    refused(world.a, "opportunities_number_uq", lambda: make(world.a, number="OPP-000001"))
    assert make(world.b, number="OPP-000001").number == "OPP-000001"  # cada organización, la suya


def test_items_are_lines_per_variant_with_a_positive_quantity(world: SimpleNamespace) -> None:
    opp, sku = make(world.a), variant(world.a)
    with tenant_scope(ctx(world.a)):
        item = OpportunityItem.objects.create(opportunity=opp, variant=sku, quantity=2)
    assert (item.estimated_unit_price, item.notes) == (0, "")
    refused(
        world.a,
        "opportunity_items_variant_uq",
        lambda: OpportunityItem.objects.create(opportunity=opp, variant=sku, quantity=1),
    )
    for constraint, over in (
        ("opportunity_items_quantity_ck", {"quantity": 0}),
        ("opportunity_items_price_ck", {"estimated_unit_price": -1}),
    ):
        refused(
            world.a,
            constraint,
            lambda o=over: OpportunityItem.objects.create(
                opportunity=opp, variant=variant(world.a, "OTRO"), **{"quantity": 1, **o}
            ),
        )


def test_the_history_starts_without_a_stage_and_never_moves_to_the_same_one(
    world: SimpleNamespace,
) -> None:
    opp, nuevo, cotizado = make(world.a), stage(world.a), stage(world.a, "cotizado")
    now = timezone.now()
    with tenant_scope(ctx(world.a)):
        OpportunityStageHistory.objects.create(opportunity=opp, to_stage=nuevo, changed_at=now)
        OpportunityStageHistory.objects.create(
            opportunity=opp, from_stage=nuevo, to_stage=cotizado, changed_at=now,
            seconds_in_previous_stage=60,
        )  # fmt: skip
    refused(  # sin etapa de origen no hay tiempo en ella, y con etapa de origen, sí
        world.a,
        "opp_history_from_seconds_ck",
        lambda: OpportunityStageHistory.objects.create(
            opportunity=opp, to_stage=nuevo, changed_at=now, seconds_in_previous_stage=5
        ),
    )
    refused(
        world.a,
        "opp_history_from_seconds_ck",
        lambda: OpportunityStageHistory.objects.create(
            opportunity=opp, from_stage=nuevo, to_stage=cotizado, changed_at=now
        ),
    )
    refused(
        world.a,
        "opp_history_moved_ck",
        lambda: OpportunityStageHistory.objects.create(
            opportunity=opp,
            from_stage=nuevo,
            to_stage=nuevo,
            changed_at=now,
            seconds_in_previous_stage=0,
        ),  # fmt: skip
    )


def test_the_runtime_role_cannot_rewrite_the_history_or_delete_opportunities(
    world: SimpleNamespace,
) -> None:
    opp, nuevo = make(world.a), stage(world.a)
    with tenant_scope(ctx(world.a)):
        OpportunityStageHistory.objects.create(
            opportunity=opp, to_stage=nuevo, changed_at=timezone.now()
        )
        OpportunityItem.objects.create(opportunity=opp, variant=variant(world.a), quantity=1)
    denied = (
        "UPDATE opportunity_stage_history SET seconds_in_previous_stage = 1",
        "UPDATE opportunity_stage_history SET changed_at = now()",
        "UPDATE opportunity_stage_history SET changed_by_user_id = NULL",
        "DELETE FROM opportunity_stage_history",
        "TRUNCATE opportunity_stage_history",
        "DELETE FROM opportunities",
        "TRUNCATE opportunities",
        "TRUNCATE opportunity_items",
    )
    for statement in denied:
        with pytest.raises(DatabaseError, match="permission denied"):
            sql(world.a, statement)
    assert len(sql(world.a, "SELECT 1 FROM opportunity_stage_history")) == 1  # intacta
    assert sql(world.a, "UPDATE opportunities SET title = 'Nuevo título'") == []  # lo demás, sí
    assert sql(world.a, "UPDATE opportunity_items SET quantity = 3") == []
    assert sql(world.a, "DELETE FROM opportunity_items") == []  # las líneas se reemplazan


def test_numbers_are_consecutive_and_distinct_under_concurrency(
    world: SimpleNamespace,
) -> None:
    threads, per_thread, seen = 8, 5, []
    start, lock = threading.Barrier(threads, timeout=30), threading.Lock()
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            start.wait()
            for _ in range(per_thread):
                with tenant_scope(ctx(world.a)) as tenant:
                    number = next_opportunity_number(tenant)
                    Opportunity.objects.create(number=number, **fields(world.a))
                with lock:
                    seen.append(number)
        except BaseException as error:  # noqa: BLE001 — se re-lanza en el hilo principal
            errors.append(error)
        finally:
            connection.close()

    pool = [threading.Thread(target=worker, daemon=True) for _ in range(threads)]
    for thread in pool:
        thread.start()
    for thread in pool:
        thread.join(timeout=120)
    assert not errors, errors
    assert not any(thread.is_alive() for thread in pool)
    total = threads * per_thread
    assert sorted(seen) == [f"OPP-{n:06d}" for n in range(1, total + 1)]  # únicos y sin huecos
    with tenant_scope(ctx(world.b)) as tenant:  # la secuencia es de cada organización
        assert next_opportunity_number(tenant) == "OPP-000001"


def test_a_rolled_back_opportunity_does_not_use_up_its_number(world: SimpleNamespace) -> None:
    class Undo(Exception):
        pass

    with pytest.raises(Undo), tenant_scope(ctx(world.a)) as tenant:
        assert next_opportunity_number(tenant) == "OPP-000001"
        raise Undo
    assert make(world.a).number == "OPP-000001"
    assert make(world.a).number == "OPP-000002"


def test_the_organization_foreign_key_rejects_an_organization_that_does_not_exist(
    world: SimpleNamespace,
) -> None:
    """Con un tenant inventado, la política de RLS deja pasar la fila y solo la FK la rechaza."""
    nobody, nuevo = uuid4(), stage(world.a)
    with tenant_scope(ctx(nobody)), pytest.raises(IntegrityError, match="organization_fk"):
        Opportunity.objects.create(
            number="OPP-000001", contact_id=uuid4(), pipeline_id=nuevo.pipeline_id,
            stage_id=nuevo.pk, title="x", currency="PEN", probability=10,
            last_activity_at=timezone.now(), stage_entered_at=timezone.now(),
        )  # fmt: skip


def test_the_migration_depends_on_what_it_uses() -> None:
    """Sin estas dependencias, `migrate deals` en una base vacía fallaría al crear las FKs."""
    ancestors = MigrationLoader(None).graph.forwards_plan(("deals", "0003_opportunities"))
    for node in (
        ("core", "0001_tenancy_functions"),
        ("organizations", "0010_invitations_are_not_deleted"),
        ("contacts", "0006_contact_tags"),
        ("catalog", "0004_variant_label"),
        ("deals", "0002_default_pipelines"),
    ):
        assert node in ancestors

"""F5-01: las tablas `pipelines`, `pipeline_stages` y `lost_reasons` y la semilla del pipeline
minorista (organizaciones nuevas y existentes), con PostgreSQL real y el rol `crm_app`."""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from django.core.management import call_command
from django.db import DatabaseError, IntegrityError, connection
from django.db.migrations.loader import MigrationLoader
from django.utils import timezone

from apps.deals.models import LostReason, Pipeline, PipelineStage
from apps.deals.seed import LOST_REASONS, STAGES, ensure_default_pipeline
from apps.provisioning import seeds
from core.tenancy.scope import tenant_scope
from tests.conftest import migrator_settings
from tests.factories import make_user
from tests.test_bootstrap import bootstrap
from tests.test_inventory_stock import race
from tests.test_memberships import ctx, join

pytestmark = pytest.mark.usefixtures("tenant_db")
TABLES = ("pipelines", "pipeline_stages", "lost_reasons")


def make_pipeline(org: UUID, code: str = "retail", **fields: Any) -> Pipeline:
    with tenant_scope(ctx(org)):
        made: Pipeline = Pipeline.objects.create(
            **{"name": code, "code": code, "kind": "RETAIL", "currency": "PEN", **fields}
        )
    return made


def make_stage(org: UUID, pipeline: Pipeline, code: str, **fields: Any) -> PipelineStage:
    wanted = {"name": code, "stage_type": "OPEN", "default_probability": 10, "color": "#112233"}
    with tenant_scope(ctx(org)):
        made: PipelineStage = PipelineStage.objects.create(
            pipeline=pipeline, code=code, **{**wanted, **fields}
        )
    return made


def refused(org: UUID, constraint: str, make: Any) -> None:
    """La base rechaza lo que `make` escribe, nombrando la restricción."""
    with tenant_scope(ctx(org)), pytest.raises(IntegrityError, match=constraint):
        make()


def rows(org: UUID, table: str) -> list[tuple[Any, ...]]:
    with tenant_scope(ctx(org)), connection.cursor() as cursor:
        cursor.execute(f"SELECT * FROM {table}")  # noqa: S608 — nombres fijos de test
        found: list[tuple[Any, ...]] = cursor.fetchall()
    return found


@pytest.fixture
def world(orgs: dict[str, Any]) -> Any:
    """`ana` es miembro de A y `luis` solo de B."""
    ana, luis = make_user(), make_user()
    join(orgs["A"], ana)
    join(orgs["B"], luis)
    return type("World", (), {"a": orgs["A"], "b": orgs["B"], "ana": ana, "luis": luis})


def test_what_a_creates_never_exists_for_b(world: Any) -> None:
    pipeline = make_pipeline(world.a)
    make_stage(world.a, pipeline, "nuevo")
    with tenant_scope(ctx(world.a)):
        LostReason.objects.create(name="Precio", code="PRICE")
    with tenant_scope(ctx(world.b)):
        assert not Pipeline.objects.exists() and not PipelineStage.objects.exists()
        assert not LostReason.objects.exists()
    for table in TABLES:  # RLS, no solo el manager
        assert len(rows(world.a, table)) == 1
        assert rows(world.b, table) == []
    with pytest.raises(DatabaseError), tenant_scope(ctx(world.b)), connection.cursor() as cursor:
        cursor.execute(  # por SQL, como `crm_app`, tampoco se escribe en la organización ajena
            "INSERT INTO lost_reasons (id, organization_id, name, code, sort_order, is_active, "
            "created_at, updated_at, version) VALUES (%s, %s, 'x', 'X', 0, true, now(), now(), 1)",
            [uuid4(), world.a],
        )


def test_composite_foreign_keys_keep_every_reference_in_the_organization(world: Any) -> None:
    other = make_pipeline(world.b, "otro")
    refused(world.a, "pipeline_stages_pipeline_fk", lambda: make_stage(world.a, other, "nuevo"))
    for who in ("created_by", "updated_by", "deleted_by"):  # `luis` no es miembro de A
        key = f"{who}_user_id"
        refused(
            world.a,
            f"pipelines_{who}_fk",
            lambda k=key: make_pipeline(world.a, **{k: world.luis.pk}),
        )
        refused(
            world.a,
            f"lost_reasons_{who}_fk",
            lambda k=key: LostReason.objects.create(name="X", code="X", **{k: world.luis.pk}),
        )
    pipeline = make_pipeline(world.a, created_by_user_id=world.ana.pk)  # un miembro sí
    assert pipeline.created_by_user_id == world.ana.pk


def test_one_live_default_and_one_live_code_per_organization(world: Any) -> None:
    first = make_pipeline(world.a, "uno", is_default=True)
    refused(world.a, "pipelines_default_uq", lambda: make_pipeline(world.a, "dos", is_default=True))
    refused(world.a, "pipelines_code_uq", lambda: make_pipeline(world.a, "uno"))
    make_pipeline(world.b, "uno", is_default=True)  # cada organización, el suyo
    with tenant_scope(ctx(world.a)):
        Pipeline.objects.filter(pk=first.pk).update(is_default=False, deleted_at=timezone.now())
    make_pipeline(world.a, "uno", is_default=True)  # el borrado no cuenta: ni marca ni código
    refused(  # el predeterminado está activo y vivo
        world.a,
        "pipelines_default_active_ck",
        lambda: make_pipeline(world.a, "tres", is_active=False, is_default=True),
    )


@pytest.mark.parametrize(
    ("constraint", "fields"),
    [
        ("pipelines_code_ck", {"code": "Retail"}),
        ("pipelines_code_ck", {"code": "retail_1"}),
        ("pipelines_currency_ck", {"currency": "pen"}),
        ("pipelines_kind_ck", {"kind": "SHOP"}),
        ("pipelines_version_ck", {"version": 0}),
    ],
)
def test_the_database_refuses_a_bad_pipeline(world: Any, constraint: str, fields: Any) -> None:
    refused(world.a, constraint, lambda: make_pipeline(world.a, **fields))


@pytest.mark.parametrize(
    ("constraint", "fields"),
    [
        ("pipeline_stages_one_won_uq", {"stage_type": "WON", "default_probability": 100}),
        ("pipeline_stages_closed_prob_ck", {"stage_type": "WON", "default_probability": 90}),
        ("pipeline_stages_closed_prob_ck", {"stage_type": "LOST", "default_probability": 10}),
        ("pipeline_stages_(type|closed_prob)_ck", {"stage_type": "DONE"}),
        ("pipeline_stages_prob_ck", {"default_probability": 101}),
        ("pipeline_stages_stale_ck", {"stale_after_days": 0}),
        ("pipeline_stages_color_ck", {"color": "red"}),
        ("pipeline_stages_color_ck", {"color": "#AABBCC"}),
        ("pipeline_stages_code_ck", {"code": "En Curso"}),
        ("pipeline_stages_requires_ck", {"requires_fields": []}),
        ("pipeline_stages_requires_ck", {"requires_fields": 3}),
        ("pipeline_stages_code_uq", {"code": "nuevo"}),
    ],
)
def test_the_database_refuses_a_bad_stage(world: Any, constraint: str, fields: Any) -> None:
    pipeline = make_pipeline(world.a)
    make_stage(world.a, pipeline, "nuevo")
    make_stage(world.a, pipeline, "ganado", stage_type="WON", default_probability=100)
    refused(
        world.a, constraint, lambda: make_stage(world.a, pipeline, **{"code": "otra", **fields})
    )
    other = make_pipeline(world.a, "otro")  # una etapa por pipeline, no por organización
    make_stage(world.a, other, "nuevo")
    make_stage(world.a, other, "ganado", stage_type="WON", default_probability=100)


def test_lost_reasons_codes(world: Any) -> None:
    with tenant_scope(ctx(world.a)):
        first = LostReason.objects.create(name="Precio", code="PRICE")
    for code, constraint in (("PRICE", "lost_reasons_code_uq"), ("price", "lost_reasons_code_ck")):
        refused(world.a, constraint, lambda c=code: LostReason.objects.create(name="X", code=c))
    refused(
        world.a,
        "lost_reasons_version_ck",
        lambda: LostReason.objects.create(name="X", code="X", version=0),
    )
    with tenant_scope(ctx(world.a)):
        LostReason.objects.filter(pk=first.pk).update(deleted_at=timezone.now())
        LostReason.objects.create(name="Precio", code="PRICE")  # el borrado libera el código


def test_the_runtime_role_cannot_delete_or_truncate(world: Any) -> None:
    make_pipeline(world.a)
    for table in TABLES:
        for statement in (f"DELETE FROM {table}", f"TRUNCATE {table}"):  # noqa: S608
            with (
                pytest.raises(DatabaseError, match="permission denied"),
                tenant_scope(ctx(world.a)),
            ):
                connection.cursor().execute(statement)
    with tenant_scope(ctx(world.a)):  # lo demás sí: se actualiza
        assert Pipeline.objects.update(name="Ventas") == 1


def summary(migrator: psycopg.Connection[Any], org: UUID) -> dict[str, Any]:
    one = migrator.execute(
        "SELECT code, kind, currency, is_default, is_active FROM pipelines "
        "WHERE organization_id = %s",
        [org],
    ).fetchall()
    stages = migrator.execute(
        "SELECT name, code, stage_type, default_probability, stale_after_days, sort_order, color "
        "FROM pipeline_stages WHERE organization_id = %s ORDER BY sort_order",
        [org],
    ).fetchall()
    reasons = migrator.execute(
        "SELECT name, code, sort_order FROM lost_reasons WHERE organization_id = %s "
        "ORDER BY sort_order",
        [org],
    ).fetchall()
    return {"pipelines": one, "stages": stages, "reasons": reasons}


IDS = "SELECT id FROM pipelines UNION ALL SELECT id FROM pipeline_stages ORDER BY 1"
EXPECTED = {
    "pipelines": [("retail", "RETAIL", "PEN", True, True)],
    "stages": [(n, c, t, p, s, i, col) for i, (n, c, t, p, s, col) in enumerate(STAGES)],
    "reasons": [(n, c, i) for i, (n, c) in enumerate(LOST_REASONS)],
}


def test_the_seed_is_the_retail_pipeline_and_runs_twice_without_duplicating(
    world: Any, migrator: psycopg.Connection[Any]
) -> None:
    with tenant_scope(ctx(world.a)) as tenant:
        assert ensure_default_pipeline(tenant) is True
    before = summary(migrator, world.a)
    assert before == EXPECTED
    kinds = [stage[2] for stage in before["stages"]]
    assert kinds.count("WON") == 1 and kinds.count("LOST") == 1 and kinds[-2:] == ["WON", "LOST"]
    assert [s[4] for s in before["stages"]] == [None, None, 7, 5, None, None]
    ids = migrator.execute(
        "SELECT id FROM pipelines UNION ALL SELECT id FROM pipeline_stages"
    ).fetchall()
    with tenant_scope(ctx(world.a)) as tenant:
        assert ensure_default_pipeline(tenant) is False
    assert summary(migrator, world.a) == before
    assert (
        migrator.execute(
            "SELECT id FROM pipelines UNION ALL SELECT id FROM pipeline_stages"
        ).fetchall()
        == ids
    )
    assert summary(migrator, world.b) == {"pipelines": [], "stages": [], "reasons": []}


def test_the_seed_does_not_overwrite_what_the_organization_set_up(
    world: Any, migrator: psycopg.Connection[Any]
) -> None:
    make_pipeline(world.a, "mayoristas", is_default=True)
    with tenant_scope(ctx(world.a)):
        reason = LostReason.objects.create(name="Mío", code="MINE")
        LostReason.objects.filter(pk=reason.pk).update(deleted_at=timezone.now())  # borrado
    with tenant_scope(ctx(world.a)) as tenant:
        assert ensure_default_pipeline(tenant) is False  # ya tiene pipeline vivo
    assert [p[0] for p in summary(migrator, world.a)["pipelines"]] == ["mayoristas"]
    assert [r[1] for r in summary(migrator, world.a)["reasons"]] == ["MINE"]  # y ya tuvo motivos


def test_an_organization_with_only_deleted_pipelines_gets_the_default(
    world: Any, migrator: psycopg.Connection[Any]
) -> None:
    old = make_pipeline(world.a, "retail", is_default=True)
    with tenant_scope(ctx(world.a)):
        Pipeline.objects.filter(pk=old.pk).update(is_default=False, deleted_at=timezone.now())
        assert ensure_default_pipeline(ctx(world.a)) is True  # el código `retail` quedó libre
    assert migrator.execute(
        "SELECT count(*), count(*) FILTER (WHERE is_default) FROM pipelines "
        "WHERE organization_id = %s AND deleted_at IS NULL",
        [world.a],
    ).fetchone() == (1, 1)


def test_the_seed_is_registered_for_new_organizations() -> None:
    assert ensure_default_pipeline in seeds._SEEDS  # si no, el alta no sembraría nada


def test_a_failing_seed_rolls_the_whole_organization_back(
    migrator: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(tenant: Any) -> None:
        raise RuntimeError("semilla rota")

    monkeypatch.setattr(seeds, "_SEEDS", [*seeds._SEEDS, boom])
    with pytest.raises(RuntimeError, match="semilla rota"):
        bootstrap()
    for table in ("organizations", "organization_memberships", "users", "pipelines"):
        assert migrator.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,)  # noqa: S608


@pytest.mark.parametrize("table", ["pipelines", "lost_reasons"])
def test_the_organization_foreign_key_rejects_an_organization_that_does_not_exist(
    table: str,
) -> None:
    """Con un tenant inventado, la política de RLS deja pasar la fila y solo la FK la rechaza."""
    nobody = uuid4()
    make = {
        "pipelines": lambda: Pipeline.objects.create(
            name="X", code="x", kind="RETAIL", currency="PEN"
        ),
        "lost_reasons": lambda: LostReason.objects.create(name="X", code="X"),
    }[table]
    refused(nobody, f"{table}_organization_fk", make)


def test_the_migrations_depend_on_what_they_use() -> None:
    """Sin `core` ni `organizations` en el grafo, `migrate deals` en una base vacía fallaría, y
    `migrate organizations zero` dejaría `deals` aplicada sin sus FKs."""
    graph = MigrationLoader(None).graph
    for node in (("deals", "0001_pipelines"), ("deals", "0002_default_pipelines")):
        ancestors = graph.forwards_plan(node)
        assert ("core", "0001_tenancy_functions") in ancestors
        assert ("organizations", "0010_invitations_are_not_deleted") in ancestors


def test_two_seeds_at_once_leave_one_pipeline(
    world: Any, migrator: psycopg.Connection[Any]
) -> None:
    seeds = race(world.a, world.ana, [ensure_default_pipeline, ensure_default_pipeline])
    assert seeds == [True, False]
    assert summary(migrator, world.a) == EXPECTED


def test_a_new_organization_is_born_with_its_pipeline(migrator: psycopg.Connection[Any]) -> None:
    made = bootstrap()  # el alta de F2-06
    assert summary(migrator, made.organization_id) == EXPECTED


@contextmanager
def as_migrator() -> Iterator[None]:
    """Las migraciones corren con `crm_migrator`, como en `django_db_setup`."""
    runtime = dict(connection.settings_dict)
    connection.close()
    keys = ("NAME", "USER", "PASSWORD", "HOST", "PORT")
    connection.settings_dict.update({k: v for k, v in migrator_settings().items() if k in keys})
    try:
        yield
    finally:
        connection.close()
        connection.settings_dict.clear()
        connection.settings_dict.update(runtime)


def test_the_data_migration_seeds_the_organizations_that_already_exist(
    world: Any, migrator: psycopg.Connection[Any]
) -> None:
    """`orgs` ya creó A y B, como si existieran antes de F5-01; B tenía un pipeline propio."""
    make_pipeline(world.b, "propio", is_default=True)
    with as_migrator():
        call_command("migrate", "deals", "0001", verbosity=0)
        call_command("migrate", "deals", verbosity=0)
    assert summary(migrator, world.a) == EXPECTED
    assert [p[0] for p in summary(migrator, world.b)["pipelines"]] == ["propio"]  # no se toca
    assert summary(migrator, world.b)["stages"] == []
    assert len(summary(migrator, world.b)["reasons"]) == len(LOST_REASONS)  # sin motivos antes
    with as_migrator():  # otra vez: sin duplicar
        call_command("migrate", "deals", "0001", verbosity=0)
        call_command("migrate", "deals", verbosity=0)
    assert summary(migrator, world.a) == EXPECTED

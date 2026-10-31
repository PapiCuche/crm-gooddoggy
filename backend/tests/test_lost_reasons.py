"""F5-01: los comandos de motivos de pérdida y el contrato de import-linter de `apps.deals`, con
PostgreSQL real y el rol `crm_app`. Sin API: llega con F5-02. Pipelines y etapas, con F5-01c."""

import json
import tomllib
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest

from apps.deals import commands
from apps.deals.models import LostReason
from core.tenancy.scope import tenant_scope
from tests.factories import make_user
from tests.test_memberships import ctx, join

pytestmark = pytest.mark.usefixtures("tenant_db")
BACKEND = Path(__file__).resolve().parent.parent


@pytest.fixture
def world(orgs: dict[str, Any]) -> SimpleNamespace:
    ana, luis = make_user(), make_user()
    join(orgs["A"], ana)
    join(orgs["B"], luis)
    return SimpleNamespace(a=orgs["A"], b=orgs["B"], ana=ana)


def run(world: SimpleNamespace, command: Callable[..., Any], **fields: Any) -> Any:
    org = fields.pop("org", "a")  # `b`: la otra organización, sin persona
    me = ctx(getattr(world, org), world.ana if org == "a" else None)
    with tenant_scope(me) as tenant:
        return command(tenant, **fields)


def audit(migrator: psycopg.Connection[Any]) -> list[tuple[str, str, dict[str, Any]]]:
    rows = migrator.execute(
        "SELECT action, entity_label, changes::text FROM audit_logs "
        "WHERE entity_type IN ('pipeline', 'stage', 'lost_reason') ORDER BY occurred_at, id"
    ).fetchall()
    return [(action, label, json.loads(changes)) for action, label, changes in rows]


def count(migrator: psycopg.Connection[Any], table: str) -> int:
    row = migrator.execute(f"SELECT count(*) FROM {table}").fetchone()  # noqa: S608
    assert row is not None
    return int(row[0])


def test_lost_reasons(world: SimpleNamespace, migrator: psycopg.Connection[Any]) -> None:
    price = run(world, commands.create_lost_reason, name=" Precio  alto ", code="price")
    stock = run(world, commands.create_lost_reason, name="Sin stock", code="OUT_OF_STOCK")
    assert (price.code, price.name, price.sort_order, stock.sort_order) == (
        "PRICE",
        "Precio alto",
        0,
        1,
    )
    with pytest.raises(commands.CodeTaken):
        run(world, commands.create_lost_reason, name="Otro", code="PRICE")
    for bad in ("1PRICE", "NO-GUION", "", "Ñ"):
        with pytest.raises(ValueError):
            run(world, commands.create_lost_reason, name="X", code=bad)
    same = run(
        world, commands.update_lost_reason, lost_reason_id=price.pk, version=1, name="Precio alto"
    )
    assert same.version == 1
    changed = run(world, commands.update_lost_reason, lost_reason_id=price.pk, version=1,
                  name="Precio", is_active=False, sort_order=5)  # fmt: skip
    assert (changed.version, changed.is_active, changed.updated_by_user_id) == (
        2,
        False,
        world.ana.pk,
    )
    with pytest.raises(commands.StaleVersion):
        run(world, commands.update_lost_reason, lost_reason_id=price.pk, version=1, name="Pisado")
    with pytest.raises(ValueError, match="no admitidos"):
        run(world, commands.update_lost_reason, lost_reason_id=price.pk, version=2, code="X")
    with pytest.raises(LostReason.DoesNotExist):  # de otra organización
        run(
            world,
            commands.update_lost_reason,
            org="b",
            lost_reason_id=price.pk,
            version=2,
            name="Y",
        )
    rows = audit(migrator)
    assert [a for a, _, _ in rows] == [
        "lost_reason.created",
        "lost_reason.created",
        "lost_reason.updated",
    ]
    assert rows[2][2] == {
        "name": ["Precio alto", "Precio"],
        "is_active": [True, False],
        "sort_order": [0, 5],
    }
    assert rows[0][2]["code"] == [None, "PRICE"]


# Endurecimiento (F5-02, de la revisión de F5-01b): tipos exactos y nada escrito.
NOT_TEXTS = [5, None, ["a"], {"a": 1}, b"x", 1.5, True]


@pytest.mark.parametrize("value", [1, 0, "true", "False", None, [], {}])
def test_is_active_is_an_exact_boolean(
    world: SimpleNamespace, migrator: psycopg.Connection[Any], value: Any
) -> None:
    made = run(world, commands.create_lost_reason, name="Precio", code="PRICE")
    with pytest.raises(ValueError, match="verdadero o falso"):
        run(world, commands.update_lost_reason, lost_reason_id=made.pk, version=1, is_active=value)
    assert [a for a, _, _ in audit(migrator)] == ["lost_reason.created"]


@pytest.mark.parametrize("value", [True, "1", 1.0, None, [1]])
def test_the_version_is_an_exact_integer(
    world: SimpleNamespace, migrator: psycopg.Connection[Any], value: Any
) -> None:
    made = run(world, commands.create_lost_reason, name="Precio", code="PRICE")
    with pytest.raises(ValueError, match="versión"):  # `True` no vale por la versión 1
        run(world, commands.update_lost_reason, lost_reason_id=made.pk, version=value, name="X")
    assert [a for a, _, _ in audit(migrator)] == ["lost_reason.created"]
    done = run(world, commands.update_lost_reason, lost_reason_id=made.pk, version=1, name="Ok")
    assert done.version == 2


@pytest.mark.parametrize("value", NOT_TEXTS)
def test_a_name_or_code_that_is_not_text_is_a_value_error(
    world: SimpleNamespace, migrator: psycopg.Connection[Any], value: Any
) -> None:
    made = run(world, commands.create_lost_reason, name="Precio", code="PRICE")
    attempts: list[Callable[[], Any]] = [
        lambda: run(world, commands.create_lost_reason, name=value, code="OTHER"),
        lambda: run(world, commands.create_lost_reason, name="Otro", code=value),
        lambda: run(
            world, commands.update_lost_reason, lost_reason_id=made.pk, version=1, name=value
        ),
    ]
    for attempt in attempts:
        with pytest.raises(ValueError):
            attempt()
    assert count(migrator, "lost_reasons") == 1
    assert len(audit(migrator)) == 1


@pytest.mark.parametrize("value", [True, 32768, -32769, "1", 1.5, None, [0]])
def test_an_update_checks_the_order_like_a_creation_does(
    world: SimpleNamespace, migrator: psycopg.Connection[Any], value: Any
) -> None:
    made = run(world, commands.create_lost_reason, name="Precio", code="PRICE")
    with pytest.raises(ValueError, match="orden"):
        run(world, commands.update_lost_reason, lost_reason_id=made.pk, version=1, sort_order=value)
    assert [a for a, _, _ in audit(migrator)] == ["lost_reason.created"]
    done = run(
        world, commands.update_lost_reason, lost_reason_id=made.pk, version=1, sort_order=-32768
    )
    assert done.sort_order == -32768  # los dos extremos que caben


def test_the_implicit_order_stops_at_the_column_limit(
    world: SimpleNamespace, migrator: psycopg.Connection[Any]
) -> None:
    run(world, commands.create_lost_reason, name="Alto", code="HIGH", sort_order=32766)
    last = run(world, commands.create_lost_reason, name="Tope", code="TOP")
    assert last.sort_order == 32767  # el último que cabe
    with pytest.raises(commands.Invalid, match="orden") as error:
        run(world, commands.create_lost_reason, name="Pasa", code="OVER")
    assert error.value.field == "sort_order"  # la API lo muestra en su campo
    assert count(migrator, "lost_reasons") == 2
    explicit = run(world, commands.create_lost_reason, name="Indicado", code="SET", sort_order=0)
    assert explicit.sort_order == 0 and len(audit(migrator)) == 3


def test_the_import_contracts_keep_the_commands_behind_their_api() -> None:
    config = tomllib.loads((BACKEND / "pyproject.toml").read_text())
    contracts = config["tool"]["importlinter"]["contracts"]
    protected = [c for c in contracts if "apps.deals.commands" in c.get("protected_modules", [])]
    assert [(c["type"], c["allowed_importers"]) for c in protected] == [
        ("protected", ["apps.deals.api"])
    ]
    layers = next(c for c in contracts if c["type"] == "layers")["layers"]
    assert "apps.inventory | apps.pricing | apps.deals" in layers  # sobre contacts, no al revés

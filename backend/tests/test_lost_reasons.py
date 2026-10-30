"""F5-01: los comandos de motivos de pérdida y el contrato de import-linter de `apps.deals`, con
PostgreSQL real y el rol `crm_app`. Sin API: llega con F5-02. Pipelines y etapas, con F5-01b."""

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


def test_the_import_contracts_keep_the_commands_behind_their_api() -> None:
    config = tomllib.loads((BACKEND / "pyproject.toml").read_text())
    contracts = config["tool"]["importlinter"]["contracts"]
    protected = [c for c in contracts if "apps.deals.commands" in c.get("protected_modules", [])]
    assert [(c["type"], c["allowed_importers"]) for c in protected] == [
        ("protected", ["apps.deals.api"])
    ]
    layers = next(c for c in contracts if c["type"] == "layers")["layers"]
    assert "apps.inventory | apps.pricing | apps.deals" in layers  # sobre contacts, no al revés

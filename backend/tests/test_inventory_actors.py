"""F3-39: un agente de IA o una integración no reserva, no libera ni transfiere stock (ADR-034), y
las rutas lo responden con un 403 `ACTOR_NOT_ALLOWED`, nunca con un 500. Y una sola regla para la
`idempotency_key` de movimientos, transferencias y reservas: tal cual, sin recortar."""

from collections.abc import Callable
from dataclasses import replace
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from apps.inventory import reservations as res
from apps.inventory import stock, transfers
from core.tenancy.context import ActorType, TenantContext
from core.tenancy.resolution import resolve_tenant
from core.tenancy.scope import tenant_scope
from tests import test_authorization, test_self_context
from tests.test_authorization import give
from tests.test_inventory_reservations import asked, consistent, ledger, release, reserve, stocked
from tests.test_inventory_reservations_api import body as reservation
from tests.test_inventory_transfers import site
from tests.test_inventory_transfers_api import body as transfer
from tests.test_products import send
from tests.test_self_context import signed

world, real_stack = test_authorization.world, test_self_context.real_stack
pytestmark = pytest.mark.usefixtures("tenant_db")
ROBOTS = [ActorType.AI_AGENT, ActorType.INTEGRATION]
BASE = "/api/v1/o/org-a/inventory/"
ALL: dict[str, str | None] = dict.fromkeys(
    ("inventory.view", "inventory.adjust", "inventory.manage", "inventory.transfer")
)
COUNTS = (
    "SELECT (SELECT count(*) FROM inventory_movements), (SELECT count(*) FROM audit_logs),"
    " (SELECT count(*) FROM inventory_reservations), (SELECT sum(reserved) FROM inventory_levels)"
)


@pytest.mark.parametrize("actor", ROBOTS)
def test_a_robot_neither_reserves_nor_releases_and_writes_nothing(
    world: Any, actor: ActorType
) -> None:
    here = stocked(world.a, 2, world.ana)
    made, _ = reserve(world.a, world.ana, asked(here))
    robot = TenantContext(world.a, "test", actor_type=actor, actor_id=uuid4())
    calls: list[Callable[[TenantContext], Any]] = [
        lambda tenant: res.reserve(tenant, asked(here)),
        lambda tenant: res.release(tenant, made.pk, "No"),
    ]
    for call in calls:
        with tenant_scope(robot) as tenant, CaptureQueriesContext(connection) as seen:
            with pytest.raises(stock.ActorRefused) as refused:
                call(tenant)
        assert (refused.value.actor_type, len(seen)) == (actor, 0)  # antes de bloquear nada
    assert consistent(world.a, here) == (2, 1, 1) and ledger(world.a, "RELEASE") == []
    assert release(world.a, world.ana, made.pk)[1] is True  # una persona, sí


def test_the_system_and_a_person_still_reserve_and_a_person_needs_her_user(world: Any) -> None:
    here = stocked(world.a, 3, world.ana)
    reserve(world.a, None, asked(here))  # el sistema (la Fase 4, tras su propio permiso)
    reserve(world.a, world.ana, asked(here))
    assert consistent(world.a, here) == (3, 2, 1)
    other = TenantContext(world.a, "test", world.ana.pk, ActorType.USER, uuid4())
    shop = site(world.a, here, "TDA")
    with tenant_scope(other) as tenant, pytest.raises(ValueError, match="user_id"):
        transfers.transfer(tenant, transfers.Wanted(*here, shop, 1, "Reponer", uuid4().hex))


def routes(where: tuple[Any, Any], target: Any) -> list[tuple[str, dict[str, Any]]]:
    return [
        ("transfers/", transfer(where[0], where[1], target)),
        ("reservations/", reservation(where)),
        (f"reservations/{uuid4()}/release/", {"reason": "Ya no"}),
    ]


@pytest.mark.parametrize("route", range(3))
def test_each_route_answers_a_refused_actor_with_a_403_and_its_code(
    world: Any, monkeypatch: pytest.MonkeyPatch, route: int
) -> None:
    """La ruta, si el servicio rechaza al actor: el 403 documentado, sin cifras ni 500."""
    give(world.a, world.membership, ALL)
    here = stocked(world.a, 2, world.ana)

    def refuse(*args: Any) -> Any:
        raise stock.ActorRefused(ActorType.AI_AGENT)

    for module, name in ((transfers, "transfer"), (res, "reserve"), (res, "release")):
        monkeypatch.setattr(module, name, refuse)
    path, sent = routes(here, site(world.a, here, "TDA"))[route]
    answer = send(signed(world.ana), "post", BASE + path, sent)
    assert answer.status_code == 403, answer.content
    assert answer.json() == {
        "code": "ACTOR_NOT_ALLOWED",
        "message": "Un agente de IA o una integración no reserva ni transfiere stock.",
    }


@pytest.mark.parametrize("actor", ROBOTS)
def test_a_robot_over_http_is_turned_away_before_the_service(
    world: Any, migrator: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch, actor: ActorType
) -> None:
    """Por HTTP la primera barrera es el permiso: a quien no es una persona, el motor de
    autorización lo trata como a un no miembro (404, el mismo de siempre), nunca un 500."""
    give(world.a, world.membership, ALL)
    here = stocked(world.a, 2, world.ana)
    monkeypatch.setattr(
        "core.tenancy.middleware.resolve_tenant",
        lambda *a: replace(resolve_tenant(*a), actor_type=actor, actor_id=uuid4()),
    )
    client, before = signed(world.ana), migrator.execute(COUNTS).fetchone()
    for path, sent in routes(here, site(world.a, here, "TDA")):
        answer = send(client, "post", BASE + path, sent)
        assert (answer.status_code, answer.json()) == (404, {"code": "NOT_FOUND"}), path
    assert migrator.execute(COUNTS).fetchone() == before


PADDED = (" clave-0001", "clave-0001 ", "\tclave-0001", "clave-0001\n", 12345678)


@pytest.mark.parametrize("key", PADDED)
def test_one_rule_for_the_key_a_padded_one_is_a_400_on_every_route(
    world: Any, migrator: psycopg.Connection[Any], key: Any
) -> None:
    """Movimientos recortaba la clave y transferencias no: las tres la toman tal cual (F3-39). El
    frontend manda 32 cifras hexadecimales, así que nunca le afecta."""
    give(world.a, world.membership, ALL)
    here = stocked(world.a, 2, world.ana)
    receipt: dict[str, Any] = {"variant_id": str(here[0]), "warehouse_id": str(here[1])}
    receipt |= {"movement_type": "RECEIPT", "quantity": 1, "reason": "Compra"}
    client, before = signed(world.ana), migrator.execute(COUNTS).fetchone()
    for path, sent in [("movements/", receipt), *routes(here, site(world.a, here, "TDA"))[:2]]:
        answer = send(client, "post", BASE + path, {**sent, "idempotency_key": key})
        assert answer.status_code == 400, (path, answer.content)
        assert list(answer.json()["fields"]) == ["idempotency_key"]
    assert migrator.execute(COUNTS).fetchone() == before

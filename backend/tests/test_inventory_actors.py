"""F3-39, F3-39b y F3-39c: un agente de IA o una integración no mueve, reserva, libera ni
transfiere stock (ADR-034), ni previsualiza, aplica o revierte una importación, y las rutas lo
responden con un 403
`ACTOR_NOT_ALLOWED`, nunca con un 400 sobre un campo ni con un 500. Un contexto de persona sin su
persona (`ActorMismatch`, así llega una tarea de Celery con `actor_type=USER`) recibe lo mismo.
Y una sola regla para la `idempotency_key` de movimientos, transferencias y reservas: tal cual,
sin recortar."""

import base64
import json
import pickle
from collections.abc import Callable
from dataclasses import replace
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from celery import current_app
from django.db import connection
from django.test.utils import CaptureQueriesContext

from apps.imports.models import DataImport
from apps.inventory import imports, stock, transfers
from apps.inventory import reservations as res
from apps.inventory.api.stock import REFUSED
from apps.inventory.models import InventoryMovement
from core.tenancy.context import ActorType, TenantContext
from core.tenancy.resolution import resolve_tenant
from core.tenancy.scope import tenant_scope
from tests import test_authorization, test_self_context
from tests.tenancy_app.tasks import stock_actor
from tests.test_authorization import give
from tests.test_inventory_reservations import asked, consistent, ledger, release, reserve, stocked
from tests.test_inventory_reservations_api import body as reservation
from tests.test_inventory_stock import level, wanted
from tests.test_inventory_transfers import site
from tests.test_inventory_transfers_api import body as transfer
from tests.test_memberships import ctx
from tests.test_products import send
from tests.test_self_context import signed
from tests.test_stock_import_preview import preview

world, real_stack = test_authorization.world, test_self_context.real_stack
pytestmark = pytest.mark.usefixtures("tenant_db")
ROBOTS = [ActorType.AI_AGENT, ActorType.INTEGRATION]
CSV = "sku,almacen,cantidad\nIP16,ALM,5\n"
BASE = "/api/v1/o/org-a/inventory/"
ALL: dict[str, str | None] = dict.fromkeys(
    ("inventory.view", "inventory.adjust", "inventory.manage", "inventory.transfer")
)
COUNTS = (
    "SELECT (SELECT count(*) FROM inventory_movements), (SELECT count(*) FROM audit_logs),"
    " (SELECT count(*) FROM inventory_reservations), (SELECT sum(reserved) FROM inventory_levels),"
    " (SELECT array_agg(status ORDER BY id) FROM data_imports)"
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
    with tenant_scope(other) as tenant, pytest.raises(stock.ActorMismatch, match="user_id"):
        transfers.transfer(tenant, transfers.Wanted(*here, shop, 1, "Reponer", uuid4().hex))


def test_a_refused_actor_is_never_a_value_error() -> None:
    """Las rutas convierten ciertos `ValueError` en un 400 sobre un campo (`expires_at` en
    reservas): un actor rechazado, ni el desacuerdo de una persona, nunca debe caer ahí."""
    assert not issubclass(stock.ActorRefused, ValueError)
    assert issubclass(stock.ActorMismatch, stock.ActorRefused)
    assert stock.ActorMismatch().actor_type == ActorType.USER


@pytest.mark.parametrize("error", [stock.ActorRefused(ActorType.AI_AGENT), stock.ActorMismatch()])
def test_a_refused_actor_is_rebuilt_from_its_args(error: stock.ActorRefused) -> None:
    """Celery devuelve la excepción de una tarea como `cls(*args)` con JSON (F3-39c), y con pickle
    también la reconstruye: el mismo tipo, el mismo mensaje y el mismo actor, nunca otra."""
    backend = current_app.backend
    sent = json.loads(json.dumps(backend.prepare_exception(error, "json")))
    rebuilt = [
        type(error)(*error.args),
        pickle.loads(pickle.dumps(error)),  # noqa: S301 — así la devuelve Celery con pickle
        backend.exception_to_python(sent),
    ]
    for again in rebuilt:
        assert (type(again), str(again)) == (type(error), str(error))
        assert (again.args, again.actor_type) == (error.args, error.actor_type)


@pytest.mark.parametrize("actor", ROBOTS)
def test_a_robot_neither_moves_nor_previews_applies_or_reverts_an_import(
    world: Any, actor: ActorType
) -> None:
    """La misma regla en `stock.move` (F3-39b): rechazado antes de la primera consulta. Y en la
    importación, antes de tocar ninguna fila (cada una pasa por `stock.move`) y antes de guardar
    una previsualización (F3-39c)."""
    here = stocked(world.a, 2, world.ana)
    job = preview(world.a, world.ana, CSV)
    robot = TenantContext(world.a, "test", actor_type=actor, actor_id=uuid4())
    calls: list[Callable[[TenantContext], Any]] = [
        lambda tenant: stock.move(tenant, wanted(here, "RECEIPT", 1)),
        lambda tenant: imports.apply(tenant, import_id=job.pk),
        lambda tenant: imports.revert(tenant, import_id=job.pk),
        lambda tenant: imports.preview(tenant, file_name="conteo.csv", data=CSV.encode()),
    ]
    for call in calls:
        with tenant_scope(robot) as tenant, CaptureQueriesContext(connection) as seen:
            with pytest.raises(stock.ActorRefused) as refused:
                call(tenant)
        assert (refused.value.actor_type, len(seen)) == (actor, 0)
    assert level(world.a, here) == (2, 0, 2) and ledger(world.a, "ADJUSTMENT") == []
    with tenant_scope(ctx(world.a)):
        assert list(DataImport.objects.values_list("pk", flat=True)) == [job.pk]


def test_the_system_and_a_person_still_move_and_apply_an_import(world: Any) -> None:
    """La importación de stock sigue pasando: la aplica una persona (la ruta) o el sistema."""
    here = stocked(world.a, 1)  # el sistema
    for user, count in ((None, 4), (world.ana, 6)):
        job = preview(world.a, world.ana, f"sku,almacen,cantidad\nIP16,ALM,{count}\n")
        with tenant_scope(ctx(world.a, user)) as tenant:
            assert imports.apply(tenant, import_id=job.pk).status == "APPLIED"
        assert level(world.a, here) == (count, 0, count)
    with tenant_scope(ctx(world.a)):
        found = InventoryMovement.objects.order_by("id")
        assert list(found.values_list("created_by_type", "created_by_user_id")) == [
            ("SYSTEM", None),
            ("SYSTEM", None),
            ("USER", world.ana.pk),
        ]


def test_a_celery_task_moves_stock_as_the_system_never_for_a_person(world: Any) -> None:
    """`@tenant_task` arma el contexto con `user_id=None` (tenancy-context §3), así que una tarea
    encolada con `actor_type=USER` recibe `ActorMismatch` (403 si llegara a una ruta): las que
    mueven, reservan o transfieren stock actúan como `SYSTEM`, el valor por defecto (F3-39b)."""
    org = str(world.a)
    assert stock_actor.apply(kwargs={"organization_id": org}).get() == ["SYSTEM", None]
    person = {"organization_id": org, "actor_type": "USER", "actor_id": str(world.ana.pk)}
    with pytest.raises(stock.ActorMismatch):
        stock_actor.apply(kwargs=person).get()


def routes(where: tuple[Any, Any], target: Any) -> list[tuple[str, dict[str, Any]]]:
    return [
        ("transfers/", transfer(where[0], where[1], target)),
        ("reservations/", reservation(where)),
        (f"reservations/{uuid4()}/release/", {"reason": "Ya no"}),
    ]


def every_route(world: Any, here: tuple[Any, Any]) -> list[tuple[str, Any]]:
    """Las de `routes` y, con la regla de F3-39b, movimientos y aplicar o revertir una importación
    previsualizada (aún sin aplicar). Con la de F3-39c, previsualizar otra."""
    job = preview(world.a, world.ana, CSV).pk
    upload = {"file_name": "conteo.csv", "content": base64.b64encode(CSV.encode()).decode()}
    receipt = {"variant_id": str(here[0]), "warehouse_id": str(here[1]), "quantity": 1}
    receipt |= {"movement_type": "RECEIPT", "reason": "Compra", "idempotency_key": uuid4().hex}
    return [
        *routes(here, site(world.a, here, "TDA")),
        ("movements/", receipt),
        (f"imports/{job}/apply/", None),
        (f"imports/{job}/revert/", {"confirm": True}),
        ("imports/", upload),
    ]


SERVICES = (
    (transfers, "transfer"),
    (res, "reserve"),
    (res, "release"),
    (stock, "move"),
    (imports, "apply"),
    (imports, "revert"),
    (imports, "preview"),
)


@pytest.mark.parametrize("route", range(len(SERVICES)))
def test_each_route_answers_a_refused_actor_with_a_403_and_its_code(
    world: Any, monkeypatch: pytest.MonkeyPatch, route: int
) -> None:
    """La ruta, si el servicio rechaza al actor: el 403 documentado, sin cifras ni 500."""
    give(world.a, world.membership, ALL)
    here = stocked(world.a, 2, world.ana)
    path, sent = every_route(world, here)[route]

    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise stock.ActorRefused(ActorType.AI_AGENT)

    for module, name in SERVICES:
        monkeypatch.setattr(module, name, refuse)
    answer = send(signed(world.ana), "post", BASE + path, sent)
    assert answer.status_code == 403, answer.content
    assert answer.json() == {"code": "ACTOR_NOT_ALLOWED", "message": REFUSED}
    assert REFUSED == "Solo una persona o el sistema mueve, reserva o transfiere stock."


@pytest.mark.parametrize("actor", ROBOTS)
def test_a_robot_over_http_is_turned_away_before_the_service(
    world: Any, migrator: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch, actor: ActorType
) -> None:
    """Por HTTP la primera barrera es el permiso: a quien no es una persona, el motor de
    autorización lo trata como a un no miembro (404, el mismo de siempre), nunca un 500."""
    give(world.a, world.membership, ALL)
    here = stocked(world.a, 2, world.ana)
    sent = every_route(world, here)
    monkeypatch.setattr(
        "core.tenancy.middleware.resolve_tenant",
        lambda *a: replace(resolve_tenant(*a), actor_type=actor, actor_id=uuid4()),
    )
    client, before = signed(world.ana), migrator.execute(COUNTS).fetchone()
    for path, body in sent:
        answer = send(client, "post", BASE + path, body)
        assert (answer.status_code, answer.json()) == (404, {"code": "NOT_FOUND"}), path
    assert migrator.execute(COUNTS).fetchone() == before


def test_a_person_without_her_user_gets_the_403_on_every_route_never_a_field(
    world: Any, migrator: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`ActorMismatch` (`actor_id` no es `user_id`) pasa el permiso y llega al servicio real: el
    403 en las siete rutas, nunca un 400 sobre `expires_at` (reservas) ni un 500, sin escribir."""
    give(world.a, world.membership, ALL)
    here = stocked(world.a, 2, world.ana)
    sent = every_route(world, here)
    monkeypatch.setattr(
        "core.tenancy.middleware.resolve_tenant",
        lambda *a: replace(resolve_tenant(*a), actor_id=uuid4()),
    )
    client, before = signed(world.ana), migrator.execute(COUNTS).fetchone()
    for path, body in sent:
        answer = send(client, "post", BASE + path, body)
        assert answer.status_code == 403, (path, answer.content)
        assert answer.json() == {"code": "ACTOR_NOT_ALLOWED", "message": REFUSED}, path
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

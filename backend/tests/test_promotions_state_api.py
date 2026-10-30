"""F3-48e: publicar y cancelar promociones por HTTP (`…/promotions/{id}/publish|cancel/`).
Middleware, sesión, motor de autorización y PostgreSQL con el rol `crm_app`. Los comandos y su
carrera se prueban en `test_promotions`; aquí, la ruta: permiso antes de leer, 404, 409 con su
código, auditoría, dos peticiones a la vez y el motor de precios de punta a punta."""

import threading
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from django.db import connection
from django.test import Client
from django.utils import timezone

from apps.accounts.models import User
from apps.audit import services as audit
from apps.pricing import promotions as commands
from apps.pricing.models import Promotion
from core.tenancy.scope import tenant_scope
from tests import test_price_lists_api, test_self_context
from tests import test_promotions_api as api
from tests.factories import make_user
from tests.test_authorization import give
from tests.test_member_branch import DENIED
from tests.test_memberships import ctx, join
from tests.test_prices import price_list
from tests.test_pricing_engine import T0, row
from tests.test_products import send
from tests.test_promotion_tables import final, promo, stock
from tests.test_self_context import NOT_FOUND, reply, signed

team, shop = api.team, test_price_lists_api.shop
real_stack = test_self_context.real_stack
pytestmark = pytest.mark.usefixtures("tenant_db")
DAY = timedelta(days=1)


def at(made: Any, action: str) -> str:
    return f"{api.PROMOS}{made}/{action}/"


def live(team: Any, status: str, **fields: Any) -> UUID:
    """Una promoción con una ventana que aún no termina (publicarla es posible)."""
    now = timezone.now()
    target = [{"category_id": team.category}]
    return promo(
        team.org,
        [team.regular],
        target,
        status=status,
        starts=now - DAY,
        ends=now + DAY * 9,
        **fields,
    )


def state(team: Any, made: Any) -> tuple[str, int]:
    with tenant_scope(ctx(team.org)):
        status, version = Promotion.objects.values_list("status", "version").get(pk=made)
    return str(status), int(version)


def code(response: Any) -> tuple[int, str | None]:
    return response.status_code, response.json().get("code")


STATES = [  # (estado de partida, acción, respuesta, estado final)
    ("DRAFT", "publish", (200, None), "PUBLISHED"),
    ("PUBLISHED", "publish", (409, "PROMOTION_NOT_DRAFT"), "PUBLISHED"),
    ("CANCELLED", "publish", (409, "PROMOTION_NOT_DRAFT"), "CANCELLED"),
    ("DRAFT", "cancel", (200, None), "CANCELLED"),
    ("PUBLISHED", "cancel", (200, None), "CANCELLED"),
    ("CANCELLED", "cancel", (409, "PROMOTION_ALREADY_CANCELLED"), "CANCELLED"),
]


def test_the_state_machine_over_http_with_its_audit(
    team: Any, migrator: psycopg.Connection[Any]
) -> None:
    for start, action, wanted, end in STATES:
        made = live(team, start)
        response = send(team.a, "post", at(made, action))
        answer = response.json()
        assert (response.status_code, answer.get("code")) == (
            wanted[0],
            wanted[1] or answer.get("code"),
        )
        moved = wanted[0] == 200
        assert state(team, made) == (end, 2 if moved else 1), (start, action)
        if moved:  # el mismo cuerpo que el detalle, con el estado nuevo
            assert answer == team.a.get(f"{api.PROMOS}{made}/").json()
            assert (answer["status"], answer["version"], len(answer["items"])) == (end, 2, 1)
        else:
            assert set(answer) >= {"code", "message"} and answer["code"] == wanted[1]
        query = "SELECT action, changes, actor_id FROM audit_logs WHERE entity_id = %s"
        rows = migrator.execute(query, [made]).fetchall()
        assert [r[0] for r in rows] == (
            [f"promotion.{'published' if action == 'publish' else 'cancelled'}"] if moved else []
        )
        if moved:
            assert rows[0][1] == {"status": [start, end]}
            assert rows[0][2] == User.objects.get(email="a@example.com").pk
    ended = UUID(api.create(team.a, team).json()["id"])  # su ventana de abril ya pasó
    assert code(send(team.a, "post", at(ended, "publish"))) == (409, "PROMOTION_ENDED")
    assert state(team, ended) == ("DRAFT", 1)
    assert send(team.a, "post", at(ended, "cancel")).status_code == 200  # cancelar sí se puede


def test_the_permission_is_checked_before_any_read(team: Any) -> None:
    nobody = make_user(email="ciega@example.com")
    join(team.org, nobody)  # miembro sin roles
    reader = make_user(email="lectora@example.com")
    give(team.org, join(team.org, reader).pk, {"prices.view": None, "products.view": None})
    for action in ("publish", "cancel"):
        made, unknown = live(team, "DRAFT"), uuid4()
        for client in (team.v, signed(nobody), signed(reader)):  # sin `prices.manage`, exista o no
            assert reply(send(client, "post", at(made, action))) == DENIED
            assert reply(send(client, "post", at(unknown, action))) == DENIED
        assert reply(send(team.a, "post", at(unknown, action))) == NOT_FOUND
        assert send(Client(), "post", at(made, action)).status_code == 401
        assert send(team.x, "post", at(made, action)).status_code == 200  # el Administrador
    with tenant_scope(ctx(team.org)):  # solo las del Administrador cambiaron
        assert sorted(Promotion.objects.values_list("version", flat=True)) == [2, 2]


def test_another_organization_is_not_found_and_stays_untouched(
    team: Any, orgs: dict[str, UUID], migrator: psycopg.Connection[Any]
) -> None:
    their_list = price_list(orgs["B"], "REGULAR")
    theirs = promo(orgs["B"], [their_list], [{"variant_id": stock(orgs["B"])[1]}], status="DRAFT")
    for action in ("publish", "cancel"):
        assert reply(send(team.a, "post", at(theirs, action))) == NOT_FOUND
        assert (
            reply(send(team.a, "post", at(theirs, action).replace("org-a", "org-b"))) == NOT_FOUND
        )
    with tenant_scope(ctx(orgs["B"])):
        assert Promotion.objects.get(pk=theirs).status == "DRAFT"
    gone = live(team, "DRAFT")  # una borrada tampoco existe
    migrator.execute("UPDATE promotions SET deleted_at = now() WHERE id = %s", [gone])
    assert reply(send(team.a, "post", at(gone, "publish"))) == NOT_FOUND


def http_race(monkeypatch: pytest.MonkeyPatch, team: Any, paths: list[str]) -> list[Any]:
    """La primera petición retiene su transacción (tras auditar) y la segunda espera su bloqueo."""
    wrote, release, outcome = threading.Event(), threading.Event(), list[Any]([None, None])
    real = audit.record

    def hold(*args: Any, **kwargs: Any) -> Any:
        done = real(*args, **kwargs)
        if threading.current_thread().name == "first":
            wrote.set()
            release.wait(timeout=10)
        return done

    monkeypatch.setattr(commands, "record", hold, raising=True)
    owner = User.objects.get(email="a@example.com")

    def run(index: int) -> None:
        try:
            response = send(signed(owner), "post", paths[index])
            outcome[index] = code(response)
        except Exception as error:  # noqa: BLE001 — el resultado lo comprueba cada test
            outcome[index] = type(error).__name__
        finally:
            wrote.set()
            connection.close()

    first, second = (
        threading.Thread(target=run, args=(i,), name=n) for i, n in enumerate(("first", "second"))
    )
    first.start()
    assert wrote.wait(timeout=10)
    second.start()
    second.join(timeout=1)
    waited = second.is_alive()
    release.set()
    for thread in (first, second):
        thread.join(timeout=10)
    assert waited, "la segunda no esperó a la primera"
    return outcome


def test_two_requests_at_once_give_the_second_a_409_never_a_500(
    team: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    both = live(team, "DRAFT")
    assert http_race(monkeypatch, team, [at(both, "publish")] * 2) == [
        (200, None),
        (409, "PROMOTION_NOT_DRAFT"),
    ]
    assert state(team, both) == ("PUBLISHED", 2)
    twice = live(team, "PUBLISHED")
    assert http_race(monkeypatch, team, [at(twice, "cancel")] * 2) == [
        (200, None),
        (409, "PROMOTION_ALREADY_CANCELLED"),
    ]
    late = live(team, "DRAFT")  # cancelar y luego publicar: la que espera ve la cancelada
    paths = [at(late, "cancel"), at(late, "publish")]
    assert http_race(monkeypatch, team, paths) == [(200, None), (409, "PROMOTION_NOT_DRAFT")]
    assert state(team, late) == ("CANCELLED", 2)


def test_created_then_published_it_applies_exactly_in_its_window_until_cancelled(team: Any) -> None:
    viewer = User.objects.get(email="v@example.com")
    row(team.org, (team.variant, team.regular), "4399", T0 - timedelta(days=90))
    start = timezone.now().replace(microsecond=0) + timedelta(hours=1)
    end = start + timedelta(days=2)
    items = [{"variant_id": str(team.variant)}]
    made = api.create(
        team.a, team, items=items, starts_at=start.isoformat(), ends_at=end.isoformat()
    )
    assert made.status_code == 201, made.content
    one = made.json()["id"]

    def promoted(when: Any) -> str | None:
        found = final(team.org, viewer, team.variant, team.regular, when).promotion_id
        return None if found is None else str(found)

    assert promoted(start + DAY) is None  # en borrador no se aplica
    assert send(team.a, "post", at(one, "publish")).status_code == 200
    assert [
        promoted(when)
        for when in (start - timedelta(microseconds=1), start, end - timedelta(microseconds=1), end)
    ] == [None, one, one, None]
    assert send(team.a, "post", at(one, "cancel")).json()["status"] == "CANCELLED"
    assert promoted(start + DAY) is None  # cancelada: ya no se aplica

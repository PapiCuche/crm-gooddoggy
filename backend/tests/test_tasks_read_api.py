"""F5-08b: los permisos de tareas, la política de alcance por responsable y las rutas de lectura
(`…/tasks/` y `…/tasks/{id}/`): alcance, filtros, nombre del contacto y paginación. Middleware,
sesión, motor de autorización y PostgreSQL con el rol `crm_app`."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from django.test import Client
from django.utils import timezone

from apps.access.catalog import BY_CODE, ROLE_TEMPLATES, Scope
from apps.access.scopes import policy_for
from apps.deals.scopes import AssignedTo
from apps.tasks.models import Task
from core.tenancy.scope import tenant_scope
from tests import test_opportunity_scopes as scopes
from tests.test_authorization import give
from tests.test_member_branch import DENIED
from tests.test_memberships import ctx
from tests.test_opportunities_tables import make
from tests.test_self_context import NOT_FOUND, reply, signed

crm, pipelines, real_stack = scopes.crm, scopes.pipelines, scopes.real_stack
pytestmark = pytest.mark.usefixtures("tenant_db")
TASKS = "/api/v1/o/org-a/tasks/"
UNAUTHENTICATED = (401, b'{"code":"NOT_AUTHENTICATED"}')
FIELDS = {
    "id", "type", "title", "description", "assigned_user_id", "contact", "opportunity_id",
    "due_at", "is_all_day", "reminder_at", "status", "priority", "completed_at",
    "completed_by_user_id", "cancel_reason", "source", "created_at", "updated_at", "version",
}  # fmt: skip
NOW = timezone.now()
SCOPED = ("tasks.view", "tasks.update")


@pytest.fixture
def board(crm: SimpleNamespace) -> SimpleNamespace:
    """Sobre `crm` (ana Owner, vera Vendedora y sam Supervisor, del mismo equipo; bea sin rol; luis
    de B): una tarea de cada uno, una sin responsable, una borrada y una de B."""

    def task(org: UUID, who: Any, title: str, contact: str | None = None, **over: Any) -> UUID:
        with tenant_scope(ctx(org)):
            return Task.objects.create(  # type: ignore[no-any-return]
                title=title, assigned_user_id=None if who is None else who.pk,
                contact_id=crm.ids.get(contact), **over,
            ).pk  # fmt: skip

    ids = {
        "vera": task(crm.a, crm.vera, "De vera", "owned"),
        "sam": task(crm.a, crm.sam, "De sam", "sam"),
        "ana": task(crm.a, crm.ana, "De ana", "plain"),
        "nobody": task(crm.a, None, "Sin responsable"),
        "gone": task(crm.a, crm.vera, "Borrada", deleted_at=NOW),
        "b": task(crm.b, crm.luis, "De B"),
    }
    return SimpleNamespace(**vars(crm), tasks=ids)


def pick(board: SimpleNamespace, *keys: str) -> set[UUID]:
    return {board.tasks[key] for key in keys}


def ids_of(client: Client, url: str = TASKS, **query: Any) -> set[UUID]:
    response = client.get(url, query)
    assert response.status_code == 200, response.content
    return {UUID(row["id"]) for row in response.json()["results"]}


def test_the_catalog_and_the_templates_carry_the_task_permissions() -> None:
    for code in (*SCOPED, "tasks.create", "tasks.assign"):
        permission = BY_CODE[code]
        assert (permission.module, permission.is_sensitive) == ("tasks", False)
        assert permission.supports_scope is (code in SCOPED)
    held = {
        t.code: {k: v for k, v in t.grants.items() if k.startswith("tasks.")}
        for t in ROLE_TEMPLATES
    }
    every = {
        **dict.fromkeys(SCOPED, Scope.ORGANIZATION),
        "tasks.create": None,
        "tasks.assign": None,
    }
    assert held["owner"] == held["admin"] == every
    assert held["supervisor"] == {**dict.fromkeys(SCOPED, Scope.TEAM), "tasks.create": None,
                                  "tasks.assign": None}  # fmt: skip
    assert held["seller"] == {**dict.fromkeys(SCOPED, Scope.OWN), "tasks.create": None}
    assert policy_for(Task) == AssignedTo()


def test_each_role_sees_what_its_scope_reaches(board: SimpleNamespace) -> None:
    assert ids_of(signed(board.ana)) == pick(board, "vera", "sam", "ana", "nobody")
    assert ids_of(signed(board.vera)) == pick(board, "vera")  # `OWN`
    assert ids_of(signed(board.sam)) == pick(board, "vera", "sam")  # `TEAM`: su compañera
    assert reply(signed(board.bea).get(TASKS)) == DENIED
    assert reply(Client().get(TASKS)) == UNAUTHENTICATED
    assert reply(signed(board.luis).get(TASKS)) == NOT_FOUND  # no es miembro de A
    other = TASKS.replace("org-a", "org-b")
    assert reply(signed(board.ana).get(other)) == NOT_FOUND  # ana no es miembro de B
    assert reply(signed(board.luis).get(other)) == DENIED  # luis sí, sin `tasks.view`
    give(board.b, board.m_luis, {"tasks.view": "ORGANIZATION"})
    assert ids_of(signed(board.luis), other) == pick(board, "b")  # nunca las de A


def test_the_view_permission_alone_decides_reading(board: SimpleNamespace) -> None:
    give(board.a, board.m["bea"], {"tasks.update": "ORGANIZATION", "tasks.create": None})
    assert reply(signed(board.bea).get(TASKS)) == DENIED  # editar o crear no es ver
    give(board.a, board.m["bea"], {"tasks.view": "BRANCH"})
    assert ids_of(signed(board.bea)) == set()  # `BRANCH` es lo propio, y no tiene
    give(board.a, board.m["bea"], {"tasks.view": "OWN"})
    assert ids_of(signed(board.bea)) == set()


def test_the_detail_is_the_same_row_and_every_miss_is_the_same_404(board: SimpleNamespace) -> None:
    detail = signed(board.ana).get(f"{TASKS}{board.tasks['vera']}/")
    assert detail.status_code == 200 and set(detail.json()) == FIELDS
    listed = signed(board.ana).get(TASKS).json()["results"]
    assert next(r for r in listed if r["id"] == str(board.tasks["vera"])) == detail.json()
    vera = signed(board.vera)
    misses = {
        reply(vera.get(f"{TASKS}{pk}/"))
        for pk in (board.tasks["sam"], board.tasks["gone"], board.tasks["b"], uuid4())
    }
    assert misses == {NOT_FOUND}  # fuera de alcance, borrada, de B o inexistente: lo mismo
    assert reply(signed(board.bea).get(f"{TASKS}{board.tasks['vera']}/")) == DENIED
    assert reply(Client().get(f"{TASKS}{board.tasks['vera']}/")) == UNAUTHENTICATED


def test_a_row_has_its_documented_values_and_the_contact_name_follows_the_contact_scope(
    board: SimpleNamespace,
) -> None:
    with tenant_scope(ctx(board.a)):
        Task.objects.filter(pk=board.tasks["vera"]).update(
            due_at=NOW,
            is_all_day=True,
            type="CALL",
            priority="HIGH",
            description="Línea 1\nLínea 2",
        )
    row = signed(board.ana).get(f"{TASKS}{board.tasks['vera']}/").json()
    assert (row["type"], row["priority"], row["status"], row["source"], row["version"]) == (
        "CALL", "HIGH", "PENDING", "MANUAL", 1,
    )  # fmt: skip
    assert (row["is_all_day"], row["description"], row["completed_at"]) == (
        True,
        "Línea 1\nLínea 2",
        None,
    )
    assert row["contact"] == {"id": str(board.ids["owned"]), "display_name": "De vera"}
    mine = signed(board.vera).get(f"{TASKS}{board.tasks['vera']}/").json()
    assert mine["contact"]["display_name"] == "De vera"  # el contacto es suyo (`OWN`)
    with tenant_scope(ctx(board.a)):  # una tarea suya sobre un contacto que no puede ver
        Task.objects.filter(pk=board.tasks["vera"]).update(contact_id=board.ids["plain"])
    hidden = signed(board.vera).get(f"{TASKS}{board.tasks['vera']}/").json()
    assert hidden["contact"] == {"id": str(board.ids["plain"]), "display_name": None}
    give(board.a, board.m["vera"], {"contacts.view": "ORGANIZATION"})
    assert signed(board.vera).get(f"{TASKS}{board.tasks['vera']}/").json()["contact"][
        "display_name"
    ] == "Sin nadie"  # fmt: skip
    nobody = signed(board.ana).get(f"{TASKS}{board.tasks['nobody']}/").json()
    assert nobody["contact"] == {"id": None, "display_name": None}


def test_the_filters_combine_and_each_one_is_asked_once(board: SimpleNamespace) -> None:
    opp = make(board.a, contact_id=board.ids["plain"])
    with tenant_scope(ctx(board.a)):
        Task.objects.filter(pk=board.tasks["vera"]).update(
            status="DONE", completed_at=NOW, due_at=NOW - timedelta(days=2)
        )
        Task.objects.filter(pk=board.tasks["sam"]).update(due_at=NOW - timedelta(days=1))
        Task.objects.filter(pk=board.tasks["ana"]).update(
            due_at=NOW + timedelta(days=1), opportunity_id=opp.pk
        )
    ana = signed(board.ana)
    assert ids_of(ana, mine="true") == pick(board, "ana")
    assert ids_of(ana, assigned_user_id=str(board.sam.pk)) == pick(board, "sam")
    assert ids_of(ana, status="DONE") == pick(board, "vera")
    assert ids_of(ana, overdue="true") == pick(
        board, "sam"
    )  # abierta y vencida (la de vera, hecha)
    assert ids_of(ana, due_from=(NOW - timedelta(days=1, hours=1)).isoformat()) == pick(
        board, "sam", "ana"
    )  # fmt: skip
    assert ids_of(ana, due_to=(NOW - timedelta(hours=1)).isoformat()) == pick(board, "vera", "sam")
    assert ids_of(ana, contact_id=str(board.ids["plain"])) == pick(board, "ana")
    assert ids_of(ana, opportunity_id=str(opp.pk)) == pick(board, "ana")
    assert ids_of(ana, overdue="true", mine="true") == set()
    assert ids_of(ana, status="") == pick(
        board, "vera", "sam", "ana", "nobody"
    )  # vacío: sin filtro
    for bad in ({"status": "DONE", "mine": ["true", "false"]}, {"status": "SOON"}, {"mine": "x"},
                {"due_from": "ayer"}, {"contact_id": "x"}):  # fmt: skip
        assert ana.get(TASKS, bad).status_code == 400, bad
    assert ids_of(signed(board.vera), mine="true") == pick(board, "vera")


def test_the_due_range_includes_its_start_and_excludes_its_end(board: SimpleNamespace) -> None:
    edge = datetime(2026, 11, 20, 15, 0, tzinfo=UTC)
    with tenant_scope(ctx(board.a)):
        Task.objects.filter(pk=board.tasks["sam"]).update(due_at=edge)
    ana = signed(board.ana)
    assert ids_of(ana, due_from=edge.isoformat()) == pick(board, "sam")
    assert ids_of(ana, due_to=edge.isoformat()) == set()
    assert ids_of(ana, due_to=(edge + timedelta(seconds=1)).isoformat()) == pick(board, "sam")


def test_the_list_pages_by_cursor_without_repeating_a_row(board: SimpleNamespace) -> None:
    ana, seen = signed(board.ana), []
    page = ana.get(TASKS, {"limit": 2}).json()
    assert len(page["results"]) == 2 and page["next"] is not None
    seen += [r["id"] for r in page["results"]]
    page = ana.get(TASKS, {"limit": 2, "cursor": page["next"]}).json()
    seen += [r["id"] for r in page["results"]]
    assert len(seen) == len(set(seen)) == 4 and page["next"] is None

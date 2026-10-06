"""F2-54: `PATCH /api/v1/o/{slug}/teams/{id}/`, editar y desactivar un equipo. Middleware,
sesión, motor de autorización y PostgreSQL con el rol `crm_app`."""

import threading
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from django.db import IntegrityError, connection
from django.test import Client

from apps.organizations import teams
from apps.organizations.models import Team, TeamMember
from apps.organizations.teams import update_team
from core.tenancy.scope import tenant_scope
from tests import test_authorization, test_self_context, test_team_writes
from tests.test_anti_escalation import audit, race
from tests.test_authorization import give
from tests.test_memberships import ctx, raw
from tests.test_self_context import NOT_FOUND, reply, signed
from tests.test_team_writes import DENIED, ROWS, URL, send
from tests.test_teams import team

world, real_stack = test_authorization.world, test_self_context.real_stack
ana = test_team_writes.ana
pytestmark = pytest.mark.usefixtures("tenant_db")
STAMPS = "SELECT id, xmin::text, updated_at FROM teams ORDER BY id"  # cambian al escribir


def update(client: Client, team_id: Any, body: Any, org: str = "org-a") -> Any:
    return send(client, "patch", f"{URL.format(org)}{team_id}/", body)


def test_it_changes_only_what_is_sent_and_audits_the_difference(
    world: Any, ana: Client, migrator: psycopg.Connection[Any]
) -> None:
    sales = team(world.a, "ventas", name="Ventas", description="Clientes nuevos")
    other = team(world.a, "soporte", name="Soporte")
    body = {"name": " Ventas  Norte ", "description": "", "assignment_strategy": "MANUAL"}
    edited = update(ana, sales.pk, body)
    assert edited.status_code == 200
    assert edited.json() == {
        "id": str(sales.pk),
        "slug": "ventas",
        "name": "Ventas Norte",
        "description": "",
        "assignment_strategy": "MANUAL",
        "is_active": True,
    }
    changes = {"name": ["Ventas", "Ventas Norte"], "description": ["Clientes nuevos", ""]}
    assert audit(migrator)[-1] == ("team.updated", "team", sales.pk, changes, {}, world.ana.pk)
    label = "SELECT entity_label FROM audit_logs WHERE action = 'team.updated'"
    assert migrator.execute(label).fetchall() == [("ventas",)]
    before, stamps = migrator.execute(ROWS).fetchone(), migrator.execute(STAMPS).fetchall()
    for same in ({}, {"name": "Ventas Norte"}, {"slug": "otro", "organization_id": str(world.b)}):
        assert update(ana, sales.pk, same).json() == edited.json()  # el `slug` no se cambia
    assert migrator.execute(ROWS).fetchone() == before  # sin cambios, ni escribe ni audita
    assert migrator.execute(STAMPS).fetchall() == stamps  # ni un `UPDATE`: las mismas filas
    listed = {row["slug"]: row for row in ana.get(URL.format("org-a")).json()["results"]}
    assert listed["ventas"] == edited.json() and listed["soporte"]["name"] == "Soporte"
    with tenant_scope(ctx(world.a)):
        assert Team.objects.get(pk=other.pk).updated_at == other.updated_at  # el otro, intacto
        assert Team.objects.get(pk=sales.pk).updated_at > sales.updated_at


def test_it_deactivates_and_reactivates_without_touching_its_members(
    world: Any, ana: Client, migrator: psycopg.Connection[Any]
) -> None:
    sales = team(world.a, "ventas")
    with tenant_scope(ctx(world.a)):
        TeamMember.objects.create(team=sales, membership_id=world.membership)
    off = update(ana, sales.pk, {"is_active": False, "assignment_strategy": "ROUND_ROBIN"}).json()
    assert (off["is_active"], off["assignment_strategy"]) == (False, "ROUND_ROBIN")
    assert audit(migrator)[-1][3] == {
        "is_active": [True, False],
        "assignment_strategy": ["MANUAL", "ROUND_ROBIN"],
    }
    assert update(ana, sales.pk, {"is_active": False}).json() == off
    assert len(audit(migrator)) == 1  # ya estaba inactivo
    with tenant_scope(ctx(world.a)):
        assert TeamMember.objects.filter(team=sales, is_active=True).count() == 1  # siguen ahí
    assert update(ana, sales.pk, {"is_active": True}).json()["is_active"] is True
    assert audit(migrator)[-1][3] == {"is_active": [False, True]}


def test_two_edits_at_once_queue_and_each_audits_what_it_found(
    world: Any, migrator: psycopg.Connection[Any]
) -> None:
    sales = team(world.a, "ventas", name="Ventas")

    def edit(**fields: Any) -> tuple[Any, Any]:
        return world.ana, lambda tenant: update_team(tenant, team_id=sales.pk, **fields)

    changes = {"first": edit(name="Uno"), "second": edit(name="Dos", is_active=False)}
    race(world.a, changes, hold="first")  # el segundo espera el bloqueo de la fila y la relee
    assert [row[3] for row in audit(migrator)] == [
        {"name": ["Ventas", "Uno"]},
        {"name": ["Uno", "Dos"], "is_active": [True, False]},  # su «antes» es lo que dejó el otro
    ]
    race(world.a, {"first": edit(is_active=True), "second": edit(is_active=True)}, hold="first")
    assert len(audit(migrator)) == 3  # el segundo ya lo encontró activo: ni escribe ni audita


def test_an_edit_in_flight_does_not_block_adding_a_member(world: Any) -> None:
    """`FOR NO KEY UPDATE`: la FK de `team_members` solo pide `KEY SHARE` sobre el equipo."""
    sales = team(world.a, "ventas")
    held, release = threading.Event(), threading.Event()

    def edit() -> None:
        try:
            with tenant_scope(ctx(world.a, world.ana)) as tenant:
                update_team(tenant, team_id=sales.pk, name="Retenido")
                held.set()  # ya escribió y aún no ha confirmado: conserva el bloqueo
                release.wait(timeout=10)
        finally:
            held.set()
            connection.close()

    editing = threading.Thread(target=edit)
    editing.start()
    try:
        assert held.wait(timeout=10)
        with tenant_scope(ctx(world.a)):
            raw("SET LOCAL lock_timeout = '2s'")
            TeamMember.objects.create(team=sales, membership_id=world.membership)  # no espera
    finally:
        release.set()
        editing.join(timeout=10)
    with tenant_scope(ctx(world.a)):
        assert (Team.objects.get().name, TeamMember.objects.count()) == ("Retenido", 1)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("name", ""),
        ("name", "---"),
        ("name", "Dos\nlíneas"),
        ("name", "N" * 101),
        ("name", None),
        ("description", "Dos\tcolumnas"),
        ("description", "D" * 256),
        ("assignment_strategy", ""),
        ("assignment_strategy", "manual"),
        ("assignment_strategy", "RANDOM"),
        ("is_active", "quizá"),
        ("is_active", None),
        ("is_active", "false"),  # solo los booleanos de JSON, como el comando
        ("is_active", "no"),
        ("is_active", 0),
    ],
)
def test_a_field_that_does_not_fit_is_a_400_that_names_it(
    world: Any, ana: Client, migrator: psycopg.Connection[Any], field: str, value: Any
) -> None:
    sales = team(world.a, "ventas", name="Ventas")
    refused = update(ana, sales.pk, {"description": "Sí serviría", field: value})
    assert refused.status_code == 400 and refused.json()["code"] == "VALIDATION_ERROR"
    assert list(refused.json()["fields"]) == [field]
    assert migrator.execute(ROWS).fetchone() == (1, 0)  # tampoco el campo que sí servía
    with tenant_scope(ctx(world.a)):
        kept = Team.objects.get()
        assert (kept.name, kept.description, kept.is_active) == ("Ventas", "", True)


def test_only_who_manages_teams_edits_and_only_in_their_organization(
    world: Any, migrator: psycopg.Connection[Any]
) -> None:
    mine, theirs = team(world.a, "ventas"), team(world.b, "soporte")
    before, change = migrator.execute(ROWS).fetchone(), {"name": "Tocado", "is_active": False}
    assert update(Client(), mine.pk, change).status_code == 401
    client = signed(world.ana)
    assert reply(update(client, mine.pk, change)) == DENIED  # miembro sin roles
    give(world.a, world.membership, {"teams.view": None})  # verlos no es administrarlos
    assert reply(update(client, mine.pk, change)) == DENIED
    assert reply(update(client, uuid4(), change)) == DENIED  # sin permiso no se sabe si existe
    give(world.a, world.membership, {"teams.manage": None})
    for missing in (theirs.pk, uuid4()):  # el de otra organización no existe
        assert reply(update(client, missing, change)) == NOT_FOUND
    for org in ("org-b", "no-existe"):
        assert reply(update(client, theirs.pk, change, org)) == NOT_FOUND
        assert reply(update(client, mine.pk, change, org)) == NOT_FOUND
    for method in ("put", "delete", "post", "get"):  # sobre un equipo solo hay `PATCH`
        assert reply(send(client, method, f"{URL.format('org-a')}{mine.pk}/", {})) == DENIED
    assert migrator.execute(ROWS).fetchone() == before
    with tenant_scope(ctx(world.b)):
        assert (Team.objects.get().name, Team.objects.get().is_active) == ("soporte", True)
    assert update(client, mine.pk, change).json()["name"] == "Tocado"


def test_the_command_validates_and_writes_all_or_nothing(
    world: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    sales = team(world.a, "ventas", name="Ventas")
    with tenant_scope(ctx(world.a)) as tenant:
        for bad in (
            {"slug": "otro"},
            {"is_active": 1},
            {"is_active": "false"},
            {"name": "\n"},
            {"name": 7},
            {"assignment_strategy": "manual"},
            {"id": uuid4()},
            {"organization_id": world.b},
        ):
            with pytest.raises(ValueError, match=next(iter(bad))):
                update_team(tenant, team_id=sales.pk, description="No llega", **bad)
        assert (Team.objects.get().name, Team.objects.get().description) == ("Ventas", "")
        cleaned = update_team(tenant, team_id=sales.pk, name=" Cafe\u0301\u00a0 de  Lima ")
        assert cleaned.name == "Caf\u00e9 de Lima"
        edge = update_team(tenant, team_id=sales.pk, name="9" * 100, description="\ufb01" * 255)
        assert (len(edge.name), len(edge.description)) == (100, 255)  # justo en cada límite

        def unaudited(*args: Any, **kwargs: Any) -> None:
            raise IntegrityError("sin auditoría")

        monkeypatch.setattr(teams, "record", unaudited)
        with pytest.raises(IntegrityError, match="sin auditoría"):
            update_team(tenant, team_id=sales.pk, name="Sin rastro")
        assert Team.objects.get().name == "9" * 100  # el savepoint deshizo el cambio
    with pytest.raises(Team.DoesNotExist), tenant_scope(ctx(world.b)) as other:
        update_team(other, team_id=sales.pk, name="Ajeno")

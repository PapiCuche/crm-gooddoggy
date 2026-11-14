"""Puntos de entrada con contexto de tenant: HTTP (T7), Celery (T9), WS (T10) y comandos."""

import json
import re
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from asgiref.sync import async_to_sync
from asgiref.testing import ApplicationCommunicator
from celery import Celery
from channels.routing import URLRouter
from django.core.management import CommandError, call_command
from django.test import Client
from django.urls import URLResolver, get_resolver, path

import config.urls
from config.celery import app as celery_app
from core.tenancy.celery import TenancyCheck
from core.tenancy.commands import PlatformCommand, TenantCommand
from core.tenancy.context import TenantContext, TenantContextError, TenantContextMissing
from core.tenancy.scope import tenant_scope
from core.tenancy.second_factor import MESSAGE, SecondFactor
from tests import fakes
from tests.tenancy_app.consumers import WidgetConsumer
from tests.tenancy_app.models import Widget
from tests.tenancy_app.tasks import PINGS, count_without_tenant, platform_ping, visible_widgets

pytestmark = pytest.mark.usefixtures("tenant_db")
NOT_FOUND = {"code": "NOT_FOUND"}


@pytest.fixture
def member(orgs: dict[str, UUID]) -> Iterator[UUID]:
    """Usuario miembro (simulado) SOLO de la organización A."""
    user = uuid4()
    fakes.MEMBERS.add((user, orgs["A"]))
    yield user
    fakes.MEMBERS.clear()


def tenant_routes(patterns: list[Any] | None = None) -> list[str]:
    def walk(patterns: list[Any], prefix: str) -> Iterator[str]:
        for p in patterns:
            route = prefix + str(p.pattern)
            if isinstance(p, URLResolver):
                yield from walk(p.url_patterns, route)
            elif route.startswith("api/v1/o/"):
                yield route

    return list(walk(get_resolver().url_patterns if patterns is None else patterns, ""))


def fill(route: str, slug: str, foreign_id: UUID) -> str:
    """`<slug:org_slug>` → slug; cualquier otro parámetro → un ID del OTRO tenant."""
    return "/" + re.sub(
        r"<(?:\w+:)?(\w+)>", lambda m: slug if m[1] == "org_slug" else str(foreign_id), route
    )


def test_t7_every_tenant_route_hides_other_tenant(
    orgs: dict[str, UUID], member: UUID, migrator: psycopg.Connection[Any]
) -> None:
    routes = tenant_routes()
    assert routes, "el harness necesita al menos una ruta de tenant"
    # Las rutas reales del proyecto usan la membresía de la tabla: al miembro simulado de este
    # arnés le responden el 404 común. Su aislamiento lo prueban sus tests con el stack real.
    real = set(tenant_routes(config.urls.urlpatterns))
    client = Client(headers={"X-Test-User": str(member)})
    for route in routes:
        url = fill(route, "org-a", orgs["widget_B"])
        if "<" not in route.removeprefix("api/v1/o/<slug:org_slug>/"):
            response = client.get(url)  # listado: 200 sin filas de B
            assert response.status_code == (404 if route in real else 200), route
            assert str(orgs["widget_B"]) not in response.text
            continue
        for method in ("get", "patch", "delete"):
            response = getattr(client, method)(url)
            assert (response.status_code, response.json()) == (404, NOT_FOUND), (method, url)
        for slug in ("org-b", "no-existe"):  # sin membresía o inexistente: mismo 404
            response = client.get(fill(route, slug, orgs["widget_A"]))
            assert (response.status_code, response.json()) == (404, NOT_FOUND), (slug, route)
    unchanged = "SELECT count(*) FROM tenancy_app_widget WHERE id = %s AND name = 'widget B'"
    assert migrator.execute(unchanged, [orgs["widget_B"]]).fetchone() == (1,)  # B intacto


def test_tenant_route_runs_inside_tenant_scope(orgs: dict[str, UUID], member: UUID) -> None:
    client = Client(headers={"X-Test-User": str(member)})
    response = client.get(f"/api/v1/o/org-a/widgets/{orgs['widget_A']}/")
    assert response.json() == {"id": str(orgs["widget_A"])}
    assert Client().get("/api/v1/o/org-a/widgets/").status_code == 401


def test_suspended_org_is_403_only_for_members(
    orgs: dict[str, UUID], member: UUID, migrator: psycopg.Connection[Any]
) -> None:
    migrator.execute("UPDATE organizations SET status = 'SUSPENDED'")
    client = Client(headers={"X-Test-User": str(member)})
    assert client.get("/api/v1/o/org-a/widgets/").json() == {"code": "ORG_SUSPENDED"}
    assert client.get("/api/v1/o/org-b/widgets/").json() == NOT_FOUND


def test_t9_tenant_task_requires_organization_and_sees_only_its_tenant(
    orgs: dict[str, UUID],
) -> None:
    for kwargs in ({}, {"organization_id": "no-es-uuid"}):
        with pytest.raises(TenantContextError):
            visible_widgets.apply_async(kwargs=kwargs)  # falla al encolar, sin broker
    result = visible_widgets.apply(kwargs={"organization_id": str(orgs["A"])})
    assert result.get() == [str(orgs["widget_A"])]
    assert platform_ping.apply().get() == "ok"  # fuera de tenant, la tarea de plataforma corre
    with pytest.raises(TenantContextMissing):
        count_without_tenant.apply().get()


def test_platform_task_refuses_active_tenant(orgs: dict[str, UUID]) -> None:
    PINGS.clear()
    with tenant_scope(TenantContext(orgs["A"], "test")):
        for task in (platform_ping, count_without_tenant):  # con el tenant, count vería 1 fila
            with pytest.raises(TenantContextError, match="tenant_scope activo"):
                task.apply().get()
            with pytest.raises(TenantContextError, match="tenant_scope activo"):
                task()  # llamada directa: mismo guard
    assert PINGS == []  # el guard corre antes del cuerpo


def test_worker_refuses_to_start_with_undecorated_tasks() -> None:
    assert TenancyCheck in celery_app.steps["worker"]
    TenancyCheck(SimpleNamespace(app=celery_app))  # todas las tareas del proyecto decoradas
    bad = Celery("bad", set_as_current=False)
    bad.task(name="raw")(fakes.membership)  # cualquier función sin @tenant/@platform_task
    bad.steps["worker"].add(TenancyCheck)
    with pytest.raises(TenantContextError, match="raw"):
        bad.WorkController(pool_cls="solo", hostname="t@test")


def ws(slug: str, user: UUID, *messages: dict[str, str]) -> tuple[bool, Any, list[Any]]:
    router = URLRouter([path("ws/o/<slug:org_slug>/", WidgetConsumer.as_asgi())])

    async def run() -> tuple[bool, Any, list[Any]]:
        scope = {"type": "websocket", "path": f"/ws/o/{slug}/", "user": user, "headers": []}
        communicator = ApplicationCommunicator(router, scope)
        await communicator.send_input({"type": "websocket.connect"})
        opened = await communicator.receive_output()
        replies = []
        for message in messages if opened["type"] == "websocket.accept" else ():
            await communicator.send_input(
                {"type": "websocket.receive", "text": json.dumps(message)}
            )
            replies.append(json.loads((await communicator.receive_output())["text"]))
        await communicator.send_input({"type": "websocket.disconnect", "code": 1000})
        await communicator.wait()
        return opened["type"] == "websocket.accept", opened.get("code"), replies

    return async_to_sync(run)()


def test_t10_ws_subscription_to_other_tenant_is_rejected(
    orgs: dict[str, UUID], member: UUID
) -> None:
    own, other = str(orgs["widget_A"]), str(orgs["widget_B"])
    connected, _, replies = ws("org-a", member, {"subscribe": own}, {"subscribe": other})
    assert connected and replies == [{"subscribed": own}, {"error": "NOT_FOUND"}]
    assert ws("org-b", member)[:2] == (False, 4404)  # sin membresía: no conecta


def lacking(ctx: TenantContext) -> SecondFactor:
    return SecondFactor(required=True, enrolled=False)


def enrolled(ctx: TenantContext) -> SecondFactor:
    return SecondFactor(required=True, enrolled=True)


def test_with_enforce_a_member_who_lacks_the_second_factor_opens_no_route(
    orgs: dict[str, UUID], member: UUID, settings: Any
) -> None:
    """ADR-038: el 403 `MFA_REQUIRED` en toda ruta de tenant y el 4428 en el WebSocket, solo para
    quien es miembro; quien no lo es sigue recibiendo el 404 común (4404)."""
    settings.MFA_ENFORCEMENT = "enforce"
    settings.TENANCY_SECOND_FACTOR_POLICY = f"{__name__}.lacking"
    client = Client(headers={"X-Test-User": str(member)})
    for route in tenant_routes():
        for method in ("get", "patch", "delete"):
            response = getattr(client, method)(fill(route, "org-a", orgs["widget_A"]))
            assert (response.status_code, response.json()) == (
                403,
                {"code": "MFA_REQUIRED", "message": MESSAGE},
            ), (method, route)
        for slug in ("org-b", "no-existe"):
            response = client.get(fill(route, slug, orgs["widget_A"]))
            assert (response.status_code, response.json()) == (404, NOT_FOUND), (slug, route)
    assert ws("org-a", member)[:2] == (False, 4428)
    assert ws("org-b", member)[:2] == (False, 4404)
    settings.TENANCY_SECOND_FACTOR_POLICY = f"{__name__}.enrolled"
    assert client.get("/api/v1/o/org-a/widgets/").status_code == 200
    assert ws("org-a", member, {"subscribe": str(orgs["widget_A"])})[0]
    settings.MFA_ENFORCEMENT, settings.TENANCY_SECOND_FACTOR_POLICY = "warn", f"{__name__}.lacking"
    assert client.get("/api/v1/o/org-a/widgets/").status_code == 200  # `warn` no cierra nada
    assert ws("org-a", member)[0]


class Probe(TenantCommand):
    seen: list[str] | None = None

    def handle_tenant(self, ctx: TenantContext, **options: Any) -> None:
        self.seen = [str(i) for i in Widget.objects.values_list("id", flat=True)]


class Platform(PlatformCommand):
    ran = False

    def handle_platform(self, **options: Any) -> None:
        self.ran = True
        Widget.objects.count()  # sin tenant: TenantContextMissing


def test_commands_require_org_and_reason(orgs: dict[str, UUID]) -> None:
    probe = Probe()
    call_command(probe, "--org", "org-a", "--reason", "test")
    assert probe.seen == [str(orgs["widget_A"])]
    for args in (["--org", "org-a"], ["--reason", "x"], ["--org", "no-existe", "--reason", "x"]):
        with pytest.raises(CommandError):
            call_command(Probe(), *args)
    platform = Platform()
    with pytest.raises(TenantContextMissing):
        call_command(platform, "--reason", "x")
    assert platform.ran  # fuera de tenant se ejecuta; TenantManager falla dentro


def test_commands_refuse_active_tenant(orgs: dict[str, UUID]) -> None:
    platform, probe = Platform(), Probe()
    with tenant_scope(TenantContext(orgs["A"], "test")):
        with pytest.raises(TenantContextError, match="tenant_scope activo"):
            call_command(platform, "--reason", "x")
        with pytest.raises(TenantContextError, match="tenant_scope activo"):
            call_command(probe, "--org", "org-a", "--reason", "x")
    assert not platform.ran and probe.seen is None  # el cuerpo nunca se ejecutó

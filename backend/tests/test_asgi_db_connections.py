"""F1-DB-01 (#1134): bajo ASGI el backend no acumula conexiones inactivas con PostgreSQL, y una
conexión que sí se reutiliza (Celery, comandos) nunca conserva el contexto de tenant."""

import asyncio
import os
import subprocess
import sys
import threading
import time
from contextlib import nullcontext
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from asgiref.testing import ApplicationCommunicator
from django.conf import settings
from django.core.asgi import get_asgi_application
from django.db import DatabaseError, connection
from django.db.backends.signals import connection_created

from config import env
from core.tenancy.context import TenantContext
from core.tenancy.scope import assert_clean_connection, read_context, tenant_scope
from tests.tenancy_app.models import Widget

pytestmark = pytest.mark.usefixtures("tenant_db")
CLIENTS, ROUNDS = 8, 3
OPEN = "SELECT count(*) FROM pg_stat_activity WHERE usename = %s AND datname = current_database()"


def open_connections(migrator: psycopg.Connection[Any]) -> int:
    row = migrator.execute(OPEN, [settings.DATABASES["default"]["USER"]]).fetchone()
    return int(row[0]) if row else 0


def settled(migrator: psycopg.Connection[Any], ceiling: int) -> int:
    """Las conexiones abiertas, tras dar tiempo a que salgan los backends ya cerrados."""
    deadline = time.monotonic() + 3
    while (count := open_connections(migrator)) > ceiling and time.monotonic() < deadline:
        time.sleep(0.05)
    return count


async def get(app: Any, path: str) -> int:
    cookie = f"{settings.SESSION_COOKIE_NAME}={uuid4().hex}".encode()  # se busca en la BD
    scope: dict[str, Any] = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET",
        "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": b"",
        "root_path": "", "server": ("testserver", 80), "client": ("127.0.0.1", 50000),
        "headers": [(b"host", b"testserver"), (b"cookie", cookie)],
    }  # fmt: skip
    communicator = ApplicationCommunicator(app, scope)
    await communicator.send_input({"type": "http.request", "body": b""})
    start = await communicator.receive_output(10)
    await communicator.wait(10)
    return int(start["status"])


def test_the_asgi_process_closes_each_request_connection(
    migrator: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """N peticiones a la vez, R veces, por el ASGIHandler real (un hilo por petición): cada una
    abre su conexión (sin reutilizar la de otra) y al terminar no queda ninguna más abierta."""
    max_age = env.database("postgres://u:p@db:5432/crm", asgi=True)["CONN_MAX_AGE"]
    monkeypatch.setitem(connection.settings_dict, "CONN_MAX_AGE", max_age)
    app, lock, created = get_asgi_application(), threading.Lock(), [0]

    def opened(**kwargs: Any) -> None:
        with lock:
            created[0] += 1

    connection_created.connect(opened, weak=False)
    try:
        baseline = open_connections(migrator)
        for _ in range(ROUNDS):

            async def burst() -> list[int]:
                paths = ["/api/v1/auth/session/"] * CLIENTS
                return await asyncio.gather(*(get(app, path) for path in paths))

            assert asyncio.run(burst()) == [401] * CLIENTS  # la sesión se buscó en la BD
            assert settled(migrator, baseline) == baseline  # no crecen ronda a ronda
    finally:
        connection_created.disconnect(opened)
    assert created == [CLIENTS * ROUNDS]  # una conexión nueva por petición, ninguna heredada


def test_only_the_asgi_entrypoint_drops_persistent_connections() -> None:
    """`config.asgi` marca su proceso antes de cargar los ajustes; el resto conserva 60 s."""
    assert env.database("postgres://u:p@db:5432/crm")["CONN_MAX_AGE"] == 60
    assert settings.DATABASES["default"]["CONN_MAX_AGE"] == 60  # este proceso no es ASGI
    child = os.environ | {"CRM_ASGI_PROCESS": "false"}  # el punto de entrada manda
    probe = "import config.asgi; from django.conf import settings as s; "
    probe += "print(s.DATABASES['default']['CONN_MAX_AGE'])"
    out = subprocess.run(  # noqa: S603 — el intérprete de los tests, código fijo
        [sys.executable, "-c", probe], env=child, cwd=Path(settings.BASE_DIR),
        capture_output=True, text=True, timeout=60, check=True,
    )  # fmt: skip
    assert out.stdout.split()[-1] == "0"


@pytest.mark.parametrize("failure", [None, RuntimeError, DatabaseError])
def test_a_reused_connection_keeps_no_tenant_after_its_scope(
    orgs: dict[str, UUID], migrator: psycopg.Connection[Any], failure: type[Exception] | None
) -> None:
    """Donde la conexión sí se reutiliza: tras el scope de A (confirmado o con un error a mitad,
    tras escribir), la misma sesión de PostgreSQL no conserva nada de A; sin contexto RLS no deja
    ver nada y B solo ve lo suyo."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_backend_pid()")
        pid = cursor.fetchone()[0]
    with pytest.raises(failure) if failure else nullcontext():
        with tenant_scope(TenantContext(orgs["A"], "test")):
            Widget.objects.create(name="de A")
            assert read_context()[0] == str(orgs["A"])
            if failure is RuntimeError:
                raise RuntimeError("fallo a mitad de la petición")
            if failure is DatabaseError:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT 1 / 0")
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_backend_pid(), count(*) FROM tenancy_app_widget")
        assert cursor.fetchone() == (pid, 0)  # la misma sesión; sin contexto, RLS no deja ver nada
    assert read_context() == ("", "", "")
    assert_clean_connection()
    with tenant_scope(TenantContext(orgs["B"], "test")):
        assert list(Widget.objects.values_list("id", flat=True)) == [orgs["widget_B"]]
    stored = migrator.execute("SELECT count(*) FROM tenancy_app_widget").fetchone()
    assert stored == (2 if failure else 3,)  # con un error, la escritura de A se deshizo

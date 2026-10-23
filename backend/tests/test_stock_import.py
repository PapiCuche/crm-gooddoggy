"""F3-20b: las rutas de subir y previsualizar una importación de stock (`…/inventory/imports/`).
Middleware, sesión, motor de autorización y PostgreSQL con el rol `crm_app`. Las reglas de cada
fila las prueba `test_stock_import_preview` (F3-20)."""

import base64
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from django.test import Client

from apps.imports import csvfile
from tests import test_authorization, test_self_context
from tests.factories import make_user
from tests.test_authorization import give
from tests.test_inventory_stock import cell, level, move, wanted
from tests.test_memberships import join
from tests.test_products import send
from tests.test_self_context import signed
from tests.test_stock_import_preview import preview

world, real_stack = test_authorization.world, test_self_context.real_stack
pytestmark = pytest.mark.usefixtures("tenant_db")
IMPORTS = "/api/v1/o/org-a/inventory/imports/"
ADJUST: dict[str, str | None] = {"inventory.view": None, "inventory.adjust": None}


def upload(client: Client, text: str, name: str = "conteo.csv") -> Any:
    content = base64.b64encode(text.encode()).decode()
    return send(client, "post", IMPORTS, {"file_name": name, "content": content})


def rows(client: Client, job: str, **query: str) -> Any:
    return client.get(f"{IMPORTS}{job}/rows/", query)


@pytest.fixture
def ana(world: Any) -> Client:
    give(world.a, world.membership, ADJUST)
    return signed(world.ana)


def test_upload_and_read_the_preview(ana: Client, world: Any) -> None:
    here = cell(world.a)
    move(world.a, world.ana, wanted(here, "RECEIPT", 2))
    response = upload(ana, "sku;almacen;cantidad;notas\nIP16;ALM;5\nNOPE;ALM;1\nIP16;ALM;x\n")
    assert response.status_code == 201, response.content
    job = response.json()
    assert {k: v for k, v in job.items() if k not in ("id", "created_at")} == {
        "status": "VALIDATED",
        "file_name": "conteo.csv",
        "total_rows": 3,
        "valid_rows": 1,
        "error_rows": 2,
        "new_count": 0,
        "update_count": 1,
        "unchanged_count": 0,
        "duplicate_of": None,
        "encoding": "utf-8",
        "ignored_columns": ["notas"],
    }
    found = rows(ana, job["id"]).json()["results"]
    assert [(r["row_number"], r["action"]) for r in found] == [
        (2, "UPDATE"),
        (3, "ERROR"),
        (4, "ERROR"),
    ]
    assert {k: found[0][k] for k in ("sku", "almacen", "cantidad", "quantity", "on_hand_seen")} == {
        "sku": "IP16",
        "almacen": "ALM",
        "cantidad": "5",
        "quantity": 5,
        "on_hand_seen": 2,
    }
    assert (found[0]["errors"], found[2]["quantity"], found[2]["on_hand_seen"]) == ([], None, None)
    errors = rows(ana, job["id"], action="ERROR").json()["results"]
    assert [r["row_number"] for r in errors] == [3, 4]
    assert rows(ana, job["id"], action="X").status_code == 400
    assert rows(ana, job["id"], limit="1").json()["next"]
    assert level(world.a, here) == (2, 0, 2)  # previsualizar no cambia el stock


def test_what_cannot_be_read_is_a_400_on_content(
    ana: Client, migrator: psycopg.Connection[Any]
) -> None:
    for text in ("", "sku,cantidad\nA,1\n", "sku,almacen,cantidad\n"):
        refused = upload(ana, text)
        assert refused.status_code == 400 and list(refused.json()["fields"]) == ["content"]
    for content, says in (
        ("QUJD!", "No es base64."),  # sin `validate`, `!` se ignora
        ("A" * (csvfile.MAX_BYTES * 2), "El archivo pasa de 1024 KB: divídelo."),
    ):
        refused = send(ana, "post", IMPORTS, {"file_name": "a.csv", "content": content})
        assert refused.json()["fields"]["content"][0]["message"] == says
    assert "file_name" in upload(ana, "sku,almacen,cantidad\nA,B,1\n", name="").json()["fields"]
    assert migrator.execute("SELECT count(*) FROM data_imports").fetchone() == (0,)


def test_the_template(ana: Client) -> None:
    response = ana.get(f"{IMPORTS}template/")
    assert response.status_code == 200 and response["Content-Type"] == "text/csv; charset=utf-8"
    assert response.content == "\ufeffsku,almacen,cantidad\r\n".encode()
    assert response["Content-Disposition"] == 'attachment; filename="plantilla-stock.csv"'


def test_it_needs_inventory_adjust_and_stays_in_its_organization(world: Any) -> None:
    bo = make_user(email="bo@example.com")
    join(world.b, bo)
    theirs = preview(world.b, bo, "sku,almacen,cantidad\nB1,ALM,1\n")
    give(world.a, world.membership, {"inventory.view": None})
    viewer = signed(world.ana)
    assert viewer.get(f"{IMPORTS}template/").status_code == 403
    assert rows(viewer, str(theirs.pk)).status_code == 403
    assert upload(viewer, "sku,almacen,cantidad\nB1,ALM,1\n").status_code == 403
    give(world.a, world.membership, ADJUST)
    for job in (theirs.pk, uuid4()):
        assert rows(viewer, str(job)).status_code == 404

"""F7b-06e (ADR-035 §3): una página es de una sola organización, sea cual sea la plataforma
(`channel_accounts_page_owner_ex`, migración `0049`). En la misma organización, Messenger e
Instagram de una página conviven. La negativa es el mismo 400 que una cuenta mal formada o ya
registrada, y el DETAIL de PostgreSQL (que nombra la otra organización) no sale a ningún sitio."""

import importlib
import logging
import threading
import time
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from django.db import connection, connections
from psycopg.conninfo import make_conninfo

from apps.conversations import accounts
from tests.meta_sim.page_graph import FakePageGraph
from tests.meta_sim.social import IG_ACCOUNT_ID, PAGE_ID
from tests.test_channel_accounts_api import signed
from tests.test_self_context import reply
from tests.test_social_accounts import OTHER, connect, crm, meta, real_stack
from tests.test_social_channel_routes_api import BASE, body
from tests.test_team_writes import send

__all__ = ["crm", "meta", "real_stack"]  # los fixtures, para pytest
pytestmark = pytest.mark.usefixtures("tenant_db")
DB = psycopg.Connection[Any]
B = BASE.replace("org-a", "org-b")
FIELD = {"messenger": "page_id", "instagram": "instagram_account_id"}
INSERT = (
    "INSERT INTO channel_accounts (id, organization_id, platform, name, external_account_id,"
    " external_parent_id, display_identifier, status, created_at, updated_at, version)"
    " VALUES (%s, %s, %s, 'Tienda', %s, %s, '', 'PENDING', now(), now(), 1)"
)


def post(crm: SimpleNamespace, org: str, platform: str, **over: Any) -> Any:
    client, base = (signed(crm.ana), BASE) if org == "a" else (signed(crm.luis), B)
    return send(client, "post", f"{base}{platform}/", body(platform, **over))


@pytest.mark.parametrize(
    ("first", "then"),
    [
        ("instagram", "messenger"),
        ("messenger", "instagram"),
        ("instagram", "instagram"),
    ],  # fmt: skip
)
def test_a_page_of_another_organization_gets_the_answer_of_a_malformed_account(
    crm: SimpleNamespace, meta: FakePageGraph, migrator: DB, caplog: pytest.LogCaptureFixture,
    first: str, then: str,
) -> None:  # fmt: skip
    mine = connect(crm, first.upper(), "a")[0]
    over: dict[str, Any] = {}
    if first == then:  # la página se vinculó después a otra cuenta de Instagram
        meta.pages[PAGE_ID], over = OTHER, {"instagram_account_id": OTHER}
    malformed = reply(post(crm, "b", then, **{FIELD[then]: "no-es-un-id"}))
    with caplog.at_level(logging.DEBUG):
        refused = post(crm, "b", then, **over)
    assert reply(refused) == malformed  # la misma respuesta, byte a byte: sin oráculo
    assert refused.status_code == 400 and list(refused.json()["fields"]) == [FIELD[then]]
    assert refused.json()["fields"][FIELD[then]][0]["message"] == accounts.ACCOUNT_REFUSED
    leaked = refused.content.decode() + caplog.text
    for secret in (str(crm.a), str(mine.pk), "page_owner_ex", "conflicts with"):
        assert secret not in leaked, secret  # ni la otra organización ni el DETAIL
    others = "SELECT count(*) FROM {} WHERE organization_id = %s"
    for table in ("channel_accounts", "credentials", "audit_logs"):
        assert migrator.execute(others.format(table), [crm.b]).fetchone() == (0,), table


def test_one_organization_holds_both_platforms_of_its_page_and_freeing_it_lets_another_in(
    crm: SimpleNamespace, meta: FakePageGraph
) -> None:
    made = [post(crm, "a", platform) for platform in ("messenger", "instagram")]
    assert [r.status_code for r in made] == [201, 201]
    assert post(crm, "b", "messenger").status_code == 400
    ana = signed(crm.ana)
    for response in made:
        account = response.json()
        path = f"{BASE}{account['platform'].lower()}/{account['id']}/"
        assert send(ana, "delete", path, None).status_code == 204
    assert [post(crm, "b", p).status_code for p in ("instagram", "messenger")] == [201, 201]
    assert post(crm, "a", "messenger").status_code == 400  # ahora es de B


def app() -> psycopg.Connection[Any]:
    """Una conexión propia como crm_app (el rol de los tests), sin RLS que saltarse."""
    db = connection.settings_dict
    return psycopg.connect(make_conninfo(
        dbname=db["NAME"], user=db["USER"], password=db["PASSWORD"], host=db["HOST"],
        port=db["PORT"] or None,
    ))  # fmt: skip


@pytest.mark.parametrize("ending", ["commit", "rollback"])
def test_the_database_decides_between_two_organizations_connecting_the_page_at_once(
    crm: SimpleNamespace, meta: FakePageGraph, migrator: DB, ending: str
) -> None:
    """Como crm_app, con la RLS de B la fila de A no se ve; aun así la `EXCLUDE` espera a la
    transacción de A y decide: rechaza si A confirma, deja pasar si A se deshace."""
    outcome: list[Any] = []

    def other() -> None:
        try:
            outcome.append(connect(crm, "MESSENGER", "b")[0].platform)
        except Exception as error:  # noqa: BLE001 — se comprueba abajo
            outcome.append(error)
        finally:
            connections.close_all()

    with app() as holder:
        holder.execute("SELECT set_config('app.tenant_id', %s, true)", [str(crm.a)])
        holder.execute(INSERT, [uuid4(), crm.a, "INSTAGRAM", IG_ACCOUNT_ID, PAGE_ID])
        thread = threading.Thread(target=other)
        thread.start()
        waiting = "SELECT count(*) FROM pg_locks WHERE NOT granted"  # B espera la fila de A
        for _ in range(200):
            if migrator.execute(waiting).fetchone() == (1,):
                break
            time.sleep(0.05)
        else:
            pytest.fail("B no llegó a esperar a A")
        getattr(holder, ending)()
    thread.join(10)
    if ending == "commit":
        assert len(outcome) == 1 and isinstance(outcome[0], accounts.BadNumber)
        assert str(outcome[0]) == accounts.ACCOUNT_REFUSED
    else:
        assert outcome == ["MESSENGER"]


def test_raw_sql_as_the_app_role_cannot_put_a_page_in_two_organizations(
    crm: SimpleNamespace, meta: FakePageGraph
) -> None:
    connect(crm, "MESSENGER", "a")
    with app() as raw:
        raw.execute("SELECT set_config('app.tenant_id', %s, true)", [str(crm.b)])
        assert raw.execute("SELECT count(*) FROM channel_accounts").fetchone() == (0,)
        with pytest.raises(psycopg.errors.ExclusionViolation) as caught:
            raw.execute(INSERT, [uuid4(), crm.b, "INSTAGRAM", IG_ACCOUNT_ID, PAGE_ID])
        assert caught.value.diag.constraint_name == "channel_accounts_page_owner_ex"
        raw.rollback()
        raw.execute("SELECT set_config('app.tenant_id', %s, true)", [str(crm.a)])
        raw.execute(INSERT, [uuid4(), crm.a, "INSTAGRAM", IG_ACCOUNT_ID, PAGE_ID])  # su página
        raw.rollback()


def test_the_migration_refuses_pages_already_shared_and_says_only_how_many(
    crm: SimpleNamespace, meta: FakePageGraph, migrator: DB
) -> None:
    step = importlib.import_module("apps.conversations.migrations.0049_channel_accounts_page_owner")
    connect(crm, "INSTAGRAM", "a")
    with app() as raw, pytest.raises(RuntimeError, match="BYPASSRLS"):
        step.refuse_shared_pages(raw.cursor())  # crm_app no ve las demás organizaciones
    with migrator.transaction(force_rollback=True), migrator.cursor() as cursor:
        cursor.execute(INSERT, [uuid4(), crm.a, "MESSENGER", PAGE_ID, None])  # la misma org.
        step.refuse_shared_pages(cursor)  # una página en una sola organización: pasa
        cursor.execute(
            "ALTER TABLE channel_accounts DROP CONSTRAINT channel_accounts_page_owner_ex"
        )
        theirs = uuid4()
        cursor.execute(INSERT, [theirs, crm.b, "INSTAGRAM", OTHER, PAGE_ID])  # lo de antes
        gone = "UPDATE channel_accounts SET deleted_at = {} WHERE id = %s"
        cursor.execute(gone.format("now()"), [theirs])
        step.refuse_shared_pages(cursor)  # una desconectada no cuenta
        cursor.execute(gone.format("NULL"), [theirs])
        with pytest.raises(RuntimeError) as refused:
            step.refuse_shared_pages(cursor)
    message = str(refused.value)
    assert "1 page(s) connected in more than one organization" in message
    for secret in (PAGE_ID, IG_ACCOUNT_ID, OTHER, str(crm.a), str(crm.b), str(theirs)):
        assert secret not in message

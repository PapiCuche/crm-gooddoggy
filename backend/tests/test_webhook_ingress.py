"""F7-03b: la ingesta de los webhooks de Meta (`apps.conversations.ingress`, migración `0008`,
ADR-029 §3): las tablas de plataforma sin acceso directo, la función `SECURITY DEFINER` que inserta
y la ruta que guarda cada ítem firmado. PostgreSQL real con `crm_app` y el simulador de Meta."""

import logging
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest
from django.core.management import call_command
from django.db import DatabaseError, connection

from apps.conversations import ingress
from tests.meta_sim import webhooks
from tests.meta_sim.webhooks import PHONE_NUMBER_ID, WABA_ID
from tests.test_meta_webhook import channel, post
from tests.test_pipelines_tables import as_migrator

__all__ = ["channel"]  # la fixture, para pytest
pytestmark = pytest.mark.usefixtures("tenant_db")
DB = psycopg.Connection[Any]
INSERT = "webhook_ingress_insert(text, text, text, text, text, jsonb)"


def put(key: str, number: str | None = PHONE_NUMBER_ID, **payload: Any) -> bool:
    return ingress.insert("message", key, WABA_ID, number, payload or {"id": key})


def rows(migrator: DB) -> dict[str, dict[str, Any]]:
    cursor = migrator.execute("SELECT * FROM webhook_ingress ORDER BY event_key")
    names = [c.name for c in cursor.description or []]
    return {
        r[names.index("event_key")]: dict(zip(names, r, strict=True)) for r in cursor.fetchall()
    }


def text(wamid: str, body: str = "Hola") -> dict[str, Any]:
    return webhooks.text_message(body, wamid=wamid)


def deliver(*messages: dict[str, Any]) -> Any:
    value = webhooks.value(messages=list(messages))
    return post(webhooks.deliver(webhooks.encode(webhooks.envelope(value))))


def test_the_tables_are_closed_to_the_app_role_and_only_the_function_opens_them(
    migrator: DB,
) -> None:
    for table in ("webhook_ingress", "webhook_failures"):
        privileges = migrator.execute(
            "SELECT p FROM unnest(ARRAY['SELECT','INSERT','UPDATE','DELETE','TRUNCATE',"
            "'REFERENCES','TRIGGER']) AS p WHERE has_table_privilege('crm_app', %s, p)",
            [table],
        ).fetchall()
        assert privileges == [], table
        flags = migrator.execute(
            "SELECT relrowsecurity FROM pg_class WHERE relname = %s", [table]
        ).fetchone()
        policies = migrator.execute(
            "SELECT count(*) FROM pg_policies WHERE tablename = %s", [table]
        ).fetchone()
        assert flags == (True,) and policies == (0,), table  # RLS activada y sin política
        with connection.cursor() as cursor, pytest.raises(DatabaseError, match="permission denied"):
            cursor.execute(f"SELECT 1 FROM {table}")  # noqa: S608
        connection.close()
    found = migrator.execute(
        "SELECT p.prosecdef, pg_get_userbyid(p.proowner), p.proconfig,"
        " has_function_privilege('public', p.oid, 'EXECUTE'),"
        " has_function_privilege('crm_app', p.oid, 'EXECUTE')"
        " FROM pg_proc p WHERE p.oid = %s::regprocedure",
        [f"public.{INSERT}"],
    ).fetchone()
    fixed = ["search_path=pg_catalog, public, pg_temp"]
    assert found == (True, "crm_migrator", fixed, False, True)


def test_the_same_event_twice_is_one_row_and_the_second_is_not_new(migrator: DB) -> None:
    assert put("wa:msg:1") is True and put("wa:msg:1") is False
    assert put("wa:msg:2") is True
    assert migrator.execute("SELECT count(*) FROM webhook_ingress").fetchone() == (2,)
    found = rows(migrator)["wa:msg:1"]
    assert (found["status"], found["attempts"], found["provider"]) == ("RECEIVED", 0, "meta")
    assert found["routed_organization_id"] is None and len(found["payload_hash"]) == 64
    other = rows(migrator)["wa:msg:2"]
    again = migrator.execute(
        "SELECT encode(sha256(convert_to(payload::text, 'UTF8')), 'hex') FROM webhook_ingress"
        " WHERE event_key = 'wa:msg:1'"
    ).fetchone()
    assert again == (found["payload_hash"],) and other["payload_hash"] != found["payload_hash"]


@pytest.mark.parametrize(
    "bad",
    [
        {"kind": "otro"}, {"key": ""}, {"key": "x" * 701}, {"waba": "abc"}, {"waba": None},
        {"number": "abc"}, {"payload": None}, {"payload": [1]}, {"provider": "Meta"},
        {"key": "con espacio"}, {"key": "a\nb"}, {"payload": {"x": "a" * 1_048_577}},
    ],
)  # fmt: skip
def test_the_insert_function_refuses_what_is_not_a_validated_item(bad: dict[str, Any]) -> None:
    args: dict[str, Any] = {"provider": "meta", "key": "k", "kind": "message", "waba": WABA_ID,
                            "number": PHONE_NUMBER_ID, "payload": {}} | bad  # fmt: skip
    payload = None if args["payload"] is None else psycopg.types.json.Jsonb(args["payload"])
    params: list[Any] = [args["provider"], args["key"], args["kind"], args["waba"], args["number"]]
    with connection.cursor() as cursor, pytest.raises(DatabaseError, match="no válidos"):
        cursor.execute(
            "SELECT public.webhook_ingress_insert(%s, %s, %s, %s, %s, %s::jsonb)",
            [*params, payload],  # type: ignore[list-item]
        )
    connection.close()


def test_a_nul_character_or_a_lone_surrogate_in_the_payload_never_breaks_the_insert(
    migrator: DB,
) -> None:
    assert ingress.insert("message", "k", WABA_ID, None, {"t": "a\x00b", "\x00": ["\ud800", "ok"]})
    stored = rows(migrator)["k"]["payload"]
    assert stored == {"t": "a�b", "�": ["?", "ok"]}


def test_a_signed_delivery_is_stored_once_per_item_and_a_repeat_adds_nothing(
    migrator: DB,
) -> None:
    assert deliver(text("wamid.1"), text("wamid.2")).status_code == 200
    assert deliver(text("wamid.1"), text("wamid.2")).status_code == 200  # Meta repite la entrega
    assert sorted(rows(migrator)) == ["wa:msg:wamid.1", "wa:msg:wamid.2"]
    found = rows(migrator)["wa:msg:wamid.1"]
    assert (found["status"], found["kind"], found["waba_id"]) == ("RECEIVED", "message", WABA_ID)
    assert found["phone_number_id"] == PHONE_NUMBER_ID and found["payload"]["id"] == "wamid.1"


def test_one_bad_item_does_not_poison_the_batch_of_other_customers(migrator: DB) -> None:
    deliver(text("wamid.1"), text("wamid.2") | {"text": {"body": 5}}, text("wamid.3"))
    assert sorted(rows(migrator)) == ["wa:msg:wamid.1", "wa:msg:wamid.3"]


def test_if_the_database_cannot_store_the_answer_is_503_so_that_meta_retries(
    migrator: DB, monkeypatch: pytest.MonkeyPatch
) -> None:
    def down(items: list[Any]) -> int:
        raise DatabaseError("sin base")

    monkeypatch.setattr(ingress, "store", down)
    assert deliver(text("wamid.1")).status_code == 503
    monkeypatch.undo()
    assert deliver(text("wamid.1")).status_code == 200 and len(rows(migrator)) == 1


def test_the_migration_goes_back_and_forward_and_the_tables_come_back_closed(migrator: DB) -> None:
    exists = "SELECT to_regclass('public.webhook_ingress') IS NOT NULL"
    function = "SELECT count(*) FROM pg_proc WHERE proname = 'webhook_ingress_insert'"
    put("k")
    with as_migrator():
        call_command("migrate", "conversations", "0007", verbosity=0)
    assert migrator.execute(exists).fetchone() == (False,)
    assert migrator.execute(function).fetchone() == (0,)
    with as_migrator():
        call_command("migrate", "conversations", verbosity=0)
    assert migrator.execute(exists).fetchone() == (True,)
    assert migrator.execute(function).fetchone() == (1,)
    grant = "SELECT has_table_privilege('crm_app', 'webhook_ingress', 'SELECT')"
    assert migrator.execute(grant).fetchone() == (False,) and put("again")


def test_a_delivery_is_stored_whole_or_not_at_all(
    migrator: DB, monkeypatch: pytest.MonkeyPatch
) -> None:
    from django.db import OperationalError

    def item(key: str, kind: str) -> Any:
        return SimpleNamespace(
            kind=kind, key=key, waba_id=WABA_ID, phone_number_id=None, payload={"id": key}
        )

    real = ingress.insert

    def flaky(kind: str, key: str, *rest: Any) -> bool:
        if key == "wa:msg:2":
            raise OperationalError("la base se cayó")  # transitorio: todo o nada
        return real(kind, key, *rest)

    monkeypatch.setattr(ingress, "insert", flaky)
    with pytest.raises(OperationalError):
        ingress.store([item("wa:msg:1", "message"), item("wa:msg:2", "message")])
    monkeypatch.undo()
    assert migrator.execute("SELECT count(*) FROM webhook_ingress").fetchone() == (0,)
    assert ingress.store([item("wa:msg:1", "message"), item("wa:msg:2", "status")]) == 2
    assert ingress.store([item("wa:msg:1", "message"), item("wa:msg:3", "status")]) == 1


def test_an_infinite_number_is_stored_as_null_and_does_not_make_meta_retry_forever(
    migrator: DB,
) -> None:
    raw = (
        b'{"object":"whatsapp_business_account","entry":[{"id":"%b","changes":[{"field":"messages",'
        b'"value":{"metadata":{"phone_number_id":"%b"},"messages":[{"type":"text","id":"w1",'
        b'"text":{"body":"x"},"n":1e999,"m":-1e999,"ok":1.5}]}}]}]}'
    ) % (WABA_ID.encode(), PHONE_NUMBER_ID.encode())
    assert post(webhooks.deliver(raw)).status_code == 200
    stored = rows(migrator)["wa:msg:w1"]["payload"]
    assert (stored["n"], stored["m"], stored["ok"]) == (None, None, 1.5)


def test_a_real_database_refusal_drops_that_item_and_the_rest_still_goes_in(
    migrator: DB, caplog: pytest.LogCaptureFixture
) -> None:
    """El rechazo real de la función aborta la transacción: sin punto de guardado, el siguiente
    ítem fallaría con «transacción abortada»."""

    def item(key: str, kind: str) -> Any:
        return SimpleNamespace(
            kind=kind, key=key, waba_id=WABA_ID, phone_number_id=None, payload={"id": key}
        )

    caplog.set_level(logging.DEBUG)
    batch = [item("wa:msg:1", "message"), item("wa:msg:2", "otro"), item("wa:msg:3", "message")]
    assert ingress.store(batch) == 2
    assert sorted(rows(migrator)) == ["wa:msg:1", "wa:msg:3"]
    assert "meta.webhook.refused" in caplog.text and "'count': 1" in caplog.text

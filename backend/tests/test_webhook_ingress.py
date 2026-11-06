"""F7-03b: la ingesta de los webhooks de Meta (`apps.conversations.ingress`, migración `0008`,
ADR-029 §3): las tablas de plataforma sin acceso directo, la función `SECURITY DEFINER` que inserta
y la ruta que guarda cada ítem firmado. PostgreSQL real con `crm_app` y el simulador de Meta."""

import logging
import threading
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from django.core.management import call_command
from django.db import DatabaseError, DataError, connection
from django.utils import timezone

from apps.conversations import ingress, tasks
from apps.conversations.models import ChannelAccount
from core.tenancy.scope import tenant_scope
from tests.meta_sim import webhooks
from tests.meta_sim.webhooks import PHONE_NUMBER_ID, WABA_ID
from tests.test_contacts_api import crm, real_stack
from tests.test_memberships import ctx
from tests.test_meta_webhook import channel, post
from tests.test_pipelines_tables import as_migrator
from tests.test_tenant_task_signature import broker
from tests.test_whatsapp_account import make

__all__ = ["broker", "channel", "crm", "real_stack"]  # las fixtures, para pytest
pytestmark = pytest.mark.usefixtures("tenant_db")
DB = psycopg.Connection[Any]
INSERT = "webhook_ingress_insert(text, text, text, text, text, jsonb)"
FUNCTIONS = (
    INSERT,
    "webhook_ingress_claim(integer, integer)",
    "webhook_ingress_mark(uuid, integer, text, uuid, uuid, text)",
    "webhook_ingress_list(text, integer)",
    "webhook_ingress_purge(integer, integer)",
    "resolve_channel_account(text, text)",
)
OTHER_WABA, OTHER_NUMBER = "100000000000007", "100000000000008"


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


def deliver(*messages: dict[str, Any], waba: str = WABA_ID, number: str = PHONE_NUMBER_ID) -> Any:
    value = webhooks.value(messages=list(messages), phone_number_id=number)
    return post(webhooks.deliver(webhooks.encode(webhooks.envelope(value, waba_id=waba))))


def test_the_tables_are_closed_to_the_app_role_and_only_the_functions_open_them(
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
    fixed = ["search_path=pg_catalog, public, pg_temp"]
    for function in FUNCTIONS:
        found = migrator.execute(
            "SELECT p.prosecdef, pg_get_userbyid(p.proowner), p.proconfig,"
            " has_function_privilege('public', p.oid, 'EXECUTE'),"
            " has_function_privilege('crm_app', p.oid, 'EXECUTE')"
            " FROM pg_proc p WHERE p.oid = %s::regprocedure",
            [f"public.{function}"],
        ).fetchone()
        assert found == (True, "crm_migrator", fixed, False, True), function


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


def row(migrator: DB, key: str) -> dict[str, Any]:
    cursor = migrator.execute("SELECT * FROM webhook_ingress WHERE event_key = %s", [key])
    names = [c.name for c in cursor.description or []]
    found = cursor.fetchone()
    assert found is not None, key
    return dict(zip(names, found, strict=True))


def force(migrator: DB, key: str, sql: str) -> None:
    migrator.execute(f"UPDATE webhook_ingress SET {sql} WHERE event_key = %s", [key])  # noqa: S608


def account(org: UUID, **over: Any) -> ChannelAccount:
    return make(org, **over)


def test_a_claim_leases_the_oldest_due_items_once_and_an_expired_lease_comes_back(
    migrator: DB,
) -> None:
    for n in range(5):
        put(f"k{n}")
    first = ingress.claim(2)
    assert [c.attempts for c in first] == [1, 1] and len(ingress.claim(10)) == 3
    assert ingress.claim(10) == []  # nada más hasta que el reclamo caduque
    kept = first[0]
    force(migrator, f"k{0}", "claimed_until = now() - interval '1 second'")
    again = ingress.claim(10)
    assert [c.id for c in again] == [kept.id] and again[0].attempts == 2
    assert again[0].payload == {"id": "k0"}
    assert row(migrator, "k0")["status"] == "PROCESSING"


def test_an_item_whose_lease_expires_after_eight_attempts_is_dead_with_a_failure(
    migrator: DB,
) -> None:
    put("k")
    force(
        migrator, "k", "status = 'PROCESSING', attempts = 8, claimed_until = now() - interval '1 s'"
    )
    assert ingress.claim(10) == []
    dead = row(migrator, "k")
    assert dead["status"] == "DEAD" and dead["payload"] is not None  # DEAD conserva el payload
    reasons = migrator.execute("SELECT reason, attempt FROM webhook_failures").fetchall()
    assert reasons == [("LEASE_EXPIRED", 8)]


def test_two_workers_claiming_at_once_never_get_the_same_item() -> None:
    for n in range(40):
        put(f"k{n:02d}")
    got: list[list[UUID]] = []

    def worker() -> None:
        try:
            got.append([c.id for c in ingress.claim(25)])
        finally:
            connection.close()

    threads = [threading.Thread(target=worker) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    flat = [i for ids in got for i in ids]
    assert len(flat) == len(set(flat)) == 40 and all(len(ids) <= 25 for ids in got)


def test_marking_closes_the_item_empties_the_payload_and_keeps_the_hash(
    crm: SimpleNamespace, migrator: DB
) -> None:
    account = make(crm.a)
    put("k", payload_ok=1)
    before = row(migrator, "k")["payload_hash"]
    [item] = ingress.claim(1)
    assert ingress.mark(item, "PROCESSED", crm.a, account.pk) == "PROCESSED"
    done = row(migrator, "k")
    assert done["payload"] is None and done["payload_hash"] == before and done["processed_at"]
    assert (done["routed_organization_id"], done["routed_channel_account_id"]) == (
        crm.a,
        account.pk,
    )
    assert ingress.mark(item, "IGNORED", reason="X") is None  # ya no estaba reclamado


@pytest.mark.parametrize(
    "args",
    [
        {"status": "OTRO"}, {"status": "PROCESSED"}, {"status": "FAILED", "org": True},
        {"status": "IGNORED", "reason": "minúscula"}, {"status": "IGNORED", "org": "alone"},
        {"status": "IGNORED", "org": "foreign"},
    ],
)  # fmt: skip
def test_marking_refuses_a_status_or_a_route_it_cannot_trust(
    crm: SimpleNamespace, args: dict[str, Any]
) -> None:
    mine, theirs = make(crm.a), make(crm.b, external_account_id=OTHER_NUMBER)
    put("k")
    [item] = ingress.claim(1)
    org = {True: (crm.a, mine.pk), "alone": (crm.a, None), "foreign": (crm.a, theirs.pk)}
    chosen = org.get(args.get("org"), (None, None))
    with pytest.raises(DatabaseError):
        ingress.mark(item, args["status"], chosen[0], chosen[1], args.get("reason"))


def test_a_failure_waits_longer_each_time_and_the_eighth_is_dead(migrator: DB) -> None:
    put("k")
    waits = []
    for attempt in range(1, 9):
        force(migrator, "k", "next_attempt_at = now() - interval '1 second'")
        [item] = ingress.claim(1)
        assert item.attempts == attempt
        state = ingress.mark(item, "FAILED", reason="ERROR")
        gap = migrator.execute(
            "SELECT extract(epoch FROM next_attempt_at - now()) FROM webhook_ingress"
        ).fetchone()
        waits.append(round(gap[0] / 30) if gap else 0)
        assert state == ("DEAD" if attempt == 8 else "FAILED")
    assert waits[:5] == [1, 4, 20, 120, 720] and ingress.claim(10) == []
    assert migrator.execute("SELECT count(*) FROM webhook_failures").fetchone() == (8,)
    assert row(migrator, "k")["status"] == "DEAD"


def test_the_list_gives_only_metadata_never_the_payload() -> None:
    put("a", texto="el contenido del cliente")
    put("b")
    [item] = ingress.claim(1)
    ingress.mark(item, "IGNORED", reason="UNKNOWN_NUMBER")
    everything = ingress.listing()
    assert {r[4] for r in everything} == {"RECEIVED", "IGNORED"} and "contenido" not in str(
        everything
    )
    assert [r[4] for r in ingress.listing("IGNORED")] == ["IGNORED"] and len(
        ingress.listing(None, 1)
    ) == 1
    with pytest.raises(DatabaseError):
        ingress.listing("otro")


def test_the_purge_deletes_what_is_older_than_the_cap_and_its_failures(migrator: DB) -> None:
    put("old")
    put("new")
    force(migrator, "old", "received_at = now() - interval '91 days'")
    [item] = ingress.claim(1)
    ingress.mark(item, "FAILED", reason="ERROR")
    assert migrator.execute("SELECT count(*) FROM webhook_failures").fetchone() == (1,)
    assert ingress.purge() == 1 and ingress.purge() == 0
    assert migrator.execute("SELECT event_key FROM webhook_ingress").fetchall() == [("new",)]
    assert migrator.execute("SELECT count(*) FROM webhook_failures").fetchone() == (0,)


def test_the_resolver_gives_the_live_whatsapp_account_of_a_number_and_nothing_else(
    crm: SimpleNamespace,
) -> None:
    mine = make(crm.a)
    assert ingress.resolve("WHATSAPP", PHONE_NUMBER_ID) == (crm.a, mine.pk)
    assert ingress.resolve("WHATSAPP", "100000000000099") is None
    assert ingress.resolve("INSTAGRAM", PHONE_NUMBER_ID) is None
    assert ingress.resolve("WHATSAPP", "' OR 1=1 --") is None
    with tenant_scope(ctx(crm.a)):
        ChannelAccount.objects.filter(pk=mine.pk).update(deleted_at=timezone.now())
    assert ingress.resolve("WHATSAPP", PHONE_NUMBER_ID) is None  # desconectada: ya no es de nadie
    other = make(crm.b)  # el número se libera y otra organización lo registra
    assert ingress.resolve("WHATSAPP", PHONE_NUMBER_ID) == (crm.b, other.pk)


def test_the_tenant_comes_from_the_registered_account_never_from_the_payload(
    crm: SimpleNamespace, migrator: DB
) -> None:
    """Prueba T11: un payload que dice ser de otra organización no cambia el enrutado."""
    mine, theirs = account(crm.a), account(crm.b, external_account_id=OTHER_NUMBER)
    lie = text("wamid.1") | {"organization_id": str(crm.b), "channel_account_id": str(theirs.pk)}
    assert deliver(lie).status_code == 200
    assert ingress.process() == {"PROCESSED": 1}
    found = rows(migrator)["wa:msg:wamid.1"]
    assert (found["routed_organization_id"], found["routed_channel_account_id"]) == (crm.a, mine.pk)


def test_a_signed_event_for_a_number_of_another_organization_is_not_routed_to_the_wrong_account(
    crm: SimpleNamespace, migrator: DB, caplog: pytest.LogCaptureFixture
) -> None:
    """El cruce del WABA: el número es de B, pero el evento llega firmado con el WABA de A."""
    account(crm.a)
    theirs = account(crm.b, external_account_id=OTHER_NUMBER, external_parent_id=OTHER_WABA)
    caplog.set_level(logging.DEBUG)
    assert deliver(text("wamid.1"), number=OTHER_NUMBER).status_code == 200  # WABA de A
    assert ingress.process() == {"IGNORED": 1}
    found = rows(migrator)["wa:msg:wamid.1"]
    assert found["status"] == "IGNORED" and found["ignored_reason"] == "WABA_MISMATCH"
    assert found["routed_organization_id"] is None and found["routed_channel_account_id"] is None
    assert found["payload"] is None and "meta.webhook.waba_mismatch" in caplog.text
    assert str(theirs.pk) in caplog.text  # solo el id de la cuenta, para la alerta


def test_an_unknown_number_and_a_change_without_a_number_are_ignored(
    crm: SimpleNamespace, migrator: DB
) -> None:
    account(crm.a)
    deliver(text("wamid.1"), number="100000000000099")
    change = {"field": "account_update", "value": {"event": "x"}}
    body = {"object": "whatsapp_business_account", "entry": [{"id": WABA_ID, "changes": [change]}]}
    post(webhooks.deliver(webhooks.encode(body)))
    assert ingress.process() == {"IGNORED": 2}
    reasons = sorted(r["ignored_reason"] for r in rows(migrator).values())
    assert reasons == ["NO_NUMBER", "UNKNOWN_NUMBER"]


def test_a_disconnected_number_is_not_routed(crm: SimpleNamespace, migrator: DB) -> None:
    gone = account(crm.a)
    with tenant_scope(ctx(crm.a)):
        ChannelAccount.objects.filter(pk=gone.pk).update(deleted_at=timezone.now())
    deliver(text("wamid.1"))
    assert ingress.process() == {"IGNORED": 1}


def test_an_error_while_routing_fails_the_item_with_a_code_and_never_loses_it(
    crm: SimpleNamespace, migrator: DB, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:  # fmt: skip
    account(crm.a)
    deliver(text("wamid.1", "Texto privado del cliente"))

    def boom(*args: Any) -> None:
        raise RuntimeError("Texto privado del cliente")

    monkeypatch.setattr(ingress, "resolve", boom)
    caplog.set_level(logging.DEBUG)
    assert ingress.process() == {"FAILED": 1}
    found = rows(migrator)["wa:msg:wamid.1"]
    assert found["status"] == "FAILED" and found["payload"] is not None and found["attempts"] == 1
    assert migrator.execute("SELECT reason FROM webhook_failures").fetchall() == [("ERROR",)]
    assert "Texto privado" not in caplog.text and "RuntimeError" in caplog.text
    monkeypatch.undo()
    assert ingress.process() == {}  # la espera creciente: aún no toca
    force(migrator, "wa:msg:wamid.1", "next_attempt_at = now() - interval '1 second'")
    assert ingress.process() == {"PROCESSED": 1}


def test_a_broker_that_is_down_never_spoils_the_answer(
    crm: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def down(*args: Any, **kwargs: Any) -> None:
        raise OSError("sin broker")

    monkeypatch.setattr(tasks.process_webhook_ingress, "apply_async", down)
    caplog.set_level(logging.DEBUG)
    assert deliver(text("wamid.1")).status_code == 200 and "meta.webhook.wake_failed" in caplog.text


def test_the_processor_tasks_validate_their_arguments_for_real_and_are_scheduled(
    settings: Any, broker: list[Any]
) -> None:
    """Lección de #568: `apply_async` valida la firma registrada, no la de la función."""
    scheduled = (
        (tasks.process_webhook_ingress, {"batch_size": 5}),
        (tasks.purge_webhook_ingress, {"days": 90, "batch_size": 10}),
    )
    for task, kwargs in scheduled:
        assert task.tenancy == "platform" and task.apply_async(kwargs=kwargs).id
        assert task.apply_async().id
        with pytest.raises(TypeError):
            task.apply_async(kwargs={"organization_id": str(uuid4())})
    schedule = settings.CELERY_BEAT_SCHEDULE
    assert schedule["conversations.process_webhook_ingress"]["schedule"] == 15.0
    assert "conversations.purge_webhook_ingress" in schedule


def test_the_tasks_do_the_work_when_called(crm: SimpleNamespace, migrator: DB) -> None:
    account(crm.a)
    deliver(text("wamid.1"))
    put("old")
    force(migrator, "old", "received_at = now() - interval '91 days'")
    assert tasks.process_webhook_ingress() == {"PROCESSED": 2}  # el de la entrega y el viejo
    assert tasks.purge_webhook_ingress() == 1  # solo el viejo pasa del tope
    assert list(rows(migrator)) == ["wa:msg:wamid.1"]


def test_a_signed_delivery_is_stored_once_per_item_and_routed_to_the_registered_account(
    crm: SimpleNamespace, migrator: DB, monkeypatch: pytest.MonkeyPatch
) -> None:
    mine = account(crm.a)
    woken: list[int] = []
    monkeypatch.setattr(ingress, "wake", lambda: woken.append(1))
    assert deliver(text("wamid.1"), text("wamid.2")).status_code == 200
    assert deliver(text("wamid.1"), text("wamid.2")).status_code == 200  # Meta repite la entrega
    assert sorted(rows(migrator)) == ["wa:msg:wamid.1", "wa:msg:wamid.2"] and woken == [1]
    assert ingress.process() == {"PROCESSED": 2}
    for found in rows(migrator).values():
        assert found["status"] == "PROCESSED" and found["payload"] is None
        assert (found["routed_organization_id"], found["routed_channel_account_id"]) == (
            crm.a,
            mine.pk,
        )
    assert ingress.process() == {}


def test_only_the_holder_of_the_claim_closes_an_item_and_a_closed_one_never_changes(
    crm: SimpleNamespace, migrator: DB
) -> None:
    mine = make(crm.a)
    put("k")
    force(migrator, "k", "next_attempt_at = now() - interval '1 second'")
    [first] = ingress.claim(1)
    force(migrator, "k", "claimed_until = now() - interval '1 second'")  # caduca el plazo
    [second] = ingress.claim(1)  # otro trabajador lo reclama: intento 2
    assert (first.attempts, second.attempts) == (1, 2)
    assert ingress.mark(first, "PROCESSED", crm.a, mine.pk) is None  # el primero ya no puede
    assert ingress.mark(first, "FAILED", reason="ERROR") is None
    assert migrator.execute("SELECT count(*) FROM webhook_failures").fetchone() == (0,)
    assert row(migrator, "k")["status"] == "PROCESSING"
    assert ingress.mark(second, "PROCESSED", crm.a, mine.pk) == "PROCESSED"
    assert ingress.mark(second, "FAILED", reason="ERROR") is None  # lo cerrado no cambia
    assert row(migrator, "k")["status"] == "PROCESSED" and row(migrator, "k")["payload"] is None


def test_a_dead_item_is_never_claimed_or_brought_back(migrator: DB) -> None:
    put("k")
    force(migrator, "k", "status = 'DEAD', attempts = 8")
    assert ingress.claim(10) == []
    found = migrator.execute("SELECT id FROM webhook_ingress WHERE event_key = 'k'").fetchone()
    assert found is not None
    dead = ingress.Claimed(found[0], "message", WABA_ID, None, {}, 8)
    assert ingress.mark(dead, "FAILED", reason="ERROR") is None
    assert row(migrator, "k")["status"] == "DEAD"


def test_the_failure_and_the_refusal_logs_never_carry_the_payload_or_the_signature(
    crm: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    body = webhooks.encode(
        webhooks.envelope(webhooks.value(messages=[text("wamid.1", "Texto privado del cliente")]))
    )
    delivery = webhooks.deliver(body)

    def down(items: list[Any]) -> int:
        raise DatabaseError("Texto privado del cliente")

    monkeypatch.setattr(ingress, "store", down)
    assert post(delivery).status_code == 503
    monkeypatch.undo()
    real = ingress.insert

    def refuse(kind: str, key: str, *rest: Any) -> bool:
        raise DataError("Texto privado del cliente")

    monkeypatch.setattr(ingress, "insert", refuse)
    assert post(delivery).status_code == 200
    monkeypatch.setattr(ingress, "insert", real)
    signature = delivery.headers[webhooks.SIGNATURE_HEADER]
    assert "meta.webhook.store_failed" in caplog.text and "meta.webhook.refused" in caplog.text
    for secret in ("Texto privado", webhooks.TEST_APP_SECRET, signature):
        assert secret not in caplog.text, secret


def test_a_delivery_of_the_maximum_size_is_stored_in_a_reasonable_time(migrator: DB) -> None:
    import time

    items = [
        SimpleNamespace(
            kind="message", key=f"wa:msg:{n}", waba_id=WABA_ID, phone_number_id=None,
            payload={"id": f"wamid.{n}", "text": {"body": "x" * 200}},
        )
        for n in range(500)
    ]  # fmt: skip
    start = time.monotonic()
    assert ingress.store(items) == 500
    assert time.monotonic() - start < 10  # 500 viajes en una transacción: lejos de ser un cuello


def test_going_back_to_the_tables_only_removes_the_processing_functions(migrator: DB) -> None:
    names = "SELECT count(*) FROM pg_proc WHERE proname LIKE 'webhook_ingress_%%' OR proname = %s"
    put("k")
    with as_migrator():
        call_command("migrate", "conversations", "0008", verbosity=0)
    assert migrator.execute(names, ["resolve_channel_account"]).fetchone() == (1,)  # solo el insert
    assert migrator.execute("SELECT count(*) FROM webhook_ingress").fetchone() == (1,)
    with as_migrator():
        call_command("migrate", "conversations", verbosity=0)
    assert migrator.execute(names, ["resolve_channel_account"]).fetchone() == (6,)

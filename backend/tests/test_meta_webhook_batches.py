"""F7-11b: los lotes grandes de Meta nunca se pierden en silencio. Meta agrupa «un máximo de 1000
actualizaciones» por POST y sus cuerpos miden «hasta 3 MB»; lo que no recibe un 200 lo reintenta
hasta 7 días. Hasta 1000 ítems y 3 MiB se guardan enteros; más es 503, sin guardar nada; un ítem
que la base rechaza queda registrado como `DEAD` (sin payload) y el resto se guarda. PostgreSQL
real con `crm_app` y el simulador de Meta."""

import logging
import threading
import time
from collections.abc import Sequence
from typing import Any

import psycopg
import pytest
from django.core.management import call_command
from django.db import DatabaseError, OperationalError, connection

from apps.conversations import ingress, meta_webhook
from tests.meta_sim import webhooks
from tests.meta_sim.webhooks import PHONE_NUMBER_ID, WABA_ID
from tests.test_meta_webhook import channel, post
from tests.test_pipelines_tables import as_migrator

__all__ = ["channel"]
pytestmark = pytest.mark.usefixtures("tenant_db")
DB = psycopg.Connection[Any]
REFUSE = "SELECT public.webhook_ingress_refuse(%s, %s, %s, %s, %s, %s, %s)"
HASH = "0" * 64


def count(migrator: DB, sql: str = "SELECT count(*) FROM webhook_ingress") -> int:
    return int(migrator.execute(sql).fetchone()[0])  # type: ignore[index]


def msg(n: int, body: str = "Hola") -> dict[str, Any]:
    return webhooks.text_message(body, wamid=f"wamid.B{n:05d}")


def change(messages: Sequence[Any] = (), statuses: Sequence[Any] = ()) -> dict[str, Any]:
    return {
        "field": "messages",
        "value": webhooks.value(messages=list(messages), statuses=list(statuses)),
    }


def body(*entries: list[Any]) -> bytes:
    found = [{"id": WABA_ID, "changes": changes} for changes in entries]
    return webhooks.encode({"object": "whatsapp_business_account", "entry": found})


def statuses(start: int, n: int) -> list[dict[str, Any]]:
    return [webhooks.status(f"wamid.S{k:05d}", "delivered") for k in range(start, start + n)]


SHAPES = {  # n -> un cuerpo con exactamente n ítems, de cada forma en que Meta los agrupa
    "one_list": lambda n: body([change([msg(k) for k in range(n)])]),
    "entries": lambda n: body(*[[change([msg(k)])] for k in range(n)]),
    "changes": lambda n: body([change([msg(k)]) for k in range(n)]),
    "mixed": lambda n: body(
        *[
            [
                change([msg(e * 100 + c * 10 + k) for k in range(5)], statuses(e * 100 + c * 10, 5))
                for c in range(10)
            ]
            for e in range(n // 100)
        ],
        *([[change([msg(9000 + k) for k in range(n % 100)])]] if n % 100 else []),
    ),  # fmt: skip
}


def test_the_caps_are_the_documented_ones() -> None:
    assert meta_webhook.BODY_MAX == 3 * 1024 * 1024 and meta_webhook.MAX_ITEMS == 1000
    assert meta_webhook.MAX_ENTRIES == meta_webhook.MAX_CHANGES == meta_webhook.MAX_ROWS == 1000


@pytest.mark.parametrize("shape", SHAPES)
def test_a_batch_of_a_thousand_updates_is_stored_whole_and_a_repeat_adds_nothing(
    shape: str, migrator: DB
) -> None:
    delivery = webhooks.deliver(SHAPES[shape](1000))
    assert len(meta_webhook.parse(delivery.body).items) == 1000
    assert post(delivery).status_code == 200 and count(migrator) == 1000
    assert post(delivery).status_code == 200 and count(migrator) == 1000  # Meta repite


@pytest.mark.parametrize("shape", SHAPES)
def test_one_update_over_the_documented_cap_is_503_and_nothing_is_stored(
    shape: str, migrator: DB, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    response = post(webhooks.deliver(SHAPES[shape](1001)))
    assert (response.status_code, response.content) == (503, b"") and count(migrator) == 0
    [record] = [r for r in caplog.records if "meta.webhook.over_cap" in r.getMessage()]
    assert record.levelno == logging.ERROR and "Hola" not in caplog.text
    assert "meta.webhook.received" not in caplog.text and "wamid" not in caplog.text


def test_over_the_cap_counts_all_lists_and_rejected_parts_do_not_hide_it(migrator: DB) -> None:
    over = [
        body([change([msg(k) for k in range(600)], statuses(0, 401))]),  # 1001 en un cambio
        body([change([msg(k) for k in range(500)]), change([msg(600 + k) for k in range(501)])]),
        body(*[[change([msg(k)])] for k in range(1000)], [{"field": "x"}]),  # 1001 entradas
        body(["x"] * 1001),  # 1001 cambios, aunque ninguno valga
    ]
    for raw in over:
        assert post(webhooks.deliver(raw)).status_code == 503
    assert count(migrator) == 0
    exactly = body([change([msg(k) for k in range(999)])], [{"field": "x"}])  # 999 y un descarte
    assert post(webhooks.deliver(exactly)).status_code == 200 and count(migrator) == 999


def test_the_signature_is_checked_before_parsing_an_oversized_batch(
    migrator: DB, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    parsed: list[bytes] = []
    monkeypatch.setattr(meta_webhook, "parse", lambda raw: parsed.append(raw))
    raw = SHAPES["one_list"](1001)
    for delivery in (webhooks.deliver(raw, secret=None),
                     webhooks.deliver(raw, signature="sha256=" + "0" * 64),
                     webhooks.deliver(raw, secret="otro-ficticio")):  # noqa: S106  # fmt: skip
        response = post(delivery)
        assert (response.status_code, response.content) == (401, b"")
    assert parsed == [] and count(migrator) == 0 and "meta.webhook" not in caplog.text


def test_a_thousand_long_messages_fit_under_the_body_cap(migrator: DB) -> None:
    raw = SHAPES["one_list"](1000).replace(b'"Hola"', b'"' + b"x" * 2500 + b'"')
    assert 1 << 20 < len(raw) <= meta_webhook.BODY_MAX  # el tope de antes (1 MiB) lo rechazaba
    start = time.monotonic()
    assert post(webhooks.deliver(raw)).status_code == 200
    elapsed = time.monotonic() - start
    assert count(migrator) == 1000 and elapsed < 10, elapsed


def test_a_body_over_the_cap_is_413_and_logged_without_its_content(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    raw = b'{"x":"Texto privado"}' + b" " * meta_webhook.BODY_MAX
    assert post(webhooks.deliver(raw)).status_code == 413
    assert "meta.webhook.too_large" in caplog.text and "Texto privado" not in caplog.text


def poison(n: int) -> dict[str, Any]:
    """Un mensaje que pasa el esquema y que la función de la base nunca guarda (más de 1 MiB)."""
    return msg(n) | {"extra": "Texto privado " + "x" * (1 << 20)}


def failures(migrator: DB) -> list[tuple[Any, ...]]:
    return migrator.execute(
        "SELECT w.event_key, w.status, w.payload IS NULL, w.attempts, f.reason, f.attempt"
        " FROM webhook_failures f JOIN webhook_ingress w ON w.id = f.ingress_id"
    ).fetchall()


def test_a_poison_item_is_recorded_dead_without_content_and_the_rest_is_stored(
    migrator: DB, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    items = [msg(k) for k in range(999)] + [poison(999)]
    delivery = webhooks.deliver(body([change(items)]))
    assert post(delivery).status_code == 200
    assert count(migrator) == 1000
    assert count(migrator, "SELECT count(*) FROM webhook_ingress WHERE status = 'RECEIVED'") == 999
    assert failures(migrator) == [("wa:msg:wamid.B00999", "DEAD", True, 0, "REFUSED_22023", 0)]
    [record] = [r for r in caplog.records if "meta.webhook.refused" in r.getMessage()]
    assert record.levelno == logging.ERROR and "Texto privado" not in caplog.text
    assert post(delivery).status_code == 200  # Meta repite: ni filas ni fallos nuevos
    assert count(migrator) == 1000 and len(failures(migrator)) == 1


def test_an_event_already_stored_is_never_turned_dead_by_a_later_refusal(migrator: DB) -> None:
    assert post(webhooks.deliver(body([change([msg(1)])]))).status_code == 200
    assert post(webhooks.deliver(body([change([poison(1), msg(2)])]))).status_code == 200
    found = migrator.execute("SELECT event_key, status FROM webhook_ingress ORDER BY 1").fetchall()
    assert found == [("wa:msg:wamid.B00001", "RECEIVED"), ("wa:msg:wamid.B00002", "RECEIVED")]
    assert failures(migrator) == []


def test_a_transient_error_while_recording_a_refusal_stores_nothing_and_is_503(
    migrator: DB, monkeypatch: pytest.MonkeyPatch
) -> None:
    def down(*args: Any) -> bool:
        raise OperationalError("la base se cayó")

    monkeypatch.setattr(ingress, "refuse", down)
    delivery = webhooks.deliver(body([change([msg(1), poison(2), msg(3)])]))
    assert post(delivery).status_code == 503 and count(migrator) == 0
    monkeypatch.undo()
    assert post(delivery).status_code == 200 and count(migrator) == 3


def test_two_overlapping_deliveries_at_once_both_get_200_and_store_each_item_once(
    migrator: DB,
) -> None:
    forward = [msg(k) for k in range(300)]
    deliveries = [body([change(forward)]), body([change(list(reversed(forward)))])]
    codes: list[int] = []

    def send(raw: bytes) -> None:
        try:
            codes.append(post(webhooks.deliver(raw)).status_code)
        finally:
            connection.close()

    threads = [threading.Thread(target=send, args=(raw,)) for raw in deliveries * 2]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert codes == [200] * 4 and count(migrator) == 300


@pytest.mark.parametrize(
    "bad",
    [
        {"reason": "refused"}, {"reason": "REFUSED_2202"}, {"reason": "OTRO"}, {"reason": None},
        {"hash": "x" * 64}, {"hash": None}, {"kind": "otro"}, {"key": "con espacio"},
        {"key": "x" * 701}, {"waba": "abc"}, {"number": "abc"}, {"provider": "Meta"},
        {"provider": "otro"}, {"number": "1" * 31}, {"waba": "1234"},
    ],
)  # fmt: skip
def test_the_refuse_function_checks_every_argument(bad: dict[str, Any], migrator: DB) -> None:
    args: dict[str, Any] = {"provider": "meta", "key": "wa:msg:x", "kind": "message",
                            "waba": WABA_ID, "number": PHONE_NUMBER_ID, "hash": HASH,
                            "reason": "REFUSED_22023"} | bad  # fmt: skip
    with connection.cursor() as cursor, pytest.raises(DatabaseError, match="no válidos"):
        cursor.execute(REFUSE, list(args.values()))
    connection.close()
    assert count(migrator) == 0


def test_the_refuse_function_is_idempotent_and_only_opens_through_itself(migrator: DB) -> None:
    args = ["meta", "wa:msg:x", "message", WABA_ID, None, HASH, "REFUSED"]
    with connection.cursor() as cursor:
        cursor.execute(REFUSE, args)
        assert cursor.fetchone()[0] is not None
        cursor.execute(REFUSE, args)
        assert cursor.fetchone() == (None,)
    assert failures(migrator) == [("wa:msg:x", "DEAD", True, 0, "REFUSED", 0)]
    assert ingress.insert("message", "wa:msg:y", WABA_ID, None, {"id": "y"}) is True
    with connection.cursor() as cursor:  # un ítem ya guardado no pasa a `DEAD`
        cursor.execute(REFUSE, ["meta", "wa:msg:y", "message", WABA_ID, None, HASH, "REFUSED"])
        assert cursor.fetchone() == (None,)
    status = "SELECT status FROM webhook_ingress WHERE event_key = 'wa:msg:y'"
    assert migrator.execute(status).fetchone() == ("RECEIVED",) and len(failures(migrator)) == 1
    found = migrator.execute(
        "SELECT p.prosecdef, pg_get_userbyid(p.proowner), p.proconfig,"
        " has_function_privilege('public', p.oid, 'EXECUTE'),"
        " has_function_privilege('crm_app', p.oid, 'EXECUTE')"
        " FROM pg_proc p WHERE p.oid ="
        " 'public.webhook_ingress_refuse(text, text, text, text, text, text, text)'::regprocedure"
    ).fetchone()
    assert found == (True, "crm_migrator", ["search_path=pg_catalog, public, pg_temp"], False, True)
    claimed = [c.payload for c in ingress.claim(10)]
    assert claimed == [{"id": "y"}]  # un ítem rechazado nunca se procesa; el guardado, sí


def test_the_refuse_migration_goes_back_and_forward(migrator: DB) -> None:
    exists = "SELECT count(*) FROM pg_proc WHERE proname = 'webhook_ingress_refuse'"
    with as_migrator():
        call_command("migrate", "conversations", "0030", verbosity=0)
    assert count(migrator, exists) == 0
    with as_migrator():
        call_command("migrate", "conversations", verbosity=0)
    assert count(migrator, exists) == 1

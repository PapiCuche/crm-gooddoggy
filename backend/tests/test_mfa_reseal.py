"""F2-98: volver a cifrar los secretos TOTP tras una rotación de clave (ADR-022 §5, paso 2).
`apps.accounts.mfa.reseal` y la tarea `accounts.reseal_mfa_secrets`. PostgreSQL con `crm_app`."""

import logging
from typing import Any

import psycopg
import pytest
from django.core.exceptions import ImproperlyConfigured
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.accounts import mfa
from apps.accounts.models import MfaDevice, User
from apps.accounts.tasks import reseal_mfa_secrets
from config.settings import base
from core import sealed
from core.observability import reporting
from core.observability.logging import json_formatter
from tests.factories import make_user

pytestmark = pytest.mark.usefixtures("tenant_db")
OLD, NEW = bytes(range(32)), bytes(range(32, 64))  # dos claves maestras de prueba
NOTHING = {"key": 1, "resealed": 0, "unreadable": 0, "busy": 0}  # `key`: la clave vigente


@pytest.fixture(autouse=True)
def keys(settings: Any) -> None:
    settings.SECRETS_KEKS = {1: OLD}


def device(user: User, secret: bytes) -> MfaDevice:
    box = sealed.seal(mfa.PURPOSE, mfa.owner(user.pk), secret)
    return MfaDevice.objects.create(user=user, type=MfaDevice.Type.TOTP, secret=box)


def opened(row: MfaDevice) -> bytes:
    row.refresh_from_db()
    return sealed.unseal(mfa.PURPOSE, mfa.owner(row.user_id), row.secret)


def test_after_a_new_key_one_pass_leaves_every_secret_under_it_and_a_second_changes_nothing(
    settings: Any, caplog: pytest.LogCaptureFixture
) -> None:
    ana, luis = make_user(email="ana@example.com"), make_user(email="luis@example.com")
    confirmed = device(ana, b"a" * 20)
    MfaDevice.objects.filter(pk=confirmed.pk).update(
        confirmed_at=timezone.now(), last_step=57_000_000
    )
    pending = device(luis, b"l" * 20)
    stamp = MfaDevice.objects.get(pk=confirmed.pk).confirmed_at
    assert mfa.reseal() == NOTHING  # sin rotación no hay nada que hacer
    settings.SECRETS_KEKS = {1: OLD, 7: NEW}  # rotación: se añade una clave
    before = {row.pk: bytes(row.secret) for row in MfaDevice.objects.all()}
    with caplog.at_level(logging.DEBUG):
        assert reseal_mfa_secrets() == {**NOTHING, "key": 7, "resealed": 2}
    for row, secret in ((confirmed, b"a" * 20), (pending, b"l" * 20)):
        assert opened(row) == secret  # el mismo secreto, atado a la misma cuenta
        assert bytes(row.secret)[:3] == b"\x01\x00\x07" and bytes(row.secret) != before[row.pk]
        assert not sealed.stale(row.secret)
    confirmed.refresh_from_db()
    assert (confirmed.confirmed_at, confirmed.last_step) == (stamp, 57_000_000)  # solo `secret`
    after = {row.pk: bytes(row.secret) for row in MfaDevice.objects.all()}
    assert mfa.reseal() == {**NOTHING, "key": 7}  # otra pasada: nada, ni un nonce nuevo
    assert {row.pk: bytes(row.secret) for row in MfaDevice.objects.all()} == after
    settings.SECRETS_KEKS = {7: NEW}  # ya se puede retirar la anterior: todo sigue abriéndose
    assert opened(confirmed) == b"a" * 20 and mfa.reseal() == {**NOTHING, "key": 7}
    logged = "\n".join(json_formatter().format(record) for record in caplog.records)
    for text in (b"a" * 20, b"l" * 20, *before.values(), *after.values()):
        assert text.hex() not in logged and text.decode("latin-1") not in logged
    assert "ana@example.com" not in logged
    assert "clave 7, vueltos a cifrar 2, ilegibles 0, ocupados 0" in logged  # lo que se lee


def test_what_cannot_be_opened_is_left_untouched_counted_and_reported(
    settings: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    ana, luis, eva = (make_user(email=f"{name}@example.com") for name in ("ana", "luis", "eva"))
    lost = device(ana, b"a" * 20)  # cifrado con la clave 1, que se va a retirar antes de tiempo
    moved = device(luis, b"l" * 20)
    MfaDevice.objects.filter(pk=moved.pk).update(secret=lost.secret)  # copiado de otra cuenta
    settings.SECRETS_KEKS = {1: OLD, 7: NEW}
    good = device(eva, b"e" * 20)  # este ya nace con la clave nueva
    MfaDevice.objects.filter(pk=good.pk).update(
        secret=sealed.seal(mfa.PURPOSE, mfa.owner(eva.pk), b"e" * 20)
    )
    settings.SECRETS_KEKS = {7: NEW}  # la anterior, retirada sin haber vuelto a cifrar
    kept = {row.pk: bytes(row.secret) for row in MfaDevice.objects.all()}
    reports: list[tuple[str, Any]] = []

    class Spy:
        def capture_exception(self, error: BaseException, context: Any = None) -> None:
            reports.append((repr(error), context))

    monkeypatch.setattr(reporting, "reporter", lambda: Spy())
    with caplog.at_level(logging.DEBUG):
        assert mfa.reseal() == {**NOTHING, "key": 7, "unreadable": 2}
    assert {row.pk: bytes(row.secret) for row in MfaDevice.objects.all()} == kept  # no se toca
    assert reports == [("UnreadableSecrets(2)", {"action": "mfa.reseal"})]  # una vez, sin valores
    told = [record for record in caplog.records if "no se pueden abrir" in record.getMessage()]
    assert [record.levelname for record in told] == ["ERROR"]
    logged = "\n".join(json_formatter().format(record) for record in caplog.records)
    assert all(value.hex() not in logged for value in kept.values())
    assert not any(str(user.pk) in logged for user in (ana, luis, eva))  # ni la cuenta
    settings.SECRETS_KEKS = {1: OLD, 7: NEW}  # la clave vuelve: lo de ana se recupera
    assert mfa.reseal() == {
        "key": 7,
        "resealed": 1,
        "unreadable": 1,
        "busy": 0,
    }  # lo copiado sigue sin abrirse
    assert (
        opened(lost) == b"a" * 20
        and bytes(MfaDevice.objects.get(pk=moved.pk).secret) == kept[moved.pk]
    )

    def broken() -> Any:
        raise RuntimeError("sin reporte")

    monkeypatch.setattr(reporting, "reporter", broken)
    assert mfa.reseal() == {**NOTHING, "key": 7, "unreadable": 1}  # un reporte roto no la para


def test_a_process_without_the_new_key_does_not_seal_back_with_the_old_one(settings: Any) -> None:
    ana, luis = make_user(email="ana@example.com"), make_user(email="luis@example.com")
    old = device(ana, b"a" * 20)
    settings.SECRETS_KEKS = {1: OLD, 7: NEW}
    new = device(luis, b"l" * 20)  # otro proceso, que ya tiene la clave nueva
    kept = bytes(MfaDevice.objects.get(pk=new.pk).secret)
    settings.SECRETS_KEKS = {1: OLD}  # este aún no la recibió (ADR-022 §5)
    assert sealed.stale(kept)  # lo ve como pendiente…
    assert mfa.reseal() == {**NOTHING, "unreadable": 1}  # …y no lo abre ni lo pisa
    assert bytes(MfaDevice.objects.get(pk=new.pk).secret) == kept
    assert bytes(MfaDevice.objects.get(pk=old.pk).secret)[:3] == b"\x01\x00\x01"
    settings.SECRETS_KEKS = {}
    with pytest.raises(ImproperlyConfigured, match="SECRETS_KEKS"):
        mfa.reseal()


def test_a_row_another_operation_holds_waits_for_the_next_pass(
    settings: Any, migrator: psycopg.Connection[Any]
) -> None:
    ana, luis = make_user(email="ana@example.com"), make_user(email="luis@example.com")
    held, free = device(ana, b"a" * 20), device(luis, bytes(range(20)))
    with migrator.transaction():  # otra operación tiene la fila (confirmar, comprobar un código)
        migrator.execute("SELECT 1 FROM user_mfa_devices WHERE id = %s FOR UPDATE", [held.pk])
        with CaptureQueriesContext(connection) as queries:
            assert mfa.reseal() == NOTHING  # sin rotación no la cuenta: no tiene nada pendiente
        assert len(queries) == 1  # una lectura: ni un bloqueo ni una transacción por fila
        settings.SECRETS_KEKS = {1: OLD, 7: NEW}
        assert mfa.reseal() == {**NOTHING, "key": 7, "resealed": 1, "busy": 1}  # no espera
        assert bytes(MfaDevice.objects.get(pk=held.pk).secret)[:3] == b"\x01\x00\x01"
    assert opened(free) == bytes(range(20))  # byte a byte: no es un secreto capicúa
    assert mfa.reseal() == {**NOTHING, "key": 7, "resealed": 1}  # la siguiente pasada
    assert opened(held) == b"a" * 20


def test_a_row_that_changed_after_it_was_read_is_looked_at_again_under_the_lock(
    settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    gone, kept = (device(make_user(), bytes(range(n, n + 20))) for n in (0, 20))
    settings.SECRETS_KEKS = {1: OLD, 7: NEW}
    stale = sealed.stale

    def meanwhile(box: Any) -> bool:  # leída la tabla, otra pasada termina y un alta se borra
        monkeypatch.setattr(sealed, "stale", stale)
        assert mfa.reseal()["resealed"] == 2 and MfaDevice.objects.filter(pk=gone.pk).delete()
        return stale(box)

    monkeypatch.setattr(sealed, "stale", meanwhile)
    assert mfa.reseal() == {**NOTHING, "key": 7}  # ni cifrada otra vez ni contada como ocupada
    assert opened(kept) == bytes(range(20, 40))


def test_zeros_from_a_process_without_the_new_key_name_the_old_one(
    settings: Any, caplog: pytest.LogCaptureFixture
) -> None:
    old = device(make_user(email="ana@example.com"), bytes(range(20)))
    # Otros procesos ya tienen la clave 7 y aún no cifraron nada con ella; este sigue con la 1.
    with caplog.at_level(logging.INFO):
        assert reseal_mfa_secrets() == NOTHING  # ceros, pero de la clave 1: no es luz verde
    assert caplog.messages[0].endswith("clave 1, vueltos a cifrar 0, ilegibles 0, ocupados 0")
    settings.SECRETS_KEKS = {7: NEW}  # quien retirara la 1 por esos ceros perdería el secreto
    with pytest.raises(sealed.SealError):
        opened(old)


def test_the_task_is_a_daily_platform_task(settings: Any) -> None:
    job = base.CELERY_BEAT_SCHEDULE["accounts.reseal_mfa_secrets"]
    assert (reseal_mfa_secrets.name, reseal_mfa_secrets.tenancy) == (job["task"], "platform")
    assert [len(job["schedule"].hour), len(job["schedule"].minute)] == [1, 1]  # una vez al día
    assert reseal_mfa_secrets() == NOTHING  # sin dispositivos
    settings.SECRETS_KEKS = {}
    with pytest.raises(ImproperlyConfigured, match="SECRETS_KEKS"):
        reseal_mfa_secrets()  # sin claves falla también con la tabla vacía: nunca da ceros

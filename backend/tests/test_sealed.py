"""F2-94: cifrado en reposo de secretos (ADR-022). `core.sealed` y el ajuste `SECRETS_KEKS`.
Sin base de datos."""

import base64
import hashlib
import importlib
import logging
import os
import sys
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from django.core.exceptions import ImproperlyConfigured

from config import env
from core import sealed
from core.sealed import SealError, seal, stale, unseal
from tests.test_settings import load_migrate, load_production

OLD, NEW = bytes(range(32)), bytes(range(32, 64))  # dos claves maestras de prueba
PLAIN = b"JBSWY3DPEHPK3PXP-un-secreto-totp"
PURPOSE, OWNER = "mfa.totp", "user:0198a6f2-0000-7000-8000-000000000001"
VAR = "SECRETS_KEKS"


def text(key: bytes) -> str:
    return base64.urlsafe_b64encode(key).decode().rstrip("=")


@pytest.fixture(autouse=True)
def keys(settings: Any) -> None:
    settings.SECRETS_KEKS = {1: OLD}


def test_it_seals_with_aes_256_gcm_under_a_key_derived_for_the_purpose(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    nonce = bytes(range(100, 112))
    monkeypatch.setattr(os, "urandom", lambda size: nonce[:size])
    box = seal(PURPOSE, OWNER, PLAIN)
    # Recalculado aparte: si esto cambia, lo ya guardado deja de poder leerse.
    info = b"good-doggy-crm/sealed/v1/" + PURPOSE.encode()
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=info).derive(OLD)
    head = b"\x01\x00\x01"  # formato 1, clave de versión 1
    bound = head + PURPOSE.encode() + b"\x00" + OWNER.encode()
    assert box == head + nonce + AESGCM(key).encrypt(nonce, PLAIN, bound)
    assert len(box) == 3 + 12 + len(PLAIN) + 16 and PLAIN not in box
    assert unseal(PURPOSE, OWNER, box) == PLAIN
    assert unseal(PURPOSE, OWNER, memoryview(box)) == PLAIN  # type: ignore[arg-type]  # un BinaryField
    with pytest.raises(Exception):  # noqa: B017, PT011 — la clave maestra no cifra datos
        AESGCM(OLD).decrypt(nonce, box[15:], bound)


def test_two_seals_of_the_same_secret_differ_and_both_open() -> None:
    first, second = seal(PURPOSE, OWNER, PLAIN), seal(PURPOSE, OWNER, PLAIN)
    assert first != second and first[3:15] != second[3:15]  # un nonce aleatorio cada vez
    assert unseal(PURPOSE, OWNER, first) == unseal(PURPOSE, OWNER, second) == PLAIN


def test_a_sealed_value_only_opens_for_its_purpose_and_owner_and_untouched() -> None:
    box = seal(PURPOSE, OWNER, PLAIN)
    other = OWNER[:-1] + "2"
    refused = [
        ("mfa.backup", OWNER, box),  # otro propósito
        (PURPOSE, other, box),  # copiado a la fila de otra cuenta
        (PURPOSE, OWNER, box[:-1]),  # truncado
        (PURPOSE, OWNER, box + b"\x00"),
        (PURPOSE, OWNER, b"\x02" + box[1:]),  # otro formato
        (PURPOSE, OWNER, box[:3] + box[3:15]),  # sin cifrado
        (PURPOSE, OWNER, b""),
        (PURPOSE, OWNER, PLAIN),  # algo guardado en claro no se «abre»
    ]
    refused += [  # un bit cambiado en cualquier byte: cabecera, nonce, cifrado o etiqueta
        (PURPOSE, OWNER, box[:at] + bytes([box[at] ^ 1]) + box[at + 1 :]) for at in range(len(box))
    ]
    for purpose, owner, value in refused:
        with pytest.raises(SealError) as failed:
            unseal(purpose, owner, value)
        assert str(failed.value) == "" and failed.value.__cause__ is None  # sin decir por qué
    # La clave de otro propósito no abre esto ni con los datos asociados de este.
    bound = box[:3] + PURPOSE.encode() + b"\x00" + OWNER.encode()
    assert sealed._cipher(1, PURPOSE).decrypt(box[3:15], box[15:], bound) == PLAIN
    with pytest.raises(Exception):  # noqa: B017, PT011
        sealed._cipher(1, "mfa.backup").decrypt(box[3:15], box[15:], bound)


def test_the_highest_version_seals_every_key_opens_and_a_removed_key_fails_closed(
    settings: Any,
) -> None:
    before = seal(PURPOSE, OWNER, PLAIN)
    assert stale(before) is False
    settings.SECRETS_KEKS = {1: OLD, 7: NEW}  # rotación: se añade una clave
    after = seal(PURPOSE, OWNER, PLAIN)
    assert (before[:3], after[:3]) == (b"\x01\x00\x01", b"\x01\x00\x07")
    assert unseal(PURPOSE, OWNER, before) == unseal(PURPOSE, OWNER, after) == PLAIN
    assert (stale(before), stale(after), stale(memoryview(after))) == (True, False, False)  # type: ignore[arg-type]
    assert stale(b"") and stale(b"\x02" + after[1:])  # lo que no es de este formato, también
    settings.SECRETS_KEKS = {1: OLD}  # un proceso que aún no recibió la clave nueva (§5)
    assert (stale(before), stale(after)) == (False, True)  # lo ve pendiente, y no lo abre
    with pytest.raises(SealError):
        unseal(PURPOSE, OWNER, after)
    settings.SECRETS_KEKS = {7: NEW}  # se retira la anterior
    assert unseal(PURPOSE, OWNER, after) == PLAIN
    with pytest.raises(SealError):
        unseal(PURPOSE, OWNER, before)
    settings.SECRETS_KEKS = {7: OLD}  # la versión dice qué clave, no la sustituye
    with pytest.raises(SealError):
        unseal(PURPOSE, OWNER, after)


def test_without_keys_nothing_is_sealed_or_opened(settings: Any) -> None:
    box = seal(PURPOSE, OWNER, PLAIN)
    settings.SECRETS_KEKS = {}
    for call in (
        lambda: seal(PURPOSE, OWNER, PLAIN),
        lambda: unseal(PURPOSE, OWNER, box),
        lambda: stale(box),
    ):
        with pytest.raises(ImproperlyConfigured, match=VAR):
            call()


def test_it_refuses_what_is_not_a_purpose_an_owner_or_a_secret_without_echoing_it(
    caplog: pytest.LogCaptureFixture,
) -> None:
    long = b"x" * (sealed.PLAINTEXT_MAX + 1)
    bad: list[tuple[Any, Any, Any]] = [
        ("", OWNER, PLAIN),
        ("MFA.totp", OWNER, PLAIN),
        ("mfa totp", OWNER, PLAIN),
        ("mfa.totp\n", OWNER, PLAIN),
        ("a" * 33, OWNER, PLAIN),
        (PURPOSE, "", PLAIN),
        (PURPOSE, "user:ñ", PLAIN),
        (PURPOSE, "user:\x00", PLAIN),
        (PURPOSE, "u" * 201, PLAIN),
        (PURPOSE, OWNER, b""),
        (PURPOSE, OWNER, long),
        (PURPOSE, OWNER, PLAIN.decode()),
        (PURPOSE, OWNER, bytearray(PLAIN)),
    ]
    with caplog.at_level(logging.DEBUG):
        for purpose, owner, plaintext in bad:
            with pytest.raises(ValueError, match="^(purpose|owner|plaintext): ") as failed:
                seal(purpose, owner, plaintext)
            assert PLAIN.decode() not in str(failed.value)
        assert len(seal("a" * 32, "u" * 200, b"x" * sealed.PLAINTEXT_MAX)) == 3 + 12 + 4096 + 16
        with pytest.raises(ValueError, match="^purpose: "):
            unseal("MFA", OWNER, seal(PURPOSE, OWNER, PLAIN))
    assert caplog.records == []  # el módulo no registra nada


def test_the_setting_takes_versioned_keys_and_refuses_anything_else_without_echoing_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(VAR, raising=False)
    assert env.keys(VAR) == {}
    monkeypatch.setenv(VAR, "  ")
    assert env.keys(VAR) == {}
    monkeypatch.setenv(VAR, f" 1:{text(OLD)}, 65535:{text(NEW)}= ")
    assert env.keys(VAR) == {1: OLD, 65535: NEW}
    urlsafe = bytes(range(224, 256))  # su base64 lleva `-` y `_`: el alfabeto para URL
    monkeypatch.setenv(VAR, f"9:{text(urlsafe)}")
    assert {"-", "_"} < set(text(urlsafe)) and env.keys(VAR) == {9: urlsafe}
    short = base64.urlsafe_b64encode(OLD[:31]).decode()
    for value in (
        text(OLD),  # sin versión
        f"0:{text(OLD)}",
        f"65536:{text(OLD)}",
        f"01:{text(OLD)}",
        f"-1:{text(OLD)}",
        f"1:{text(OLD)},1:{text(NEW)}",  # una versión, dos claves
        f"1:{text(OLD)},2:{text(OLD)}",  # una clave, dos versiones
        f"1:{short}",
        f"1:{text(OLD)}A",
        f"1:{text(OLD)[:-1]}+",  # base64 corriente, no para URL
        f"1:{text(OLD)},",
        f"1:{text(OLD)};2:{text(NEW)}",
        f"1:{OLD.hex()}",
    ):
        monkeypatch.setenv(VAR, value)
        with pytest.raises(ImproperlyConfigured) as failed:
            env.keys(VAR)
        assert VAR in str(failed.value) and text(OLD)[:8] not in str(failed.value), value[:6]


def test_local_and_test_derive_a_development_key_and_production_and_migrate_never_do(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(VAR, raising=False)
    for name in ("config.settings.test", "config.settings.local"):  # el módulo, sin el fixture
        sys.modules.pop(name, None)
        module: Any = importlib.import_module(name)
        derived = hashlib.sha256(b"dev-kek:" + module.SECRET_KEY.encode()).digest()
        assert module.SECRETS_KEKS == {1: derived} and len(derived) == 32
    monkeypatch.setenv(VAR, f"4:{text(NEW)}")  # si se da, manda la del entorno
    for name in ("config.settings.test", "config.settings.local"):
        sys.modules.pop(name)
        assert importlib.import_module(name).SECRETS_KEKS == {4: NEW}
    job: Any = load_migrate(
        monkeypatch,
        DATABASE_URL=None,
        DATABASE_MIGRATOR_URL="postgres://crm_migrator:x@db:5432/crm",
    )
    assert job.SECRETS_KEKS == {}  # el job de migraciones no las guarda aunque se las den
    production = load_production(monkeypatch, **{VAR: None})
    assert production.SECRETS_KEKS == {}  # type: ignore[attr-defined]  # falla cerrado al usarse
    production = load_production(monkeypatch, **{VAR: f"3:{text(NEW)}"})
    assert production.SECRETS_KEKS == {3: NEW}  # type: ignore[attr-defined]
    with pytest.raises(ImproperlyConfigured, match=VAR):
        load_production(monkeypatch, **{VAR: "3:corta"})  # mal formada: no arranca


def test_only_core_sealed_reads_the_keys() -> None:
    """ADR-022 §1: ningún otro módulo conoce las claves. Complementa a import-linter, que
    deja `cryptography` solo dentro de `core.sealed`."""
    root = Path(__file__).resolve().parents[1]
    assert VAR in (root / "core" / "sealed.py").read_text()  # el barrido mira donde está el módulo
    offenders = [
        str(path.relative_to(root)) for folder in ("core", "apps")
        for path in (root / folder).rglob("*.py")
        if path != root / "core" / "sealed.py" and "SECRETS_KEKS" in path.read_text()
    ]  # fmt: skip
    assert offenders == []

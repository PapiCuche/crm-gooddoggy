"""Redactor compartido: claves de proveedores, claves sensibles y contenido de mensajes.

Los secretos de prueba se construyen en tiempo de ejecución (sin literales que gitleaks
tenga que ignorar; no hay allowlist).
"""

import copy
import functools
import time
from typing import Any

import pytest

from core.redaction import REDACTED, mask_emails, redact, redact_text

OPENAI_PROJECT = "sk-" + "proj-" + "Ab1" * 16
OPENAI_LEGACY = "sk-" + "Zx9Yw8" * 8
ANTHROPIC = "sk-" + "ant-api03-" + "Xy9" * 30 + "AA"
META = "EAA" + "Bw" + "Zq7" * 15
JWT = "eyJ" + "hbGciOi" + "." + "eyJzdWIi" + "OjEyMzQ" + "." + "SflKxwRJ" + "SMeKKF2Q"


@pytest.mark.parametrize(
    "secret", [OPENAI_PROJECT, OPENAI_LEGACY, ANTHROPIC, META, JWT], ids=["openai-proj",
    "openai", "anthropic", "meta", "jwt"]
)  # fmt: skip
def test_redacts_provider_key_patterns(secret: str) -> None:
    for text in (secret, f"error con la clave {secret}, reintentar", f"key={secret}&x=1"):
        out = redact_text(text)
        assert secret not in out and REDACTED in out
    assert redact({"detalle": [f"token: {secret}"]}) == {"detalle": [f"token: {REDACTED}"]}


def test_redacts_bearer_and_url_credentials() -> None:
    assert (
        redact_text("Authorization: Bearer abcdef123456789") == f"Authorization: Bearer {REDACTED}"
    )
    url = "postgres://crm_app:" + "s3cr3t" + "@db:5432/crm"
    assert redact_text(url) == f"postgres://{REDACTED}@db:5432/crm"
    assert redact_text("https://x.test/cb?access_token=abc123&ok=1").endswith(f"={REDACTED}&ok=1")
    for text in (
        "?client_id=1&client_secret=0123abcd",
        "hub.verify_token=v3r1fy",
        "DB_PASSWORD=pw",
    ):
        assert redact_text(text).endswith(f"={REDACTED}"), text
    assert redact_text("redis://:" + "pw9" + "@redis:6379/0") == f"redis://{REDACTED}@redis:6379/0"
    assert "hunter2" not in redact_text("{'password': 'hunter2', \"api_key\": \"k9\"}")
    for text in ("input_tokens=1200&x=1", "http://host:8080/p", "Polo basic Oversize negro"):
        assert redact_text(text) == text  # sin falsos positivos


def test_redacts_sensitive_keys_preserving_shape() -> None:
    secret_keys = ("apiKey", "x-api-key", "Authorization", "Set-Cookie", "client_secret",
                   "new_password2", "cookies", "secrets")  # fmt: skip
    nested = {"access_token": "t", "hub.verify_token": "v", "credentials": {"a": 1}}
    kept = {"input_tokens": 1200, "key_last_four": ["4242", "1111"], "daily_token_limit": [1, 2],
            "credential_id": "c1", "password_changed_at": "2026-09-30"}  # fmt: skip
    data = dict.fromkeys(secret_keys, "valor") | kept | {"password": ["a", None], "n": nested}
    out = redact(data)
    assert {k: out[k] for k in secret_keys} == dict.fromkeys(secret_keys, REDACTED)
    assert out["password"] == [REDACTED, None]  # conserva la forma [antes, después]
    assert out["n"] == dict.fromkeys(nested, REDACTED)
    assert {k: out[k] for k in kept} == kept


def test_redacts_message_content() -> None:
    data = {"body": "hola, mi tarjeta es…", "message_content": ["a", None], "draft_text": "x"}
    assert redact(data) == {
        "body": REDACTED,
        "message_content": [REDACTED, None],
        "draft_text": REDACTED,
    }


def test_redact_does_not_mutate_and_handles_edge_values() -> None:
    data = {"password": "x", "items": ({"token": "y"},), "blob": b"\x00", "n": 1}
    original = copy.deepcopy(data)
    assert redact(data) == {
        "password": REDACTED,
        "items": [{"token": REDACTED}],
        "blob": "[BINARY]",
        "n": 1,
    }
    assert data == original
    deep: dict[str, Any] = functools.reduce(lambda acc, _: {"k": acc}, range(12), {})
    assert "[TRUNCATED]" in repr(redact(deep))


SESSION_KEYS = ["session_key", "session_id", "sessionId", "SESSION_KEY", "sessionid", "csrftoken",
                "csrfmiddlewaretoken", "X-CSRFToken", "XCSRFToken", "csrf", "csrf_token",
                "crm_session", "__Host-crm_session", "ai_session_id", "session_ids", "sessionkey",
                "sessiontoken"]  # fmt: skip


@pytest.mark.parametrize("key", SESSION_KEYS)
def test_session_and_csrf_keys_are_secrets(key: str) -> None:
    """F2-10: una sesión o un token CSRF valen como credencial. Toda clave `*session_id` se
    redacta, aunque termine en `_id`: para correlacionar se usa otro nombre."""
    assert redact({key: "valor"}) == {key: REDACTED}


def test_session_and_csrf_values_inside_text_are_redacted_without_false_positives() -> None:
    leaks = (
        "Cookie: __Host-crm_session=LEAK1",
        "Cookie: tema=oscuro; csrftoken=LEAK2",
        "Set-Cookie: sessionid=LEAK3; Path=/",
        "fallo con session_key=LEAK4",
        "cabecera X-CSRFToken: LEAK5",
        "{'sessionid': 'LEAK6'}",
        "https://x.test/cb?crm_session=LEAK7&ok=1",
    )
    for text in leaks:
        assert "LEAK" not in redact_text(text), text
    kept = {"conversation_id": "c1", "session_count": 3, "session_type": "web", "accept": "json"}
    assert redact(kept) == kept
    for text in (
        "input_tokens=120 session_count=3 reason=unknown_identifier",
        "training session: adiestramiento básico",
        "la sesión de Ana caducó",
        "Mascota Cookie: raza=beagle, edad=3 años, dueña Ana",  # un nombre de perro frecuente
        "Galletas Cookie: caja=12 unidades",
        "csrf_failure_count=3 y CSRF_TRUSTED_ORIGINS=https://crm.example.com",
    ):
        assert redact_text(text) == text


def test_redaction_cost_is_linear_on_hostile_text() -> None:
    started = time.perf_counter()
    hostile: tuple[str, ...] = ("." * 64000, "a" * 64000, "a." * 32000, "token" * 12000)
    hostile += ("=" * 64000, "csrf" * 16000, "csrf-" * 13000, "session_id" * 6400)
    hostile += ("cookie:" * 9000, "'token" * 10000, '"a' * 30000)
    for text in hostile:
        redact_text(text)
    assert time.perf_counter() - started < 1  # antes: varios segundos con 16 000 puntos


def test_mask_emails_hides_every_address_and_nothing_else() -> None:
    text = "alta pedida por ana@acme.pe (ticket 42), copia a O'Brien%40gooddoggy.pe y a nadie más"
    assert mask_emails(text) == "alta pedida por [EMAIL] (ticket 42), copia a [EMAIL] y a nadie más"
    assert mask_emails("sin direcciones: 3 @ 4, a las 10") == "sin direcciones: 3 @ 4, a las 10"


def test_the_meta_channel_secrets_are_redacted_by_key_and_by_shape() -> None:
    signature = "sha256=" + "ab12" * 16
    data = redact({
        "access_token": "x", "app_secret": "x", "verify_token": "x", "hub.verify_token": "x",
        "X-Hub-Signature-256": signature, "phone_number_id": "100000000000002",
        "waba_id": "100000000000001",
    })  # fmt: skip
    assert [
        data[k] for k in ("access_token", "app_secret", "verify_token", "hub.verify_token")
    ] == ["[REDACTED]"] * 4
    assert data["X-Hub-Signature-256"] == "[REDACTED]"
    assert redact({"x-hub-signature-256": "opaco"})["x-hub-signature-256"] == REDACTED  # por clave
    assert data["phone_number_id"] == "100000000000002" and data["waba_id"] == "100000000000001"
    assert signature not in redact_text(f"cabecera X-Hub-Signature-256: {signature} recibida")

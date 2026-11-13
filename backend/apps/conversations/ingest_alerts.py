"""Métricas y alertas mínimas de la ingesta de Meta (F7-11a; ADR-011, ADR-029 §3): enterarse de que
WhatsApp falla antes que el cliente.

**Firmas inválidas.** El webhook cuenta cada `401` en memoria del proceso y lo suma a la base como
mucho una vez por segundo (`FLUSH_EVERY`): una avalancha de firmas falsas no se convierte en una
escritura por petición. Lo que queda sin sumar al acabar la avalancha se suma con la siguiente
firma inválida de ese proceso (o se pierde si el proceso termina): el conteo es un mínimo. El
contador es una fila por ventana de `login_throttles` (de plataforma, compartida entre instancias):
`mwh:sig:<ventana>`; nunca guarda la dirección ni el cuerpo.

**El barrido** (`conversations.meta_alerts`, cada minuto) mira las dos últimas ventanas cerradas de
`WINDOW` segundos, alineadas con el reloj de la base. Cada ventana se evalúa una sola vez en todo el
sistema: la reclama una fila `mwh:alr:<ventana>` (`ON CONFLICT DO NOTHING`) en la misma transacción
que cuenta y avisa; otro barrido a la vez espera esa fila y ya no la evalúa. Los conteos salen de
`meta_ingest_counts` (`SECURITY DEFINER`, solo lectura: conteos y códigos cerrados, sin ids). El
retraso de la cola es del momento: solo se evalúa en la ventana más reciente.

Cada ventana deja una línea `meta.ingest.metrics` y, por umbral (`settings.META_ALERTS`) alcanzado,
un evento `meta.alert.<tipo>` de nivel `ERROR` y, si hay Sentry, el mismo evento con esa huella.
Solo fechas, conteos, umbrales y códigos propios: nunca teléfonos, textos, ids de Meta ni tokens.
"""

import datetime as dt
import threading
import time
from collections.abc import Iterable
from typing import Any

from django.conf import settings
from django.db import Error, connection, transaction

from core.observability import reporting
from core.observability.logging import get_logger

logger = get_logger(__name__)
WINDOW = 300  # segundos; lo que vive un contador cabe en la calma de la purga (prueba)
FLUSH_EVERY = 1.0  # segundos entre dos sumas a la base de un mismo proceso
SIGNATURES, CLAIM = "mwh:sig:", "mwh:alr:"
LAG = "ingress_lag_seconds"
GRACE = 60  # segundos que una ventana cerrada espera antes de evaluarse
# `updated_at` es el fin de la ventana del contador: la purga de calma lo cuenta desde ahí.
_ADD = """
WITH b AS (SELECT floor(extract(epoch FROM now()) / %(window)s)::bigint AS n)
INSERT INTO login_throttles AS t (key, failures, blocked_until, updated_at)
SELECT %(prefix)s || b.n, %(count)s, NULL, to_timestamp((b.n + 1) * %(window)s) FROM b
ON CONFLICT (key) DO UPDATE SET failures = t.failures + EXCLUDED.failures
"""
_CLAIM = """
INSERT INTO login_throttles (key, failures, blocked_until, updated_at) VALUES (%s, 0, NULL, now())
ON CONFLICT (key) DO NOTHING RETURNING 1
"""
_lock = threading.Lock()
_pending = 0
_last_flush = 0.0
_now = time.monotonic  # separado para que una prueba mueva el reloj


def bad_signature() -> None:
    """Cuenta una firma inválida. Nunca lanza: contar no cambia el `401`."""
    global _pending, _last_flush
    with _lock:
        _pending += 1
        now = _now()
        if now - _last_flush < FLUSH_EVERY:
            return
        count, _pending, _last_flush = _pending, 0, now
    try:
        with connection.cursor() as cursor:
            cursor.execute(_ADD, {"prefix": SIGNATURES, "window": WINDOW, "count": count})
    except Error:  # también `InterfaceError`: contar nunca cambia el `401`
        logger.warning("meta.webhook.count_failed", count=count)


def _measure(
    rows: Iterable[tuple[Any, ...]], signatures: int
) -> tuple[dict[str, int], list[list[Any]]]:
    """(lo medido por métrica, los salientes `FAILED` como `[código, n]`: una lista y no un
    diccionario, porque el redactor taparía una clave como `TOKEN_INVALID`)."""
    measured = dict.fromkeys(settings.META_ALERTS, 0)
    codes: list[list[Any]] = []
    for metric, code, total in rows:
        if metric == "outbound_failed":
            codes.append([code or "NONE", int(total)])
            if code not in settings.META_ALERT_QUIET_CODES:
                measured[metric] += int(total)
        else:
            measured[metric] = int(total)
    measured["signature_invalid"] = signatures
    return measured, codes


def _alert(name: str, value: int, threshold: int, start: str, codes: list[list[Any]]) -> None:
    event = f"meta.alert.{name}"
    context: dict[str, Any] = {"window_start": start, "count": value, "threshold": threshold}
    if name == "outbound_failed":
        context["codes"] = codes
    logger.error(event, **context)
    try:
        reporting.reporter().capture_message(event, context, fingerprint=[event])
    except Exception:  # noqa: BLE001 — un reporte roto no frena las demás alertas
        logger.warning("meta.alert.report_failed", alert=name)


def _bucket(bucket: int, *, latest: bool) -> list[str]:
    start = dt.datetime.fromtimestamp(bucket * WINDOW, dt.UTC)
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(_CLAIM, [f"{CLAIM}{bucket}"])
        if cursor.fetchone() is None:
            return []  # ya evaluada (o la evalúa otro barrido ahora)
        end = start + dt.timedelta(seconds=WINDOW)
        cursor.execute("SELECT * FROM public.meta_ingest_counts(%s, %s)", [start, end])
        rows = cursor.fetchall()
        cursor.execute(
            "SELECT failures FROM login_throttles WHERE key = %s", [f"{SIGNATURES}{bucket}"]
        )
        found = cursor.fetchone()
        measured, codes = _measure(rows, int(found[0]) if found else 0)
        if not latest:
            measured.pop(LAG)  # el retraso es de ahora, no de una ventana pasada
        logger.info("meta.ingest.metrics", window_start=start.isoformat(), **measured, codes=codes)
        fired = []
        for name, value in measured.items():
            threshold = max(1, settings.META_ALERTS[name])  # 0 dispararía sin datos
            if value >= threshold:
                _alert(name, value, threshold, start.isoformat(), codes)
                fired.append(name)
        return fired


def sweep() -> list[str]:
    """Evalúa las dos últimas ventanas cerradas que nadie evaluó. Devuelve las alertas emitidas."""
    if not settings.META_ENABLED:
        return []
    current = _current()
    return _bucket(current - 2, latest=False) + _bucket(current - 1, latest=True)


def _current() -> int:
    """La ventana en curso por el reloj de la base (el de las filas que cuenta), con `GRACE` de
    retraso: una fila que se confirma tarde (su hora es la del inicio de su transacción) o que
    lleva la hora de un proceso algo adelantado aún cae en una ventana sin evaluar."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT extract(epoch FROM now())")
        return window_of(float(cursor.fetchone()[0]))


def window_of(epoch: float) -> int:
    return int((epoch - GRACE) // WINDOW)

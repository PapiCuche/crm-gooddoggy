"""Límite de intentos de acceso (F2-03B; ADR-003 §2, ADR-014 §3, D-F2-9).

Tres contadores por intento, en una tabla que comparten todas las instancias:

- `identifier`: la huella del email presentado. Nunca rechaza: mide si la cuenta está bajo
  ataque («caliente»). Así nadie deja fuera a su dueña desde otras direcciones.
- `pair`: la huella junto con la dirección. Rechaza a quien prueba contraseñas de una cuenta
  desde una dirección; con la cuenta caliente, cada dirección tiene un intento y espera.
- `ip`: la dirección sola (en IPv6, su red /64). Rechaza a quien prueba muchas cuentas.

El intento se cuenta **antes** de comprobar las credenciales (`admit`), con la huella de lo
presentado, exista o no la cuenta: la respuesta no dice nada de ella, y una ráfaga simultánea
no prueba más contraseñas que el límite. Un acceso correcto devuelve lo que contó (`forgive`);
uno fallido adelanta el reloj de sus contadores (`failed`). Un intento rechazado no cuenta en
ninguna clave. Un contador vuelve a empezar solo tras una ventana entera sin fallos ni bloqueo:
mientras los fallos siguen, la espera llega a su máximo.
"""

import ipaddress
from dataclasses import dataclass

from django.conf import settings
from django.db import connection, transaction

from apps.accounts.emails import presented_email
from apps.audit.platform import identifier_hash

SCOPES = ("identifier", "pair", "ip")  # también el orden en que se bloquean las filas
PAIR_KEPT = 24 * 60 * 60  # segundos de calma tras los que la purga borra un contador `pair`
_QUIET = """GREATEST(t.updated_at, COALESCE(t.blocked_until, t.updated_at))
    < now() - %(window)s::int * interval '1 second'"""
# Una sentencia que bloquea la fila: dos instancias no se pisan, y la segunda ve el bloqueo que
# la primera acaba de empezar. Contar no mueve el reloj (`updated_at`, el último fallo): si lo
# moviera, un acceso correcto retrasaría el reinicio y se notaría desde otras direcciones. Con
# la ventana a NULL el contador no vuelve a empezar.
_COUNT = f"""
INSERT INTO login_throttles AS t (key, failures, blocked_until, updated_at)
VALUES (%(key)s, 1, NULL, now())
ON CONFLICT (key) DO UPDATE SET
  failures = CASE WHEN {_QUIET} THEN 1 ELSE t.failures + 1 END,
  updated_at = CASE WHEN {_QUIET} THEN now() ELSE t.updated_at END
RETURNING failures, COALESCE(blocked_until > now(), false)
"""  # noqa: S608 — texto fijo, sin datos
_FAILED = "UPDATE login_throttles SET updated_at = now() WHERE key = %s"
_BLOCK = """
UPDATE login_throttles SET blocked_until = now() + %s * interval '1 second' WHERE key = %s
"""
_WAIT = """
SELECT COALESCE(CEIL(EXTRACT(EPOCH FROM MAX(blocked_until) - now())), 0)::int
FROM login_throttles WHERE key = ANY(%s) AND blocked_until > now()
"""
_RETURN_HEAT = "UPDATE login_throttles SET failures = GREATEST(failures - 1, 0) WHERE key = %s"
# La dirección pierde este intento y el bloqueo que empezó con él, o el que ya no se sostiene.
_RETURN_ADDRESS = """
UPDATE login_throttles SET failures = GREATEST(failures - 1, 0),
  blocked_until = CASE WHEN %s OR failures - 1 < %s THEN NULL ELSE blocked_until END
WHERE key = %s
"""
# Borrar no cambia lo que haría el siguiente intento. Un contador `pair` en calma no vuelve a
# empezar si su cuenta está caliente cuando la dirección regresa: por eso se conserva más.
_PURGE = f"""
DELETE FROM login_throttles WHERE id IN (
  SELECT id FROM login_throttles AS t
  WHERE {_QUIET} AND (left(t.key, 4) <> 'par:' OR {_QUIET.replace("window", "kept")})
  FOR UPDATE SKIP LOCKED)
"""  # noqa: S608 — texto fijo, sin datos


@dataclass(frozen=True, slots=True)
class Limit:
    failures: int  # intentos sin una ventana de calma a partir de los cuales actúa
    window: int  # segundos de calma tras los que el contador vuelve a empezar
    first: int  # primer bloqueo, en segundos; se duplica con cada fallo posterior
    longest: int  # bloqueo máximo, en segundos

    def block(self, failures: int) -> int:
        doubled: int = self.first << min(failures - self.failures, 16)
        return min(self.longest, doubled)


@dataclass(frozen=True, slots=True)
class Blocked:
    scope: str  # `identifier`: la espera de una dirección mientras la cuenta está caliente
    failures: int
    seconds: int


class Refused(Exception):
    """Hay un bloqueo en curso: el intento no se evalúa ni cuenta."""

    def __init__(self, seconds: int) -> None:
        super().__init__(seconds)
        self.seconds = seconds


def limits() -> dict[str, Limit]:
    return {scope: Limit(*settings.LOGIN_THROTTLE[scope]) for scope in SCOPES}


def _address(ip: str) -> str:
    """La dirección que cuenta: en IPv6, la red /64 (quien la tiene rota dentro de ella)."""
    address = ipaddress.ip_address(ip)
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is None:
            return f"{ipaddress.IPv6Address(int(address) >> 64 << 64)}/64"
        address = address.ipv4_mapped
    return str(address)


def keys(identifier: str, ip: str | None) -> dict[str, str]:
    """Las claves de un intento. Sin dirección conocida todos comparten una (`-`): el límite
    sigue contando, y es la señal de un proxy mal declarado."""
    mark = identifier_hash(presented_email(identifier))  # la cuenta, se escriba como se escriba
    address = _address(ip) if ip else "-"
    return {"identifier": f"id:{mark}", "pair": f"par:{mark}:{address}", "ip": f"ip:{address}"}


def blocked_for(attempt: dict[str, str]) -> int:
    """Segundos que faltan para poder intentarlo de nuevo, con el reloj de la base de datos;
    0 si no hay bloqueo."""
    with connection.cursor() as cursor:
        cursor.execute(_WAIT, [list(attempt.values())])
        return int(cursor.fetchone()[0])


def admit(attempt: dict[str, str]) -> list[Blocked]:
    """Cuenta el intento en cada clave antes de evaluarlo y devuelve los bloqueos que empiezan
    con él: el que llega al límite aún se evalúa, el siguiente ya no. `Refused` si hay un
    bloqueo en curso, también el que otro intento simultáneo acaba de empezar."""
    if wait := blocked_for(attempt):
        raise Refused(wait)
    rules, started, heat, hot = limits(), [], 0, False
    with transaction.atomic(), connection.cursor() as cursor:
        for scope in SCOPES:  # siempre en este orden: sin interbloqueos
            rule, strike = rules[scope], scope == "pair" and hot
            if strike:  # cuenta caliente: un intento por dirección, con la espera de la cuenta,
                window = None  # y su contador no vuelve a empezar mientras siga caliente
                rule = Limit(1, rule.window, *settings.LOGIN_THROTTLE["identifier"][2:])
            else:
                window = rule.window
            cursor.execute(_COUNT, {"key": attempt[scope], "window": window})
            failures, blocked = cursor.fetchone()
            if scope == "identifier":  # nunca rechaza: solo dice si la cuenta está caliente
                heat, hot = failures, failures >= rule.failures
                continue
            if blocked:
                transaction.set_rollback(True)  # rechazado: no cuenta en ninguna clave
                break
            if failures >= rule.failures:
                seconds = rule.block(failures)
                cursor.execute(_BLOCK, [seconds, attempt[scope]])
                held = Blocked("identifier", heat, seconds) if strike else None
                started.append(held or Blocked(scope, failures, seconds))
        else:
            return started
    raise Refused(blocked_for(attempt) or 1)


def failed(attempt: dict[str, str]) -> None:
    """El intento falló: el reloj de sus contadores avanza. Cada clave en su propia sentencia,
    para no retener una fila mientras se espera otra."""
    with connection.cursor() as cursor:
        for scope in SCOPES:
            cursor.execute(_FAILED, [attempt[scope]])


def forgive(attempt: dict[str, str], started: list[Blocked]) -> None:
    """Un acceso correcto devuelve lo que contó, en el orden de `admit`: la cuenta y la
    dirección quedan como estaban, y el contador de esa cuenta en esa dirección se borra."""
    with connection.cursor() as cursor:
        cursor.execute(_RETURN_HEAT, [attempt["identifier"]])
        cursor.execute("DELETE FROM login_throttles WHERE key = %s", [attempt["pair"]])
        mine = any(blocked.scope == "ip" for blocked in started)
        cursor.execute(_RETURN_ADDRESS, [mine, limits()["ip"].failures, attempt["ip"]])


def purge() -> int:
    """Borra los contadores que ya no cuentan: una ventana entera sin fallos y sin bloqueo en
    curso (`PAIR_KEPT` los de `pair`). Salta las filas que un acceso tiene bloqueadas."""
    window = max(limit.window for limit in limits().values())
    with connection.cursor() as cursor:
        cursor.execute(_PURGE, {"window": window, "kept": max(window, PAIR_KEPT)})
        return int(cursor.rowcount)

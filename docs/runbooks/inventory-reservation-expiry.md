# Vencimiento automático de reservas de inventario (F3-34)

Una reserva ACTIVE cuyo `expires_at` ya pasó devuelve sus unidades a lo disponible sin que nadie la toque.

## Qué hace

- `inventory.expire_reservations_sweep` (beat, cada 5 minutos) lee las organizaciones ACTIVE o TRIAL y encola `inventory.expire_reservations` por cada una. No ve reservas: solo ids. El orden rota en cada tick de 5 minutos (un paso cercano a n/φ y primo con n): si la cola de los workers pasa de 300 s y los últimos mensajes vencen, en n barridos seguidos cada organización va primera una vez (F3-34b). Es un reparto de mejor esfuerzo: el tick sale del reloj (si beat se salta o repite uno, la secuencia se desplaza) y n cambia al entrar o salir organizaciones.
- `inventory.expire_reservations` (comprueba que la organización del mensaje esté activa; como `crm_app`, sin bypass de RLS) vence cada reserva vencida en su propia transacción, con el mismo orden de bloqueo que reservar y liberar (variante, almacén, saldo, reserva). Estado EXPIRED, un `RELEASE` con la clave `R/<id>/release`, actor `SYSTEM` y auditoría `inventory.reservation_expired`.
- Topes: lotes de 100, 500 intentos por organización y pasada, 120 s por pasada y, por cada bloqueo de una reserva, 10 s o la cuarta parte de lo que le queda a la pasada, lo menor (al menos 1 ms): la pasada dura unos 120 s más las sentencias de una fila que no esperan.
- Una reserva que no consigue un bloqueo (`lock_timeout`, SQLSTATE 55P03) deja fuera de esa pasada el resto de su celda (variante × almacén): una celda ocupada gasta un intento, no el tope. Cualquier otro error salta solo esa fila y la pasada sigue con las demás de su celda. Si quedan filas tras la última que se intentó (fuera de las celdas ocupadas), la pasada encola la siguiente con ese cursor (`after`), que sigue por detrás: unas filas que fallan siempre al principio no tapan las demás. El cursor avanza en cada pasada, así que la cadena termina al llegar al final; lo que falló, y lo que venció mientras tanto por delante del cursor, lo recoge el siguiente barrido, que empieza sin cursor. Una pasada que se queda sin tiempo sin vencer ninguna (p. ej., un bloqueo sobre toda la tabla) no se encadena. Con muchas filas que fallan, una cadena puede durar más de 5 minutos y solaparse con la del barrido siguiente: es seguro (ver abajo) y cada cadena termina.
- Es seguro repetirla o correrla a la vez (o contra una liberación o una reserva): la clave del `RELEASE` es única y cada reserva se relee con la celda bloqueada.

## Cómo verlo

- Registros JSON (solo ids y contadores, nunca datos de personas):
  - `inventory.reservations_expiry_swept`: `organizations`, `enqueued`, `failed`.
  - `inventory.reservations_expired`: `organization_id`, `expired`, `skipped` (otro la cerró antes), `failed`, `capped` (quedan más).
  - `inventory.reservation_expiry_failed` (`reservation_id`, `error`) e `inventory.expire_enqueue_failed` / `inventory.expire_chain_failed` / `inventory.expire_org_inactive`.
- En la base: reservas vencidas aún abiertas, `SELECT count(*) FROM inventory_reservations WHERE status = 'ACTIVE' AND expires_at <= now();` (con un rol que vea la organización; baja a 0 en unos minutos, con pasadas encadenadas; quedan las que vencieron en los últimos 5 minutos y las que fallan: mirar `failed`).
- Auditoría: `action = 'inventory.reservation_expired'` con actor `SYSTEM`.
- Que beat está vivo: la entrada `inventory.expire_reservations_sweep` de `CELERY_BEAT_SCHEDULE` (300 s).

## Si algo falla

- `failed > 0` repetido con el mismo `reservation_id`:
  - `error = OperationalError` de bloqueo: una celda ocupada de forma sostenida (también la fila de la reserva). Se reintenta en cada barrido; esa reserva y las de detrás en su celda siguen contando como reservadas hasta que la celda se libere. Mirar las transacciones largas sobre esa variante.
  - otro `error`: un error de datos de esa fila. No se arregla solo: cada barrido la reintenta, vuelve a fallar y la reserva sigue ACTIVE contando como reservada hasta que alguien corrija el dato (las demás de su celda sí vencen). Mirar `error` y la fila.
- Sin registros `inventory.reservations_expiry_swept`: beat o el worker no corren.

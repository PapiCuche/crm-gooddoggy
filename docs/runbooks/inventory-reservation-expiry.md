# Vencimiento automático de reservas de inventario (F3-34)

Una reserva ACTIVE cuyo `expires_at` ya pasó devuelve sus unidades a lo disponible sin que nadie la toque.

## Qué hace

- `inventory.expire_reservations_sweep` (beat, cada 5 minutos) lee las organizaciones ACTIVE o TRIAL y encola `inventory.expire_reservations` por cada una. No ve reservas: solo ids.
- `inventory.expire_reservations` (comprueba que la organización del mensaje esté activa; como `crm_app`, sin bypass de RLS) vence cada reserva vencida en su propia transacción, con el mismo orden de bloqueo que reservar y liberar (variante, almacén, saldo, reserva). Estado EXPIRED, un `RELEASE` con la clave `R/<id>/release`, actor `SYSTEM` y auditoría `inventory.reservation_expired`.
- Topes: lotes de 100, 500 por organización y pasada, 120 s por pasada, 10 s de espera por cada bloqueo de una celda ocupada (la pasada también se corta a los 120 s dentro del lote). Si quedan más y la pasada avanzó, se encadena sola; si no, la recoge el siguiente barrido.
- Es seguro repetirla o correrla a la vez (o contra una liberación o una reserva): la clave del `RELEASE` es única y cada reserva se relee con la celda bloqueada.

## Cómo verlo

- Registros JSON (solo ids y contadores, nunca datos de personas):
  - `inventory.reservations_expiry_swept`: `organizations`, `enqueued`, `failed`.
  - `inventory.reservations_expired`: `organization_id`, `expired`, `skipped` (otro la cerró antes), `failed`, `capped` (quedan más).
  - `inventory.reservation_expiry_failed` (`reservation_id`, `error`) e `inventory.expire_enqueue_failed` / `inventory.expire_chain_failed` / `inventory.expire_org_inactive`.
- En la base: reservas vencidas aún abiertas, `SELECT count(*) FROM inventory_reservations WHERE status = 'ACTIVE' AND expires_at <= now();` (con un rol que vea la organización; debe volver a 0 tras una pasada).
- Auditoría: `action = 'inventory.reservation_expired'` con actor `SYSTEM`.
- Que beat está vivo: la entrada `inventory.expire_reservations_sweep` de `CELERY_BEAT_SCHEDULE` (300 s).

## Si algo falla

- `failed > 0` repetido con el mismo `reservation_id`: una celda ocupada de forma sostenida o un error de datos; se reintenta cada 5 minutos y la reserva sigue contando como reservada hasta entonces. Mirar `error` y las transacciones largas sobre esa variante.
- Sin registros `inventory.reservations_expiry_swept`: beat o el worker no corren.

# Outbox y auditoría (F1-06)

**Relacionado:** ADR-001, ADR-002, ADR-011, `docs/fase-0/04-precios-pipeline-seguridad-infra.md` §N y §O, [tenancy-context.md](tenancy-context.md)
**Estado:** implementado en F1-06 (#8)

Ambas se escriben **en la transacción del `tenant_scope` activo** (`require_scope(ctx)`: sin `using`). Si el cambio hace rollback, no queda ni evento ni auditoría.

| Pieza | Comportamiento |
|---|---|
| `core.outbox.emit(ctx, "modulo.evento", aggregate_type=…, aggregate_id=…, payload=…)` | Inserta en `outbox_events` (tenant-owned, RLS + FORCE, FK a `organizations`). El payload lleva IDs y campos mínimos, sin contenido de mensajes, hasta 16 KiB |
| `@subscribe("modulo.evento")` sobre un `@tenant_task` | Registra un handler `handler(event_id)`. Se ejecuta en el tenant del evento con su `correlation_id`; es idempotente por `event_id` y la entrega es al menos una vez |
| `core.publish_outbox` (`@platform_task`, beat cada 1 s) | `outbox_pending_organizations()` (SECURITY DEFINER, solo IDs), luego un `tenant_scope` por organización y `FOR UPDATE SKIP LOCKED`. Encola los handlers y marca `published_at`. Si el broker falla: `attempts` + la clase del error |
| `apps.audit.services.record(ctx, action, entity, changes, metadata, *, result, actor_label)` | Inserta en `audit_logs` (particionada por mes, RLS en la tabla padre y en cada partición). `changes` y `metadata` pasan siempre por el redactor |
| `core.redaction.redact()` | Oculta claves sensibles (password/password1, pwd, token, secret, api_key, authorization, cookie…), sin tocar fechas, límites, contadores ni FKs (`*_at`, `*_limit`, `*_count`, `*_id`). Oculta el contenido de mensajes (body, content, text, `*_text`, `*_preview`…). Conserva `*_last_four` con la forma `[antes, después]`. Detecta en cualquier texto claves de OpenAI/Anthropic/Meta, Bearer, `Authorization: Basic`, JWT, URLs con credenciales (también `redis://:clave@`) y parámetros `*_secret=`/`*_token=`/`*_password=`. `entity_label` y `actor_label` también se redactan |

- **Append-only:** `crm_app` solo tiene SELECT e INSERT en `audit_logs` y ningún privilegio directo sobre las particiones.
- **Lectura (F2-73):** `GET /api/v1/o/{slug}/audit/`, con el permiso `audit.view`, sobre el modelo de solo lectura `apps.audit.models.AuditLog` (`managed = False`). El listado ordena por `id` y usa el índice `audit_logs_org_id_idx (organization_id, id)`.
- **Particiones:** `audit_ensure_partitions(n)` (propiedad de `crm_migrator`, sin EXECUTE para `crm_app`) crea el mes actual + 12. La llaman la migración y el `post_migrate` de cada job de migraciones. No hay partición DEFAULT: si el horizonte se agota, el INSERT falla (fail-closed). Hay que ejecutar el job de migraciones al menos una vez al año.

## Contrato de los handlers del outbox

- La firma es `handler(event_id: str)`, sobre un `@tenant_task` suscrito con `@subscribe("modulo.evento")` en `<app>/tasks.py` (el worker debe importarlo).
- Se ejecuta en el tenant del evento, con actor SYSTEM y el `correlation_id` del evento.
- Relee el evento (`OutboxEvent.objects.get(id=event_id)`) y es **idempotente por `event_id`**.
- La entrega es al menos una vez y sin orden garantizado. Los reintentos son los del propio task (`autoretry_for`).
- El publisher encola los handlers y marca `published_at` en la misma transacción que reclama el lote (`FOR UPDATE SKIP LOCKED`). Si el commit falla, el evento se reenvía: duplicado, nunca pérdida.
- Si el broker falla, el evento queda pendiente con `attempts` y la clase del error. Nunca se guarda el mensaje del error, que puede contener URLs con credenciales.
- El payload **no se redacta**: se corrompería el dato para los consumidores. Por eso solo lleva IDs y campos mínimos, nunca contenido de mensajes, y tiene un máximo de 16 KiB.

- El descubrimiento (`outbox_pending_organizations`) devuelve primero el tenant con el evento pendiente **más antiguo**, así ningún tenant queda sin servicio aunque haya más de `max_orgs` organizaciones con eventos pendientes.
- Su límite se acota **dentro de la función** (fail-closed): `NULL` o ≤ 0 → 0 filas, y nunca más de 1000 (`LIMIT NULL` significaría "sin límite").
- **Despliegues:** los workers deben ejecutar el código nuevo (con sus `@subscribe`) antes que el código que emite un tipo de evento nuevo, o pausar beat durante el despliegue. Un publisher sin suscriptores registrados marca el evento como publicado (OBS-F1-06-3).

## Decisiones frente a la documentación de la Fase 0

| Tema | Decisión |
|---|---|
| `audit_logs.organization_id` nullable (04 §N.1) | NOT NULL (ADR-001 §2, T4). Los eventos sin tenant van a un sumidero propio, `platform_audit_logs` ([ADR-013](../adr/ADR-013-platform-audit.md)), implementado en F2-10 (#56): `apps.audit.platform.record()`, solo de inserción para `crm_app`. Todavía no lo llama nadie: los eventos de acceso llegan con F2-03A |
| `outbox_events` | Tenant-owned con RLS. El publisher descubre tenants con una función SECURITY DEFINER que devuelve solo IDs (ADR-002 §3.3) |
| Particiones con beat diario (04 §O.2) | El runtime no tiene DDL: las crean la migración y el `post_migrate`, con un horizonte de 12 meses y sin partición DEFAULT (OBS-F1-06-2) |
| PK de una tabla particionada | `(organization_id, occurred_at, id)` |
| Firma de `audit.record` | Los cinco argumentos de ADR-011, más `result` y `actor_label` como keyword-only |
| `PLATFORM_STAFF` | El CHECK de BD lo admite; `ActorType` no cambia |
| Enmascarado de documentos de identidad (04 §N.1) | Fuera del alcance de #8 (OBS-F1-06-4) |
| Hora del evento | `now()` = hora de la transacción, la misma en `outbox_events` y `audit_logs` del mismo cambio |

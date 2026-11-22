# ADR-045: `ai_budget_reserve` inserta los dos avisos del presupuesto de IA en el outbox

- **Status:** Accepted (2026-11-18, decisión del propietario, [registrada en #932](https://github.com/PapiCuche/crm-gooddoggy/issues/932#issuecomment-6985805311))
- **Date:** 2026-11-18
- **Deciders:** propietario del producto; redactado por el programa autónomo (ADR-015).
- **Work items:** F8-05b4 ([#957](https://github.com/PapiCuche/crm-gooddoggy/issues/957)), parte de F8-05b ([#932](https://github.com/PapiCuche/crm-gooddoggy/issues/932))
- **Related:** [ADR-044](ADR-044-ai-budget-ledger-writer-definers.md) (lo enmienda **solo** en las tablas que puede escribir `ai_budget_reserve`), [ADR-002](ADR-002-postgresql-rls.md) §3.3, [ADR-034](ADR-034-ai-copilot-first.md) §5, [ADR-043](ADR-043-ai-budget-time-zone-lima.md)

## Context

ADR-034 §5 pide un aviso al 80 % del tope (`ai.budget.threshold_reached`) y otro al cortar (`ai.budget.exceeded`). #957 los quiere una sola vez por organización y mes de Lima, con marcas que solo escribe `ai_budget_reserve`. ADR-044 limita esa función a las tablas del registro de gasto, y el evento va en `outbox_events`, que no es una de ellas.

Si la función solo pusiera la marca y la aplicación emitiera el evento después, la aplicación tendría que adivinar si la marca era de esta llamada. Un error ahí duplica o pierde el aviso. Si la marca y el evento salen de la misma sentencia condicionada, en la misma transacción, la base garantiza los dos a la vez.

## Decision

Se amplía la excepción de ADR-044, solo en esto: `public.ai_budget_reserve` puede **insertar en `outbox_events`** dos eventos, `ai.budget.threshold_reached` y `ai.budget.exceeded`, y nada más. Condiciones:

1. **En la misma transacción que su marca** (`ai_budget_usage.threshold_alerted_at` o `exceeded_alerted_at`), y solo si la sentencia que pone la marca (`… WHERE marca IS NULL`) cambió la fila: una vez por organización y mes de Lima. `ai.budget.exceeded` solo sale si el corte llega con el mes ya en el 80 % o más; una reserva enorme cortada antes se rechaza igual, pero no gasta el aviso del mes.
2. **Agregado:** `aggregate_type = 'ai_budget'` y `aggregate_id` = el id de la fila de `ai_budgets` de la organización. **Payload:** exactamente `{"month": "AAAA-MM-01"}`. Sin importes, prompts, personas ni conversaciones.
3. **Filtro explícito por organización** en cada sentencia, como en ADR-044 §2: la organización del evento es `app_current_tenant()` y el tope se busca con `organization_id = app_current_tenant()`.
4. **Pruebas que matan los mutantes** que quitan cada uno de esos filtros, y pruebas de concurrencia de que dos reservas a la vez dejan un solo evento.

Todo lo demás de ADR-044 sigue igual. `ai_budget_settle` no escribe en el outbox. Cualquier otro evento u otra tabla necesita su propio ADR.

## Alternatives considered

| Alternativa | Por qué no |
| --- | --- |
| La aplicación emite el evento tras leer la marca | Tiene que deducir si la marca era de esta llamada; dos reservas a la vez pueden duplicar o perder el aviso. |
| Solo auditoría | Un aviso no es una acción de una persona, y la auditoría no despierta a nadie. Los eventos del outbox sí pueden tener suscriptores. |
| Avisar también del tope diario | El día se renueva cada día y avisaría a menudo. Queda como pregunta al propietario. |

## Consequences

- El aviso y su marca se confirman o se deshacen juntos. Para que el aviso del corte se confirme, el corte del **mes** ya no lanza una excepción: `ai_budget_reserve` devuelve NULL sin reservar nada y el gateway responde `AI_BUDGET_EXHAUSTED` sin llamar al proveedor.
- `crm_app` conserva su INSERT en `outbox_events` (lo usa `core.outbox.emit`), así que el runtime puede escribir un evento `ai.budget.*` falso. No puede tocar las marcas. Los suscriptores deben tratar estos eventos solo como notificaciones, nunca como prueba del gasto ni como orden.
- Por los mismos permisos, que ya existían, sobre `outbox_events` (INSERT, UPDATE y DELETE), el runtime también podría borrar o cambiar un aviso real antes de publicarlo. La marca queda puesta igual. Este ADR no cambia esos permisos.

## Security implications

La función sigue escribiendo solo dentro de la organización de la sesión. El evento no lleva más que dos ids y el mes. El riesgo nuevo es un evento en otra organización por un filtro ausente, y los mutantes de la condición 4 lo cubren.

## Operational implications

Ninguna acción al desplegar. Hoy nadie se suscribe a estos eventos: el publicador los marca como publicados. La notificación al propietario llega con su propio work item.

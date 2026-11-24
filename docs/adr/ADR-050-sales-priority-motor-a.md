# ADR-050: Prioridad comercial explicable sin IA («Motor A»): puntaje operativo por cliente desde señales estructuradas, tabla derivada recalculada por el outbox y por umbrales de tiempo, revisión humana auditada; la intención leída del texto («Motor B») queda apagada y necesita su propio ADR

- **Status:** Proposed
- **Date:** 2026-11-24
- **Deciders:** propietario del producto (pendiente); propuesta de diseño de la serie F5-10 («Sales Intelligence», primer hito).
- **Work items:** este ADR lo publica #1106 (F0-PROP); la serie F5-10o, F5-10a … F5-10i está en el anexo «Plan de work items» y solo se implementa **si el propietario acepta este ADR**.
- **Related:** [ADR-001](ADR-001-multi-tenancy.md) y [ADR-002](ADR-002-postgresql-rls.md) (tablas tenant-owned con FORCE RLS y FKs compuestas; sin excepción nueva); [ADR-003](ADR-003-auth-session.md) §5 (permisos con alcance); [ADR-007](ADR-007-conversation-assignment.md) (responsable); [ADR-011](ADR-011-observability-and-logs.md) (auditoría); [ADR-016](ADR-016-api-list-convention.md) §6 (un orden nuevo pide su cursor; este ADR lo usa, no lo enmienda); [ADR-018](ADR-018-owner-role-follows-catalog.md); [ADR-028](ADR-028-store-data-connectors.md) (stock externo fuera); [ADR-034](ADR-034-ai-copilot-first.md) §7, §11 y §12 (sin cambios: Motor A no es IA y Motor B no se implementa con este ADR); `Organization.timezone` (`organizations/models.py:106`, por omisión `America/Lima`) para todo «hoy» y «día»; ADR-043 solo fija el día del presupuesto de IA y no se usa aquí; [ADR-049](ADR-049-internal-follow-up-automations.md) (propuesta paralela de automatizaciones: se beneficiaría de los eventos `task.*` y `opportunity.*` de F5-10c). Diseño de fase 0: [02 §E.10](../fase-0/02-modelo-de-datos.md) (`leads`, `lead_score_events`), [03 §J.11](../fase-0/03-tenancy-rbac-inbox-ia.md) (Supervisor con reglas deterministas), historias E09-02, E09-03 y E11-19.

> **Mientras este ADR esté `Proposed` no se implementa nada.** No crea tablas, permisos, eventos ni pantallas. Es una propuesta para que el propietario decida.

## Context

- **El hueco.** El CRM ya tiene todo lo que dice si un cliente necesita atención comercial, pero repartido: «Sin responder» en el Inbox (`conversations/selectors.py:36`), la solicitud de persona (`conversations.human_requested_at`), cotizaciones enviadas que vencen (`quotes.valid_until`, barredor de `quotes/tasks.py`), oportunidades con próxima acción y estancadas (`deals/models.py:251-254`), tareas vencidas y de cobro (`tasks/models.py:11-20,66-70`). Nadie ve en un solo lugar **a quién atender primero y por qué**.
- **Lo que no existe.** No hay tabla `leads` ni `lead_score_events` (02 §E.10 no se implementó), ni permiso de prioridad, ni pagos o pedidos. Oportunidades, tareas e inventario **solo se auditan**: no emiten al outbox (`deals/opportunities.py:334-413`, `deals/opportunity_stages.py:151-224`, `tasks/commands.py:219-281`; ningún `emit` en `apps/inventory`). `quote.created` tampoco (`quotes/commands.py:357`).
- **La lista necesita el puntaje guardado.** ADR-016 §4 solo admite el orden `id`/`-id`; `RecencyPagination` (`core/api/pagination.py:118`) es el precedente de un cursor de dos valores. Ordenar por puntaje con cursor exige una columna guardada e indexada.
- **El texto del cliente es dato personal** (security-boundaries §3). Hoy solo se procesa para una intención (el detector de traspaso de `ai_agents/detectors.py:321`) **dentro** de una llamada del copiloto que pide una persona, con la IA encendida y la conversación incluida. Interpretar el texto en segundo plano para perfilar al cliente sería un **tratamiento nuevo** (Ley 29733: finalidad, aviso, oposición y el derecho al tratamiento objetivo que cita el borrador `docs/legal/aviso-ia-tratamiento-datos-borrador.md:91`).
- **Un puntaje no es una probabilidad.** La fase 0 ya advertía (03 §J) que una confianza autodeclarada no está calibrada. Aquí tampoco hay datos de cierre para calibrar nada.

## Decision (propuesta)

### 1. Qué es Motor A

Una función **pura y determinista** (`apps.sales_intel.engine`, sin base de datos, sin red, sin reloj propio: recibe `now`) que, a partir de **señales estructuradas** de un cliente, devuelve un puntaje **operativo** de 0 a 100 para **ordenar el trabajo**, una categoría, un nivel de intención, una confianza, sus señales con fuente y hora, la versión del algoritmo y el próximo instante en que el resultado cambia solo por el paso del tiempo. **No es una probabilidad de compra** y la pantalla lo dice. Mismas entradas y mismo `now` → mismo resultado (prueba de propiedad).

- **Unidad: el contacto** (`contacts.id`). Es lo que existe hoy; las cotizaciones y tareas cuelgan del contacto, no de la conversación (`quotes` no tiene `conversation_id`). Si llega un módulo `leads`, adoptará el mismo motor por lead con otro ADR.
- **Módulo:** `apps.sales_intel`, nueva fila de L4 junto a `apps.ai_agents` y **sin importarse entre sí** (lectura transversal de module-dependencies §1): lee el negocio solo con selectores de `conversations`, `quotes`, `deals`, `tasks`, `contacts` e `inventory`, y no escribe en ningún otro módulo. Un contrato `protected` de import-linter: solo `apps.sales_intel.tasks` y su `api` importan el servicio que escribe.

### 2. Señales permitidas (algoritmo `A1`)

Solo hechos estructurados ya guardados. Cada señal tiene un código, un tipo (`INTENCION` o `URGENCIA`), una fuente (`{type, id}`: conversación, cotización, oportunidad, tarea o variante), su hora (`at`: la del hecho, no la del cálculo) y sus puntos. Una señal por código y contacto (la más fuerte; empate, la más reciente).

| Código | Tipo | Fuente y regla (todo «abierto» excluye borrado lógico) | Puntos | Frase (es-PE) |
|---|---|---|---|---|
| `UNANSWERED` | URGENCIA | Conversación que cumple `selectors.UNANSWERED` (tras #1102, con su regla de respuestas fallidas); `at = last_inbound_at` | 30; 40 desde 2 h | «escribió hace {tiempo} y todavía espera respuesta» |
| `REPLY_FAILED` | URGENCIA | `last_reply_failed` (tras #1102) | 15 | «tu última respuesta no le llegó» |
| `HUMAN_REQUESTED` | URGENCIA | Solicitud de persona abierta (la regla `waiting()` de #1102). Ojo: `human_requested_at` la fija el detector de texto del copiloto (`ai_agents/detectors.py`), no solo una acción humana | 25 | «pidió que lo atienda una persona» (nunca el motivo `UPSET`) |
| `QUOTE_IN_PREPARATION` | INTENCION | Cotización `DRAFT`, `PENDING_APPROVAL` o `APPROVED` creada hace ≤ 7 días | 20 | «tiene una cotización por enviar ({número})» |
| `QUOTE_SENT_AWAITING` | INTENCION | `SENT`/`VIEWED` vigente; `at = sent_at` | 20 | «tiene una cotización enviada sin respuesta ({número})» |
| `QUOTE_EXPIRING` | URGENCIA | `SENT`/`VIEWED` con `valid_until` en ≤ 48 h | 20 | «su cotización vence {cuándo}» |
| `QUOTE_ACCEPTED_OPEN` | INTENCION | `ACCEPTED` hace ≤ 14 días y su oportunidad sigue `OPEN` (o no tiene) | 35 | «aceptó la cotización y falta cerrar la venta» |
| `QUOTE_EXPIRED_RECENT` | INTENCION | `EXPIRED` hace ≤ 7 días sin revisión posterior emitida | 10 | «su cotización venció hace poco» |
| `QUOTE_DECLINED_RECENT` | INTENCION | `REJECTED` por el cliente hace ≤ 14 días | −20 | «rechazó una cotización hace {tiempo}» |
| `PAYMENT_PENDING` | INTENCION | Tarea abierta de tipo `COLLECTION` | 30 | «tiene un cobro pendiente» |
| `FOLLOW_UP_OVERDUE` | URGENCIA | Tarea abierta con `due_at < now` | 25; 35 desde 48 h | «un seguimiento venció hace {tiempo}» |
| `FOLLOW_UP_DUE_TODAY` | URGENCIA | Tarea abierta que vence hoy (día local de la organización, `Organization.timezone`) | 10 | «tiene un seguimiento para hoy» |
| `OPPORTUNITY_NEXT_ACTION_DUE` | URGENCIA | Oportunidad `OPEN` con `next_action_at ≤ now` | 15 | «la próxima acción de {número} ya venció» |
| `OPPORTUNITY_ADVANCED` | INTENCION | Paso a una etapa posterior (`sort_order` mayor) en ≤ 7 días (`opportunity_stage_history`) | 10 | «su oportunidad avanzó de etapa» |
| `OPPORTUNITY_HIGH_PROBABILITY` | INTENCION | `OPEN` con `probability ≥ 60` | 10 | «su oportunidad tiene probabilidad alta» |
| `OPPORTUNITY_STALE` | URGENCIA | `OPEN` sin actividad en ≥ 3 días (`last_activity_at`) | 10 | «su oportunidad lleva {días} días sin actividad» |
| `STOCK_BACK` | INTENCION | Una variante de una oportunidad `OPEN` pasó de disponible ≤ 0 a > 0 en ≤ 72 h (solo inventario LOCAL, ADR-028) | 15 | «volvió el stock de un producto que le interesa» |

**Prohibido en Motor A:** el texto de mensajes, notas, títulos, descripciones y motivos libres; el contenido de adjuntos; la salida de un modelo; costos, márgenes y listas internas; datos de otros contactos. Las frases describen **el hecho guardado**, nunca una intención que nadie registró (con datos estructurados no se sabe que el cliente «pidió» una cotización; se sabe que hay una por enviar).

**Reglas por palabras clave sobre el texto (descartadas en Motor A).** Una regla local («precio», «cuánto», «lo quiero») no sale a un tercero y es barata, pero: (a) es un tratamiento nuevo del contenido con fin de perfilado, que el aviso de datos no cubre; (b) tendría que respetar la exclusión y la oposición igual que la IA; (c) acerca el texto del cliente a un proceso en segundo plano (más superficie para logs y errores); (d) falla con negaciones, ironía y jerga («no, ya no lo quiero»), y su acierto aparente daría una confianza falsa. **Recomendación: ninguna regla sobre el texto en Motor A.** La única huella del texto que usa es `human_requested_at`, que ya es un estado del Inbox y nace de una acción humana o de una llamada del copiloto con la IA permitida. Cualquier intención leída del texto va en Motor B (§12).

### 3. Puntaje, categoría, intención y confianza

- **Puntaje** = suma de los puntos, acotada a 0–100. **Categoría:** `ALTA` ≥ 60; `MEDIA` 30–59; `BAJA` 1–29, o solo señales negativas; `SIN_DATOS` sin señales.
- **Intención** (filtro aparte): la suma de las señales `INTENCION`: `ALTA` ≥ 35; `MEDIA` 15–34; `BAJA` 1–14 o negativa; `SIN_DATOS` sin ninguna.
- **Confianza** (solidez de la evidencia, **no** una probabilidad): `ALTA` con dos o más señales positivas de dos o más tipos de fuente; `MEDIA` con dos de una sola fuente, o una sola de menos de 24 h; `BAJA` en lo demás; nula con `SIN_DATOS`.
- **Próximo cambio** (`next_recompute_at`): el menor instante futuro en que una regla cambia sola (2 h de espera, 48 h antes del vencimiento, el vencimiento, el `due_at`, el inicio del día local de la organización, el fin de cada ventana de 7, 14 o 72 h, `last_activity_at` + 3 días).
- **Pesos y umbrales fijos en el código por versión.** Cambiarlos es una versión nueva (`A2`), con su prueba de tabla y su nota en `docs/phases/`; nunca un ajuste por organización en este corte.

### 4. Explicaciones en español

La API devuelve **códigos y parámetros** (ids, números `COT-…`/`OPP-…`, horas, tipo de canal), nunca frases ni texto libre; la pantalla compone la explicación con `next-intl` (`es-PE.json`). Titular: categoría + las dos señales con más puntos (empate: la más reciente), p. ej. **«Alta prioridad: escribió hace 3 h y todavía espera respuesta, y tiene una cotización por enviar (COT-000123)»** (40 + 20 = 60); las demás, como etiquetas. La frase del pedido original («pidió una cotización…») no se usa: Motor A no sabe qué pidió el cliente, solo que hay una cotización por enviar. Junto al puntaje: «Puntaje para ordenar el trabajo; no es una probabilidad de compra.» Los títulos de tareas y oportunidades no se muestran en la razón (pueden llevar datos personales): solo su número o su tipo.

### 5. Almacenamiento: tabla derivada, con un primer corte calculado al leer

- **F5-10a calcula al leer** la prioridad de **un** contacto (sin tabla ni migración) para validar señales y frases con el propietario. La lista no puede calcularse al leer (orden por puntaje con cursor, §8).
- **Desde F5-10d, `sales_priorities`** (una fila por contacto con señales):
  - `id` UUIDv7; `organization_id` NOT NULL (FK a `organizations`); `contact_id` NOT NULL, `UNIQUE (organization_id, contact_id)` y FK compuesta `(organization_id, contact_id) → contacts (organization_id, id)`.
  - `score smallint CHECK 0–100`; `category`, `intent_level` con `CHECK` de dominio; `confidence` nula solo con `SIN_DATOS` (`CHECK`).
  - `signals jsonb` (`CHECK jsonb_typeof = 'array'`, como mucho 20 elementos y 8 KiB): `{code, kind, points, source_type, source_id, at}` y solo números de documento como parámetro; **ni texto libre ni nombres**.
  - `algorithm_version` (`CHECK '^A[0-9]+$'`), `computed_at`, `next_recompute_at` (índice parcial `(organization_id, next_recompute_at) WHERE next_recompute_at IS NOT NULL`), `basis_hash` (sha256 del conjunto ordenado `código:fuente`: identifica «la situación» para la revisión, §7).
  - Columnas de filtro derivadas: `conversation_id` (la conversación abierta más relevante; FK compuesta a `conversations`), `waiting_since`, `last_interaction_at`, `has_pending_quote`, `has_overdue_followup`.
  - Índices: `(organization_id, score DESC, id DESC)` para la lista; `(organization_id, contact_id)` único para el Inbox.
- **RLS** con FORCE y una política PERMISSIVE por comando (`EnableRLS`). **Privilegios de `crm_app`:** `SELECT`, `INSERT`, `DELETE` (es derivada: una fila sin señales se borra) y `UPDATE` **solo de las columnas calculadas** (nunca `organization_id`, `id`, `contact_id`, `created_at`); sin `TRUNCATE`. Sin funciones `SECURITY DEFINER`: el recálculo corre en el tenant del evento, con RLS, como en el resto del outbox.
- No es la fuente de verdad: se puede vaciar y reconstruir con el comando de reconciliación (§6) sin perder nada; la revisión humana vive en otra tabla (§7).

### 6. Recálculo incremental

- **Servicio único** `apps.sales_intel.refresh.refresh_contact(ctx, contact_id)`: lee el estado actual, llama a `engine`, hace upsert (o borra) y nada más. Al ser función del estado actual es **idempotente y tolera desorden y duplicados**: un evento viejo recalcula con datos nuevos. Serializa por contacto con `pg_advisory_xact_lock` y no escribe si la fila ya tiene un `computed_at` posterior a la hora del evento.
- **Disparadores por outbox** (`@subscribe` en `apps/sales_intel/tasks.py`, cola propia de baja prioridad): los que ya existen (`message.received`, `message.queued`, `message.updated`, `conversation.updated`, `conversation.assigned`, `quote.issued`, `quote.accepted`, `quote.declined`, `quote.expired`, `quote.cancelled`, `quote.revised`) y **eventos nuevos solo con ids** que añade F5-10c: `quote.created`; `opportunity.created|updated|assigned|deleted|moved|won|lost|reopened` (`opportunity_id`, `contact_id`, `status`); `task.created|updated|assigned|completed|cancelled|reopened|deleted` (`task_id`, `contact_id`, `opportunity_id`, `type`, `status`); y en F5-10i `inventory.available_restored` (`variant_id`, `warehouse_id`).
- **Umbrales de tiempo:** un barredor de plataforma cada 5 minutos encola por organización activa el recálculo de las filas con `next_recompute_at ≤ now()`, con el patrón de `quotes/tasks.py` (tope por pasada y tiempo, `lock_timeout`, un fallo no detiene el resto).
- **Reconciliación** diaria y como comando idempotente (también el *backfill* del despliegue): recorre los contactos candidatos (con conversación abierta, cotización abierta o reciente, oportunidad `OPEN` o tarea abierta) por lotes y por organización.

### 7. Revisión humana y ajuste manual

- Tabla **`sales_priority_reviews`** [INM]: `crm_app` solo `SELECT` e `INSERT` (sin `UPDATE`, `DELETE` ni `TRUNCATE`, como el historial de etapas). Columnas: contacto (FK compuesta), `basis_hash` y `algorithm_version` de lo que se revisó, `decision` (`CONFIRMED`, `OVERRIDDEN`, `DISMISSED`), `override_category` (obligatoria solo con `OVERRIDDEN`, `CHECK`), `reason_code` de una lista cerrada (`YA_COMPRO`, `NO_INTERESADO`, `DATOS_DESACTUALIZADOS`, `CRITERIO_DEL_VENDEDOR`, `OTRO`), `note` opcional (500, auditada como `[texto]`), `reviewed_by_user_id` (FK compuesta a membresías, `core.db.member_fk`), `reviewed_at`, `expires_at` (≤ 30 días).
- **Vigencia:** cuenta la última revisión del contacto cuyo `basis_hash` es el actual y no venció. Si la situación cambia (aparece o desaparece una señal), el ajuste **caduca solo** y la fila vuelve a «Sin revisar». El puntaje calculado **nunca se sobrescribe**: la API devuelve ambos (`computed` y `effective`).
- Estado para la pantalla: `SIN_REVISAR`, `CONFIRMADA`, `AJUSTADA`, `DESCARTADA` («no requiere atención»: sale de la vista por defecto hasta que la situación cambie).
- **Auditoría** `sales_priority.reviewed` con ids, decisión, categoría antes y después, código de motivo y versión; la nota como `[texto]`. El recálculo automático no se audita (es derivado; queda `computed_at`).

### 8. Permisos, alcance y lectura

- **Permisos nuevos:** `sales_priority.view` y `sales_priority.review`, ambos con alcance (◎), en el módulo `sales_intel`. Plantillas propuestas (decisión del propietario): Owner (todo, ADR-018), Administrador y Supervisor con `ORGANIZATION`, Vendedor con `OWN`.
- **Alcance de una fila:** visible si el contacto está en el alcance de quien consulta (la política de columnas de contactos: responsable y sucursal) **o** si lo está su `conversation_id` (la política del Inbox: asignada a mí, a mi equipo o sin asignar). Fuera de alcance, de otra organización o inexistente: el mismo 404.
- **Cada señal se filtra por quien mira:** en el detalle y en la explicación de la lista, una señal se muestra solo si quien consulta ve su fuente con el permiso de su módulo y su alcance (`quotes.view`, `opportunities.view`, `tasks.view`, `conversations.view`), comprobado por lotes por página. El puntaje y la categoría que se muestran en esa explicación se **recalculan con `engine` sobre las señales visibles**.
- **Límite abierto (decisión D-SI-5): los valores guardados no se filtran.** El orden de la lista, los filtros sobre valores guardados (`category`, `intent`, `pending_quote`, `overdue_followup`, `waiting`) y la insignia del Inbox (categoría, puntaje y titular, por el `LEFT JOIN`) usan lo guardado, que se calculó con **todas** las señales del contacto. Quien no ve alguna de las fuentes puede inferir que existe (por ejemplo, filtrar `pending_quote` revela que el contacto tiene una cotización que no puede ver). Este ADR **no afirma que eso sea seguro**. Opciones: (a) la insignia, el orden y esos filtros solo para quien tiene alcance `ORGANIZATION` en `sales_priority.view` y en los módulos de las señales; los demás ven la lista recalculada con sus señales visibles y sin esos filtros; (b) recalcular por página para todos, con el coste de consultas y un orden que ya no coincide con el índice; (c) aceptar la fuga para los cuatro roles de plantilla, porque la fuente de cada señal ya es visible para su responsable. Recomendación: (a) en el primer corte.
- **Lista:** `GET /api/v1/o/{slug}/sales/attention/` con un `ScorePagination` (cursor de dos valores `score`, `id`, como `RecencyPagination`; ADR-016 §6) y filtros: `category`, `intent`, `channel` (plataforma de `conversation_id`), `assignee` (`me`, un id o `none`: quien atiende la conversación o, sin ella, el responsable del contacto), `last_interaction` (`today`, `7d`, `30d`, `older`), `waiting` (`15m`, `2h`, `24h`), `pending_quote`, `overdue_followup`, `review` (`pending`, `all`). **Detalle:** `GET …/contacts/{id}/sales-priority/`.
- **Inbox:** la lista de conversaciones añade `priority` (`category`, `score`, código del titular) con un `LEFT JOIN` por `(organization_id, contact_id)`, sin recalcular nada al leer y sin cambiar su orden por recencia; un filtro «Prioridad alta». La insignia y ese filtro usan valores guardados y están sujetos a D-SI-5.

### 9. Rendimiento

- Lista: recorrido del índice `(organization_id, score DESC, id DESC)` con `LIMIT 51` y uniones por clave primaria a `contacts` y `conversations`; como mucho cuatro consultas por lotes para la visibilidad de las señales.
- Inbox: una sonda por fila al índice único; sin subconsultas nuevas.
- Recálculo: unas siete consultas con los índices que ya existen (`conversations_org_contact_idx`, `quotes_org_contact_idx`, `opportunities_org_contact_idx`, `tasks_org_contact_idx`, `opp_history_org_opp_idx`); una ráfaga de mensajes del mismo cliente se colapsa con el candado y la comparación de `computed_at`.

### 10. Despliegue (expand/contract)

- Todo es **aditivo**: dos tablas nuevas, columnas de filtro nuevas, permisos nuevos y eventos nuevos. La imagen anterior no las conoce y sigue funcionando; volver atrás no exige migración.
- Orden (outbox-audit §«Despliegues»): primero los workers con los `@subscribe` de `sales_intel`, después los emisores nuevos. Un evento sin suscriptor se marca publicado y la reconciliación lo cubre.
- *Backfill* con el comando de reconciliación tras migrar; la vista muestra «Calculado hace …» con `computed_at`.
- El *contract* (borrar tablas o columnas) solo en un release posterior y con su work item.

### 11. Versiones

`algorithm_version` en cada fila y en cada revisión. Una versión nueva recalcula todo con la reconciliación y caduca los ajustes hechos sobre la anterior (cambia `basis_hash`).

### 12. Motor B (fuera de este ADR; queda apagado)

Motor B sería la intención **leída del texto** de una conversación por un modelo. Este ADR **no lo decide ni lo implementa**; fija sus límites para que el ADR que lo proponga parta de ellos:

1. **Apagado por omisión** y con su propio interruptor (servidor y organización), además de `AI_ENABLED` y del interruptor de IA de la organización.
2. **Exclusión estricta:** nunca lee una conversación con `ai_excluded_at` (la misma comprobación y el mismo candado del copiloto, `attention.ai_call`/`ai_included`); no mezcla conversaciones.
3. **Las siete condiciones** de [ai-copilot-activation.md](../runbooks/ai-copilot-activation.md) §2 cumplidas, más un **GO separado** del propietario para este propósito: el GO del copiloto no lo cubre.
4. **Sin llamadas en segundo plano:** solo cuando una persona con `ai.use` lo pide en una conversación. Un análisis por lotes o por evento necesita otro ADR, presupuesto propio y otro GO.
5. **Minimización de ADR-034 §7:** solo los mensajes visibles para el cliente, enmascarados. Enviar al modelo datos de cotizaciones, oportunidades o la salida de Motor A **contradice** ADR-034 §7 y exigiría enmendarlo.
6. **Nunca cambia Motor A:** su resultado se guarda en una tabla `ai_*` con su `ai_run_id`, se muestra aparte con la etiqueta «Generado por IA» y solo una persona puede convertirlo en un ajuste manual (§7), con su auditoría.
7. **Aviso de datos** actualizado con la nueva finalidad antes de encenderlo.

## Alternatives considered

| Alternativa | Por qué no (o por qué se pregunta) |
|---|---|
| Implementar `leads` y `lead_score_events` de la fase 0 | Obliga a crear y mantener leads (alta, conversión, deduplicación) antes de dar valor; nada los crea hoy. El motor por contacto se puede adoptar por lead después. |
| Calcular todo al leer (sin tabla) | Válido para un contacto (F5-10a); la lista ordenada por puntaje con cursor y los filtros no escalan, y duplicar las reglas en SQL rompe la explicabilidad (dos implementaciones). |
| Vista materializada de PostgreSQL | Refresco completo, sin incrementalidad por evento ni FORCE RLS sobre la vista; el *runtime* no tiene DDL. |
| Puntaje dentro de `conversations` o `contacts` | Mezcla un dato derivado con tablas de dominio y sus `version`; cada recálculo chocaría con ediciones humanas (`StaleVersion`). |
| Pesos configurables por organización desde el primer corte | Sin datos para calibrarlos; complica pruebas y explicaciones. Versión fija y revisable. |
| Reglas por palabras clave sobre el texto | §2: tratamiento nuevo del contenido, frágil, sin cobertura en el aviso. |
| Un LLM que puntúe (Motor B como base) | No determinista, con costo y salida de datos; ADR-034 §7, §11 y §12 lo impiden sin otro ADR. |
| Escribir el puntaje en `opportunities.priority` | Es la prioridad **manual** del vendedor (`deals/models.py:246`): se pisaría una decisión humana. |

## Consequences

- Una vista «Leads que requieren atención» y una insignia en el Inbox con su porqué en español, sin IA y sin salida de datos.
- Oportunidades, tareas y `quote.created` pasan a emitir eventos con ids: también sirven a automatizaciones futuras (E16).
- Dos tablas nuevas, dos permisos, un barredor y una cola de baja prioridad. Más carga de workers proporcional a la actividad.
- El puntaje depende de que la gente use cotizaciones, tareas y oportunidades: con poco uso habrá muchos `SIN_DATOS` (es honesto, no un error).

## Security implications

- Sin excepción a ADR-001/ADR-002: FORCE RLS, FKs compuestas, organización del contexto, sin `SECURITY DEFINER` que escriba.
- **Sin texto de clientes** en las tablas, en el outbox, en la auditoría ni en los logs: solo códigos, ids, números de documento y horas. Prueba: el payload y `signals` no contienen ningún campo libre de las fuentes.
- La **explicación** (detalle y razones) no revela lo que quien mira no podría ver en su fuente: se recalcula con las señales visibles (§8), con pruebas de alcance cruzado (vendedor OWN, otro equipo, otra organización). El orden, los filtros sobre valores guardados y la insignia del Inbox **no tienen esa garantía** hasta que el propietario decida D-SI-5 (§8); no se afirma que sean seguros.
- El ajuste manual no borra el cálculo y queda auditado; `sales_priority_reviews` es de solo inserción por privilegios.
- Ley 29733: es una priorización **interna** que no decide nada sobre el cliente (una persona decide siempre). Se recomienda que la asesoría confirme si el aviso de datos debe mencionarla (derecho al tratamiento objetivo).

## Operational implications

- Al desplegar: migraciones, workers primero, el comando de reconciliación una vez. Sin variables de entorno nuevas en Motor A.
- Observabilidad: contadores del barredor (`refreshed`, `skipped`, `failed`) como `quotes.expired`; alerta si la cola de `sales_intel` crece.
- `docs/phases/phase-5.md` recibe la serie F5-10; el runbook de activación de IA no cambia hasta que exista Motor B.

## Decisiones requeridas del propietario

| # | Decisión | Opciones | Recomendación |
| --- | --- | --- | --- |
| D-SI-1 | Motor A y la tabla de señales y pesos `A1` (§2 y §3) | Aceptar `A1` tal cual / cambiar pesos o umbrales. Punto a validar: ¿una cotización aceptada sin venta cerrada (35) debe pesar más que un cliente sin respuesta desde hace 2 h (40)? ALTA empieza en 60 | Aceptar `A1` y revisarla con uso real en una versión `A2` |
| D-SI-2 | Quién ve y quién revisa (`sales_priority.view` y `.review`) | Owner, Administrador y Supervisor con toda la organización y Vendedor con lo suyo / otro reparto | Ese reparto, sujeto a D-SI-5 |
| D-SI-3 | Texto del cliente y revisión legal | Confirmar que Motor A no usa reglas sobre el texto, salvo la huella de `human_requested_at` (que viene del detector de texto del copiloto, no de una acción humana) / retirar `HUMAN_REQUESTED` de `A1` / permitir reglas sobre el texto; y si la asesoría debe confirmar que el aviso de datos menciona la priorización interna (Ley 29733) antes de usarla en producción | Confirmar sin reglas sobre el texto, dejar `HUMAN_REQUESTED` documentada como derivada del copiloto y pedir la revisión de la asesoría |
| D-SI-4 | Release de la serie frente a R0 a R4 | Después de cerrar R0 y sin bloquear R1 a R3 / otra / `release:later`. F5-10a es pequeño y puede ir antes para validar las frases | Después de R0, sin bloquear R1 a R3 |
| D-SI-5 | Visibilidad de los valores guardados (§8): orden, filtros sobre lo guardado e insignia del Inbox | (a) solo con alcance `ORGANIZATION` / (b) recalcular por página para todos / (c) aceptar la inferencia | (a) en el primer corte |

## Anexo: plan de work items

Orientativo: no se crea ningún issue de implementación hasta que el propietario acepte este ADR (F5-10o). Al crearlos se aplica la plantilla `.github/ISSUE_TEMPLATE/work-item.yml`, con las etiquetas `work-item`, `phase:f5`, la release que decida el propietario y `status:blocked` hasta que se cumpla su dependencia. El tamaño es una estimación del redactor en líneas relevantes (tope de `.github/scripts/pr_governance.py`: 800) y se confirma al crear cada issue. **Motor B no tiene work item:** necesita su propio ADR y un GO separado (§12).

| ID | Objetivo en una línea | Depende de | Tamaño estimado |
| --- | --- | --- | --- |
| F5-10o | El propietario responde las cinco decisiones (D-SI-1 a D-SI-5) y el ADR pasa a `Accepted` (con lo que cambie) o a `Deprecated` | Este ADR publicado (#1106) | Solo documentación |
| F5-10a | Prioridad de un cliente calculada al leer, con sus razones en español, en el panel del cliente del Inbox (primer corte útil, sin tabla) | F5-10o, #1093 | ~750; si se pasa, dividir en motor y API / tarjeta |
| F5-10b | Motor A completo (`A1`): resto de señales, intención y confianza, y tarjeta en la ficha 360° | F5-10a, #1093 | ~700 |
| F5-10c | Eventos del outbox de oportunidades, tareas y `quote.created`, solo con ids | F5-10o | ~550 |
| F5-10d | Tabla derivada `sales_priorities` y recálculo incremental (outbox, umbrales de tiempo y reconciliación) | F5-10b, F5-10c | ~800 |
| F5-10e | Permisos `sales_priority.view` y API de «Leads que requieren atención», con cada razón visible solo para quien ve su fuente | F5-10d | ~700 |
| F5-10f | Pantalla «Leads que requieren atención» | F5-10e | ~800 |
| F5-10g | Revisión humana y ajuste manual de la prioridad (backend), con auditoría y sin borrar el cálculo | F5-10e | ~500 |
| F5-10g2 | Revisar y ajustar desde la pantalla y desde la tarjeta del cliente | F5-10g, F5-10f | ~400 |
| F5-10h | Inbox: insignia de prioridad en la lista y filtro «Prioridad alta» | F5-10d, #1093 | ~350 |
| F5-10i | Señal «volvió el stock» de un producto de una oportunidad abierta (solo inventario local) | F5-10d | ~300 |

Orden: F5-10o abre la serie; F5-10a → F5-10b y F5-10c pueden ir en paralelo; ambos alimentan F5-10d, de la que cuelgan F5-10e (y de él F5-10f y F5-10g), F5-10h y F5-10i.

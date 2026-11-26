# ADR-049: Automatizaciones internas de seguimiento comercial: reglas fijas configurables por organización, evaluadas por barrido de estado, con efectos solo internos; el motor genérico (DSL y constructor) se aplaza

- **Status:** Accepted (2026-11-26, decisión del propietario registrada en [#1166](https://github.com/PapiCuche/crm-gooddoggy/issues/1166): automatizaciones internas de seguimiento con D-F11-01 a D-F11-07 y los modos OFF→SIMULATE→ON, con una semana de SIMULATE y revisión del propietario antes de ON; nunca mensajes a clientes. Registro y enmiendas en «Registro de decisiones del propietario (2026-11-26)»)
- **Date:** 2026-11-24
- **Deciders:** propietario del producto; redactado por el programa autónomo (ADR-015). Aceptado por el propietario el 2026-11-26 ([#1166](https://github.com/PapiCuche/crm-gooddoggy/issues/1166)).
- **Work items:** F11-02 … F11-09, en el anexo «Plan de work items» (sin issues de implementación hasta que el propietario acepte este ADR). Este ADR lo publica #1106 (F0-PROP). Aceptación: ADR-ACC ([#1166](https://github.com/PapiCuche/crm-gooddoggy/issues/1166)). Creados el 2026-11-26: F11-02 [#1176](https://github.com/PapiCuche/crm-gooddoggy/issues/1176), F11-03 [#1177](https://github.com/PapiCuche/crm-gooddoggy/issues/1177), F11-04 [#1178](https://github.com/PapiCuche/crm-gooddoggy/issues/1178), F11-05 [#1180](https://github.com/PapiCuche/crm-gooddoggy/issues/1180), F11-06 [#1181](https://github.com/PapiCuche/crm-gooddoggy/issues/1181), F11-07 [#1179](https://github.com/PapiCuche/crm-gooddoggy/issues/1179), F11-08a [#1183](https://github.com/PapiCuche/crm-gooddoggy/issues/1183), F11-08b [#1184](https://github.com/PapiCuche/crm-gooddoggy/issues/1184), F11-09 [#1182](https://github.com/PapiCuche/crm-gooddoggy/issues/1182) y F11-10 [#1185](https://github.com/PapiCuche/crm-gooddoggy/issues/1185) (retención).
- **Related:** [ADR-001](ADR-001-multi-tenancy.md), [ADR-002](ADR-002-postgresql-rls.md) (sin excepción nueva), [ADR-007](ADR-007-conversation-assignment.md), [ADR-010](ADR-010-messaging-policy.md) (no se toca: nada se envía), [ADR-011](ADR-011-observability-and-logs.md), [ADR-016](ADR-016-api-list-convention.md), [ADR-018](ADR-018-owner-role-follows-catalog.md), [ADR-019](ADR-019-outbound-email.md), [ADR-034](ADR-034-ai-copilot-first.md) §5 y §10 (patrón del kill switch), [ADR-045](ADR-045-ai-budget-alert-events.md) (patrón de aviso una sola vez), [ADR-048](ADR-048-continuous-delivery-policy.md) (clases de despliegue de las migraciones de esta serie, si se acepta), [ADR-050](ADR-050-sales-priority-motor-a.md) (propuesta paralela: añadiría eventos `task.*` al outbox)

## Context

El equipo comercial pierde ventas por seguimientos olvidados: conversaciones en las que el cliente escribió y nadie contestó, y cotizaciones enviadas de las que nadie volvió a preguntar. El CRM ya tiene las piezas, pero nada las une:

- «Sin responder» existe como filtro y contador (`conversations/selectors.py:34-36`, F6-09), y #1093 (F7b-14, PR #1102, ambos abiertos) lo corregirá para que una respuesta fallida no cuente. Nadie avisa cuando una conversación lleva demasiado tiempo así.
- Las tareas aceptan `source = AUTOMATION` desde F5-08a (`tasks/models.py:29-32`), con «Mis tareas» y un contador de vencidas en el menú. Ningún código crea tareas automáticas, y `reminder_at` se guarda pero nadie lo dispara.
- Las cotizaciones enviadas tienen `valid_until` y un barrido de vencimiento (`quotes/tasks.py`). No hay seguimiento antes de que venzan.
- El outbox (`core/outbox`) entrega al menos una vez, pero no tiene backoff, dead-letter ni purga, y un evento sin suscriptor se marca como publicado (OBS-F1-06-3, `docs/architecture/outbox-audit.md:32`). La Fase 1 aplazó ese diseño hasta tener «el primer consumidor real (automatizaciones o notificaciones)».
- La Fase 0 planificó un motor genérico WHEN/IF/THEN (`automation_rules` con DSL JSONB, `automation_runs`, acciones `SEND_TEMPLATE`, constructor visual; E16, Fase 11, P2). El mismo backlog dice que las automatizaciones fijas del MVP (seguimiento, estancadas, SLA) van como jobs configurables y no con el motor genérico (`docs/fase-0/05-backlog-y-roadmap.md:261`).
- Ya existen tres avisos de sistema hechos a mano con el mismo patrón: el presupuesto de IA (ADR-045), la caducidad de tokens (F7b-12) y la ingesta de Meta (F7-11a). En los tres, una marca se reclama en la base una sola vez, se audita antes de avisar y el aviso no se reintenta.
- El propietario quiere un CRM en producción con omnicanal e IA, y exige que nada salga solo al cliente (ADR-034, «nada se envía solo»).

## Decision

### 1. Alcance: solo efectos internos

Una automatización **nunca** escribe al cliente. No llama a `record_outbound`, no encola mensajes, no usa plantillas, no cambia consentimientos y no cambia el estado de una cotización, de una oportunidad ni de una conversación. Sus únicos efectos son:

| Efecto | Primer corte | Después |
|---|---|---|
| Crear una tarea interna (`source = AUTOMATION`) para un miembro activo | Sí | — |
| Cancelar esa misma tarea cuando su condición deja de cumplirse | Sí | — |
| Escalar: una segunda tarea, para supervisores | Sí | — |
| Prioridad de la conversación | No (la columna no existe) | Con su propio work item |
| Sugerir asignación (sin asignar) | No | Con su propio work item |
| Correo interno a miembros (`core.mail`) | No (SES en sandbox; decisión del propietario) | Opcional por regla |

Lo hacen cumplir dos cosas. Un contrato `forbidden` de import-linter impide que `apps.automations` importe `apps.conversations.commands`, `apps.conversations.delivery`, `apps.conversations.api`, las vistas, comandos y respuestas de `apps.quotes`, `apps.contacts.consents` y `core.mail` (este último solo hasta que el propietario lo permita). Y una prueba comprueba que un barrido completo no crea filas en `messages` ni eventos `message.*`.

### 2. Reglas fijas configurables, no un DSL (todavía)

Las reglas son **código registrado** en `apps.automations.rules`, una por clave: `conversation.unanswered` y `quote.no_response`. Cada organización tiene **una fila por regla** con su modo y sus parámetros. Los parámetros los valida el esquema de la regla en código, con tipos y rangos cerrados. No hay condiciones ni acciones libres en JSON. El motor genérico (condiciones componibles, más disparadores, constructor visual) queda para cuando haya al menos tres reglas pedidas que estas no cubran. Ese motor reutilizará las tablas, el registro de ejecuciones, los interruptores y las acciones de este ADR.

### 3. Disparo por barrido de estado (level-triggered), no por eventos

Cada regla es una **consulta de estado** que se evalúa periódicamente: «conversaciones abiertas sin responder desde hace ≥ N minutos» o «cotizaciones `SENT`/`VIEWED` enviadas hace ≥ H horas y todavía vigentes». No es una reacción a eventos del outbox. Razones:

- **Se corrige sola.** Si un barrido falla o un worker se cae, el siguiente encuentra el mismo estado. No hay eventos perdidos ni duplicados que conciliar, y no depende de OBS-F1-06-3.
- **No puede formar bucles.** Las acciones (tareas) no emiten eventos (`apps/tasks` no llama a `emit`) y ninguna regla lee tareas como disparador. Si se aceptara ADR-050, las tareas pasarían a emitir eventos `task.*`; aun así ninguna regla de este ADR los lee como disparador, así que el barrido no puede formar bucles. Además, cada ejecución guarda `depth = 0`, y el motor genérico futuro rechazará disparadores sobre eventos `task.*` y `automation.*` o con `depth ≥ 1`.
- **El cierre es igual de simple.** Cuando la condición deja de cumplirse (se respondió, se cerró, la cotización se aceptó, se rechazó, venció o se canceló), el mismo barrido cancela la tarea abierta que creó (`CANCELLED`, motivo fijo, p. ej. «La conversación ya se respondió»).

Patrón: el de `quotes.expire_sweep` (`quotes/tasks.py:52-81`). Una entrada de beat `automations.sweep` cada 60 s (`expires` 60) encola `automations.evaluate` por organización accesible. Esa tarea vuelve a comprobar el estado de la organización, lee sus reglas bajo RLS y vuelve enseguida si ninguna está en `SIMULATE` u `ON`. No hace falta una función `SECURITY DEFINER` nueva, así que no hay excepción a ADR-002 §3.3.

### 4. Modelo de datos (todo tenant-owned, RLS con FORCE, FKs compuestas, solo expand)

**`automation_rules`**: la configuración de una regla en una organización.
- `id` UUIDv7, `organization_id`, `key` (CHECK con las claves registradas), `mode` ∈ {`OFF`, `SIMULATE`, `ON`} (por omisión `OFF`), `params` JSONB (CHECK `jsonb_typeof = 'object'` y tamaño ≤ 2 KiB; la validación fina, en código), `version` ≥ 1 (sube en cada cambio, con control optimista como las tareas), `updated_by_user_id` (FK compuesta a `organization_memberships`), `created_at`, `updated_at`.
- UNIQUE (`organization_id`, `key`) y UNIQUE (`organization_id`, `id`) como destino de FKs compuestas.
- No hay fila hasta que alguien configura la regla: sin fila, la regla está en `OFF`.
- Cada cambio se audita como `automation.rule.updated`, con el antes y el después del modo y de los parámetros (son números y códigos, no texto libre).

**`automation_switches`**: el interruptor de emergencia de la organización, con el patrón de `ai_switches`.
- Una fila por organización: `paused` booleano, `changed_by_user_id` y `changed_at`. Sin fila = no pausada; las reglas siguen en `OFF` por omisión, así que el estado inicial ya es seguro.
- Pausar corta todas las reglas en el siguiente barrido, sin tocar su configuración, y se audita como `automation.paused`/`automation.resumed`. Lo pueden hacer quienes tienen `automations.manage` o el permiso nuevo `automations.kill_switch` (solo apagar, como `ai.kill_switch`). Reanudar pide `automations.manage`.

**`automation_runs`**: historial de ejecuciones, con idempotencia y cola de reintentos.
- `id`, `organization_id`, `rule_id` (FK compuesta), `rule_version` (la versión con que se decidió), `rule_key`, `subject_type` ∈ {`conversation`, `quote`}, `subject_id`, `episode_at` (timestamptz: el inicio del episodio, ver §5), `step` (1 = aviso, 2 = escalado), `mode` ∈ {`SIMULATE`, `ON`}, `status` ∈ {`DONE`, `SKIPPED`, `FAILED`, `DEAD`, `CLOSED`}, `reason` (código cerrado: `NO_RECIPIENT`, `MEMBER_INACTIVE`, `CAPPED`, `LOCK_TIMEOUT`, `DB_ERROR`, `RESOLVED`, `RULE_CHANGED`…; nunca el texto de una excepción), `recipients` (lista de ids de miembro, ≤ 10), `task_ids` (≤ 10; las tareas creadas, sin FK de array; ver Consecuencias), `attempts` (≤ 3), `depth` (0), `created_at`, `updated_at`, `closed_at`.
- **Clave de idempotencia:** UNIQUE (`organization_id`, `rule_id`, `subject_id`, `episode_at`, `step`, `mode`). Lleva `rule_id` y **no** `rule_version`: si la clave llevara la versión, cada edición de los parámetros volvería a avisar de todos los episodios abiertos, que es justo la tormenta que hay que evitar. La versión queda como dato. Lleva `mode` para que pasar de `SIMULATE` a `ON` sí cree la tarea real.
- Escriben en ella el barrido (`crm_app`, bajo RLS) y nadie más. `crm_app` no tiene `DELETE` ni `TRUNCATE` sobre ella, como las tareas. La purga a 180 días (D-F11-07) necesita otro camino, que elige F11-02 y documenta: un `GRANT DELETE` limitado a esta tabla para `crm_app` (que cambia esta frase) o un comando de mantenimiento con el rol `crm_migrator`. Hasta que el propietario decida la retención, no se purga nada. **Decidida el 2026-11-26 (D-F11-07):** 180 días, con una purga segura y auditada (F11-10, [#1185](https://github.com/PapiCuche/crm-gooddoggy/issues/1185)).

**`tasks`** (expand, sin contract): dos columnas nullable nuevas, `conversation_id` (FK compuesta a `conversations`; `tasks ──► inbox` ya está permitido en `module-dependencies.md`) y `quote_id` (FK compuesta a `quotes`). Se añaden `NOT VALID` y se validan en una migración aparte, como `core/db/member_fk.py`. El código viejo las ignora. La tarea enlaza a su conversación o a su cotización, y la pantalla lleva a ella.

### 5. Semántica de las dos reglas del primer corte

**`conversation.unanswered`** (conversación sin respuesta)
- **Cuándo:** la conversación está en `UNANSWERED` (la regla de #1093) y `awaiting_reply_since ≤ now() − first_after` (por omisión 15 min). `awaiting_reply_since` es una columna nueva de `conversations` (F11-04). La fija el primer entrante de un episodio y la borran una respuesta que cuenta (según #1093) y el paso a resuelta o cerrada. Es el `waiting_since` de 02 §E.7. El episodio es ese instante (`episode_at`).
- **Si:** la regla está en `SIMULATE` u `ON`, la organización no está pausada, `AUTOMATIONS_ENABLED` está encendido y la hora local de la organización (`Organization.timezone`, por omisión `America/Lima`, `organizations/models.py:106`; no el «día de Lima» de ADR-043, que solo vale para el presupuesto de IA) está dentro del horario de atención de los parámetros. Fuera de horario no se crea nada; al abrir, la condición sigue cumpliéndose y avisa entonces.
- **Entonces (paso 1):** una tarea `MESSAGE`, prioridad `HIGH`, vencida ya (`due_at = now`), con un título fijo («Responder a la conversación {número}») y enlazada a la conversación y al contacto. Va para la persona asignada, si es miembro activo. Si no hay persona, va para los `SUPERVISOR` del equipo asignado. Si tampoco hay equipo, va para quienes tienen `conversations.assign`. **Enmienda E1 (2026-11-26, D-F11-04):** si no aplica ningún supervisor (sin equipo, o un equipo sin supervisores activos), va para quienes tienen `conversations.assign`. Cuando el aviso no va a la persona asignada (supervisores o `conversations.assign`), el tope es `max_recipients`: por omisión 3, configurable solo entre 1 y 3, así que nunca pasa de 3; el orden, por `user_id`.
- **Escalado (paso 2):** si a `escalate_after` (por omisión 45 min) **desde que se creó el paso 1** la conversación sigue sin responder, se crea una tarea `URGENT` para los `SUPERVISOR` del equipo (o, sin equipo, para quienes tienen `conversations.assign`), sin repetir a quien recibió el paso 1. **Enmienda E1 (2026-11-26, D-F11-04):** si no aplica ningún supervisor (sin equipo, o un equipo sin supervisores activos), va para quienes tienen `conversations.assign`, con el mismo tope `max_recipients` (1 a 3). Se cuenta desde el paso 1 y no desde el episodio para que, al abrir el horario, el aviso y el escalado no salgan a la vez.
- **Cierre:** cuando el episodio termina, el barrido cancela las tareas abiertas de sus pasos con un motivo fijo y pone la ejecución en `CLOSED`/`RESOLVED`. Una tarea que una persona ya completó o cambió no se toca: el barrido compara su `version` y solo cancela si sigue abierta y sin cambios de nadie más.

**`quote.no_response`** (cotización enviada sin respuesta)
- **Cuándo:** la cotización está en `SENT` o `VIEWED`, `sent_at ≤ now() − after_hours` (por omisión 48 h) y `valid_until > now()`. El episodio es (`quote_id`, `revision`), con `episode_at = sent_at`.
- **Entonces:** una tarea `QUOTE`, prioridad `MEDIUM`, con vencimiento hoy, enlazada a la cotización, al contacto y a la oportunidad. Va para la persona asignada a la oportunidad o, si no la hay, para quien creó la cotización (si es miembro activo). Paso 2 opcional: un recordatorio `before_expiry_hours` (por omisión 24 h) antes de `valid_until`. **Decidido el 2026-11-26 (D-F11-03):** el recordatorio está activo por omisión junto con el aviso de 48 h; los dos son configurables.
- **Cierre:** cuando la cotización se acepta, se rechaza, vence, se cancela o se revisa, la tarea abierta se cancela.

### 6. Ejecución: idempotencia, reintentos, concurrencia y topes

- **Una transacción por sujeto.** La tarea y su fila de `automation_runs` se escriben juntas, con `lock_timeout` de 5 s. Si algo falla, no queda ninguna de las dos (salvo el fallo, que se anota en una transacción aparte).
- **Idempotencia:** `INSERT … ON CONFLICT DO NOTHING` sobre la clave del §4, *antes* de crear la tarea y en la misma transacción. Dos barridos a la vez (o una entrega repetida de Celery) dejan una sola tarea.
- **Concurrencia:** `pg_try_advisory_xact_lock(hashtextextended('automations:<org>:<rule>', 0))` por organización y regla durante la selección. Si otro barrido la tiene, este sale con `skipped_locked`. La unicidad sigue siendo la garantía última.
- **Reintentos acotados:** un fallo deja la ejecución en `FAILED` con `attempts + 1` y su `reason`. El siguiente barrido la reintenta si `attempts < 3` y pasaron `2^attempts` minutos. Al tercer fallo pasa a `DEAD` (dead-letter): no se reintenta más, se ve en el historial con su motivo y deja un log `automations.run_dead` de nivel `ERROR` (y Sentry, si está configurado).
- **Topes contra tormentas:** por pasada, organización y regla, como mucho `MAX_PER_PASS` = 100 tareas nuevas. Por persona, como mucho `max_open_per_user` (por omisión 20) tareas abiertas de la misma regla. Lo que pase el tope queda como `SKIPPED/CAPPED`, sin tarea, y se reintenta en el siguiente barrido. La pasada tiene un tope de 60 s. Si una regla supera el tope de pasada en tres barridos seguidos, se registra `automations.storm` (`ERROR`) y la regla **no** se apaga sola: lo decide una persona.
- **Simulación (dry-run):** en `SIMULATE` se escribe la ejecución (`mode = SIMULATE`, `status = DONE`, `recipients` calculados) y no se crea ninguna tarea. La pantalla muestra cuántas tareas habría creado y para quién. Es el modo recomendado durante la primera semana. **Enmienda E2 (2026-11-26):** deja de ser una recomendación: una regla pasa por `SIMULATE` al menos una semana, y el propietario revisa esa simulación antes de autorizar `ON`.
- **Actor:** `TenantContext(org, "celery", actor_type=SYSTEM)`, sin `user_id`. La tarea lleva `created_by_user_id = NULL` y `source = AUTOMATION`, y la auditoría `task.created` añade `{"rule": key, "run_id": …}` en `metadata`.
- **Entrada única a las tareas:** un módulo `apps.tasks.automation` (`create_automation_task`, `cancel_automation_task`) que solo puede importar `apps.automations` (contrato `protected`). Fija `source = AUTOMATION`, exige que el responsable sea miembro activo (`lock_active_member`) y no comprueba permisos de persona, porque no actúa ninguna.

### 7. Interruptores

1. **Global:** `AUTOMATIONS_ENABLED` (entorno, por omisión `False`; en `test` `True`). Apagado, el barrido no encola nada y la API de configuración sigue respondiendo y dice `global_enabled = false`.
2. **Por organización:** `automation_switches.paused`.
3. **Por regla:** `mode = OFF`.

Se comprueban al empezar la pasada y otra vez antes de cada sujeto. Pausar a mitad de una pasada la detiene en el siguiente sujeto.

### 8. Permisos

- Nuevos en el catálogo: `automations.view` (ver reglas e historial), `automations.manage` (cambiar modo y parámetros, reanudar) y `automations.kill_switch` (solo pausar; sensible, como `ai.kill_switch`).
- Plantillas por defecto: lo decide el propietario (D-F11-05). El Owner los recibe por ADR-018. **Decidido el 2026-11-26:** Owner y Administrador administran (`view` y `manage`); Supervisor puede leer y pausar (`view` y `kill_switch`).
- El historial muestra el número de la conversación o de la cotización, nunca textos del cliente, y solo a quien tiene además `conversations.view` o `quotes.view` sobre ese sujeto. A quien no los tiene se le muestra «sin acceso».

## Alternatives considered

| Alternativa | Por qué no ahora |
|---|---|
| Motor genérico WHEN/IF/THEN con DSL JSONB y constructor visual (02 §E.12, E16) | Son al menos cinco PRs de 800 líneas antes de tener la primera regla útil (DSL, evaluador, validación, constructor, pruebas de bucles). Encima exige endurecer el outbox (OBS-F1-06-3) y su superficie de error es mucho mayor. Hoy hay dos reglas pedidas. El backlog ya decidió jobs configurables para el MVP (`05:261`). |
| Disparo por eventos del outbox (`message.received` → programar el aviso a +N min con `scheduled_actions`) | Necesita acciones diferidas cancelables por evento, el orden de los eventos (que no está garantizado) y DLQ y backoff en el outbox. Un evento perdido es un aviso que no sale nunca; un barrido de estado lo vuelve a ver. Queda para el motor genérico. |
| Centro de notificaciones nuevo (`notifications`, E14) como canal del aviso | Necesita tabla, API, tiempo real, preferencias y UI nuevas. «Mis tareas» ya tiene lista, contador de vencidas y responsable, y una tarea también deja constancia de que hay algo que hacer. El centro de notificaciones puede llegar después como otro efecto. |
| Correo a la persona en cada aviso | SES sigue en sandbox en producción y avisos de minutos por correo generan ruido. Queda como opción por regla (D-F11-02). |
| Seguimiento automático al cliente con plantilla (`SEND_TEMPLATE`, 04 §167) | Prohibido por el alcance: nada sale solo. Además exigiría consentimiento y ventana (ADR-010). |
| `rule_version` dentro de la clave de idempotencia | Reabriría avisos ya dados en cada edición (tormenta). La versión se guarda como dato. |
| Una función `SECURITY DEFINER` que liste solo las organizaciones con reglas activas | Necesitaría una excepción de ADR-002 §3.3. Encolar por organización accesible, como el vencimiento de cotizaciones, basta a este tamaño. |

## Consequences

- Aparece `apps.automations` (L4, junto a `ai_agents`), con tres tablas, un barrido de beat cada 60 s, tres permisos y una pantalla de ajustes. Las tareas ganan `conversation_id` y `quote_id` y un punto de entrada protegido para el sistema.
- `conversations` gana `awaiting_reply_since`, que también le sirve al Inbox («esperando hace 25 min»). Depende de #1093 y de cómo quede «Sin responder».
- `automation_runs.task_ids` es una lista sin FK. La tarea no se borra (borrado lógico), así que la referencia no queda colgando. Si hace falta integridad referencial, una tabla puente (`automation_run_tasks`) la da sin contract.
- El generador de tareas automáticas puede llenar «Mis tareas». Los topes por persona y por pasada, la simulación previa y el cierre automático lo contienen.
- Cuando llegue el motor genérico, reutilizará `automation_runs` (idempotencia, reintentos, DEAD), los interruptores, los permisos y las acciones. Sus reglas serán filas con `key = 'custom'` y columnas nuevas (expand).
- No cambia ADR-002, ADR-010 ni el outbox.

## Security implications

- **Tenancy:** las tres tablas son tenant-owned con RLS y FORCE. Las FKs compuestas (`organization_id`, …) a reglas, membresías, tareas, conversaciones y cotizaciones impiden referencias cruzadas entre organizaciones. Las pruebas de aislamiento cubren cada ruta nueva con el stack real (OBS-F2-11-2).
- **Sin escalada:** el barrido actúa como `SYSTEM`, pero solo puede crear y cancelar tareas por `apps.tasks.automation`, solo para miembros activos y solo con los sujetos que su consulta ve bajo RLS. Ninguna persona puede usar una regla para crear tareas a nombre de otra con texto propio, porque título y descripción son fijos por regla.
- **Nada al cliente:** contratos de import-linter y una prueba de que no hay filas en `messages` (§1).
- **Sin PII en logs ni auditoría:** ids, códigos y cifras. Los títulos de las tareas llevan el número de la conversación o de la cotización, nunca el nombre ni el texto del cliente.
- **Abuso interno:** un `automations.manage` malicioso solo puede generar tareas internas, con topes. Pausar está al alcance de `automations.kill_switch`.

## Operational implications

- Clase de despliegue (ADR-048, si se acepta): las migraciones de las tres tablas nuevas serían de expansión (A3). En cambio, las FKs compuestas que F11-03 añade a la tabla `tasks` ya existente (`NOT VALID` y luego validadas) y sus índices parciales no están en la lista de §2.2 de ADR-048: ese release sería de clase B (aprobación explícita siempre) y no automático. Mientras ADR-048 no esté aceptado, nada cambia: cada despliegue sigue con su GO.
- Despliegue inerte: `AUTOMATIONS_ENABLED=false` por omisión y reglas en `OFF`. El orden es migraciones (expand), luego workers con el código y por último la entrada de beat. Encender es poner la variable en el archivo de entorno de `worker` y `beat` (ADR-033) y reiniciarlos. **Enmienda E3 (2026-11-26):** encender `AUTOMATIONS_ENABLED` en producción es una operación del propietario, no un simple cambio de variable, igual que el interruptor de ADR-050 (E1).
- Logs por pasada: `automations.swept` con conteos (`created`, `simulated`, `closed`, `skipped`, `failed`, `dead`, `capped`). `automations.run_dead` y `automations.storm` son de nivel `ERROR`.
- Runbook nuevo `docs/runbooks/automations.md`: encender, simular, pausar, leer el historial y qué hacer con `DEAD` y `storm`.
- Coste de base: una consulta indexada por organización, regla y minuto. «Sin responder» usa `conversations_unanswered_idx` (F6-09, migración 0037; #1102 puede cambiar su predicado y entonces se revisa esta frase); las cotizaciones, `quotes (organization_id, valid_until) WHERE SENT/VIEWED`. Un índice parcial nuevo `conversations (organization_id, awaiting_reply_since) WHERE awaiting_reply_since IS NOT NULL` se crea `CONCURRENTLY`.

## Decisiones requeridas del propietario

**Respondidas por el propietario el 2026-11-26** ([#1166](https://github.com/PapiCuche/crm-gooddoggy/issues/1166)): lo que decidió está en «Registro de decisiones del propietario (2026-11-26)», justo debajo. La tabla queda como se propuso.

| # | Decisión | Opciones | Recomendación |
|---|---|---|---|
| D-F11-01 | Enfoque | (A) reglas fijas configurables ahora y motor genérico después / (B) motor genérico ya | A |
| D-F11-02 | Canal del aviso | (A) tarea en «Mis tareas» / (B) A + correo interno / (C) centro de notificaciones nuevo | A; B cuando SES salga de sandbox |
| D-F11-03 | Valores por omisión y horario | Sin respuesta: 15 min; escalado: +45 min; cotización: 48 h y 24 h antes de vencer; horario: lunes a sábado de 09:00 a 19:00, hora de la organización | Esos valores, editables por organización |
| D-F11-04 | A quién va el aviso sin persona asignada, y el escalado | Supervisores del equipo → quienes tienen `conversations.assign` (tope 3) / solo Owner | La cadena con tope 3 |
| D-F11-05 | Permisos en plantillas | `manage` para Owner y Administrador; `view` para Supervisor; `kill_switch` para Supervisor | Así |
| D-F11-06 | Release y prioridad | Release nueva (p. ej. R1.5 «seguimiento») / `release:later` / dentro de R3 | Tras cerrar F7b-14 (#1093); antes de R4 |
| D-F11-07 | Retención del historial | 90 / 180 / 365 días | 180 |

## Registro de decisiones del propietario (2026-11-26)

El propietario aceptó este ADR como **automatizaciones internas de seguimiento** el 2026-11-26 y pidió no volver a preguntar estas decisiones. Queda registrado en [#1166](https://github.com/PapiCuche/crm-gooddoggy/issues/1166).

| # | Decisión del propietario |
| --- | --- |
| D-F11-01 | (A) Primero reglas fijas configurables; el motor genérico, después. |
| D-F11-02 | (A) Crear tareas en «Mis tareas». El correo interno, después, cuando SES esté autorizado y funcionando. |
| D-F11-03 | 15 min sin respuesta. Escalado 45 min después. Seguimiento de la cotización a las 48 h y 24 h antes de vencer. Lunes a sábado de 09:00 a 19:00 en la zona horaria de la organización. Todos los valores, configurables. |
| D-F11-04 | Avisar a los supervisores del equipo. Si no aplica ninguno, a quienes tienen `conversations.assign`, como mucho 3. |
| D-F11-05 | Owner y Administrador administran. Supervisor puede leer y pausar. |
| D-F11-06 | Una release propia, después de las dependencias del Inbox y antes de R4 (etiqueta `release:seguimiento`). |
| D-F11-07 | Historial de 180 días, con una purga segura y auditada. |

**Modos:** `OFF` → `SIMULATE` → `ON`. El propietario quiere una semana de `SIMULATE` y una revisión antes de autorizar `ON`.

**Límites:** nunca se envían mensajes a clientes. Nunca se cambian cotizaciones, ventas ni estados de conversación fuera del alcance aprobado (§1).

### Enmiendas al texto propuesto

- **E1 (§5, paso 1 y escalado).** Si no aplica ningún supervisor (sin equipo, o un equipo sin supervisores activos), el aviso va para quienes tienen `conversations.assign`, como mucho 3 (D-F11-04). El texto propuesto no decía qué pasaba con un equipo sin supervisores.
- **E2 (§6, simulación).** `SIMULATE` durante al menos una semana y la revisión del propietario son obligatorios antes de `ON`, no una recomendación. El código lo impone en parte: la API (F11-07, [#1179](https://github.com/PapiCuche/crm-gooddoggy/issues/1179)) y la pantalla (F11-08a, [#1183](https://github.com/PapiCuche/crm-gooddoggy/issues/1183)) rechazan pasar una regla a `ON` si no lleva al menos 7 días en `SIMULATE`. La revisión del propietario la impone el proceso (runbook de F11-08b).
- **E3 (Operational implications).** Encender `AUTOMATIONS_ENABLED` en producción es una operación del propietario, no un simple cambio de variable de entorno.
- **Tope de destinatarios (§5, D-F11-04).** `max_recipients` es configurable solo entre 1 y 3, para que ningún aviso pase de 3 personas.

## Anexo: plan de work items

Orientativo: no se crea ningún issue de implementación hasta que el propietario acepte este ADR (D-F11-01). **Creados el 2026-11-26** tras la aceptación, con los números de «Work items» (arriba) y la etiqueta `release:seguimiento`. F11-08 se dividió en F11-08a (pantalla) y F11-08b (marca «Automática», runbook y E2E), como preveía su fila; F11-10 (retención de 180 días) se añadió por D-F11-07. Las dependencias del Inbox (#1093) ya están cerradas. Al crearlos se aplica la plantilla `.github/ISSUE_TEMPLATE/work-item.yml`, con las etiquetas `work-item`, `phase:f11` (nueva), la release que decida D-F11-06 y `status:blocked` hasta que se acepte. El tamaño es una estimación en líneas relevantes (tope de `.github/scripts/pr_governance.py`: 800). F11-01 es este mismo ADR, que publica #1106.

| ID | Objetivo en una línea | Depende de | Tamaño estimado |
| --- | --- | --- | --- |
| F11-02 | Base de `apps.automations`: tablas, RLS, permisos, interruptores y comandos, sin API ni barrido | ADR aceptado | ~600 |
| F11-03 | Tareas: entrada del sistema (`apps.tasks.automation`) y enlaces a conversación y cotización | F11-02 | ~550 |
| F11-04 | Conversaciones: `awaiting_reply_since`, para saber desde cuándo espera respuesta | #1093 | ~450 |
| F11-05 | Barrido y regla «Conversación sin respuesta»: aviso al responsable, horario, simulación y cierre (primer corte con valor) | F11-02, F11-03, F11-04 | ~780 |
| F11-06 | Escalado a supervisores (paso 2) | F11-05 | ~350 |
| F11-07 | API: reglas, interruptor e historial | F11-02 | ~650 |
| F11-08 | Pantalla «Automatizaciones», marca «Automática», runbook y E2E | F11-03, F11-07 | ~800; si no cabe, dividir en 08a y 08b |
| F11-09 | Regla «Cotización enviada sin respuesta» y recordatorio antes de vencer | F11-05 | ~450 |

Camino crítico hasta el primer valor visible: F11-02 → (F11-03, y F11-04 tras #1093) → F11-05 → F11-07 → F11-08. F11-04 y F11-07 pueden ir en paralelo con F11-03. Si se quiere el aviso antes de la pantalla, el propietario puede activar la regla con un comando de gestión que llame a `commands.set_rule` (~40 líneas más en F11-05).

Fuera de esta serie (no hace falta decidirlo ya): sugerir asignación, prioridad de la conversación, avisos de stock bajo, oportunidades estancadas, `reminder_at` de las tareas manuales, el correo interno y el centro de notificaciones.

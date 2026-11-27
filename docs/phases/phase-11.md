# Fase 11 — Automatizaciones internas de seguimiento

- **Estado:** [ADR-049](../adr/ADR-049-internal-follow-up-automations.md) aceptada el 2026-11-26 (ADR-ACC, [#1166](https://github.com/PapiCuche/crm-gooddoggy/issues/1166)), con D-F11-01 a D-F11-07. Serie creada y bloqueada hasta que se cierre esa aceptación.
- **Objetivo (release propia, `release:seguimiento`):** avisar al equipo, con tareas en «Mis tareas», de las conversaciones sin respuesta y de las cotizaciones enviadas sin respuesta, con reglas fijas configurables por organización. Va después de las dependencias del Inbox (#1093, cerrada) y antes de R4.
- **Origen:** [ADR-049](../adr/ADR-049-internal-follow-up-automations.md) y la épica E16 del [backlog](../fase-0/05-backlog-y-roadmap.md), que deja el motor genérico para después.

Los issues llevan la etiqueta `phase:f11` y `release:seguimiento`. **El issue es la fuente de verdad** de cada work item.

## Principios

- **Nada sale al cliente.** Ningún mensaje, plantilla ni cambio de estado de cotizaciones, ventas o conversaciones fuera del alcance aprobado.
- **Apagado por omisión.** `AUTOMATIONS_ENABLED` en falso y cada regla en `OFF`. Antes de `ON`, una semana en `SIMULATE` y la revisión del propietario.
- **Sin IA.**

## Work items

| ID | Issue | Rama | Depende de |
|---|---|---|---|
| F11-02 | [#1176](https://github.com/PapiCuche/crm-gooddoggy/issues/1176) Base de `apps.automations` | `feature/f11-automations-foundation` | #1166 |
| F11-03 | [#1177](https://github.com/PapiCuche/crm-gooddoggy/issues/1177) Tareas: entrada del sistema y enlaces | `feature/f11-tasks-automation-entry` | #1176 |
| F11-04 | [#1178](https://github.com/PapiCuche/crm-gooddoggy/issues/1178) Conversaciones: `awaiting_reply_since` | `feature/f11-conversation-awaiting-reply-since` | #1166, #1093 |
| F11-07 | [#1179](https://github.com/PapiCuche/crm-gooddoggy/issues/1179) API de reglas, interruptor e historial | `feature/f11-automations-api` | #1176 |
| F11-07b | [#1214](https://github.com/PapiCuche/crm-gooddoggy/issues/1214) Historial de automatizaciones (API) | `feature/f11-automations-runs-api` | #1179 |
| F11-05 | [#1180](https://github.com/PapiCuche/crm-gooddoggy/issues/1180) Barrido y regla «Conversación sin respuesta» | `feature/f11-unanswered-conversation-rule` | #1176, #1177, #1178 |
| F11-06 | [#1181](https://github.com/PapiCuche/crm-gooddoggy/issues/1181) Escalado a supervisores | `feature/f11-unanswered-escalation` | #1180 |
| F11-09 | [#1182](https://github.com/PapiCuche/crm-gooddoggy/issues/1182) Regla «Cotización enviada sin respuesta» | `feature/f11-quote-no-response-rule` | #1180 |
| F11-08a | [#1183](https://github.com/PapiCuche/crm-gooddoggy/issues/1183) Pantalla «Automatizaciones» | `feature/f11-automations-settings-screen` | #1179, #1214 |
| F11-08b | [#1184](https://github.com/PapiCuche/crm-gooddoggy/issues/1184) Marca «Automática», runbook y E2E (cierra la serie) | `feature/f11-automations-tasks-badge-runbook` | #1177, #1183 |
| F11-10 | [#1185](https://github.com/PapiCuche/crm-gooddoggy/issues/1185) Retención de 180 días con purga auditada | `feature/f11-automation-runs-retention` | #1176 |

## Hecho

- **Hecho en F11-02 ([#1176](https://github.com/PapiCuche/crm-gooddoggy/issues/1176)):** `apps.automations` con `automation_rules`, `automation_switches` y `automation_runs` (FORCE RLS, FKs compuestas, privilegios por columna; `crm_app` sin `DELETE` ni `TRUNCATE`), los permisos `automations.view`, `automations.manage` y `automations.kill_switch`, los comandos `set_rule`, `pause` y `resume`, el esquema cerrado de parámetros con los valores de D-F11-03 y `AUTOMATIONS_ENABLED` apagado. Sin barrido, API ni pantalla.
  - **Plantillas (D-F11-05):** Owner, todo; Administrador, `view`, `manage` y también `kill_switch` (la regla de anti-escalada exige que cubra al Supervisor, como con `ai.kill_switch`; `manage` ya le deja pausar); Supervisor, `view` y `kill_switch`; Vendedor, ninguno. Las organizaciones que ya existen solo los reciben en el rol Owner (ADR-018 §3): el propietario los concede a los demás roles si quiere.
  - **`simulate_since`:** se fija con el reloj de la base al entrar en `SIMULATE`, se conserva mientras sigue ahí y se borra al salir (CHECK). Como defensa en profundidad, `set_rule` ya rechaza `ON` si la regla no lleva al menos 7 días en `SIMULATE` (E2); F11-07 añade la API y la pantalla.
  - **Topes de la base (E1):** `params.max_recipients`, si está, es un entero de 1 a 3, y una ejecución lleva como mucho 3 destinatarios y 3 tareas; en `SIMULATE`, ninguna tarea.
  - **Camino de la purga de 180 días (D-F11-07), para F11-10:** un `GRANT DELETE` sobre `automation_runs` solo para `crm_app`, que borra dentro de `tenant_scope` de cada organización bajo RLS, con un disparador `BEFORE DELETE` que rechaza las filas de menos de 180 días y una fila de auditoría por organización con el número de filas. Cambia la frase de ADR-049 §4 «`crm_app` no tiene `DELETE`», como el propio ADR prevé. Se descarta un comando con `crm_migrator`: ese rol no corre fuera del job de migraciones (ADR-002 §1.1). Hasta F11-10 no se purga nada.
  - **Clase de despliegue (ADR-048 §2.2):** solo crea tablas nuevas, pero los privilegios (`RunPython`) y las FKs a organizaciones y membresías (`RunSQL`) no están en la lista de A3: es B, compatible con la imagen anterior (no conoce las tablas).
- **Hecho en F11-07 ([#1179](https://github.com/PapiCuche/crm-gooddoggy/issues/1179)):** la API de `…/automations/`: `GET` (las dos reglas registradas, con su modo, parámetros completos, `version` (0 = sin configurar), `simulate_since` y `on_allowed_at`; el interruptor de la organización y `global_enabled`, solo de lectura) con `automations.view`; `PATCH rules/{key}/` (`version`, `mode`, `params` parciales) con `automations.manage`; `POST pause/` con `automations.kill_switch` o `automations.manage`; `POST resume/` con `automations.manage`. Supervisor: lee y pausa; Vendedor: nada.
  - **E2 en la API:** pasar a `ON` sin 7 días seguidos en `SIMULATE` (reloj de la base) responde 409 `SIMULATION_TOO_SHORT` y no cambia nada; versión vieja, 409 `STALE_VERSION`; una clave de `params` fuera del esquema o un valor fuera de rango (`max_recipients` de 1 a 3), 400 en `params`, sin repetir el valor.
  - **`global_enabled` apagado:** una regla puede pasar a `SIMULATE` u `ON`, pero nada se ejecuta; la lectura y el `PATCH` lo dicen con `global_enabled = false` (la pantalla, F11-08a, lo muestra). Ninguna ruta lo enciende (E3).
  - **Historial, dividido a F11-07b:** no cabía en el tope de 800 líneas relevantes (con él eran 1081), así que `GET runs/` (con «sin acceso» y el límite de consultas por página) pasa a F11-07b ([#1214](https://github.com/PapiCuche/crm-gooddoggy/issues/1214)). F11-08a depende también de él.
  - **Sin segundo factor:** ADR-049 no lo pide. Pedirlo en `PATCH` hacia `ON` y en `resume` es un endurecimiento posible más adelante.

- **Hecho en F11-03 ([#1177](https://github.com/PapiCuche/crm-gooddoggy/issues/1177)):** `tasks.conversation_id` y `tasks.quote_id` (nulas, sin reescribir la tabla) con FKs compuestas `(organization_id, …)` a `conversations` y `quotes`, añadidas `NOT VALID` en una sola sentencia con `lock_timeout` de 5 s (`0002`) y validadas aparte (`0003`, no atómica). Y `apps.tasks.automation`, la entrada del sistema a las tareas, que solo importa `apps.automations` (contrato `protected`) y que el contrato `forbidden` de ADR-049 §1 también revisa:
  - `create_automation_task`: solo un contexto `SYSTEM` sin persona; título y descripción fijos por regla y paso (solo el número del sujeto, leído de la base bajo RLS); el contacto (y la oportunidad de la cotización) salen del sujeto; de 1 a 3 responsables distintos, todos miembros activos (`lock_active_member`) o ninguna tarea; `source = AUTOMATION`, sin autor; auditoría `task.created` con la regla, el paso y la ejecución.
  - `cancel_automation_task`: solo una tarea automática viva de la organización enlazada al sujeto de la regla (si no, el mismo `DoesNotExist`); bloquea la fila y la deja si alguien la cambió (`version`) o ya está cerrada; motivo fijo por código (`RESOLVED`, `RULE_CHANGED`).
  - **Pendiente para F11-05 y F11-09:** ADR-049 (Operational implications) habla de «índices parciales» de estas FKs; F11-03 no añade ninguno (`crm_app` no borra conversaciones ni cotizaciones y nadie busca aún tareas por sujeto): F11-05 decide si el barrido busca por sujeto y, si lo hace, lo añade `CONCURRENTLY`. Una cotización cuya oportunidad es de otro contacto, o cuyo contacto u oportunidad está borrado, da `BadReference`: F11-09 lo anota como ejecución fallida.
  - **Sin idempotencia ni modo propios:** la idempotencia es la clave única de `automation_runs`, que el barrido (F11-05, [#1180](https://github.com/PapiCuche/crm-gooddoggy/issues/1180)) reclama con `ON CONFLICT DO NOTHING` en la misma transacción antes de crear; en `SIMULATE` no la llama (y la base rechaza tareas en una ejecución simulada). Aquí se prueba esa composición; el ejecutor real llega con F11-05.
  - **Clase de despliegue:** B a mano; C con el clasificador de #1206 (`RunSQL` y FKs `NOT VALID` sobre tablas que ya existen). La imagen anterior sigue escribiendo igual (prueba que repite su `INSERT`). La API y la pantalla de tareas no cambian.

## Observaciones

- **Hecho en F11-04 (#1178):** `conversations.awaiting_reply_since` (nula, migración `0066`), escrita solo por el disparador `conversations_reply_wait` (BEFORE, INVOKER, después de `conversations_reply_guard`) con la regla de «Sin responder» de #1093: la fija el primer mensaje del cliente de un episodio (su `messages.created_at`), la conserva mientras sigue sin respuesta y la borran una respuesta que cuenta (en cola cuenta hasta que falle; `PROVIDER_UNKNOWN` cuenta) y el paso a resuelta o cerrada. Cuando una respuesta en cola termina `FAILED`, vuelve el inicio del episodio: el primer mensaje del cliente después de la última respuesta que cuenta (una nota interna no es respuesta), leído de la misma columna del mismo mensaje, así que es exactamente la hora de antes, **salvo en los casos a) y b) de abajo**: ahí la hora cambia dentro de un mismo episodio y, con la clave de ejecución de ADR-049 (`episode_at` forma parte de una clave única), F11-05 abriría una segunda ejecución o tarea si no lo trata (requisito añadido a #1180). Nunca rechaza una escritura, así que la imagen anterior sigue escribiendo igual; lo que mande la aplicación se ignora. Índice parcial `conversations_awaiting_idx (organization_id, awaiting_reply_since) WHERE awaiting_reply_since IS NOT NULL`, `CONCURRENTLY` (`0067`), y relleno por tandas de las que ya esperaban (`0068`, solo como `crm_migrator`). Sin cambios en la API ni en el Inbox.
- **Aproximaciones de `awaiting_reply_since` que el barrido (F11-05) debe tener en cuenta:**
  - a) **Dice más espera de la real** si la conversación se resolvió sin responder, el cliente volvió a escribir y la respuesta siguiente falla: la hora es la del mensaje anterior a la resolución, porque el paso 4 no ve la resolución. Lo mismo en el relleno de una conversación así. La hora del episodio cambia.
  - b) **Dice menos** si el cliente escribe mientras una respuesta sigue en cola y luego esa respuesta falla: la conversación no se vuelve a escribir y la hora queda en el mensaje más reciente. Si después falla otra respuesta, el paso 4 la mueve al mensaje anterior (M1, respuesta en cola, M2, falla, falla otra: M2, M2 y luego M1): la hora del episodio cambia.
  - c) Volver a «Sin responder» sin un mensaje nuevo (hoy ningún comando lo hace) toma la hora del último mensaje del cliente. Si el disparador no ve ningún mensaje del cliente, toma `last_message_at`, sin fallar.
  - d) **No termina la espera** una respuesta enviada fuera del CRM (`FOREIGN_ECHO` de `delivery`: otra app o la bandeja nativa), ni las respuestas de otra app en un hilo en `standby`.
  - e) **Sí la termina** la confirmación automática de una baja o un alta (`queue_confirmation`, remitente `SYSTEM`), como en «Sin responder».
  - f) `messages.created_at` lo pone el reloj de cada proceso (web y worker pueden estar en hosts distintos), no el de la base.
  - g) Una respuesta que falla a la vez que llega un mensaje del cliente cae en el caso b). Tras un `save()`, el objeto en memoria puede tener un valor viejo: el barrido vuelve a leer la fila.

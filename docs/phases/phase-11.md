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
| F11-05 | [#1180](https://github.com/PapiCuche/crm-gooddoggy/issues/1180) Barrido y regla «Conversación sin respuesta» | `feature/f11-unanswered-conversation-rule` | #1176, #1177, #1178 |
| F11-06 | [#1181](https://github.com/PapiCuche/crm-gooddoggy/issues/1181) Escalado a supervisores | `feature/f11-unanswered-escalation` | #1180 |
| F11-09 | [#1182](https://github.com/PapiCuche/crm-gooddoggy/issues/1182) Regla «Cotización enviada sin respuesta» | `feature/f11-quote-no-response-rule` | #1180 |
| F11-08a | [#1183](https://github.com/PapiCuche/crm-gooddoggy/issues/1183) Pantalla «Automatizaciones» | `feature/f11-automations-settings-screen` | #1179 |
| F11-08b | [#1184](https://github.com/PapiCuche/crm-gooddoggy/issues/1184) Marca «Automática», runbook y E2E (cierra la serie) | `feature/f11-automations-tasks-badge-runbook` | #1177, #1183 |
| F11-10 | [#1185](https://github.com/PapiCuche/crm-gooddoggy/issues/1185) Retención de 180 días con purga auditada | `feature/f11-automation-runs-retention` | #1176 |

## Hecho

- **Hecho en F11-02 ([#1176](https://github.com/PapiCuche/crm-gooddoggy/issues/1176)):** `apps.automations` con `automation_rules`, `automation_switches` y `automation_runs` (FORCE RLS, FKs compuestas, privilegios por columna; `crm_app` sin `DELETE` ni `TRUNCATE`), los permisos `automations.view`, `automations.manage` y `automations.kill_switch`, los comandos `set_rule`, `pause` y `resume`, el esquema cerrado de parámetros con los valores de D-F11-03 y `AUTOMATIONS_ENABLED` apagado. Sin barrido, API ni pantalla.
  - **Plantillas (D-F11-05):** Owner, todo; Administrador, `view`, `manage` y también `kill_switch` (la regla de anti-escalada exige que cubra al Supervisor, como con `ai.kill_switch`; `manage` ya le deja pausar); Supervisor, `view` y `kill_switch`; Vendedor, ninguno. Las organizaciones que ya existen solo los reciben en el rol Owner (ADR-018 §3): el propietario los concede a los demás roles si quiere.
  - **`simulate_since`:** se fija con el reloj de la base al entrar en `SIMULATE`, se conserva mientras sigue ahí y se borra al salir (CHECK). Como defensa en profundidad, `set_rule` ya rechaza `ON` si la regla no lleva al menos 7 días en `SIMULATE` (E2); F11-07 añade la API y la pantalla.
  - **Topes de la base (E1):** `params.max_recipients`, si está, es un entero de 1 a 3, y una ejecución lleva como mucho 3 destinatarios y 3 tareas; en `SIMULATE`, ninguna tarea.
  - **Camino de la purga de 180 días (D-F11-07), para F11-10:** un `GRANT DELETE` sobre `automation_runs` solo para `crm_app`, que borra dentro de `tenant_scope` de cada organización bajo RLS, con un disparador `BEFORE DELETE` que rechaza las filas de menos de 180 días y una fila de auditoría por organización con el número de filas. Cambia la frase de ADR-049 §4 «`crm_app` no tiene `DELETE`», como el propio ADR prevé. Se descarta un comando con `crm_migrator`: ese rol no corre fuera del job de migraciones (ADR-002 §1.1). Hasta F11-10 no se purga nada.
  - **Clase de despliegue (ADR-048 §2.2):** solo crea tablas nuevas, pero los privilegios (`RunPython`) y las FKs a organizaciones y membresías (`RunSQL`) no están en la lista de A3: es B, compatible con la imagen anterior (no conoce las tablas).

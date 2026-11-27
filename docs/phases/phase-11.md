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

## Observaciones

- **Hecho en F11-04 (#1178):** `conversations.awaiting_reply_since` (nula, migración `0066`), escrita solo por el disparador `conversations_reply_wait` (BEFORE, INVOKER, después de `conversations_reply_guard`) con la regla de «Sin responder» de #1093: la fija el primer mensaje del cliente de un episodio (su `last_message_at`), la conserva mientras sigue sin respuesta y la borran una respuesta que cuenta (en cola cuenta hasta que falle; `PROVIDER_UNKNOWN` cuenta) y el paso a resuelta o cerrada. Cuando una respuesta en cola termina `FAILED`, vuelve el inicio del episodio: el primer mensaje del cliente después de la última respuesta que cuenta. Nunca rechaza una escritura, así que la imagen anterior sigue escribiendo igual; lo que mande la aplicación se ignora. Índice parcial `conversations_awaiting_idx (organization_id, awaiting_reply_since) WHERE awaiting_reply_since IS NOT NULL`, `CONCURRENTLY` (`0067`), y relleno por tandas de las que ya esperaban (`0068`, solo como `crm_migrator`). Sin cambios en la API ni en el Inbox.
- **Aproximaciones de `awaiting_reply_since` que el barrido (F11-05) debe tener en cuenta:**
  - a) **Dice más espera de la real** si la conversación se resolvió sin responder, el cliente volvió a escribir y la respuesta siguiente falla: la hora es la del mensaje anterior a la resolución, porque el paso 4 no ve la resolución. Lo mismo en el relleno de una conversación así.
  - b) **Dice menos** si el cliente escribe mientras una respuesta sigue en cola y luego esa respuesta falla: la conversación no se vuelve a escribir y la hora queda en el mensaje más reciente.
  - c) Volver a «Sin responder» sin un mensaje nuevo (hoy ningún comando lo hace) toma `last_message_at`. Si el paso 4 no ve ningún mensaje del cliente, también toma `last_message_at`, sin fallar.

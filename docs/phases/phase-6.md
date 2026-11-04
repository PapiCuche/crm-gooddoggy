# Fase 6 — Inbox omnicanal (canal Sandbox)

- **Estado:** en curso desde el 2026-11-04 (F6-01).
- **Objetivo:** conversaciones y mensajes con clientes en el producto oficial, primero con el canal Sandbox (sin proveedores externos): tablas, API, pantalla de lectura, envío, notas internas, asignación y panel del cliente. WhatsApp llega en la Fase 7.
- **Origen:** [roadmap §S.1](../fase-0/05-backlog-y-roadmap.md) (fila "Fase 6"), historias E06 del [backlog §R](../fase-0/05-backlog-y-roadmap.md), [modelo de datos §E.6](../fase-0/02-modelo-de-datos.md), [03 §I](../fase-0/03-tenancy-rbac-inbox-ia.md), [ADR-007](../adr/ADR-007-conversation-assignment.md) y [ADR-010](../adr/ADR-010-messaging-policy.md).

Los issues llevan la etiqueta `phase:f6`.

## Principios

- Tres ejes independientes (03 §I): el estado de la conversación, quién la atiende (equipo y persona a la vez, ADR-007) y el modo de atención. Mezclarlos es el error más común.
- Una conversación es un hilo de un contacto en una cuenta de canal; como máximo una no cerrada por contacto y cuenta (índice parcial), así dos mensajes entrantes a la vez no la duplican.
- Una nota interna no puede salir por ningún canal, y la primera barrera es la base de datos (ADR-010): un `CHECK` impide que un mensaje `INTERNAL` tenga id externo o remitente cliente.
- El historial de asignaciones es inmutable (`crm_app` no tiene `UPDATE` ni `DELETE`), nada del Inbox se borra, y el estado de entrega de un mensaje solo avanza.
- Las tablas son tenant-owned con FORCE RLS y FKs compuestas (ADR-001 §3); solo los comandos de `apps.conversations` las escriben (contrato de import-linter).

## Work items

Cada work item es un issue con el alcance completo. Este documento solo los ordena; **el issue es la fuente de verdad**.

| ID | Issue | Rama | Depende de | Tipo |
|---|---|---|---|---|
| F6-01 | [#536](https://github.com/PapiCuche/crm-gooddoggy/issues/536) Inbox: tablas de canales, conversaciones y mensajes (Sandbox) | `feature/f6-inbox-tables` | — | backend |
| F6-01b | [#543](https://github.com/PapiCuche/crm-gooddoggy/issues/543) Inbox: mensajes y estado de entrega (sin comandos) | `feature/f6-inbox-messages` | F6-01 | backend |
| F6-01c | [#544](https://github.com/PapiCuche/crm-gooddoggy/issues/544) Inbox: comandos de conversación y mensajes, con auditoría | `feature/f6-inbox-commands` | F6-01b | backend |
| F6-02 | [#537](https://github.com/PapiCuche/crm-gooddoggy/issues/537) Inbox: API de conversaciones y mensajes con el canal Sandbox | `feature/f6-inbox-api` | F6-01c | backend |
| F6-03 | [#538](https://github.com/PapiCuche/crm-gooddoggy/issues/538) Inbox: pantalla de conversaciones y chat (solo lectura) | `feature/f6-inbox-screen` | #537 | frontend |
| F6-04 | [#539](https://github.com/PapiCuche/crm-gooddoggy/issues/539) Inbox: enviar mensajes salientes con el canal Sandbox | `feature/f6-inbox-send` | #538 | full-stack |
| F6-05 | [#540](https://github.com/PapiCuche/crm-gooddoggy/issues/540) Inbox: notas internas y asignación (tomar y transferir) | `feature/f6-inbox-assign` | #539 | full-stack |
| F6-06 | [#541](https://github.com/PapiCuche/crm-gooddoggy/issues/541) Inbox: panel del cliente y oportunidades desde la conversación | `feature/f6-inbox-customer-panel` | #540 | full-stack |

## Historias

| Historia | Estado |
|---|---|
| E06-01 (lista), E06-02 (chat), E06-03 (estados de entrega) | Tablas en F6-01 y F6-01b; comandos en F6-01c; API en F6-02; pantalla en F6-03 |
| E06-04 (notas internas), E06-06 y E06-07 (tomar y transferir) | Tablas en F6-01 y F6-01b; comandos en F6-01c; rutas y pantalla en F6-05 |
| E09-12 (lead y oportunidad desde el Inbox) | F6-06 |

## Decisiones abiertas

| ID | Pregunta | Recomendación | Bloquea |
|---|---|---|---|
| D-F6-01 | **Significado de PENDING** (D-INB-1). | **Aplicada en F6-01:** en cola, sin tomar por un humano; la conversación nace `PENDING`. Al enviar la primera respuesta humana pasa a `OPEN` (F6-01). | Nada |
| D-F6-02 | **Reapertura** (D-INB-2): RESOLVED + 72 h reabre; CLOSED crea una nueva. | **Aplicada en F6-01:** un entrante en `RESOLVED` hasta 72 h después de `resolved_at` la reabre (`OPEN`, misma conversación); pasado el plazo la cierra y abre otra; en `WAITING_CUSTOMER` pasa a `OPEN`. Falta el enlace a la conversación anterior (sin columna todavía). | Nada |
| D-F6-03 | **Cierre automático** (D-INB-3): RESOLVED → CLOSED a los 7 días. | Sin tarea programada todavía; la fase 6 no tiene Celery beat de negocio. A decidir con la pantalla (F6-05). | Nada |
| D-F6-04 | **Reparto automático** (D-CH-5): round robin por equipo. | Fuera de la fase 6 en este corte: `reason_code` ya admite `ROUND_ROBIN` en el historial. | F6-05 y siguientes |
| D-F6-05 | **Quién recibe permisos de Inbox en las plantillas** (03 §H solo dice `conversations.view` ORG, ORG, TEAM, OWN + sin asignar y `messages.send` ✅, ✅, ✅, OWN). | Administrador: los cinco (`channels.manage` figura ⚙️ en la matriz, desactivado por defecto; aquí activado por indicación del coordinador: a confirmar); Supervisor: `view`, `send`, `note` con `TEAM` y `assign`; Vendedor: los tres con `OWN` (más lo sin asignar), sin `assign` ni `channels.manage`. Tomar una conversación (`conversations.take`) llega con F6-05. | Que el Vendedor pueda tomar |

## Observaciones

- **F6-01:** nueva app `apps.conversations` (fila de `deals` en el contrato de capas: sobre `contacts`, sin importarse con `deals`). **División (ADR-009):** tablas, permisos, comandos y sus tests suman unas 2.000 líneas relevantes, así que el issue se entrega en tres cortes apilados: F6-01 (esta: cuentas de canal, conversaciones, historial de asignaciones, permisos, plantillas y alcance), F6-01b (mensajes, con el estado de entrega que solo avanza) y F6-01c (comandos y auditoría). **Tablas** (migración `0001`, FORCE RLS, FKs compuestas, concesiones mínimas): `channel_accounts` (plataforma `SANDBOX` como enum en código para que lleguen WhatsApp y los demás; `external_account_id` único entre las vivas **de toda la plataforma**, no por organización, como pide §E.6: el webhook resolverá la organización con él), `conversations` (estado, `attention_mode` `HUMAN` por omisión, equipo y persona a la vez con FKs compuestas, `assignee_kind` generado, `assignment_version`, `last_inbound_at`, número `CONV-00191` por organización, `resolved_at` y `closed_at` con un `CHECK` que ata `closed_at` al estado CLOSED; a lo más una no cerrada por contacto y cuenta) y `conversation_assignments` ([INM], `crm_app` sin `UPDATE`, `DELETE` ni `TRUNCATE`; actor: una persona o el sistema, exactamente uno). **No se añaden** `assigned_ai_agent_id`, `handoff_*`, `contact_identity_id` ni los `CHECK` que necesitan un agente (Fase 8; ADR-007; `attention_mode` admite los valores de IA en el enum, sin sus invariantes), ni `platform` denormalizado en la conversación, ni `priority`, `snoozed_until`, `is_archived` o los campos de SLA (los pedirá una pantalla). **Permisos:** `conversations.view`, `messages.send` y `messages.note` con alcance, `conversations.assign` y `channels.manage` sin él (como `contacts.assign`: lo que se alcanza lo decide `view`); ninguno es sensible. El Owner los recibe al migrar con `ORGANIZATION` (ADR-018, sin migración propia). **En organizaciones creadas antes de F6-01** (también `pruebas`) solo el Owner los tiene hasta que se concedan a un rol en «Roles» (`PUT …/roles/{id}/permissions/{code}/` con `scope`). **Alcance** (`apps.conversations.scopes.Visible`, 03 §H «OWN + sin asignar»): `OWN` (y `BRANCH`, que la conversación aún no tiene) ve las asignadas a quien consulta y las que no tienen equipo ni persona; `TEAM` suma las del equipo de quien consulta (`ectx.team_ids`, también con pertenencia inactiva, como siempre); `ORGANIZATION`, todas. El listado y la comprobación por objeto salen de las mismas condiciones y un test lo comprueba fila a fila. Poner a un Supervisor de plantilla en un equipo exige cubrir esos permisos en ese equipo (D-F2-13). `core.sequences.allocate` gana el ancho (`CONV` usa 5 cifras).

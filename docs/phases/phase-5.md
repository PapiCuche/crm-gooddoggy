# Fase 5 — Ventas (pipeline, oportunidades, cotizaciones y pedidos)

- **Estado:** en curso desde el 2026-10-30 (F5-01).
- **Objetivo:** el flujo comercial sobre el cliente de la Fase 4: pipelines con etapas configurables, oportunidades (tablero y tabla), motivos de pérdida y, después, cotizaciones, ventas y reservas de stock, cada pieza con su pantalla en el producto oficial.
- **Origen:** [roadmap §S.1](../fase-0/05-backlog-y-roadmap.md) (fila "Fase 5"), historias E09 del [backlog §R](../fase-0/05-backlog-y-roadmap.md), [modelo de datos §E.6](../fase-0/02-modelo-de-datos.md) y el catálogo de permisos de [03 §H](../fase-0/03-tenancy-rbac-inbox-ia.md) (`opportunities.*`, `pipelines.manage`).

Los issues llevan la etiqueta `phase:f5`.

## Principios

- Un pipeline es configuración de la organización; una oportunidad apunta a una etapa. Toda organización, nueva o existente, tiene desde el primer día un pipeline minorista predeterminado con una etapa WON y al menos una LOST (D-COM-7).
- Invariantes de un pipeline (02 §E.6): exactamente una etapa WON, al menos una LOST y al menos una OPEN, activas; un solo pipeline predeterminado, vivo y activo. Las valida el servicio en cada comando y la base respalda lo que puede.
- Nada se borra: un pipeline y un motivo de pérdida tienen borrado lógico, una etapa se desactiva, y `crm_app` no tiene `DELETE` ni `TRUNCATE` sobre sus tablas.
- Las tablas son tenant-owned con FORCE RLS y FKs compuestas (ADR-001 §3); `opportunities` (F5-03) referenciará etapas y motivos con las suyas.

## Work items

Cada work item es un issue con el alcance completo. Este documento solo los ordena; **el issue es la fuente de verdad**.

| ID | Issue | Rama | Depende de | Tipo |
|---|---|---|---|---|
| F5-01 | [#476](https://github.com/PapiCuche/crm-gooddoggy/issues/476) Pipeline: tablas y semilla del pipeline por defecto (sin API) | `feature/f5-pipelines-table` | — | backend |
| F5-01b | _(issue por crear)_ Pipeline: comandos de motivos de pérdida (sin API) | `feature/f5-lost-reason-commands` | F5-01 | backend |
| F5-01c | _(issue por crear)_ Pipeline: comandos de pipelines y etapas (sin API) | `feature/f5-pipeline-commands` | F5-01b | backend |

Siguen, en este orden: permisos `pipelines.manage` y API (F5-02), oportunidades (F5-03 y F5-04), pantallas (F5-05 en adelante).

## Historias

| Historia | Estado |
|---|---|
| E09-05 Configurar pipelines y etapas | Tablas y semilla en F5-01; comandos de motivos de pérdida en F5-01b; de pipelines y etapas en F5-01c; API en F5-02 |
| E09-01 a E09-04, E09-06 en adelante | Pendientes |

## Decisiones abiertas

| ID | Pregunta | Recomendación | Bloquea |
|---|---|---|---|
| D-F5-01 | **Desactivar un pipeline o una etapa con oportunidades abiertas** (02 §E.6: «no se desactiva una etapa con oportunidades abiertas sin migrarlas»). `opportunities` aún no existe. | Cuando exista (F5-03), `update_pipeline(is_active=False)` y `update_stage(is_active=False)` lanzan `OpenOpportunities` si hay oportunidades OPEN en el pipeline o la etapa, y un comando propio las mueve antes. El gancho ya está en los comandos (`_no_open_opportunities`); su test se escribe con F5-03, junto con la tabla, porque hoy no hay nada que comprobar. | Nada hoy |
| D-F5-02 | **`lost_reasons.requires_note`** (02 §E.6, §34: «+ nota si `requires_note`»). Nada lo lee hasta marcar una oportunidad como perdida. | Añadirlo con F5-04 (marcar perdida), con su migración. | Nada hoy |
| D-F5-03 | **Moneda del pipeline.** La columna admite cualquier ISO 4217, los comandos solo `PEN` (como las listas de precios, D-PRC-2). | Abrirla cuando haya otra moneda en el MVP. | Nada hoy |

## Observaciones

- **F5-01:** fundación de la serie (AGENTS.md §11), sin API: la cierra el tablero de oportunidades (F5-05). **División (ADR-009):** tablas, semilla, comandos de pipelines y etapas y de motivos, con sus tests, sumaban unas 1.500 líneas relevantes; esta entrega son las tablas y la semilla, los comandos de motivos van en F5-01b y los de pipelines y etapas en F5-01c. **Tablas** `pipelines`, `pipeline_stages` y `lost_reasons` (§E.6) con FORCE RLS, FK de tenant y FKs compuestas (quien crea, cambia o borra es miembro de la organización; la etapa es de un pipeline de la misma organización). Código único entre los vivos (`retail`; los motivos, `OUT_OF_STOCK`); un solo predeterminado vivo y activo (índice parcial); una sola etapa WON por pipeline, incluso desactivada (índice parcial: la WON no se desactiva ni cambia de tipo); WON es 100 % y LOST 0 %; `requires_fields` es un objeto JSON, solo se guarda. **Columnas de §E.6 que aún no entran** porque nada las usa: `lost_reasons.requires_note` (D-F5-02) y todo lo de `opportunities`. **Semilla:** `deals.seed.ensure_default_pipeline(ctx)` crea el pipeline «Ventas minoristas» (`retail`, PEN, predeterminado) con Nuevo 10 %, Contactado 20 %, Cotizado 40 % (estancada a los 7 días), Negociación 60 % (5 días), Ganado (WON) y Perdido (LOST), y los motivos Precio, Sin stock, Compró en otro lugar, Sin respuesta, Ya no lo necesita y Otro (códigos de 02 §E.6). Es idempotente y no pisa lo que la organización cambió: siembra el pipeline solo si no tiene ninguno vivo y los motivos solo si nunca tuvo ninguno (ni siquiera borrado); un bloqueo consultivo por organización (el mismo de los comandos de F5-01c) evita dos semillas a la vez. **Organizaciones nuevas:** `provisioning` está por debajo de los módulos de negocio y no puede importarlos, así que `apps.provisioning.seeds` es un registro: `DealsConfig.ready` registra la semilla y el alta (`bootstrap_organization`) la ejecuta en su transacción. **Organizaciones existentes:** la migración de datos `deals.0002` recorre `organizations` y llama a la misma `seed_organization` con los modelos históricos; corre como `crm_migrator`, pero fija `app.tenant_id` de cada organización, así que no depende de `BYPASSRLS` (que el runtime sigue sin tener). No audita (no hay actor; el alta de la organización ya lo hace) y al revertir no borra nada. Un test la ejecuta de verdad (`migrate` hacia atrás y adelante con el migrador).

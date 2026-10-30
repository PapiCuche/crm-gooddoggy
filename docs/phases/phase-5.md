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
| F5-01b | [#479](https://github.com/PapiCuche/crm-gooddoggy/issues/479) Pipeline: comandos de motivos de pérdida (sin API) | `feature/f5-lost-reason-commands` | #476 | backend |
| F5-01c | [#480](https://github.com/PapiCuche/crm-gooddoggy/issues/480) Pipeline: comandos de pipelines y etapas (sin API) | `feature/f5-pipeline-commands` | #479 | backend |

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

- **F5-01:** fundación de la serie (AGENTS.md §11), sin API: la cierra el tablero de oportunidades (F5-05). **División (ADR-009):** tablas, semilla, comandos de pipelines y etapas y de motivos, con sus tests, sumaban unas 1.500 líneas relevantes; esta entrega son las tablas y la semilla, los comandos de motivos van en F5-01b y los de pipelines y etapas en F5-01c. **Tablas** `pipelines`, `pipeline_stages` y `lost_reasons` (§E.6) con FORCE RLS, FK de tenant y FKs compuestas (quien crea, cambia o borra es miembro de la organización; la etapa es de un pipeline de la misma organización). Código único entre los vivos (`retail`; los motivos, `OUT_OF_STOCK`); un solo predeterminado vivo y activo (índice parcial); una sola etapa WON por pipeline, incluso desactivada (índice parcial: la WON no se desactiva ni cambia de tipo); WON es 100 % y LOST 0 %; `requires_fields` es un objeto JSON, solo se guarda. **Columnas de §E.6 que aún no entran** porque nada las usa: `lost_reasons.requires_note` (D-F5-02) y todo lo de `opportunities`. **Semilla:** `deals.seed.ensure_default_pipeline(ctx)` crea el pipeline «Ventas minoristas» (`retail`, PEN, predeterminado) con Nuevo 10 %, Contactado 20 %, Cotizado 40 % (estancada a los 7 días), Negociación 60 % (5 días), Ganado (WON) y Perdido (LOST), y los motivos Precio, Sin stock, Compró en otro lugar, Sin respuesta, Ya no lo necesita y Otro (códigos de 02 §E.6). Es idempotente y no pisa lo que la organización cambió: siembra el pipeline solo si no tiene ninguno vivo y los motivos solo si nunca tuvo ninguno (ni siquiera borrado); un bloqueo consultivo por organización (el mismo de los comandos de F5-01c) evita dos semillas a la vez. **Organizaciones nuevas:** `provisioning` está por debajo de los módulos de negocio y no puede importarlos, así que `apps.provisioning.seeds` es un registro: `DealsConfig.ready` registra la semilla y el alta (`bootstrap_organization`) la ejecuta en su transacción. **Organizaciones existentes:** la migración de datos `deals.0002` recorre `organizations` y lleva su propia copia congelada de los datos y de las columnas de la `0001` y usa los modelos históricos (no importa `deals.seed`: un cambio posterior de tablas o de semilla no debe romper `migrate` en una base nueva); depende de `core.0001` y de `organizations.0010`, como las demás migraciones con RLS y FKs de tenant. Corre como `crm_migrator`, pero fija `app.tenant_id` de cada organización, así que no depende de `BYPASSRLS` (que el runtime sigue sin tener). No toma el bloqueo consultivo por organización (uno por organización en una transacción agotaría la memoria de bloqueos con decenas de miles; los índices únicos protegen la carrera). No audita (no hay actor; el alta de la organización ya lo hace) y al revertir no borra nada. **Estado conocido:** una organización con un pipeline vivo que no es el predeterminado no recibe uno por la semilla ni por la migración; solo lo evita el servicio de F5-01c, que marca el primer pipeline como predeterminado. Un test la ejecuta de verdad (`migrate` hacia atrás y adelante con el migrador).
- **F5-01b:** `create_lost_reason` y `update_lost_reason` en `apps.deals.commands`, con el contrato de import-linter (solo los importará `apps.deals.api`, F5-02; el paquete `api` existe vacío para que el contrato lo nombre). El código va en mayúsculas y guion bajo (`OUT_OF_STOCK`) y no cambia; sin `sort_order`, el motivo nuevo va al final. `update_lost_reason` exige la `version` leída (`StaleVersion`, el 409 de dominio) y no escribe ni audita sin cambios. Auditoría: `lost_reason.created` y `lost_reason.updated` con el antes y el después. Sin permisos: los declarará F5-02 (`pipelines.manage`).
- **F5-01c:** `create_pipeline`, `update_pipeline`, `add_stage`, `update_stage` y `reorder_stages` en `apps.deals.commands`, con la misma auditoría (`pipeline.created`, `pipeline.updated`, `stage.added`, `stage.updated`, `stage.reordered`; sin datos personales) y sin escribir ni auditar si nada cambia. **Invariantes tras cada comando:** una etapa WON (activa, que no se desactiva ni cambia de tipo), al menos una LOST y una OPEN activas; la probabilidad de WON es 100 y la de LOST, 0, y cambiar el tipo de una etapa pide la que le corresponde. `create_pipeline` crea el pipeline y sus etapas en una transacción, numeradas en el orden recibido; el código y la moneda no cambian. **Predeterminado:** el primer pipeline de la organización lo es sin decirlo; `is_default=True` (al crear o en `update_pipeline`) quita la marca al anterior en la misma transacción y lo audita; no se desactiva ni se quita la marca al predeterminado sin elegir otro (`PipelineIsDefault`). `update_pipeline` exige la `version` (`StaleVersion`, el 409 de dominio). **Concurrencia:** crear o cambiar pipelines toma el bloqueo consultivo de la organización (el de la semilla), así que dos «predeterminado» a la vez dejan uno; las etapas bloquean la fila del pipeline (`FOR NO KEY UPDATE`), que serializa cualquier cambio de sus etapas (dos desactivaciones a la vez no dejan el pipeline sin OPEN). `add_stage` coloca una OPEN tras la última OPEN y otra al final (o en `position`) y renumera; `reorder_stages` recibe todas las etapas, también las inactivas, y no escribe si ya están en ese orden. **Desactivar con oportunidades abiertas:** el gancho `_no_open_opportunities` está llamado donde se desactiva un pipeline o una etapa y hoy no comprueba nada (D-F5-01); su test llega con F5-03, no como un test fallido o saltado.

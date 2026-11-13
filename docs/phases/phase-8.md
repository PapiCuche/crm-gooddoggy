# Fase 8 — Infraestructura de IA y tools de lectura: el copiloto del Inbox

- **Estado:** diseño propuesto el 2026-11-13 (F8-01). [ADR-034](../adr/ADR-034-ai-copilot-first.md) está `Proposed`: hasta que el propietario lo acepte, lo cambie o lo rechace (F8-02, #765), este documento es un plan y no un compromiso. Ningún work item con código empieza antes.
- **Objetivo (release R2):** que quien atiende el Inbox pida a Claude una **sugerencia de respuesta** o un **resumen** de la conversación, y decida qué hacer con ella: insertarla en el compositor, editarla o descartarla. **Nada se envía solo.**
- **Origen:** [ADR-005](../adr/ADR-005-ai-provider-abstraction.md) (Accepted), que ADR-034 acota; historias E08 y E11-07, E11-12 y E11-13 del [backlog §R](../fase-0/05-backlog-y-roadmap.md) y la estrategia de despliegue de la IA ([05 §S.4](../fase-0/05-backlog-y-roadmap.md)); [03 §J](../fase-0/03-tenancy-rbac-inbox-ia.md); ADR-006, ADR-010, ADR-011, ADR-022 y ADR-029.

Los issues llevan la etiqueta `phase:f8` y `release:R2`. **El issue es la fuente de verdad** de cada work item.

## Principios

- **Una persona decide siempre.** No existe ningún camino de la IA hacia `record_outbound` ni hacia el outbox de mensajes; lo comprueba import-linter.
- **Solo lectura.** Las tools leen catálogo, precio publicado y stock con los servicios que ya existen; ninguna escribe.
- **Ninguna cifra la escribe el modelo.** Precio, stock y promoción salen de una tool y los copia el sistema; el Output Guard marca lo que no tenga evidencia.
- **Lo mínimo sale fuera.** Sin notas internas, costos, secretos, campos del contacto ni datos de otros clientes; el texto del cliente es dato no confiable.
- **Cuesta dinero de la organización.** Presupuesto diario y mensual con corte duro, registro de uso y kill switch desde el primer corte.
- **Simulación primero.** Cada proveedor tiene un simulador sin red; CI nunca llama a un proveedor real.
- **Cada work item cabe en 800 líneas relevantes** (ADR-009); si no cabe, se divide antes de empezar.

## Estado de partida

No hay nada de IA en el código: ni `apps.ai_gateway`, ni `apps.ai_agents`, ni tablas `ai_*`, ni permisos `ai.*`. Lo que el copiloto usa ya existe: `core.sealed` (ADR-022), el segundo factor reciente (`core.api.step_up`), `core.http` con allowlist, `PricingService` con la audiencia `PUBLIC_AI`, los selectores del catálogo y del inventario, y el Inbox con su compositor. Detalle en ADR-034, «Lo que existe hoy en el código».

## Work items

| ID    | Título                                                                                                                               | Rama                                        | Depende de          | Tipo               |
| ----- | ------------------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------- | ------------------- | ------------------ |
| F8-01 | [#764](https://github.com/PapiCuche/crm-gooddoggy/issues/764) ADR-034 copiloto con aprobación humana                                 | `docs/f8-adr-034-copilot`                   | —                   | documentación      |
| F8-02 | [#765](https://github.com/PapiCuche/crm-gooddoggy/issues/765) Decisión del propietario: aceptar ADR-034                              | — (`docs/f8-adr-034-accepted` si se acepta) | F8-01               | decisión           |
| F8-03 | [#766](https://github.com/PapiCuche/crm-gooddoggy/issues/766) Claves de proveedor: tabla sellada, API protegida y prueba de conexión | `feature/f8-ai-provider-keys`               | F8-02               | backend            |
| F8-04 | [#767](https://github.com/PapiCuche/crm-gooddoggy/issues/767) Pantalla de claves y prueba de conexión                                | `feature/f8-ai-provider-keys-screen`        | F8-03               | frontend           |
| F8-05 | [#768](https://github.com/PapiCuche/crm-gooddoggy/issues/768) `ai_gateway` con Anthropic: presupuesto, corte duro y registro de uso  | `feature/f8-ai-gateway-anthropic`           | F8-03               | backend            |
| F8-06 | [#769](https://github.com/PapiCuche/crm-gooddoggy/issues/769) Kill switch global y por organización                                  | `feature/f8-ai-kill-switch`                 | F8-05               | backend y frontend |
| F8-07 | [#770](https://github.com/PapiCuche/crm-gooddoggy/issues/770) Tools de solo lectura: catálogo, precio publicado y stock              | `feature/f8-ai-read-tools`                  | F8-02               | backend            |
| F8-08 | [#771](https://github.com/PapiCuche/crm-gooddoggy/issues/771) API del copiloto: sugerir y resumir                                    | `feature/f8-copilot-suggest-api`            | F8-05, F8-06, F8-07 | backend            |
| F8-09 | [#772](https://github.com/PapiCuche/crm-gooddoggy/issues/772) Panel del copiloto en el Inbox (**cierra la serie**)                   | `feature/f8-copilot-panel`                  | F8-08               | frontend           |
| F8-10 | [#773](https://github.com/PapiCuche/crm-gooddoggy/issues/773) OpenAI como segundo proveedor                                          | `feature/f8-ai-openai-adapter`              | F8-05, F8-09        | backend            |

Cómo reparte ADR-034 entre ellos:

- **F8-03:** `ai_provider_accounts` y su tabla de claves (RLS con FORCE), propósito `ai.provider_key`, permiso `ai.manage` (sensible) con segundo factor reciente, auditoría sin la clave, prueba de conexión que solo devuelve un código, re-cifrado tras rotar la KEK y simulador (ADR-034 §4).
- **F8-04:** la pantalla de F8-03; la clave nunca en el estado de React, el DOM, el almacenamiento, la URL ni la consola.
- **F8-05:** `apps.ai_gateway` (L1) y sus contratos de import-linter; adaptador de Anthropic por `core.http` (tools con `tool_choice: auto` y la respuesta con salida estructurada); errores normalizados; lista permitida de modelos con su tabla de precios completa (caché, tramos por longitud del prompt) y fecha; presupuesto **diario y mensual** en la zona horaria de la organización, con reserva que es una cota superior, filas bloqueadas en orden mes → día y lo dudoso contado como gastado; `ai_llm_calls` sin prompt ni respuesta, particionada por mes (ADR-034 §3, §5). **#764 y #768 hablan de presupuesto por mes; ADR-034 añade el tope diario: si el propietario lo acepta en F8-02, #768 se ajusta a diario y mensual antes de pasar a `status:ready`.** Si el tope diario no cabe en 800 líneas, va en un corte propio antes de F8-08.
- **F8-06:** `AI_ENABLED` (entorno, apagado por omisión, respaldo con reinicio) y el interruptor por organización (apagado por omisión, auditado): **apagar** con `ai.kill_switch` o `ai.manage` y sin segundo factor; **encender** con `ai.manage`, segundo factor reciente y la confirmación del aviso de datos. El permiso nuevo `ai.kill_switch` va al Owner y a las plantillas Administrador y Supervisor (ADR-034 §5, §10).
- **F8-07:** Tool Registry en `apps.ai_agents` (L4) que solo admite `READ` y `PUBLIC_SAFE`; `search_product`, `get_product_variants`, `get_price` (`PricingService` con `PUBLIC_AI` **y** la lista dentro de las `visible_price_lists` de la persona) y `get_stock` (disponible total, sin almacenes); evidencia en `ai_actions`; la prueba que captura las consultas y falla ante una escritura fuera de las tablas `ai_*` (ADR-034 §2, §6).
- **F8-08:** las rutas `…/copilot/suggest/` y `…/copilot/summary/` (`conversations.view` y `ai.use`, alcance de la conversación); contexto minimizado con el texto del cliente no confiable y el enmascarado; Output Guard asistido con detectores de cifras, descuentos, stock, términos comerciales y enlaces, y un reintento con feedback; detección de traspaso; `ai_runs` y `ai_suggestions`; la ruta que audita cada inserción (`ai.suggestion.inserted`); nada en el outbox; la prueba de consultas de una invocación completa (ADR-034 §2, §7, §8, §9).
- **F8-09:** el panel: insertar, editar o descartar; al insertar, cada tramo sin evidencia entra como `[verificar]` y el compositor no envía mientras quede una; aviso de traspaso; el resumen con la etiqueta «Generado por IA» y sus marcas «sin verificar»; ningún botón que envíe la sugerencia.
- **F8-10:** el adaptador de OpenAI con las mismas pruebas que el de Anthropic; elección de proveedor por organización, sin fallback automático.

### F8-01 · ADR-034

- **Hecho en F8-01 (#764):** [ADR-034](../adr/ADR-034-ai-copilot-first.md) `Proposed` (alcance del primer corte, módulos y contratos, proveedor y modelo, clave sellada, presupuesto y registro de uso, kill switch, tools de solo lectura, datos que salen y los que no, traspaso, panel y Output Guard asistido, permisos, acciones prohibidas, autonomía futura, decisiones del propietario y la tabla de lo que cambia respecto de ADR-005), su fila en el índice y este documento. Las afirmaciones sobre la API de Anthropic (ids de modelos, errores y retención) se comprobaron en la documentación oficial el 2026-11-13; los precios no se fijan en el ADR. Sin código.
- **Revisión de #785 (F8-01):** el ADR se corrigió antes de entregarlo: `get_price` exige también las listas que ve la persona; la reserva del presupuesto es una cota superior y lo dudoso cuenta como gastado; apagar la IA no pide segundo factor (`ai.kill_switch`); lo que no tiene evidencia entra en el compositor como `[verificar]` y cada inserción se audita; detector de enlaces; se citan los contratos `protected` que ya existen y lo que import-linter no ve.

## Qué necesita el propietario (sin ningún valor real)

| #   | Qué                                                                                     | Dónde se guarda                                                                                   | Cuándo se necesita                  |
| --- | --------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------- | ----------------------------------- |
| 1   | Aceptar, cambiar o rechazar ADR-034 y responder sus decisiones                          | En el ADR o en un comentario de #765                                                              | Antes de F8-03                      |
| 2   | Cuenta de **Anthropic** (Claude Console) de la organización, con método de pago y saldo | Anthropic. No se guarda en el CRM                                                                 | Antes de usarlo en vivo             |
| 3   | **Clave de API** de esa cuenta                                                          | Solo en la pantalla del CRM (F8-04), cifrada; nunca en el repositorio, un issue, un PR ni un chat | Con F8-04, contra el proveedor real |
| 4   | **Límite de gasto** en la consola del proveedor (segunda barrera, D-F8-09)              | Anthropic                                                                                         | Antes de usarlo con clientes        |
| 5   | **Aviso de tratamiento de datos** para los clientes (D-F8-04)                           | La política del negocio                                                                           | Antes de encender la IA             |
| 6   | Qué listas de precios puede citar la IA (`ai_exposable`, D-F8-06)                       | La configuración de precios del CRM                                                               | Para que haya precios               |
| 7   | Cuenta de **OpenAI** y su clave, si se usa (D-F8-02)                                    | Igual que las filas 2 y 3                                                                         | F8-10                               |

## Decisiones abiertas para el propietario

Las preguntas y las recomendaciones están en [ADR-034 §13](../adr/ADR-034-ai-copilot-first.md#13-decisiones-del-propietario): D-F8-01 presupuesto mensual y diario; D-F8-02 proveedores; D-F8-03 modelos de la lista permitida; D-F8-04 aviso de tratamiento de datos; D-F8-05 retención de los registros de IA y de los borradores; D-F8-06 listas que puede citar la IA; D-F8-07 stock como número o franja; D-F8-08 roles con `ai.use`; D-F8-09 límite de gasto en el proveedor. No se programa nada que dependa de una de ellas antes de la respuesta.

## Después de esta fase

La autonomía (respuestas automáticas a preguntas frecuentes con tools de solo lectura) necesita un ADR propio, una fase en sombra y pruebas controladas con conversaciones reales (ADR-034 §12). Las tools que escriben llegan como pronto en la Fase 10.

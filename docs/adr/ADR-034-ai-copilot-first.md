# ADR-034: Primer corte de IA: copiloto del Inbox con aprobación humana

- **Status:** Proposed
- **Date:** 2026-11-13
- **Deciders:** propuesto por Claude Code (Opus 5.5) dentro del programa autónomo (ADR-015) para la decisión del propietario. Decide qué datos de clientes salen a un proveedor externo y cuánto se gasta: no lo acepta el programa. La aceptación es el work item F8-02 (#765), que solo cierra el propietario. Mientras esté `Proposed` no obliga al código: F8-03 a F8-10 (#766 a #773) están bloqueados hasta esa decisión.
- **Related:** [ADR-005](ADR-005-ai-provider-abstraction.md) (Accepted; este ADR lo acota), [ADR-006](ADR-006-pricing-engine.md), [ADR-010](ADR-010-messaging-policy.md), [ADR-011](ADR-011-observability-and-logs.md), [ADR-022](ADR-022-secrets-at-rest.md), [ADR-023](ADR-023-mfa-totp.md) §7, [ADR-028](ADR-028-store-data-connectors.md), [ADR-029](ADR-029-whatsapp-cloud-api-channel.md), [module-dependencies.md](../architecture/module-dependencies.md) §1, [security-boundaries.md](../architecture/security-boundaries.md), [03 §I y §J](../fase-0/03-tenancy-rbac-inbox-ia.md), [05 §R (E08, E11) y §S.4](../fase-0/05-backlog-y-roadmap.md), [phase-8.md](../phases/phase-8.md), #764 a #773

## Context

### Lo que ya está decidido y este ADR no cambia

ADR-005 (Accepted) fija la arquitectura de la IA: dos capas (`ai_gateway`, que habla con los proveedores y no conoce el CRM, y `ai_agents`, el runtime que toca el negocio solo a través del Tool Registry), el registro explícito de tools con `ToolContext` inyectado por el runtime, el Output Guard (afirmaciones ligadas a evidencia de tools) y los registros de IA separados (`ai_runs`, `ai_llm_calls`, `ai_actions`). ADR-011 separa esos registros del log de aplicación y de la auditoría. ADR-022 da el único módulo que cifra secretos que la aplicación tiene que leer (`core.sealed`). ADR-006 hace de `PricingService` la única autoridad de precios. [module-dependencies.md](../architecture/module-dependencies.md) §1 coloca `ai_gateway` en L1 y `ai_agents` en L4. El roadmap de la fase 0 ([05 §S.4](../fase-0/05-backlog-y-roadmap.md)) pide empezar por la IA asistida (el humano aprueba cada envío) y dejar la autonomía para después.

### Lo que existe hoy en el código (comprobado el 2026-11-13, `main` en `c193f31a`)

- **No hay nada de IA:** no existen `apps.ai_gateway` ni `apps.ai_agents`, ninguna tabla `ai_*` y ningún permiso `ai.*` en `apps/access/catalog.py`. ADR-005 va por delante del código.
- **Secretos:** `core.sealed` (`seal`, `unseal`, `stale`; propósito `[a-z][a-z0-9_.]{0,31}`, dueño de hasta 200 caracteres ASCII). Lo usan el TOTP (`mfa.totp`) y el token de canal (`channel.access_token`, `apps/conversations/credentials.py`, dueño `channel_account:<id>`, con re-cifrado por tarea). La tabla `credentials` es de `apps.conversations` (su único escritor) y su CHECK solo admite `owner_type = 'CHANNEL_ACCOUNT'`.
- **Segundo factor reciente:** `core.api.step_up` (15 minutos) y `step_up_methods` en las vistas; las rutas de la cuenta de canal lo piden con `channels.manage` (D-F7-11).
- **HTTP saliente:** `core.http` (allowlist `HTTP_ALLOWED_HOSTS`, https, IP pública fijada, tope de respuesta).
- **Precios:** `apps.pricing.services.resolve` y `resolve_many` con `Audience.PUBLIC_AI`, que solo ve listas `ai_exposable` (por omisión `False`) y nunca la que pide `prices.view_internal`, y solo variantes y productos `ACTIVE` con `ai_visible`. `PriceResolution` no tiene campos de costo; los costos son de `apps.pricing.costs` (`product_cost.view`).
- **Catálogo y stock:** `apps.catalog.selectors.live_variants` y `apps.inventory.selectors.levels` (`available = on_hand - reserved`, columna generada). El stock externo de ADR-028 todavía no existe.
- **Inbox:** `apps.conversations.selectors.messages(conversation_id, notes=False)` excluye las notas (`direction = 'INTERNAL'`, `message_type = 'NOTE'`); el alcance de `conversations.view` está en `apps.conversations.scopes`; solo `apps.conversations.commands.record_outbound` pone en cola un mensaje al cliente. `Conversation.attention_mode` ya tiene `AI_ASSISTED` y `AI_AUTONOMOUS`, sin invariantes. En el frontend, `frontend/src/components/inbox/composer.tsx` es el compositor y `chat-panel.tsx` y `customer-panel.tsx` son los paneles del chat.
- **Auditoría:** `audit_logs` particionada por mes (`audit_ensure_partitions`), como pide ADR-011 para las tablas de IA.

### Por qué hace falta un ADR nuevo

ADR-005 describe el sistema completo (agentes públicos, routing, fallback, catálogo de modelos sincronizado, varios proveedores desde el inicio, circuit breaker). Construirlo entero antes de dar valor contradice la regla de slice vertical (AGENTS.md §11) y abre a la vez todas las superficies de riesgo. El propietario pidió un primer corte: Claude sugiere y resume en el Inbox, una persona decide, y nada se envía solo. Este ADR **acota y ordena** ADR-005 para ese corte; no lo contradice. Donde el corte hace menos de lo que ADR-005 describe, o lo hace más estricto, lo dice en «Qué cambia respecto de ADR-005».

## Decision

### 1. Alcance del primer corte

**Dentro:**

- **Sugerir respuesta** a la última petición del cliente en una conversación del Inbox.
- **Resumir** una conversación para quien la atiende.
- **Tools de solo lectura**: catálogo, variantes, precio publicado y stock disponible (§6).
- **Detección de traspaso**: el cliente pide hablar con una persona o está molesto (§8).
- **Panel del copiloto** en el Inbox: insertar en el compositor, editar o descartar (§9).
- Infraestructura mínima para eso: claves por organización, gateway con Anthropic, presupuesto con corte duro, registro de uso y kill switch.

**Fuera** (cada cosa necesita su propio work item y, si cambia una decisión, su ADR):

- Envío automático y cualquier agente de cara al cliente (§12).
- Tools que escriben, y cualquier acción del §11.
- Memoria larga, perfiles de contacto, base de conocimiento (Fase 9).
- Routing, fallback entre cuentas o proveedores, sincronización del catálogo de modelos y circuit breaker.
- Streaming de respuestas, imágenes, audio y documentos como entrada del modelo.

### 2. Módulos y fronteras

- **`apps.ai_gateway` (L1)**: interfaz de proveedor (`LLMProviderAdapter` de ADR-005 Parte B, sin `stream()` ni `list_models()` en este corte), adaptadores, cuentas de proveedor y su clave sellada, presupuestos, registro de llamadas (`ai_llm_calls`) y kill switch. No importa ningún módulo de negocio (L3) ni `ai_agents`. Toda llamada a un modelo pasa por aquí.
- **`apps.ai_agents` (L4)**: el copiloto (un agente interno definido en código, sin tablas de agentes configurables), el Tool Registry, el constructor de contexto y el Output Guard; `ai_runs`, `ai_actions` y los borradores (`ai_suggestions`). Lee el negocio solo con selectores y servicios ya existentes (§6, §7).
- **Contratos de import-linter** que nacen con estos módulos: ningún módulo de L0 a L3 importa `apps.ai_*`; `apps.ai_*` **no** importa `apps.conversations.commands` (el único que pone en cola un mensaje al cliente), los comandos de precios, de inventario (`stock`, `transfers`, `reservations`) ni de cotizaciones. Así «la IA no envía ni escribe por esos caminos» es una propiedad que comprueba CI, no una promesa del prompt; SQL directo o un módulo nuevo que escriba quedan para la revisión de cada PR.

### 3. Proveedor, cuenta y modelo

- **Anthropic primero**, con la **Messages API oficial** (`POST /v1/messages` en `api.anthropic.com`, que se añade a `HTTP_ALLOWED_HOSTS`), con la **clave de API de la propia organización** del CRM, facturada a la cuenta de Anthropic (Claude Console) que contrate el propietario. **Nunca** las credenciales de una sesión de Claude Code, de una suscripción personal, de un perfil OAuth ni la clave de un desarrollador: el runtime no lee `ANTHROPIC_API_KEY` ni otra variable del entorno para esto, solo la clave sellada de la organización.
- **OpenAI segundo** (F8-10), detrás de la misma interfaz y con las mismas garantías. **Sin fallback automático** entre proveedores ni entre modelos en este corte.
- **El adaptador llama por `core.http`** (allowlist, IP fijada, tiempo límite) y no con el SDK oficial, que abre sus propias conexiones fuera de esa frontera. El formato de petición, respuesta, uso y errores sigue la referencia oficial de la API.
- **Errores normalizados** (según la [página oficial de errores](https://platform.claude.com/docs/en/api/errors), consultada el 2026-11-13): 401 y 403 → `CREDENTIALS_REJECTED`; 402 → `PROVIDER_BILLING`; 429, 500, 504 y 529 → `PROVIDER_UNAVAILABLE` con reintentos acotados (como mucho 2, respetando `retry-after`, dentro de un plazo total); 400 y 413 → error interno (es un fallo nuestro: no se reintenta). Un 400 por el límite de gasto que el propietario fije en la consola del proveedor también corta. Ninguna respuesta del CRM lleva el cuerpo del proveedor.
- **Un modelo por cuenta.** La organización elige el modelo de su cuenta de proveedor entre una **lista permitida en configuración** (revisada por PR), con su precio por token y la fecha desde la que rige. Ningún id de modelo está fijado en la lógica (ADR-005 Parte B). Un modelo sin precio en la tabla no se llama (falla cerrado). Los ids vigentes según la [página oficial de modelos](https://platform.claude.com/docs/en/about-claude/models/overview) el 2026-11-13 son `claude-opus-5-5`, `claude-sonnet-5-5` y `claude-haiku-5-5`, entre otros; cuál entra en la lista es una decisión del propietario (§13). Los precios **no** se fijan en este ADR: se copian de la [página oficial de precios](https://platform.claude.com/docs/en/about-claude/pricing) a la tabla de configuración en el work item que la cree, con la fecha de consulta.
- **Retención del proveedor.** La [página oficial de retención de datos de la API](https://platform.claude.com/docs/en/manage-claude/api-and-data-retention) (consultada el 2026-11-13) dice que el contenido no se usa para entrenar sin permiso expreso y que algunos modelos («Covered Models») exigen retención de 30 días. La lista permitida no incluye esos modelos salvo decisión expresa del propietario, que además confirma estas condiciones en los términos de su cuenta.

### 4. La clave del proveedor

- **Una cuenta de proveedor por organización y proveedor** (`ai_provider_accounts`, tenant-owned, RLS con FORCE, una política por comando): proveedor, modelo elegido, estado, `key_last_four`, quién la configuró y cuándo, `rotated_at`, `revoked_at`.
- **La clave se sella con `core.sealed`** bajo un **propósito nuevo**, `ai.provider_key`, con dueño `ai_provider_account:<proveedor>:<id de la fila>` (el proveedor no cambia en la fila: una clave copiada a otra fila u otro proveedor no se abre; ADR-022 §4). Vive en una tabla del propio `ai_gateway`, con el patrón de `credentials` (viva o invalidada; al rotar o borrar, el cifrado se borra), y no en `credentials`, cuyo único escritor es `apps.conversations`. El re-cifrado tras rotar la KEK (ADR-022 §5, paso 2) la incluye desde el primer día.
- **Solo una pantalla protegida la guarda:** crear, rotar, borrar y cambiar el modelo piden el permiso nuevo `ai.manage` (sensible) **y** el segundo factor de hace 15 minutos como mucho (`step_up_methods`, como D-F7-11), en este orden: sin permiso, `PERMISSION_DENIED`, nunca `STEP_UP_REQUIRED`. Con límite de tasa y auditoría (`ai.account.created`, `ai.account.key_rotated`, `ai.account.disabled`, `ai.account.tested`), siempre sin la clave.
- **La clave no sale nunca:** ni en respuestas (solo `key_last_four`, proveedor, modelo, estado y fechas), ni en logs, auditoría, outbox, errores, reporte de errores, el contexto del modelo o el estado del navegador (F8-04: campo de contraseña que se vacía al enviar, como el token de canal). Se abre solo en memoria, justo antes de la llamada.
- **Prueba de conexión sin revelar nada:** la llamada más barata que valide la clave (para Anthropic, listar modelos, que no consume tokens) al host fijo de la allowlist, nunca a una URL que dé el usuario. Devuelve solo `ok` o un código (`CREDENTIALS_REJECTED`, `PROVIDER_BILLING`, `PROVIDER_UNAVAILABLE`), nunca el cuerpo del proveedor.

### 5. Presupuesto, registro de uso y kill switch

- **Presupuesto por organización, diario y mensual**, en USD con `Decimal` (la moneda en que factura el proveedor), con **corte duro**: antes de cada llamada se **reserva** el costo máximo estimado (tokens de entrada calculados más `max_tokens` de salida, al precio del modelo) con una actualización atómica condicionada (`gastado + reservado + estimado <= tope`, en el día y en el mes); si no cabe, `AI_BUDGET_EXHAUSTED` y **no se llama al proveedor**. Al terminar se ajusta al uso real; si la llamada falla sin uso, se libera; una reserva huérfana (proceso caído) cuenta entera hasta que un barrido la cierra. Dos llamadas concurrentes nunca superan el tope. Sin presupuesto configurado no hay llamadas (falla cerrado). Aviso al 80 % (`ai.budget.threshold_reached`) y `ai.budget.exceeded` al cortar. Cambiar los topes es `ai.manage` con segundo factor reciente y auditado (`ai.limits.changed`).
- **Registro de uso (ADR-005 Parte E, ADR-011):** `ai_llm_calls` guarda por llamada la organización, quién la pidió, la conversación, el propósito (`SUGGEST`, `SUMMARY`), proveedor, modelo, tokens de entrada, de salida y de caché, costo calculado, latencia, resultado y el id de petición del proveedor; **nunca el prompt ni la respuesta**. `ai_runs` (una invocación del copiloto, con sus hallazgos del guard) y `ai_actions` (cada tool call con argumentos y resultado redactados: la evidencia) completan el registro. Son de solo inserción, con RLS y particionadas por mes desde su creación, como `audit_logs`. El log de aplicación solo lleva `ai_run_id`.
- **Kill switch en dos niveles:** global `AI_ENABLED` (entorno, **apagado por omisión**; la aplicación no lo puede encender; cambiarlo pide reiniciar los procesos) y por organización (`ai.manage`, segundo factor reciente, auditado `ai.kill_switch.activated` / `ai.kill_switch.deactivated`, **apagado por omisión**, efecto en la siguiente llamada sin redeploy). `ai_gateway` comprueba los dos **antes** de reservar y de llamar (`AI_DISABLED`), leyendo la base en cada llamada o con una caché de pocos segundos como mucho. Apagar la IA no afecta a canales, webhooks ni al Inbox.
- **Encender la IA de una organización** exige además que quien la enciende confirme que el aviso de tratamiento de datos está publicado (§13); la confirmación queda en la auditoría.

### 6. Tools de solo lectura

Registro explícito en código (ADR-005 Parte C), con esquema estricto de entrada y salida, sin argumentos de identidad (`organization_id`, `contact_id`, `conversation_id`), ejecutadas en el `tenant_scope` de la organización y con el permiso equivalente de **la persona** que usa el copiloto. En este corte el registro **rechaza al cargar** cualquier tool cuyo `risk` no sea `READ` o cuya `audience` no sea `PUBLIC_SAFE`.

| Tool                   | Lee con                                                                    | Permiso de la persona | Devuelve                                                                                                           | Nunca devuelve                                               |
| ---------------------- | -------------------------------------------------------------------------- | --------------------- | ------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------ |
| `search_product`       | `catalog.selectors` (productos y variantes `ACTIVE` con `ai_visible`)      | `products.view`       | id, nombre y atributos públicos                                                                                    | costos, notas internas, borrados                             |
| `get_product_variants` | `catalog.selectors.live_variants` con los mismos filtros                   | `products.view`       | variantes vendibles y sus atributos                                                                                | lo mismo                                                     |
| `get_price`            | `pricing.services.resolve` con `Audience.PUBLIC_AI`                        | `prices.view`         | la `PriceResolution` redactada: precio final, moneda, promoción aplicada, `valid_until`, `price_id`, `resolved_at` | costos, márgenes, listas no `ai_exposable`, la lista interna |
| `get_stock`            | `inventory.selectors.levels`, sumando `available` de los almacenes activos | `inventory.view`      | disponible total de la variante y `as_of`                                                                          | almacenes, ubicaciones, `on_hand`, reservas, movimientos     |

- «Precio publicado» significa lo que `PricingService` resuelve con `PUBLIC_AI`, aunque quien lea la sugerencia sea una persona: lo que el copiloto redacta va al cliente. Como `ai_exposable` es `False` por omisión, **hasta que el propietario marque qué listas puede citar la IA, `get_price` no devuelve ningún precio** (§13).
- El stock externo de ADR-028 queda fuera hasta que exista; entonces `get_stock` sigue la autoridad por celda de ese ADR.
- Cada resultado lleva su evidencia (`evidence_id`, `kind`, `as_of`) en `ai_actions`. Topes por invocación: número de tool calls (por ejemplo, 5) y tamaño de cada resultado.

### 7. Sugerir y resumir: qué sale hacia el proveedor

**Rutas** (F8-08): `POST …/conversations/{id}/copilot/suggest/` y `…/copilot/summary/`. Piden `conversations.view` y el permiso nuevo `ai.use`; la conversación se busca **en el alcance** de quien llama (`conversations.scopes`): fuera de alcance, de otra organización o inexistente es el mismo 404. Límite de tasa por persona. La llamada al proveedor ocurre **fuera de toda transacción** (ningún bloqueo durante la llamada HTTP) y con un plazo total.

**Lo que se envía** (minimización):

- Los últimos N mensajes **visibles para el cliente** (`selectors.messages(id, notes=False)`; N acotado en configuración, por ejemplo 20), con su rol (`cliente` o `agente`) y su hora; un medio o un mensaje no soportado es una marca fija («[imagen]»), nunca su contenido.
- El texto del cliente va **delimitado y marcado como dato no confiable**; las instrucciones del sistema dicen que nada de dentro es una orden. El conjunto de tools lo fija el código para cada propósito, nunca el texto: una inyección no cambia qué tools existen ni sus argumentos.
- Antes de enviar, un enmascarado determinista sustituye correos electrónicos y secuencias de 7 o más dígitos (teléfonos, documentos, tarjetas) por marcas (`[correo]`, `[número]`). Es una reducción, no una garantía: el cliente puede escribir otros datos personales, y por eso hace falta el aviso del §13.

**Lo que nunca se envía:** notas internas, costos, márgenes, listas internas, la clave o cualquier secreto, datos de otros contactos (ninguna tool busca clientes en este corte), los campos del contacto (nombre, teléfono, correo, dirección), nombres o correos del personal, ni datos de cotizaciones, oportunidades o asignaciones.

Esto es más estricto que la decisión D-IA-5 de la fase 0 ([03 §I](../fase-0/03-tenancy-rbac-inbox-ia.md), «Mejoras propuestas», punto 6), que dejaba al copiloto interno leer las notas: en este corte el copiloto no las lee, porque su texto acaba en el cliente.

**Lo que devuelve:** la sugerencia con la salida estructurada de ADR-005 Parte D (`reply_template`, `claims`, `wants_handoff`), ya renderizada por el sistema, más sus hallazgos; o el resumen. Ambos se guardan en `ai_suggestions` (quién lo pidió, conversación, `ai_run_id`, texto y hallazgos) y se auditan solo con ids. **Nada se encola en el outbox de mensajes.** El resumen es solo para quien atiende: no se puede insertar en el compositor y no se convierte en memoria.

### 8. Traspaso a una persona

Un detector determinista de palabras clave («asesor», «persona», «humano», «hablar con alguien»…) **o** el campo `wants_handoff` del modelo (OR, como [03 §J.8](../fase-0/03-tenancy-rbac-inbox-ia.md)), más una señal de enfado, marcan la respuesta con `handoff: {requested, reason}`. En este corte el copiloto **no asigna ni transfiere nada**: el panel muestra el aviso y quien atiende decide (tomarla o transferirla con los comandos que ya existen). La sugerencia que acompaña al aviso solo puede ser un mensaje de paso a una persona.

### 9. Panel del Inbox y Output Guard asistido

- **Panel** (F8-09, con `ai.use`): «Sugerir respuesta» y «Resumir». La sugerencia se **inserta** en el compositor (editable) o se **descarta**. **No existe un botón que envíe la sugerencia**: lo que sale lo envía la persona con el envío normal (`record_outbound`, remitente `USER`), y entonces es su mensaje. Estados: cargando, presupuesto agotado, IA apagada, error.
- **Ninguna cifra la escribe el modelo.** Toda cifra dinámica (precio, descuento, stock, plazo) va como placeholder ligado a una evidencia de una tool de esta invocación, y el sistema copia el valor del resultado de la tool (ADR-005 Parte D). El Output Guard comprueba que cada claim tenga evidencia válida (de esta invocación, tool autorizada, `kind` correcto, campo existente, fresca: precio y stock 10 minutos como mucho) y busca con detectores deterministas cifras, porcentajes, términos de disponibilidad y plazos **fuera** de los placeholders.
- **En modo asistido el guard marca, no bloquea** (ADR-005 Parte D, último punto): una afirmación sin evidencia se resalta en el panel y la sugerencia lleva un aviso visible antes de insertarla. Un término comercial (descuentos no evidenciados, condiciones de pago, garantías, entregas) no tiene tool en este corte: siempre se marca. Si la evidencia caduca antes de insertar, el panel pide regenerar.

### 10. Permisos

| Permiso     | Para qué                                                      | Sensible                                  | Quién lo tiene al nacer                                     |
| ----------- | ------------------------------------------------------------- | ----------------------------------------- | ----------------------------------------------------------- |
| `ai.manage` | Claves, modelo, presupuestos y kill switch de la organización | Sí (lo concede el Owner con MFA reciente) | El Owner (ADR-018)                                          |
| `ai.use`    | Pedir sugerencias y resúmenes en el Inbox                     | No                                        | El Owner; los demás roles, cuando el Owner lo conceda (§13) |

La fase 0 ([03 §H](../fase-0/03-tenancy-rbac-inbox-ia.md), «Catálogo de permisos (v1)») listaba `ai_credentials.manage`, `ai.kill_switch` y `ai.copilot.use`. Este corte los reduce a dos; si más adelante hace falta separar el kill switch de las claves, se añade un permiso sin romper estos.

### 11. Acciones prohibidas

La IA **no** cambia precios, listas ni promociones; **no** ajusta, reserva ni transfiere stock; **no** aprueba, envía ni cambia cotizaciones; **no** cobra ni registra pagos; **no** crea ni modifica contactos, leads, oportunidades o tareas; **no** asigna ni cierra conversaciones; **no** envía mensajes. Se garantiza por tres vías independientes: no hay tools de escritura en el registro (§6), los contratos de import-linter (§2) y el panel sin envío (§9). Una tool de escritura llega como pronto en la Fase 10 y con su propio ADR.

### 12. Autonomía más adelante

Las respuestas automáticas quedan para un **ADR futuro**, que solo podrá proponerse con estas condiciones: solo preguntas frecuentes y tools de solo lectura; antes, una fase en sombra y pruebas controladas con conversaciones reales; métricas de este corte (tasa de sugerencias aceptadas sin cambios, marcas del guard, traspasos); el Output Guard en modo que bloquea, con el clasificador secundario de ADR-005 Parte D; traspaso y kill switch probados; y la decisión expresa del propietario. Este corte no cambia `attention_mode`: los valores `AI_ASSISTED` y `AI_AUTONOMOUS` siguen sin usarse.

### 13. Decisiones del propietario

| ID      | Pregunta                                                                                                                                                                                                             | Recomendación                                                                                   | Bloquea                          |
| ------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------- | -------------------------------- |
| D-F8-01 | Presupuesto mensual y diario por organización (USD)                                                                                                                                                                  | Un tope mensual bajo para el piloto y un diario de una fracción de él; subirlo con datos de uso | F8-05                            |
| D-F8-02 | Proveedores: ¿Anthropic primero y OpenAI segundo? ¿Se contrata ya la cuenta de OpenAI?                                                                                                                               | Anthropic primero; OpenAI cuando el copiloto funcione de punta a punta                          | F8-03, F8-10                     |
| D-F8-03 | Modelos en la lista permitida y el de la cuenta del piloto                                                                                                                                                           | Un modelo equilibrado en costo y calidad para sugerir; sin «Covered Models»                     | F8-05                            |
| D-F8-04 | Aviso de tratamiento de datos: qué datos salen a un proveedor de IA, para qué, y la transferencia fuera del país, publicado antes de encender la IA (normativa aplicable de datos personales; en Perú, la Ley 29733) | Publicarlo con asesoría legal antes de F8-09 en producción                                      | Encender la IA                   |
| D-F8-05 | Retención: detalle de `ai_llm_calls`, `ai_runs` y `ai_actions`; texto de `ai_suggestions`                                                                                                                            | 12 meses de detalle (ADR-011) sin contenido; el texto de las sugerencias y resúmenes, 30 días   | F8-05, F8-08                     |
| D-F8-06 | Qué listas de precios puede citar la IA (`ai_exposable`)                                                                                                                                                             | Solo la lista general al público                                                                | Que `get_price` devuelva precios |
| D-F8-07 | ¿El stock se muestra como número o como franja («disponible», «pocas unidades», «agotado»)?                                                                                                                          | Franja para el cliente; el número solo en la evidencia                                          | F8-07                            |
| D-F8-08 | Qué roles reciben `ai.use`                                                                                                                                                                                           | El Owner al principio; después Supervisor y Vendedor                                            | F8-08                            |
| D-F8-09 | ¿Se fija además un límite de gasto en la consola del proveedor?                                                                                                                                                      | Sí, como segunda barrera independiente del CRM                                                  | El uso con clientes reales       |

Ninguna respuesta incluye una clave: las claves solo se escriben en la pantalla protegida del §4.

### Qué cambia respecto de ADR-005

ADR-005 sigue `Accepted` y no se edita. Este ADR lo acota así:

| ADR-005                                                                           | En este corte                                                                                                                                | Tipo                                             |
| --------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------ |
| Parte A: `ai_agents` con agentes configurables                                    | Un único agente interno definido en código (el copiloto); sin tablas de agentes ni versiones                                                 | Ordena (llega después)                           |
| Parte B: OpenAI y Anthropic desde el inicio                                       | Anthropic primero; OpenAI en F8-10                                                                                                           | Ordena                                           |
| Parte B: `Provider ≠ Account ≠ Model`, catálogo sincronizado, `ai_account_models` | Una cuenta por organización y proveedor; un modelo por cuenta de una lista permitida en configuración; sin `list_models()` ni sincronización | Acota                                            |
| Parte B: routing por propósito y cadena de fallback                               | Sin routing ni fallback                                                                                                                      | Acota                                            |
| Parte B: circuit breaker en Redis                                                 | Reintentos acotados; sin circuit breaker                                                                                                     | Ordena                                           |
| Parte B: presupuestos por cuenta con BLOCK o FALLBACK                             | Por organización (diario y mensual), solo BLOCK                                                                                              | Acota (equivale con una cuenta por organización) |
| Parte B: `SecretStore.get(credential_id)` e `integrations.credentials`            | `core.sealed` (ADR-022) con el propósito `ai.provider_key`, en una tabla de `ai_gateway`                                                     | Concreta con lo que ya existe                    |
| Parte B: `base_url_override` con allowlist                                        | Sin override: host fijo por proveedor                                                                                                        | Acota                                            |
| Parte B: `stream()`                                                               | Sin streaming                                                                                                                                | Ordena (era P1)                                  |
| Parte C: tools `INTERNAL_ONLY` para el copiloto (`search_customer`)               | Solo `READ` y `PUBLIC_SAFE`; el precio con `Audience.PUBLIC_AI`                                                                              | Más estricto                                     |
| Parte D: clasificador secundario                                                  | Solo detectores deterministas (el clasificador es para respuestas autónomas)                                                                 | Acota                                            |
| Parte D: el copiloto advierte en vez de impedir                                   | Igual                                                                                                                                        | Igual                                            |
| Parte E: registros separados                                                      | Igual, particionados por mes (ADR-011)                                                                                                       | Igual                                            |
| Fase 0, D-IA-5: el copiloto lee notas internas                                    | No las lee                                                                                                                                   | Más estricto                                     |

Nada de este ADR permite lo que ADR-005 prohíbe: cifras sin evidencia, tools con argumentos de identidad, secretos en el contexto del modelo o costos en un resultado.

## Alternatives considered

| Alternativa                                              | Por qué se descarta                                                                                                |
| -------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------ |
| Construir ADR-005 completo antes de dar valor            | Meses sin nada usable y todas las superficies de riesgo abiertas a la vez (AGENTS.md §11)                          |
| Una clave de la plataforma para todas las organizaciones | Mezcla la facturación y el riesgo de varias organizaciones; ADR-005 ya fija claves por organización                |
| Guardar la clave en `credentials` con otro `owner_type`  | Su único escritor es `apps.conversations`; `ai_gateway` (L1) no puede depender de L3                               |
| El SDK oficial de Anthropic                              | Abre conexiones fuera de `core.http` (allowlist, IP fijada); se reconsidera si el SDK puede usar ese transporte    |
| Enviar automáticamente lo que pase el guard              | Es autonomía: necesita su ADR, una fase en sombra y pruebas reales (§12)                                           |
| Dejar al copiloto leer las notas internas (D-IA-5)       | Su texto acaba en el cliente: el riesgo de filtrar costos u opiniones es mayor que el beneficio en el primer corte |
| Presupuesto solo mensual                                 | Un error o un abuso puede gastar el mes en un día; el tope diario limita el daño                                   |
| Usar la sesión de Claude Code o una suscripción personal | No es la API del producto, no se factura a la organización y mezcla credenciales personales con las del CRM        |

## Consequences

- Aparecen dos módulos (`ai_gateway`, `ai_agents`), dos permisos (`ai.manage`, `ai.use`), un propósito de `core.sealed` (`ai.provider_key`), un host en la allowlist y las tablas `ai_provider_accounts`, su tabla de claves, los presupuestos, `ai_llm_calls`, `ai_runs`, `ai_actions` y `ai_suggestions`, repartidas entre F8-03 y F8-08.
- El copiloto no da precios hasta que el propietario marque listas `ai_exposable`, y no funciona en ninguna organización hasta que se enciendan los dos kill switch y exista presupuesto.
- Una sugerencia tarda lo que tarda el modelo (segundos): el panel muestra el estado y la ruta tiene un plazo total.
- Cada sugerencia cuesta dinero de la organización: el presupuesto y el registro de uso son parte del primer corte, no un añadido.
- CI y desarrollo nunca llaman a un proveedor real: cada adaptador tiene un simulador sin red que registra lo que recibe (y así las pruebas comprueban que una nota interna nunca llega).

## Security implications

- **Datos que salen a un tercero:** solo los del §7, después del enmascarado; nunca notas, costos, secretos ni datos de otros contactos. El simulador que registra lo recibido es la prueba de esa frontera.
- **Inyección de prompts:** el texto del cliente es dato no confiable; las tools no tienen argumentos de identidad, solo leen y las fija el código; el modelo no escribe cifras; nada se envía sin una persona. Una inyección puede, como mucho, producir una sugerencia mala que la persona descarta.
- **Clave del proveedor:** sellada con dueño y propósito, guardada solo con `ai.manage` y segundo factor reciente, nunca devuelta ni registrada; la prueba de conexión no es un oráculo del contenido.
- **Ataques de costo:** presupuesto con corte duro y reserva previa, límites de tasa por persona, tope de tool calls y de tokens, y kill switch.
- **Aislamiento:** RLS con FORCE en todas las tablas nuevas, organización siempre del contexto, mismo 404 para lo ausente y lo ajeno.
- No debilita ADR-001, ADR-002, ADR-003, ADR-013 ni ADR-014.

## Operational implications

- **El propietario** crea la cuenta del proveedor, carga saldo, fija un límite de gasto en su consola (D-F8-09), genera la clave y la guarda en la pantalla del CRM con su segundo factor; ningún valor pasa por el repositorio, un issue o un chat.
- **Despliegue:** `AI_ENABLED` en el entorno (apagado por omisión), el host del proveedor en `HTTP_ALLOWED_HOSTS` y `SECRETS_KEKS` (ya obligatorio por MFA).
- **Precios de los modelos:** se revisan contra la página oficial al cambiar de modelo y, como mínimo, cada trimestre; un cambio de precio es un PR con la fecha.
- **Métricas de este corte:** costo por organización y día, sugerencias pedidas, insertadas y descartadas, marcas del guard por tipo, traspasos detectados y errores del proveedor por código.
- **Incidente:** apagar la organización (inmediato) o `AI_ENABLED` (reinicio); rotar la clave desde la pantalla; el registro de uso y la auditoría dicen quién pidió qué y cuánto costó.

# G–J · Multi-tenancy, RBAC, Inbox y arquitectura de IA

---

## G. Multi-tenancy: cómo evitar fugas entre organizaciones

### Modelo elegido: **base de datos compartida, esquema compartido, `organization_id` en cada fila + Row Level Security (RLS) de PostgreSQL como segunda barrera**

**Por qué no una base de datos o un esquema por tenant:** complica migraciones, pooling, reportes de plataforma y costes, y no aporta ventajas decisivas a nuestro tamaño. Con `organization_id` + RLS obtenemos aislamiento fuerte sin esa carga operativa.

**Por qué RLS desde el día 1 [RECOMENDACIÓN, D4]:** el filtrado solo en la aplicación depende de que **ningún desarrollador olvide nunca** un `.filter(organization=…)`, incluidos Celery, las tools de IA, los reportes y los scripts. Con RLS, olvidarlo devuelve 0 filas en vez de datos ajenos. Retrofitear RLS después es caro. Coste: ~1 semana extra en la Fase 1 y disciplina en los tests.

### Las 12 capas de defensa

| # | Capa | Mecanismo concreto |
|---|---|---|
| 1 | **Resolución del tenant** | Middleware: usuario autenticado → organización activa (URL `/o/{org_slug}/…` en el frontend y `/api/v1/o/{org_slug}/…` en la API; la URL solo selecciona, no autoriza) → verifica que exista una `organization_memberships` en estado ACTIVE → `request.ctx = ExecutionContext(org, membership, perms)`. Sin membresía válida: **404** (no 403, para no revelar la existencia de la organización) |
| 2 | **Contexto de BD** | Todo request corre en una transacción (`ATOMIC_REQUESTS` o equivalente en las vistas) que fija `app.tenant_id` y `app.user_id` con `set_config(…, true)` (equivalente a `SET LOCAL`), ver [tenancy-context](../architecture/tenancy-context.md). Muere con la transacción, así que es compatible con PgBouncer en modo transacción |
| 3 | **Políticas RLS** | En cada tabla con tenant: `ENABLE` + **`FORCE ROW LEVEL SECURITY`** y `CREATE POLICY tenant_isolation USING (organization_id = app_current_tenant()) WITH CHECK (organization_id = app_current_tenant())`, donde `app_current_tenant()` = `NULLIF(current_setting('app.tenant_id', true), '')::uuid` (tras un `set_config` local, la variable puede quedar como `''` y no como NULL). Sin contexto, la consulta no ve nada. `organization_memberships` usa una política SELECT especial que **no amplía** la visibilidad cuando hay tenant activo (ADR-002 §3.2) |
| 4 | **Roles de BD** | `crm_app` (runtime: no es propietario de las tablas, no tiene BYPASSRLS y tiene revocado UPDATE/DELETE en tablas append-only); `crm_migrator` (propietario con BYPASSRLS, **solo** en el job de migraciones: su credencial nunca llega a web, worker, ws ni beat); `crm_platform` (opcional, BYPASSRLS, solo para comandos de plataforma explícitos y auditados). Ver ADR-002 |
| 5 | **ORM** | `TenantModel` base con `organization` FK NOT NULL. Manager por defecto `TenantManager` que filtra por el contexto actual (contextvar) y **lanza una excepción si no hay contexto** en lugar de devolver todo. El manager `all_tenants` existe solo para `platform`, y import-linter prohíbe usarlo fuera de ese módulo |
| 6 | **Escrituras** | `organization_id` se asigna **siempre desde el contexto** en el servicio, nunca desde el payload. Los serializers no exponen `organization` como campo escribible |
| 7 | **FKs recibidas por la API** (la fuga clásica) | Un `TenantPrimaryKeyRelatedField` valida que el ID recibido (p. ej., `contact_id` al crear un lead) pertenezca al tenant. RLS también lo cubre en lecturas, pero **las comprobaciones de FK de PostgreSQL ignoran RLS**: sin esta validación, alguien podría vincular un registro a un contacto de otra organización. [RECOMENDACIÓN] Para relaciones críticas, añadir **FKs compuestas** `(organization_id, contact_id) → contacts(organization_id, id)` vía SQL en las migraciones |
| 8 | **Celery** | Toda tarea recibe `organization_id` explícito; un decorador `@tenant_task` abre una transacción y fija el contexto y el `SET LOCAL`. Las tareas sin tenant (health checks) se marcan `@platform_task` |
| 9 | **WebSockets** | Grupos con el tenant en el nombre: `org.{org_id}.inbox`, `org.{org_id}.conv.{conv_id}`. La suscripción a un grupo de conversación **verifica permiso sobre esa conversación** |
| 10 | **Caché, storage y búsqueda** | Claves de caché `t:{org_id}:…`; object storage con prefijo `org/{org_id}/` y URLs firmadas emitidas tras verificar permisos; `search_documents` y `kb_chunks` con RLS + filtro explícito |
| 11 | **Webhooks** | La organización se deriva de `channel_accounts.external_account_id` (único global), **nunca** de un campo del payload |
| 12 | **IA** | El `organization_id`, el `contact_id` y el `conversation_id` los inyecta el runtime en el contexto de la tool. **Las tools no aceptan esos IDs como argumentos del LLM.** La búsqueda vectorial filtra por organización |

### Verificación automática (obligatoria en CI)

1. **Suite de aislamiento cruzado:** un test parametrizado recorre **todas las rutas registradas** del router DRF, crea datos en las organizaciones A y B, autentica como A y verifica que cualquier acceso a IDs de B devuelve 404 (GET, PATCH, DELETE) y que ningún listado contiene filas de B. Si se añade un endpoint nuevo, entra automáticamente en el test.
2. **Test de RLS:** una consulta sin `app.tenant_id` devuelve 0 filas en cada tabla con tenant (lista completa de tests: tenancy-context §8).
3. **Test de esquema:** toda tabla que herede de `TenantModel` tiene política RLS y `FORCE` (introspección de `pg_policies`/`pg_class`).
4. **Test de restricciones únicas:** todo UNIQUE de una tabla con tenant incluye `organization_id` (introspección).
5. **Test de tools de IA:** cada tool se ejecuta con el contexto de A intentando referenciar entidades de B → `NOT_FOUND`.

### Superadmin e impersonación

- El **Superadmin es personal de la plataforma**, no un rol de la organización (ver H y D1). Para ver datos de una organización debe abrir una `impersonation_session` con motivo obligatorio, expiración de 60 minutos, banner visible en la UI y auditoría de cada acción con `impersonated_by`. [RECOMENDACIÓN] En P2, exigir que el Owner de la organización active "permitir acceso de soporte".

---

## H. RBAC

### Modelo definitivo

```text
Permiso (código, catálogo global)  ──<  RolePermission(scope)  >──  Rol (por organización)
                                                                       │
Membresía (usuario en la organización) ──< membership_roles >──────────┘
Permisos efectivos = unión de los permisos de todos sus roles; alcance efectivo = el máximo
Políticas adicionales (no binarias): discount_policies, price_lists.required_permission,
                                     data scopes, reglas de propiedad
```

**Tres niveles de verificación:**

1. **Permiso** (¿puede hacer esta acción?): `ctx.require("quotes.approve")`.
2. **Alcance** (¿sobre qué registros?): `OWN` (asignados a mí o creados por mí) · `TEAM` (de mis equipos) · `BRANCH` (de mi sucursal) · `ORGANIZATION` (toda la organización). Se aplica en los `selectors` como filtro de queryset **y** en la verificación por objeto.
3. **Política** (¿con qué límites?): descuento máximo, listas de precios visibles, montos.

[DECISIÓN D7] ¿Necesitamos alcance por sucursal y equipo desde el MVP? Recomiendo **sí en el modelo** (columna `scope`) y usar OWN/ORGANIZATION en la UI inicial. *(Cerrada: D7 / ADR-003.)* Añadirlo después obliga a revisar todos los selectors.

### Reglas anti-escalada de privilegios

1. Nadie puede asignar a un rol un permiso que él mismo no tenga.
2. Nadie puede asignarse roles a sí mismo.
3. Los **permisos sensibles** (`is_sensitive`) solo los puede otorgar el Owner, y su concesión exige una sesión con MFA reciente (≤ 15 minutos): `ai_credentials.manage`, `integrations.manage`, `roles.manage`, `product_cost.view`, `prices.manage`, `users.manage`, `audit.view`, `data.export`, `organization.manage`, `ai.kill_switch`.
4. Siempre debe existir **al menos un Owner activo** en la organización (no se puede degradar, desactivar ni eliminar al último).
5. Cada cambio de rol o permiso se audita con el diff (antes/después).
6. El frontend recibe la lista de permisos efectivos **solo para ocultar o mostrar UI**. La autorización se hace siempre en el backend.

### Catálogo de permisos (v1)

| Módulo | Permisos (★ = sensible; ◎ = admite alcance) |
|---|---|
| Organización | `organization.view`, `organization.manage`★, `branches.manage`, `settings.manage` |
| Usuarios/equipos | `users.view`, `users.manage`★, `users.invite`, `teams.view`, `teams.manage`, `roles.view`, `roles.manage`★ |
| Contactos | `contacts.view`◎, `contacts.create`, `contacts.update`◎, `contacts.delete`◎, `contacts.merge`, `contacts.export`★, `contacts.assign` |
| Inbox | `conversations.view`◎, `messages.send`◎, `messages.send_template`, `conversations.assign`, `conversations.transfer`◎, `conversations.take` (tomar una conversación no asignada), `conversations.close`◎, `conversations.reopen`, `conversations.archive`, `conversations.supervise` (panel de supervisor, tomar la de otro), `notes.internal.create`, `quick_replies.manage` |
| Leads | `leads.view`◎, `leads.create`, `leads.update`◎, `leads.assign`, `leads.delete`◎, `leads.convert` |
| Oportunidades | `opportunities.view`◎, `opportunities.create`, `opportunities.update`◎, `opportunities.assign`, `opportunities.delete`◎, `opportunities.mark_won`◎, `opportunities.mark_lost`◎, `pipelines.manage` |
| Cotizaciones | `quotes.view`◎, `quotes.create`, `quotes.update`◎, `quotes.send`◎, `quotes.approve`, `quotes.cancel`◎, `quotes.override_price` (escribir precio manual), `quotes.discount` (limitado por `discount_policies`) |
| Ventas | `orders.view`◎, `orders.create`, `orders.update`◎, `orders.cancel` |
| Productos | `products.view`, `products.manage`, `products.import`, `categories.manage`, `brands.manage` |
| Precios | `prices.view` (lista por defecto), `prices.view_wholesale`, `prices.view_distributor`, `prices.view_internal`, `prices.manage`★, `prices.bulk_update`★, `product_cost.view`★, `product_cost.manage`★, `promotions.manage` |
| Inventario | `inventory.view`, `inventory.manage`, `inventory.adjust`, `inventory.transfer`, `warehouses.manage` |
| Tareas | `tasks.view`◎, `tasks.create`, `tasks.update`◎, `tasks.assign` |
| IA (operativa) | `ai_agents.view`, `ai_agents.manage`, `ai_agents.publish`, `ai_knowledge.manage`, `ai_conversations.view`, `ai_feedback.create`, `ai_metrics.view`, `ai.copilot.use`, `ai.kill_switch`★ |
| IA (infraestructura) | `ai_credentials.manage`★, `ai_models.manage`, `ai_routing.manage`, `ai_usage.view`, `ai_limits.manage` |
| Canales | `channels.view`, `integrations.manage`★, `templates.manage` |
| Automatizaciones | `automations.view`, `automations.manage` |
| Reportes | `reports.sales.view`, `reports.support.view`, `reports.marketing.view`, `reports.ai.view`, `dashboard.admin.view`, `data.export`★ |
| Auditoría/papelera | `audit.view`★, `trash.view`, `trash.restore`, `trash.purge`★ |

> **Lectura de inventario (decisión del mantenedor, 2026-10-22, F3-19):** `inventory.view` incluye leer, de cada fila de existencias o movimientos, el SKU, el nombre del producto y de la variante y los identificadores necesarios para navegar y filtrar, aunque el rol no tenga `products.view`. No concede costos, márgenes, proveedores, precios ni editar el catálogo.

### Matriz de roles plantilla (se clonan por organización y son editables salvo Owner)

| Permiso / grupo | Owner | Admin | Supervisor | Vendedor | Soporte | Marketing | Consulta |
|---|---|---|---|---|---|---|---|
| organization.manage, roles.manage, ai_credentials.manage, integrations.manage | ✅ | ⚙️ (si el Owner lo otorga) | — | — | — | — | — |
| users.manage / teams.manage | ✅ | ✅ | teams (propio) | — | — | — | — |
| conversations.view | ORG | ORG | TEAM | OWN + sin asignar | OWN + sin asignar | — | ORG (lectura) |
| messages.send | ✅ | ✅ | ✅ | OWN | OWN | — | — |
| conversations.supervise / take | ✅ | ✅ | ✅ | take | take | — | — |
| contacts.view | ORG | ORG | TEAM | OWN (+ búsqueda limitada) | ORG | ORG | ORG |
| contacts.export | ✅ | ✅ | — | — | — | ⚙️ | — |
| leads / opportunities | ORG | ORG | TEAM | OWN | view | view ORG | view |
| quotes.create / send | ✅ | ✅ | ✅ | ✅ | — | — | — |
| quotes.approve | ✅ | ✅ | ✅ | — | — | — | — |
| discount_policies (máx.) | sin límite | sin límite | p. ej., 10 % | p. ej., 3 % | 0 % | — | — |
| prices.view | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| prices.view_wholesale | ✅ | ✅ | ✅ | ⚙️ | — | — | — |
| prices.manage / bulk_update | ✅ | ✅ | — | — | — | — | — |
| product_cost.view | ✅ | ⚙️ | — | — | — | — | — |
| inventory.manage | ✅ | ✅ | view | view | view | — | view |
| ai_agents.manage / publish | ✅ | ✅ | view | — | — | — | — |
| ai.kill_switch | ✅ | ✅ | ✅ | — | — | — | — |
| automations.manage | ✅ | ✅ | view | — | — | ⚙️ | — |
| reports.* | ✅ | ✅ | ventas + atención | propios | atención propia | marketing | ✅ lectura |
| audit.view | ✅ | ⚙️ | — | — | — | — | — |

⚙️ = desactivado por defecto; el Owner puede activarlo. ORG = scope `ORGANIZATION`.

### ¿Y los agentes de IA?

Los agentes **no tienen roles**. Son principales de servicio cuyo poder se define así:

`permisos del agente = tools habilitadas en su versión ∩ tools permitidas para su tipo (customer_facing o interno)`

Cada tool declara el permiso de negocio equivalente, y el servicio que invoca lo verifica igual que para un humano, con `actor = AIAgent`. Un agente de cara al cliente **nunca** obtiene `product_cost.view`, `prices.manage`, `quotes.approve`, `contacts.delete`, `contacts.export` ni `conversations.supervise`, **aunque alguien lo configure así**: la restricción está en el registro de tools del código, no en la base de datos.

---

## I. Inbox: validación y mejoras del modelo de conversaciones

### Lo que confirmo del prompt

- **Estado (§13) independiente de quién atiende (§14):** ✅ Correcto. Son dos ejes ortogonales. Mezclarlos es el error más común en estos sistemas.
- **Modo de atención por conversación:** ✅ Correcto, permite pasar de IA autónoma a asistida sin reasignar.
- **Notas internas como tipo de mensaje distinto:** ✅ Correcto, siempre que la separación se imponga en la base de datos y en el pipeline de salida (ver abajo).

### Mejoras propuestas

**1. Tres ejes, no dos:**

| Eje | Campo | Valores |
|---|---|---|
| Ciclo de vida | `status` | OPEN · PENDING · WAITING_CUSTOMER · WAITING_INTERNAL · RESOLVED · CLOSED |
| Quién atiende | `assigned_team_id` + `assigned_user_id` / `assigned_ai_agent_id` (ADR-007) | Equipo y persona **a la vez** (la conversación está en la cola de Ventas y la tiene María) |
| Cómo atiende la IA | `attention_mode` | AI_AUTONOMOUS · AI_ASSISTED · HUMAN |
| (transitorio) Handoff | `handoff_status` | NONE · REQUESTED · ACCEPTED |

**[INCONSISTENCIA]** `HANDOFF_REQUESTED` (§17) no cabe en ninguno de los enums del prompt. Propongo que sea `handoff_status` + un registro `ai_handoffs`, y que la conversación aparezca en el filtro "Transferencias".

**Invariantes entre ejes (validadas en el servicio):**
- `attention_mode = AI_AUTONOMOUS` ⇒ `assigned_ai_agent_id IS NOT NULL` y el nivel de autonomía de la versión del agente ≥ 2.
- `assigned_user_id IS NOT NULL` ⇒ `attention_mode ∈ {HUMAN, AI_ASSISTED}` (con un humano asignado, la IA no envía sola).
- Kill switch activo ⇒ ninguna conversación en AI_AUTONOMOUS (se migran en bloque a HUMAN y a la cola por defecto).
- **Nivel de autonomía del agente = techo** y `attention_mode` = estado actual. Un agente de nivel 1 solo puede estar en AI_ASSISTED.

**2. Definición precisa de estados (propuesta, [DECISIÓN D-INB-1]):**

| Estado | Significado propuesto | Transición automática |
|---|---|---|
| OPEN | Activa; el equipo o la IA la está trabajando | Inbound en cualquier estado no cerrado → OPEN |
| PENDING | En cola, **sin tomar todavía por un humano** (nueva, reabierta o transferida sin aceptar) | Pasa a OPEN al tomarla o al enviar la primera respuesta humana |
| WAITING_CUSTOMER | Se respondió y se espera al cliente | Outbound de humano/IA con "esperar respuesta" → WAITING_CUSTOMER; inbound → OPEN |
| WAITING_INTERNAL | Esperando a otra área (tasación, stock, aprobación) | Manual o por evento (p. ej., cotización en PENDING_APPROVAL) |
| RESOLVED | Resuelta; puede reabrirse | Inbound dentro de `conversation_reopen_window_hours` → OPEN (misma conversación) |
| CLOSED | Terminal | Inbound después de CLOSED → **nueva conversación** (con enlace a la anterior) |

**Filtros derivados (no son estados):** "No respondidos" = `waiting_since IS NOT NULL`; "Archivados" = `is_archived`; "Mis conversaciones" = `assigned_user_id = yo`; "Sin asignar" = sin equipo ni usuario ni IA; "Transferencias" = `handoff_status = REQUESTED`; "Tiempo esperando > X" = `now() - waiting_since`. **[INCONSISTENCIA]** "Pendientes" (filtro §12) y PENDING (estado §13) deben significar lo mismo. Con la definición de arriba, coinciden.

**3. Tomar, asignar y concurrencia:**
- **Tomar conversación** es atómico: `UPDATE conversations SET assigned_user_id = :me, assignment_version = assignment_version + 1 WHERE id = :id AND assigned_user_id IS NULL AND assigned_ai_agent_id IS NULL` (ADR-007). Si afecta a 0 filas → "María la tomó hace un instante".
- Reasignar/transferir: `SELECT … FOR UPDATE` + verificación de versión + registro en `conversation_assignments` + evento (siempre vía `AssignmentService`), en la misma transacción.
- **Carrera IA ↔ humano:** si un humano toma la conversación mientras la IA está generando, el worker `outbound` revalida justo antes de enviar que `assigned_ai_agent_id` y `version` siguen siendo los mismos. Si cambiaron, la respuesta de la IA **se descarta** (`ai_runs.status = SUPERSEDED`) y se guarda como sugerencia visible para el humano.
- **Presencia y "está escribiendo":** eventos efímeros por WS (no se persisten) para evitar que dos agentes respondan a la vez: "Carlos está respondiendo…".

**4. Debounce de mensajes entrantes (crítico para la IA):** los clientes escriben en ráfagas ("hola" / "precio del 16 pro" / "256"). La IA espera `ai_debounce_seconds` (configurable, 3–6 s) desde el último mensaje y procesa el lote completo. Se implementa con una tarea programada con ETA que se reprograma, más un lock por conversación en Redis. Sin esto, la IA responde tres veces.

**5. Ventana de 24 h (WhatsApp, Instagram, Messenger):** `customer_window_expires_at` se guarda denormalizado para UI e índices, pero la autorización la calcula `MessagingPolicyService` desde `last_inbound_at` y las capacidades vigentes del adapter, justo antes de cada envío (ADR-010). Si la ventana está cerrada, el editor del Inbox **deshabilita el texto libre** y ofrece plantillas aprobadas (WhatsApp) o la etiqueta HUMAN_AGENT (IG/Messenger, 7 días, solo para humanos). La IA tampoco puede enviar texto libre fuera de ventana.

**6. Notas internas blindadas (tres barreras):**
1. En la BD: `CHECK` que impide que un mensaje INTERNAL tenga `external_message_id`.
2. En el `OutboundDispatcher`: rechaza (y alerta) cualquier mensaje con `direction = INTERNAL`.
3. En el Context Builder: los **agentes de cara al cliente no reciben notas internas** (pueden contener costos, márgenes u opiniones). El copiloto interno sí las recibe. [DECISIÓN D-IA-5]

**7. Orden y deduplicación:** ordenar por `created_at` (servidor) y mostrar `external_timestamp`. Upsert idempotente por `external_message_id`. Los estados de entrega pueden llegar **antes** que el mensaje (condición de carrera del webhook de estado frente a la respuesta del envío): se guardan en `message_status_events` y se reconcilian.

**8. Omnicanalidad en la UI:** una conversación = un hilo en un canal. La vista del **contacto** agrupa todas sus conversaciones. En el panel derecho del Inbox se muestra "Este cliente también escribió por Instagram hace 2 días" con enlace. No se mezclan en un solo hilo mensajes de canales distintos: la ventana de 24 h, las plantillas y el origen del envío son diferentes por canal.

**9. Paginación del chat:** por cursor (`before=<message_id>`), 50 mensajes por página; la lista de conversaciones con cursor sobre (`last_message_at`, `id`).

### Flujo de envío de un mensaje humano

```text
UI: optimistic append (client_message_id=uuid) → POST /conversations/{id}/messages
API: permiso messages.send (+alcance) → ventana 24 h → quick-reply/variables → INSERT (QUEUED)
     → outbox message.created → 202
Worker outbound: Adapter.send → external_message_id → SENT → WS message.updated
Webhook status: DELIVERED/READ → WS → UI actualiza los checks
Error: FAILED + error_code legible ("Fuera de la ventana de 24 h: usa una plantilla")
```

---

## J. Arquitectura de IA

### J.1 Componentes

```text
┌───────────────────────────── ai_agents ─────────────────────────────┐
│ Trigger (mensaje, copiloto, handoff, supervisor)                    │
│   → Gatekeeper: kill switch plataforma/org · agente activo ·        │
│     horario · modo de atención · ventana 24 h · presupuesto         │
│   → Context Builder (versión de agente, prompt, memoria, KB, tools) │
│   → Orchestrator (tool loop, máx. N tool calls)                     │
│        ↕ Tool Registry → servicios de negocio (con ctx del agente)  │
│   → Output Guardrails (validación de cifras, fugas, políticas)      │
│   → Decision Engine (autonomía × confianza × reglas)                │
│   → Enviar | Sugerencia | Handoff | Nada                            │
│   → Registro: ai_runs, ai_actions, audit_logs, métricas             │
└──────────────────────────────┬──────────────────────────────────────┘
                               │ LLMRequest neutro (mensajes, tools JSON Schema)
┌──────────────────────────── ai_gateway ─────────────────────────────┐
│ Router: resuelve la cadena [principal, fallback1, …] de la versión  │
│ Budget guard (Redis) · Circuit breaker por cuenta · Retry policy    │
│ Adapters: OpenAIAdapter · AnthropicAdapter · (Gemini, xAI, Local)   │
│ Metering: ai_llm_calls (tokens, coste con ai_model_prices)          │
│ Secretos: SecretStore.get(credential_id) en memoria, nunca en logs  │
└─────────────────────────────────────────────────────────────────────┘
```

### J.2 Providers, accounts y models (§66–73)

- **Provider ≠ Account ≠ Model** ✅ Confirmo la separación del §72: es correcta y necesaria. `ai_providers` (global, requiere adapter en código) → `ai_provider_accounts` (por organización, con credencial) → `ai_models` (catálogo global) ← `ai_account_models` (qué modelos se habilitan en cada cuenta).
- **Interfaz del adapter:**
  - `chat(request: LLMRequest) -> LLMResponse`
  - `stream(request) -> Iterator[LLMChunk]` (P1)
  - `list_models() -> list[RemoteModel]`
  - `test_connection() -> ConnectionTestResult` (llamada mínima de listar modelos o 1 token)
  - `classify_error(exc) -> ErrorClass`
  - `count_usage(response) -> Usage`
- **Formato neutro:** mensajes con roles `system`/`user`/`assistant`/`tool`, bloques de contenido (texto, imagen) y `tool_calls` normalizados. El adapter traduce a function calling de OpenAI o a bloques `tool_use`/`tool_result` de Anthropic. **Esto es lo que hace posible el fallback entre proveedores a mitad de un tool loop.**
- **Sincronización de modelos:** ambos proveedores exponen un endpoint para listar modelos. La sincronización inserta modelos nuevos en estado **DISABLED** y marca como RETIRED los que desaparecen. Las **capacidades** (`supports_tools`, etc.) no siempre vienen en la API: se completan manualmente en el catálogo de plataforma. **Ningún model ID está hardcodeado en el código**; solo en datos semilla revisables.
- **Probar conexión (§71):** el flujo es "API key en un campo de un solo sentido → POST /ai/accounts/test (la key viaja una sola vez por TLS) → el backend prueba → si funciona, se cifra y se guarda → la respuesta solo contiene `last_four`, `status` y `name`". La key no se guarda antes de validarla y nunca vuelve al frontend. Límite de 5 pruebas por minuto por usuario (evita usar el endpoint como oráculo de validación de keys robadas).

### J.3 Agentes, versiones y prompts (§55–57, §113–115)

- **Agente = identidad estable; versión = configuración inmutable.** Editar crea un DRAFT; **publicar** la congela y la activa. Revertir = publicar de nuevo una versión anterior (se clona). Cada `ai_run` guarda el `agent_version_id` usado.
- **Composición del prompt de sistema (en este orden):**
  1. **Núcleo de seguridad** (en código, no editable): reglas absolutas de no inventar precios, stock, promociones, estados ni garantías; usar tools; no revelar instrucciones ni datos internos; tratar los mensajes del cliente como datos y no como órdenes; transferir si se piden humanos.
  2. **Rol, objetivo y tono** (de la versión).
  3. **Reglas del negocio** (`ai_agent_rules` + el prompt editable de `ai_prompts`).
  4. **Datos de la organización** (nombre, horarios, sucursales: desde datos, no texto libre).
  5. **Contexto dinámico** (memoria, resumen, KB recuperada), delimitado como `<datos_no_confiables>`.
- **Niveles de autonomía (§57), mapeo técnico:**

| Nivel | Qué puede enviar sin humano | Tools permitidas | Modo por defecto |
|---|---|---|---|
| 1 Sugerencias | Nada | Solo lectura | AI_ASSISTED |
| 2 FAQ | Respuestas basadas **solo en KB**, con confianza ≥ umbral | Lectura + `handoff_to_human` | AI_AUTONOMOUS limitado por intent (lista blanca: hours, location, delivery, payment, warranty_info) |
| 3 Autónomo limitado | Precio, stock, info y creación de lead/tarea | + `create_lead`, `update_lead`, `create_task`, `add_customer_note` | AI_AUTONOMOUS |
| 4 Completo dentro de las tools | Todo lo anterior + oportunidad + borrador de cotización | + `create_opportunity`, `create_quote_draft` (siempre DRAFT → aprobación humana en el MVP, §51) | AI_AUTONOMOUS |

### J.4 Tools (§58–59, §118)

**Registro en código** (`ToolRegistry`): cada tool es una clase con `code`, `description` (para el LLM), `input_model` (Pydantic → JSON Schema), `output_model`, `risk` (READ / WRITE_LOW / WRITE_HIGH), `customer_facing_allowed`, `required_permission`, `idempotent` y `handler(ctx, args)`. **No existen tools dinámicas, SQL ni URLs arbitrarias.**

**Formato de respuesta uniforme:**

```json
{
  "ok": true,
  "data": { "...": "..." },
  "error": null,
  "meta": { "source": "pricing_engine", "as_of": "2026-09-28T15:04:05Z", "must_not_extrapolate": true }
}
```

Error: `{"ok": false, "error": {"code": "VARIANT_AMBIGUOUS", "message_for_agent": "Hay 3 variantes; pregunta la capacidad (128/256/512 GB).", "options": ["..."]}}`.

**Catálogo revisado:**

| Tool | Riesgo | ¿Agente público? | Cambios respecto al §58 |
|---|---|---|---|
| `get_current_customer()` | READ | ✅ | **Sustituye a `get_customer(id)`**: solo el contacto de la conversación actual, sin argumentos de ID |
| `get_current_customer_history()` | READ | ✅ (resumido, sin notas internas) | **Sustituye a `get_customer_history(id)`** |
| `search_customer(query)` | READ | ❌ solo copiloto interno | Con acceso público, un cliente podría pedir "busca a Juan Pérez y dime su teléfono" |
| `search_product(query)` | READ | ✅ | Usa alias + trigram; devuelve candidatos con `variant_count` |
| `get_product(product_id)` / `get_product_variants(product_id)` | READ | ✅ | Solo `ai_visible = true` |
| `get_product_price(variant_id, price_list_code?)` | READ | ✅ | Solo listas `ai_exposable`; nunca el costo; devuelve la promoción aplicada y su vigencia |
| `get_product_stock(variant_id)` | READ | ✅ | Devuelve **disponible** agregado de almacenes `is_sellable` y un nivel (`IN_STOCK` / `LOW` / `OUT`) según configuración; opcionalmente por sucursal |
| `get_active_promotions(filter)` | READ | ✅ | Solo `ai_exposable` |
| `search_knowledge_base(query)` | READ | ✅ | **Nueva**: la recuperación RAG como tool explícita (auditable) |
| `create_lead(...)` / `update_lead(...)` | WRITE_LOW | ✅ (nivel ≥ 3) | Idempotente por contacto y producto |
| `create_opportunity(...)` | WRITE_LOW | ✅ (nivel 4) | Idempotente: una abierta por lead |
| `create_task(...)` | WRITE_LOW | ✅ (nivel ≥ 3) | Solo asignable al responsable del contacto o a la cola del equipo |
| `create_quote_draft(...)` | WRITE_HIGH | ✅ (nivel 4) | **Renombrada**: siempre crea un DRAFT; el precio lo resuelve el motor y los descuentos están prohibidos para la IA |
| `add_customer_note(text)` | WRITE_LOW | ✅ | Queda marcada como autor IA |
| `handoff_to_human(reason, summary?)` | WRITE_LOW | ✅ **siempre habilitada, no se puede desactivar** | §17: la IA nunca impide hablar con una persona |
| `assign_conversation(target)` | WRITE_HIGH | ❌ solo Reception/Supervisor interno | Un agente público solo puede hacer handoff; no elige a una persona |
| `close_conversation(reason)` | WRITE_LOW | ⚙️ (nivel 4, configurable) | Solo pasa a RESOLVED, nunca a CLOSED |
| `request_trade_in_appraisal(...)` | WRITE_LOW | P2 | Futuro (crea tarea + handoff) |
| **Prohibidas por diseño (no existen como tools)** | — | ❌ | Cambiar precio o costo, aplicar descuentos, borrar entidades, autorizar garantías o devoluciones, mover dinero, exportar datos, enviar mensajes a otro contacto |

**Idempotencia de tools:** `idempotency_key = ai_run_id + tool_call_id`. Si un fallback repite el turno, la tool devuelve el resultado ya registrado en `ai_actions` en lugar de crear otro lead.

### J.5 Routing y fallback (§74–77)

**Routing:** `ai_agent_model_configs` por versión y propósito. Los propósitos auxiliares (clasificación de intent, resumen, extracción) pueden usar **modelos más baratos** que el de conversación.

**Política de errores (qué hace fallback y qué no):**

| Clase de error | Reintento en la misma cuenta | Fallback | Otros efectos |
|---|---|---|---|
| Timeout / conexión | 1 reintento con backoff y jitter | ✅ | Cuenta en el circuit breaker |
| 429 rate limit | Si `retry-after` ≤ 2 s, 1 vez | ✅ | Circuit breaker de 30–60 s |
| 5xx / overloaded | 1 reintento | ✅ | Circuit breaker |
| 401/403 auth | ❌ | ✅ | Cuenta → INVALID + notificación a administradores |
| Presupuesto excedido (propio) | ❌ | ✅ si `on_limit_behavior = FALLBACK`; si BLOCK → handoff | Alerta |
| 400 request inválido / contexto demasiado largo | ❌ | ❌ | Es un bug nuestro: error + handoff |
| Filtro de contenido del proveedor | ❌ | ❌ | Handoff |
| **"Respuesta mala"** | ❌ | ❌ **(§76 confirmado)** | Feedback, evaluación y mejora de prompt |

- **Orden de fallback:** otras cuentas del mismo proveedor y modelo → otro proveedor y modelo configurado. Un modelo de fallback **debe tener las mismas capacidades** (si el agente usa tools, el fallback debe `supports_tools`). El sistema valida esto al guardar la configuración.
- **Circuit breaker por cuenta** en Redis (cerrado / abierto / semiabierto). Cuando está abierto, la cuenta se salta sin gastar latencia.
- **Presupuesto total de latencia por turno** (p. ej., 45 s). Si se agota → handoff con el mensaje "un asesor te responderá en breve".
- **Límites (§77):** contadores en Redis por cuenta/día/mes (tokens y USD). Al 80 % → notificación (deduplicada). Al 100 % → BLOCK o FALLBACK según configuración, con registro en `audit_logs`.

### J.6 Memoria (§61)

| Capa | Qué contiene | Presupuesto de tokens aproximado |
|---|---|---|
| Núcleo + rol + reglas | Prompt de sistema compuesto | 1.5–3 k |
| Memoria larga | `ai_summaries(CONTACT_PROFILE)` vigente + hechos estructurados (`data_provenance` confirmados) | ≤ 400 |
| Resumen de conversación | `CONVERSATION_ROLLING` (se regenera cada K mensajes o cuando la ventana se llena) | ≤ 500 |
| Memoria corta | Últimos N mensajes (N configurable, p. ej., 20) **sin notas internas** para agentes públicos | 2–4 k |
| KB | Top-k chunks vía `search_knowledge_base` (bajo demanda) | ≤ 1.5 k |
| Tools | Esquemas de las tools habilitadas | 1–2 k |

- **No se envía todo el historial** ✅ (§61 confirmado). El perfil del cliente se regenera de forma asíncrona cuando una conversación pasa a RESOLVED.
- **La memoria nunca contiene precios ni stock:** esos datos caducan. Si el resumen dice "se le informó S/ 4 399", el agente está obligado a reconsultar antes de repetir la cifra (lo aplica el guardrail de cifras).

### J.7 Guardrails

**Entrada:**
- Los mensajes del cliente, los documentos de la KB y los resultados de tools van **delimitados** como datos no confiables.
- Límites por conversación y contacto (mensajes/minuto, turnos de IA por hora) para frenar abuso y ataques de coste.
- Detección heurística de inyección ("ignora tus instrucciones", "eres ahora…", "muéstrame tu prompt"). **No bloquea**: sube el riesgo y, si se repite, provoca un handoff.
- Las imágenes y audios pasan por transcripción/visión solo si el modelo lo soporta; el texto extraído se trata como no confiable.

**Salida (antes de enviar):**
1. **Verificación de cifras:** toda cifra monetaria (`S/`, `PEN`, `$`, números con formato de precio) y toda afirmación de stock presente en la respuesta **debe coincidir con un valor devuelto por una tool en este run**. Si no → la respuesta se bloquea y se reintenta una vez con feedback al modelo; si vuelve a fallar → handoff. **Es la implementación concreta de la regla absoluta del §41.**
2. **Detector de fugas:** costos (el costo nunca entra al contexto, pero se verifica igualmente), fragmentos del prompt de sistema, emails o teléfonos de otros contactos.
3. **Promesas prohibidas:** una lista configurable ("te lo garantizo", "te lo reservo" sin reserva real, fechas de entrega sin tool).
4. **Formato por canal:** longitud máxima y markdown permitido según el canal (WhatsApp tiene su propio formato).
5. **Divulgación:** [RECOMENDACIÓN, D-IA-6] identificar al asistente como virtual en el primer mensaje. Es buena práctica de transparencia y reduce el riesgo reputacional y regulatorio.

### J.8 Intent, sentimiento, confianza y extracción (§63–65, §28)

- Una llamada auxiliar de **clasificación con salida estructurada** (JSON Schema): `{intent, confidence, sentiment, requested_human: bool, entities: {product_query, capacity, color, budget, timeframe, trade_in}}`. Se ejecuta en el mismo run, antes de la respuesta, con un modelo barato.
- **La detección de "quiero hablar con un humano" (§17) no depende solo del LLM:** hay un detector determinista de palabras clave ("asesor", "humano", "persona", "hablar con alguien"…) **más** el LLM. Cualquiera de los dos dispara el handoff (OR, no AND).
- **Aviso sobre `confidence` (§65):** la confianza autodeclarada por un LLM está **mal calibrada**. Propongo tratarla como una señal combinada: `decision_score = f(confianza del LLM, intent en lista blanca, todas las tools OK, guardrails limpios, sentimiento no negativo)`. Los umbrales siguen siendo configurables por versión de agente ✅. Deben calibrarse con `ai_feedback` antes de habilitar el nivel 3 o 4 en producción.
- **Sentimiento (§64):** puede **subir la prioridad** y **provocar un handoff**, pero nunca bloquear, descontar ni tomar decisiones comerciales ✅ (confirmo tu criterio).
- **Extracción:** cada dato extraído se escribe en `data_provenance` con `source = AI_EXTRACTION`, `message_id` y `confidence`. Si el campo del lead lo había fijado un humano, **la IA no lo sobrescribe**: crea una propuesta que la UI muestra.

### J.9 Uso, coste y métricas (§78)

- El coste se calcula por llamada con `ai_model_prices` vigente en ese momento y se agrega a `ai_runs` → `ai_sessions` → `ai_usage_daily`.
- **Coste por conversación** = suma de `ai_sessions.cost_usd` de la conversación.
- KPI de calidad: % de runs AUTO_SENT, % de handoffs por motivo, % de sugerencias aceptadas sin edición, distancia de edición media, feedback negativo por cada 100 runs y bloqueos por guardrail.

### J.10 Kill switch (§79)

- Dos niveles: **plataforma** (variable en Redis + BD, solo staff) y **organización** (`organizations.ai_enabled`, permiso `ai.kill_switch`).
- Efecto inmediato: el Gatekeeper rechaza nuevos runs; los runs en curso se marcan SUPERSEDED antes del envío (revalidación en outbound); una tarea migra las conversaciones AI_AUTONOMOUS a HUMAN y a la cola por defecto (`reason = AI_DISABLED`); el copiloto también se desactiva. **Canales, webhooks e Inbox siguen funcionando.**
- Por agente: `ai_agents.status = PAUSED` produce el mismo efecto acotado.

### J.11 Evaluación y Supervisor IA (§76, §116)

- **Conjunto dorado:** conversaciones reales anonimizadas con el resultado esperado (tools que debe llamar, cifras correctas, handoff sí/no). Al **publicar** una versión de agente se ejecuta la evaluación offline y se muestra el diff frente a la versión anterior. Es P1, pero el modelo de datos queda listo desde el MVP.
- **Supervisor IA:** empieza como **reglas deterministas** en Celery beat (SQL): lead caliente sin seguimiento, cotización enviada sin respuesta, vendedor con carga > X, conversación esperando > SLA. Genera `notifications` y tareas. El LLM se añade después solo para matices (p. ej., "¿esta conversación necesita a un humano?"). Es más barato, más predecible y más fácil de probar.

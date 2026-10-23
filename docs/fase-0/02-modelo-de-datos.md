# E–F · Modelo de datos inicial y ERD textual

---

## E. Modelo de datos

> **Documento normalizado en la Fase 0.5 (2026-09-28):** el contenido de abajo es el modelo objetivo vigente y es coherente con los ADR-001…011. Es un **mapa**: cada tabla se crea en la fase que la usa (05 §S.1).

### E.0 Convenciones comunes (aplican a todas las tablas salvo que se indique lo contrario)

| Convención | Definición |
|---|---|
| **PK** | `id UUID` (UUIDv7, ordenable por tiempo). Nunca se exponen IDs secuenciales |
| **Tenant** | `organization_id UUID NOT NULL FK → organizations`, con índice. Es la **primera columna de todo índice compuesto y de toda restricción única**. Las tablas marcadas **[GLOBAL]** no tienen `organization_id` |
| **Timestamps** | `created_at timestamptz NOT NULL DEFAULT now()`, `updated_at timestamptz` |
| **Personas (ADR-001)** | Toda referencia de negocio a una persona se llama `*_user_id` y tiene **FK compuesta** `(organization_id, *_user_id) → organization_memberships(organization_id, user_id)`: la persona debe ser miembro de la misma organización. Excepciones: las tablas N:M de la propia membresía (`membership_roles`, `team_members`) referencian `membership_id` |
| **Autoría / actor** | `created_by_user_id`, `updated_by_user_id` (nullable). Cuando el actor puede no ser humano: `<x>_by_type` ∈ {USER, AI_AGENT, SYSTEM, INTEGRATION} + `<x>_by_user_id` + `<x>_by_ai_agent_id` (FKs explícitas; un CHECK exige coherencia con `<x>_by_type`). Nunca `<x>_by_id` polimórfico |
| **Referencias polimórficas** | Solo para **punteros informativos sin integridad de negocio** (`audit_logs.actor_id/entity_id`, `notifications.entity_type/entity_id`, `sync_jobs.target_type/target_id`, `timeline_events`). Nunca para relaciones de negocio (asignaciones, propietarios, pertenencia) |
| **Soft delete [SD]** | `deleted_at timestamptz NULL`, `deleted_by_user_id`. Las restricciones únicas son **índices parciales** `WHERE deleted_at IS NULL` para poder recrear un SKU borrado |
| **Bloqueo optimista [V]** | `version INT NOT NULL DEFAULT 1`; los `UPDATE` verifican la versión (evita pisar cambios concurrentes en asignaciones, etapas y cotizaciones) |
| **Dinero** | `amount NUMERIC(14,2)` + `currency CHAR(3)` (ISO 4217) |
| **Enums** | `VARCHAR` + `CHECK` (o `TextChoices` de Django). No se usan tipos ENUM de PostgreSQL: son difíciles de migrar |
| **JSONB** | Solo para datos realmente variables (payloads, metadata, configuración extensible). Nunca para datos que filtramos o unimos con frecuencia |
| **Inmutables [INM]** | Filas append-only: la aplicación no emite `UPDATE`/`DELETE`, y el rol de BD de la app tiene revocados esos privilegios donde sea viable |

---

### E.1 Plataforma y organización

**`organizations`** — Tenant raíz. Una empresa cliente del SaaS.
- Campos: `name`, `legal_name`, `slug` (único global), `tax_id` (RUC), `country` (PE), `timezone` (America/Lima), `default_currency` (PEN), `locale` (es-PE), `status` (TRIAL/ACTIVE/SUSPENDED/CANCELLED), `plan_code`, **`ai_enabled` bool** (interruptor de emergencia por organización, §79), `ai_disabled_reason`, `ai_disabled_at`, `ai_disabled_by_user_id`, [SD].
- Claves: PK `id`; UNIQUE `slug`.
- Relaciones: 1:N con prácticamente todo; 1:1 `organization_settings`.

**`organization_settings`** — Configuración tipada por organización (1:1). Evita hardcodear reglas.
- Campos: `organization_id` (PK+FK), `conversation_reopen_window_hours` (p. ej., 72), `ai_debounce_seconds` (4), `default_quote_validity_days` (7), `quote_followup_hours` (24), `stale_opportunity_days` (3), `tax_rate` (0.18), `prices_include_tax` bool, `rounding_rule`, `lead_score_thresholds` JSONB, `ai_confidence_defaults` JSONB, `business_hours_schedule_id` → `work_schedules`, `extra` JSONB (para configuraciones nuevas antes de tiparlas).

**`branches`** — Sucursales / tiendas físicas.
- Campos: `name`, `code`, `address`, `district`, `city`, `phone`, `timezone`, `is_active`, [SD].
- UNIQUE (`organization_id`, `code`). Relaciones: 1:N `warehouses`, `organization_memberships.default_branch`, `contacts.branch`, `orders`.

**`service_health_checks`** [GLOBAL + `organization_id` nullable] — Alimenta el panel "Estado de servicios" (§10).
- Campos: `component` (REDIS/WORKERS/WEBHOOKS/CHANNEL_ACCOUNT/AI_ACCOUNT/DB), `component_ref_id`, `status` (OK/DEGRADED/DOWN/NOT_CONNECTED), `latency_ms`, `detail` JSONB, `checked_at`. Se conserva el último estado + 7 días de historia.

**`impersonation_sessions`** [GLOBAL] — Soporte de plataforma entrando en una organización.
- Campos: `staff_user_id`, `organization_id`, `reason` (obligatorio), `started_at`, `expires_at` (máx. 60 min), `ended_at`. Todo lo que ocurra en la sesión se audita con `impersonated_by`.

---

### E.2 Identidad, membresías, equipos

**`users`** [GLOBAL] — Identidad de persona que inicia sesión. **No pertenece a una organización**: se vincula mediante membresías.
- Campos: `email` (citext, UNIQUE), `password` (Argon2), `first_name`, `last_name`, `phone_e164`, `avatar_platform_file_id` → `platform_files` (avatar **global**; nunca FK a `files`, que es tenant-owned), `locale`, `is_platform_staff` bool (superadmin de la plataforma, ver H), `is_active`, `mfa_enabled`, `last_login_at`, `password_changed_at`, [SD].
- [DECISIÓN D2] ¿Un usuario puede pertenecer a varias organizaciones? Recomiendo **sí** (agencias, consultores, dueños con varias empresas).

**`user_mfa_devices`** [GLOBAL] — TOTP y códigos de respaldo (django-otp).
- Campos: `user_id`, `type` (TOTP/BACKUP_CODES), `secret_encrypted`, `confirmed_at`, `last_used_at`.

**`user_invitations`** — Invitaciones a una organización.
- Campos: `email`, `role_ids` UUID[], `team_ids` UUID[], `token_hash` (SHA-256; el token en claro solo va en el email), `expires_at`, `accepted_at`, `invited_by_user_id`, `status` (PENDING/ACCEPTED/EXPIRED/REVOKED).
- UNIQUE parcial (`organization_id`, `email`) WHERE status = 'PENDING'.

**`organization_memberships`** — La relación usuario ↔ organización. Es el "usuario" desde el punto de vista del CRM (§91). **Toda FK de negocio a una persona apunta aquí mediante la FK compuesta `(organization_id, user_id)`** (convención E.0). RLS: política SELECT especial (ADR-002 §3.2).
- Campos: `user_id`, `status` (INVITED/ACTIVE/SUSPENDED/DEACTIVATED), `job_title`, `default_branch_id`, `work_schedule_id`, `max_concurrent_conversations` (para asignación por carga), `availability` (ONLINE/AWAY/OFFLINE, lo actualiza la presencia WS), `display_name_override` y `avatar_file_id` → `files` (opcionales: foto y nombre **por organización**, tenant-owned), `joined_at`, `deactivated_at`, [SD].
- UNIQUE (`organization_id`, `user_id`) — destino de las FKs compuestas `*_user_id`. Relaciones: N:M `roles` vía `membership_roles`; N:M `teams` vía `team_members`.

**`teams`** — Equipos (Ventas, Soporte, Postventa…).
- Campos: `name`, `slug`, `description`, `assignment_strategy` (MANUAL/ROUND_ROBIN/LOAD_BALANCED/SKILL_BASED/AI_RULES), `business_hours_schedule_id`, `is_active`, [SD].
- UNIQUE (`organization_id`, `slug`).

**`team_members`** — Integrantes.
- Campos: `team_id`, `membership_id`, `team_role` (MEMBER/SUPERVISOR), `is_active` (participa en la asignación automática), `last_assigned_at` (round robin), `skills` TEXT[] (especialidad: "mac", "mayorista").
- UNIQUE (`team_id`, `membership_id`).

**`work_schedules`** — Horarios reutilizables (usuarios, equipos, agentes IA, organización).
- Campos: `name`, `timezone`, `weekly_rules` JSONB (`[{weekday:1, from:"09:00", to:"19:00"}]`), `holidays` JSONB.

**`assignment_rules`** — Reglas de enrutamiento hacia equipos/usuarios/agentes (§93).
- Campos: `name`, `priority`, `conditions` JSONB (canal, intent, categoría de producto, etiqueta, horario), `target_team_id`, `target_user_id`, `target_ai_agent_id`, `is_active`.

---

### E.3 Control de acceso

**`permissions`** [GLOBAL, sembrada desde código] — Catálogo de permisos. **Los permisos los define el código** (el código es quien los verifica); la UI no crea permisos, solo los asigna a roles.
- Campos: `code` (PK, p. ej., `quotes.approve`), `module`, `description`, `is_sensitive` bool, `supports_scope` bool, `is_platform_only` bool.

**`roles`** — Roles por organización. Al crear una organización se clonan las plantillas del sistema.
- Campos: `code`, `name`, `description`, `is_system` (plantilla sembrada: no se borra y su código no cambia), `is_owner_role` bool, [SD].
- UNIQUE (`organization_id`, `code`).

**`role_permissions`** — Permiso asignado a rol, **con alcance**.
- Campos: `role_id`, `permission_code`, `scope` (OWN/TEAM/BRANCH/ORGANIZATION; NULL si el permiso no admite alcance). Semántica en ADR-003 §5.
- UNIQUE (`role_id`, `permission_code`).

**`membership_roles`** — N:M membresía ↔ rol.
- UNIQUE (`membership_id`, `role_id`).

**`discount_policies`** — Límites de descuento por rol (§50).
- Campos: `role_id`, `max_discount_percent` NUMERIC(5,2), `max_discount_amount` (opcional), `currency`, `applies_to_price_list_id` (NULL = todas).
- UNIQUE (`organization_id`, `role_id`, `applies_to_price_list_id`). Si un usuario tiene varios roles, **se aplica el límite más alto**.

---

### E.4 Archivos

**`files`** — Metadatos de archivos **de una organización** (tenant-owned) guardados en el object storage (ADR-008).
- Campos: `storage_key` (`org/{org_id}/{yyyy}/{mm}/{uuid}`), `original_name`, `mime_type`, `size_bytes`, `sha256`, `purpose` (MESSAGE_MEDIA/QUOTE_PDF/IMPORT/AVATAR/KB/ATTACHMENT), `uploaded_by_*` (actor, E.0), `scan_status` (PENDING/CLEAN/INFECTED/SKIPPED), [SD].
- Nunca se sirve con URL pública: solo con URL firmada de corta duración tras verificar permisos.

**`platform_files`** [GLOBAL] — Metadatos de objetos **platform-owned** que no pertenecen a ninguna organización (hoy: avatar global del usuario; mañana: recursos de la plataforma).
- Campos: `storage_key` (`platform/{purpose}/{yyyy}/{mm}/{uuid}`), `purpose` (USER_AVATAR/…), `owner_user_id` → `users` (si aplica), `mime_type`, `size_bytes`, `sha256`, `scan_status`, `created_at`, [SD].
- Sin RLS de tenant; acceso solo mediante servicios de `accounts`/`platform`. El avatar global lo ve cualquier organización donde el usuario es miembro; si la organización quiere otra imagen, usa `organization_memberships.avatar_file_id`.

---

### E.5 Contactos

**`contacts`** — Perfil central único del cliente (principio 1).
- Campos: `display_name`, `first_name`, `last_name`, `primary_phone_e164`, `primary_email` (citext), `document_type` (DNI/CE/RUC/PASSPORT), `document_number`, `avatar_file_id`, `owner_user_id` (responsable), `branch_id`, `source` (INBOUND_MESSAGE/WALK_IN/REFERRAL/IMPORT/MANUAL/CAMPAIGN/AI), `origin_platform`, `segment` (texto controlado o FK a catálogo en el futuro), `customer_type` (NEW/RECURRING, derivado de `orders`), `is_blocked` (los consentimientos viven en `contact_consents`), `custom_fields` JSONB, `first_seen_at`, `last_interaction_at`, `merged_into_id` (FK self, si fue fusionado), `search_vector` tsvector, [SD], [V].
- Índices: (`organization_id`, `primary_phone_e164`), GIN trigram sobre `display_name`, GIN sobre `search_vector`. **No** se pone UNIQUE sobre el teléfono: los duplicados se gestionan, no se impiden a ciegas (familias que comparten número, números reciclados).

**`contact_identities`** — Identidades digitales (§22). Sustituyen a columnas del tipo `contact.whatsapp`.
- Campos: `contact_id`, `platform` (WHATSAPP/INSTAGRAM/FACEBOOK/TIKTOK/EMAIL/PHONE/TELEGRAM/WEBCHAT/OTHER), `channel_account_id` (nullable; **obligatorio para IDs con alcance de página/negocio**, como IGSID/PSID y el BSUID de WhatsApp), `external_id`, `username`, `display_name`, `profile_picture_url`, `metadata` JSONB, `is_verified`, `verified_method` (CHANNEL/OTP/MANUAL), `first_seen_at`, `last_seen_at`.
- UNIQUE NULLS NOT DISTINCT (`organization_id`, `platform`, `channel_account_id`, `external_id`) (PostgreSQL ≥ 15).
- Nota: un PSID de Messenger y un IGSID de Instagram **no son comparables entre sí** ni con el teléfono. La unificación entre plataformas se hace por sugerencia más confirmación (ver `contact_merge_candidates`).

**`contact_consents`** — Consentimiento vigente por contacto, canal y propósito (sustituye a un único `marketing_opt_in`). Lo consulta `MessagingPolicyService` (ADR-010).
- Campos: `contact_id`, `contact_identity_id` (nullable: el consentimiento puede ligarse a una identidad concreta, p. ej. un número de WhatsApp), `channel` (WHATSAPP/INSTAGRAM/MESSENGER/TIKTOK/EMAIL/SMS/PHONE/ANY), `purpose` (MARKETING/PROMOTIONS/FOLLOW_UP/TRANSACTIONAL/SURVEYS/ALL_PROACTIVE…; texto controlado y extensible), `status` (GRANTED/REVOKED/UNKNOWN), `source` (CHANNEL_OPT_IN/CUSTOMER_MESSAGE/WEB_FORM/IN_STORE/IMPORT/AGENT_RECORDED/KEYWORD_STOP), `granted_at`, `revoked_at`, `expires_at` (nullable), `policy_version` (texto o versión de la cláusula aceptada), `evidence` JSONB (message_id, formulario, IP, operador…), `recorded_by_*` (actor, E.0), `updated_at`, [V].
- UNIQUE (`organization_id`, `contact_id`, `channel`, `purpose`) — una fila de **estado vigente** por combinación. `channel = ANY` o `purpose = ALL_PROACTIVE` expresan exclusiones globales (p. ej., el cliente escribe "STOP").

**`contact_consent_events`** [INM] — Historial de cada cambio (concesión, revocación, expiración) con el mismo detalle de `source`, `evidence` y actor. Es la prueba ante un reclamo; `contact_consents` es la proyección rápida para decidir.

**`contact_addresses`** — Direcciones de entrega.
- Campos: `contact_id`, `label`, `line1`, `line2`, `district`, `city`, `region`, `country`, `reference`, `latitude`, `longitude`, `is_default`.

**`tags`** — Etiquetas configurables.
- Campos: `name`, `color`, `applies_to` (CONTACT/CONVERSATION/OPPORTUNITY), `is_active`. UNIQUE (`organization_id`, `applies_to`, lower(`name`)).

**`contact_tags`** — N:M. Campos: `contact_id`, `tag_id`, `added_by_*` (actor, E.0). UNIQUE (`contact_id`, `tag_id`).
(Existen tablas análogas `conversation_tags` y `opportunity_tags`: **FKs explícitas en lugar de una tabla polimórfica**, para mantener la integridad referencial.)

**`notes`** — Notas sobre el cliente (no confundir con notas internas del chat, que son mensajes).
- Campos: `contact_id` (NOT NULL), `opportunity_id`, `lead_id`, `quote_id` (opcionales), `body`, `author_*` (actor, E.0), `is_pinned`, [SD].

**`contact_merge_candidates`** — Posibles duplicados (§23).
- Campos: `contact_a_id`, `contact_b_id` (siempre a < b), `score` (0–100), `match_reasons` JSONB (`["same_phone","similar_name","same_email"]`), `status` (PENDING/MERGED/DISMISSED), `resolved_by_user_id`, `resolved_at`.
- UNIQUE (`organization_id`, `contact_a_id`, `contact_b_id`).

**`contact_merges`** [INM] — Registro de fusiones.
- Campos: `survivor_contact_id`, `merged_contact_id`, `merged_snapshot` JSONB (estado completo del contacto absorbido), `moved_counts` JSONB (conversaciones, leads, etc. reasignados), `performed_by_user_id`, `performed_at`.
- El contacto absorbido queda con `merged_into_id` y `deleted_at` (no se borra físicamente).

**`timeline_events`** [INM] — Proyección del historial del cliente (§24). Se escribe desde los eventos de dominio; evita un `UNION` de 10 tablas en cada carga.
- Campos: `contact_id`, `event_type` (`message.received`, `lead.created`, `opportunity.stage_changed`, `quote.sent`, `ai.handoff_requested`…), `actor_type/id`, `conversation_id`, `lead_id`, `opportunity_id`, `quote_id`, `task_id`, `order_id` (nullable), `summary` (texto corto ya renderizado), `payload` JSONB, `occurred_at`.
- Índice: (`organization_id`, `contact_id`, `occurred_at DESC`). Los mensajes individuales **no** se proyectan uno a uno: se agrupan por conversación y día para no inflar la tabla.

**`data_provenance`** — Trazabilidad de datos extraídos (§28, `source = AI_EXTRACTION`).
- Campos: `entity_type` (LEAD/CONTACT/OPPORTUNITY), `entity_id`, `field` (`budget_amount`, `product_variant_id`…), `value` JSONB, `source` (USER/AI_EXTRACTION/IMPORT/CHANNEL_PROFILE/SYSTEM), `ai_run_id`, `message_id`, `confidence`, `confirmed_by_user_id`, `confirmed_at`, `superseded_at`.
- Índice: (`organization_id`, `entity_type`, `entity_id`, `field`). La UI muestra una marca "✨ Extraído por IA" con enlace al mensaje de origen.

---

### E.6 Canales e Inbox

> **[RECOMENDACIÓN]** No crear una tabla `channels` como la del §96. Los tipos de canal son un enum en código (cada uno necesita un adapter en código). La entidad configurable es la **cuenta** (`channel_accounts`).

**`channel_accounts`** — Un número de WhatsApp, una página de Facebook, una cuenta de IG o de TikTok conectada.
- Campos: `platform`, `name`, `external_account_id` (phone_number_id / page_id / ig_user_id / tiktok business id), `external_parent_id` (WABA id / business id), `display_identifier` (+51 9…, @usuario), `credential_id` → `credentials`, `webhook_verify_token_ref`, `status` (PENDING/CONNECTED/DEGRADED/DISCONNECTED/ERROR), `status_detail`, `default_team_id`, `default_ai_agent_id`, `settings` JSONB (política de reapertura propia, mensaje fuera de horario…), `quality_rating` (WhatsApp), `messaging_limit_tier`, `last_healthcheck_at`, [SD].
- **UNIQUE global** (`platform`, `external_account_id`) WHERE deleted_at IS NULL: el webhook resuelve la organización a partir de este ID, y una misma cuenta no puede pertenecer a dos organizaciones.

**`message_templates`** — Plantillas de WhatsApp (obligatorias fuera de la ventana de 24 h).
- Campos: `channel_account_id`, `name`, `language`, `category` (MARKETING/UTILITY/AUTHENTICATION), `status` (APPROVED/PENDING/REJECTED/PAUSED/DISABLED), `components` JSONB, `variables_schema` JSONB, `external_template_id`, `purpose` (QUOTE_SENT/FOLLOW_UP/REENGAGE/…), `last_synced_at`.
- UNIQUE (`channel_account_id`, `name`, `language`).

**`conversations`** — Un hilo con un contacto en una cuenta de canal.
- Campos:
  - Identidad: `number` (secuencia por organización → `CONV-00191`), `contact_id`, `contact_identity_id`, `channel_account_id`, `platform` (denormalizado para filtros).
  - Estado: `status` (OPEN/PENDING/WAITING_CUSTOMER/WAITING_INTERNAL/RESOLVED/CLOSED), `is_archived` bool, `snoozed_until`.
  - Atención: `attention_mode` (AI_AUTONOMOUS/AI_ASSISTED/HUMAN).
  - Asignación (ADR-007): `assigned_team_id`, `assigned_user_id`, `assigned_ai_agent_id` (desde la Fase 8), `assignee_kind` (UNASSIGNED/TEAM/USER/AI_AGENT, columna generada), `assignment_version`, `assigned_at`, con `CHECK NOT (assigned_user_id IS NOT NULL AND assigned_ai_agent_id IS NOT NULL)`. Solo `AssignmentService` escribe estas columnas.
  - Handoff: `handoff_status` (NONE/REQUESTED/ACCEPTED) + `active_handoff_id`.
  - Prioridad y SLA: `priority` (LOW/NORMAL/HIGH/URGENT), `first_inbound_at`, `first_response_at`, `last_inbound_at`, `last_outbound_at`, `waiting_since` (NULL si no hay nada pendiente de responder), `customer_window_expires_at` (**dato denormalizado para UI e índices**; `MessagingPolicyService` no lo usa para autorizar: recalcula desde `last_inbound_at` + capacidades vigentes del adapter justo antes de cada envío, ADR-010).
  - Resumen para la lista: `last_message_at`, `last_message_preview` (sin notas internas), `last_message_direction`.
  - IA/análisis: `last_intent_code`, `last_intent_confidence`, `sentiment` (POSITIVE/NEUTRAL/UPSET/VERY_UPSET), `current_summary_id`.
  - Vínculos: `lead_id`, `opportunity_id` (principal), `referral` JSONB (anuncio Click-to-WhatsApp de origen).
  - Cierre: `resolved_at`, `closed_at`, `closed_by_*` (actor, E.0), `close_reason`.
  - [V] (versión para asignación y estado concurrentes).
- Índices: (`organization_id`, `status`, `last_message_at DESC`); (`organization_id`, `assigned_user_id`, `status`); (`organization_id`, `assigned_team_id`, `status`); parcial sobre `waiting_since` WHERE `waiting_since IS NOT NULL`. UNIQUE parcial (`organization_id`, `contact_identity_id`, `channel_account_id`) WHERE status NOT IN ('CLOSED'): **como máximo una conversación no cerrada por identidad y cuenta** (evita duplicados por webhooks concurrentes).
- Sin soft delete: las conversaciones se archivan. Borrar es una operación administrativa excepcional (derecho de supresión).

**`conversation_participants`** — Estado por usuario (no leídos, seguimiento).
- Campos: `conversation_id`, `user_id`, `role` (ASSIGNEE/COLLABORATOR/WATCHER/MENTIONED), `last_read_message_id`, `last_read_at`, `unread_count`.
- UNIQUE (`conversation_id`, `user_id`).

**`conversation_assignments`** [INM] — Historial de asignaciones y transferencias (§16, ADR-007). El estado actual vive en `conversations`.
- Campos: `conversation_id`, `previous_team_id`, `previous_user_id`, `previous_ai_agent_id`, `new_team_id`, `new_user_id`, `new_ai_agent_id`, `reason_code` (MANUAL/TAKE/TRANSFER/ROUND_ROBIN/LOAD_BALANCE/SKILL_RULE/AI_ROUTING/HANDOFF/SUPERVISOR_TAKEOVER/USER_DEACTIVATED/AI_DISABLED/REOPEN), `reason_note`, `internal_comment`, `priority_at_assignment`, `assigned_by_user_id`, `assigned_by_ai_agent_id`, `assigned_by_system` bool, `handoff_id`, `summary_id` → `ai_summaries`, `created_at`.
- `CHECK`: exactamente uno de `assigned_by_user_id`, `assigned_by_ai_agent_id` o `assigned_by_system = true`. Sin UPDATE/DELETE para `crm_app`.

**`conversation_status_history`** [INM] — Cambios de estado con autor y tiempo en el estado anterior (métricas de atención).

**`messages`** — Todo lo que aparece en el chat.
- Campos: `conversation_id`, `direction` (INBOUND/OUTBOUND/INTERNAL), `sender_type` (CONTACT/USER/AI_AGENT/SYSTEM), `sender_user_id`, `sender_ai_agent_id`, `message_type` (TEXT/IMAGE/AUDIO/VIDEO/DOCUMENT/STICKER/LOCATION/CONTACTS/TEMPLATE/INTERACTIVE/REACTION/SYSTEM_EVENT/NOTE), `body` (texto plano), `content` JSONB (estructura del tipo: botones, ubicación, plantilla y variables), `transcript` (texto de notas de voz), `reply_to_message_id`, `external_message_id`, `client_message_id` (idempotencia desde el frontend), `ai_run_id`, `ai_suggestion_id`, `delivery_status` (PENDING/QUEUED/SENT/DELIVERED/READ/FAILED; NULL para INBOUND/INTERNAL), salida (ADR-010): `send_mode` (FREE_FORM/TEMPLATE/HUMAN_AGENT_TAG), `template_id`, `template_language`, `template_variables` JSONB, `policy_decision` JSONB (snapshot de la decisión), `provider_error_code`, `provider_error_title`, `provider_error_detail`, `failed_at`, `external_timestamp`, `sent_at`, `delivered_at`, `read_at`, `created_at`.
- Restricciones:
  - UNIQUE parcial (`channel_account_id`, `external_message_id`) WHERE `external_message_id IS NOT NULL` (deduplicación de webhooks; `channel_account_id` se denormaliza).
  - UNIQUE parcial (`organization_id`, `client_message_id`).
  - `CHECK (direction <> 'INTERNAL' OR (external_message_id IS NULL AND sender_type IN ('USER','AI_AGENT','SYSTEM')))`.
  - `CHECK (message_type <> 'NOTE' OR direction = 'INTERNAL')`.
- Índice: (`conversation_id`, `created_at`). Particionar por mes cuando supere ~50 M filas.

**`message_attachments`** — Media y documentos.
- Campos: `message_id`, `file_id` (NULL hasta descargar), `kind`, `mime_type`, `size_bytes`, `external_media_id`, `download_status` (PENDING/DONE/FAILED/EXPIRED), `caption`, `duration_ms`, `width`, `height`.

**`message_status_events`** [INM] — Historial de estados de entrega (lo que el §96 llama `message_statuses`).
- Campos: `message_id`, `status`, `external_timestamp`, `error` JSONB, `received_at`, `webhook_ingress_id`.
- Regla: `messages.delivery_status` **solo avanza** (SENT → DELIVERED → READ). Un DELIVERED que llega tarde después de READ no retrocede el estado. FAILED es terminal.

**`quick_reply_categories`** — Ventas, Garantía, Delivery… (configurable).

**`quick_replies`** — Respuestas rápidas (§20).
- Campos: `shortcut` (`garantia`), `title`, `category_id`, `body` (con variables `{{contact.first_name}}`, `{{org.address}}`), `attachment_file_ids` UUID[], `visibility` (ORG/TEAM/PERSONAL), `team_id`, `owner_user_id`, `is_active`, `usage_count`, [SD].
- UNIQUE parcial (`organization_id`, `shortcut`) WHERE visibility = 'ORG' AND deleted_at IS NULL.
- **Regla:** una respuesta rápida **no puede contener precios fijos** (validación con aviso si detecta `S/ 1234`). Los precios salen del motor.

---

### E.7 Catálogo

**`brands`** — `name`, `slug`, `logo_file_id`, [SD]. UNIQUE (`organization_id`, `slug`).

**`categories`** — Árbol de categorías.
- Campos: `parent_id` (self), `name`, `slug`, `path` (materialized path `/celulares/iphone/`), `sort_order`, [SD]. UNIQUE (`organization_id`, `path`).

**`products`** — Producto comercial (iPhone 16 Pro) o servicio (Cambio de batería).
- Campos: `product_type` (PHYSICAL/SERVICE), `brand_id`, `category_id`, `name`, `slug`, `description`, `short_description` (para IA/cotización), `status` (DRAFT/ACTIVE/DISCONTINUED), `tracks_inventory` bool (false para servicios), `warranty_months`, `warranty_terms`, `ai_visible` bool (si la IA puede ofrecerlo), `compatible_product_id` (servicios: el equipo al que aplica, p. ej., batería → iPhone 13), `search_vector`, [SD].
- UNIQUE (`organization_id`, `slug`).

**`product_options`** — Dimensiones de variante por producto: Capacidad, Color, Condición, Calidad (GX OLED/Original).
- Campos: `product_id`, `name`, `code`, `sort_order`. UNIQUE (`product_id`, `code`).

**`product_option_values`** — `option_id`, `value` ("256 GB", "Negro"), `sort_order`. UNIQUE (`option_id`, `value`).

**`product_variants`** — Unidad vendible. **Todo precio, stock y línea de cotización apunta a la variante, nunca al producto.**
- Campos: `product_id`, `sku`, `barcode`, `name` (autogenerado: "iPhone 16 Pro 256 GB Negro"), `condition` (NEW/OPEN_BOX/REFURBISHED/USED), `status` (ACTIVE/INACTIVE/DISCONTINUED), `ai_visible`, `sort_order`, [SD].
- UNIQUE parcial (`organization_id`, `sku`) WHERE deleted_at IS NULL.
- Un producto sin opciones tiene **una variante por defecto** (así el modelo es uniforme).

**`variant_option_values`** — N:M variante ↔ valor de opción. UNIQUE (`variant_id`, `option_id`), lo que garantiza un valor por dimensión.

**`product_search_aliases`** — Sinónimos para búsqueda humana y de IA ("16 pro max", "ip16pm", "promax 16").
- Campos: `product_id`, `variant_id` (nullable), `alias`. Índice trigram sobre `alias`.

**`product_media`** — `product_id`, `variant_id`, `file_id`, `sort_order`, `is_primary`.

---

### E.8 Precios

> **[INCONSISTENCIA resuelta]** El §38 incluye "promoción" como `price_type` y el §42 define promociones con fechas e ítems. Si existen ambos, hay dos fuentes de verdad para un precio promocional. **Propuesta:** `product_prices` guarda solo precios de **listas**; las promociones viven únicamente en `promotions` y el motor las aplica sobre la lista. Los "tipos de precio" del §38 pasan a ser **listas de precios**.

**`price_lists`** — Listas: Regular, Efectivo, Transferencia, Mayorista, Distribuidor, Interno.
- Campos: `code`, `name`, `kind` (RETAIL/CASH/TRANSFER/WHOLESALE/DISTRIBUTOR/INTERNAL/CUSTOM), `currency`, `is_default` (una por organización y moneda), `required_permission` (p. ej., `prices.view_wholesale`; NULL = visible con `prices.view`), `customer_segment` (tipo de cliente al que aplica; NULL = todos), `ai_exposable` bool (si un agente público puede comunicar precios de esta lista), `payment_condition` (NULL/CASH/BANK_TRANSFER/CARD…), `base_price_list_id` + `derived_adjustment_percent` (opcional: "Efectivo = Regular − 3 %"), `is_active`, `sort_order`, [SD].
- UNIQUE (`organization_id`, `code`). UNIQUE parcial (`organization_id`, `currency`) WHERE is_default.

**`product_prices`** [INM salvo cierre de vigencia] — Precio de una variante en una lista durante un intervalo.
- Campos: `variant_id`, `price_list_id`, `amount`, `currency`, `tax_included` bool, `starts_at`, `ends_at` (NULL = indefinido), `change_reason`, `change_batch_id` → `price_change_batches`, `previous_price_id` (FK self, para historial directo), `created_by_user_id`, `created_at`.
- **Restricción clave:** `EXCLUDE USING gist (organization_id WITH =, variant_id WITH =, price_list_id WITH =, tstzrange(starts_at, ends_at, '[)') WITH &&)` (extensión `btree_gist`). La base **impide** dos precios vigentes solapados.
- Cambiar un precio = cerrar el vigente (`ends_at = now()`) + insertar uno nuevo, en la misma transacción. **El historial del §43 es esta misma tabla**; no se necesita una tabla `price_history` aparte (evita duplicación). Los precios futuros programados se crean con `starts_at` en el futuro.

**`variant_costs`** — Costo de la variante (§39). **Tabla separada a propósito**, no una lista más:
- Campos: `variant_id`, `amount`, `currency`, `starts_at`, `ends_at`, `source` (MANUAL/IMPORT/PURCHASE), `change_reason`, `created_by_user_id`. Misma restricción EXCLUDE.
- Motivo: ningún serializer, selector de precios ni tool de IA hace JOIN con esta tabla. El acceso pasa por un único selector que exige `product_cost.view`. Es mucho más difícil filtrar un costo por accidente que si fuese una lista llamada "COST".

**`promotions`** — Reglas promocionales (§42).
- Campos: `name`, `code` (opcional, cupón futuro), `description`, `terms` (texto para cliente/IA), `discount_type` (FIXED_PRICE/PERCENT_OFF/AMOUNT_OFF), `value`, `currency`, `starts_at`, `ends_at` (NOT NULL: no hay promociones eternas), `status` (DRAFT/PUBLISHED/CANCELLED; "activa" es un estado **derivado**: PUBLISHED y now() dentro de la vigencia), `priority`, `stackable` bool (default false), `ai_exposable` bool, `channels` TEXT[] (NULL = todos), `max_units` (opcional), `created_by_user_id`, [SD].

**`promotion_items`** — A qué aplica. Un objetivo por fila:
- Campos: `promotion_id`, `variant_id`, `product_id`, `category_id`, `brand_id` (exactamente uno NOT NULL, con CHECK), `override_value` (valor específico para ese ítem).

**`promotion_price_lists`** — Sobre qué listas aplica (por defecto, la lista default). UNIQUE (`promotion_id`, `price_list_id`).

**`price_change_batches`** — Edición masiva (§44) e importación de precios (§45).
- Campos: `kind` (BULK_ADJUST/IMPORT/MANUAL_MULTI), `status` (DRAFT/PREVIEWED/APPLYING/APPLIED/CANCELLED/FAILED), `params` JSONB (`{op: "PERCENT_DECREASE", value: 5, filters: {...}, rounding: "0.90"}`), `reason` (obligatorio), `effective_at`, `import_id`, `summary` JSONB (conteos), `created_by_user_id`, `applied_by_user_id`, `applied_at`, [V].

**`price_change_batch_items`** — Preview línea a línea.
- Campos: `batch_id`, `variant_id`, `price_list_id`, `current_price_id`, `old_amount`, `new_amount`, `delta_percent`, `status` (PENDING/APPLIED/SKIPPED/ERROR/STALE), `error`. `STALE` = el precio cambió entre el preview y la aplicación: no se aplica a ciegas.

---

### E.9 Inventario

**`warehouses`** — Almacenes / stock de tienda.
- Campos: `branch_id`, `code`, `name`, `type` (STORE/WAREHOUSE/TRANSIT/SERVICE), `is_sellable` (cuenta para el stock disponible que ven la IA y el cliente), `is_active`, [SD].

**`inventory_levels`** — Lo que el §96 llama `inventory`. Saldo por variante y almacén.
- Campos: `variant_id`, `warehouse_id`, `on_hand` INT, `reserved` INT, `available` (columna generada `on_hand - reserved`), `low_stock_threshold`, `updated_at`, [V].
- `CHECK (on_hand >= 0 AND reserved >= 0 AND reserved <= on_hand)`. UNIQUE (`variant_id`, `warehouse_id`).
- Solo se modifica mediante el servicio de inventario, con `SELECT … FOR UPDATE`, y **siempre junto a un movimiento**.

**`inventory_movements`** [INM] — Ledger (§46).
- Campos: `variant_id`, `warehouse_id`, `movement_type` (RECEIPT/ISSUE/ADJUSTMENT/TRANSFER_OUT/TRANSFER_IN/RESERVE/RELEASE/SALE), `quantity` (con signo sobre la magnitud afectada), `on_hand_after`, `reserved_after`, `reference_type` (ORDER/QUOTE/RESERVATION/TRANSFER/IMPORT/MANUAL), `reference_id`, `transfer_group_id`, `reason`, `idempotency_key` (UNIQUE por organización), `created_by_*` (actor, E.0), `created_at`.

**`inventory_reservations`** — Reservas con vencimiento.
- Campos: `variant_id`, `warehouse_id`, `quantity`, `status` (ACTIVE/RELEASED/CONSUMED/EXPIRED), `quote_id`, `order_id`, `opportunity_id`, `expires_at`, `created_by_*` (actor, E.0).
- [DECISIÓN D-INV-2] ¿Cuándo se reserva? Mi recomendación: al **aceptar** la cotización (o al confirmar el pedido), con vencimiento configurable, no al crear el borrador.

> Seriales/IMEI: **no** se incluyen en el MVP (§105: inventario no complejo). El modelo queda preparado para añadir `inventory_units(imei, variant_id, warehouse_id, status)` sin romper nada [D-INV-1].

---

### E.10 Comercial

**`leads`** — Interés comercial en calificación (§25).
- Campos: `contact_id`, `conversation_id` (origen), `product_id`, `product_variant_id`, `source` (INBOUND_MESSAGE/CAMPAIGN/REFERRAL/WALK_IN/IMPORT/MANUAL/AI), `platform`, `channel_account_id`, `campaign_ref` JSONB (referral de anuncios de Meta, UTM), `assigned_user_id`, `team_id`, `status` (NEW/CONTACTED/QUALIFYING/QUALIFIED/DISQUALIFIED/CONVERTED), `interest_level` (LOW/MEDIUM/HIGH, derivado del score), `score` SMALLINT CHECK 0–100, `score_updated_at`, `budget_amount`, `budget_currency`, `purchase_timeframe` (TODAY/THIS_WEEK/THIS_MONTH/LATER/UNKNOWN), `trade_in_description`, `next_action`, `next_action_at`, `disqualification_reason`, `converted_opportunity_id`, `converted_at`, `last_activity_at`, [SD], [V].
- UNIQUE parcial (`organization_id`, `contact_id`, `product_id`) WHERE status NOT IN ('CONVERTED','DISQUALIFIED') AND deleted_at IS NULL: **un lead abierto por contacto y producto** (idempotencia de `create_lead` de la IA; si ya existe, se actualiza).

**`lead_score_events`** [INM] — Explica el score (§27).
- Campos: `lead_id`, `signal_code` (ASKED_PRICE/ASKED_STOCK/ASKED_DELIVERY/MENTIONED_BUDGET/ASKED_PAYMENT_METHODS/REQUESTED_QUOTE/STATED_PURCHASE_INTENT/NO_RESPONSE_48H…), `points` (±), `source` (RULE/AI_EXTRACTION/USER), `message_id`, `ai_run_id`, `created_at`.
- El score es la suma acotada a 0–100 con decaimiento temporal. La tabla de puntos por señal es configurable (`organization_settings.lead_score_thresholds` o una tabla `lead_score_rules` en P1).

**`pipelines`** — `name`, `code`, `kind` (RETAIL/WHOLESALE/TECH_SERVICE/OTHER), `currency`, `is_default`, `sort_order`, `is_active`, [SD].

**`pipeline_stages`** — Etapas configurables (§30).
- Campos: `pipeline_id`, `name`, `code`, `sort_order`, `stage_type` (OPEN/WON/LOST), `default_probability` (0–100), `stale_after_days` (sobrescribe la configuración general), `color`, `requires_fields` JSONB (p. ej., en "Cotización" se exige una cotización vinculada), `is_active`.
- Invariantes (validadas en el servicio): exactamente 1 etapa WON y al menos 1 LOST por pipeline; no se desactiva una etapa con oportunidades abiertas sin migrarlas.

**`opportunities`** — Posible venta (§29).
- Campos: `number` (`OPP-000123`), `contact_id`, `lead_id`, `conversation_id`, `pipeline_id`, `stage_id`, `status` (OPEN/WON/LOST, derivado del tipo de etapa y guardado para indexar), `assigned_user_id`, `team_id`, `branch_id`, `title`, `amount`, `currency`, `probability`, `priority`, `source`, `platform`, `next_action`, `next_action_at`, `expected_close_date`, `last_activity_at`, `stage_entered_at`, `won_at`, `lost_at`, `lost_reason_id`, `lost_reason_note`, `competitor`, `won_order_id`, [SD], [V].
- Índices: (`organization_id`, `pipeline_id`, `stage_id`, `status`); (`organization_id`, `assigned_user_id`, `status`); (`organization_id`, `last_activity_at`) WHERE status = 'OPEN' (detección de oportunidades estancadas).

**`opportunity_items`** — Productos de interés (no son precios firmes).
- Campos: `opportunity_id`, `variant_id`, `quantity`, `estimated_unit_price` (referencial), `notes`.

**`opportunity_stage_history`** [INM] — `opportunity_id`, `from_stage_id`, `to_stage_id`, `changed_by_*` (actor, E.0), `changed_at`, `seconds_in_previous_stage`.

**`lost_reasons`** — Catálogo configurable (§34): `code`, `label`, `requires_note`, `is_active`, `sort_order`. Se siembran: PRICE, OUT_OF_STOCK, COMPETITOR, NO_RESPONSE, FINANCING, CHANGED_MODEL, CHANGED_MIND, OTHER.

**`quotes`** — Cotización (§47).
- Campos: `number` (`COT-000123`), `revision` INT, `revision_of_id` (FK self), `contact_id`, `opportunity_id`, `lead_id`, `conversation_id`, `price_list_id`, `branch_id`, `status` (DRAFT/PENDING_APPROVAL/APPROVED/SENT/VIEWED/ACCEPTED/REJECTED/EXPIRED/CANCELLED), `currency`, `tax_mode` (INCLUDED/EXCLUDED), `tax_rate`, `subtotal`, `discount_total`, `tax_total`, `total`, `valid_until`, `terms_snapshot`, `warranty_snapshot`, `company_snapshot` JSONB (logo, razón social, contacto en el momento de emitir), `customer_snapshot` JSONB, `notes_to_customer`, `internal_notes`, `created_by_*` (actor, E.0) (USER/AI_AGENT), `approved_by_user_id`, `approved_at`, `sent_at`, `sent_message_id`, `viewed_at`, `accepted_at`, `rejected_at`, `rejection_reason`, `cancelled_at`, `pdf_file_id`, `public_token_hash`, `public_token_expires_at`, [V].
- **Inmutabilidad:** a partir de APPROVED no se editan ítems ni totales; cualquier cambio crea una **revisión** (nueva fila con `revision + 1`) y la anterior pasa a CANCELLED.
- UNIQUE (`organization_id`, `number`, `revision`).

**`quote_items`** [INM tras emisión] — Snapshot completo (§49).
- Campos: `quote_id`, `sort_order`, `variant_id`, `product_name_snapshot`, `variant_name_snapshot`, `sku_snapshot`, `description_snapshot`, `quantity`, `list_unit_price` (precio de lista resuelto), `price_id_snapshot`, `promotion_id_snapshot`, `promotion_name_snapshot`, `unit_price` (tras la promoción), `manual_price_override` bool + `override_reason`, `discount_type` (NONE/PERCENT/AMOUNT), `discount_value`, `discount_amount`, `final_unit_price`, `line_total`, `currency`, `warranty_months_snapshot`.

**`quote_approvals`** — Flujo de aprobación (§50).
- Campos: `quote_id`, `reason` (DISCOUNT_OVER_LIMIT/MANUAL_PRICE/AI_CREATED), `requested_by_*` (actor, E.0), `requested_discount_percent`, `limit_percent_at_request`, `status` (PENDING/APPROVED/REJECTED/CANCELLED), `decided_by_user_id`, `decided_at`, `comment`.

**`payment_methods`** — Efectivo, transferencia, tarjeta, Yape, Plin… `code`, `name`, `is_active`, `sort_order`.

**`orders`** — **Venta registrada** (§33, menú "Ventas > Ventas"). **No es facturación.**
- Campos: `number` (`VTA-000045`), `contact_id`, `opportunity_id`, `quote_id`, `branch_id`, `seller_user_id`, `status` (CONFIRMED/PAID/DELIVERED/CANCELLED), `currency`, `subtotal`, `discount_total`, `tax_total`, `total`, `payment_method_id`, `paid_at`, `delivered_at`, `cancelled_at`, `cancel_reason`, `closed_at`, `notes`, [V].

**`order_items`** — Snapshot idéntico en estructura a `quote_items`.

**`tasks`** — Tareas y seguimientos (§53).
- Campos: `type` (CALL/MESSAGE/MEETING/FOLLOW_UP/QUOTE/COLLECTION/DELIVERY/OTHER), `title`, `description`, `assigned_user_id`, `contact_id`, `lead_id`, `opportunity_id`, `quote_id`, `conversation_id`, **`due_at` timestamptz + `is_all_day` bool** ([RECOMENDACIÓN] en lugar de `due_date` + `due_time` separados: evita errores de zona horaria), `reminder_at`, `status` (PENDING/IN_PROGRESS/DONE/CANCELLED), `priority`, `completed_at`, `completed_by_user_id`, `cancel_reason`, `source` (MANUAL/AUTOMATION/AI), `automation_run_id`, `created_by_*` (actor, E.0), [SD].
- Índice: (`organization_id`, `assigned_user_id`, `status`, `due_at`).

**`scheduled_actions`** — Acciones diferidas **cancelables por evento** (§54: seguimiento a +24 h que se cancela si el cliente responde).
- Campos: `action_type` (CREATE_TASK/SEND_TEMPLATE/NOTIFY/REASSIGN), `payload` JSONB, `run_at`, `status` (PENDING/RUNNING/DONE/CANCELLED/FAILED), `cancel_on_events` TEXT[] (`["message.received"]`), `cancel_scope` JSONB (`{contact_id: …}`), `source_type/id` (automation rule, quote), `idempotency_key` UNIQUE, `executed_at`, `cancelled_at`, `cancel_reason`.

---

### E.11 IA

**`ai_providers`** [GLOBAL] — OpenAI, Anthropic, luego Gemini, xAI, local. `code`, `name`, `adapter_key`, `default_base_url`, `is_enabled`. Los añade la plataforma porque cada proveedor requiere código (un adapter).

**`ai_models`** [GLOBAL] — Catálogo de modelos por proveedor (§73).
- Campos: `provider_id`, `model_id` (string exacto de la API), `display_name`, `supports_tools`, `supports_vision`, `supports_streaming`, `supports_structured_output`, `supports_audio_input`, `context_window`, `max_output_tokens`, `modality` (CHAT/EMBEDDING/TRANSCRIPTION), `status` (ACTIVE/DEPRECATED/RETIRED/DISABLED), `source` (SYNCED/MANUAL), `deprecation_date`, `last_synced_at`.
- UNIQUE (`provider_id`, `model_id`).

**`ai_model_prices`** [GLOBAL] — Precio por token con vigencia (los precios cambian y el coste histórico no debe recalcularse).
- `model_id`, `input_per_mtok_usd`, `output_per_mtok_usd`, `cached_input_per_mtok_usd`, `effective_from`, `effective_to`.

**`ai_provider_accounts`** — Cuentas con API key (§68): "OpenAI Principal", "Claude Backup"…
- Campos: `provider_id`, `name`, `credential_id` → `credentials`, `key_last_four`, `status` (ACTIVE/DISABLED/INVALID/SUSPENDED_BUDGET), `last_tested_at`, `last_test_status`, `last_test_error`, `external_org_or_project_id`, `base_url_override` (**solo con allowlist**, ver M: SSRF), `daily_budget_usd`, `monthly_budget_usd`, `daily_token_limit`, `alert_threshold_percent` (80), `on_limit_behavior` (BLOCK/FALLBACK), `is_default`, [SD].
- UNIQUE (`organization_id`, `name`).

**`ai_account_models`** — Qué modelos se habilitan por cuenta. `account_id`, `model_id`, `is_enabled`. UNIQUE (`account_id`, `model_id`).

**`ai_agents`** — Identidad estable del agente (§55).
- Campos: `name` ("Alex IA"), `slug`, `role` (RECEPTION/SALES/SUPPORT/AFTER_SALES/WHOLESALE/SUPERVISOR/CUSTOM), `is_customer_facing` bool, `status` (DRAFT/ACTIVE/PAUSED/ARCHIVED), `current_version_id`, `avatar_file_id`, [SD].

**`ai_agent_versions`** [INM tras publicar] — Configuración versionada (§112, §114).
- Campos: `agent_id`, `version`, `status` (DRAFT/PUBLISHED/ARCHIVED), `objective`, `description`, `tone` (PROFESSIONAL/FRIENDLY/CONCISE/SALES), `language`, `system_prompt_id` → `ai_prompts`, `autonomy_level` (1–4), `auto_send_min_confidence` (0.90), `suggest_min_confidence` (0.70), `max_tool_calls_per_turn` (8), `max_ai_turns_per_conversation`, `max_output_tokens`, `temperature`, `schedule_id`, `out_of_hours_behavior` (HANDOFF_QUEUE/REPLY_AND_QUEUE/AI_CONTINUES), `change_note`, `created_by_user_id`, `published_by_user_id`, `published_at`.
- UNIQUE (`agent_id`, `version`). Cada `ai_run` guarda el `agent_version_id` con el que se ejecutó, lo que permite revertir y reproducir.

**`ai_agent_model_configs`** — Routing y fallback por versión de agente (§74–75).
- Campos: `agent_version_id`, `priority` (0 = principal, 1..n = fallbacks), `provider_account_id`, `model_id`, `params` JSONB, `purpose` (CHAT/SUMMARY/CLASSIFICATION/EXTRACTION).
- UNIQUE (`agent_version_id`, `purpose`, `priority`).

**`ai_agent_tools`** — Tools habilitadas por versión.
- Campos: `agent_version_id`, `tool_code` (debe existir en el Tool Registry del código), `is_enabled`, `requires_human_approval` bool, `config` JSONB (p. ej., `allowed_price_list_codes`), `max_calls_per_conversation`.
- El servicio impide habilitar en agentes `is_customer_facing` las tools marcadas `customer_facing_allowed = false`.

**`ai_agent_channels`** — `agent_id`, `channel_account_id`, `is_default`, `is_active`. UNIQUE (`agent_id`, `channel_account_id`).

**`ai_agent_rules`** — Reglas declarativas (§115): `agent_version_id`, `rule_type` (INSTRUCTION/GUARDRAIL/HANDOFF_TRIGGER/ESCALATION), `condition` JSONB (intent ∈ …, sentiment ≥ UPSET, keyword…), `action` (HANDOFF/RAISE_PRIORITY/ADD_TAG/BLOCK_TOPIC), `text`, `priority`.

**`ai_prompts`** [INM por versión] — Prompts administrables y versionados (§113).
- Campos: `key` (`sales.system`, `handoff.summary`, `extraction.lead`, `intent.classify`), `version`, `agent_id` (nullable = prompt compartido de la organización), `content`, `variables` JSONB, `is_active`, `change_note`, `created_by_user_id`.
- UNIQUE (`organization_id`, `key`, `version`). Los prompts de sistema **base de seguridad** (guardrails no negociables) viven en código y se anteponen siempre; la organización edita solo la parte de negocio.

**`ai_intents`** — Catálogo extensible de intenciones (§63): `code`, `label`, `description` (se envía al clasificador), `is_system`, `default_team_id`, `default_ai_agent_id`, `raise_priority_to`, `is_active`.

**`ai_sessions`** — Tramo continuo en que un agente atiende una conversación.
- Campos: `conversation_id`, `ai_agent_id`, `agent_version_id`, `status` (ACTIVE/ENDED/HANDED_OFF), `started_at`, `ended_at`, `end_reason`, `turn_count`, `input_tokens`, `output_tokens`, `cost_usd`.

**`ai_runs`** — Una invocación del agente ante uno o varios mensajes. **Sustituye y unifica `ai_decisions`.**
- Campos: `session_id`, `conversation_id`, `agent_version_id`, `trigger_message_ids` UUID[], `purpose` (REPLY/COPILOT_SUGGEST/SUMMARY/EXTRACTION/CLASSIFY/SUPERVISOR), `status` (RUNNING/SUCCEEDED/FAILED/BLOCKED/SUPERSEDED), `intent_code`, `intent_confidence`, `sentiment`, `decision` (AUTO_SENT/SUGGESTION_CREATED/HANDOFF/NO_REPLY/BLOCKED_BY_GUARDRAIL), `decision_reason`, `guardrail_findings` JSONB, `output_message_id`, `final_provider_account_id`, `final_model_id`, `fallback_used` bool, `latency_ms`, `input_tokens`, `output_tokens`, `cost_usd`, `context_manifest` JSONB (qué entró en el contexto: IDs de mensajes, chunks de KB, resúmenes; **no** el texto completo), `error_code`, `created_at`.

**`ai_llm_calls`** [INM] — Cada llamada HTTP a un proveedor, incluidos los reintentos y fallbacks.
- Campos: `run_id`, `attempt_no`, `provider_account_id`, `model_id`, `status` (OK/TIMEOUT/RATE_LIMITED/PROVIDER_ERROR/AUTH_ERROR/BAD_REQUEST/CONTENT_FILTERED), `http_status`, `external_request_id`, `latency_ms`, `input_tokens`, `cached_input_tokens`, `output_tokens`, `cost_usd`, `created_at`. Particionada por mes.

**`ai_actions`** [INM] — Cada tool call (§84).
- Campos: `run_id`, `conversation_id`, `ai_agent_id`, `tool_code`, `arguments` JSONB (redactado), `result` JSONB (redactado; **nunca** costos ni secretos), `status` (SUCCESS/ERROR/DENIED/PENDING_APPROVAL), `error_code`, `duration_ms`, `idempotency_key` (UNIQUE: `run_id + tool_call_id`), `created_entity_type/id`, `created_at`.

**`ai_handoffs`** — Transferencias IA ↔ humano (§17, §101).
- Campos: `conversation_id`, `direction` (AI_TO_HUMAN/HUMAN_TO_AI), `from_ai_agent_id`, `from_user_id`, `to_team_id`, `to_user_id`, `to_ai_agent_id`, `reason_code` (CUSTOMER_REQUESTED/LOW_CONFIDENCE/NEGOTIATION/COMPLAINT/POLICY_RULE/TOOL_FAILURE/BUDGET_EXCEEDED/AI_DISABLED/OUT_OF_SCOPE/MAX_TURNS), `status` (REQUESTED/ACCEPTED/CANCELLED/EXPIRED), `priority`, `summary_id`, `requested_at`, `accepted_at`, `accepted_by_user_id`, `first_human_response_at`.

**`ai_summaries`** — Resúmenes y memoria larga (§61–62).
- Campos: `kind` (CONVERSATION_ROLLING/HANDOFF/CONTACT_PROFILE/OPPORTUNITY), `contact_id`, `conversation_id`, `opportunity_id`, `content` (texto), `structured` JSONB (interés, variante, presupuesto, trade-in, pendientes), `run_id`, `covers_until_message_id`, `is_current` bool, `created_at`.
- UNIQUE parcial por (`kind`, sujeto) WHERE is_current. La **memoria larga del cliente** es `CONTACT_PROFILE` con `is_current`.

**`ai_suggestions`** — Borradores del copiloto / modo AI_ASSISTED.
- Campos: `conversation_id`, `run_id`, `kind` (REPLY/IMPROVE/QUOTE_DRAFT/ACTION), `content`, `proposed_actions` JSONB, `status` (PENDING/ACCEPTED/EDITED_AND_SENT/REJECTED/EXPIRED/SUPERSEDED), `decided_by_user_id`, `decided_at`, `final_message_id`, `edit_distance` (mide cuánto corrige el humano).

**`ai_feedback`** — `run_id`, `message_id`, `given_by_user_id`, `rating` (UP/DOWN), `category` (WRONG_INFO/INVENTED_DATA/TONE/SHOULD_HANDOFF/UNNECESSARY_HANDOFF/TOO_LONG/OTHER), `comment`, `corrected_text`, `status` (NEW/REVIEWED/ACTIONED).

**`ai_usage_daily`** — Agregado diario (§78). `date`, `provider_account_id`, `model_id`, `ai_agent_id`, `platform`, `runs`, `llm_calls`, `input_tokens`, `output_tokens`, `cost_usd`, `handoffs`, `auto_sent`, `suggestions`. UNIQUE por la combinación de dimensiones. Los contadores en tiempo real para presupuestos van en Redis y se concilian con esta tabla.

**`kb_documents`** — Base de conocimiento (§60).
- Campos: `title`, `category` (COMPANY/HOURS/LOCATION/WARRANTY/PAYMENT/DELIVERY/FAQ/POLICY/TECH_SERVICE/OTHER), `content` (markdown), `source_file_id`, `status` (DRAFT/PUBLISHED/ARCHIVED), `version`, `review_by` (fecha de revisión), `contains_price_warning` bool (validación automática), `published_at`, [SD].

**`kb_document_agents`** — Qué agentes usan cada documento. UNIQUE (`document_id`, `agent_id`).

**`kb_chunks`** — `document_id`, `document_version`, `chunk_index`, `content`, `token_count`, `embedding vector(N)`, `embedding_model_id`. Índice HNSW sobre `embedding`; **filtro obligatorio por `organization_id`**.

---

### E.12 Automatizaciones, notificaciones, integraciones, sistema

**`automation_rules`** — `name`, `trigger_event` (`lead.created`, `conversation.intent_detected`, `opportunity.stale`, `quote.sent`, `schedule.cron`), `conditions` JSONB (DSL tipado: `{all: [{field: "intent", op: "eq", value: "warranty"}]}`), `actions` JSONB (lista ordenada: ASSIGN_TEAM, SET_PRIORITY, CREATE_TASK, SCHEDULE_ACTION, NOTIFY, ADD_TAG, SEND_TEMPLATE), `is_active`, `priority`, `cooldown_seconds`, `max_runs_per_entity`, `version`, `created_by_user_id`, [SD].

**`automation_runs`** [INM] — `rule_id`, `rule_version`, `event_id`, `entity_type/id`, `status` (SUCCESS/SKIPPED/FAILED/PARTIAL), `conditions_result` JSONB, `actions_result` JSONB, `depth` (anti-bucles), `error`, `started_at`, `finished_at`. UNIQUE (`rule_id`, `event_id`): la idempotencia garantiza que una regla no se ejecute dos veces por el mismo evento.

**`notifications`** — `recipient_user_id`, `type` (`conversation.assigned`, `handoff.requested`, `task.overdue`, `quote.approval_requested`, `stock.low`, `ai.account_error`, `webhook.failing`…), `title`, `body`, `link`, `entity_type/id`, `priority`, `dedupe_key`, `read_at`, `created_at`. UNIQUE parcial (`organization_id`, `recipient_user_id`, `dedupe_key`) WHERE read_at IS NULL.

**`notification_preferences`** — `user_id` (por organización), `type`, `in_app`, `email`, `push`, `sound`.

**`credentials`** — Almacén de secretos único para integraciones de canal **y** cuentas de IA (§70).
- Campos: `owner_type` (CHANNEL_ACCOUNT/AI_ACCOUNT/INTEGRATION), `store` (DB_ENCRYPTED/VAULT/AWS_SM/GCP_SM), `credential_reference` (ruta en el gestor externo, o NULL si es DB), `ciphertext` bytea (AES-256-GCM, solo si DB_ENCRYPTED), `key_version` (rotación de la KEK), `last_four`, `fingerprint` (HMAC para detectar reutilización sin descifrar), `expires_at` (tokens de Meta), `rotated_at`, `created_by_user_id`.
- **Ningún serializer de API expone `ciphertext`**, y el modelo de Django lo excluye de `__repr__`/`__str__`.

**`webhook_ingress`** [GLOBAL, platform-owned] — Ingesta cruda de webhooks (ver Q y tenancy-context §5). Sin RLS de tenant: acceso solo desde `integrations` (plataforma). `provider`, `channel_account_id` y `organization_id` (nullable; se rellenan **después** de enrutar con la función SECURITY DEFINER; informativos, no autorizan), `event_key` (dedupe), `signature_valid`, `headers` JSONB (sanitizados), `payload` JSONB, `status` (RECEIVED/PROCESSING/PROCESSED/IGNORED/FAILED/DEAD), `attempts`, `next_attempt_at`, `last_error`, `received_at`, `processed_at`. UNIQUE (`provider`, `event_key`). Particionada por mes; retención de 90 días.

**`webhook_failures`** [INM, GLOBAL] — `webhook_ingress_id`, `attempt`, `error_class`, `error_message`, `traceback_hash`, `occurred_at`.

**`sync_jobs`** — `kind` (TEMPLATES_SYNC/AI_MODELS_SYNC/CHANNEL_PROFILE_SYNC/HEALTHCHECK), `target_type/id`, `status`, `started_at`, `finished_at`, `result` JSONB.

**`data_imports`** — `kind` (CATALOG/PRICES/STOCK/CONTACTS), `file_id`, `status` (UPLOADED/VALIDATING/VALIDATED/APPLYING/APPLIED/REVERTED/FAILED/CANCELLED), `total_rows`, `valid_rows`, `error_rows`, `new_count`, `update_count`, `summary` JSONB, `created_by_user_id`, `applied_by_user_id`, `applied_at`.

**`data_import_rows`** — `import_id`, `row_number`, `raw` JSONB, `normalized` JSONB, `action` (CREATE/UPDATE/UNCHANGED/SKIP/ERROR/STALE), `errors` JSONB, `target_entity_type/id`.

> **F3-20 (importación de stock, ADR-028 §5):** `STALE` es la fila cuya celda cambió entre la previsualización y la aplicación: no se aplica. El archivo no se guarda (ni en `files` ni en el object storage): de él quedan sus filas, con solo las columnas conocidas, `file_name` y `content_sha256`, con el que se avisa de un archivo ya subido. `file_id` llega cuando haga falta conservar el original (con el antivirus de ADR-008). El tamaño (1 MiB) y las filas (1000) tienen tope; la previsualización y la aplicación son síncronas.

**`search_documents`** — Proyección para la búsqueda global (§86). `entity_type`, `entity_id`, `title`, `subtitle`, `keywords` (teléfonos normalizados, @usernames, SKU, números), `search_vector`, `updated_at`. UNIQUE (`entity_type`, `entity_id`); índices GIN (tsvector) + trigram.

**`org_sequences`** — `organization_id`, `sequence_key` (CONV/OPP/COT/VTA), `period` (año o NULL), `prefix`, `next_value`. UNIQUE (`organization_id`, `sequence_key`, `period`). Se incrementa con `UPDATE … RETURNING` dentro de la transacción de creación.

**`outbox_events`** — Eventos de dominio (ver B). `event_type`, `aggregate_type`, `aggregate_id`, `payload` JSONB, `occurred_at`, `published_at`, `attempts`, `correlation_id`. Índice parcial WHERE published_at IS NULL.

**`audit_logs`** [INM] — Ver N. Particionada por mes.

---

## F. ERD textual (relaciones principales y cardinalidades)

```text
PLATAFORMA / ACCESO
users 1 ──< N organization_memberships >── N 1 organizations
users N >── 0..1 platform_files (avatar global)   ; organization_memberships N >── 0..1 files (avatar por organización)
(personas) *_user_id ──> organization_memberships(organization_id, user_id)   [FK compuesta]
organizations 1 ── 1 organization_settings
organizations 1 ──< N branches 1 ──< N warehouses
organization_memberships N >──< N roles              (membership_roles)
roles 1 ──< N role_permissions >── 1 permissions     [GLOBAL]
roles 1 ──< N discount_policies
teams N >──< N organization_memberships              (team_members)

CONTACTOS
contacts 1 ──< N contact_identities >── 0..1 channel_accounts
contacts 1 ──< N contact_consents (1 fila vigente por canal+propósito) ; contacts 1 ──< N contact_consent_events
contacts 1 ──< N contact_addresses
contacts N >──< N tags                               (contact_tags)
contacts 1 ──< N notes
contacts 1 ──< N timeline_events
contacts 0..1 ── merged_into ──> 1 contacts          (auto-referencia)
contacts 1 ──< N contact_merge_candidates (como a o b)

INBOX
channel_accounts 1 ──< N conversations
channel_accounts 1 ──< N message_templates
contacts 1 ──< N conversations >── 1 contact_identities
conversations 1 ──< N messages 1 ──< N message_attachments >── 0..1 files
messages 1 ──< N message_status_events
conversations 1 ──< N conversation_participants >── 1 organization_memberships
conversations 1 ──< N conversation_assignments (historial; estado actual en conversations)
conversations N >── 0..1 teams | 0..1 organization_memberships | 0..1 ai_agents  (asignación)
conversations N >── 0..1 leads ; N >── 0..1 opportunities   (vínculo principal)

CATÁLOGO / PRECIOS / INVENTARIO
brands 1 ──< N products >── 1 categories (árbol: categories 0..1 parent ──< N)
products 1 ──< N product_options 1 ──< N product_option_values
products 1 ──< N product_variants N >──< N product_option_values (variant_option_values)
product_variants 1 ──< N product_prices >── 1 price_lists       (sin solapes de vigencia)
product_variants 1 ──< N variant_costs                          (acceso restringido)
promotions 1 ──< N promotion_items >── variant|product|category|brand
promotions N >──< N price_lists                                 (promotion_price_lists)
price_change_batches 1 ──< N price_change_batch_items ; 1 ──< N product_prices
product_variants 1 ──< N inventory_levels >── 1 warehouses      (único por par)
product_variants 1 ──< N inventory_movements ; 1 ──< N inventory_reservations

COMERCIAL
contacts 1 ──< N leads >── 0..1 product_variants
leads 1 ──< N lead_score_events
leads 0..1 ── converted_to ──> 0..1 opportunities
contacts 1 ──< N opportunities >── 1 pipelines 1 ──< N pipeline_stages
opportunities N >── 1 pipeline_stages
opportunities 1 ──< N opportunity_items ; 1 ──< N opportunity_stage_history
opportunities N >── 0..1 lost_reasons
opportunities 1 ──< N quotes 1 ──< N quote_items >── 1 product_variants (snapshot)
quotes 1 ──< N quote_approvals ; quotes 0..1 ── revision_of ──> quotes
opportunities 1 ── 0..1 orders 1 ──< N order_items
orders N >── 1 payment_methods
tasks N >── 0..1 contacts | leads | opportunities | quotes | conversations
scheduled_actions N >── (origen polimórfico registrado; se cancela por evento)

IA
ai_providers 1 ──< N ai_models 1 ──< N ai_model_prices           [GLOBAL]
ai_providers 1 ──< N ai_provider_accounts >── 1 credentials
ai_provider_accounts N >──< N ai_models                          (ai_account_models)
ai_agents 1 ──< N ai_agent_versions 1 ──< N ai_agent_model_configs >── ai_provider_accounts
ai_agent_versions 1 ──< N ai_agent_tools ; 1 ──< N ai_agent_rules ; N >── 1 ai_prompts
ai_agents N >──< N channel_accounts                              (ai_agent_channels)
conversations 1 ──< N ai_sessions 1 ──< N ai_runs 1 ──< N ai_llm_calls
ai_runs 1 ──< N ai_actions ; ai_runs 1 ── 0..1 messages (salida)
conversations 1 ──< N ai_handoffs >── 0..1 ai_summaries
contacts 1 ── 0..1 ai_summaries (CONTACT_PROFILE vigente)
ai_runs 1 ──< N ai_feedback ; ai_runs 1 ──< N ai_suggestions
kb_documents 1 ──< N kb_chunks ; kb_documents N >──< N ai_agents (kb_document_agents)

TRANSVERSAL
automation_rules 1 ──< N automation_runs >── 1 outbox_events
organization_memberships 1 ──< N notifications
channel_accounts 1 ──< N webhook_ingress 1 ──< N webhook_failures
(todas las entidades de negocio) ──< audit_logs (entity_type, entity_id)
```

### Resumen de tablas por dominio (≈ 95 tablas)

| Dominio | Tablas |
|---|---|
| Plataforma/Org | organizations, organization_settings, branches, service_health_checks, impersonation_sessions, org_sequences, outbox_events |
| Identidad/Equipos | users, user_mfa_devices, user_invitations, organization_memberships, teams, team_members, work_schedules, assignment_rules |
| Acceso | permissions, roles, role_permissions, membership_roles, discount_policies |
| Archivos | files (tenant), platform_files (plataforma) |
| Contactos | contacts, contact_identities, contact_consents, contact_consent_events, contact_addresses, tags, contact_tags, notes, contact_merge_candidates, contact_merges, timeline_events, data_provenance |
| Inbox/Canales | channel_accounts, message_templates, conversations, conversation_participants, conversation_assignments, conversation_status_history, conversation_tags, messages, message_attachments, message_status_events, quick_reply_categories, quick_replies |
| Catálogo | brands, categories, products, product_options, product_option_values, product_variants, variant_option_values, product_search_aliases, product_media |
| Precios | price_lists, product_prices, variant_costs, promotions, promotion_items, promotion_price_lists, price_change_batches, price_change_batch_items |
| Inventario | warehouses, inventory_levels, inventory_movements, inventory_reservations |
| Comercial | leads, lead_score_events, pipelines, pipeline_stages, opportunities, opportunity_items, opportunity_stage_history, opportunity_tags, lost_reasons, quotes, quote_items, quote_approvals, payment_methods, orders, order_items, tasks, scheduled_actions |
| IA | ai_providers, ai_models, ai_model_prices, ai_provider_accounts, ai_account_models, ai_agents, ai_agent_versions, ai_agent_model_configs, ai_agent_tools, ai_agent_channels, ai_agent_rules, ai_prompts, ai_intents, ai_sessions, ai_runs, ai_llm_calls, ai_actions, ai_handoffs, ai_summaries, ai_suggestions, ai_feedback, ai_usage_daily, kb_documents, kb_document_agents, kb_chunks |
| Transversal | automation_rules, automation_runs, notifications, notification_preferences, credentials, webhook_ingress, webhook_failures, sync_jobs, data_imports, data_import_rows, search_documents, audit_logs |

### Diferencias con la lista del §96 (y por qué)

| §96 | Propuesta | Motivo |
|---|---|---|
| `users` con rol/equipo | `users` (global) + `organization_memberships` | Multiempresa real: un usuario en varias organizaciones sin duplicar su identidad |
| `user_roles` | `membership_roles` | El rol pertenece a la membresía de una organización, no a la persona global |
| `channels` | Enum en código + `channel_accounts` | Cada tipo de canal requiere código (un adapter); lo configurable es la cuenta |
| `message_statuses` | `message_status_events` + `messages.delivery_status` | Estado actual rápido de consultar + historial completo |
| `inventory` | `inventory_levels` | Nombre más preciso; se añade `inventory_reservations` |
| `ai_decisions` | `ai_runs` | La decisión es un atributo de la ejecución; tenerla aparte duplicaría datos |
| `ai_usage` | `ai_llm_calls` (detalle) + `ai_usage_daily` (agregado) | Costo exacto por llamada + dashboards rápidos |
| `integration_credentials` | `credentials` | Un único almacén de secretos para canales e IA |
| `contacts.marketing_opt_in` (implícito) | `contact_consents` + `contact_consent_events` | Consentimiento por canal y propósito, con evidencia e historial |
| `webhook_ingress` | `webhook_ingress` (platform-owned) | La organización se desconoce al recibir; se resuelve después con una función SECURITY DEFINER |
| `integrations` | Absorbida por `channel_accounts` / `ai_provider_accounts` | Hoy todas las integraciones son canales o IA; se crea cuando aparezca otra categoría (p. ej., un ERP) |
| Precio "promoción" como `price_type` | Solo `promotions` | Una sola fuente de verdad |
| — (faltaban) | `orders`, `payment_methods`, `message_templates`, `scheduled_actions`, `data_provenance`, `timeline_events`, `outbox_events`, `org_sequences`, `variant_costs`, `ai_agent_versions`, `ai_prompts`, `ai_intents`, `kb_*`, `notifications`, `files`, `search_documents` | Requeridas por funcionalidades del prompt que no tenían tabla |

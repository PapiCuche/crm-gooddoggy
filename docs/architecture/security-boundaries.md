# Fronteras de seguridad

**Relacionado:** ADR-001 … ADR-011, `docs/fase-0/04-precios-pipeline-seguridad-infra.md` §M (análisis de amenazas)

---

## 1. Zonas de confianza

```text
 ZONA 0 — No confiable                     ZONA 1 — Borde              ZONA 2 — Aplicación          ZONA 3 — Datos
┌──────────────────────────────┐        ┌──────────────┐        ┌──────────────────────────┐   ┌─────────────────┐
│ Navegador del usuario         │─HTTPS─►│ Reverse proxy│───────►│ web (Django API, ASGI)    │──►│ PostgreSQL 18    │
│ Cliente final (WhatsApp, IG…) │        │ TLS, límites │───────►│ ws  (Channels)            │   │  (RLS, roles)    │
│ Proveedores (webhooks)        │─HTTPS─►│ de tamaño,   │───────►│ next (UI, sin secretos)   │   ├─────────────────┤
│ Contenido generado por la IA  │        │ rate limit   │        │ workers (Celery)          │──►│ Redis (broker,   │
│ Archivos subidos, Excel       │        └──────────────┘        │ beat                      │   │  channels, cache)│
│ Documentos KB, outputs tools  │                                │                           │──►│ Object storage   │
└──────────────────────────────┘                                └──────────┬───────────────┘   └─────────────────┘
                                                                            │ HTTPS saliente (allowlist)
                                                               ┌────────────▼───────────────┐
                                                               │ ZONA 4 — Terceros            │
                                                               │ Meta, TikTok, OpenAI,        │
                                                               │ Anthropic, email, Sentry     │
                                                               └──────────────────────────────┘
```

**Toda entrada que cruza desde la Zona 0 es no confiable**, incluidos: los mensajes de clientes, los nombres de perfil, los archivos, **las respuestas del LLM** (ADR-005) y el contenido de la base de conocimiento.

## 2. Fronteras y controles

| # | Frontera | Qué cruza | Controles |
|---|---|---|---|
| B1 | Navegador → proxy → API | Peticiones autenticadas | TLS, HSTS, cookie `__Host-` HttpOnly + SameSite=Lax, CSRF, verificación de Origin, rate limit, límite de body, CSP |
| B2 | API → capa de dominio | Comandos | Autenticación → membresía → `ExecutionContext` → permiso + scope → validación de entrada → servicio |
| B3 | Dominio → BD | Consultas | `tenant_scope` (SET LOCAL), `TenantManager`, RLS con FORCE (una sola política PERMISSIVE por comando; `organization_memberships` con política SELECT condicional), rol `crm_app` sin privilegios de propietario, FKs compuestas, funciones SECURITY DEFINER endurecidas (ADR-002 §3.3) |
| B4 | Proveedor → `/webhooks/*` | Eventos | Firma HMAC sobre el body crudo, deduplicación, ACK rápido, tenant resuelto por cuenta de canal (SECURITY DEFINER) |
| B5 | Runtime IA ↔ LLM | Prompts, tool calls, respuestas | Sin secretos ni costos en el contexto, datos no confiables delimitados, salida estructurada, **Output Guard** (claims → evidencia), presupuestos |
| B6 | LLM → Tool Registry | Tool calls | Tools registradas en código, validación Pydantic, audience PUBLIC/INTERNAL, sin argumentos de identidad, resource scoping, auditoría en `ai_actions` |
| B7 | Aplicación → canal (salida) | Mensajes al cliente | `MessagingPolicyService` (ventana recalculada, plantillas, consentimientos de `contact_consents`, notas internas bloqueadas), revalidación antes de enviar |
| B8 | Aplicación → storage | Bytes | `ObjectStorageService`, claves con prefijo de tenant, bucket privado, URLs firmadas cortas tras autorizar |
| B9 | Aplicación → terceros (HTTP saliente) | Llamadas a APIs | Cliente HTTP con allowlist de hosts, bloqueo de IPs privadas (anti-SSRF), timeouts, sin seguir redirects a hosts no permitidos |
| B10 | Operador → sistema | Comandos, shell, admin | `TenantCommand` / `PlatformCommand` con motivo y auditoría, admin solo para plataforma, impersonación temporal y auditada |
| B11 | CI/CD → producción | Código, migraciones | PR revisado, CI (gitleaks, auditoría de dependencias, tests de aislamiento), migraciones con `crm_migrator` **solo en el job de migraciones**; web/worker/ws/beat nunca reciben su credencial (ADR-002 §1.1) |
| B12 | Aplicación → servidor de correo (SMTP saliente) | Correos con enlaces de un solo uso | Único módulo (`core.mail`, ADR-019): servidor fijado por entorno (no hay destino elegido por un usuario), cifrado y credenciales obligatorios en producción, tiempo máximo de conexión, validación contra inyección de cabeceras, dirección enmascarada en el log y nunca el cuerpo |

## 3. Clasificación de datos

| Nivel | Ejemplos | Reglas |
|---|---|---|
| **Restringido** | Credencial de `crm_migrator` (BYPASSRLS = acceso a todos los tenants), API keys de IA, tokens de Meta, app secrets, secretos TOTP, KEK, contraseñas | Cifrado (AES-256-GCM, KEK fuera de la BD) o hash; nunca en logs, auditoría, frontend, contexto LLM ni exportaciones; solo `last_four` visible |
| **Confidencial comercial** | Costos, márgenes, listas mayorista/distribuidor/interna, descuentos máximos | Permiso específico (`product_cost.view`, `prices.view_*`); jamás a agentes IA públicos ni a clientes; tablas y servicios separados (costos) |
| **Personal (PII)** | Nombre, teléfono, email, documento, dirección, contenido de mensajes, audios | RLS, alcances, redacción en logs, retención y anonimización (Ley 29733), exportación como permiso sensible |
| **Interno** | Notas internas, resúmenes IA, scoring | No se envían a clientes ni a agentes públicos |
| **Público** | Catálogo visible, precios de listas `ai_exposable`, información de la KB publicada | Se puede comunicar al cliente (vía tools y evidencia) |

## 4. Superficies de ataque priorizadas y control principal

| Superficie | Control principal | Verificación |
|---|---|---|
| Fuga entre tenants | RLS + scoping + FKs compuestas | Tests T1–T17 de [tenancy-context.md](tenancy-context.md) |
| Prompt injection → datos o acciones | Tools sin IDs de identidad, audience, Output Guard | Tests de tools + conjunto de evaluación adversarial |
| Credenciales de IA o canales | Secret store cifrado, `last_four`, step-up MFA | Test de patrones de key en respuestas y logs |
| XSS desde mensajes de clientes | Render sin HTML, CSP, lint contra `dangerouslySetInnerHTML` | Tests de componentes con payloads |
| Escalada de privilegios | Reglas anti-escalada de RBAC | Tests específicos (ADR-003) |
| Webhooks falsos | HMAC + deduplicación | Tests de firma inválida y replay |
| SSRF | Cliente HTTP con allowlist | Tests con URLs internas |
| Denial of wallet (IA) | Presupuestos y rate limits | Tests de límites |

## 5. Secretos: dónde vive cada uno

| Secreto | Ubicación | Rotación |
|---|---|---|
| `DATABASE_URL` (`crm_app`), `DJANGO_SECRET_KEY`, KEK de credenciales, credenciales de storage, DSN de Sentry | Variables de entorno de **web, worker, ws y beat**, inyectadas por el gestor de secretos del hosting (en local: `.env` no versionado) | Procedimiento en runbook; la KEK con `key_version` |
| `DATABASE_MIGRATOR_URL` / `CRM_MIGRATOR_PASSWORD` (`crm_migrator`) | **Solo** el job de migraciones del deploy, con una política de acceso propia en el gestor de secretos. Los procesos de runtime se niegan a arrancar si la ven | Periódica y ante cualquier sospecha |
| API keys de IA, tokens de canales | Tabla `credentials` cifrada (o Vault/Secret Manager vía `credential_reference`) | Desde el panel, sin deploy; auditado |
| Secretos TOTP | `user_mfa_devices` cifrados | Al re-enrolar |
| Secretos de CI | GitHub Actions secrets (mínimos; CI no necesita secretos de producción) | Anual o ante incidente |

**Nunca:** en el repositorio, en el bundle del frontend, en `NEXT_PUBLIC_*`, en logs ni en mensajes de error.

## 6. Checklist de seguridad para cada PR (resumen)

- [ ] ¿Nueva tabla tenant-owned? → `TenantModel`, política RLS en la migración, UNIQUE con `organization_id`, FKs compuestas en relaciones críticas.
- [ ] ¿Nueva política RLS? → **una sola PERMISSIVE por tabla y comando** (las permisivas se combinan con OR); cualquier excepción, con lógica condicional o RESTRICTIVE y un test de no ampliación.
- [ ] ¿Nueva función `SECURITY DEFINER`? → propietario explícito (`crm_migrator`), `SET search_path` fijo, inputs tipados, retorno mínimo, sin SQL dinámico, `REVOKE ALL ON FUNCTION … FROM PUBLIC` y `GRANT EXECUTE … TO crm_app`; cubierta por el test de introspección T16.
- [ ] ¿Cambia la configuración de despliegue? → el runtime solo recibe `DATABASE_URL` (`crm_app`); la credencial del migrador solo va al job de migraciones.
- [ ] ¿Nuevo endpoint? → permiso + scope declarados, se ejecuta dentro de `tenant_scope`, entra automáticamente en la suite cruzada.
- [ ] ¿Recibe IDs de otras entidades? → `TenantPrimaryKeyRelatedField`.
- [ ] ¿Cambia datos importantes? → `audit.record`.
- [ ] ¿Toca secretos o PII? → redacción, sin logs.
- [ ] ¿Nueva tool IA? → audience, resource scope, evidence kinds, tests de scoping y redacción.
- [ ] ¿Envía mensajes? → `MessagingPolicyService` (ventana recalculada al enviar, plantillas, `contact_consents`).
- [ ] ¿HTTP saliente? → cliente con allowlist.
- [ ] ¿Archivos? → `ObjectStorageService` + validación de tipo y tamaño.

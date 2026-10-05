# Dependencias entre módulos

**Relacionado:** `docs/fase-0/01-vision-y-arquitectura.md` §C, ADR-005, ADR-006
**Verificación automática:** import-linter en CI (se activa en la Fase 1 con los primeros módulos)

---

## 1. Capas

```text
┌────────────────────────────────────────────────────────────────────────────┐
│ L5  Interfaces       api/ (DRF), consumers WS, webhooks, comandos, admin    │
├────────────────────────────────────────────────────────────────────────────┤
│ L4  Orquestación     ai_agents (runtime, tools), automations                 │
├────────────────────────────────────────────────────────────────────────────┤
│ L3  Dominio de       tasks, orders, quotes, inbox, deals, leads             │
│     negocio          pricing, inventory, contacts, catalog, channels         │
├────────────────────────────────────────────────────────────────────────────┤
│ L2+ Orquestación de  provisioning (alta de una organización), members        │
│     L2               (administración de sus miembros, ADR-017): unen los     │
│                      módulos de L2, que no se importan entre sí              │
├────────────────────────────────────────────────────────────────────────────┤
│ L2  Organización     organizations, access, accounts                         │
├────────────────────────────────────────────────────────────────────────────┤
│ L1  Plataforma       platform, ai_gateway, integrations (credenciales,       │
│                      webhook ingress), files, audit, notifications           │
├────────────────────────────────────────────────────────────────────────────┤
│ L0  Kernel           core (tenancy, ids, sequences, money, outbox, storage,  │
│                      observability, errors, http client)                     │
└────────────────────────────────────────────────────────────────────────────┘
  Lectura transversal: analytics, search (solo selectors/eventos, no escriben en otros módulos)
```

**Regla de capas:** un módulo solo importa de su capa o de capas inferiores. Dentro de L3 se aplica el grafo de la sección 2 (sin ciclos).

## 2. Grafo permitido (L3 y superiores)

```text
(A ──► B significa "A depende de B")

tasks  ──► orders, quotes, deals, leads, inbox, contacts
orders ──► quotes, deals, inventory, pricing, contacts
quotes ──► deals, pricing, inventory, catalog, contacts, files
deals  ──► leads, catalog, contacts
inbox  ──► channels, contacts, organizations
leads  ──► catalog, contacts
pricing, inventory ──► catalog
channels ──► integrations, files

ai_agents ──► ai_gateway
ai_agents ──(solo vía tools)──► services públicos de: catalog, pricing, inventory,
                                 contacts, inbox, leads, deals, tasks, quotes
automations ──(solo vía acciones registradas)──► services públicos
analytics, search ──► selectors públicos (lectura) + eventos
```

La reacción inversa (p. ej., "cuando se gana una oportunidad, consumir la reserva") se hace **por eventos de dominio** (outbox), no con un import de `deals` hacia `inventory` y otro de vuelta.

## 3. API pública de cada módulo

```text
apps/<modulo>/
  models.py        privado: solo el propio módulo lo importa para escribir
  services.py      PÚBLICO: comandos (escriben, validan permisos, auditan, emiten eventos)
  selectors.py     PÚBLICO: consultas de lectura con scope
  events.py        PÚBLICO: nombres y payloads de eventos que emite
  api/             privado: vistas y serializers
  tasks.py         privado
  tests/
```

- Otro módulo **solo** puede importar `services`, `selectors`, `events` y los tipos/DTO que estos exponen.
- `access` expone además `scopes` (registro de `ScopePolicy`): cada módulo declara ahí el alcance de sus recursos. La autorización se consulta con `access.selectors` (`execution_context`, `can`, `require`, `scoped`); las vistas de DRF la reciben de `access.permissions` (`HasPermission`, `ScopeFilter`), que son los valores por defecto; `IsMember` es solo para el contexto propio (F2-11). Los cambios de RBAC pasan por `access.services` (`grant_permission`, `assign_role`, `remove_role`, `ensure_owner_remains`, `ensure_can_manage_member`).
- `accounts`, `organizations` y `access` exponen además `bootstrap` (F2-06): su parte del alta de una organización. No es API pública: solo la importa `apps.provisioning`, que a su vez no importa los `models` de ningún módulo. Dos contratos de import-linter lo comprueban (`protected` y `forbidden`).
- `apps.members` (ADR-017) orquesta la administración de miembros: primero las reglas de `access.services.ensure_can_manage_member`, después la escritura de `organizations.services.set_membership_status` y, al suspender, `accounts.services.revoke_sessions` (F2-20). `organizations.services` no comprueba permisos, así que tampoco es API pública: solo lo importa `apps.members` (contrato `protected`, que tampoco deja importarlo al resto de `organizations`). `apps.members` no importa los `models` de ningún módulo.
- `organizations.branches` (F2-44) tiene los comandos de sucursales (`create_branch`, `update_branch`). Tampoco comprueban permisos, así que tampoco son API pública: solo los importa `apps.organizations.api`, cuyas vistas declaran `branches.manage` (otro contrato `protected`).
- Las FKs entre módulos se declaran con **referencia en texto** (`models.ForeignKey("contacts.Contact", …)`), sin importar el modelo, para no crear dependencias de import. La dirección de la FK debe respetar igualmente el grafo (una FK "hacia arriba", p. ej. `opportunities.conversation_id → inbox`, se permite solo si está documentada aquí; hoy: `deals → inbox` y `leads → inbox` para el origen, ambas nullable).

## 4. Contratos import-linter (borrador; se activan cuando existan los módulos)

```ini
[importlinter]
root_packages = core, apps

[importlinter:contract:layers]
name = Capas
type = layers
layers =
    apps.ai_agents | apps.automations
    apps.tasks
    apps.orders
    apps.quotes
    apps.inbox | apps.deals
    apps.leads | apps.pricing | apps.inventory | apps.channels
    apps.contacts | apps.catalog
    apps.provisioning | apps.members
    apps.organizations | apps.access | apps.accounts
    apps.platform | apps.ai_gateway | apps.integrations | apps.files | apps.audit | apps.notifications
    core

[importlinter:contract:gateway-independent]
name = ai_gateway no conoce el negocio
type = forbidden
source_modules = apps.ai_gateway
forbidden_modules = apps.contacts, apps.inbox, apps.catalog, apps.pricing, apps.inventory,
                    apps.leads, apps.deals, apps.quotes, apps.orders, apps.tasks

[importlinter:contract:pricing-authority]
name = Solo pricing lee precios y promociones
type = forbidden
source_modules = apps.quotes, apps.orders, apps.ai_agents, apps.inbox, apps.analytics
forbidden_modules = apps.pricing.models

[importlinter:contract:storage]
name = boto3 solo en core.storage
type = forbidden
source_modules = apps
forbidden_modules = boto3, botocore

[importlinter:contract:providers]
name = SDKs de IA solo en adapters del gateway
type = forbidden
source_modules = apps, core
forbidden_modules = openai, anthropic
ignore_imports =
    apps.ai_gateway.adapters.* -> openai
    apps.ai_gateway.adapters.* -> anthropic
```

(Las capas del contrato se ajustarán a medida que se creen los módulos; import-linter falla si un módulo listado no existe, así que cada fase añade solo los suyos.)

## 5. Reglas adicionales verificadas en CI

| Regla | Mecanismo |
|---|---|
| `sentry_sdk` solo en `core.observability` | Contrato forbidden |
| Sin `TenantModel.all_tenants` fuera de `apps.platform` | Test estático (grep AST) |
| Solo `AssignmentService` escribe las columnas de asignación | Test estático |
| Solo `MessagingPolicyService` + outbound envían a los adapters | Contrato forbidden (`apps.channels.adapters` solo desde `apps.channels`) |
| Sin `requests` o `httpx` directos: usar `core.http` (allowlist) | Contrato forbidden |

## 6. Frontend

```text
src/app/            rutas (App Router) — solo composición
src/features/<x>/   componentes, hooks y estado del dominio x
src/lib/api/        cliente generado desde OpenAPI (no editar)
src/components/ui/  shadcn (primitivas)
src/lib/            utilidades transversales (i18n, formato, ws)
```

Regla ESLint (`no-restricted-imports` o `eslint-plugin-boundaries`): `features/x` no importa internos de `features/y`; lo compartido sube a `components/` o `lib/`.

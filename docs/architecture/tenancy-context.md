# Tenant context: cómo se establece de forma segura en cada punto de entrada

**Relacionado:** ADR-001, ADR-002, ADR-003, ADR-005
**Estado:** diseño aprobado para implementar en la Fase 1 (infraestructura base)

> Regla de oro: **ninguna consulta a una tabla tenant-owned se ejecuta sin un `tenant_scope` activo, y ningún `tenant_scope` sobrevive a su transacción.**

---

## 1. Piezas

```text
core/tenancy/
  context.py      TenantContext (dataclass inmutable) + contextvar _current
  scope.py        tenant_scope(), user_scope(), platform_scope()
  db.py           set_config local, verificación de conexión limpia
  middleware.py   resolución HTTP (/api/v1/o/{slug}/…)
  celery.py       @tenant_task, @platform_task, verificación al arrancar
  channels.py     TenantConsumerMixin (WebSockets)
  commands.py     TenantCommand, PlatformCommand
  resolvers.py    lookups cross-tenant vía funciones SECURITY DEFINER
```

### 1.1 `TenantContext`

```python
@dataclass(frozen=True, slots=True)
class TenantContext:
    organization_id: UUID
    source: Literal["http", "celery", "ws", "webhook", "ai_tool", "command", "test"]
    user_id: UUID | None = None          # humano, si existe
    actor_type: ActorType = ActorType.SYSTEM
    actor_id: UUID | None = None         # user_id, ai_agent_id, …
    correlation_id: str | None = None
```

`ExecutionContext` (servicios de negocio) **contiene** un `TenantContext` más la membresía y los permisos efectivos.

### 1.2 `tenant_scope()`: el único modo de fijar el contexto

```python
@contextmanager
def tenant_scope(ctx: TenantContext, *, using: str = "default"):
    if _current.get() is not None and _current.get().organization_id != ctx.organization_id:
        raise TenantContextError("Nested scope with a different tenant")   # prohibido anidar tenants distintos
    with transaction.atomic(using=using):
        conn = connections[using]
        with conn.cursor() as cur:
            cur.execute(
                "SELECT set_config('app.tenant_id', %s, true),"
                "       set_config('app.user_id', %s, true),"
                "       set_config('app.actor_type', %s, true)",
                [str(ctx.organization_id), str(ctx.user_id or ""), ctx.actor_type],
            )
        token = _current.set(ctx)
        try:
            yield ctx
        finally:
            _current.reset(token)
    # Al salir de atomic(): COMMIT/ROLLBACK → los set_config locales desaparecen.
```

**Propiedades:**

- `is_local = true` → el valor muere con la transacción. **Una conexión devuelta al pool no conserva el tenant.**
- Si `tenant_scope` se usa dentro de un `atomic()` externo (savepoint), `set_config` local afecta a la transacción externa hasta su fin. Por eso **se prohíbe anidar tenants distintos** y el mismo tenant anidado es un no-op seguro.
- El contextvar se resetea siempre (incluido en caso de excepción).
- `TenantManager.get_queryset()` lee el contextvar: sin contexto → `TenantContextMissing`. Con contexto → añade `organization_id = ctx.organization_id` (filtro de aplicación, **además** de RLS).

### 1.3 Verificación de conexión limpia (cinturón y tirantes)

Al inicio de cada petición HTTP, tarea Celery y mensaje WS **en entornos de desarrollo y test** (y muestreado en producción):

```sql
SELECT current_setting('app.tenant_id', true)   -- debe ser NULL o ''
```

Si no está vacío → error crítico + la conexión se cierra (`connection.close()`). Detecta cualquier `SET` de sesión introducido por error. Además, un test estático falla si aparece `SET app.` o `set_config(..., false)` en el código.

---

## 2. HTTP (API)

```text
Request /api/v1/o/{org_slug}/contacts/…
  ├─ SecurityMiddleware, SessionMiddleware, CsrfViewMiddleware, AuthenticationMiddleware
  ├─ RequestIdMiddleware (request_id, correlation_id)
  ├─ ApiEnvelopeMiddleware (F2-12, ADR-014 §1): por fuera de todos; errores de /api/ con el contrato
  ├─ ApiCsrfMiddleware (F2-13, ADR-014 §2): CSRF en los métodos no seguros de /api/
  └─ TenantResolutionMiddleware
        1. ¿ruta tenant (prefijo /api/v1/o/)? si no → pasa sin contexto (rutas de auth/plataforma)
        2. user autenticado? si no → 401
        3. org = platform_selectors.organization_by_slug(slug)          # tabla platform-owned
        4. membership = user_scope(user) → organization_memberships      # sin tenant: la política SELECT deja ver solo las del usuario
                        WHERE organization_id = org.id AND status = ACTIVE
           no existe → 404
        5. org.status ∈ {ACTIVE, TRIAL}? si no → 403 ORG_SUSPENDED
        6. request.tenant = TenantContext(org.id, "http", user.id, USER, user.id, correlation_id)
        7. with tenant_scope(request.tenant): response = get_response(request)
```

- **Toda la vista** (autorización, serializers, servicios y serialización de la respuesta) se ejecuta dentro del `tenant_scope` → una transacción por petición en las rutas de tenant. Si la respuesta es 400 o superior, el middleware deshace esa transacción (F2-12): una petición fallida no deja nada escrito.
- **Respuestas en streaming** (exportaciones grandes): prohibidas en las vistas de tenant; se generan con Celery y se descargan del storage con una URL firmada.
- `user_scope(user)`: transacción corta con solo `app.user_id` fijado (sin `app.tenant_id`); la política SELECT de `organization_memberships` permite ver las propias membresías **solo porque no hay tenant activo**. Dentro de `tenant_scope` la misma política restringe al tenant, aunque `app.user_id` también esté fijado (ADR-002 §3.2). `user_scope` no permite escrituras en tablas tenant-owned. Se usa para resolver la membresía y listar "mis organizaciones".
- Las rutas de plataforma (`/api/v1/auth/…`, `/api/v1/me/organizations`) no abren un `tenant_scope`; solo tocan tablas platform-owned o usan `user_scope`.
- **Las vistas asíncronas de Django no se usan en las rutas de tenant** en el MVP (el contextvar funciona, pero una transacción no puede cruzar un `await` de forma segura con el ORM). Si se usan en el futuro, el acceso a BD va en `sync_to_async` con su propio scope.

## 3. Celery

```python
@tenant_task(queue="default")
def recalc_lead_score(*, organization_id: str, lead_id: str, correlation_id: str | None = None): ...
```

- `@tenant_task` exige `organization_id` como kwarg (falla al encolar si falta), crea `TenantContext(source="celery", actor_type=SYSTEM)` y ejecuta el cuerpo dentro de `tenant_scope`.
- **Firma que ve Celery (#568):** `apply_async` valida los argumentos contra la firma de la tarea (`check_arguments`, también en modo eager). Celery 5.6 con Python 3.14 la obtiene con `inspect.signature`, que sigue `__wrapped__` hasta la función original; `@tenant_task` fija `__signature__` en el envoltorio con la firma de la función más `organization_id`, `actor_type`, `actor_id` y `correlation_id` (con y sin `bind=True`), de modo que el publicador del outbox y los reintentos (`task.retry`) puedan encolar. Una función que declare alguno de esos nombres falla al decorarla. `Task.apply()` (ejecución en el acto) no valida: los tests que solo la usan no ejercen esta comprobación. `actor_type` y `actor_id` se aceptan de los kwargs de quien encola y nunca deben reenviarse desde datos externos (un webhook o la carga de un evento).
- Las tareas largas que procesan en lotes (imports) abren **un `tenant_scope` por lote**, no una transacción gigante.
- `@platform_task` para tareas sin tenant (health checks, particiones, purga de `webhook_ingress`). No pueden usar `TenantManager` y, si necesitan iterar sobre tenants, lo hacen así: `for org_id in platform_selectors.active_organization_ids(): with tenant_scope(TenantContext(org_id, "celery")): …` (un scope por tenant, nunca varios a la vez).
- **Verificación al arrancar el worker:** toda tarea registrada debe estar decorada con `@tenant_task` o `@platform_task`; si no, el worker no arranca.
- Si el contexto de la tarea incluye un actor humano (acción iniciada por un usuario), se pasan `actor_type` y `actor_id` para la auditoría.
- Los argumentos son IDs; la tarea re-lee el estado dentro de su scope (idempotencia).

## 4. WebSockets (Channels)

```text
ws /ws/o/{org_slug}/
  connect():
    - scope["user"] autenticado (AuthMiddlewareStack con la cookie de sesión); Origin permitido
    - resolución org + membership igual que en HTTP (función compartida) vía database_sync_to_async
    - guarda self.tenant = TenantContext(org.id, "ws", user.id, USER, user.id)
    - se une a grupos con prefijo del tenant: org.{org_id}.user.{user_id}, …
  receive_json(subscribe conversation X):
    - database_sync_to_async(lambda: check_can_view_conversation(self.tenant, X))
        → internamente: with tenant_scope(self.tenant): …
    - si ok → group_add(f"org.{org_id}.conv.{X}")
```

- **Nunca se mantiene una transacción ni un scope entre `await`s.** Cada acceso a BD es una función síncrona corta con su propio `tenant_scope`, ejecutada vía `database_sync_to_async`.
- El contextvar no se fija a nivel de conexión WS (se evita que corrutinas concurrentes lo compartan); `self.tenant` se pasa explícitamente.
- Los eventos que llegan del outbox ya se emitieron en grupos del tenant; aun así, el consumer filtra por permiso antes de reenviar al cliente (el alcance OWN no recibe conversaciones ajenas).
- Revocar la sesión o desactivar la membresía → evento `session.revoked` → el consumer cierra con el código 4401.

## 5. Webhooks

Los webhooks llegan **sin usuario y sin tenant conocido**.

```text
POST /webhooks/meta/
  1. Verificar la firma (sin tocar tablas tenant)
  2. INSERT en webhook_ingress (platform-owned: provider, event_key, payload, received_at)
     ON CONFLICT DO NOTHING                                  ← sin tenant_scope
  3. Encolar process_webhook_ingress(ingress_id)                      ← @platform_task
Worker:
  4. route = resolvers.resolve_channel_account(platform, external_account_id)
       → función SECURITY DEFINER: devuelve (organization_id, channel_account_id) o nada
  5. with tenant_scope(TenantContext(route.organization_id, "webhook", actor_type=INTEGRATION)):
        procesar: contacto, conversación, mensaje (tablas tenant)
  6. marcar ingress como PROCESSED (plataforma)
```

- El tenant **siempre** sale del resolver (ID de cuenta de canal único global), **nunca** de un campo del payload.
- El resolver es una función `SECURITY DEFINER` que cumple los requisitos de ADR-002 §3.3 (propietario explícito, `search_path` fijo, inputs tipados, retorno mínimo, sin SQL dinámico, `REVOKE ALL … FROM PUBLIC` + `GRANT EXECUTE … TO crm_app`).
- `webhook_ingress` es platform-owned (sin RLS de tenant) y solo la usa el módulo `integrations`; su `organization_id` se rellena tras enrutar y es informativo. Los datos de negocio resultantes (contactos, mensajes, estados) se escriben en tablas tenant-owned dentro del `tenant_scope`.

## 6. Tools de IA

- El runtime del agente es una `@tenant_task` (`ai.process_conversation(organization_id, conversation_id)`): **el tenant ya está fijado antes de que el LLM intervenga.**
- El `ToolContext` (ADR-005) se construye desde la conversación cargada **bajo RLS**: `organization_id`, `conversation_id`, `contact_id` y `agent_id` salen de la BD, no del modelo.
- Cada tool se ejecuta dentro del scope del run (misma organización) y, si escribe, en un `atomic()` anidado (savepoint) del **mismo** tenant. No pueden abrir un scope de otro tenant (lo impide la regla de no anidar tenants distintos).
- Los argumentos del LLM **no incluyen** identificadores de tenant, contacto ni conversación. Los IDs de catálogo que el LLM pasa (p. ej., `variant_id` obtenido de `search_product`) se resuelven bajo RLS: si pertenecen a otro tenant, son invisibles → `NOT_FOUND`.
- Las llamadas HTTP al proveedor de IA **no** se hacen dentro de una transacción abierta (latencias de segundos). El orquestador alterna: `tenant_scope` corto (preparar el contexto) → llamada al LLM fuera de la transacción → `tenant_scope` corto (ejecutar tools y registrar) → … Así se evitan transacciones largas y bloqueos.

## 7. Comandos administrativos

```python
class Command(TenantCommand):            # manage.py rebuild_search_index --org acme
    def handle_tenant(self, ctx: TenantContext, **opts): ...
```

- `TenantCommand` exige `--org <slug>` (o `--all-orgs`, que itera con un scope por tenant) y `--reason "<texto>"` para las escrituras. Registra `audit_logs` con `actor_type = SYSTEM` y `metadata.operator` (usuario del SO o del deploy).
- `PlatformCommand`: sin tenant. Si necesita BYPASSRLS usa el alias de conexión `platform` (rol `crm_platform`, que solo se crea cuando haga falta), requiere `--reason` y audita.
- **Django admin:** solo para tablas platform-owned en producción. Los datos de tenant se gestionan desde la aplicación (con permisos y auditoría). En desarrollo, el admin puede operar bajo un tenant elegido.
- **Shell (`manage.py shell`):** arranca sin contexto; hay que usar `with tenant_scope(...)` explícitamente (lo recuerda un banner del shell).
- **Migraciones:** se ejecutan con `crm_migrator` (`DATABASE_MIGRATOR_URL`) en un **job separado**; web, worker, ws y beat nunca reciben esa credencial (ADR-002 §1.1). Las migraciones de datos que tocan tablas de tenant iteran por tenant o usan la capacidad BYPASSRLS del migrador de forma consciente y revisada en el PR.

## 8. Tests (gate de CI)

| # | Test | Qué detecta |
|---|---|---|
| T1 | Sin contexto: `SELECT count(*)` en cada tabla tenant-owned con el rol `crm_app` → 0 | Políticas ausentes o mal escritas |
| T2 | Contexto A: ninguna fila de B visible (tablas pobladas por factories en ambas organizaciones) | Fugas por política |
| T3 | Contexto A: `INSERT` con `organization_id = B` → error | `WITH CHECK` ausente |
| T4 | Introspección: `relrowsecurity` y `relforcerowsecurity` en toda tabla con `organization_id`; política `tenant_isolation` presente (salvo `organization_memberships`, con sus 4 políticas específicas); **como máximo una política PERMISSIVE por tabla y comando** | Migraciones sin RLS |
| T5 | Introspección de roles: `crm_app` no es propietario ni superusuario y no tiene BYPASSRLS | Configuración de roles |
| T6 | Fuga por pool: dos peticiones secuenciales con A y B reutilizando la misma conexión → al inicio de la segunda, `current_setting` vacío | `SET` de sesión |
| T7 | HTTP cruzado: recorrer **todas** las rutas del router de tenant con los IDs de B autenticado como A → 404 en detalle, update y delete; los listados no contienen B | Scoping de vistas y FKs recibidas |
| T8 | FK cruzada: crear un recurso de A referenciando un ID de B → 400/404, sin filas creadas | Validación de FKs + FKs compuestas |
| T9 | Celery: encolar una tarea sin `organization_id` → error; tarea de A no ve datos de B | Decorador |
| T10 | WS: suscribirse a una conversación de B → rechazo | Consumer |
| T11 | Webhook: payload con un `organization_id` falso → ignorado; el enrutado usa la cuenta de canal | Resolver |
| T12 | Tools IA (desde la Fase 8): cada tool con un ID de B → `NOT_FOUND`; tools de contacto actual sin argumentos de ID | Tool scoping |
| T13 | Estático: sin `SET app.`, sin `set_config(…, false)`, sin `.objects` de modelos tenant en módulos de plataforma, sin `all_tenants` fuera de `platform` | Regresiones de código |
| T14 | Memberships con tenant: usuario miembro de A y B; `tenant_scope(A)` (con `app.user_id` fijado) + `SELECT` raw sobre `organization_memberships` → **solo** la membresía de A | Ampliación por la rama `user_id` de la política |
| T15 | Memberships sin tenant: `user_scope(user)` → devuelve las membresías de A **y** B; INSERT/UPDATE/DELETE → rechazados | Política de resolución de organización |
| T16 | SECURITY DEFINER (introspección): cada función `prosecdef` tiene `search_path` fijo en `proconfig`, propietario `crm_migrator`, **sin** EXECUTE para `PUBLIC` y con EXECUTE para `crm_app` | Funciones cross-tenant expuestas o secuestrables |
| T17 | Credenciales: `web`/`worker`/`ws`/`beat` no arrancan si el entorno contiene `DATABASE_MIGRATOR_URL` o `CRM_MIGRATOR_PASSWORD`, o si el rol conectado es superusuario, tiene BYPASSRLS o es propietario de tablas | Credencial del migrador en el runtime |

**Todos los tests de BD se ejecutan conectados como un rol equivalente a `crm_app`** (el fixture crea la BD con el migrador y conecta los tests con el rol de aplicación). Si los tests corren como superusuario, RLS no se prueba: el pipeline falla si detecta que el usuario de test tiene `rolsuper` o `rolbypassrls`.

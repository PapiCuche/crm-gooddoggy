# backend/

Django 5.2 LTS (ASGI) sobre Python 3.14 — versiones exactas en [ADR-012](../docs/adr/ADR-012-engineering-runtime-baseline.md).
**Fase 1:** esqueleto con health checks, tooling y CI (F1-02), roles y RLS (F1-03) y puntos de entrada con tenant (F1-04), UUIDv7 y numeración por organización (F1-05), outbox y auditoría (F1-06). Sin dominio de negocio.

## Requisitos

- [uv](https://docs.astral.sh/uv/) **0.12.19** (lo exige `required-version`); uv usa Python 3.14.7 (`.python-version`).
- PostgreSQL para los tests y `/health/ready` (local: `infra/docker/compose.yaml`; CI: `postgres:18.6`).

## Uso

```bash
cd backend
uv sync --frozen                         # entorno reproducible desde uv.lock
uv run python manage.py check            # settings local por defecto
DJANGO_SETTINGS_MODULE=config.settings.local uv run uvicorn config.asgi:application --reload
```

`manage.py` usa `config.settings.local`. `config/asgi.py` usa `config.settings.production` si no se indica otra cosa.

## Validaciones (las mismas que CI)

```bash
uv run ruff format --check . && uv run ruff check .
uv run mypy .
uv run lint-imports
uv run python manage.py makemigrations --check --dry-run
DATABASE_URL=postgres://crm_app:…@localhost:5432/crm \
DATABASE_MIGRATOR_URL=postgres://crm_migrator:…@localhost:5432/crm uv run pytest
```

**Tests de BD (F1-03, ADR-002):** necesitan los roles de `infra/docker/postgres/init/01-roles.sh`.
- `tests/conftest.py` aplica las migraciones como `crm_migrator` y ejecuta los tests como `crm_app`.
- Si el rol de test es superusuario o tiene BYPASSRLS, la sesión se aborta.
- La app `tests.tenancy_app` (solo en `config.settings.test`) aporta modelos con RLS para probar el aislamiento.

## Settings

| Módulo | Uso | Notas |
|---|---|---|
| `config.settings.base` | Común | `DJANGO_SECRET_KEY` y `DATABASE_URL` obligatorias (el proceso no arranca sin ellas) |
| `config.settings.local` | Desarrollo | `DEBUG=True`, valores locales por defecto (nunca producción) |
| `config.settings.test` | pytest | BD desde `DATABASE_URL` |
| `config.settings.production` | Producción | `DEBUG=False` forzado; falla si `DJANGO_ALLOWED_HOSTS` está vacío, si `FORWARDED_ALLOW_IPS` falta o es `*` (ver «Dirección del cliente»), si la clave es insegura (< 50 caracteres o `django-insecure…`) o si el entorno contiene `DATABASE_MIGRATOR_URL`/`CRM_MIGRATOR_PASSWORD` (ADR-002 §1.1; el error nombra la variable, nunca su valor). Tras validar los hosts añade `127.0.0.1`, `localhost` y `[::1]` para las sondas locales. HSTS, cookies seguras, redirección SSL (excepto `/health/`) |

Variables de producción: `DJANGO_SECRET_KEY`, `DJANGO_ALLOWED_HOSTS`, `FORWARDED_ALLOW_IPS`, `DATABASE_URL`, `DJANGO_CSRF_TRUSTED_ORIGINS` (opcional), `DJANGO_LOG_LEVEL` (opcional). El correo saliente es opcional: `EMAIL_HOST` y, con él, `EMAIL_PORT`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD`, `EMAIL_USE_TLS` o `EMAIL_USE_SSL` y `MAIL_FROM` (ver «Correo saliente») y, con correo configurado, `APP_ORIGIN` (el origen público, `https`; ver «Recuperación de contraseña: emitir y enviar el enlace»). En F1-03 `DATABASE_URL` pasa a ser exclusivamente el rol `crm_app` (ADR-002 §1.1).

## Roles de BD y tenancy (F1-03, ADR-002)

| Proceso | Settings | Rol |
|---|---|---|
| web / worker / ws / beat | `config.settings.production` | `DATABASE_URL` → `crm_app`. Rechaza las variables del migrador y, en cada conexión nueva (y al arrancar ASGI), un rol superusuario, con BYPASSRLS o propietario de tablas |
| Job de migraciones | `config.settings.migrate` | `DATABASE_MIGRATOR_URL` → `crm_migrator`. Rechaza `DATABASE_URL`; la `SECRET_KEY` es efímera |

**Kernel de tenancy:**
- `core.tenancy`: `TenantContext`, `tenant_scope()` / `user_scope()` (solo `set_config(…, true)`) y `assert_clean_connection()`.
- `core.db.models`: `TenantModel` / `TenantManager`, que fallan sin contexto.
- `core.db.operations`: `EnableRLS`, `CompositeTenantFK` y `SecurityDefinerFunction`.
- Funciones SQL `app_current_tenant()` y `app_current_user()`.

## Puntos de entrada con tenant (F1-04, tenancy-context §2–§4 y §7)

| Entrada | Pieza | Comportamiento |
|---|---|---|
| HTTP `/api/v1/o/{slug}/…` | `core.tenancy.middleware.TenantResolutionMiddleware` | Sin usuario → 401. Organización inexistente o sin membresía → **404 idéntico** (`{"code":"NOT_FOUND"}`). Miembro de una organización suspendida → 403 `ORG_SUSPENDED`. La vista completa corre dentro de `tenant_scope` (streaming prohibido) |
| Celery | `@tenant_task` / `@platform_task` (`core.tenancy.celery`), app en `config/celery.py` | `@tenant_task` exige el kwarg `organization_id` (UUID) **al encolar** y ejecuta en `tenant_scope`. El bootstep `TenancyCheck` impide arrancar el worker si hay tareas sin decorar |
| WebSockets | `TenantConsumerMixin` (`core.tenancy.channels`) | `connect_tenant()` resuelve igual que HTTP (cierra con 4404/4403). `in_tenant(fn)` ejecuta cada acceso a BD en su propio scope vía `database_sync_to_async`: sin transacciones entre `await` |
| Comandos | `TenantCommand` (`--org`, `--reason`) / `PlatformCommand` (`--reason`) | El motivo y el operador se registran en el log (auditoría persistente en F1-06) |

- `apps.organizations`: tabla `organizations` mínima (platform-owned, sin RLS) y el selector `organization_by_slug`.
- Resolución compartida (`core.tenancy.resolution`) inyectada por settings (`TENANCY_ORGANIZATION_SELECTOR`, `TENANCY_MEMBERSHIP_RESOLVER`): el kernel no importa `apps` (import-linter).
- **Sin membresías reales hasta la Fase 2:** el resolvedor por defecto niega todo (fail-closed). Los tests usan un doble (`tests/fakes.py`).
- Broker de Celery y `AuthMiddlewareStack` de Channels: F1-10 y Fase 2.

## Usuarios (F2-01, ADR-001 §2, ADR-003 §2)

`apps.accounts.User` es la **identidad global** de una persona (`AUTH_USER_MODEL = "accounts.User"`, tabla `users`, platform-owned y sin RLS de tenant).

- **Sin organización ni rol.** No tiene `organization_id`, `role` ni grupos o permisos de Django. Un usuario se vincula a organizaciones mediante membresías, y los roles (Owner, Admin, Vendedor o personalizados) se asignan a la membresía (F2-02, F2-04, F2-05).
- **`is_platform_staff`** es administración técnica de la plataforma, no un rol de negocio. No da acceso a ningún tenant: un usuario autenticado sin membresía recibe el mismo 404 que cualquier otro. `createsuperuser` crea este tipo de usuario.
- **Email canónico** (`apps.accounts.emails.canonical_email`): se quita el espacio exterior; parte local en minúsculas y solo ASCII; dominio en minúsculas y en forma IDNA si es internacionalizado; formato validado. No hay reglas por proveedor: los puntos y los `+tag` distinguen direcciones.
- **Unicidad sin distinguir mayúsculas, garantizada en la BD:** `UNIQUE (email)` más `CHECK (email = lower(email))`. No se usa la extensión `citext` (decisión D-F2-3 en [phase-2.md](../docs/phases/phase-2.md)).
- **Contraseñas:** solo Argon2id (`argon2-cffi`, ADR-012). Validadores: mínimo 12 caracteres, contraseñas comunes, solo numéricas y parecido con los datos del usuario.
- **Sesiones:** `django.contrib.sessions` y `AuthenticationMiddleware` están activos para que exista `request.user`. Los endpoints de login, la cookie de sesión y el almacén de sesiones están en la sección «Inicio de sesión».

## Membresías (F2-02, ADR-002 §3.2)

`apps.organizations.OrganizationMembership` (tabla `organization_memberships`, **tenant-owned**) une un usuario global con una organización. Un usuario puede tener cero, una o varias.

- **Campos:** `id`, `organization_id`, `user`, `status` (`INVITED`, `ACTIVE`, `SUSPENDED`, `DEACTIVATED`), `default_branch` (su sucursal, opcional; F2-68, ver «Sucursales»), `created_at`, `updated_at`. Solo `ACTIVE` da acceso. **No lleva rol:** los roles se asignarán a la membresía (F2-04).
- **Constraints en BD:** `UNIQUE (organization_id, user_id)`, `CHECK` de `status`, FK a `organizations`, FK a `users` y FK compuesta `(organization_id, default_branch_id)` a `branches` (F2-68).
- **RLS con FORCE, sin `tenant_isolation`:** cuatro políticas, una por comando. Con tenant activo, `SELECT` solo ve ese tenant. Sin tenant, dentro de `user_scope(user)`, el usuario ve solo sus propias membresías y no puede escribir. Sin contexto no se ve nada. Así se resuelve "¿es miembro?" sin haber entrado todavía al tenant y sin BYPASSRLS.
- **Resolvedor:** `TENANCY_MEMBERSHIP_RESOLVER = "apps.organizations.selectors.active_membership"`. Usuario activo con membresía `ACTIVE` → tenant resuelto. Cualquier otro caso (sin membresía, membresía no activa, usuario inactivo, staff de plataforma sin membresía) → el mismo 404 que una organización inexistente.
- **Selectores** (`apps.organizations.selectors`): `active_membership` y `organizations_for_user`. El manager `OrganizationMembership.for_user` solo se usa dentro de `user_scope`; con tenant activo se usa `objects`.

## Alta de una organización (F2-06, E01-04)

```bash
python manage.py bootstrap_organization --slug acme --name "Acme SAC" \
    --owner-email ana@acme.pe --reason "alta del cliente"
```

`apps.provisioning.services.bootstrap_organization` crea la organización, la membresía de su Owner inicial, los cuatro roles plantilla y la asignación del rol Owner. Es una operación de plataforma: la lanza un operador con el comando de arriba (F2-06B), no un usuario del CRM.

- **Contraseña del Owner.** El comando la pide dos veces sin mostrarla. Sin terminal: `--password-stdin` (una sola línea por la entrada estándar). Nunca va en un argumento, en el log ni en la auditoría.
- **Cuenta existente.** Para dar el rol a una cuenta que ya existe hay que decirlo con `--existing-owner`; conserva su contraseña. Una contraseña entregada para un email que ya existe se rechaza y no crea nada.
- **Motivo.** `--reason` es obligatorio. Llega al log del comando sin direcciones de email (`core.redaction.mask_emails`, la misma máscara de la auditoría de plataforma).

- **Atómica.** Todo va en una sola transacción, la del `tenant_scope` de la organización nueva. Si un paso falla no queda organización, membresía, roles ni usuario.
- **Owner.** Con contraseña, una cuenta nueva que cumple la política vigente. Sin contraseña, una cuenta que ya existe, activa y con contraseña utilizable; nunca se le cambia.
- **Slug.** Minúsculas, dígitos y guiones, de 1 a 63 caracteres, sin empezar ni acabar en guion. Un slug en uso se rechaza.

## Cuentas de prueba (F2-116, ADR-026)

Solo en local o en una demo aislada. Se activan con `DJANGO_TEST_ACCOUNTS_ORGANIZATION=<slug>` (por ejemplo `pruebas`) en `infra/env/.env`; `production` no arranca con esa variable.

```bash
make up
docker compose -f infra/docker/compose.yaml --env-file infra/env/.env exec backend \
    python manage.py provision_test_accounts --reason "pruebas locales"
```

- Deja la organización `<slug>` («Good Doggy · Pruebas») con cuatro cuentas sintéticas en el dominio reservado `<slug>.gooddoggy.test`: `owner@`, `administrador@`, `supervisor@` y `vendedor@`, cada una con su membresía activa y el rol de plantilla de su nombre (Owner, Administrador, Supervisor, Vendedor). Sus permisos son los de esa plantilla.
- **Idempotente.** Crea lo que falta (cuentas, membresías, roles) y deja lo que hay. Una membresía suspendida sigue suspendida.
- **Contraseña.** La de las cuentas nuevas se pide dos veces sin mostrarla (o `--password-stdin`); las que ya existen conservan la suya. Nunca se imprime, se registra ni se audita.
- **Nunca toca una organización real.** Si `<slug>` existe y su Owner no es `owner@<slug>.gooddoggy.test`, se niega sin cambiar nada.
- Entran por el login normal: contraseña, límites, CSRF y segundo paso si lo activan. Auditoría: `test_accounts.provision.started` y `test_accounts.provisioned` (plataforma), `membership.activated` y `membership.role_assigned` (tenant).
- La única membresía sin invitación aparte del Owner inicial es esta: `organizations.bootstrap.add_test_membership`, que solo importa `apps.provisioning` y se niega fuera de la organización del setting.
- **Auditoría.** Dentro del tenant: `membership.role_assigned` y `organization.created`, con el operador y el motivo (hasta 200 caracteres). En la auditoría de plataforma (ADR-013 §5): una fila `organization.bootstrap.started` antes de empezar y una `organization.bootstrapped` con el resultado, que se decide por lo que quedó confirmado en la base. Si no se puede escribir la primera, el alta no empieza.
- **Fronteras.** Cada módulo de L2 expone su parte del alta en un `bootstrap.py` (`accounts`, `organizations`, `access`). Solo `apps.provisioning` puede importarlos y no toca sus modelos: dos contratos de import-linter lo comprueban.
- **No es una vía para asignar roles.** `apps.access.bootstrap.install_initial_owner` solo actúa sin actor, sobre una organización sin roles cuya única membresía es la indicada, activa y de un usuario activo. Los cambios posteriores pasan por `assign_role` y `remove_role`, con sus reglas (F2-05C).

## Roles y permisos: modelo (F2-04, ADR-003 §5)

Esta sección cubre el **modelo** RBAC de `apps.access`; el cálculo de permisos efectivos y su verificación están en el motor (sección siguiente, F2-05A).

- **`permissions`** (global): catálogo definido en `apps/access/catalog.py` y sincronizado tras cada `migrate`. El runtime (`crm_app`) solo puede leerlo.
- **El rol Owner sigue al catálogo** (ADR-018): tras ese paso, `extend_owner_roles` añade al rol Owner de cada organización las concesiones del catálogo que le falten, con alcance `ORGANIZATION` si el permiso lo admite. Solo añade y solo al rol Owner (por `is_owner_role`). Cada organización es una transacción dentro de su `tenant_scope`, con el actor `SYSTEM`: cada concesión queda en la auditoría de esa organización (`role.permission_granted`, `metadata.source = "catalog"`) y la operación entera, en la de plataforma: intención (`access.owner_roles.extend.started`) y resultado (`access.owner_roles.extended`, `SUCCESS` o `FAILED`), con el mismo `correlation_id` que las filas de cada organización. Corre con `crm_migrator`: desde el runtime, sin tenant activo no ve ningún rol, y con uno activo o con un contexto filtrado a la sesión se niega. Las demás plantillas solo llevan los permisos nuevos a las organizaciones nuevas. Un work item que necesite un permiso nuevo solo lo añade al catálogo.
- **`roles`**, **`role_permissions`** y **`membership_roles`** (tenant-owned, RLS con FORCE): roles por organización, concesiones con alcance (`OWN`, `TEAM`, `BRANCH`, `ORGANIZATION`, o `NULL` si el permiso no lo admite) y roles asignados a membresías.
- **Integridad en la BD:** FK compuestas con `organization_id` impiden enlazar una membresía de una organización con un rol de otra, también con SQL directo.
- **Roles plantilla** (`owner`, `admin`, `supervisor`, `seller`): `clone_role_templates(ctx)` los crea en una organización de forma idempotente y no asigna roles a nadie. Una organización puede crear roles propios sin cambios de esquema.
- **Nada decide por el código o el nombre de un rol:** la autorización depende de permisos y alcances.

## Autorización: motor (F2-05A, ADR-003 §5)

`apps.access.selectors` decide si una membresía puede hacer algo. Solo cuentan permisos y alcances: nunca el código o el nombre de un rol, ni `is_platform_staff`.

- `execution_context(ctx)`: membresía activa del usuario, sus equipos (`team_ids`, F2-52), su sucursal (`branch_ids`, F2-68: vacío si no tiene) y sus permisos efectivos (unión de los alcances de todos sus roles), en dos consultas. Sin membresía activa lanza `AccessDenied`.
- `has_permission`, `can(ectx, code, obj)`, `require(...)` y `scoped(ectx, code, queryset)`: permiso, alcance sobre un objeto y filtro de listado. Todo dentro del `tenant_scope` del propio contexto.
- `apps.access.scopes.register(Modelo, FieldScopes(...))`: cada modelo declara una vez sus columnas de propietario, equipo y sucursal, por el nombre de la columna (`assigned_user_id`, no `assigned_user`); de ahí salen el filtro y la verificación por objeto.
- `TEAM` alcanza los recursos de los equipos a los que pertenece la membresía (`team_members`, F2-52), además de lo propio. `BRANCH` alcanza los recursos de la sucursal de la membresía (`organization_memberships.default_branch_id`, F2-68), además de lo propio; sin sucursal, equivale a `OWN`. La asigna `PUT …/members/{id}/branch/` (F2-69).
- El `ExecutionContext` es una foto de su transacción: usarlo en otro `tenant_scope` posterior falla; hay que recalcularlo.
- Falla cerrado: un código de permiso inexistente lanza `UnknownPermission`; un modelo sin política lanza `ScopePolicyMissing`, también para quien tiene `ORGANIZATION`.

## Autorización en DRF (F2-05B)

DRF deniega por defecto: `apps.access.permissions.HasPermission` y `ScopeFilter` son sus clases por defecto. Una vista de tenant declara el permiso de cada método:

```python
class ContactDetail(generics.RetrieveUpdateAPIView):
    required_permissions = {
        "GET": "contacts.view",
        "PUT": "contacts.update",
        "PATCH": "contacts.update",
    }

    def get_queryset(self):  # nunca `queryset = ...` de clase: no hay tenant al importar
        return Contact.objects.all()
```

- **Varios permisos a la vez (F2-56):** un método puede declarar una tupla, `{"GET": ("teams.view", "users.view")}`, y entonces los exige todos: a quien le falta uno, 403; sobre un objeto y en un listado se aplica el alcance de cada uno, así que se ve lo que todos dejan ver. No existe «uno de varios». Una tupla vacía, una lista o un valor que no es texto no declaran nada: el método se deniega.
- Vista sin `required_permissions` o método sin declarar: 403. HEAD usa el permiso de GET. Pedir con `Accept` un formato que la API no sirve da 406 `NOT_ACCEPTABLE`.
- `ScopeFilter` aplica `scoped()` al queryset de listados y de `get_object()`: se filtra en SQL. Solo actúa donde la vista llama a `filter_queryset()`: una vista que consulte por su cuenta debe pasar su queryset por `scoped()`.
- Cada método usa su permiso y, sobre un objeto que ya existe, su alcance. Crear (POST) solo comprueba el permiso, y el filtro mira la fila antes de escribirla, no los valores que llegan (OBS-F2-05B-5). Un método de escritura responde con el objeto, así que poder escribirlo implica leer esa respuesta.
- Un serializador de tenant nunca acepta del cliente la clave primaria ni `organization_id`.
- El contexto es el que resolvió el middleware, nunca `request.user` ni datos del cliente. Los permisos se leen una vez por petición.

| Caso | Respuesta | Quién responde |
|---|---|---|
| Sin sesión | 401 `NOT_AUTHENTICATED` | middleware de tenant |
| Organización inexistente o sin membresía activa | 404 `NOT_FOUND` | middleware de tenant |
| Miembro sin el permiso, vista o método sin declarar | 403 `PERMISSION_DENIED` | `HasPermission` |
| Objeto fuera de alcance, de otra organización o inexistente | 404 `NOT_FOUND`, con los mismos bytes que los del middleware | `ScopeFilter` |

Las vistas de plataforma son las rutas fuera de `/api/v1/o/<slug>/`. Se excluyen por la ruta, nunca por el actor: declaran sus propias `permission_classes` (y `filter_backends` si son genéricas: sin tenant, `ScopeFilter` devuelve vacío) y se añaden a `PLATFORM` en `tests/test_access_api.py`. Ser staff de plataforma no abre ninguna ruta de tenant.

Ese test recorre todo el URLconf y falla si una ruta de tenant no es una vista de DRF con `HasPermission`, un permiso del catálogo por cada método que implementa y, si es genérica, `ScopeFilter` (salvo las rutas listadas en `MEMBER`, que declaran `IsMember`: ver «Contexto propio en una organización»), o si aparece una vista de DRF de plataforma que no está en `PLATFORM`. Mira lo que usa cada ruta (también `as_view(...)` y `@action(...)`). Exige las dos clases tal cual: una subclase o una composición (`A | B`) se rechazan y se revisan a mano. Rechaza también las vistas que redefinen `get_permissions`, `check_permissions`, `permission_denied`, `initial` o `dispatch`, las genéricas que redefinen `get_object` o `filter_queryset`, y las envueltas en un decorador (`cache_page`, por ejemplo). Una ruta fuera de `api/v1/o/` que pueda casar con una ruta de tenant (segmento dinámico antes del prefijo, o `re_path` sin `^`) también falla. Es una auditoría estática: no sustituye a la revisión de una vista con consultas o escrituras propias.

`HasPermission` comprueba además cada objeto que pase por `check_object_permissions` y responde 404. Es una red de seguridad, no un sustituto de `scoped()`: su cuerpo puede diferir del de un objeto inexistente.

## Contrato de errores de la API (F2-12, ADR-014 §1)

**Un solo cuerpo de error:** `{"code": "…", "message"?: "…", "fields"?: {…}}`. El cliente decide solo por `code`.

- `core.api.errors.exception_handler` (el `EXCEPTION_HANDLER` de DRF) lo produce para los errores de una vista. Los 401 y los 404 van sin `message`.
- `VALIDATION_ERROR` lleva `fields`: cada campo, una lista de `{code, message}`; los errores generales, en `_`; un serializador anidado, un objeto; una lista, un objeto por índice de fila. El nombre `non_field_errors` de DRF no sale a ningún nivel.
- Un servicio lanza un código de dominio con `core.api.errors.ApiError(code, status, message)`.
- `core.api.middleware.ApiEnvelopeMiddleware` es el middleware más externo. Convierte al contrato todo error bajo `/api/` que no salga ya en JSON, venga de donde venga: una ruta sin resolver, un `Host` no permitido, una excepción en otro middleware. También con `DEBUG`. Conserva las cabeceras de la respuesta original (`Allow`, cookies). Un 500 es siempre `{"code":"INTERNAL_ERROR"}`: el detalle va al log.
- Las respuestas de la API llevan `Content-Security-Policy: default-src 'none'; frame-ancestors 'none'`.
- La API solo sirve JSON: `?format=` no existe (`URL_FORMAT_OVERRIDE`). `APPEND_SLASH` está desactivado: una ruta sin su barra final es un 404 del contrato, no una redirección.
- En OpenAPI, el componente común es `core.api.schema.ERROR`: `@extend_schema(responses={200: …, **errors(401, 404)})`. Aparece en `openapi/schema.yaml` con el primer endpoint que lo use (F2-03A): drf-spectacular no publica componentes sin referencias.

**Una petición de tenant que acaba en error no deja nada escrito.** `TenantResolutionMiddleware` es dueño de la transacción de la petición y la deshace si la respuesta es 400 o superior, la haya producido una excepción de dominio, una validación o un fallo inesperado. Lo que deba sobrevivir a una petición fallida (por ejemplo, una futura auditoría de accesos denegados) tiene que escribirse fuera de ese `tenant_scope`.

Un slug imposible bajo `/api/v1/o/` responde 404 sin llegar a ninguna vista. El resolvedor de tenant consulta la membresía exista o no la organización, para que el tiempo de respuesta del 404 no delate qué slugs existen.

## Listados: paginación por cursor (F2-15, ADR-016)

`core.api.pagination.CursorPagination` es el paginador por defecto de DRF: una vista de lista (`ListAPIView`, el `list` de un viewset genérico) queda paginada sin declararlo. Es un valor por defecto, no una barrera: una vista con `pagination_class = None`, o un `APIView` que arma la lista a mano, devuelve el listado entero. Un listado de tenant no hace ninguna de las dos cosas.

- **Petición:** `?limit=` (por defecto 50, máximo 200) y `?cursor=` (el `next` de la página anterior).
- **Respuesta:** `{"results": […], "next": "…"}`. `next` es `null` en la última página; es un valor opaco, no una URL. Sin total de filas ni página anterior.
- **Errores:** un `limit` fuera de rango o un `cursor` ilegible o con una forma que la API no emite responden 400 `VALIDATION_ERROR` con el campo en `fields`. El cursor no va firmado: uno bien formado con otro `id` se acepta y solo cambia desde dónde se leen las filas propias.
- **Orden:** lo declara la vista con `ordering`, nunca el cliente. Por defecto `id` (UUIDv7: orden de creación); la única alternativa es `-id`. El queryset devuelve cada fila una sola vez (ante un `join` a varios, `Exists` o `distinct()`). Con cualquier otro orden, o con filas repetidas, la vista falla al paginar: el cursor de DRF solo guarda la primera columna, y si no es única y no nula repite o pierde filas.
- **Tenancy:** se pagina después de `ScopeFilter`. El cursor solo dice desde qué posición se lee; uno fabricado o de otra organización no trae filas ajenas.
- **Filtros:** no hay un mecanismo general. La primera ruta con filtros es la auditoría (F2-74) y fija la forma: un parámetro de consulta con el nombre del campo, por valor exacto, validado con un serializador y declarado en OpenAPI (`parameters=[…]`); un valor inválido es un 400 en ese campo. Los filtros se aplican antes de paginar y el cursor no los guarda.
- **Una vista de lista nueva** declara su queryset, `required_permissions` y, en OpenAPI, `**errors(400, 401, 403, 404)`. El tipo `Paginated…List` del contrato lo genera drf-spectacular.

## Sesión, CSRF y rutas de plataforma (F2-13, ADR-014 §2 y §4)

- `core.api.middleware.ApiCsrfMiddleware` exige el token CSRF en todo `POST`, `PUT`, `PATCH` y `DELETE` bajo `/api/`, haya sesión o no, y antes de resolver el tenant. No depende de la vista: DRF marca las suyas como exentas.
- El token solo se acepta en la cabecera `X-CSRFToken` (con la cookie `csrftoken`). El campo de formulario `csrfmiddlewaretoken` no vale, y el cuerpo no se lee antes de autenticar. El motivo de un rechazo va al log `django.security.csrf`; al cliente, solo `CSRF_FAILED`.
- `core.api.authentication.SessionAuthentication` es la clase por defecto de DRF: entrega a la vista el usuario de la sesión de Django y hace que la falta de sesión sea un 401 con `WWW-Authenticate: Session`. Además exige la marca del control de CSRF: una vista de DRF montada fuera de `/api/` rechaza los métodos no seguros.
- En desarrollo, `next dev` hace de proxy y cambia la cabecera `Host`: `config.settings.local` confía en `http://localhost:3000` para la comprobación de `Origin`. En producción, `DJANGO_CSRF_TRUSTED_ORIGINS`.
- Con el cliente de tests de Django, usar `Client(enforce_csrf_checks=True)` para probar el CSRF.

| Caso en un método no seguro | Respuesta |
|---|---|
| Sin token CSRF válido, con sesión o sin ella | 403 `CSRF_FAILED` |
| Con token y sin sesión | 401 `NOT_AUTHENTICATED` |

**Rutas de plataforma** (todo `/api/` fuera de `/api/v1/o/<slug>/`): declaran exactamente una clase de `core.api.permissions`, `Public` (sin sesión) o `Authenticated` (sesión de un usuario activo), y se añaden a `PLATFORM` en `tests/test_access_api.py`. Las dos deniegan dentro de un `tenant_scope` y bajo el prefijo de tenant: no sirven para saltarse `HasPermission`.

La auditoría del URLconf falla si:

- una ruta bajo `api/` no es de tenant ni está en la lista, sea o no de DRF;
- una vista de DRF está fuera de `api/`;
- una ruta listada no declara una de las dos clases;
- una vista cambia `authentication_classes` o redefine `get_authenticators`, `perform_authentication`, `get_authenticate_header`, `handle_exception` o `check_object_permissions`;
- un manejador de una vista de tenant lleva un decorador (`method_decorator(cache_page(…))`);
- una expresión regular sin `$` puede casar con una ruta de tenant;
- dos rutas comparten el mismo texto y la primera, que es la que responde, no cumple.

## Inicio de sesión (F2-03A, ADR-003 §2–3, ADR-013 §5)

| Ruta de plataforma | Clase | Qué hace |
|---|---|---|
| `GET /api/v1/auth/csrf/` | `Public` | Entrega la cookie `csrftoken` antes del primer método no seguro. 204 |
| `POST /api/v1/auth/login/` | `Public` | `{email, password}`. Abre la sesión y devuelve `{user}`; con segundo factor, 202 `{mfa_required}` y ninguna sesión (F2-106, abajo). Exige CSRF |
| `GET /api/v1/auth/session/` | `Authenticated` | El usuario de la sesión actual, o 401 `NOT_AUTHENTICATED` |

- **Una sola respuesta de rechazo.** Email desconocido, contraseña incorrecta y usuario desactivado responden 401 `INVALID_CREDENTIALS` con el mismo cuerpo. El motivo real solo queda en la auditoría de plataforma.
- **Cookie.** `crm_session`: `HttpOnly`, `SameSite=Lax`, `Path=/`, sin `Domain`. En producción se llama `__Host-crm_session` y lleva `Secure`. Sesiones de Django en base de datos (D-F2-2); el login rota el identificador de sesión y el token CSRF.
- **Caducidad.** 12 horas de inactividad y 7 días absolutos (ver «Ciclo de la sesión»).
- **Auditoría de plataforma.** `auth.login.succeeded` (actor `USER`) y `auth.login.failed` (actor `ANONYMOUS`, huella del email presentado, la cuenta como entidad si existe y `metadata.reason`); con segundo factor, la contraseña correcta deja `auth.login.mfa_required` (F2-106, abajo). Nunca la contraseña ni el email. Si el acceso correcto no se puede auditar, no se abre la sesión y la anterior del mismo navegador sigue valiendo. Un fallo al auditar un rechazo no cambia su respuesta: va al log y al reporte de errores, solo con el nombre de la acción.
- **Rotación.** Todo login correcto emite un identificador de sesión nuevo, también el de un usuario que ya tenía sesión: la anterior deja de valer.
- **Sin caché.** Toda respuesta bajo `/api/` lleva `Cache-Control: no-store`.
- **Cuerpos.** `core.api.parsers.Utf8JSONParser` es el analizador por defecto: JSON y solo en UTF-8. Un `charset` como `zlib` o `bz2` responde 415 `UNSUPPORTED_MEDIA_TYPE`; nunca elige el códec con el que se lee el cuerpo. Un JSON que no se puede leer responde 400 `PARSE_ERROR`, y también el que anida más de 100 niveles de `[` o `{` (`MAX_DEPTH`): más allá, un cuerpo desborda al analizador o a quien recorre lo que el analizador leyó, y acababa en un 500. Lo mismo una cadena, valor o clave, con un sustituto Unicode suelto (`"\ud800"`): no es texto que se pueda guardar ni devolver, y un campo de opción que la repetía en su error acababa en un 500. Un par de sustitutos válido (un emoji escrito con dos escapes) se acepta. De una clave repetida solo cuenta el último valor, como en cualquier JSON: lo que ese valor tapa no llega a ninguna parte y no se mira.
- **Todo 401** lleva `WWW-Authenticate: Session`, también `INVALID_CREDENTIALS`.
- **Contrato.** Las rutas están en `openapi/schema.yaml` (esquema de seguridad `sessionCookie`) y en el cliente generado del frontend.

## Dirección del cliente y proxy de confianza (F2-03D, ADR-003 §1, ADR-013 §4)

La dirección del cliente es la que resuelve el servidor ASGI (`REMOTE_ADDR`). El código nunca lee `X-Forwarded-For`: lo hace `uvicorn --proxy-headers`, y solo cuando la conexión llega de una dirección de `FORWARDED_ALLOW_IPS`. De esa dirección dependen la auditoría de plataforma y el límite de intentos de acceso (F2-03B).

- **Stack de compose:** Caddy tiene una dirección fija (`STACK_PROXY_IP`, por defecto `172.31.250.2`) en la subred del stack, fuera del rango que Docker reparte, y es la única de la que el backend acepta la cabecera. Caddy descarta el `X-Forwarded-For` que envíe el cliente y pone la dirección de quien se conecta a él.
- **Producción:** `FORWARDED_ALLOW_IPS` es obligatoria en todos los servicios del backend (comparten la configuración). Admite direcciones IP y redes en notación CIDR sin bits de host (`10.0.3.2`, `10.0.5.0/24`), separadas por comas. No arranca con `*`, con una red más ancha que `/8` (IPv4) o `/16` (IPv6), con una IPv4 escrita como IPv6 (`::ffff:…`), con un nombre de host ni con cualquier otro texto: uvicorn lo ignoraría sin avisar. Tampoco con `UVICORN_FORWARDED_ALLOW_IPS` en el entorno, que uvicorn antepone.
- **Varios saltos:** el backend debe confiar en todos los proxies de la cadena: el que se conecta a él y los que aparecen en la cabecera. uvicorn la recorre de derecha a izquierda y se queda con la primera dirección en la que no confía. Con un balanceador o una CDN delante de Caddy hacen falta las dos cosas: `trusted_proxies` en Caddy con la red del balanceador (si no, Caddy sustituye la cabecera) y esa misma red en `FORWARDED_ALLOW_IPS` junto a la dirección de Caddy (`10.0.3.2,10.0.5.0/24`). Una red de confianza solo debe contener proxies.
- **Cómo se nota un valor incorrecto:** en un despliegue con clientes de direcciones distintas, todos los accesos de `platform_audit_logs` tienen la misma `ip`: la del primer salto sin declarar. Con el límite de intentos activo, los fallos de todos los usuarios cuentan como una sola dirección.
- **En el stack local** todos los accesos desde el propio equipo llevan la misma dirección, la puerta de enlace de la red de Docker (por defecto `172.31.250.128`): es lo normal. Nunca debe ser `STACK_PROXY_IP`.
- **Nunca `*`:** uvicorn tomaría la primera dirección de la cabecera, que escribe el cliente.

## Límite de intentos de acceso (F2-03B, ADR-003 §2, ADR-014 §3, D-F2-9)

Cada intento de acceso se cuenta en la tabla `login_throttles` (platform-owned, compartida por todas las instancias) **antes** de comprobar las credenciales, con la huella del email presentado, exista o no la cuenta. Un acceso correcto devuelve lo que contó; uno fallido adelanta el reloj de sus contadores.

| Contador | Clave | Qué hace | Por defecto (`LOGIN_THROTTLE`) |
|---|---|---|---|
| `identifier` | huella | Mide si la cuenta está bajo ataque («caliente»). **Nunca rechaza** | Caliente con 20 intentos; espera de 1 a 60 min por dirección |
| `pair` | huella + dirección | Rechaza a quien prueba contraseñas de una cuenta desde una dirección | 5 intentos; espera de 1 a 15 min |
| `ip` | dirección | Rechaza a quien prueba muchas cuentas | 30 intentos; espera de 5 a 60 min |

- **Respuesta:** 429 `RATE_LIMITED` con `Retry-After` en segundos (la espera más larga de las claves del intento, con el reloj de la base de datos: lo que queda del bloqueo redondeado al segundo hacia arriba, nunca más que el bloqueo), idéntica para una cuenta real y una inexistente, y aunque la contraseña sea correcta. Un intento rechazado no cuenta en ninguna clave ni se audita.
- **Umbral:** «N intentos» son N intentos sin una ventana de calma entre medias (15 min sin fallos y sin bloqueo en curso), no N en 15 minutos. El contador solo vuelve a empezar tras esa calma, así que mientras los fallos siguen la espera llega a su máximo. Los accesos correctos no cuentan ni retrasan la calma.
- **Espera progresiva:** el intento que llega al umbral todavía se evalúa y, si falla, empieza el primer bloqueo; cada fallo posterior lo duplica hasta el máximo.
- **Nadie deja fuera a otro atacando su cuenta desde otra dirección:** el contador por cuenta no rechaza a nadie. Con la cuenta caliente, cada dirección tiene un solo intento antes de esperar (con la espera de `identifier`; su contador no vuelve a empezar si la cuenta está caliente cuando esa dirección regresa, aunque haya callado más de 15 minutos), pero quien presenta la contraseña correcta desde una dirección que no ha fallado entra. La excepción es compartir dirección con quien falla: un bloqueo de `ip` (el mismo NAT, o la misma red IPv6 `/64`) rechaza a todos los que están detrás, acierten o no (OBS-F2-03B-1).
- **Un acceso correcto** borra el contador de esa cuenta en esa dirección. En los demás devuelve su propio intento y nada más. El reloj de un contador solo lo mueve un fallo, así que el contador de la cuenta queda como si ese acceso no hubiera existido (tampoco con dos accesos a la vez) y desde otra dirección no se nota. En la dirección quita además el bloqueo que ese intento hubiera empezado: si la dirección ya estaba en su umbral, con él se va la referencia de cuándo acabó su bloqueo anterior, y con el último fallo a más de 15 minutos su contador vuelve a empezar.
- **Con segundo factor** (F2-106, abajo), «un acceso correcto» es el acceso completado, no la contraseña correcta. La contraseña correcta de una cuenta con dispositivo confirmado **no devuelve lo que contó**: sigue contada en los tres contadores hasta que el segundo paso la devuelve, y si nadie lo completa, se queda. No mueve el reloj de ningún contador; pero si con ella empieza un bloqueo, ese bloqueo acaba en el momento (`throttle.release`) y ese instante sí retrasa la calma.
- **Ráfagas simultáneas:** contar y bloquear es una sola transacción por intento, con las filas bloqueadas siempre en el mismo orden. Muchas peticiones a la vez no prueban más contraseñas que el umbral. Un acceso correcto que aún se está evaluando cuenta como intento hasta que termina.
- **Una cuenta, una clave:** la huella se calcula sobre el email canónico (D-F2-3), así que mayúsculas, espacios y otras escrituras del mismo dominio cuentan juntas. Lo que no es un email válido cuenta tal como llegó.
- **Dirección:** una IPv4, o la red `/64` de una IPv6. Quien dispone de una red IPv6 mayor y cambia de `/64` en cada intento no tiene tope: acotarlo exige una decisión pendiente (D-F2-10, F2-03E). Sin dirección conocida (`REMOTE_ADDR` vacío o inválido) todos los clientes comparten una y el login lo avisa en el log: es la señal de un proxy mal declarado (ver «Dirección del cliente y proxy de confianza»).
- **Auditoría de plataforma:** una fila `auth.login.throttled` (`DENIED`) cuando un acceso empieza un bloqueo (el que empieza con una contraseña incorrecta al empezar un alta de MFA no la deja: ver esa sección), con el contador, los intentos y los segundos; no una por cada intento rechazado. Con la cuenta caliente el contador de la fila es `identifier`. Las filas de `ip` no llevan huella. Un fallo al auditar no cambia la respuesta.
- **Nunca el email:** las claves llevan la huella de la auditoría de plataforma (`identifier_hash`).
- **Configuración:** `LOGIN_THROTTLE` vive en `config/settings/base.py` y se valida al cargar la aplicación (`apps.accounts.checks`): los tres contadores, cuatro enteros positivos cada uno (hasta 31 días), un umbral de 1000 intentos como mucho, una ventana de calma de 60 segundos o más, un primer bloqueo que no supere el máximo y una espera de `identifier` que no sea menor que la de `pair`. Con un valor inválido no arranca ningún proceso: web, worker, beat ni `manage.py`.
- **Purga:** `accounts.purge_login_throttles`, cada hora en beat: borra los contadores con una ventana entera sin fallos ni bloqueo. Los de `pair` se conservan un día de calma (`throttle.PAIR_KEPT`): borrarlos antes daría intentos nuevos a quien pausa y vuelve con la cuenta todavía bajo ataque. Sobre una cuenta que no está caliente, uno conservado vuelve a empezar igual tras su ventana de calma.
- **Para la interfaz:** un 429 puede llegar tras un solo fallo si la cuenta está caliente. Mostrar la espera de `Retry-After`, no un número de intentos.

## Ciclo de la sesión y organizaciones del usuario (F2-03C, ADR-003 §2, D-F2-2)

| Ruta de plataforma | Clase | Qué hace |
|---|---|---|
| `POST /api/v1/auth/logout/` | `Authenticated` | Cierra la sesión: borra su fila y la cookie. 204. Exige CSRF |
| `GET /api/v1/me/organizations/` | `Authenticated` | Organizaciones con membresía `ACTIVE` del usuario, no suspendidas, por nombre |

- **Caducidad** (`apps.accounts.middleware.SessionLifetimeMiddleware`, antes de resolver el tenant):
  - Inactividad: `SESSION_COOKIE_AGE` (12 h). Django solo renueva la caducidad al guardar la sesión, así que se guarda como mucho una vez cada `SESSION_REFRESH_INTERVAL` (5 min), no en cada petición. Se guarda antes de la vista: si la fila ya no existe (otra petición cerró la sesión), esta acaba en 401. Un fallo pasajero de la base de datos al guardar no cierra la sesión: esa petición responde 500 (OBS-F2-03C-2).
  - Absoluta: `SESSION_ABSOLUTE_AGE` (7 días) desde el login, haya o no actividad. Una sesión sin marca de inicio se trata como vencida.
  - Usuario desactivado o borrado: la sesión se destruye al detectarla (fila y cookie). Reactivar al usuario no la devuelve.
  - Revocada: la sesión guarda la época del usuario al iniciarla (`users.session_epoch`). Si ya no coincide, se destruye al detectarla, también si le tocaba renovarse. Una sesión sin época (anterior a F2-20) cuenta como de la época 0. El usuario ya está cargado: no añade consultas.
- **Revocar las sesiones de un usuario** (F2-20, D-F2-11): `accounts.services.revoke_sessions(user_id)` incrementa su época. Cada sesión abierta se destruye en su siguiente petición: una ruta que exige sesión responde 401. El usuario puede volver a iniciar sesión. Corre en la transacción de quien llama (`using=`) y no comprueba permisos. La época solo la mueve este servicio: `save()` sobre un `User` leído de la base nunca la escribe, tampoco nombrándola en `update_fields`, así que una instancia leída antes de una revocación no la devuelve atrás. Escriben fuera de esa protección `QuerySet.update`, `bulk_update` y una instancia construida a mano: ningún código los usa con ese campo. Como consecuencia, guardar un usuario cuya fila ya no existe falla en lugar de volver a insertarla. Hoy lo llama `apps.members` al suspender una membresía (ADR-003 §2).
- **Logout.** Primero cierra la sesión y después audita `auth.logout`: un fallo de auditoría nunca la mantiene abierta. La cookie anterior deja de valer.
- **Mis organizaciones.** No abre `tenant_scope`: lee las membresías del propio usuario dentro de `user_scope` (ADR-002 §3.2). No devuelve roles ni permisos: eso es `GET /api/v1/o/{slug}/me/` (F2-11).
- **Purga.** `accounts.purge_expired_sessions` (tarea de plataforma, una vez al día a hora fija en beat) borra las filas caducadas de `django_session`.
- **En tests:** `tests.factories.sign_in(client, user)` en lugar de `client.force_login(user)`, que no pone la marca de inicio ni la época. `user` debe estar recién leído si sus sesiones se revocaron: con una instancia anterior, la sesión nace revocada.

## Contexto propio en una organización (F2-11, ADR-003 §5)

`GET /api/v1/o/{slug}/me/` responde quién es el usuario en esa organización y qué puede hacer. La interfaz lo usa para construir la navegación. **Informa, no autoriza:** cada ruta sigue comprobando su permiso.

```json
{
  "user": {"id": "…", "email": "ana@acme.pe", "first_name": "Ana", "last_name": "López"},
  "organization": {"id": "…", "slug": "acme", "name": "Acme SAC"},
  "membership_id": "…",
  "roles": [{"code": "seller", "name": "Vendedor"}],
  "permissions": [{"code": "organization.view", "scopes": []}]
}
```

- **Permisos efectivos:** la unión de las concesiones de todos los roles de la membresía, leída en la propia petición (sin caché: un cambio de rol se ve en la siguiente). Van ordenados por código.
- **`scopes`:** vacío significa que el permiso no admite alcance. Si lo admite, trae los alcances concedidos en orden alfabético (`BRANCH`, `ORGANIZATION`, `OWN`, `TEAM`); con varios roles, la unión. El catálogo actual no tiene permisos con alcance: llegan con los módulos de negocio.
- **Roles:** solo código y nombre, para mostrar, ordenados por nombre y luego por código. El código es la clave de la lista (el nombre no es único en la organización) y no identifica al rol Owner: en una organización cuyo rol Owner tenga otro código, un rol propio puede llamarse `owner`. La marca de Owner (`is_owner_role`) no sale. La interfaz decide qué mostrar por `permissions`, nunca por el rol.
- **Quién lo lee:** cualquier miembro activo, también sin roles (recibe listas vacías). La clase es `apps.access.permissions.IsMember`, no `HasPermission`: no hay un permiso del catálogo para "leer mi propio contexto".
- **`IsMember` está acotada:** solo admite `GET` (y su `HEAD`); `OPTIONS` y las escrituras responden 403. La auditoría del URLconf solo la acepta en las rutas listadas en `MEMBER` (`tests/test_access_api.py`), en vistas no genéricas, que solo atienden `GET` y que no redefinen los ganchos de DRF. Fuera de esa lista, una vista de tenant sin `HasPermission` falla el test.
- **Lo que la auditoría no ve:** es estática. No detecta una consulta escrita a mano dentro del manejador. Tampoco ve una vista que redefina `setup` o `http_method_not_allowed` para atender otro método. Una vista de `MEMBER` solo puede leer el contexto de ejecución, la organización de ese contexto, los roles de la propia membresía (código y nombre) y `request.user`; eso se revisa en el PR que añade la ruta.
- Sin sesión, 401; sin membresía activa, en otra organización o con un slug inexistente, el mismo 404 con cualquier método; escribir siendo miembro, 403.

## Directorio de miembros (F2-16, ADR-016)

`GET /api/v1/o/{slug}/members/` lista quién pertenece a la organización. Exige el permiso `users.view` (sin él, 403; sin membresía activa, 404).

```json
{
  "results": [
    {
      "id": "…",
      "status": "ACTIVE",
      "joined_at": "2026-10-04T15:49:34.123456Z",
      "user": {"id": "…", "email": "ana@acme.pe", "first_name": "Ana", "last_name": "López"},
      "default_branch": {"id": "…", "code": "LIM-01", "name": "Centro de Lima"},
      "roles": [{"id": "…", "code": "owner", "name": "Owner"}]
    }
  ],
  "next": null
}
```

- **Qué incluye:** todas las membresías de la organización, en cualquier estado (`INVITED`, `ACTIVE`, `SUSPENDED`, `DEACTIVATED`). `id` es el de la membresía; `joined_at`, su fecha de alta, en UTC (los microsegundos se omiten si son cero). `status` es el estado de la membresía, no el de la cuenta: un usuario con la cuenta global desactivada sigue saliendo con el estado de su membresía (por ejemplo `ACTIVE`) aunque no pueda entrar.
- **Qué no incluye:** contraseña, marcas de plataforma ni las otras organizaciones del usuario. La tabla `users` es global: el listado sale de `organization_memberships` (RLS con FORCE) y solo une los usuarios de esas filas.
- **Sucursal (F2-70):** `default_branch` es la sucursal del miembro (identificador, código y nombre), o `null` si no tiene. Llega en la misma consulta que las membresías. Es un dato del miembro, como los nombres de sus roles: se ve con `users.view`, sin `organization.view`; quien no puede listar las sucursales ve así el identificador, el código y el nombre de las que tienen algún miembro (en cualquier estado de la membresía, y también si la sucursal está inactiva), no las demás ni su dirección, teléfono o zona horaria. El identificador es el que pide `PUT …/members/{id}/branch/`: quien tiene además `users.manage` puede llevar a un miembro a una de esas sucursales sin `organization.view`. No dice si la sucursal está activa. La cambia `PUT …/members/{id}/branch/` («La sucursal de un miembro»).
- **Roles:** identificador, código y nombre. El código y el nombre, para mostrar; el identificador es el que piden las rutas que asignan o quitan el rol («Roles de un miembro»). Nada decide por ellos. Se ven con `users.view`, sin `roles.view`: quién tiene qué rol es dato del directorio; lo que concede cada rol no sale aquí.
- **Paginación:** por cursor, en orden de alta (`?limit=`, `?cursor=`; ver «Listados»). Dos consultas por página, sean cuantos sean los miembros: las membresías con su usuario y su sucursal, y los roles de esa página.
- **Solo lectura.** Suspender y reactivar, asignar o quitar roles y asignar la sucursal están en las secciones siguientes; invitar, en «Invitar a una persona» (F2-80).
- `apps.access` lee las membresías con `apps.get_model`, como el motor de autorización: los módulos de L2 no se importan entre sí. Los selectores `memberships` y `roles_by_membership` filtran por organización, no por permiso: `users.view` lo exige la vista (`HasPermission` y `ScopeFilter`), y otra vista que los use declara el suyo.

## Directorio de roles (F2-22, ADR-016)

`GET /api/v1/o/{slug}/roles/` lista los roles de la organización. Exige el permiso `roles.view` (sin él, 403; sin membresía activa, 404). `users.view` no basta: la respuesta dice qué concede cada rol.

```json
{
  "results": [
    {
      "id": "…",
      "code": "seller",
      "name": "Vendedor",
      "description": "",
      "is_system": true,
      "permissions": [{"code": "organization.view", "scope": null}],
      "members": 3,
      "editable": true
    }
  ],
  "next": null
}
```

- **Qué incluye:** todos los roles de la organización, de plantilla (`is_system`) o propios. `permissions` son sus concesiones tal como están guardadas, ordenadas por código (orden de Python, como en `…/me/`, no el de la intercalación de la base); `scope` es `null` si el permiso no admite alcance. Una concesión de un código que el catálogo ya no tiene se ve aquí, aunque el motor de autorización la ignore. `members` cuenta las membresías que tienen el rol, en cualquier estado.
- **`editable`** (F2-34) es falso para el rol Owner y para un rol que tiene asignado quien pregunta: dos casos en que la API rechaza cualquier cambio de sus concesiones («Permisos de un rol»). Es una ayuda para que la pantalla no ofrezca lo que se va a rechazar; no autoriza nada ni dice que el actor cubra cada concesión. Depende de quién pregunta. No mira si quien pregunta tiene `roles.manage` (eso lo dice `…/me/`: la pantalla debe exigir las dos cosas) ni si la organización tiene rol Owner (sin él, toda escritura responde 409 `LAST_OWNER`).
- **Qué no incluye:** la marca de rol Owner como tal (no autoriza nada), ni roles o concesiones de otra organización (RLS con FORCE en `roles`, `role_permissions` y `membership_roles`).
- **Paginación:** por cursor, en orden de creación (ver «Listados»). Cuatro consultas por página, sean cuantos sean los roles: los roles, sus concesiones, sus miembros y cuáles no son editables.
- **Lectura.** Crear un rol está en «Crear un rol»; asignarlos a un miembro, en «Roles de un miembro»; conceder y retirar un permiso de un rol, en «Permisos de un rol»; renombrarlo y borrarlo, en «Renombrar y borrar un rol».
- **Módulos:** los lectores están en `apps.access.directory`, aparte del motor (`selectors`), que no decide por nombres, códigos ni marcas de rol (sus lectores `role_names` y `roles_by_membership` solo muestran código y nombre; el segundo, también el identificador). Filtran por organización, no por permiso: `roles.view` lo exige la vista. `directory.locked_roles` lee la marca de Owner solo para `editable`; un test impide que el motor, los alcances, los permisos o los servicios lean de `directory`.

## Catálogo de permisos (F2-34)

`GET /api/v1/o/{slug}/permissions/` devuelve los permisos que se pueden conceder a un rol. Exige `roles.view`.

```json
{"results": [{"code": "users.manage", "module": "users", "is_sensitive": true, "supports_scope": false}]}
```

- Es el catálogo del producto (`apps.access.catalog`), el mismo para todas las organizaciones, ordenado por código. Sale del código, no de la base: es una lista cerrada y no se pagina (no lleva `next`).
- `is_sensitive`: solo lo delega y lo retira un Owner. `supports_scope`: se concede con un alcance.
- Los nombres para mostrar no están aquí: son textos de la interfaz.

## Crear un rol (F2-29, ADR-003 §5)

`POST /api/v1/o/{slug}/roles/` con `{"name": "…", "description": "…"}` crea un rol propio de la organización. Responde 201 con el rol en la forma del directorio. Exige el permiso `roles.manage`.

- **Nace vacío:** sin concesiones, sin miembros, con `is_system` falso y sin la marca de Owner. Un rol vacío no concede nada, así que crearlo no tiene nada que cubrir. Concederle permisos es otro work item (E01-08); asignarlo, «Roles de un miembro».
- **Nombre:** obligatorio, imprimible, legible (no solo marcas, acentos sueltos como «´» o caracteres que no se ven) y de hasta 100 caracteres. Se guarda en forma NFC, sin espacios exteriores y con los interiores reducidos a uno; un espacio de no separación cuenta como un espacio. Un salto de línea o un tabulador dentro del nombre se rechazan (en los extremos se recortan, como los espacios), y también un carácter de anchura cero, incluido el que une emojis. La API mide además los 100 caracteres sobre lo recibido sin sus espacios exteriores: un nombre con muchos espacios interiores repetidos se rechaza aunque guardado quepa; el servicio mide lo que se guarda. `description` es opcional, de hasta 255.
- **Dos roles no se leen igual:** el nombre no puede coincidir con el de otro rol de la organización, tampoco de plantilla, comparando sin distinguir mayúsculas, formas de composición, de anchura o de compatibilidad (superíndices, ligaduras, números en círculo), espacios repetidos, caracteres que no se ven, el guion tipográfico frente al del teclado ni un punto añadido sobre una letra que ya lo lleva, como «i» o «j» (`apps.access.names.key`). No cubre letras de otro alfabeto que se parecen (una «О» cirílica): eso no lo resuelve una validación.
- **Código:** lo genera el servidor a partir del nombre (`Caja y Cobros` → `caja-y-cobros`), con un sufijo si ya está tomado. El cliente no lo elige: lo que envíe en `code` se ignora.
- **Errores:** 400 `VALIDATION_ERROR` con el campo; 403 sin el permiso (antes de mirar el cuerpo); 409 `ROLE_NAME_TAKEN`; 409 `LAST_OWNER` si la organización no tiene rol Owner (OBS-F2-05C-4).
- **Concurrencia:** el alta corre bajo el bloqueo de RBAC de la organización, como los demás cambios: dos altas simultáneas con el mismo nombre van en fila y la segunda recibe el 409. La unicidad del nombre la comprueba el servicio; la base solo exige único el código.
- **Auditoría de tenant:** `role.created`, con el nombre, la descripción y el código, en la misma transacción. Como toda auditoría, pasa por el redactor: un nombre con aspecto de secreto se guarda tal cual en el rol y redactado en el nombre y la etiqueta de la auditoría; el código, que sale de ese nombre, se audita tal cual.

## Renombrar y borrar un rol (F2-38, ADR-003 §5)

`PATCH /api/v1/o/{slug}/roles/{role_id}/` con `{"name": "…", "description": "…"}` cambia lo que se envía y responde 200 con el rol en la forma del directorio. `DELETE` en la misma ruta borra el rol y sus concesiones, y responde 204. Las dos exigen `roles.manage`.

- **El rol Owner no se renombra ni se borra**, sea quien sea el actor, y nadie renombra ni borra un rol que tiene asignado (como al cambiar sus permisos).
- **Renombrar:** el nombre sigue las reglas del alta («Crear un rol») y no puede leerse igual que el de otro rol (409 `ROLE_NAME_TAKEN`); el suyo propio no le estorba. El código del rol no cambia. Lo que no cambia nada responde 200, no escribe y no audita.
- **Renombrar y borrar exigen lo mismo que retirar cada concesión** del rol: el actor las cubre todas, y lo sensible solo lo retira un Owner. Borrar las retira. Renombrar no, pero quien asigna un rol lo elige por su nombre, y la respuesta del `PATCH` enseña las concesiones y los miembros del rol sin pedir `roles.view`. Un rol con una concesión de un permiso retirado del catálogo no se puede renombrar ni borrar por la API (falla cerrado, OBS-F2-05C-3).
- **Lo que cubrir el rol no impide:** intercambiar los nombres de dos roles que el actor cubre, y dos roles vacíos los cubre cualquiera con `roles.manage`. Las concesiones no se mueven, pero quien después concede un permiso a un rol lo elige por su nombre, y lo reciben los miembros del otro. Queda en la auditoría (`role.updated`), y el directorio enseña cuántos miembros tiene cada rol (OBS-F2-29-2). Tampoco impide un nombre con letras de otro alfabeto que se parecen («Crear un rol»).
- **Un rol con miembros no se borra:** 409 `ROLE_IN_USE`, sea cual sea el estado de esas membresías. Primero se les quita («Roles de un miembro»). La negativa por no cubrir el rol (403) va antes que el 409.
- **Un rol de plantilla no se borra** (409 `ROLE_IS_SYSTEM`, modelo de datos §E.3): se renombra y se le cambian los permisos como a uno propio, y su código no cambia. Es una regla del producto, no una autorización: la marca `is_system` no decide quién puede hacer algo.
- **Errores:** 400 `VALIDATION_ERROR` con el campo; 403 `PERMISSION_DENIED` sin decir la regla (sin el permiso, antes de mirar el cuerpo); 404 si el rol no es de la organización, también al repetir un borrado; 409 `ROLE_NAME_TAKEN`, `ROLE_IS_SYSTEM`, `ROLE_IN_USE` o `LAST_OWNER`. Al borrar, el orden es: 403, plantilla, miembros.
- **Auditoría de tenant:** `role.updated` con el antes y el después de lo que cambió, y `role.deleted` con el nombre, la descripción, el código y las concesiones que tenía, en la misma transacción.

## Permisos de un rol (F2-31 y F2-33, ADR-003 §5)

`PUT /api/v1/o/{slug}/roles/{role_id}/permissions/{code}/` deja el rol con ese permiso. El cuerpo lleva `{"scope": "OWN" | "TEAM" | "BRANCH" | "ORGANIZATION"}` si el permiso admite alcance, y va vacío (o con `scope` nulo) si no. Responde 204 sin cuerpo. Exige el permiso `roles.manage`.

- **Reglas:** las de `access.services.grant_permission` (ver «Cambios de RBAC sin escalada»). Nadie concede un permiso que no tiene ni con más alcance; un permiso sensible solo lo delega un Owner; nadie cambia las concesiones de un rol que tiene asignado.
- **Cambiar el alcance:** repetir la ruta con otro alcance lo cambia. El actor debe cubrir el alcance que había y el nuevo: reducir un alcance es retirar parte de una concesión, y retirar exige lo mismo que conceder.
- **El rol Owner no se edita:** sus concesiones no cambian por esta ruta, sea quien sea el actor. Así no puede perder sus permisos sensibles (OBS-F2-25-1). Cuando crece el catálogo lo amplía el job de migraciones, no la API (ADR-018).
- **Efecto:** inmediato y para todos los miembros del rol, en su siguiente petición. A ellos no los cubrió nadie uno a uno: solo se comprueba a quien concede (OBS-F2-29-2).
- **Repetir** la misma concesión responde 204 y no escribe, si el actor pasa las reglas: quien no cubre lo que el rol ya tiene recibe 403 también al repetirlo, y así la respuesta no le dice qué alcance tiene el rol.
- **Errores:** 400 `VALIDATION_ERROR` en `scope` si el alcance no es uno de los cuatro o no corresponde al permiso; 403 `PERMISSION_DENIED` sin decir la regla (y sin `roles.manage`, antes de mirar el cuerpo); 404 si el rol no es de la organización o el permiso no está en el catálogo; 409 `LAST_OWNER` si la organización no tiene rol Owner.
- **Auditoría de tenant:** `role.permission_granted` al conceder y `role.permission_scope_changed` al cambiar el alcance, con el antes y el después, en la misma transacción.
- **Sin step-up MFA** al delegar un permiso sensible: MFA no existe todavía (E01-03).

`DELETE` en la misma ruta retira el permiso al rol. Responde 204 sin cuerpo y exige `roles.manage`.

- **Retirar exige lo mismo que conceder** (`access.services.revoke_permission`): el actor cubre la concesión que retira, con su alcance; un permiso sensible solo lo retira un Owner; nadie cambia las concesiones de un rol que tiene asignado, tampoco un Owner (OBS-F2-29-2). Retirar no da poder a nadie, pero se lo quita a otros.
- **El rol Owner tampoco pierde concesiones**, sea quien sea el actor: como siempre queda un Owner activo, ninguna retirada deja a la organización sin quien la administre.
- **Efecto:** inmediato y para todos los miembros del rol.
- **Repetir:** retirar una concesión que el rol no tiene responde 404.
- **Errores:** 403 `PERMISSION_DENIED` sin decir la regla; 404 si el rol no es de la organización o no tiene esa concesión; 409 `LAST_OWNER` si la organización no tiene rol Owner. Quien tiene `roles.manage` y no `roles.view` aprende así, dentro de su organización, si un rol tiene o no una concesión; sobre el rol Owner y sobre un rol propio la respuesta es siempre 403.
- **Un permiso retirado del catálogo** que un rol aún tenga no se puede retirar por la API: falla cerrado (403), como asignar o quitar ese rol (OBS-F2-05C-3).
- **Auditoría de tenant:** `role.permission_revoked`, con el permiso y el alcance que tenía, en la misma transacción.

## Roles de un miembro (F2-25, ADR-003 §5)

`PUT /api/v1/o/{slug}/members/{membership_id}/roles/{role_id}/` asigna el rol al miembro; `DELETE` en la misma ruta se lo quita. Las dos responden 204 sin cuerpo y exigen el permiso `users.manage`.

- **Reglas:** las de `access.services.assign_role` y `remove_role` (ver «Cambios de RBAC sin escalada»). Nadie cambia sus propios roles; el actor cubre todas las concesiones del rol, con alcance igual o superior; un permiso sensible solo lo delega un Owner; quitar exige lo mismo que asignar; siempre queda un Owner activo. La ruta no añade ni quita ninguna.
- **Efecto:** inmediato. Los permisos se leen en cada petición: el miembro los tiene, o deja de tenerlos, en la siguiente.
- **Repetir:** asignar un rol que ya tiene responde 204 y no escribe. Quitar un rol que no tiene responde 404.
- **Errores:** 403 `PERMISSION_DENIED` (sin el permiso, uno mismo o un rol que el actor no cubre; no dice cuál); 404 si el miembro o el rol no son de la organización; 409 `LAST_OWNER` al quitar el rol Owner al último Owner activo y, en las dos operaciones, si la organización no tiene rol Owner (OBS-F2-05C-4). Para quien tiene `users.manage`, un 403 significa que el miembro y el rol existen, y al quitar, que el miembro lo tiene.
- **Estado del miembro:** no se mira. Se puede asignar o quitar un rol a una membresía invitada, suspendida o dada de baja. Reactivar vuelve a exigir cubrir sus roles (ADR-017 §2); la aceptación de invitaciones (E01-06) debe decidir lo mismo.
- **Pendiente:** el step-up MFA que ADR-003 §5 exige para delegar un permiso sensible llega con MFA (E01-03) y se aplicará a esta ruta.
- **Auditoría de tenant:** `membership.role_assigned` y `membership.role_removed`, que escriben los servicios en la misma transacción.
- `access.permissions.rbac_errors()` da a esas negativas la forma del contrato; lo usa también la suspensión de miembros.

## Suspender y reactivar a un miembro (F2-19, ADR-017)

`PUT /api/v1/o/{slug}/members/{id}/status/` con `{"status": "SUSPENDED"}` o `{"status": "ACTIVE"}`. Responde `{"id": "…", "status": "…"}`. Exige el permiso `users.manage`.

- **Efecto:** suspender revoca todas las sesiones del usuario (ADR-003 §2, F2-20): cada una se destruye en su siguiente petición, y toda ruta que exige sesión responde 401, también fuera de esa organización. Puede volver a iniciar sesión; en esa organización recibe 404 y deja de verla en `GET /api/v1/me/organizations/`. Su cuenta y sus otras organizaciones no cambian. Al reactivarlo vuelve con los roles que tenía. Reactivar, repetir una suspensión y una suspensión denegada no revocan nada.
- **Reglas** (las mismas para suspender y para reactivar): nadie cambia su propia membresía; el actor debe cubrir todas las concesiones de todos los roles del miembro, como para quitárselos. Mientras el rol Owner conserve un permiso sensible (hoy siempre), solo un Owner suspende a un Owner o a otro administrador. Al suspender a un Owner activo debe quedar otro activo.
- **Transiciones:** solo `ACTIVE` ↔ `SUSPENDED`. Repetir la petición responde 200 y no escribe ni audita. `INVITED` y `DEACTIVATED` no se tocan.
- **Errores:** 403 `PERMISSION_DENIED` (sin el permiso, uno mismo o un miembro que el actor no cubre; no dice cuál); 404 si la membresía no es de la organización; 409 `LAST_OWNER` (también si la organización no tiene rol Owner); 409 `INVALID_TRANSITION`; 400 `VALIDATION_ERROR` con otro `status`. Los dos 409 llevan `message`.
- **Auditoría de tenant:** `membership.suspended` y `membership.reactivated`, con el actor y el antes y el después, en la misma transacción que el cambio.
- **Módulos:** `apps.members.services.set_member_status` llama a `access.services.ensure_can_manage_member` (reglas, bajo el bloqueo de RBAC de la organización), después a `organizations.services.set_membership_status` (escritura y auditoría) y, al suspender, a `accounts.services.revoke_sessions`. `organizations.services` no comprueba permisos: solo lo importa `apps.members` (contrato de import-linter).

## La sucursal de un miembro (F2-69, ADR-017)

`PUT /api/v1/o/{slug}/members/{id}/branch/` con `{"branch_id": "<uuid>"}` deja a la membresía con esa sucursal; con `{"branch_id": null}`, sin ninguna. Responde `{"id": "…", "default_branch": {"id", "code", "name"} | null}`. Exige el permiso `users.manage`.

- **Efecto:** la sucursal de una membresía es lo que alcanza una concesión con alcance `BRANCH` (`ExecutionContext.branch_ids`, F2-68), desde la siguiente petición del miembro. Hoy ningún permiso del catálogo admite alcance, así que ninguna respuesta cambia todavía.
- **Reglas, las de suspender:** nadie cambia la suya; el actor debe cubrir todas las concesiones de todos los roles del miembro. Quitar la sucursal exige lo mismo que ponerla. No hace falta ser Owner para un miembro que el actor cubre.
- **Lo que no comprueba:** de qué sucursal es cada concesión `BRANCH`. Quien tiene un permiso con alcance `BRANCH`, en su sucursal o sin tener ninguna, cubre el del miembro y puede llevarlo a cualquier otra, también a una que él no alcanza. Hoy no cambia ninguna respuesta, porque ningún permiso del catálogo admite alcance; es la parte abierta de D-F2-13.
- **La sucursal** tiene que ser de la organización: se busca dentro del tenant antes de escribir. No se mira si está activa ni el estado de la membresía.
- **Repetir** la petición responde 200 con lo mismo y no escribe ni audita.
- **Errores:** 400 `VALIDATION_ERROR` en `branch_id` si falta, no es un UUID o `null`, o no es una sucursal de la organización (una de otra organización responde igual que una que no existe); 403 `PERMISSION_DENIED` (sin el permiso, uno mismo o un miembro que el actor no cubre; no dice cuál); 404 si la membresía no es de la organización, sea cual sea la sucursal; 409 `LAST_OWNER` si la organización no tiene rol Owner. Sin el permiso, 403 antes de mirar nada.
- **Auditoría de tenant:** `membership.branch_changed`, con el actor, el antes y el después (`default_branch`) y el código de la sucursal nueva en `metadata.branch`, en la misma transacción que el cambio.
- **Módulos:** `apps.members.services.set_member_branch` llama a `access.services.ensure_can_manage_member` (reglas, bajo el bloqueo de RBAC de la organización) y después a `organizations.services.set_membership_branch` (escritura y auditoría), que devuelve la sucursal como `BranchRef`: `members` no importa los modelos de `organizations`.

## Recuperación de contraseña: la tabla (F2-86, ADR-021)

`password_resets` guarda los enlaces de recuperación de contraseña emitidos. Las reglas de toda la serie (E01-02) están en [ADR-021](../docs/adr/ADR-021-password-recovery.md); F2-86 dejó la tabla y el modelo, F2-87 la tarea que emite el enlace y lo envía, F2-88 la ruta que lo pide y F2-89 la que cambia la contraseña con él (las tres, abajo). Por API la recuperación funciona de punta a punta; faltan las pantallas.

- **Platform-owned** (ADR-001 §2): sin `organization_id` ni política de tenant, como `users` y `login_throttles`. El runtime la lee y la escribe sin contexto de tenant. Modelo `apps.accounts.models.PasswordReset`.
- **Campos:** `user` (la cuenta), `token_hash` (el SHA-256 del enlace; el enlace no se guarda nunca), `created_at` (cuándo se emitió), `expires_at` y `used_at`. Una fila es un enlace emitido, se haya entregado o no: la crea, antes de entregar el correo, la tarea que lo envía (F2-87, abajo), que es quien genera el enlace (ADR-019 §4, ADR-021 §3).
- **Restricciones en la base:** el hash con forma de SHA-256 en hexadecimal y en minúsculas (`password_resets_token_hash_ck`) y único en la tabla; la caducidad, posterior a la emisión (`password_resets_expires_ck`); el uso, si lo hay, no anterior a la emisión (`password_resets_used_ck`); FK a la cuenta, que no se borra mientras tenga enlaces (`PROTECT` en el ORM y FK en la base). Índice `password_resets_user_idx (user_id, created_at DESC)`, para contar los enlaces de una cuenta y encontrar el último.
- **Lo que la base no comprueba:** que solo valga el último enlace de una cuenta, cuántos se emiten, ni que un enlace usado no se vuelva a usar. Eso es del código: cuántos se emiten lo cuenta la tarea de F2-87, con la cuenta bloqueada (ADR-021 §3); que solo valga el último y que uno usado no se reutilice lo comprueba la ruta que cambia la contraseña (F2-89, abajo), con la fila de la cuenta y la del enlace bloqueadas (§5).
- **`crm_app` conserva `DELETE`:** la serie no borra filas, pero purgarlas será una tarea de mantenimiento del runtime, como la de los contadores de acceso.

## Recuperación de contraseña: emitir y enviar el enlace (F2-87, ADR-021 §3 y §4)

La tarea de plataforma `accounts.send_password_reset(email)` decide si una cuenta recibe un enlace de recuperación, lo emite y lo envía. El trabajo está en `apps.accounts.recovery.issue_link`. La encola la ruta que pide el enlace (F2-88, abajo).

- **Qué recibe:** el correo en su forma canónica (D-F2-3). Busca la cuenta por igualdad.
- **No emite ni envía nada, sin decir por qué,** si el correo no tiene cuenta, si la cuenta está desactivada o es de personal de plataforma, si `core.mail` no escribiría a esa dirección (`deliverable`), o si la cuenta llegó a uno de sus topes.
- **Topes por cuenta** (constantes de `recovery`): 5 enlaces emitidos en 24 horas y 2 minutos entre dos. Se cuentan sobre `password_resets` con la fila de la cuenta bloqueada (`FOR NO KEY UPDATE`): dos tareas a la vez para la misma cuenta emiten uno solo. Cuenta todo enlace emitido: usado, caducado o no entregado.
- **El enlace:** `secrets.token_urlsafe(32)` (256 bits). La fila guarda su SHA-256 (`recovery.token_hash`) y 60 minutos de vida. El secreto solo existe en la memoria de la tarea y en el correo: no se guarda, no se registra y la tarea no devuelve nada (`ignore_result`).
- **Orden:** la fila se confirma y el bloqueo de la cuenta se suelta **antes** de abrir la conexión con el servidor de correo. Un fallo de entrega deja la fila (el enlace cuenta como emitido) y la tarea falla con `MailError`, que no lleva la dirección. La tarea **no reintenta** (`max_retries=0`, sin `acks_late` ni `autoretry_for`): la persona vuelve a pedirlo.
- **«Solo vale el último»** (ADR-021 §3) no escribe nada aquí: lo calcula la ruta que cambia la contraseña (F2-89: el enlace vale si no hay otro más reciente de su cuenta).
- **El correo:** texto plano, con propósito `password_reset`. El enlace es `{APP_ORIGIN}/restablecer#<secreto>`: el secreto va en el fragmento, que el navegador no envía al servidor.
- **`APP_ORIGIN`** (`config.env.origin`): el origen público de la aplicación, por entorno; `http` o `https`, un host en ASCII (un dominio internacionalizado, en su forma IDNA; o una IPv6 entre corchetes) y el puerto si lo lleva; sin ruta, consulta, fragmento, credenciales, espacios dentro ni barras invertidas (si no, el proceso no arranca, y el error no repite el valor); el espacio de alrededor se quita. Nunca sale de una petición: una cabecera `Host` falsa no cambia el enlace. Sin él la tarea falla con un error de configuración antes de mirar la cuenta. En `production`, con correo configurado, debe ser `https`. El stack local pone el del proxy (`http://localhost:8080`).
- **Mientras `production` no tenga servidor de correo:** si tiene `APP_ORIGIN`, cada ejecución para una cuenta que puede recuperarse emite un enlace que no llega y gasta uno de sus 5 del día (ADR-021, «Consequences»); sin `APP_ORIGIN` (sin correo no es obligatorio), la tarea falla antes de mirar la cuenta y no emite ni gasta nada.

## Recuperación de contraseña: pedir el enlace (F2-88, ADR-021 §2 y §7)

`POST /api/v1/auth/password-reset/` con `{"email": "…"}` pide un enlace de recuperación. Es una ruta de plataforma de acceso anónimo: exige CSRF, no sesión. **Responde 204, con el mismo trabajo, exista o no la cuenta**: no la busca. Lo que depende de la cuenta ocurre después, en la tarea (arriba). El trabajo está en `apps.accounts.recovery.request_link`.

- **Orden:** la forma del correo; el límite de intentos; la fila de auditoría; encolar la tarea.
- **400:** lo que no es una dirección de correo (la canonicalización de D-F2-3 la rechaza). No cuenta, no audita y no encola.
- **429 `RATE_LIMITED`, con `Retry-After`:** demasiadas peticiones desde esa dirección de red (20 por hora) o para ese identificador, exista o no la cuenta (5 por hora). Son los valores de `PASSWORD_RESET_THROTTLE`, a confirmar como los del acceso (D-F2-8). Los dos rechazan, por ventana fija, y una petición rechazada no cuenta, no audita y no encola. Los contadores viven en `login_throttles` con claves propias (`rst:ip:…`, `rst:id:…`): pedir enlaces no cambia los contadores del acceso, ni al revés, y los purga la misma tarea con su propia ventana.
- **Auditoría de plataforma:** `auth.password.reset_requested`, con actor anónimo, la huella del identificador, la dirección de red y el agente; sin cuenta, porque la ruta no la mira. Se escribe y se confirma antes de encolar, y **falla cerrado**: si no se puede escribir, no se encola nada y la petición responde 500. `request_link` se niega a correr dentro de una transacción, donde esa fila no se confirmaría.
- **Encolar:** `accounts.send_password_reset` con el correo canónico como argumento. A Celery se le da el argumento enmascarado (`argsrepr`): es lo que ponen sus eventos y su log. Sus inspecciones (`celery inspect active`, `reserved` y `scheduled`) enseñan el argumento real mientras la tarea está en un worker; el enmascarado solo lo da `inspect().active(safe=True)`, desde Python (la CLI no tiene esa opción). El mensaje caduca a los 15 minutos si nadie lo recoge. La publicación no reintenta: usa una conexión propia con el broker, sin reintentos de conexión y con un plazo de 3 segundos (`recovery.BROKER_WAIT`) para conectar y para cada respuesta de Redis (por operación, no para la publicación entera), porque la del pool de Celery reintenta conectar aunque se publique con `retry=False`. Si el broker no acepta la tarea, la petición responde 500, igual para cualquier correo, con su fila de auditoría y su intento contado ya escritos.
- **Lo que no hace:** no dice si la cuenta existe, si recibirá correo ni si llegó a su tope del día; no audita los rechazos por límite; no envía nada dentro de la petición.
- **Despliegue:** el worker debe tener la tarea antes de que esta ruta reciba tráfico. Un worker que recibe una tarea que no conoce escribe el mensaje entero, con el correo, en su log de errores (es de Celery).

## Recuperación de contraseña: cambiar la contraseña (F2-89, ADR-021 §5 a §7)

`POST /api/v1/auth/password-reset/confirm/` con `{"token": "<secreto del enlace>", "password": "…"}` cambia la contraseña de la cuenta del enlace. Es una ruta de plataforma de acceso anónimo: exige CSRF, no sesión. Responde 204. **No inicia sesión:** después se entra por el acceso normal. El trabajo está en `apps.accounts.recovery.change_password`.

- **El enlace vale** si el SHA-256 del secreto está en `password_resets`, no hay otro enlace más reciente de su cuenta, no se usó, no caducó, y la cuenta sigue activa y no es de personal de plataforma.
- **400 `INVALID_RESET_LINK`:** el enlace no vale. La misma respuesta, con el mismo cuerpo, sea cual sea la razón: un secreto que no es de nadie, que no tiene la forma de uno, ya usado, caducado, sustituido por otro más nuevo, o de una cuenta que ya no puede usarlo. No cambia nada.
- **400 `VALIDATION_ERROR` en `password`:** el enlace vale, pero la contraseña no pasa los validadores de la plataforma (se comprueban con la cuenta delante: también el parecido con su correo). La respuesta dice cuáles, y **el enlace no se gasta**. Solo la ve quien tiene el enlace. La contraseña se toma tal cual, sin recortar espacios.
- **Al cambiarla,** en una transacción, con la fila de la cuenta bloqueada (`FOR NO KEY UPDATE`) y después la del enlace, que es el orden de la tarea que emite: la contraseña se guarda con Argon2id, el enlace queda usado, todas las sesiones de la cuenta se revocan (la época de sesión, D-F2-11) y se escribe la auditoría. Dos peticiones a la vez con el mismo enlace cambian la contraseña una sola vez; un enlace emitido a la vez que el cambio espera a que termine.
- **429 `RATE_LIMITED`, con `Retry-After`:** demasiados intentos desde esa dirección de red (10 en 15 minutos, `PASSWORD_RESET_THROTTLE["change"]`, a confirmar). Cuenta todo intento que trae `token` y `password`, valga o no el enlace, y se evalúa antes de mirar el enlace. Un cuerpo que no cumple el contrato (falta un campo, un `token` vacío o de más de 128 caracteres, una contraseña de más de 1024) es un 400 `VALIDATION_ERROR` que no dice nada de ningún enlace: no cuenta ni audita. Su contador es propio (`rst:chg:ip:…`): no es el de pedir enlaces ni los del acceso.
- **Auditoría de plataforma, `auth.password.changed`:** al cambiar, con actor anónimo, la cuenta como entidad y `{"method": "reset_link"}`, en la misma transacción: si no se puede escribir, la contraseña no cambia (500). Un intento con un enlace que no vale deja la misma acción con resultado `DENIED` y un motivo interno (`unknown`, `malformed`, `used`, `expired`, `superseded`, `account`), con la cuenta si el secreto era de una; se escribe fuera de la transacción, y si no se puede escribir la respuesta es el mismo rechazo. Una contraseña rechazada por los validadores no deja fila. Nunca se guarda el secreto, su hash ni la contraseña.
- **Lo que no hace:** no limpia los contadores del acceso (quien estaba bloqueado por intentos fallidos espera igual); no impide repetir la contraseña anterior; no anota cuándo cambió la contraseña (`users.password_changed_at` está en el modelo de datos y no existe todavía). `change_password` se niega a correr dentro de una transacción, donde la fila de un rechazo se desharía con ella.

## Recuperación de contraseña: el aviso de que cambió (F2-93, ADR-021, Consecuencias)

- **Qué es:** un correo a la cuenta cuando su contraseña se cambia con un enlace de recuperación. Dice qué pasó, que sus sesiones se cerraron y qué hacer si no fue ella: cambiar primero la contraseña de su correo (un enlace nuevo llegaría al mismo buzón) y después pedir un enlace nuevo y elegir otra contraseña. Es un control de detección: quien usa un enlace ajeno no puede evitar que la dueña lo sepa, salvo que controle también su correo.
- **Cuándo se encola:** `change_password`, con el cambio ya confirmado, encola la tarea de plataforma `accounts.send_password_changed` con el **identificador de la cuenta**; su correo no viaja por el broker. Publica como la ruta que pide el enlace: por una conexión propia, sin reintentos y con 3 segundos de espera como mucho por operación, no para la publicación entera. Con la contraseña ya cambiada, un broker que no contesta retrasa el 204 unos 3 segundos; uno que contesta despacio, hasta 3 por cada operación de una publicación (17 con redis-py 8.1 y kombu 5.6), más de los 30 segundos que espera la página, que para eso avisa de que la contraseña pudo cambiar (F2-92). Medido a mano contra un Redis falso: no hay test. El mensaje caduca a las 24 horas: el aviso sirve aunque llegue tarde.
- **De mejor esfuerzo:** si no se puede encolar, la contraseña ya cambió y la ruta responde 204 igual. Queda una línea de error en el log y en el reporte de errores, sin datos de la cuenta. Un enlace que no vale o una contraseña rechazada no encolan nada.
- **La tarea** (`recovery.notify_change`) no envía nada si la cuenta no existe, está desactivada o `core.mail` no escribiría a su dirección. No reintenta: si la entrega falla, la tarea falla y ese aviso se pierde (a confirmar: ADR-019 §3 da por hecho que una tarea de correo reintenta, y la razón de ADR-021 §3 para no hacerlo, que cada intento gasta un enlace, no vale para un aviso). No se audita: el cambio ya tiene su fila (`auth.password.changed`).
- **El correo** no lleva ningún secreto ni ningún enlace que cambie nada. Con `APP_ORIGIN` configurado dice la dirección de la página que pide un enlace (`/recuperar`); sin él se envía sin dirección.
- **Despliegue:** el worker antes que la web. Un worker que no conoce la tarea vuelca el mensaje en su log de errores (aquí, el identificador de la cuenta, no su correo) y el aviso se pierde.
- **Lo que no hace:** no avisa de otros cambios de la cuenta; no dice desde qué dirección de red se cambió la contraseña ni cuándo (y puede llegar hasta 24 horas después); mientras producción no tenga servidor de correo (ADR-019 §6) la tarea falla y nadie recibe el aviso.

## Cifrado de secretos en reposo (F2-94, ADR-022)

- **Para qué:** los secretos que la aplicación tiene que poder leer y que por eso no admiten un hash: el secreto TOTP de una cuenta (MFA, E01-03) y, en sus fases, las credenciales de IA y de canales. F2-94 entregó el módulo; **lo usa el alta de TOTP empezada** (F2-97, abajo), que cifra con él el secreto del dispositivo.
- **`core.sealed`** es el único sitio que cifra y descifra: `seal(purpose, owner, plaintext)` devuelve los bytes que se guardan; `unseal(purpose, owner, sealed)` los abre; `stale(sealed)` dice si un valor quedó cifrado con una clave que ya no es la vigente; `current()` da la versión de la clave vigente en el proceso (el número, no la clave), y sin claves lanza como las otras tres. No registra nada, y sus errores no llevan la clave, el texto ni el cifrado.
- **Algoritmo:** AES-256-GCM (`cryptography` 50.0.1, la versión de ADR-012). La clave maestra no cifra datos: de ella se deriva con HKDF-SHA256 una clave por propósito (`mfa.totp`), que es una constante del código.
- **Lo que se guarda:** 1 byte de formato, 2 con la versión de la clave maestra, 12 de nonce aleatorio y el cifrado con su etiqueta. Dos cifrados del mismo secreto dan valores distintos. El texto en claro, de 1 a 4096 bytes.
- **Atado a su sitio:** el propósito y el dueño (`user:<id>`: un identificador estable de la fila dueña) van como datos asociados. Un valor copiado a otra fila, presentado como de otro propósito o con un byte cambiado no se abre: `SealError`, sin decir por qué. Quien llama falla cerrado: no hace lo que pedía el secreto, y no lo confunde con un secreto que falta (una cuenta con un TOTP ilegible sigue teniendo segundo factor).
- **`SECRETS_KEKS`:** las claves maestras, del entorno: `versión:clave` separadas por comas, cada clave 32 bytes en base64 para URL, versiones de 1 a 65535 sin repetir. Cifra la de versión más alta; abren todas las presentes. Un valor mal formado impide arrancar, y el error solo nombra la variable.
- **Sin `SECRETS_KEKS`:** `seal`, `unseal` y `stale` lanzan `ImproperlyConfigured` (falla cerrado). **`production` no arranca sin ella** desde F2-97, que es la primera ruta que guarda un secreto cifrado (ADR-022 §6); nunca deriva una de desarrollo. `local` y `test` derivan una clave de desarrollo de su `SECRET_KEY`, **que no protege nada** (esa `SECRET_KEY` por defecto está en el repositorio); cambiarla deja ilegible lo cifrado en local. El job de migraciones no guarda las claves aunque se las den.
- **Rotar:** añadir una clave con una versión más alta y arrancar con ella todos los procesos, parados antes (en un despliegue escalonado, o reiniciándolos sin pararlos, el que aún no la tiene no abre lo que otro ya cifró con ella), sin quitarla si el despliegue se revierte; volver a cifrar lo que `stale` señala; retirar la anterior cuando no quede nada cifrado con ella. Retirarla antes deja ilegible lo que dependía de ella, hasta que vuelva. El trabajo que vuelve a cifrar es la tarea `accounts.reseal_mfa_secrets` (F2-98, abajo): la clave anterior se puede retirar cuando una pasada hecha con la clave nueva (su `key`) da cero en sus tres cuentas.
- **Lo que no hace:** no guarda nada en ninguna tabla; no audita (se audita la operación que lo usa); no protege de un proceso de runtime comprometido, que tiene las claves en su entorno.

## MFA: las tablas (F2-95, ADR-023)

- **Qué hay:** las dos tablas del segundo factor de una cuenta y sus reglas ([ADR-023](../docs/adr/ADR-023-mfa-totp.md)). **Las usan el alta empezada** (F2-97, abajo), que deja en `user_mfa_devices` un dispositivo sin confirmar, **y su confirmación** (F2-100, abajo), que lo confirma y guarda los códigos de respaldo en `user_mfa_backup_codes`. Nada las lee todavía para dejar entrar.
- **Platform-owned** (ADR-001 §2): sin `organization_id` ni política de tenant, como `users` y `password_resets`. El runtime las lee y las escribe sin contexto de tenant. Modelos `apps.accounts.models.MfaDevice` y `MfaBackupCode`.
- **`user_mfa_devices`:** `user` (la cuenta), `type` (hoy solo `TOTP`), `secret`, `created_at`, `confirmed_at`, `last_step` y `failed_codes`. Como mucho un dispositivo TOTP por cuenta (`user_mfa_devices_user_type_uq`).
  - `secret` es el secreto TOTP cifrado con `core.sealed` (propósito `mfa.totp`, dueño `user:<id de la cuenta>`): copiado a la fila de otra cuenta no se abre. La columna solo admite algo con la forma de un valor cifrado: de 32 a 4127 bytes y con el byte de formato de `core.sealed` delante (`user_mfa_devices_secret_ck`). Un secreto TOTP en claro no entra: sus 20 bytes no llegan, y escrito en base32 (32 caracteres) o en hexadecimal (40) no empieza por ese byte. Un formato nuevo de `core.sealed` pide cambiar antes esta restricción. La base no comprueba que de verdad esté cifrado.
  - Sin `confirmed_at` es un alta a medias: no protege ni exige nada. No se confirma antes de crearse (`user_mfa_devices_confirmed_ck`).
  - `last_step` es el último paso de tiempo cuyo código se aceptó. Lo tiene todo dispositivo confirmado, y solo él, y es mayor que cero (`user_mfa_devices_last_step_ck`): confirmar es aceptar un paso, para que el código que confirma no valga otra vez (ADR-023 §4).
  - `failed_codes` son los códigos que no valieron al confirmar un alta a medias ([ADR-024](../docs/adr/ADR-024-mfa-enrollment-code-cap.md)). Un alta guarda de 0 a 4, porque el quinto la borra, y un dispositivo confirmado guarda 0 (`user_mfa_devices_failed_codes_ck`).
- **`user_mfa_backup_codes`:** `user`, `code_hash` (el SHA-256 del código; el código no se guarda nunca), `created_at` y `used_at`. El hash tiene forma de SHA-256 en hexadecimal y en minúsculas (`user_mfa_backup_codes_code_hash_ck`) y no se repite dentro de una cuenta (`user_mfa_backup_codes_user_hash_uq`); que otra cuenta tenga el mismo no se nota. No se gasta antes de existir (`user_mfa_backup_codes_used_ck`).
- **La cuenta no se borra** mientras tenga dispositivo o códigos (`PROTECT` en el ORM y FK en la base).
- **Lo que la base no comprueba:** que un dispositivo sin confirmar caduque, que un paso no se repita, cuántos códigos de respaldo hay ni cuántos intentos se hacen contra un dispositivo confirmado. Eso será del código que las use (ADR-023 §2, §4, §5 y §6).
- `core.sealed.unseal` y `stale` aceptan lo que devuelve una columna binaria (`memoryview`).

## MFA: calcular y comprobar un código TOTP (F2-96, ADR-023 §2)

- **`apps.accounts.totp`**, con la biblioteca estándar: RFC 6238 sobre RFC 4226, HMAC-SHA-1, 6 dígitos y pasos de 30 segundos. Lo llaman el alta empezada (F2-97: `new_secret`, `as_text` y `uri`), su confirmación (F2-100: `accepted_step`) y `second_factor.accept` (F2-103, abajo: `accepted_step`), que llega a la ruta del segundo paso del acceso (F2-105, abajo). No guarda nada: el secreto entra y sale como bytes (lo cifra y lo guarda el alta, con `core.sealed`) y el último paso aceptado lo trae y lo guarda quien llama.
- **`new_secret()`:** 20 bytes aleatorios. **`as_text(secret)`:** su forma en base32 sin relleno, que es la que una persona teclea. **`uri(secret, issuer=, account=)`:** la URI `otpauth://totp/…` de un código QR, con todo escapado. Las dos llevan el secreto: se enseñan una vez, en el alta.
- **`code(secret, step)`** es el código de un paso de tiempo; **`step_at(seconds)`**, el paso de un instante.
- **`accepted_step(secret, presented, now=, last=)`** devuelve el paso cuyo código es el presentado, o `None` sin decir por qué. Acepta el paso actual, el anterior y el siguiente (hasta 30 segundos de desfase de reloj), y **solo un paso posterior a `last`**, el último aceptado: el código de un paso ya usado, o de uno anterior, no vale aunque sea correcto. Si dos pasos de la ventana dan el mismo código (uno de cada millón de pares), devuelve el último, para que ese código no valga otra vez por el otro paso. A cambio, a quien lo presentó por el primero de los dos se le rechaza el siguiente código de su aplicación, o los dos siguientes si su reloj va un paso por detrás y los dos pasos no son vecinos (uno de ellos, un código que nunca presentó): espera hasta 60 o 90 segundos, en vez de hasta 30. La regla es por paso, no por las seis cifras: las mismas cifras valen de nuevo si son el código de un paso posterior que entra en la ventana más tarde, como cualquier otro acierto a ciegas. Quien llama guarda el paso devuelto como el nuevo `last`, en la misma transacción.
- Compara en tiempo constante (`hmac.compare_digest`) con los tres pasos de la ventana, sin salir al encontrar el que coincide. Lo que no son seis dígitos ASCII (dígitos de otra escritura, espacios, un salto de línea) no vale, y hace las mismas comparaciones.
- **Tests:** los diez valores de RFC 4226 (apéndice D) y los seis instantes de RFC 6238 (apéndice B, SHA-1; con 6 dígitos valen los seis últimos de cada vector).
- **Lo que no hace:** ni límite de intentos, ni auditoría, ni sesión (son de las rutas que lo usen, ADR-023 §5); no admite otros algoritmos ni 8 dígitos; la hora la da quien llama.

## MFA: empezar el alta de un dispositivo (F2-97, ADR-023 §3, §4 y §10)

- **`POST /api/v1/auth/mfa/totp/`** con `{password}`: ruta de plataforma, con sesión y CSRF. Responde 201 con `secret` (el secreto en base32), `uri` (la `otpauth://` para un código QR, con el emisor `Good Doggy CRM` y el correo de la cuenta) y `expires_in` (600 segundos). El trabajo está en `apps.accounts.mfa.start_enrollment`. **Es la única vez que el secreto sale de la aplicación**; la respuesta no se guarda en caché (`Cache-Control: no-store`, como toda la API).
- **Lo que deja:** un dispositivo TOTP sin confirmar de la cuenta, con su secreto cifrado (`core.sealed`: propósito `mfa.totp`, dueño `user:<id>`). Sustituye al que hubiera sin confirmar: siempre hay como mucho uno. **No protege ni exige nada** hasta que se confirme (F2-100, abajo), y solo se puede confirmar en los 10 minutos de ADR-023 §4; pasado ese plazo sigue en la tabla hasta que otra alta la sustituye.
- **La contraseña actual es obligatoria:** una sesión sola no ata un factor a la cuenta. Si no es la de la cuenta: 400 `VALIDATION_ERROR` en `password` (código `invalid`), no un 401, que es la respuesta de quien no tiene sesión. El intento cuenta en el límite del acceso para esa cuenta y esa dirección (los mismos contadores: cinco fallos bloquean también el acceso desde esa dirección) y deja una fila de auditoría denegada. Con la contraseña correcta el intento se devuelve, como en un acceso correcto (se borra el contador de esa cuenta en esa dirección): empezar altas no bloquea el acceso, y tampoco tiene un límite propio. Se toma tal cual, sin recortar espacios.
- **409 `MFA_ALREADY_ENABLED`:** la cuenta ya tiene un dispositivo confirmado. No se sustituye, se pueda leer su secreto o no (ADR-023 §3): primero hay que quitarlo.
- **429 `RATE_LIMITED`, con `Retry-After`:** el límite de intentos del acceso, que se evalúa antes de mirar la contraseña. Un bloqueo que empieza con una contraseña incorrecta de esta ruta no deja la fila `auth.login.throttled` del acceso: quedan las filas denegadas de cada intento.
- **Auditoría de plataforma, `auth.mfa.enrollment_started`** (el nombre lo dejó ADR-023 §10 a este work item): con la cuenta como actor y como entidad, en la transacción que guarda el dispositivo y con la fila de la cuenta bloqueada: si no se puede escribir, no queda alta empezada ni se borra la anterior (500). Una contraseña incorrecta deja la misma acción con resultado `DENIED` y `{"reason": "password"}`, fuera de la transacción; si no se puede escribir, la respuesta es la misma. Nunca se guarda el secreto ni la contraseña.
- **Sin claves de cifrado** la ruta falla (500) y no guarda nada. En `production` eso ya no puede pasar: no arranca sin `SECRETS_KEKS`. Las comprobaciones que arrancan con esos ajustes (`make check`, CI) reciben una clave generada en el momento.
- **Lo que no hace:** no confirma el alta ni genera códigos de respaldo; no avisa por correo; `start_enrollment` se niega a correr dentro de una transacción, donde la fila de un rechazo se desharía con ella.

## MFA: volver a cifrar tras una rotación de clave (F2-98, ADR-022 §5)

- **Qué es:** el paso 2 de una rotación. `apps.accounts.mfa.reseal` recorre `user_mfa_devices` y vuelve a cifrar con la clave vigente cada secreto que `core.sealed.stale` señala. Es, con el alta y con `second_factor.accept` (F2-103, abajo), lo único que abre un secreto TOTP: lo abre y lo vuelve a cerrar en memoria, sin devolverlo ni registrarlo.
- **La tarea** de plataforma `accounts.reseal_mfa_secrets` corre a diario (03:47 UTC) y devuelve `key` (la versión de la clave vigente en el proceso que hizo la pasada) y tres cuentas: `resealed` (los que volvió a cifrar), `unreadable` (los que no pudo abrir) y `busy` (los que otra operación tenía bloqueados y quedan para la siguiente pasada). Las deja también en una línea del log del worker (`secretos TOTP: clave 7, vueltos a cifrar 2, ilegibles 0, ocupados 0`), que es donde se leen: no hay backend de resultados y la línea de Celery con el resultado de una tarea está apagada. Esa línea es de nivel `INFO`: con `DJANGO_LOG_LEVEL` por encima de `INFO` no sale, y entonces no hay pasada que leer. **La clave anterior se puede retirar cuando una pasada cuya `key` es la versión nueva da cero en las tres**, con todos los procesos arrancados ya con ella (ADR-022 §5; esto último no lo comprueba nada). Un worker que aún no tiene la clave nueva da cero en las tres sin haber vuelto a cifrar nada, mientras nadie haya cifrado con ella: su `key` es la anterior. Sin claves la tarea falla, también con la tabla vacía. Sin rotación no encuentra nada que hacer.
- **Cada fila pendiente en su transacción**, bloqueada, y solo cambia `secret`: `confirmed_at` y `last_step` no se tocan. Una fila que otra operación tiene bloqueada no se espera. La pasada lee la tabla una vez sin bloquear nada, y solo bloquea, y vuelve a mirar ya bloqueada, la fila que `stale` señala: un día sin rotación es una lectura.
- **Lo que no se abre no se toca:** su clave ya no está en `SECRETS_KEKS`, o el valor no es de esa cuenta. Se cuenta, y queda una línea de error en el log y un reporte de errores (`UnreadableSecrets`, con el número y nada más). Si la clave vuelve, la siguiente pasada lo recupera.
- **Un proceso que aún no tiene la clave nueva** ve como pendiente lo que otro ya cifró con ella, no lo puede abrir y no lo pisa con la vieja: lo cuenta como ilegible. Es el hueco de un despliegue escalonado (ADR-022 §5), y por eso una clave nueva se introduce con todos los procesos parados.
- **No es una limpieza:** no distingue un valor legítimo de uno fabricado con una clave comprometida que siga en el ajuste (ADR-022 §5).
- **Lo que no hace:** no caduca ni purga las altas sin confirmar (también las vuelve a cifrar); no hay otras tablas con secretos; no activa una clave aparte de repartirla; recorre la tabla entera en cada pasada, que es una fila por cuenta con segundo factor.

## MFA: generar y reconocer un código de respaldo (F2-99, ADR-023 §6)

- **`apps.accounts.backup_codes`**, con la biblioteca estándar. Lo llama la confirmación del alta (F2-100, abajo), que guarda los hashes en `user_mfa_backup_codes`, y `second_factor.accept` (F2-103, abajo), que gasta uno: lo usa la ruta del segundo paso del acceso (F2-105, abajo). El módulo no guarda nada.
- **`new_codes()`:** los diez códigos de una cuenta, distintos entre sí. Cada uno, 16 símbolos aleatorios (80 bits) del base32 de Crockford (`0123456789ABCDEFGHJKMNPQRSTVWXYZ`: sin I, L, O ni U), como se enseñan una vez: en grupos de cuatro, `7K3M-9QXA-0B1C-ZZ2Y`.
- **`normalized(presented)`:** lo que una persona teclea, como el código que se generó: en mayúsculas, sin guiones ni espacios, y con las letras que se leen como un dígito por su dígito (`I` y `L` por `1`, `O` por `0`). `None` si después no son 16 símbolos del alfabeto: otra longitud, una `U`, un tabulador o un salto de línea, o cualquier carácter que no sea ASCII (se mira antes de pasar a mayúsculas).
- **`code_hash(presented)`:** el SHA-256, en hexadecimal y en minúsculas, del código normalizado: lo único que se guarda, y por lo que se busca. `None` si lo presentado no es un código; quien llame lo tratará como un código que no vale, sin error distinto.
- La normalización solo junta con su dígito letras que no están en el alfabeto: dos códigos generados distintos no dan nunca el mismo hash.
- **Por qué un hash rápido:** con 80 bits aleatorios, quien lea una copia de la base de datos no recupera los códigos (ADR-023 §6).
- **Lo que no hace:** no guarda, gasta ni regenera códigos; no limita intentos ni audita; no decide si lo presentado en una ruta es un código de respaldo o uno TOTP (uno de seis dígitos no es un código de respaldo).

## MFA: confirmar el alta de un dispositivo (F2-100, ADR-023 §2, §3, §4, §6 y §10)

- **`POST /api/v1/auth/mfa/totp/confirm/`** con `{code}`: ruta de plataforma, con sesión y CSRF. Con un código válido del dispositivo sin confirmar responde 200 con `backup_codes`, los diez códigos de respaldo. **Es la única vez que salen**; la respuesta no se guarda en caché. El trabajo está en `apps.accounts.mfa.confirm_enrollment`.
- **Desde ese momento la cuenta tiene segundo factor,** y **se le pide al entrar** (F2-106, abajo): la contraseña correcta ya no abre sesión, y la abre el segundo paso.
- **Qué alta vale:** la empezada hace 10 minutos como mucho y con menos de 5 códigos fallidos. Sin alta, caducada o agotada: 409 `MFA_ENROLLMENT_NOT_STARTED` (hay que empezar otra). Ya confirmada: 409 `MFA_ALREADY_ENABLED`.
- **En una transacción,** con la fila de la cuenta bloqueada (`FOR NO KEY UPDATE`) y releída, y después la del dispositivo: `confirmed_at` y `last_step` (el paso del código: no vale después para entrar), los diez hashes en `user_mfa_backup_codes` (sustituyen a los que hubiera), la época de sesión avanzada (las demás sesiones de la cuenta dejan de valer: se abrieron sin segundo factor) y la fila de auditoría `auth.mfa.enabled`. Sin esa fila no hay nada de lo anterior (500).
- **La sesión que confirma sigue:** adopta la época nueva, cambia de ID y anota `mfa_at` (cuándo se verificó el segundo factor, para el step-up). Solo si su época era la de la cuenta leída bajo el bloqueo, la cuenta sigue activa y la fila de la sesión sigue existiendo: si una revocación (un cambio de contraseña, una suspensión) o un cierre de esa sesión llegó con la petición en curso, **no se confirma nada**, la sesión se cierra y la respuesta es 401. Un cierre de sesión que llega después de esa comprobación, con la transacción ya en marcha, no se ve: borra el ID anterior y la sesión sigue con el nuevo, el que lleva la respuesta de la confirmación (sin test).
- **400 `VALIDATION_ERROR` en `code`** (código `invalid`): el código no vale, sin decir por qué: incorrecto, de fuera de la ventana, que no son seis dígitos, o un secreto que no se abre (ADR-023 §3; eso último además se reporta como error de operación, `UnreadableSecrets`). Cuenta en el límite del acceso para esa cuenta y esa dirección, como una contraseña incorrecta al empezar el alta, y deja `auth.mfa.enabled` con resultado `DENIED` y `{"reason": "code"}`, fuera de la transacción. **Solo un acierto devuelve lo que contó,** en la transacción de la confirmación (como una contraseña correcta, borra el contador de esa cuenta en esa dirección; si no puede, no confirma nada). Un 409 o un 401 no evalúan ningún código y quedan contados: si devolvieran, quien tiene una sesión borraría con ellos los fallos de contraseña de esa dirección. No dejan fila de auditoría, y al llegar al límite bloquean a esa cuenta en esa dirección: también su acceso y empezar otra alta (una pantalla no debe reintentar un 409). Un bloqueo que empieza en esta ruta no deja la fila `auth.login.throttled` del acceso. El tope por cuenta de ADR-023 §5 no aplica aquí.
- **Tope por alta (F2-101, [ADR-024](../docs/adr/ADR-024-mfa-enrollment-code-cap.md)):** cada código que no vale suma además un fallo al alta (`failed_codes`), con la fila del dispositivo bloqueada, venga de la dirección que venga. El quinto **borra el alta**: la respuesta es el mismo 400, su fila de auditoría lleva `{"reason": "code_limit"}`, y la petición siguiente responde 409 `MFA_ENROLLMENT_NOT_STARTED`, también con el código correcto. Empezar otra pide la contraseña y parte de cero. Así quien tiene una sesión robada y no la contraseña tiene cinco intentos por alta que la dueña empiece (15 entre un millón), los haga desde las direcciones que los haga. Puede agotarle el alta, y **nadie avisa a la cuenta**: lo nota en el 409 del intento siguiente. Un 409, un 401, un 429 o un cuerpo que no cumple el contrato no evalúan ningún código y no suman.
- **Aviso por correo** a la cuenta (`accounts.send_mfa_enabled`, con su identificador), encolado con la confirmación ya guardada y de mejor esfuerzo, como el de F2-93: si no se puede encolar, el segundo factor ya está dado de alta y la respuesta es la misma. Dice qué pasó y, por si no fue ella, que cambie la contraseña con un enlace de recuperación (cierra todas las sesiones, también la de quien lo hizo) y que avise, le llegue o no ese enlace (al personal de plataforma no le llega: ADR-021 §6): cambiarla no quita el segundo factor. Sin códigos ni secretos; la tarea no reintenta.
- **Lo que no hace:** no lo quita (F2-109, abajo) ni regenera los códigos (F2-110, abajo); no caduca ni purga las altas que nadie confirma (la agotada sí se borra); `confirm_enrollment` se niega a correr dentro de una transacción.

## MFA: el tope por cuenta de códigos fallidos (F2-102, ADR-023 §5)

- **Qué hay:** `apps.accounts.code_limit`, la pieza que limita cuántos códigos del segundo factor se fallan contra una cuenta: como mucho 10 en 24 horas, vengan de donde vengan. La compartirán todas las rutas que comprueben un código de un dispositivo confirmado (el segundo paso del acceso, el step-up, quitar el factor y regenerar los códigos de respaldo). La llama `verify` (F2-104, abajo), y a `verify`, la ruta del segundo paso del acceso (F2-105), la que quita el factor (F2-109) y la que regenera los códigos de respaldo (F2-110).
- **`admit(user_id)`** cuenta el intento **antes** de que se compruebe el código, con la fila de la cuenta bloqueada (`FOR NO KEY UPDATE`): dos intentos simultáneos con nueve contados no se admiten los dos. Devuelve el intento (`Attempt`); su `last` dice que con él se alcanza el tope. Con el tope alcanzado lanza `Refused`, con los segundos que faltan, y ese intento no cuenta: se rechaza también a quien traería el código correcto. Se llama fuera de toda transacción (se niega si no): el intento queda contado aunque la de quien llama se deshaga.
- **`give_back(attempt)`** borra el intento cuyo código valió, en la transacción que lo acepta. Si esa transacción se deshace, sigue contado.
- **Ventana deslizante:** cada intento es una fila de `user_mfa_code_attempts` (la cuenta y la hora, con el reloj de la base de datos; nunca el código) y deja de contar a las 24 horas de contarse. El tope se levanta solo, intento a intento; con un contador por ventana fija cabrían 20 fallos seguidos a caballo de dos ventanas.
- **Nada lo pone a cero:** tampoco cambiar la contraseña (ADR-023 §5), ni un código correcto, que solo devuelve el suyo.
- **`MFA_CODE_THROTTLE`** (`(10, 86400)`: intentos y ventana en segundos) solo puede endurecer ADR-023 §5: con más de 10 intentos o una ventana de menos de 24 horas, ningún proceso arranca.
- **Purga:** la tarea horaria `accounts.purge_login_throttles` borra también los intentos que salieron de la ventana.
- **La tabla** es platform-owned (ADR-001 §2), como las otras dos del segundo factor; índice por cuenta y hora (`user_mfa_code_attempts_user_ix`). La cuenta no se borra mientras le queden filas (`PROTECT`), cuenten o no: salen con la purga horaria, hasta una hora después de dejar de contar, y cada intento nuevo alarga el plazo.
- **Lo que piden las rutas que lo llamen:** comprobar antes de `admit` todo lo que no es el código (la marca de acceso a medias o la sesión, la contraseña donde se pide, que hay un dispositivo confirmado): `admit` cuenta sin preguntar quién llama. Y tratar `Attempt.last` como un aviso que se repite: lo lleva cada intento admitido al que le falta uno para el tope, hasta diez veces por ventana si los fallos siguen, y también cuando otro intento en curso se devuelve después.
- **Lo que no hace:** no pone plazo a la espera por la fila de la cuenta (ningún ajuste define `lock_timeout`); no comprueba ningún código; no audita ni avisa al alcanzarse el tope (ADR-023 §5 y §10: lo hace `verify`, F2-104, con `Attempt.last`); no cuenta los códigos de un alta sin confirmar, que tienen su tope por alta (ADR-024).

## MFA: dar por bueno un código (F2-103, ADR-023 §2, §3, §5 y §6)

- **Qué hay:** `apps.accounts.second_factor.accept(account, presented)`, la pieza que decide si lo presentado vale como segundo factor de una cuenta y **lo gasta** al aceptarlo. La compartirán el segundo paso del acceso, el step-up, quitar el factor y regenerar los códigos de respaldo. La llama `verify` (F2-104, abajo), y a `verify`, la ruta del segundo paso del acceso (F2-105), la que quita el factor (F2-109) y la que regenera los códigos de respaldo (F2-110).
- **Devuelve el método** con el que valió, `totp` o `backup_code` (los nombres con que el acceso lo auditará, ADR-023 §5), o `None` si no vale, sin decir por qué.
- **Seis dígitos son un código TOTP:** vale el del paso actual, el anterior o el siguiente, y solo de un paso posterior al último aceptado (`last_step`). Al aceptarlo guarda ese paso: ni él ni uno anterior vuelven a valer.
- **Cualquier otra cosa se mira como código de respaldo** de esa cuenta, escrito como lo teclee una persona (mayúsculas o minúsculas, con guiones o espacios). Vale si no está gastado; al aceptarlo queda gastado (`used_at`), con un `UPDATE` que solo toca uno sin gastar.
- **En la transacción de quien llama,** con la fila del dispositivo bloqueada antes de mirar nada: dos peticiones con el mismo código no pasan las dos, y si esa transacción se deshace, el código no queda gastado. Fuera de una transacción se niega. La hora de un código es la de cuando se mira, con la fila ya tomada.
- **Lo que piden las rutas que lo llamen** (lo hace por ellas `verify`, F2-104, abajo)**:** bloquear **antes** la fila de la cuenta (`FOR NO KEY UPDATE`), como la confirmación del alta: la cuenta y después el dispositivo. La que tocara la cuenta después de `accept` (revocar las sesiones al quitar el factor) se cruzaría con la que lleva ese orden, y la base deshace a una de las dos (un 500). Y la que solo admite un código TOTP (regenerar los códigos, ADR-023 §6) recibe también `backup_code`, ya gastado: si lo rechaza sin deshacer su transacción, ese código se pierde sin haber servido.
- **Sin dispositivo confirmado** lanza `NoDevice`, que no es «no vale»: un alta a medias no comprueba nada. Quien llama lo comprueba antes de contar el intento y aun así lo trata: el factor se pudo quitar entre esa comprobación y la llamada, y ese intento queda contado. Nunca es un acierto: con él no se abre sesión ni se da nada por verificado.
- **Un secreto que no se abre** es un código TOTP que no vale (ADR-023 §3) y se reporta como error de operación (`UnreadableSecrets`, acción `mfa.accept`), en cada intento: como mucho los que deja pasar el tope por cuenta, que la ruta cuenta antes. Un código de respaldo no necesita el secreto y sigue valiendo.
- **Lo que no hace:** no cuenta el intento (antes, `code_limit.admit`; si vale, `give_back`), no audita y no abre sesión; no recorta espacios alrededor de un código TOTP (los serializadores ya los quitan) ni junta seis dígitos escritos con un espacio o un guion en medio (`123 456`, como los enseñan algunas aplicaciones: aquí no valen; los junta `second_factor.typed`, que cada ruta llama antes de `verify`); no dice cuántos códigos de respaldo quedan (lo dice `GET /api/v1/auth/mfa/`, F2-108, abajo). Un `used_at` anterior al `created_at` del código (relojes desajustados entre procesos) lo rechaza la base y sería un 500: falla cerrado.

## MFA: el procedimiento para comprobar un código (F2-104, ADR-023 §5 y §10)

- **Qué hay:** `apps.accounts.second_factor.verify`, lo que llama una ruta para comprobar un código de un dispositivo confirmado. Lleva en un sitio el orden que piden `code_limit` y `accept` (arriba), para que el segundo paso del acceso, el step-up, quitar el factor y regenerar los códigos no lo repitan cada uno a su manera. Lo llaman la ruta del segundo paso del acceso (F2-105, abajo), la que quita el factor (F2-109, abajo) y la que regenera los códigos de respaldo (F2-110, abajo).
- **`verify(user_id, presented, *, actor, client, ready, accepted)`**, fuera de toda transacción:
  1. Cuenta el intento (`code_limit.admit`). Con el tope alcanzado lanza `Capped`, con los segundos que faltan, sin mirar el código: tampoco vale el correcto.
  2. En una transacción, bloquea la fila de la cuenta (`FOR NO KEY UPDATE`), la relee y llama a `ready(account)`: ahí la ruta comprueba bajo el bloqueo lo que no es el código (la marca de acceso a medias y su época; la sesión; la contraseña). Una cuenta que ya no está activa no llega a `ready`: `verify` lanza `User.DoesNotExist`, como con una que no existe (ADR-023 §5: el segundo paso lo comprueba antes de mirar el código). Si `ready` lanza, o la cuenta no está activa, no se probó nada y el intento se devuelve; si devolverlo falla, queda contado y, si es el que alcanza el tope, deja su fila y su aviso.
  3. `accept`: bloquea el dispositivo y decide. Si el código no vale lanza `mfa.WrongCode`, y el intento queda contado.
  4. Si vale, `accepted(account, method)` hace lo de la ruta en esa transacción (abrir la sesión, auditar) y el intento se devuelve con ella. Si `accepted` lanza, todo se deshace: el código no queda gastado y el intento queda contado, como en el acceso. Así rechaza un `backup_code` la ruta que solo admite TOTP (regenerar los códigos): lanzando dentro de `accepted`. El coste: un código correcto cuya ruta falla después (una auditoría que no se puede escribir) cuenta como un fallo; diez así alcanzan el tope, y nada lo pone a cero.
- **Al alcanzarse el tope** (queda contado el intento que hace diez): una fila de auditoría de plataforma `auth.mfa.throttled`, denegada, con la cuenta como entidad afectada y el tope y la ventana en sus metadatos, y un aviso por correo a la cuenta (`accounts.send_mfa_code_limit`). El actor de la fila es el que diga la ruta: anónimo en el segundo paso del acceso, donde no hay sesión. Las dos cosas son de mejor esfuerzo y van fuera de la transacción: si fallan, quedan en el log y en el reporte, y la respuesta no cambia (ADR-013 §5).
- **El aviso** dice qué pasó, que la cuenta no acepta códigos durante la ventana (las horas que diga `MFA_CODE_THROTTLE`, hacia arriba), tampoco los correctos, y qué hacer si no fue ella: cambiar la contraseña con un enlace de recuperación, que cierra las sesiones pero no adelanta el final de la espera. Sin códigos ni enlaces. No se envía a una cuenta desactivada. La tarea no reintenta.
- **Lo que sigue siendo de cada ruta:** comprobar sin bloqueo, antes de llamar, lo mismo que `ready` (`admit` cuenta sin preguntar quién llama); la fila de auditoría de cada código que no vale (`auth.login.failed` en el acceso); el límite por dirección; y tratar `NoDevice`, que sale tal cual y con el intento contado, y `User.DoesNotExist` (la cuenta se desactivó mientras tanto), con el intento devuelto: sin tratarlas, las dos acaban en un 500.
- **Lo que no hace:** no limita cuántos avisos recibe una cuenta: en un ataque sostenido el tope se alcanza de nuevo cada vez que un intento sale de la ventana, hasta diez veces al día.

## MFA: la ruta del segundo paso del acceso (F2-105, ADR-023 §5, §6 y §10)

- **`POST /api/v1/auth/login/mfa/`** con `{code}`: ruta de plataforma, de acceso anónimo, con CSRF. Completa un acceso que la contraseña dejó a medias: con un código de la aplicación de autenticación, o uno de respaldo, responde 200 con lo mismo que el acceso y abre la sesión. El trabajo está en `apps.accounts.second_step.complete`.
- **La marca de acceso a medias la deja el acceso** (F2-106, abajo) cuando la contraseña correcta es de una cuenta con dispositivo confirmado. La ruta se construyó antes que eso, para que ningún estado de `main` dejara fuera a una cuenta con segundo factor.
- **La marca** vive en la sesión del navegador, bajo `mfa_pending`: la cuenta, su época de sesión, la hora y las claves del límite por dirección con que se contó la contraseña. **Al dejarla acaban los bloqueos que empezaron con esa contraseña** (`throttle.release`): era la correcta, y en el acceso el intento que llega al umbral y acierta tampoco deja bloqueo. Lo contado sigue contado hasta completar el segundo paso (ADR-023 §5), y el bloqueo acaba en ese instante, que queda como referencia de la calma: una contraseña correcta sin el código no hace que un contador vuelva a empezar. Si el bloqueo siguiera, rechazaría con 429 el segundo paso de quien acertó, y el primero de `ip` dura lo mismo que la marca. No es una sesión de usuario: `request.user` es anónimo y toda ruta con sesión responde 401. Vale 5 minutos.
- **401 `MFA_LOGIN_NOT_STARTED`**, y la marca se borra: sin marca, con una caducada o que no está entera, o si la cuenta ya no está activa, cambió de época (cambió la contraseña, se revocaron sus sesiones) o no tiene dispositivo confirmado. Se comprueba antes de contar nada y otra vez con la fila de la cuenta bloqueada: una revocación que llega con la petición en curso no abre sesión. **Una marca abre una sola sesión:** con la fila bloqueada se mira además que la sesión de la marca siga existiendo (completar el acceso la borra). De dos peticiones simultáneas con la misma marca y dos códigos válidos, la segunda recibe este 401 sin que se toque su sesión, su código no se gasta y su intento queda contado en el límite por dirección; el de la contraseña se devuelve una vez. La pantalla vuelve a la contraseña.
- **Código válido,** en una transacción (`second_factor.verify`): la fila `auth.login.succeeded` con `{"method": "totp"}` o `{"method": "backup_code"}` (sin esa fila no hay sesión: 500, y el código no queda gastado), los dos intentos del límite por dirección devueltos (este y el de la contraseña, que esperaba al segundo paso: ADR-023 §5), y la sesión abierta como en el acceso: otro ID, otro token CSRF, la época de la cuenta y `mfa_at`. La marca desaparece con el ID anterior.
- **400 `VALIDATION_ERROR` en `code`** (código `invalid`): el código no vale, sin decir por qué. La marca sigue: se puede probar otro. Deja `auth.login.failed` (`FAILED`, actor anónimo, la huella del correo, la cuenta como entidad afectada y `{"reason": "wrong_code"}`), fuera de la transacción, y cuenta en el límite por dirección y en el tope por cuenta. Si con él empieza un bloqueo de la dirección, deja además `auth.login.throttled`, como el acceso.
- **429 `RATE_LIMITED`, con `Retry-After`:** el límite por dirección del acceso, o el tope por cuenta (10 códigos fallidos en 24 horas). Con el tope alcanzado tampoco vale el código correcto; el que lo alcanza deja `auth.mfa.throttled` y el aviso por correo (F2-104).
- **`123 456` y `123-456` valen** como código TOTP: la ruta junta seis dígitos escritos con espacios o guiones. Un código de respaldo se escribe como se quiera.
- **Lo que no hace:** la pantalla llegó después (F2-107, `frontend/README.md`, «Acceso»). Un 401 de esta ruta deja contado en el límite por dirección el intento de la contraseña; un 500 después de un código correcto deja contado ese intento en el tope por cuenta, como en el acceso. Un 401 que llega con el intento ya contado (un cambio con la petición en curso, o la marca ya gastada) y un 429 del tope por cuenta dejan ese intento contado en el límite por dirección; si con él empieza un bloqueo, no queda su fila `auth.login.throttled`, como en la confirmación del alta. **Para la pantalla:** no enviar dos veces el código; una petición que llega con la cookie anterior después de completarse el acceso recibe 401 y Django le borra la cookie de sesión.

## MFA: el acceso deja la marca en vez de abrir sesión (F2-106, ADR-023 §5 y §10)

- **Desde aquí el segundo factor se pide al entrar.** `POST /api/v1/auth/login/`, con la contraseña correcta de una cuenta **con dispositivo confirmado**, no abre sesión: responde **202 `{"mfa_required": true}`**, sin ningún dato de la cuenta, y deja el acceso a medias (`second_step.begin`). La sesión la abre `POST /api/v1/auth/login/mfa/` (F2-105, arriba) con un código, en los 5 minutos siguientes.
- **Vale siempre que la cuenta tenga un dispositivo confirmado.** `MFA_ENFORCEMENT`, cuando exista, decidirá a quién se le exige tener segundo factor, no si el de quien lo tiene se comprueba (ADR-023 §5). Un alta sin confirmar no exige nada.
- **En una transacción:** la fila de auditoría `auth.login.mfa_required` (resultado `SUCCESS`, actor anónimo porque todavía no hay sesión, la huella del correo y la cuenta como entidad afectada: ADR-023 §10 dejó el nombre a este work item), el cierre de la sesión que el navegador tuviera, de esa cuenta o de otra, y la marca, que queda bajo un ID de sesión nuevo. **Sin esa fila no hay marca** (500), y la sesión que hubiera sigue como estaba. Lo mismo si falla lo que viene después en esa transacción, con la sesión ya cerrada: se deshace entero. La marca la guarda `SessionMiddleware` al responder, con la transacción ya confirmada, como los datos de una sesión que abre el acceso: si ese guardado falla (500) quedan la fila y la sesión anterior cerrada, sin marca; nunca una marca sin fila. **El token CSRF no cambia aquí:** cambia al completar el segundo paso.
- **Lo contado en el límite por dirección no se devuelve** con la contraseña: lo devuelve el segundo paso al completarse. Los bloqueos que empezaron con esa contraseña sí acaban al dejar la marca (`throttle.release`, F2-105).
- **Una contraseña incorrecta, o una cuenta desactivada, responde lo de siempre** (401 `INVALID_CREDENTIALS`), tenga o no la cuenta segundo factor.
- **Sin dispositivo confirmado el acceso es el de antes,** y al abrir sesión borra la marca que el navegador trajera: una marca nunca vive en una sesión autenticada.
- **Lo que cambia para quien llama:** la respuesta de `POST /auth/login/` tiene dos formas (200 con `user`, 202 con `mfa_required`). Quien tiene la contraseña de una cuenta con segundo factor sabe que es correcta y que la cuenta lo tiene (ADR-023, *Consequences*).
- **La pantalla** del segundo paso está en `/login` desde F2-107 (`frontend/README.md`, «Acceso»).
- **Lo que no hace:** no limita cuántas marcas deja una dirección: contar marcas no rechaza ninguna contraseña correcta (la rechaza, con 429, un bloqueo en curso, y un bloqueo solo se sostiene tras un fallo). Sus contadores siguen subiendo, y con ellos dos cosas. El siguiente fallo de esa dirección, sea una contraseña o un código, recibe un bloqueo más largo cuantas más marcas dejó: con ocho y los valores por defecto, los 15 minutos del máximo, más de lo que vive una marca, que desde ahí ya no se completa. Y una sola dirección con la contraseña pone la cuenta caliente con 20 marcas seguidas, sin esperar: mientras dure, cada fallo de la dueña, desde cualquier dirección, le cuesta una espera (OBS-F2-03B-1). No aplica la política por membresía ni el step-up.

## MFA: el estado del segundo factor propio (F2-108, ADR-023 §1, §4 y §6)

- **`GET /api/v1/auth/mfa/`**: ruta de plataforma, con sesión. Dice, **de la cuenta de la sesión y de ninguna otra** (nada de la URL ni del cuerpo elige cuenta), lo que sus pantallas necesitan para saber qué ofrecer. El trabajo está en `apps.accounts.mfa.status`; solo lee.
- **`enabled`** y **`enabled_at`**: si tiene un dispositivo confirmado, y desde cuándo (`null` si no).
- **`enrollment_expires_in`**: los segundos que le quedan a un alta empezada para confirmarse, o `null` si no hay ninguna que se pueda confirmar: no se empezó, caducó a los 10 minutos, se agotó con cinco códigos que no valen (ADR-024) o ya está confirmada. Son segundos enteros, hacia abajo, y nunca cero: en el último segundo ya dice `null`, aunque la confirmación la acepta hasta los 600 justos. Los fallos anotados (`failed_codes`) no lo cambian ni se dicen.
- **`backup_codes_left`**: cuántos códigos de respaldo quedan sin gastar. Nunca cuáles (ADR-023 §6); cero sin dispositivo confirmado.
- **Lo que no dice:** el secreto, un código o su hash; si la política de alguna organización exige el segundo factor (`MFA_ENFORCEMENT`, aún sin construir); cuándo se verificó por última vez en esta sesión (`mfa_at`, para el step-up); el estado de otra persona.
- **Con una sesión robada** se ve, además de si la cuenta tiene segundo factor, si hay un alta empezada y cuánto le queda: el momento de gastar los cinco intentos de ADR-024. Preguntarlo no cuenta en ningún límite (un 409 de la confirmación sí cuenta en el del acceso, F2-100). El tope no cambia: cinco por cada alta que la dueña empiece.
- La respuesta no se guarda en caché, como toda la API.

## MFA: quitar el segundo factor propio (F2-109, ADR-023 §8)

- **`POST /api/v1/auth/mfa/disable/`** con `{password, code}`: ruta de plataforma, con sesión y CSRF. Pide las tres cosas: la sesión, la contraseña actual y un código válido, de la aplicación de autenticación o de respaldo. Una sesión robada no basta, ni con la contraseña. Responde 204. El trabajo está en `apps.accounts.mfa_disable.disable`.
- **Qué hace,** en una transacción (`second_factor.verify`, con la fila de la cuenta bloqueada): borra el dispositivo y todos sus códigos de respaldo, revoca las demás sesiones (la época avanza) y audita `auth.mfa.disabled` con `{"method": "totp"}` o `{"method": "backup_code"}`. **Sin esa fila no se quita nada** (500), y el código no queda gastado. La sesión que lo pide sigue: adopta la época nueva, cambia de ID y pierde `mfa_at`.
- **Desde entonces la cuenta entra solo con su contraseña.** Puede dar de alta otro dispositivo cuando quiera. Quitarlo no exime de la política por membresía, cuando exista (ADR-023 §8 y §9).
- **La contraseña va primero** y cuenta en el límite por dirección del acceso, como al empezar un alta; a diferencia del alta, la correcta sola no devuelve lo que contó (abajo). **400 `VALIDATION_ERROR` en `password`** (código `invalid`): no es la de la cuenta; el código ni se mira ni cuenta contra el tope por cuenta.
- **400 `VALIDATION_ERROR` en `code`** (código `invalid`): el código no vale, sin decir por qué. Cuenta en el límite por dirección y en el tope por cuenta (10 fallos en 24 horas, compartido con el segundo paso del acceso).
- **Una contraseña o un código que no valen dejan su fila** `auth.mfa.disabled` con resultado `DENIED` y `{"reason": "password"}` o `{"reason": "code"}`, fuera de la transacción; si con ese rechazo empieza un bloqueo de la dirección, además `auth.login.throttled`, con actor anónimo aunque haya sesión, como en el acceso.
- **`123 456` y `123-456` valen** como código TOTP, como en el acceso: la ruta junta antes seis dígitos escritos con espacios o guiones (`second_factor.typed`). Sin juntarlos se mirarían como un código de respaldo y contarían como un fallo en los dos límites.
- **409 `MFA_NOT_ENABLED`:** la cuenta no tiene un dispositivo confirmado (un alta a medias no lo es, y no se toca). **401:** la sesión se revocó o se cerró con la petición en curso, o la cuenta se desactivó: no se quita nada y la sesión se cierra. **429:** cualquiera de los dos límites; con el tope por cuenta alcanzado tampoco vale el código correcto.
- **Solo el acierto devuelve lo que contó en el límite por dirección,** en su transacción. Un 401, un 409 que llega con la petición en curso (el factor se quitó mientras tanto) y un 429 del tope por cuenta llegan con la contraseña ya comprobada y dejan ese intento contado por dirección, sin fila de auditoría; si con él empieza un bloqueo, tampoco queda su fila `auth.login.throttled`, como en el segundo paso del acceso (F2-105). En el tope por cuenta, el 401 devuelve su intento, ese 409 lo deja contado y el 429 no llega a contarlo. El 409 de una cuenta sin dispositivo se comprueba antes de todo: no cuenta en ningún límite ni deja fila, y no dice nada que no diga `GET /api/v1/auth/mfa/`.
- **Aviso por correo** a la cuenta (`accounts.send_mfa_disabled`), encolado con el factor ya quitado y de mejor esfuerzo: qué pasó y, si no fue ella, que cambie la contraseña y dé de alta otro. Sin códigos; la tarea no reintenta.
- **Lo que no hace:** no quita el segundo factor de otra persona (ADR-023 §11, D-F2-15) ni hay comando de operador; no borra los intentos contados contra la cuenta (salen solos de su ventana, y un dispositivo dado de alta antes de eso empieza con ellos contados); no hay pantalla. **Para la pantalla:** no enviar dos veces la petición: la segunda llega con la cookie anterior, recibe 401 y Django le borra la cookie de sesión, aunque la primera haya quitado el factor.

## MFA: regenerar los códigos de respaldo (F2-110, ADR-023 §6)

- **`POST /api/v1/auth/mfa/backup-codes/`** con `{password, code}`: ruta de plataforma, con sesión y CSRF. Con la contraseña actual y un código **de la aplicación de autenticación**, responde 200 con `backup_codes`: diez códigos nuevos. **Es la única vez que salen.** El trabajo está en `apps.accounts.mfa_codes.regenerate`.
- **Los anteriores dejan de valer todos,** gastados o no: en una transacción se borran, se guardan los hashes de los nuevos y se audita `auth.mfa.backup_codes_regenerated` (ADR-023 §10 dejó el nombre a este work item). **Sin esa fila no cambia nada** (500): los anteriores siguen valiendo y el paso TOTP no queda gastado.
- **Un código de respaldo no vale para pedir otros diez:** quien robó uno no puede fabricarse más. Se rechaza como cualquier código que no vale (400 en `code`), cuenta en los dos límites y **no queda gastado**.
- **Lo demás es lo de quitar el factor** (F2-109, arriba; las dos rutas comparten `mfa_disable.with_password_and_code`): la contraseña primero, contada en el límite por dirección; 400 en `password` o en `code`, con su fila denegada (`{"reason": "password"}` o `{"reason": "code"}`); 409 `MFA_NOT_ENABLED` sin dispositivo confirmado; 401 si la sesión se revocó mientras tanto; 429 con cualquiera de los dos límites; `123 456` vale.
- **Lo que no cambia:** la sesión que lo pide sigue con su ID, y las demás sesiones de la cuenta no se cierran.
- **Lo que no hace:** no enseña otra vez los códigos ya generados (solo se guardan sus hashes); no avisa por correo; no hay pantalla.
- **Para la pantalla:** no enviar dos veces la petición. La segunda, con el mismo código TOTP, recibe 400 en `code` (ese paso ya se gastó) y cuenta en los dos límites, aunque la primera haya regenerado los códigos; con un código de otro paso regenera otra vez, y los diez de la primera respuesta ya no valen: solo valen los de la última regeneración.

## Invitaciones: la tabla (F2-79, ADR-020)

`user_invitations` guarda las invitaciones a una organización. Las reglas de toda la serie (E01-06) están en [ADR-020](../docs/adr/ADR-020-invitations.md); F2-79 dejó la tabla y el modelo, F2-80 la ruta que invita («Invitar a una persona», abajo), F2-81 la que las lista y F2-82 la que revoca una. Enviar y aceptar son los siguientes, y esperan a D-F2-14.

- **Tenant-owned**, con RLS forzado y FK a su organización. Modelo `apps.organizations.models.UserInvitation`; lectura `organizations.selectors.invitations()`, que filtra por organización y no por permiso.
- **Campos:** `email` (el correo invitado), `role_ids` (los roles que tendrá, por identificador), `token_hash` (el SHA-256 del enlace de un solo uso; vacío hasta que se envía), `expires_at`, `status` (`PENDING`, `ACCEPTED`, `REVOKED`, `EXPIRED`), `invited_by` (la cuenta que invitó; columna `invited_by_user_id`), `sent_at`, `send_count` (cuántas veces se envió su correo), `accepted_at`, `created_at`, `updated_at`.
- **No hay cuenta ni membresía hasta que se acepta** (ADR-020 §1): la tabla no enlaza con `users` ni con `organization_memberships` por el correo.
- **Restricciones en la base:** una sola invitación pendiente por correo y organización (`user_invitations_pending_uq`, índice único parcial); el correo en ASCII visible, sin mayúsculas ni espacios y con una sola `@` (`user_invitations_email_ck`); de 1 a 20 roles, en una lista simple y ninguno nulo (`user_invitations_roles_ck`); el hash con forma de SHA-256 en hexadecimal o ausente (`user_invitations_token_hash_ck`) y único en toda la tabla cuando lo hay (`user_invitations_token_hash_uq`: por él se encontrará la invitación de un enlace, ADR-020 §3; es la única unicidad de la tabla que no empieza por `organization_id`, ADR-001 §5.3); de 0 a 5 envíos (`user_invitations_send_count_ck`); aceptada si y solo si tiene `accepted_at` (`user_invitations_accepted_ck`); estados conocidos (`user_invitations_status_ck`); quien invita es miembro de esa organización (`user_invitations_invited_by_org_fk`, FK compuesta a `organization_memberships`, ADR-001 §3; una membresía que invitó no se puede borrar mientras exista su invitación).
- **Lo que la base no comprueba:** que `role_ids` sean roles de la organización (es una lista, no una FK: se comprueban al invitar y otra vez al aceptar), que no se repitan, ni que el correo sea entregable más allá de su forma (`core.mail` rechaza además comillas y palabras codificadas); tampoco que `send_count`, `sent_at` y `token_hash` vayan juntos.
- **Las invitaciones no se borran** (ADR-020 §2): desde F2-80 `crm_app` no tiene `DELETE` sobre la tabla (migración `0010`). Revocar es un estado.
- **`EXPIRED` no lo escribe ningún proceso:** una invitación pendiente con `expires_at` pasado no se podrá aceptar; el estado existe para quien quiera anotarlo.

## Invitar a una persona (F2-80, ADR-020)

`POST /api/v1/o/{slug}/invitations/` con `{"email": "…", "role_ids": ["<uuid>", …]}` deja creada una invitación: pendiente, con los roles que la persona tendrá al aceptar y con caducidad a 7 días. Exige los permisos `users.invite` **y** `users.manage`. Responde 201 con la invitación, con la forma de una fila del listado («Listar las invitaciones», abajo).

**Todavía no envía ningún correo, y la invitación no se puede aceptar:** el envío y la aceptación esperan a D-F2-14. Hasta entonces la ruta solo deja la fila y su auditoría.

- **Reglas, bajo el bloqueo de RBAC de la organización** (`access.services.ensure_can_invite`): las de asignar esos roles. Quien invita cubre cada concesión de cada rol, con su alcance, y un permiso sensible solo lo da un Owner. Si no, 403 sin motivo, como al asignar. Nadie da por invitación lo que no podría dar asignando.
- **Roles:** de 1 a 20, sin repetir, todos de la organización. Un rol de otra organización es un 400 en `role_ids`, igual que uno que no existe. Si además hay un rol que el actor no cubre, responde el 403.
- **Correo:** se guarda en la forma canónica de las cuentas (D-F2-3: sin espacio exterior, en minúsculas, el dominio en IDNA) y solo si `core.mail` lo entregaría (`core.mail.deliverable`): una sola dirección en ASCII, sin comillas, nombre ni palabras codificadas. Lo demás es un 400 en `email`. La respuesta devuelve el correo tal como quedó guardado.
- **409 `ALREADY_MEMBER`:** el correo ya tiene membresía en la organización, activa, suspendida o dada de baja. Una invitación no toca una membresía que existe (ADR-020 §4). Es información de la propia organización; la ruta no dice si un correo tiene cuenta en la plataforma: uno con cuenta y uno sin ella reciben la misma respuesta.
- **409 `INVITATION_PENDING`:** el correo ya tiene una invitación pendiente, también si caducó. Renovar una invitación es un envío (ADR-020 §3) y llega con él; hasta entonces una pendiente caducada ocupa su correo hasta que se revoque («Revocar una invitación»).
- **Topes** (constantes de `organizations.services`): 50 pendientes por organización, caducadas incluidas (409 `INVITATION_LIMIT`), y 100 invitaciones creadas en 24 horas, cuenten o no como pendientes (429 `RATE_LIMITED`, sin `Retry-After`). Se cuentan sobre la tabla después de tomar el bloqueo de RBAC: dos invitaciones a la vez no los cuentan a la vez.
- **Auditoría:** `membership.invited` sobre la entidad `invitation`, con el correo y los roles en `changes`. El correo queda en claro (ADR-020 §6) y lo lee quien tenga `audit.view`. Excepción: la auditoría pasa por el redactor compartido (`core.redaction`), que tapa lo que tiene forma de secreto también dentro de una dirección (`token=abc@x.pe` queda `token=[REDACTED]`; `sk-…` de 16 caracteres o más, `[REDACTED]@x.pe`). La fila de la invitación y la respuesta conservan la dirección entera: se llega a ella por `entity_id`.
- **Orden de las respuestas:** 401; 403 por permisos, antes de mirar el cuerpo; 400 por la forma del cuerpo y por el correo; 403 por un rol que no se cubre; 400 por un rol que no es de la organización; 409 y 429. Una organización sin rol Owner responde 409 `LAST_OWNER` a quien tiene los dos permisos, como en el resto de cambios bajo el bloqueo de RBAC.
- **Dónde vive:** `apps.members.services.invite` orquesta `accounts` (la forma del correo), `core.mail` (si es entregable), `access` (las reglas) y `organizations.services.create_invitation` (la fila, los topes y la auditoría), que no comprueba permisos y solo importa `apps.members`.
- **Lo que no hace:** no genera el enlace ni escribe `token_hash`, `sent_at` o `send_count`; no emite ningún evento para la tarea de envío; no renueva; no lleva equipos ni sucursal. La pantalla de auditoría nombra `membership.invited` desde F2-83.

## Listar las invitaciones (F2-81, ADR-020)

`GET /api/v1/o/{slug}/invitations/` devuelve las invitaciones de la organización, en cualquier estado, de la más reciente a la más antigua, con la paginación por cursor de los demás listados (`?limit=`, `?cursor=`). Exige lo mismo que invitar: `users.invite` **y** `users.manage`. Enseña correos de personas que todavía no son miembros; por eso no basta `users.view`.

```json
{
  "id": "…",
  "email": "nueva.persona@example.com",
  "role_ids": ["…"],
  "status": "PENDING",
  "expires_at": "2026-10-14T11:48:47.253686Z",
  "invited_by": "…",
  "created_at": "2026-10-07T11:48:47.254339Z"
}
```

- **`status` es el que se enseña** (`organizations.services.shown_status`, ADR-020 §3): una invitación pendiente cuya `expires_at` ya pasó sale como `EXPIRED`, aunque la tabla siga diciendo `PENDING`. Los demás estados salen como están guardados. Para invitar, esa invitación sigue contando como pendiente («Invitar a una persona»).
- **Identificadores, no nombres:** `role_ids` son los roles guardados, existan todavía o no, e `invited_by` es la cuenta que invitó (el mismo identificador que `actor_id` en la auditoría). Quien quiera nombres los resuelve con el directorio de roles (`roles.view`) y el de miembros (`users.view`), con sus propios permisos.
- **Lo que no sale:** el hash del enlace, `sent_at`, `send_count` y `accepted_at`.
- **Una consulta por página**, sean cuantas sean las filas. Sin filtros todavía.
- La vista es la misma que invita (`apps.members.api.views.InvitationsView`) y lee con `organizations.selectors.invitations()`.

## Revocar una invitación (F2-82, ADR-020)

`PUT /api/v1/o/{slug}/invitations/{id}/status/` con `{"status": "REVOKED"}` revoca una invitación pendiente, caducada o no. Exige `users.invite` **y** `users.manage`. Responde 200 con la fila de la invitación, como en el listado. `REVOKED` es el único estado que la API pone.

- **Efecto:** la invitación pasa a `REVOKED` y pierde el hash de su enlace, si lo tenía: un enlace revocado no queda guardado. Su correo se puede invitar otra vez y deja de contar para el tope de pendientes; sigue contando para el de 100 en 24 horas, que cuenta las creadas. La fila no se borra.
- **Repetir la petición no cambia nada:** una ya revocada responde 200 con la misma fila, sin escribir ni auditar.
- **409 `INVALID_TRANSITION`:** la invitación ya se aceptó, o está anotada como `EXPIRED` en la tabla (nada lo escribe hoy). Una pendiente que caducó sigue siendo pendiente en la tabla y sí se revoca.
- **Reglas, bajo el bloqueo de RBAC de la organización** (`access.services.ensure_can_revoke_invitation`): las de invitar con los roles que la invitación daría. Quien revoca cubre cada concesión de cada uno de esos roles, como para quitárselos a un miembro, y un permiso sensible solo lo cubre un Owner. Si no, 403 sin motivo. Un rol que ya no existe no concede nada y no se mira.
- **Orden de las respuestas:** 401; 403 por permisos, antes de mirar el cuerpo; 400 si `status` no es `REVOKED`; 404 si la invitación no es de la organización; 403 por un rol que no se cubre; 409 `INVALID_TRANSITION`. Quien tiene los dos permisos distingue así una invitación que existe de una que no: son las que ya puede listar. Una organización sin rol Owner responde 409 `LAST_OWNER` a quien tiene los dos permisos, exista o no la invitación, como en el resto de cambios bajo el bloqueo de RBAC.
- **Auditoría:** `membership.invitation_revoked` sobre la entidad `invitation`, con el cambio de estado. El correo está en la fila `membership.invited` de la misma entidad.
- **Dónde vive:** `apps.members.services.revoke_invitation` toma primero el bloqueo y comprueba los permisos, después lee los roles de la invitación (`organizations.services.invitation_role_ids`), comprueba que el actor los cubre y llama a `organizations.services.revoke_invitation`, que bloquea la fila, escribe y audita.

## Sucursales (F2-43 a F2-45 y F2-68, E01-09)

`GET /api/v1/o/{slug}/branches/` lista las sucursales de la organización. Exige `organization.view` (sin él, 403; sin membresía activa, 404).

```json
{
  "results": [
    {
      "id": "…",
      "code": "LIM-01",
      "name": "Centro de Lima",
      "address": "Av. Wilson 1234",
      "district": "Cercado",
      "city": "Lima",
      "phone": "+51 1 555 0100",
      "timezone": "America/Lima",
      "is_active": true
    }
  ],
  "next": null
}
```

- **Qué incluye:** todas, activas e inactivas. Lo opcional (`address`, `district`, `city`, `phone`) llega como texto vacío, nunca `null`.
- **Paginación:** por cursor, en orden de creación (`?limit=`, `?cursor=`; ver «Listados»). Una consulta por página.
- **Tabla `branches`:** tenant-owned, con RLS forzado y la política `tenant_isolation`. `code` es único por organización y la base de datos solo admite mayúsculas ASCII, cifras y guiones entre ellas (`LIM-01`), hasta 20 caracteres: dos códigos no se distinguen solo por mayúsculas, acentos, espacios o letras Unicode de igual aspecto. Los parecidos dentro de ASCII (`O` y `0`, `I` y `1`) siguen siendo códigos distintos. `timezone` es un nombre IANA que la tabla no comprueba; el modelo pone `America/Lima` por defecto (la columna no tiene valor por defecto).
- El selector `organizations.selectors.branches` filtra por organización, no por permiso: el permiso lo exige la vista, y otra vista que lo use declara el suyo.

Crear y editar exigen `branches.manage` (F2-44 y F2-45):

- `POST /api/v1/o/{slug}/branches/` con `{"code", "name", "address"?, "district"?, "city"?, "phone"?, "timezone"?}` crea una sucursal activa y responde 201 con la forma del listado. 409 `BRANCH_CODE_TAKEN` si la organización ya tiene ese código.
- **Código:** se acepta en minúsculas y se guarda en mayúsculas; solo letras ASCII, cifras y guiones entre ellas, hasta 20.
- **Textos:** una línea imprimible, sin espacios exteriores ni repetidos (se guardan en forma NFC). El nombre es obligatorio y lleva alguna letra o cifra. `timezone` es un nombre IANA exacto (`America/Lima`, `UTC`); sin él, `America/Lima`.
- Un campo que no sirve responde 400 `VALIDATION_ERROR` con su nombre en `fields`.
- `PATCH /api/v1/o/{slug}/branches/{branch_id}/` con cualquiera de `name`, `address`, `district`, `city`, `phone`, `timezone` e `is_active` cambia lo que se envía y responde 200 con la sucursal. Valen las mismas reglas de texto y de zona horaria. Desactivar es `{"is_active": false}`, y reactivar, `true`. 404 si la sucursal no es de la organización.
- **El código no cambia:** un `code` en el cuerpo de `PATCH` se ignora, como cualquier campo desconocido.
- **Sin cambios, no escribe.** Enviar lo que ya hay (o un cuerpo vacío) responde 200 con la sucursal y no deja fila de auditoría.
- **Auditoría de tenant:** `branch.created`, con lo que se guardó (sin los campos vacíos), y `branch.updated`, con el antes y el después de lo que cambió. La etiqueta de la entidad es el código.
- **`branches.manage`** está en el catálogo: no es sensible ni lleva alcance. Lo recibe el rol Owner de cada organización al migrar (ADR-018) y la plantilla «Administrador» en las organizaciones nuevas.
- Los comandos (`create_branch` y `update_branch`, en `apps.organizations.branches`) no comprueban permisos: solo los importa la API del módulo, que declara el permiso (contrato de import-linter).
- No hay borrado. Desactivar una sucursal no tiene todavía ningún efecto más.
- **Sucursal de una membresía (F2-68):** `organization_memberships.default_branch_id`, opcional. La FK es compuesta con `organization_id`: la base de datos no deja dar a una membresía la sucursal de otra organización. Una sucursal con membresías no se borra, ni con el ORM (`PROTECT`) ni con SQL directo. De ahí sale `ExecutionContext.branch_ids`, leído en la misma consulta que la membresía: una sola sucursal, que cuenta también si está inactiva (`is_active` habla de la operación, no de lo que alguien puede ver). Hoy ningún permiso del catálogo admite alcance, así que ninguna respuesta cambia. La asigna `PUT …/members/{id}/branch/` (F2-69, «La sucursal de un miembro»).

## Equipos (F2-50, F2-52 a F2-55, F2-58 y F2-59, E01-09)

`GET /api/v1/o/{slug}/teams/` lista los equipos de la organización. Exige `teams.view` (sin él, 403; sin membresía activa, 404).

```json
{
  "results": [
    {
      "id": "…",
      "slug": "ventas",
      "name": "Ventas",
      "description": "Atiende a clientes nuevos",
      "assignment_strategy": "MANUAL",
      "is_active": true
    }
  ],
  "next": null
}
```

- **Qué incluye:** todos, activos e inactivos. `description` llega como texto vacío si no hay, nunca `null`.
- **Paginación:** por cursor, en orden de creación (`?limit=`, `?cursor=`; ver «Listados»). Una consulta por página.
- **Tabla `teams`:** tenant-owned, con RLS forzado y la política `tenant_isolation`. `slug` es único por organización y la base de datos solo admite minúsculas ASCII, cifras y guiones entre ellas (`ventas`, `soporte-2`), hasta 50 caracteres. `assignment_strategy` es uno de `MANUAL`, `ROUND_ROBIN`, `LOAD_BALANCED`, `SKILL_BASED` o `AI_RULES` (lo impone un `CHECK`); el modelo pone `MANUAL` por defecto. Nada aplica todavía la estrategia: es dato para el Inbox.
- **`teams.view`** está en el catálogo: no es sensible ni lleva alcance. Lo recibe el rol Owner de cada organización al migrar (ADR-018), y las plantillas «Administrador» y «Supervisor» en las organizaciones nuevas.
- **Crear (F2-53):** `POST /api/v1/o/{slug}/teams/` con `{"slug", "name", "description"?, "assignment_strategy"?}` crea un equipo activo, sin integrantes, y responde 201 con la forma del listado. Exige `teams.manage`. 409 `TEAM_SLUG_TAKEN` si la organización ya tiene ese `slug`.
  - **`slug`:** se acepta en mayúsculas y se guarda en minúsculas; solo letras ASCII, cifras y guiones entre ellas, hasta 50.
  - **Textos:** el nombre es obligatorio, una línea imprimible de hasta 100 con alguna letra o cifra; la descripción, una línea de hasta 255. Se guardan en forma NFC, sin espacios exteriores ni repetidos. Son las mismas reglas que en sucursales (`apps.organizations.text`).
  - **`assignment_strategy`:** uno de los cinco valores exactos; sin él, `MANUAL`.
  - Un campo que no sirve responde 400 `VALIDATION_ERROR` con su nombre en `fields`.
  - **Auditoría de tenant:** `team.created`, con lo que se guardó (sin los campos vacíos). La etiqueta de la entidad es el `slug`. Como toda la auditoría, pasa por el redactor (ADR-011): un texto con forma de secreto queda como `[REDACTED]` en la fila, también un `slug` con un tramo `sk-` seguido de 16 caracteres o más (`sk-soporte-ventas-norte`).
- **`teams.manage`** está en el catálogo como lo lista 03 §H: no es sensible ni lleva alcance. Lo recibe el rol Owner de cada organización al migrar (ADR-018) y la plantilla «Administrador» en las organizaciones nuevas; «Supervisor» no (D-F2-12, en la fase).
- Los comandos (`create_team`, `update_team`, `put_team_member` y `remove_team_member`, en `apps.organizations.teams`) no comprueban permisos, como los de sucursales: el mismo contrato de import-linter solo deja importarlos a la API del módulo.
- **Editar (F2-54):** `PATCH /api/v1/o/{slug}/teams/{team_id}/` con cualquiera de `name`, `description`, `assignment_strategy` e `is_active` cambia lo que se envía y responde 200 con el equipo. Exige `teams.manage`. Valen las reglas de texto y de estrategia de crear. Desactivar es `{"is_active": false}`, y reactivar, `true`: solo los booleanos de JSON. 404 si el equipo no es de la organización.
  - **El `slug` no cambia:** un `slug` en el cuerpo se ignora, como cualquier campo desconocido.
  - **Sin cambios, no escribe.** Enviar lo que ya hay (o un cuerpo vacío) responde 200 y no deja fila de auditoría.
  - **Auditoría de tenant:** `team.updated`, con el antes y el después de lo que cambió.
  - **Desactivar un equipo no toca a sus integrantes** ni lo que ven: siguen contando para el alcance `TEAM` (F2-52). No hay borrado.
- **Poner a un miembro en un equipo (F2-58):** `PUT /api/v1/o/{slug}/teams/{team_id}/members/{membership_id}/` con cualquiera de `team_role` e `is_active`. Si la membresía no estaba en el equipo, la incorpora (201; `MEMBER` y activa si no se envían); si ya estaba, cambia lo que se envía (200). Responde con el integrante en la forma del listado. Repetir la petición no escribe ni audita.
  - **Permisos:** `teams.manage` **y** `users.view` (F2-56): la respuesta enseña a una persona.
  - **Nadie cambia su propia pertenencia:** 403, ni para entrar ni para cambiar su papel, exista o no el equipo. Entrar en un equipo amplía lo que deja ver una concesión con alcance `TEAM`.
  - **A quién:** cualquier membresía de la organización, en cualquier estado, y también en un equipo inactivo. 404 si el equipo o la membresía no son de la organización.
  - **Auditoría de tenant:** `team.member_added` y `team.member_updated` sobre el equipo, con el antes y el después y la membresía en `metadata.membership_id`.
  - **Lo que no comprueba:** qué gana el miembro incorporado. Quien tiene `teams.manage` y `users.view` decide a quién alcanza el alcance `TEAM` de cada equipo, también sin tener el permiso que el miembro gana: dos personas con esos permisos pueden incorporarse la una a la otra. Hoy no cambia ninguna respuesta, porque ningún permiso del catálogo admite alcance; es la decisión abierta D-F2-13.
- **Quitar a un miembro de un equipo (F2-59):** `DELETE /api/v1/o/{slug}/teams/{team_id}/members/{membership_id}/`. Responde 204, sin cuerpo.
  - **Permisos:** `teams.manage` **y** `users.view`, los mismos que para incorporar. No exige `teams.view`: quien tiene esos dos sabe si una membresía está en un equipo (204 o 404; con `PUT`, 201 o 200) aunque no pueda listar sus integrantes.
  - **Nadie se quita a sí mismo:** 403, exista o no el equipo, igual que nadie se incorpora.
  - **404:** el equipo o la membresía no son de la organización, o la membresía no está en el equipo. Repetir la petición también responde 404.
  - **No toca nada más:** ni la membresía, ni sus roles, ni sus otros equipos. La membresía puede volver al equipo con `PUT`, y entra como nueva.
  - **Auditoría de tenant:** `team.member_removed` sobre el equipo, con el papel y el `is_active` que tenía y la membresía en `metadata.membership_id`.
  - Toma el mismo bloqueo del equipo que `PUT`: quitar e incorporar a la vez se ponen en cola.
- **Integrantes de un equipo (F2-55):** `GET /api/v1/o/{slug}/teams/{team_id}/members/` lista quién pertenece al equipo. Exige `teams.view` **y** `users.view` (F2-56): enseña un equipo y a personas. 404 si el equipo no es de la organización.

  ```json
  {
    "results": [
      {
        "id": "…",
        "status": "ACTIVE",
        "team_role": "SUPERVISOR",
        "is_active": true,
        "user": {"id": "…", "email": "ana@acme.pe", "first_name": "Ana", "last_name": "López"}
      }
    ],
    "next": null
  }
  ```

  - `id` es el de la membresía, el mismo del directorio de miembros. `status` es el estado de esa membresía en la organización; `team_role` e `is_active` son del equipo.
  - Se listan todos los integrantes: también con la membresía suspendida o invitada, y con la cuenta desactivada.
  - **Enseña personas, así que exige también `users.view`:** el nombre, el correo y el estado de la membresía son los mismos datos del directorio de miembros, y se protegen igual. A quien le falta uno de los dos permisos, 403, exista o no el equipo. No salen roles, permisos, contraseñas ni las otras organizaciones del usuario.
  - **Paginación:** por cursor, en orden de incorporación al equipo. Dos consultas para la lista (si el equipo existe, y sus integrantes con su membresía y su usuario), sean cuantos sean.
- **Integrantes (`team_members`, F2-52):** qué membresías pertenecen a cada equipo, con su papel en él (`team_role`: `MEMBER` o `SUPERVISOR`) y si participan en la asignación automática (`is_active`). Tenant-owned, con RLS forzado. Las FK al equipo y a la membresía son compuestas con `organization_id`: la base de datos no deja enlazar un equipo de una organización con una membresía de otra. Un par equipo-membresía es único. Un equipo con integrantes no se borra, ni una membresía con equipos: antes hay que quitarlos. Los lee la ruta de F2-55, los escribe la de F2-58 y los quita la de F2-59.
- **Equipos propios en el motor de autorización:** `ExecutionContext.team_ids` son los equipos de la membresía de quien pide; se leen en la misma consulta que la membresía. Cuentan todos, también un equipo inactivo y una pertenencia con `is_active` en falso: esos dos datos hablan de la asignación, no de lo que alguien puede ver. `team_role` tampoco cambia el alcance. Hoy ningún permiso del catálogo admite alcance, así que ninguna respuesta cambia todavía.
- El selector `organizations.selectors.teams` filtra por organización, no por permiso: el permiso lo exige la vista.

## Cambios de RBAC sin escalada (F2-05C, ADR-003 §5)

`apps.access.services` tiene los únicos servicios que cambian el RBAC de una organización. Asignar y quitar un rol tienen ruta HTTP desde F2-25 («Roles de un miembro»); crear un rol, desde F2-29 («Crear un rol»); conceder un permiso a un rol y retirarlo, desde F2-31 y F2-33 («Permisos de un rol»).

- `create_role(ctx, name=, description=)`, `update_role(ctx, role_id=, name=, description=)`, `delete_role(ctx, role_id=)`, `grant_permission(ctx, role_id=, code=, scope=)`, `revoke_permission(ctx, role_id=, code=)`, `assign_role(ctx, membership_id=, role_id=)` y `remove_role(ctx, membership_id=, role_id=)`. Reciben el `TenantContext`, no una foto de permisos.
- Cada cambio corre en un savepoint: toma el bloqueo del rol Owner de la organización (`SELECT … FOR NO KEY UPDATE`), relee los permisos del actor, comprueba las reglas, escribe y audita. Si algo falla, incluida la auditoría, no queda nada escrito.
- **Reglas:** hace falta `roles.manage` para crear, renombrar o borrar un rol y para conceder, y `users.manage` para asignar o quitar. Nadie delega un permiso que no tiene ni con un alcance más amplio (`TEAM` y `BRANCH` no se contienen entre sí). Un permiso sensible solo lo delega quien tiene asignado el rol Owner, y además debe tenerlo. Asignar y quitar un rol, y renombrarlo o borrarlo, exigen cubrir todas sus concesiones. Nadie se asigna ni se quita roles, ni concede permisos a un rol que tiene asignado. Cambiar el alcance de una concesión exige cubrir el que había y el nuevo, y retirarla, cubrirla. Las concesiones del rol Owner no se editan.
- **Siempre queda un Owner activo** (membresía `ACTIVE` y usuario activo). `ensure_can_manage_member(ctx, membership_id=, leaving=)` aplica la misma garantía, y las reglas de escalada, a quien suspende o reactiva una membresía (F2-19).
- `is_owner_role` solo localiza el rol Owner para esas restricciones; por sí solo no concede nada. `is_system` solo impide borrar una plantilla. Nada decide por el código o el nombre de un rol, ni por `is_platform_staff`.
- Una denegación lanza `AccessDenied` con su motivo (`membership`, `permission`, `escalation`, `sensitive`, `self`, `last_owner`, `owner_role`); un id de otra organización, quitar un rol que la membresía no tiene o retirar una concesión que el rol no tiene (también al repetir la llamada), `DoesNotExist`. Repetir una concesión o una asignación no hace nada.
- Auditoría: `role.created`, `role.updated`, `role.deleted`, `role.permission_granted`, `role.permission_scope_changed`, `role.permission_revoked`, `membership.role_assigned` y `membership.role_removed`, con el antes y el después. `create_role` y `update_role` lanzan además `RoleNameTaken` y `ValueError` (nombre o descripción que no sirven); `delete_role`, `RoleIsSystem` y `RoleInUse`.

## Identificadores y numeración (F1-05, ADR-004)

- `core.ids.new_id()`: UUIDv7 de la stdlib (`uuid.uuid7()`); nunca se usa `uuid` directamente. `core.db.models.uuid7_primary_key()` añade `DEFAULT uuidv7()` (PostgreSQL 18) de respaldo para inserts SQL directos (hoy: `organizations.id`).
- `org_sequences` (tenant-owned, RLS + FORCE, PK `(organization_id, sequence_key)`, FK a `organizations`).
- `core.sequences.allocate(ctx, key, prefix=None)` → `COT-000001`: un único `INSERT … ON CONFLICT DO UPDATE … RETURNING` en la transacción del llamador. Falla fuera de `transaction.atomic()` o si `ctx` no es el tenant activo. Un rollback no consume número; las transacciones concurrentes de la misma clave se serializan (bloqueo de fila).
- Los números comerciales nunca autorizan nada.
- Un test exige que toda PK de `core`/`apps` sea UUIDv7 (`uuid7_primary_key()`) o compuesta: `DEFAULT_AUTO_FIELD` sigue siendo `BigAutoField` porque Django no admite UUID ahí.

## Outbox y auditoría (F1-06)

`core.outbox.emit()` y `apps.audit.services.record()` escriben en la transacción del `tenant_scope` activo: un rollback no deja ni evento ni auditoría. `audit_logs` es append-only para `crm_app` y está particionada por mes, y el redactor (`core.redaction`) se aplica siempre. Se lee con `GET …/audit/` (ver «Auditoría de la organización: lectura»). Diseño y decisiones: [docs/architecture/outbox-audit.md](../docs/architecture/outbox-audit.md).

## Auditoría de la organización: lectura (F2-73 y F2-74, E01-13)

`GET /api/v1/o/{slug}/audit/` devuelve las filas de `audit_logs` de la organización, de la más reciente a la más antigua, paginadas por cursor (ADR-016). Exige el permiso `audit.view`, que es sensible: solo lo delega un Owner.

```json
{
  "results": [
    {
      "id": "…",
      "occurred_at": "2026-10-07T03:15:04.123456Z",
      "actor_type": "USER",
      "actor_id": "…",
      "actor_label": null,
      "action": "branch.updated",
      "entity_type": "branch",
      "entity_id": "…",
      "entity_label": "LIM-01",
      "changes": {"name": ["Centro", "Centro de Lima"]},
      "metadata": {},
      "result": "SUCCESS",
      "correlation_id": null
    }
  ],
  "next": null
}
```

- **Cada fila llega como se guardó.** `changes` es `{"campo": [antes, después]}` y `metadata`, el contexto que anotó quien la escribió. Los dos pasaron por el redactor al escribirse (`core.redaction`, ADR-011): la ruta no redacta de nuevo ni añade nada.
- **Quién:** `actor_type` (`USER`, `AI_AGENT`, `SYSTEM`, `INTEGRATION` o `PLATFORM_STAFF`, los que admite la tabla), `actor_id` (el usuario, si es `USER`) y `actor_label` si se anotó. La ruta no trae el nombre ni el correo del actor. Un comando de plataforma escribe como `SYSTEM` y anota en `metadata` quién lo lanzó: `organization.created` trae `operator` (una etiqueta, sin enmascarar aunque tenga forma de correo), `reason`, `owner_user_id` y `user_created` (OBS-F2-73-1 en [phase-2.md](../docs/phases/phase-2.md)).
- **Orden:** por `id` descendente. Es un UUIDv7 que genera, con su reloj, el proceso que escribe la fila: prácticamente el orden en que se escribieron. `occurred_at` es la hora de la transacción en la base, en UTC, así que dos filas de una misma operación la comparten y, entre peticiones que se solapan, puede no seguir ese orden.
- **Filtros (F2-74):** `actor_type`, `actor_id`, `action`, `entity_type` y `entity_id`, en la consulta. Todos opcionales, por valor exacto, y se cumplen todos los que se envían: `?entity_type=role&entity_id=…` es la historia de un rol; `?actor_id=…`, lo que hizo una persona; `?actor_type=SYSTEM`, lo que no tiene `actor_id`. Un filtro que no encuentra nada devuelve una lista vacía, también con un identificador de otra organización. La página siguiente se pide con el `next` y los mismos filtros: el cursor solo guarda la posición.
- **Un filtro con un valor imposible es un 400**, no una lista vacía ni la lista sin filtrar: un UUID mal escrito (solo vale la forma canónica con guiones, en mayúsculas o minúsculas), un `actor_type` que la tabla no admite, una `action` o un `entity_type` sin la forma que exige `record()` al escribir (`core.outbox.NAME` y `TYPE`; ni mayúsculas ni espacios), un valor vacío o un filtro repetido. Un parámetro que la ruta no conoce se ignora, como en el resto de la API.
- **Sin filtro por fecha, por resultado ni por texto**, ni varios valores de un mismo filtro.
- **Lo que no se devuelve:** `ip`, `user_agent`, `request_id` e `impersonated_by_user_id`. Nada los escribe todavía.
- **Solo lectura.** El modelo `apps.audit.models.AuditLog` no gestiona la tabla (`managed = False`) y se niega a escribir: `save`, `delete` y las escrituras en bloque de su manager (`bulk_create`, `bulk_update`, `update`, `delete`) lanzan `TypeError`. La tabla se sigue escribiendo solo con `apps.audit.services.record()`, y `crm_app` sigue sin `UPDATE` ni `DELETE` sobre ella. `AuditLog._base_manager`, que Django usa por dentro, no lleva esa guarda.
- **Aislamiento:** RLS filtra la tabla por organización, además del filtro del manager y del de `ScopeFilter`.
- **Índices:** `audit_logs_org_id_idx (organization_id, id)` para el listado; `audit_logs_org_entity_idx (organization_id, entity_type, entity_id, id)` para la historia de una entidad y `audit_logs_org_actor_idx (organization_id, actor_id, id)` para lo que hizo un actor (F2-74). Se crean en la tabla padre; PostgreSQL los repite en cada partición, también en las nuevas. Los filtros `action` y `actor_type` solos no tienen índice propio: con un valor frecuente recorren el del listado hasta llenar la página; con uno raro o que no está, PostgreSQL lee todas las filas de la organización (o las particiones enteras) y ordena. `entity_id` sin `entity_type` usa `audit_logs_org_entity_idx` con un *skip scan* (PostgreSQL 18): una búsqueda por cada tipo de entidad.
- **Leer no deja rastro:** consultar la auditoría no escribe una fila. Tampoco se auditan los accesos denegados (OBS-F2-05A-4).
- **Errores:** 400 `VALIDATION_ERROR` (`limit`, `cursor` o un filtro), 401, 403 `PERMISSION_DENIED` sin el permiso y 404 sin membresía.
- **Capas:** `apps.audit` está en L1 y no importa `apps.access`: la vista declara `required_permissions` y las clases por defecto de DRF hacen el resto.

## Auditoría de plataforma (F2-10, ADR-013)

`apps.audit.platform.record(action, *, actor_type, …)` registra los eventos que no pertenecen a ninguna organización (acceso, altas de plataforma) en `platform_audit_logs`.

- **Solo inserción.** La tabla no tiene `organization_id` ni política de tenant. `crm_app` solo tiene `INSERT`: el runtime escribe y no puede leer, modificar ni borrar el registro. Por eso el servicio no usa `RETURNING`. La sentencia nombra `public.platform_audit_logs`: una tabla temporal con el mismo nombre no captura la fila.
- **Sin tenant.** No recibe tenant y falla dentro de un `tenant_scope`: ahí corresponde `apps.audit.services.record`. Dentro de un `user_scope` sí funciona.
- **Falla cerrado.** Escribe en la transacción del llamador, si la hay, dentro de un savepoint. Si la inserción falla, lanza el error y la transacción del llamador sigue utilizable; quien no lo captura la deshace entera.
- **Qué no se guarda.** `identifier` (el email presentado en un acceso fallido) queda solo como huella HMAC-SHA-256 con una clave derivada de `DJANGO_SECRET_KEY`. `metadata` y `user_agent` pasan por el redactor y, además, por un filtro propio que sustituye por `[EMAIL]` cualquier cadena con forma de dirección, en cualquier alfabeto. Nunca se guarda el email, la contraseña ni una cookie.
- **Validación.** `action` con el formato de siempre y como máximo 100 caracteres; un actor `USER` lleva `actor_id` y uno `ANONYMOUS` no; `metadata` de 4096 bytes como máximo; `ip` es una dirección válida o `None` (se guarda sin zona y sin forma IPv4-en-IPv6). Lo que no cumple lanza `ValueError` antes de tocar la base de datos. La tabla repite las reglas principales con `CHECK`.
- **Particiones.** Mensuales, mes actual más doce, creadas por la migración y el `post_migrate`. Sin partición DEFAULT. En cada llamada, `platform_audit_ensure_partitions` vuelve a dejar los privilegios como deben estar en la tabla padre y en todas las particiones.
- `request_id` y `correlation_id` salen del contexto de observabilidad.

El redactor compartido (`core.redaction`) trata ahora como secretos las claves de sesión y de CSRF (`session_key`, `*session_id`, `csrftoken`, `csrfmiddlewaretoken`, `crm_session`…) y sus valores dentro de un texto (`session_key=…`, `X-CSRFToken: …`, `crm_session=…`). Los nombres son exactos: `csrf_failure_count=3` o un texto sobre una mascota llamada Cookie no se tocan. Una clave que termine en `session_id` se redacta siempre: para correlacionar hay que usar otro nombre.

Todavía no hay lectura desde la aplicación: solo el rol propietario puede consultar la tabla. Revertir la migración borra el registro; se niega a hacerlo si la tabla tiene filas.

## Observabilidad (F1-07, ADR-011)

Logs JSON en stdout (structlog + `logging` estándar, redactados con `core.redaction`) con `request_id`, `correlation_id` e IDs de tenant/actor. El `X-Request-ID` es siempre un UUIDv7 generado por la aplicación. La correlación pasa de HTTP a Celery por cabecera. El log de acceso de uvicorn está apagado (lleva la dirección del cliente y la query string; el redactor solo taparía los patrones de secreto): el log de peticiones es `http.request.completed`. Errores: `NoopReporter` sin `SENTRY_DSN`, `SentryReporter` endurecido con él. Diseño: [docs/architecture/observability.md](../docs/architecture/observability.md).

## Correo saliente (F2-78, ADR-019)

`core.mail.send(Message(to=…, subject=…, body=…, purpose=…))` es la única salida de correo: un destinatario, texto plano y SMTP configurado por entorno, sin proveedor fijado ([ADR-019](../docs/adr/ADR-019-outbound-email.md)). Todavía ningún flujo envía: las invitaciones y la recuperación de contraseña son work items siguientes. `core.mail.deliverable(to)` dice de antemano si `send` aceptaría una dirección; lo usa quien la guarda antes de escribirle (F2-80).

- **Solo `core.mail` importa `django.core.mail` o `smtplib`.** Un test lo comprueba sobre `core`, `apps` y `config` (import-linter no distingue submódulos de un paquete externo).
- **Se llama desde una tarea, nunca dentro de una petición.** Es síncrono, no encola ni reintenta: un fallo de entrega lanza `MailError` y la tarea decide. La conexión tiene un máximo de 10 segundos (`EMAIL_TIMEOUT`).
- **Un enlace de un solo uso se genera en la tarea que lo envía**: no viaja por el outbox ni como argumento de la tarea, y de él solo se guarda el hash (ADR-019 §4).
- **Qué rechaza antes de intentar nada (`ValueError`):** lo que no es una sola dirección (varias, con nombre, con espacios) o que el transporte reescribiría después de validar (no ASCII, entre comillas o con una palabra codificada `=?…?=`; un dominio internacionalizado llega en su forma IDNA), saltos de línea o separadores de línea Unicode en la dirección o en el asunto, un asunto vacío o de más de 150 caracteres, un cuerpo vacío, con un carácter nulo o de más de 20.000, y un propósito que no sea un identificador en minúsculas de hasta 32 caracteres. El texto del error no repite lo recibido.
- **Log:** `mail.sent` y `mail.failed`, con `purpose`, la marca `[EMAIL]` en el lugar de la dirección y, en un fallo, el tipo de la excepción. Nunca el asunto, el cuerpo ni el texto del error de SMTP, que puede repetir la dirección. `MailError` tampoco lleva la excepción original, ni como `__context__`.
- **Configuración:** `MAIL_BACKEND` (`smtp`, o `memory` en tests), `EMAIL_HOST`, `EMAIL_PORT` (587 por defecto), `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD`, `EMAIL_USE_TLS` (STARTTLS), `EMAIL_USE_SSL` (TLS implícito) y `MAIL_FROM`. Sin `EMAIL_HOST` o sin `MAIL_FROM`, `send` lanza `ImproperlyConfigured`: no hay envío silencioso a ninguna parte.
- **`production`:** arranca sin servidor de correo; con `EMAIL_HOST`, exige cifrado (`EMAIL_USE_TLS` o `EMAIL_USE_SSL`, uno de los dos), `MAIL_FROM`, las dos credenciales y un `APP_ORIGIN` por `https` (F2-87), y `MAIL_BACKEND` solo puede ser `smtp`.
- **Local:** el stack envía a Mailpit (`EMAIL_HOST=mailpit`, puerto 1025, sin cifrado ni credenciales); los correos se ven en `http://localhost:8025`. Los tests usan el backend en memoria de Django.
- **`Message` se construye por nombre** (`Message(to=…, subject=…, body=…, purpose=…)`) y su `repr` solo enseña el propósito: ni la dirección ni el contenido llegan a una traza por accidente.
- **El backend de correo por defecto de Django es el que no envía nada** (`EMAIL_BACKEND` = `dummy`): lo que no pase por `core.mail`, como el aviso a `ADMINS` del `AdminEmailHandler` de Django, no abre una conexión SMTP con las credenciales configuradas.
- **Lo que no hay:** HTML, adjuntos, varios destinatarios, plantillas, límite de envío (lo pone cada flujo), detección de rebotes ni configuración de dominio (SPF, DKIM): dependen del proveedor que elija el despliegue.

## Object storage y HTTP saliente (F1-08)

`core.storage` (ADR-008): `S3CompatibleStorage` (boto3) / `InMemoryStorage`, claves `org/{organization_id}/…` y URLs firmadas de ≤ 5 min. La tabla `files` es tenant-owned con RLS. `core.http`: HTTPS con allowlist de hosts, IP pública validada al conectar (anti-SSRF) y sin redirecciones. La suite de contrato S3 corre contra Garage v2.4.1 en CI. Diseño: [docs/architecture/storage-http.md](../docs/architecture/storage-http.md).

## Contrato OpenAPI (F1-08A)

DRF + drf-spectacular. `GET /api/schema/` sirve el contrato; `backend/openapi/schema.yaml` es la versión commiteada y la **fuente de verdad** para el cliente TypeScript (orval, F1-09). CI lo regenera y falla si hay diferencias. Tras cambiar la API:

```bash
DJANGO_SETTINGS_MODULE=config.settings.local uv run python manage.py spectacular --validate --fail-on-warn --file openapi/schema.yaml
```

## Health checks (ADR-011 §4)

| Endpoint | Comportamiento |
|---|---|
| `GET /health/live` | `200 {"status":"ok"}`; no toca dependencias |
| `GET /health/ready` | `200` si la BD responde a `SELECT 1`; si no, `503 {"status":"fail","checks":{"database":"fail"}}` sin detalles (el motivo solo va al log). Redis y storage se añadirán cuando existan |

Ambos: solo `GET`/`HEAD`, `Cache-Control: no-cache`.

## Docker

```bash
docker build -t crm-backend backend/
```

Imagen `python:3.14.7-slim` multi-stage, dependencias de `uv.lock` (`--frozen`, sin dev), usuario no root `10001`, `HEALTHCHECK` sobre `/health/live`, servidor `uvicorn` con `--proxy-headers` (proxies de confianza en `FORWARDED_ALLOW_IPS`, obligatoria: ver «Dirección del cliente y proxy de confianza»).

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

Variables de producción: `DJANGO_SECRET_KEY`, `DJANGO_ALLOWED_HOSTS`, `FORWARDED_ALLOW_IPS`, `DATABASE_URL`, `DJANGO_CSRF_TRUSTED_ORIGINS` (opcional), `DJANGO_LOG_LEVEL` (opcional). En F1-03 `DATABASE_URL` pasa a ser exclusivamente el rol `crm_app` (ADR-002 §1.1).

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

- **Campos:** `id`, `organization_id`, `user`, `status` (`INVITED`, `ACTIVE`, `SUSPENDED`, `DEACTIVATED`), `created_at`, `updated_at`. Solo `ACTIVE` da acceso. **No lleva rol:** los roles se asignarán a la membresía (F2-04).
- **Constraints en BD:** `UNIQUE (organization_id, user_id)`, `CHECK` de `status`, FK a `organizations` y FK a `users`.
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

- `execution_context(ctx)`: membresía activa del usuario y sus permisos efectivos (unión de los alcances de todos sus roles), en dos consultas. Sin membresía activa lanza `AccessDenied`.
- `has_permission`, `can(ectx, code, obj)`, `require(...)` y `scoped(ectx, code, queryset)`: permiso, alcance sobre un objeto y filtro de listado. Todo dentro del `tenant_scope` del propio contexto.
- `apps.access.scopes.register(Modelo, FieldScopes(...))`: cada modelo declara una vez sus columnas de propietario, equipo y sucursal, por el nombre de la columna (`assigned_user_id`, no `assigned_user`); de ahí salen el filtro y la verificación por objeto.
- Los equipos (F2-50) aún no tienen integrantes, y las sucursales (F2-43) aún no se enlazan a las membresías: `TEAM` y `BRANCH` equivalen a `OWN` (OBS-F2-05A-2).
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
| `POST /api/v1/auth/login/` | `Public` | `{email, password}`. Abre la sesión y devuelve `{user}`. Exige CSRF |
| `GET /api/v1/auth/session/` | `Authenticated` | El usuario de la sesión actual, o 401 `NOT_AUTHENTICATED` |

- **Una sola respuesta de rechazo.** Email desconocido, contraseña incorrecta y usuario desactivado responden 401 `INVALID_CREDENTIALS` con el mismo cuerpo. El motivo real solo queda en la auditoría de plataforma.
- **Cookie.** `crm_session`: `HttpOnly`, `SameSite=Lax`, `Path=/`, sin `Domain`. En producción se llama `__Host-crm_session` y lleva `Secure`. Sesiones de Django en base de datos (D-F2-2); el login rota el identificador de sesión y el token CSRF.
- **Caducidad.** 12 horas de inactividad y 7 días absolutos (ver «Ciclo de la sesión»).
- **Auditoría de plataforma.** `auth.login.succeeded` (actor `USER`) y `auth.login.failed` (actor `ANONYMOUS`, huella del email presentado, la cuenta como entidad si existe y `metadata.reason`). Nunca la contraseña ni el email. Si el acceso correcto no se puede auditar, no se abre la sesión y la anterior del mismo navegador sigue valiendo. Un fallo al auditar un rechazo no cambia su respuesta: va al log y al reporte de errores, solo con el nombre de la acción.
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
- **Ráfagas simultáneas:** contar y bloquear es una sola transacción por intento, con las filas bloqueadas siempre en el mismo orden. Muchas peticiones a la vez no prueban más contraseñas que el umbral. Un acceso correcto que aún se está evaluando cuenta como intento hasta que termina.
- **Una cuenta, una clave:** la huella se calcula sobre el email canónico (D-F2-3), así que mayúsculas, espacios y otras escrituras del mismo dominio cuentan juntas. Lo que no es un email válido cuenta tal como llegó.
- **Dirección:** una IPv4, o la red `/64` de una IPv6. Quien dispone de una red IPv6 mayor y cambia de `/64` en cada intento no tiene tope: acotarlo exige una decisión pendiente (D-F2-10, F2-03E). Sin dirección conocida (`REMOTE_ADDR` vacío o inválido) todos los clientes comparten una y el login lo avisa en el log: es la señal de un proxy mal declarado (ver «Dirección del cliente y proxy de confianza»).
- **Auditoría de plataforma:** una fila `auth.login.throttled` (`DENIED`) cuando empieza cada bloqueo, con el contador, los intentos y los segundos; no una por cada intento rechazado. Con la cuenta caliente el contador de la fila es `identifier`. Las filas de `ip` no llevan huella. Un fallo al auditar no cambia la respuesta.
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
      "roles": [{"id": "…", "code": "owner", "name": "Owner"}]
    }
  ],
  "next": null
}
```

- **Qué incluye:** todas las membresías de la organización, en cualquier estado (`INVITED`, `ACTIVE`, `SUSPENDED`, `DEACTIVATED`). `id` es el de la membresía; `joined_at`, su fecha de alta, en UTC (los microsegundos se omiten si son cero). `status` es el estado de la membresía, no el de la cuenta: un usuario con la cuenta global desactivada sigue saliendo con el estado de su membresía (por ejemplo `ACTIVE`) aunque no pueda entrar.
- **Qué no incluye:** contraseña, marcas de plataforma ni las otras organizaciones del usuario. La tabla `users` es global: el listado sale de `organization_memberships` (RLS con FORCE) y solo une los usuarios de esas filas.
- **Roles:** identificador, código y nombre. El código y el nombre, para mostrar; el identificador es el que piden las rutas que asignan o quitan el rol («Roles de un miembro»). Nada decide por ellos. Se ven con `users.view`, sin `roles.view`: quién tiene qué rol es dato del directorio; lo que concede cada rol no sale aquí.
- **Paginación:** por cursor, en orden de alta (`?limit=`, `?cursor=`; ver «Listados»). Dos consultas por página, sean cuantos sean los miembros: las membresías con su usuario y los roles de esa página.
- **Solo lectura.** Suspender y reactivar, y asignar o quitar roles, están en las secciones siguientes; invitar es otro work item (E01-06).
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

## Sucursales (F2-43 a F2-45, E01-09)

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
- No hay borrado. Desactivar una sucursal no tiene todavía ningún efecto más: nada depende de ella.

## Equipos (F2-50, E01-09)

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
- **Solo lectura.** Crear y editar un equipo, sus integrantes y el permiso `teams.manage` son los siguientes work items. Hasta entonces la tabla solo se llena desde código.
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

`core.outbox.emit()` y `apps.audit.services.record()` escriben en la transacción del `tenant_scope` activo: un rollback no deja ni evento ni auditoría. `audit_logs` es append-only para `crm_app` y está particionada por mes, y el redactor (`core.redaction`) se aplica siempre. Diseño y decisiones: [docs/architecture/outbox-audit.md](../docs/architecture/outbox-audit.md).

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

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
- **`roles`**, **`role_permissions`** y **`membership_roles`** (tenant-owned, RLS con FORCE): roles por organización, concesiones con alcance (`OWN`, `TEAM`, `BRANCH`, `ORGANIZATION`, o `NULL` si el permiso no lo admite) y roles asignados a membresías.
- **Integridad en la BD:** FK compuestas con `organization_id` impiden enlazar una membresía de una organización con un rol de otra, también con SQL directo.
- **Roles plantilla** (`owner`, `admin`, `supervisor`, `seller`): `clone_role_templates(ctx)` los crea en una organización de forma idempotente y no asigna roles a nadie. Una organización puede crear roles propios sin cambios de esquema.
- **Nada decide por el código o el nombre de un rol:** la autorización depende de permisos y alcances.

## Autorización: motor (F2-05A, ADR-003 §5)

`apps.access.selectors` decide si una membresía puede hacer algo. Solo cuentan permisos y alcances: nunca el código o el nombre de un rol, ni `is_platform_staff`.

- `execution_context(ctx)`: membresía activa del usuario y sus permisos efectivos (unión de los alcances de todos sus roles), en dos consultas. Sin membresía activa lanza `AccessDenied`.
- `has_permission`, `can(ectx, code, obj)`, `require(...)` y `scoped(ectx, code, queryset)`: permiso, alcance sobre un objeto y filtro de listado. Todo dentro del `tenant_scope` del propio contexto.
- `apps.access.scopes.register(Modelo, FieldScopes(...))`: cada modelo declara una vez sus columnas de propietario, equipo y sucursal, por el nombre de la columna (`assigned_user_id`, no `assigned_user`); de ahí salen el filtro y la verificación por objeto.
- Hasta E01-09 no hay equipos ni sucursales: `TEAM` y `BRANCH` equivalen a `OWN` (OBS-F2-05A-2).
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
- **Cuerpos.** `core.api.parsers.Utf8JSONParser` es el analizador por defecto: JSON y solo en UTF-8. Un `charset` como `zlib` o `bz2` responde 415 `UNSUPPORTED_MEDIA_TYPE`; nunca elige el códec con el que se lee el cuerpo.
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
      "roles": [{"code": "owner", "name": "Owner"}]
    }
  ],
  "next": null
}
```

- **Qué incluye:** todas las membresías de la organización, en cualquier estado (`INVITED`, `ACTIVE`, `SUSPENDED`, `DEACTIVATED`). `id` es el de la membresía; `joined_at`, su fecha de alta, en UTC (los microsegundos se omiten si son cero). `status` es el estado de la membresía, no el de la cuenta: un usuario con la cuenta global desactivada sigue saliendo con el estado de su membresía (por ejemplo `ACTIVE`) aunque no pueda entrar.
- **Qué no incluye:** contraseña, marcas de plataforma ni las otras organizaciones del usuario. La tabla `users` es global: el listado sale de `organization_memberships` (RLS con FORCE) y solo une los usuarios de esas filas.
- **Roles:** nombre y código, para mostrar. Nada decide por ellos. Se ven con `users.view`, sin `roles.view`: quién tiene qué rol es dato del directorio; lo que concede cada rol no sale aquí.
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
      "members": 3
    }
  ],
  "next": null
}
```

- **Qué incluye:** todos los roles de la organización, de plantilla (`is_system`) o propios. `permissions` son sus concesiones tal como están guardadas, ordenadas por código (orden de Python, como en `…/me/`, no el de la intercalación de la base); `scope` es `null` si el permiso no admite alcance. Una concesión de un código que el catálogo ya no tiene se ve aquí, aunque el motor de autorización la ignore. `members` cuenta las membresías que tienen el rol, en cualquier estado.
- **Qué no incluye:** la marca de rol Owner (no autoriza nada), ni roles o concesiones de otra organización (RLS con FORCE en `roles`, `role_permissions` y `membership_roles`).
- **Paginación:** por cursor, en orden de creación (ver «Listados»). Tres consultas por página, sean cuantos sean los roles: los roles, sus concesiones y sus miembros.
- **Solo lectura.** Asignarlos a un miembro está en «Roles de un miembro»; crear o editar roles es otro work item (E01-08).
- **Módulos:** los lectores están en `apps.access.directory`, aparte del motor (`selectors`), que no decide por nombres, códigos ni marcas de rol (sus lectores `role_names` y `roles_by_membership` solo muestran código y nombre). Filtran por organización, no por permiso: `roles.view` lo exige la vista.

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

## Cambios de RBAC sin escalada (F2-05C, ADR-003 §5)

`apps.access.services` tiene los únicos servicios que cambian el RBAC de una organización. Asignar y quitar un rol tienen ruta HTTP desde F2-25 («Roles de un miembro»); conceder un permiso a un rol aún no (E01-08).

- `grant_permission(ctx, role_id=, code=, scope=)`, `assign_role(ctx, membership_id=, role_id=)` y `remove_role(ctx, membership_id=, role_id=)`. Reciben el `TenantContext`, no una foto de permisos.
- Cada cambio corre en un savepoint: toma el bloqueo del rol Owner de la organización (`SELECT … FOR NO KEY UPDATE`), relee los permisos del actor, comprueba las reglas, escribe y audita. Si algo falla, incluida la auditoría, no queda nada escrito.
- **Reglas:** hace falta `roles.manage` para conceder y `users.manage` para asignar o quitar. Nadie delega un permiso que no tiene ni con un alcance más amplio (`TEAM` y `BRANCH` no se contienen entre sí). Un permiso sensible solo lo delega quien tiene asignado el rol Owner, y además debe tenerlo. Asignar y quitar un rol exigen cubrir todas sus concesiones. Nadie se asigna ni se quita roles, ni concede permisos a un rol que tiene asignado.
- **Siempre queda un Owner activo** (membresía `ACTIVE` y usuario activo). `ensure_can_manage_member(ctx, membership_id=, leaving=)` aplica la misma garantía, y las reglas de escalada, a quien suspende o reactiva una membresía (F2-19).
- `is_owner_role` solo localiza el rol Owner para esas dos restricciones; por sí solo no concede nada. Nada decide por el código o el nombre de un rol, ni por `is_platform_staff`.
- Una denegación lanza `AccessDenied` con su motivo (`membership`, `permission`, `escalation`, `sensitive`, `self`, `last_owner`); un id de otra organización, o quitar un rol que la membresía no tiene (también al repetir la llamada), `DoesNotExist`. Repetir una concesión o una asignación no hace nada.
- Auditoría: `role.permission_granted`, `membership.role_assigned` y `membership.role_removed`, con el antes y el después.

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

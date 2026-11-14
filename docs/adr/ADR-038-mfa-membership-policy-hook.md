# ADR-038: Dónde se aplica la política de segundo factor por membresía

- **Status:** Accepted
- **Date:** 2026-11-14
- **Deciders:** programa autónomo (ADR-015 §5). El mantenedor puede reemplazarlo.
- **Related:** ADR-001 §5.6, ADR-002, ADR-003 §4 y §6, ADR-013, ADR-014 §1, ADR-017, ADR-022 §4, ADR-023 §9 y §11, [module-dependencies.md](../architecture/module-dependencies.md), [tenancy-context.md](../architecture/tenancy-context.md)

## Context

ADR-023 §9 decidió **a quién** se le exige segundo factor: a la membresía con el rol Owner (`is_owner_role`), con el rol Administrador de plantilla o con algún permiso sensible concedido por sus roles. Con `MFA_ENFORCEMENT = enforce`, sin dispositivo confirmado esa organización no se abre; con `warn`, se abre y el contexto propio lo dice. Faltaba decidir **dónde** se engancha, y hay cinco restricciones:

1. `apps.access` sabe qué roles y permisos tiene una membresía; `apps.accounts` sabe si una cuenta tiene un dispositivo confirmado. Están en la misma capa (L2) y no se importan entre sí (contrato de capas de import-linter).
2. La entrada a una organización vive en `core.tenancy`: el middleware HTTP, el mixin de WebSocket y la comprobación periódica del Inbox pasan por `resolve_tenant`. `core` no importa `apps`.
3. Los roles y sus concesiones son tablas de tenant con RLS (ADR-002): solo se leen dentro de un `tenant_scope`. Los dispositivos (`user_mfa_devices`) son de plataforma.
4. Ninguna entrada puede saltarse la política: una ruta, o un WebSocket, que la olvide deja pasar con solo la contraseña.
5. Quien no es miembro sigue recibiendo el 404 común (ADR-001 §5.6): la política no puede revelar qué organizaciones existen.

## Decision

### 1. El ajuste

`MFA_ENFORCEMENT` (`warn` o `enforce`, por defecto `warn`) llega del entorno. Cualquier otro valor, también uno vacío o con otra capitalización, hace que **ningún proceso arranque** (`ImproperlyConfigured`). En tiempo de ejecución, un valor que no es ninguno de los dos falla, nunca se trata como `warn`.

### 2. El gancho del kernel

`core.tenancy.second_factor` define la respuesta, `SecondFactor(required, enrolled)` (`missing` es «lo exige y no lo tiene»), y llama a la política que nombra `TENANCY_SECOND_FACTOR_POLICY`: una función `(TenantContext) -> SecondFactor` que corre **dentro del `tenant_scope`** de ese contexto. Es el patrón de `TENANCY_MEMBERSHIP_RESOLVER` (F1-04): el kernel define el punto de enganche y un módulo de capa superior lo sirve, sin que `core` importe `apps`.

### 3. Quién la sirve

`apps.members` (L2+), que ya une `access`, `organizations` y `accounts` (ADR-017). La política (`apps.members.second_factor.membership_second_factor`) solo pregunta a la API pública de cada uno:

- `access.second_factor.required(ctx)`: una consulta sobre los roles de la membresía, bajo RLS. Sí si alguno es el Owner (`is_owner_role`), es el Administrador de plantilla (`is_system` y el código `catalog.ADMIN_TEMPLATE`, nunca el nombre) o concede un permiso sensible, según el catálogo **o** según `permissions.is_sensitive` (lo más estricto de los dos).
- `accounts.selectors.has_second_factor(user_id)`: si la cuenta tiene un dispositivo **confirmado**. Un alta a medias no cuenta. Un dispositivo cuyo secreto no se abre sí cuenta: se mira la fila, nunca el secreto (ADR-022 §4: no es un secreto que falta).

Cada hecho sigue en su módulo; ninguno de los dos importa al otro. `access.second_factor` es API pública de `access` **aparte del motor de autorización** (`access.selectors`), que sigue sin leer la marca de Owner, `is_system` ni el código o el nombre de un rol (lo comprueba `tests/test_authorization.py`): no concede ni deniega nada, solo dice si se exige.

### 4. Dónde se aplica

Al final de `resolve_tenant`, **después** del 404 (organización inexistente o sin membresía activa) y del 403 `ORG_SUSPENDED`. HTTP (`TenantResolutionMiddleware`), WebSocket (`TenantConsumerMixin.connect_tenant`) y la comprobación periódica del Inbox resuelven por ahí, así que ninguna entrada se la salta y una nueva la hereda.

- Con `enforce`, `admit(ctx)` abre su propio `tenant_scope` corto, pregunta a la política y, si falta, lanza `SecondFactorRequired`.
- Con `warn`, no lee nada: el acceso no cambia y no hay consultas de más.

### 5. La respuesta

- **HTTP:** 403 con el cuerpo común (ADR-014 §1), `{"code": "MFA_REQUIRED", "message": …}`, en **toda** ruta de tenant de esa organización, también `me/`. La pantalla lleva al alta por el código.
- **WebSocket:** cierre **4428** al conectar, y también si la cuenta quita su segundo factor con la conexión abierta (la comprobación periódica del Inbox).
- Las demás organizaciones de la persona que no lo exigen se abren. Un no miembro recibe el 404 (4404) de siempre; una organización suspendida responde `ORG_SUSPENDED` antes.
- Las rutas de plataforma (`/api/v1/auth/…`, `/api/v1/me/organizations/`) no cambian: con ellas la persona se da de alta.

### 6. El contexto propio

`GET /api/v1/o/{slug}/me/` lleva `mfa: {required, missing}` en los dos modos, leído en el `tenant_scope` de la vista con la misma política. Con `warn` es lo que avisa a la pantalla; con `enforce`, quien llega a leerlo nunca tiene `missing`.

### 7. Falla cerrado

Una política que lanza, que no se puede importar o que no devuelve un `SecondFactor` deja la petición en error (500) con `enforce`: no abre nada. `admit` y `evaluate` exigen el `tenant_scope` de su contexto.

### 8. Lo que no se activa

`enforce` no se activa en ningún entorno con cuentas reales antes del comando de operador de ADR-023 §11. El stack local no pasa la variable: siempre es `warn`.

## Alternatives considered

| Alternativa | Por qué no |
|---|---|
| Un servicio público de `accounts` que lea `access` por su API | Rompe el contrato de capas (filas independientes); haría falta una excepción `ignore_imports` y `accounts` pasaría a depender de `access`. La entrada seguiría en `core`, que necesitaría el gancho de todos modos |
| Que `access` lea el dispositivo con `apps.get_model("accounts", "MfaDevice")` | Esquiva el contrato en vez de cumplirlo: `access` leería el modelo privado de otro módulo |
| Comprobarlo en cada entrada (dentro del `tenant_scope` del middleware, en el mixin y en la comprobación del Inbox) | Ahorra una transacción con `enforce`, pero son tres sitios que mantener iguales, y una entrada nueva que lo olvide abre la organización. En `resolve_tenant` lo hereda todo |
| Una clase de permiso de DRF | No cubre el WebSocket ni las vistas que no son de DRF; cada vista podría declararla o no |
| Un módulo nuevo de L2+ solo para esto | Un módulo, una fila en el contrato de capas y una app instalada para una función de quince líneas; `members` ya hace este papel. Si crece, se mueve |
| Exigirlo en el acceso (`POST /auth/login/`) | La cuenta es global (ADR-001 §2) y ADR-023 §9 lo evalúa por membresía: dejaría fuera a la persona también de las organizaciones que no lo exigen |

## Consequences

- Con `enforce`, cada petición de tenant suma una transacción corta y dos consultas; con `warn`, ninguna. `me/` suma dos consultas en los dos modos.
- Lo leído en `admit` vale para esa petición, como la membresía: un rol concedido entre esa lectura y la vista se aplica en la siguiente petición. En el WebSocket del Inbox, en la siguiente comprobación (`INBOX_WS_RECHECK_SECONDS`).
- La frase «nada decide por el código o el nombre de un rol» (`apps.access.catalog`, `backend/README.md`) gana su única excepción, la de ADR-023 §9, que solo añade una exigencia.
- La pantalla que avisa (`warn`) o lleva al alta (`enforce`) es el work item siguiente.

## Security implications

- **Endurece ADR-003 §4** tal como lo escribe ADR-023 §9, y no relaja nada: solo puede cerrar una organización que la membresía ya abría.
- **ADR-001:** la política corre después de la membresía; quien no es miembro nunca ve `MFA_REQUIRED` y el 404 sigue siendo idéntico.
- **ADR-002:** los roles se leen con `crm_app` dentro del `tenant_scope` de esa organización, bajo RLS con FORCE; los dispositivos son de plataforma. Nada usa BYPASSRLS ni el migrador.
- **ADR-013:** el rechazo no deja fila de auditoría (ADR-023 §10 no le da acción); queda en el log de la petición, con su 403.
- **ADR-014:** un código estable más, con el cuerpo común.
- Quien es miembro aprende que su membresía lo exige, que es lo que la pantalla le tiene que decir.

## Operational implications

- `MFA_ENFORCEMENT` en el entorno de web, worker, ws y beat (por defecto `warn`). Pasar a `enforce` sigue esperando al comando de operador (ADR-023 §11), y `enforce` sigue siendo requisito de la salida a producción (ADR-003, *Context*).
- Con `enforce`, una persona que pierde el dispositivo y los códigos no entra a las organizaciones que lo exigen hasta que exista ese comando.

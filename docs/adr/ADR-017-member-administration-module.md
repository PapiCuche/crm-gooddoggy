# ADR-017: Administración de miembros en un módulo de orquestación (`apps.members`)

- **Status:** Accepted
- **Date:** 2026-10-04
- **Deciders:** programa autónomo (ADR-015 §5). El mantenedor puede reemplazarlo.
- **Related:** ADR-001, ADR-002 §3.2, ADR-003 §5, ADR-014 §1, [module-dependencies.md](../architecture/module-dependencies.md), OBS-F2-05C-2 y PO-1 en [phase-2.md](../phases/phase-2.md)

## Context

Suspender a un miembro (E01-07) es la primera escritura de negocio de la API y toca dos módulos de la misma capa:

- `organizations` es dueño de la membresía y de su estado.
- `access` es dueño de las reglas de RBAC: quién puede gestionar a quién y que siempre quede un Owner activo.

Los módulos de L2 no se importan entre sí (contrato de import-linter). OBS-F2-05C-2 dejó anotado que desactivar una membresía debía llamar a `ensure_owner_remains` «en `apps.organizations`», pero `organizations` no puede importar `access`. El alta de una organización ya resolvió el mismo problema con un módulo por encima de L2 (`apps.provisioning`, F2-06).

Faltaba además decidir las reglas: el backlog dice «activar y desactivar usuarios» y ADR-003 §5 solo regula los cambios de roles y permisos.

## Decision

### 1. Módulo `apps.members`, en la capa L2+

`apps.members` orquesta la administración de los miembros de una organización. Importa los servicios públicos de `access` y de `organizations`; ninguno de los dos lo importa a él ni se importan entre sí. No tiene modelos ni importa los de otros módulos.

Cada módulo conserva lo suyo:

| Módulo | Qué aporta |
|---|---|
| `access.services.ensure_can_manage_member` | Las reglas. Toma el bloqueo de RBAC de la organización y lo conserva hasta el final del `tenant_scope` |
| `organizations.services.set_membership_status` | El cambio de estado y su fila de auditoría. No comprueba permisos |
| `members.services.set_member_status` | Llama a los dos, en ese orden, dentro de un savepoint |
| `members.api` | La ruta HTTP |

`organizations.services` no comprueba permisos, así que no es API pública: un contrato de import-linter (`protected`) solo deja importarlo a `apps.members`.

### 2. Reglas para suspender y reactivar

Las mismas en los dos sentidos. Reactivar devuelve el acceso con los roles que la membresía conserva, así que equivale a asignarlos.

1. Permiso `users.manage`, releído bajo el bloqueo.
2. Nadie cambia su propia membresía.
3. El actor cubre todas las concesiones de todos los roles del miembro, con alcance igual o superior, y un permiso sensible solo lo cubre un Owner. Es lo que ya exige quitarle esos roles (PO-1). Consecuencia: solo un Owner suspende a un Owner o a otro administrador.
4. Al suspender a quien tiene el rol Owner, debe quedar otro Owner activo.

Un miembro sin roles lo suspende cualquiera con `users.manage`.

### 3. Transiciones

Solo `ACTIVE` → `SUSPENDED` y `SUSPENDED` → `ACTIVE`. Pedir el estado que la membresía ya tiene no escribe ni audita. `INVITED` y `DEACTIVATED` no se tocan: una invitación se acepta por su propio flujo (E01-06) y la baja definitiva es otra decisión.

### 4. API

`PUT /api/v1/o/{slug}/members/{id}/status/` con `{"status": "SUSPENDED" | "ACTIVE"}`. Responde `{"id", "status"}`.

Es `PUT` sobre el estado, no `PATCH` sobre el miembro: la operación es idempotente y su cuerpo es obligatorio. Con `PATCH`, el contrato OpenAPI generado declara opcionales todos los campos.

| Respuesta | Cuándo |
|---|---|
| 403 `PERMISSION_DENIED` | Sin `users.manage`, uno mismo, o un miembro que el actor no cubre. No dice cuál |
| 404 `NOT_FOUND` | La membresía no es de esta organización |
| 409 `LAST_OWNER` | Sería el último Owner activo |
| 409 `INVALID_TRANSITION` | La membresía está invitada o dada de baja |

`LAST_OWNER` e `INVALID_TRANSITION` son códigos de dominio (ADR-014 §1) y llevan `message`.

### 5. Efecto

El resolvedor de tenancy exige una membresía `ACTIVE` en cada petición: el miembro suspendido recibe 404 en esa organización desde la siguiente, y deja de verla en su lista. La sesión no se cierra: es global y sigue valiendo para sus otras organizaciones. La cuenta (`users.is_active`) no cambia.

### 6. Fuera de esta decisión

Invitaciones (E01-06), baja definitiva, cambio de roles por API (E01-08), cierre de las sesiones de un usuario y reasignación de sus conversaciones.

## Alternatives considered

- **En `apps.organizations`, llamando a `access`.** Es lo que sugería OBS-F2-05C-2. Rompe el contrato de capas: L2 no se importa entre sí.
- **En `apps.access`, escribiendo la membresía con `apps.get_model`.** `access` ya lee así las membresías, pero escribir la tabla de otro módulo deja a `organizations` sin control de su propio estado y de su auditoría.
- **Fusionar `organizations` y `access`.** Elimina el problema, pero deshace una frontera que ADR-003 y el motor de autorización usan; es un cambio mucho mayor que esta operación.
- **Dentro de `apps.provisioning`.** Es una operación de plataforma, lanzada por un operador y sin actor del CRM. La administración de miembros la hace un usuario, con permisos.
- **Regla más laxa: basta `users.manage`.** Un administrador podría dejar sin acceso a un Owner. Se elige la más conservadora (ADR-015 §5); relajarla después no rompe nada.

## Consequences

- Las próximas operaciones sobre miembros que crucen módulos (invitar, cambiar roles por API) tienen dónde vivir.
- Un Owner no puede suspender a quien tenga un permiso que el rol Owner no tiene. Hoy no ocurre: el rol Owner nace con todo el catálogo. Ampliar el catálogo sin llevar los permisos nuevos al rol Owner lo provocaría (OBS-F2-05C-1).
- Un miembro con un rol que conserva un código retirado del catálogo no se puede suspender ni reactivar: falla cerrado, como asignar o quitar ese rol (OBS-F2-05C-3).
- Todos los cambios de estado de una organización van en serie con sus cambios de RBAC (OBS-F2-05C-4).
- El directorio (`GET …/members/`) sigue en `apps.access`. Moverlo a `apps.members` es un work item aparte.

## Security implications

- `users.manage` es un permiso sensible; la ruta lo declara y el servicio lo relee bajo el bloqueo. Un actor suspendido mientras esperaba el bloqueo ya no es miembro y se le deniega.
- La organización sale del contexto de la petición, nunca del cuerpo. Una membresía de otra organización no existe (RLS), y quien no tiene el permiso recibe 403 exista o no.
- Una denegación no escribe nada: el savepoint se deshace y el middleware de tenant deshace la transacción de toda respuesta de error.
- Cada cambio deja una fila de auditoría de tenant con el actor (`membership.suspended`, `membership.reactivated`), en la misma transacción.
- Queda fuera: un usuario suspendido conserva su sesión global, y una conexión WebSocket abierta no se corta (hoy no hay consumidores de tenant).

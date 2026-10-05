# ADR-018: El rol Owner sigue al catálogo de permisos

- **Status:** Accepted
- **Date:** 2026-10-05
- **Deciders:** programa autónomo (ADR-015 §5). El mantenedor puede reemplazarlo.
- **Related:** ADR-002 §2, ADR-003 §5, ADR-013, OBS-F2-04-1, OBS-F2-05C-1 y OBS-F2-29-1 en [phase-2.md](../phases/phase-2.md)

## Context

El catálogo de permisos lo define el código (`apps.access.catalog`) y cada fase le añade permisos. El rol Owner de una organización se modela con concesiones explícitas: al dar de alta la organización recibe todo el catálogo que había ese día.

Tres hechos hacen que una organización ya creada no reciba un permiso nuevo:

- `sync_permissions` solo sincroniza la tabla global `permissions` (OBS-F2-04-1).
- Nadie cambia las concesiones de un rol que tiene asignado (PO-2), y todo Owner tiene el rol Owner.
- Desde F2-31, ninguna ruta edita el rol Owner, sea quien sea el actor (OBS-F2-05C-1).

Sin una vía propia, el primer permiso posterior al alta (los de equipos y sucursales, E01-09) no lo tendría nadie en las organizaciones existentes, y nadie podría concederlo: lo sensible solo lo delega un Owner que lo tenga.

Alternativas consideradas:

| Alternativa | Por qué no |
|---|---|
| Una migración de datos por cada work item que añada permisos | Repite el mismo código en cada fase y es fácil olvidarla: el fallo sería silencioso (un permiso que nadie tiene) |
| Que el motor trate al rol Owner como «lo tiene todo» sin concesiones | El motor decidiría por una marca de rol. Hoy `is_owner_role` no concede nada por sí sola, y las concesiones del Owner se leen y se auditan como las de cualquier rol |
| Un servicio del runtime, o una ruta de plataforma, que reparta el permiso | El runtime (`crm_app`) no puede escribir en otra organización (RLS con FORCE). Haría falta un camino que se salte RLS, que es justo lo que ADR-002 evita |

## Decision

### 1. Invariante

El rol Owner de cada organización tiene todos los permisos del catálogo, con alcance `ORGANIZATION` en los que admiten alcance.

### 2. Lo impone el job de migraciones

Tras cada `migrate`, después de sincronizar `permissions`, `apps.access.apps.extend_owner_roles` añade al rol Owner de cada organización las concesiones del catálogo que le falten.

- **Solo añade.** No quita concesiones ni cambia el alcance de las que hay.
- **Solo el rol Owner**, localizado por `is_owner_role` (único por organización por restricción), nunca por código ni por nombre. Una organización sin rol Owner no recibe nada. El estado de la organización no cuenta: una suspendida también lo recibe.
- **Una transacción por organización**, dentro de su `tenant_scope` y con el actor `SYSTEM`, como el alta de una organización. Si una falla, las anteriores ya están hechas y el siguiente `migrate` termina el resto.
- **Corre con `crm_migrator`**, el rol dueño de las tablas, cuya credencial ya equivale a acceso total (ADR-002 §2). El runtime no gana ningún privilegio: `crm_app`, sin tenant activo, no ve los roles de ninguna organización, y con un tenant activo la función se niega.
- **Un despliegue sin permisos nuevos no escribe nada**: una consulta, y termina.

Un work item que necesite un permiso nuevo solo lo añade al catálogo.

### 3. Las demás plantillas no siguen al catálogo

Los permisos nuevos de Administrador, Supervisor o Vendedor llegan a las organizaciones nuevas, al clonar las plantillas. En las que ya existen, esos roles son de la organización: los edita quien tiene `roles.manage` (F2-31, F2-36), que desde este ADR puede ser un Owner con el permiso nuevo.

### 4. Auditoría

El cambio es de cada organización, así que queda en la auditoría de cada una (ADR-013 §1): una fila `role.permission_granted` por concesión, con el actor `SYSTEM` y `metadata.source = "catalog"`, en la misma transacción que la concesión. El Owner la ve como cualquier otra concesión de sus roles. Sin esa fila, la concesión no se escribe.

Además, cada ejecución que añade algo deja una fila en la auditoría de plataforma: `access.owner_roles.extended`, actor `SYSTEM`, con los códigos añadidos, el número de roles y el de concesiones. Es el resumen de una operación de plataforma sobre varias organizaciones; no sustituye a las filas de cada una. Se escribe al final: si falla, las concesiones ya hechas conservan su fila de tenant y el siguiente `migrate` no tiene nada que resumir.

### 5. Lo que no cambia

Retirar o renombrar un permiso sigue siendo una migración de datos que reescribe las concesiones (`sync_permissions` conserva lo que está concedido y avisa). Las reglas contra la escalada de ADR-003 §5 no cambian: este paso no es un actor de RBAC y no pasa por `access.services`.

## Consequences

- OBS-F2-04-1 queda cerrada para el rol Owner, y OBS-F2-05C-1 tiene su «otra vía».
- Migrando antes de servir el código nuevo, cuando una ruta exige un permiso nuevo los Owner ya lo tienen.
- Un rol Owner con una concesión más estrecha que `ORGANIZATION` (solo posible escribiendo en la base) no se corrige: el paso no cambia alcances.
- Con muchas organizaciones, el despliegue que trae un permiso nuevo hace una transacción por organización; los demás, una consulta.
- Que el código nuevo no sirva antes de migrar lo garantiza hoy el `compose` local (`migrate` termina antes de arrancar los servicios). El despliegue de producción, cuando exista, debe conservar ese orden.
- Quien quiera llevar un permiso nuevo a las plantillas ya clonadas necesita su propia decisión (OBS-F2-29-1 describe el riesgo de reclonar).

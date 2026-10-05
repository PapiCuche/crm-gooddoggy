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
- **Solo el rol Owner**, localizado por `is_owner_role` (único por organización por restricción), nunca por código ni por nombre. Una organización sin rol Owner no recibe nada.
- **Corre con `crm_migrator`**, el rol dueño de las tablas, cuya credencial ya equivale a acceso total (ADR-002 §2). El runtime no gana ningún privilegio: `crm_app`, sin contexto de tenant, no ve los roles de ninguna organización y la misma función no escribe nada.
- **Un despliegue sin permisos nuevos no escribe nada.**

Un work item que necesite un permiso nuevo solo lo añade al catálogo.

### 3. Las demás plantillas no siguen al catálogo

Los permisos nuevos de Administrador, Supervisor o Vendedor llegan a las organizaciones nuevas, al clonar las plantillas. En las que ya existen, esos roles son de la organización: los edita quien tiene `roles.manage` (F2-31, F2-36), que desde este ADR puede ser un Owner con el permiso nuevo.

### 4. Auditoría

Cada ejecución que añade algo deja una fila en la auditoría de plataforma (ADR-013): `access.owner_roles.extended`, actor `SYSTEM`, con los códigos añadidos, el número de roles y el de concesiones. No escribe en la auditoría de tenant: no hay actor de tenant, y no se inventa uno ni una organización ficticia.

### 5. Lo que no cambia

Retirar o renombrar un permiso sigue siendo una migración de datos que reescribe las concesiones (`sync_permissions` conserva lo que está concedido y avisa). Las reglas contra la escalada de ADR-003 §5 no cambian: este paso no es un actor de RBAC y no pasa por `access.services`.

## Consequences

- OBS-F2-04-1 queda cerrada para el rol Owner, y OBS-F2-05C-1 tiene su «otra vía».
- El orden de despliegue ya era migrar antes de servir el código nuevo: cuando una ruta exige un permiso nuevo, los Owner ya lo tienen.
- Un rol Owner con una concesión más estrecha que `ORGANIZATION` (solo posible escribiendo en la base) no se corrige: el paso no cambia alcances.
- Quien quiera llevar un permiso nuevo a las plantillas ya clonadas necesita su propia decisión (OBS-F2-29-1 describe el riesgo de reclonar).

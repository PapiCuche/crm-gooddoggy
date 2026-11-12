# ADR-031: Decidir las solicitudes de aprobación de cotizaciones en la base con un disparador `INVOKER` y `UPDATE` por columna

- **Status:** Proposed
- **Date:** 2026-11-12
- **Deciders:** propuesto por Claude Code (Opus 5.5) para la decisión del propietario. Toca un control de ADR-002, y ADR-015 §3 dice que un cambio que pueda debilitar un control de ADR-001, ADR-002, ADR-003, ADR-013 o ADR-014 se escribe como ADR `Proposed` y espera al mantenedor: no lo decide el programa autónomo. La opción recomendada (A) no necesita ninguna excepción a ADR-002. La alternativa B sí: enmendaría ADR-002 §3.3 con una excepción acotada. Mientras esté `Proposed` no obliga al código ni a los agentes: el work item #693 (F5-09b6) está bloqueado hasta esta decisión. Las decisiones que necesita están en «Decisión requerida del propietario».
- **Related:** [ADR-002](ADR-002-postgresql-rls.md) §2 y §3.3, [ADR-015](ADR-015-autonomous-delivery-program.md) §3, [ADR-018](ADR-018-owner-role-follows-catalog.md), [ADR-029](ADR-029-whatsapp-cloud-api-channel.md) (funciones `SECURITY DEFINER` que escriben, para la ingesta entre tenants), ADR-001, ADR-003, ADR-006, ADR-009, ADR-011, [phase-5.md](../phases/phase-5.md) (F5-09b2, F5-09b3, F5-09b6), #678, #692, #693, #652 y su PR #709, #705, `docs/architecture/tenancy-context.md` (T16), `docs/architecture/security-boundaries.md` §6

## Context

### Qué protege hoy la base y qué no

Las solicitudes de aprobación (`quote_approvals`, F5-09b2) se deciden con los comandos de `apps.quotes.decisions` (F5-09b3, #678/#692): aprobar, rechazar y retirar. Los comandos comprueban, en este orden, el permiso (`quotes.approve`; para retirar, ese o `quotes.update`), la cotización bloqueada, su alcance, la versión, el estado y, al aprobar, que quien aprueba no sea quien pidió (`SelfApproval`), su propio límite de descuento (`BeyondLimit`) y el suelo de costo de los precios manuales (`PriceRefused`).

La base (`quotes/0002` y `quotes/0003`) solo impone: que `PENDING_APPROVAL` → `APPROVED` tenga la **última** solicitud aprobada, que quien aprueba no sea quien pidió (`quote_approvals_separation_ck`; #709 añade quien envió, `submitted_by_user_id`, en `quotes/0004`), que rechazar lleve motivo, que una decisión sea definitiva y que lo pedido no cambie. **No** impone que quien decide sea quien actúa, ni su permiso, ni su límite.

La revisión independiente de #692 lo demostró con SQL directo como `crm_app`:

```sql
UPDATE quote_approvals SET status = 'APPROVED', decided_by_user_id = '<otro miembro>',
       decided_at = now() WHERE quote_id = '<q>';
UPDATE quotes SET status = 'APPROVED' WHERE id = '<q>';
```

Pasa todos los disparadores: un fallo de la aplicación (un comando futuro que olvide una comprobación, un `.update()` del ORM) puede aprobar una cotización en nombre de otra persona, sin `quotes.approve` y por encima de cualquier límite. La revisión de #709 añadió el caso de la IA: una solicitud `AI_CREATED` no tiene quien la pida (`requested_by_user_id` nulo), y quien envió el borrador de la IA podía aprobarlo.

### Por qué hace falta un ADR

ADR-002 §3.3 admite funciones `SECURITY DEFINER` solo para **lookups entre tenants** («enrutar un webhook…, resolver el token público…») y pide `STABLE`, «o `VOLATILE` solo si escribe, cosa que no deberían hacer». Ya existen funciones `SECURITY DEFINER` que escriben: las de la ingesta de webhooks entre tenants (`webhook_ingress_insert`, `webhook_ingress_claim` y `webhook_ingress_mark`, ADR-029, todavía `Proposed`), que se presentan como conformes a §3.3 sin declarar una excepción explícita. Lo que no existe es una función `SECURITY DEFINER` que escriba **dentro de un tenant** y **haga cumplir autoridad** (quién decide, su permiso, su límite): ese es un uso nuevo. Un ADR `Accepted` no se contradice en silencio (AGENTS.md §1): este ADR recomienda una opción que no necesita ese uso (A) y describe la que sí lo necesitaría (B), con sus condiciones.

### Modelo de amenaza (sin exagerar)

`app.tenant_id`, `app.user_id` y `app.actor_type` son variables de la transacción que fija `tenant_scope` (ADR-002 §2). `crm_app` puede fijarlas con `set_config`. Por tanto **ninguna** opción de este ADR protege de un SQL hostil de `crm_app` que se haga pasar por otra persona del mismo tenant que sí puede aprobar: eso exige identidad fuera de la sesión de base de datos (fuera de alcance). Lo que sí protegen: un fallo de la aplicación, un id de usuario que no es de un miembro activo con el permiso, un actor que no es una persona (la IA, el sistema), y quien pidió o envió aprobando lo suyo.

## Decision (propuesta): opción A, disparador `INVOKER` y `UPDATE` por columna

**Recomendación:** imponer la decisión con la guarda `INVOKER` de `quote_approvals`, que ya existe, y un `UPDATE` por columna para `crm_app`. **No** se crea ninguna función `SECURITY DEFINER` nueva y ADR-002 §3.3 no cambia.

**Por qué A y no B.** Las dos dan **la misma** garantía frente a un fallo de la aplicación, y ninguna protege de un SQL hostil que fije las GUCs (modelo de amenaza, arriba). La diferencia está en qué pasa si la implementación tiene un error:

- En **A**, el disparador corre como `crm_app`, así que **RLS con `FORCE` sigue activo** en cada lectura: un filtro de tenant olvidado no cruza tenants.
- En **B**, la función corre como `crm_migrator`, que tiene `BYPASSRLS`: un filtro de tenant olvidado **es una escritura entre tenants**.
- A sigue un patrón que ya existe (`quotes/0003` vigila igual el paso directo de `DRAFT` a `APPROVED`) y no necesita excepción.
- El coste de A: `crm_app` conserva el `UPDATE` de seis columnas, y el disparador debe cubrir cada combinación de columnas y estados. Se paga con la matriz de pruebas de abajo.

### Contrato de la opción A

1. **Privilegios.** `REVOKE UPDATE ON public.quote_approvals FROM crm_app`, y luego `GRANT UPDATE (status, decided_by_user_id, decided_at, comment, version, updated_at)`. `crm_app` conserva `SELECT` e `INSERT`; `DELETE` y `TRUNCATE` ya estaban revocados. Las columnas de identidad (`organization_id`, `id`, `quote_id`, `reason`, `requested_by_user_id`, `submitted_by_user_id`, `requested_discount_percent`, `limit_percent_at_request`, `created_at`) dan `permission denied` antes de llegar al disparador. Una prueba lo afirma columna a columna con `has_column_privilege`. Como `crm_app` sigue teniendo `UPDATE` en alguna columna, los bloqueos de fila (`select_for_update`) siguen siendo posibles: el código no cambia su forma de bloquear.
2. **La guarda.** `quote_approvals_guard` se reemplaza con `CREATE OR REPLACE` **partiendo de `quotes/0004.GUARD`** de #709, nunca de `0002`:
   - sigue siendo `INVOKER`, con `search_path = pg_catalog, public, pg_temp`, cada relación cualificada con `public.`, y listada en `tests/test_pg_temp_shadowing.py`;
   - vigila a `crm_app`, como todas las guardas de cotizaciones: el migrador no pasa por ellas (migraciones de datos, borrado de una organización);
   - aunque RLS ya filtra, cada lectura lleva `organization_id = NEW.organization_id`, y `NEW.organization_id` tiene que ser `public.app_current_tenant()`;
   - sin SQL dinámico y sin `WHEN OTHERS`.
3. **Una solicitud abierta que no se decide** (`OLD.status = 'PENDING'` y `NEW.status = 'PENDING'`): solo pueden cambiar `version` y `updated_at`. El comentario tampoco (`IS DISTINCT FROM`); quién y cuándo decide ya lo exige el `CHECK` `quote_approvals_decided_ck`. Siguen vigentes las reglas de hoy: una decisión es definitiva y lo pedido no cambia.
4. **Una decisión** (de `PENDING` a `APPROVED`, `REJECTED` o `CANCELLED`). Se comprueba en este orden y cada paso da su error:
   - **Quién actúa.** Una persona: `app.actor_type = 'USER'` y `public.app_current_user()` no nulo. Sin persona, otro tipo de actor o un valor mal formado dan **un solo** error; el `uuid` que no convierte se atrapa por su `SQLSTATE`, `22P02`. `NEW.decided_by_user_id = <quien actúa>`, comparado con `=`: un `NULL` falla.
   - **Fecha y versión.** El disparador fija `NEW.decided_at := now()` (no se puede fechar hacia atrás) y exige `NEW.version = OLD.version + 1`.
   - **Membresía y permiso**, como `apps.access.selectors` (`execution_context`, `has_permission`). Membresía `ACTIVE` del tenant y usuario `is_active`. Aprobar y rechazar piden `quotes.approve` (sin alcance). Retirar pide `quotes.approve`, o ser quien pidió (`requested_by_user_id = <quien actúa>`, con `=`: la solicitud de la IA no tiene quien pida y solo la retira quien aprueba) con `quotes.update`. El rol Owner **no** da permisos por ser Owner: los tiene porque sigue al catálogo (ADR-018); `is_owner_role` solo quita el límite de descuento.
   - **La cotización.** Se lee con el filtro de tenant y `FOR SHARE` en la misma sentencia, y tiene que estar en `PENDING_APPROVAL`.
   - **Al aprobar.** Quien actúa `IS DISTINCT FROM` quien pidió y `IS DISTINCT FROM` quien envió. El `CHECK` de separación de #709 también lo exige, pero un `CHECK` con `NULL` pasa: no basta solo. Además, el límite de quien aprueba (punto 5).
   - **Errores.** Cada error lleva el nombre de su restricción y no repite ningún valor.
5. **Una sola regla del límite.** Una función auxiliar `public.quote_discount_within(org uuid, quote uuid, membership uuid) RETURNS boolean`:
   - `STABLE`, `INVOKER`, mismo `search_path`, tenant explícito, `EXECUTE` para `crm_app`;
   - dice si el descuento cabe: el Owner, o una política de sus roles (la de la lista de la cotización o la de todas) que admite **cada línea** (`(precio - final) * 100 <= porcentaje * precio`, sin dividir) y el tope en importe en su moneda;
   - la usan `quotes_guard`, que hoy repite la regla en `quotes/0003`, y la guarda de la decisión;
   - una matriz de equivalencia la prueba contra `discounts._authority` (ver «Pruebas exigidas»).
6. **La inserción.** La guarda de inserción de `quote_approvals` **debe pasar a leer** la cotización con `FOR SHARE` (deuda (b) de #681; hoy no lo hace), para que una solicitud nueva espere a quien está pasando la cotización a `APPROVED`.
7. **Python.** Los comandos (`decisions._close`, `commands.cancel_quote` de #709) siguen escribiendo con el ORM, con `decided_by_user_id = ctx.user_id`. Traducen los rechazos de la base por el nombre de la restricción (`error.__cause__.diag.constraint_name`, nunca por el texto) a sus excepciones de dominio: `NotAllowed`, `NotPending`, `SelfApproval`, `BeyondLimit`; una desconocida, `QuoteRefused`. Las pruebas que hoy deciden con `UPDATE` directo (`test_quote_guards.py` `decide()`, `test_quote_decisions.py`, `test_quote_discounts.py` `back_to_draft`, `test_quote_hardening.py`) pasan a actuar como un aprobador real (contexto con su usuario y permiso), o esperan el rechazo nuevo.
8. **Lo que la base no comprueba** (lo hacen los comandos y queda escrito):
   - el alcance (`quotes.view`, `quotes.update` con `OWN` o `TEAM`);
   - el suelo de costo (`covers_cost`; leer costos en la base abriría otro camino al costo, ADR-006);
   - `quotes.cancel`.
9. **La migración.** Es nueva, la siguiente libre tras #709 y #705; `0002` a `0004` no se editan.

## Alternativa B (si se elige la función `SECURITY DEFINER`)

**Excepción acotada a ADR-002 §3.3:** una sola función, `public.quote_approval_decide(p_quote uuid, p_decision text, p_comment text) RETURNS uuid`, `LANGUAGE plpgsql VOLATILE SECURITY DEFINER`, puede **escribir** la decisión de una solicitud de aprobación de **su propio tenant**. Ninguna otra función queda autorizada por este ADR; otra necesitaría su propio ADR. `crm_app` pierde todo `UPDATE` sobre `quote_approvals` y decide solo por la función. A favor: un único punto de escritura, y ninguna sentencia de `crm_app` escribe una decisión. En contra: enmienda ADR-002 §3.3 y dentro corre con `BYPASSRLS` (ver «Por qué A y no B»).

### Contrato de la función (alternativa B)

1. **Construcción.** Con `core.db.operations.SecurityDefinerFunction` (siempre `ALTER FUNCTION … OWNER TO crm_migrator`, `REVOKE ALL … FROM PUBLIC`, `GRANT EXECUTE … TO crm_app` y a nadie más), `SET search_path = pg_catalog, public, pg_temp` y **cada** relación cualificada con `public.` (`crm_app` puede crear tablas temporales, F3-52). Listada en `tests/test_pg_temp_shadowing.py` y cubierta por la introspección T16. Sin SQL dinámico (`EXECUTE`), sin `WHEN OTHERS`.
2. **No `STRICT`.** Un argumento `NULL` no devuelve `NULL` en silencio: se comprueba explícitamente y es el error de entrada. `p_decision` ∈ {`APPROVED`, `REJECTED`, `CANCELLED`}; `p_comment` no nulo y de 500 caracteres como mucho. Los errores **no repiten** ningún argumento.
3. **Quién actúa.** El tenant y la persona salen de `public.app_current_tenant()` y `public.app_current_user()` (las mismas funciones que leen las políticas RLS) y `app.actor_type = 'USER'`. Sin tenant, sin persona, otro tipo de actor o un valor mal formado (el `uuid` que no convierte se atrapa por su `SQLSTATE` concreto, `22P02`, nunca con `WHEN OTHERS`) dan **un solo** error `42501`. Quien decide es `app_current_user()`: **nunca** un argumento.
4. **Permiso antes de cualquier bloqueo.** Antes de leer o bloquear la cotización: membresía `ACTIVE` del tenant y usuario `is_active`; concesiones de sus roles **como `apps.access.selectors`** (`execution_context` y `has_permission`): `quotes.approve` (sin alcance, `scope IS NULL`) para aprobar y rechazar; para retirar, `quotes.approve` o `quotes.update` (con alcance). El rol Owner **no** da permisos por ser Owner: los tiene porque sigue al catálogo (ADR-018); `is_owner_role` solo quita el límite de descuento, como en `discounts._authority`. Sin el permiso de base: `42501`, sin haber mirado si la cotización existe.
5. **Tenant explícito en cada consulta.** El dueño (`crm_migrator`) tiene `BYPASSRLS`: dentro de la función RLS no protege. **Toda** consulta filtra `organization_id = <tenant>` (cotización, solicitud, membresías, `membership_roles`, `role_permissions`, roles, políticas, líneas); `users` por id. El bloqueo lleva el filtro **en la misma sentencia** (`SELECT … FROM public.quotes WHERE organization_id = org AND id = p_quote FOR NO KEY UPDATE`), nunca bloquear primero y filtrar después.
6. **Una sola respuesta para lo que no existe.** Una cotización que no existe, de otro tenant, que no está en `PENDING_APPROVAL` o sin solicitud abierta dan el mismo error (mismo `SQLSTATE`, texto y nombre de restricción).
7. **Autoridad sobre esa solicitud** (con la fila bloqueada): retirar sin `quotes.approve` solo si `requested_by_user_id = <quien actúa>` con `=`, nunca con `IS NOT DISTINCT FROM` (una solicitud de la IA tiene `requested_by_user_id` nulo: nunca es igual, y nadie la retira sin `quotes.approve`). Aprobar: quien actúa (nunca nulo, punto 3) `IS DISTINCT FROM` quien pidió y `IS DISTINCT FROM` quien envió, además del `CHECK` de separación de #709 (un `CHECK` con `NULL` pasa: no basta solo).
8. **Límite de quien aprueba**, con una función auxiliar compartida `public.quote_discount_within(org uuid, quote uuid, membership uuid) RETURNS boolean`, `STABLE`, **`INVOKER`**, mismo `search_path`, tenant explícito: el Owner, o una política de sus roles (la de la lista de la cotización o la de todas) que admite **cada línea** (`(precio - final) * 100 <= porcentaje * precio`, sin dividir) y el tope en importe en su moneda. La usan `quotes_guard` (que hoy repite la regla en `quotes/0003`) y la función: una sola regla. Una matriz de equivalencia la prueba contra `discounts._authority` (ver «Pruebas»).
9. **La escritura.** Los disparadores de `quote_approvals` y `quotes` vigilan solo a `crm_app` (`current_user <> 'crm_app' → RETURN NEW`) y dentro de la función `current_user` es `crm_migrator`: **no corren**. La función se protege sola: `UPDATE public.quote_approvals SET status, decided_by_user_id, decided_at = now(), comment, version = version + 1, updated_at = now() WHERE organization_id = org AND id = <la bloqueada> AND status = 'PENDING'`, **solo esas seis columnas**, y comprueba `FOUND`. Los `CHECK` (decisión con autor y fecha, separación, motivo del rechazo) sí corren con el dueño. Devuelve el id de la solicitud decidida y **nada más**.
10. **Lo que no comprueba la base** (lo hacen los comandos y queda escrito): el alcance (`quotes.view`, `quotes.update` con `OWN`/`TEAM`), el suelo de costo (`covers_cost`; leer costos dentro de la función abriría otro camino al costo) y `quotes.cancel`.

### Privilegios y código (alternativa B)

- `REVOKE UPDATE ON public.quote_approvals FROM crm_app` (la tabla entera: `crm_app` cambia el `UPDATE` de la tabla por el `EXECUTE` de una función acotada que escribe; conserva `SELECT` e `INSERT`; `DELETE` y `TRUNCATE` ya estaban revocados). Una prueba lo afirma con `has_any_column_privilege('crm_app', 'public.quote_approvals', 'UPDATE') = false`.
- Las cláusulas de bloqueo (`FOR UPDATE`, `FOR NO KEY UPDATE`, `FOR SHARE`) exigen `UPDATE` en alguna columna: el `select_for_update` sobre `QuoteApproval` desaparece del código. Lo serializa el bloqueo de la cotización, que todos los escritores toman primero y la función vuelve a tomar.
- **Escritores mapeados** (todos pasan a la función o dejan de escribir): `decisions._close` (aprobar, rechazar, retirar), `commands.cancel_quote` de #709 (cancela la solicitud abierta al cancelar), `discounts._ask` (solo `INSERT`, sin cambios). En las pruebas: `test_quote_guards.py` (`decide()`, el `UPDATE` de otra organización que hoy espera `rowcount == 0`, los `CHECK` de decisión y separación), `test_quote_decisions.py` (los casos de separación y de decisión definitiva por SQL), `test_quote_discounts.py` (`back_to_draft`) y `test_quote_hardening.py` (#709): los `CHECK` se prueban con el dueño o a través de la función; los `UPDATE` directos de `crm_app` pasan a esperar `permission denied`.
- Python llama a la función dentro de la transacción del comando, después de sus propias comprobaciones, **comprueba que el id devuelto es el de la solicitud que leyó** y traduce los errores **por el nombre de la restricción** (`error.__cause__.diag.constraint_name`, nunca por el texto) a sus excepciones de dominio (`NotAllowed`, `NotPending`, `SelfApproval`, `BeyondLimit`, `ValueError`; una desconocida, `QuoteRefused`).
- La guarda de inserción de `quote_approvals` debe pasar a leer la cotización con `FOR SHARE` (deuda (b) de #681; hoy no lo hace): se reemplaza con `CREATE OR REPLACE` **partiendo de `quotes/0004.GUARD`** de #709, nunca de `0002`.
- La migración es nueva (la siguiente libre tras #709 y #705); `0002` a `0004` no se editan.

## Alternativa C: solo la aplicación

`crm_app` conserva el `UPDATE` de la tabla y los comandos siguen siendo la única garantía (lo de hoy, documentado). Sin código nuevo en la base y sin tocar ADR-002, pero **no cierra** el hallazgo de #692: un `UPDATE` directo decide en nombre de otro, sin permiso ni límite.

## Fuera de este ADR

Las reglas de congelación de `valid_until`, las notas y la cancelación tras `DRAFT` pasan a #705 (F5-09c2), que ya las incluye:

- `valid_until` debe seguir fijándose al pasar de `APPROVED` a `SENT`;
- las comparaciones son con `IS DISTINCT FROM`, porque esas columnas admiten nulo;
- «entrar en `CANCELLED`» es `OLD.status <> 'CANCELLED' AND NEW.status = 'CANCELLED'`.

Congelar `internal_notes` fuera de `DRAFT` es una decisión del propietario (abajo).

## Alternatives considered

| Opción                                                          | Qué es                                                                                                        | A favor                                                                                                                                                                    | En contra                                                                                                                                                                                                                                                           |
| --------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **A** (recomendada) Disparador `INVOKER` y `UPDATE` por columna | Seis columnas actualizables por `crm_app`; la guarda exige quién actúa, su permiso, la separación y su límite | Sin excepción a ADR-002; **RLS sigue activo** en cada lectura: un filtro olvidado no cruza tenants; patrón existente (`0003`); los bloqueos de fila siguen siendo posibles | `crm_app` conserva el `UPDATE` de seis columnas: la guarda debe cubrir cada combinación de columnas y estados (más pruebas); dos piezas (ORM y guarda) en lugar de una                                                                                              |
| **B** Función `SECURITY DEFINER`                                | `crm_app` sin `UPDATE`; decide solo por una función                                                           | Un único punto de escritura; ninguna sentencia de `crm_app` escribe una decisión                                                                                           | Enmienda ADR-002 §3.3; dentro corre con `BYPASSRLS`: **un filtro de tenant olvidado es una escritura entre tenants**; las guardas no corren dentro; `crm_app` gana `EXECUTE` sobre una función que escribe; el código pierde `select_for_update` sobre la solicitud |
| **C** Solo la aplicación                                        | Como hoy                                                                                                      | Sin cambios                                                                                                                                                                | No cierra el hallazgo de #692                                                                                                                                                                                                                                       |

Las tres dependen de `app.user_id`, que `crm_app` puede fijar: ninguna es más fuerte que otra frente a un SQL hostil que suplante a otro aprobador del mismo tenant. A y B dan **la misma** garantía frente a un fallo de la aplicación. Si la implementación tiene un error, en A una combinación de columnas sin cubrir dejaría pasar una escritura **dentro** del tenant; en B, un filtro olvidado escribiría **en otro** tenant.

## Pruebas exigidas (A o B)

Con PostgreSQL real y `crm_app`, por SQL directo además de por los comandos:

- **`UPDATE` directo.** De cada columna de identidad: `permission denied` (A y B). De cada columna de decisión: rechazo de la guarda (A) o `permission denied` (B). Bajo B, además, `SELECT … FOR UPDATE` sobre `quote_approvals` da `permission denied`.
- **Combinaciones (A).** Cada combinación de columnas actualizables en cada estado (`PENDING` sin decidir, cada decisión, ya decidida). Solo pasa la decisión completa y válida.
- **Quién decide.** La decisión (por la guarda o por la función) con:
  - el id de una cotización de **otro tenant**: mismo error, texto y restricción que un id inexistente;
  - un `app.user_id` que no es miembro;
  - el de un aprobador de **otro tenant** con el tenant propio;
  - membresía `INVITED` o `SUSPENDED`, y usuario inactivo;
  - un miembro sin `quotes.approve`;
  - `app.actor_type` `AI_AGENT`, `SYSTEM` o sin fijar; sin tenant; GUCs mal formadas;
  - `decided_by_user_id` distinto de quien actúa (A).
- **Separación y autoridad sobre la solicitud.**
  - quien pidió aprobando, y quien envió aprobando la de la IA;
  - quien no pidió ni aprueba retirando, y alguien que no aprueba retirando la de la IA;
  - una solicitud ya decidida, y una cotización que no está en `PENDING_APPROVAL`;
  - un aprobador por encima de su límite, y uno sin política.
- **Entradas.** Un comentario con SQL se guarda tal cual, y uno de 501 caracteres falla. Bajo B, además: cada argumento `NULL`, y `p_decision` con forma de inyección (`APPROVED'; --`).
- **Sombras de `pg_temp`.** Tablas temporales que sombrean `quote_approvals`, `quotes`, `role_permissions`, `membership_roles`, `organization_memberships` y `discount_policies` con filas falsas que darían permiso no cambian el resultado.
- **Metadatos.**
  - A: la guarda y `quote_discount_within` no son `prosecdef`, tienen `pg_temp` al final del `search_path` y `crm_app` no puede quitar ni reemplazar el disparador.
  - B: `prosecdef`, propietario `crm_migrator`, `proconfig` con `pg_temp` al final, `has_function_privilege('public', …)` falso, y `crm_app` no puede `CREATE OR REPLACE` ni `ALTER` la función.
- **Concurrencia**, con dos conexiones:
  - dos decisiones a la vez: una gana y la otra ve `NotPending`;
  - una solicitud nueva contra una aprobación en curso: el `FOR SHARE` espera y luego rechaza.
- **Matriz de equivalencia** del límite contra `discounts._authority`. Casos: sin descuento; justo en el límite; 0,001 % por encima; varias políticas (de la lista y de todas); tope en importe en la moneda y en otra; Owner; precio manual; sin política. Python redondea hacia arriba al céntimo y es más estricto: no puede haber ninguna fila en la que la base rechace lo que Python acepta.
- **Reversión.** La migración atrás y adelante; la reversa restaura los privilegios exactos y las funciones exactas (`pg_get_functiondef` antes y después).
- **Mutaciones** (≥ 10) sobre la guarda o la función: filtros de tenant, actor, permiso, separación, estado, límite y columnas.

## Consequences

- **Con A o B, y solo frente a un fallo de la aplicación**, un `UPDATE` directo de `crm_app` ya no decide en nombre de otra persona, sin permiso o por encima de su límite. Ninguna de las dos lo impide frente a un SQL hostil de `crm_app` que fije las GUCs para suplantar a otro aprobador del mismo tenant (modelo de amenaza). Con C, nada cambia.
- Con B, un uso nuevo de `SECURITY DEFINER` amplía lo que revisa el checklist de security-boundaries.md §6: cada consulta de la función tiene que filtrar por tenant, y la revisión lo comprueba línea por línea.
- Las pruebas que hoy deciden con `UPDATE` directo cambian (lista en cada opción). No se pierde ningún `CHECK` ni ninguna guarda.

## Security implications

- **A:** reduce el `UPDATE` de `crm_app` sobre `quote_approvals` a seis columnas vigiladas; no le da ningún privilegio nuevo. Con RLS activo dentro de la guarda, un error de implementación no cruza tenants.
- **B:** cambia el `UPDATE` de la tabla por el `EXECUTE` de una función acotada que escribe. El riesgo principal es que un filtro de tenant ausente dentro de la función escribiría en otro tenant con `BYPASSRLS`. Mitigaciones: el filtro en la misma sentencia del bloqueo, una prueba entre tenants por cada consulta, mutaciones que quitan cada filtro y una revisión independiente.
- **Ninguna opción** protege de un SQL hostil que suplante a otro aprobador del mismo tenant. La documentación no debe decir lo contrario.
- No revela costos: el suelo de costo sigue en `apps.pricing.floor` (ADR-006).

## Operational implications

- A o B: una migración nueva en `quotes` (la siguiente libre después de #709 y #705), cambios en `apps.quotes.decisions` y `apps.quotes.commands` (la traducción de errores en A; la llamada a la función en B) y sus pruebas, dentro de #693.
- **Plan de reversión:** migrar `quotes` a la migración anterior.
  - A: la reversa vuelve a conceder el `UPDATE` de la tabla y restaura las guardas exactas de `0003` y `0004`.
  - B: además, elimina la función, y el código anterior vuelve a escribir con el ORM.
  - Sin datos que migrar: las decisiones ya escritas no cambian.

## Decisión requerida del propietario

| #   | Pregunta                                                             | Opciones                                                                                                                                               | Recomendación                                                                                             | Si elige → siguiente paso                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| --- | -------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------ | --------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1   | ¿Cómo se impone en la base la decisión de una solicitud?             | **A:** disparador `INVOKER` y `UPDATE` por columna. **B:** función `SECURITY DEFINER` con excepción acotada a ADR-002 §3.3. **C:** solo la aplicación. | **A**: misma garantía frente a un fallo de la aplicación, sin excepción a ADR-002, con RLS activo dentro. | **A →** ADR-031 pasa a `Accepted` con este título y archivo; ADR-002 no cambia (sin nota en el índice); el cuerpo de #693 se reescribe con el contrato de A (sin función nueva) y #693 vuelve a `status:ready` cuando se cierre #652. **B →** ADR-031 pasa a `Accepted` con la opción B, se retitula «`SECURITY DEFINER` para decidir solicitudes de aprobación de cotizaciones» y se renombra a `ADR-031-quote-approval-decision-definer.md`; la fila de ADR-002 del índice añade «(excepción acotada en ADR-031)»; #693 conserva su alcance actual (función y `REVOKE UPDATE`) con el contrato de B y vuelve a `status:ready` cuando se cierre #652. **C →** ADR-031 pasa a `Deprecated` (no adoptado); #693 se cierra como no planificado y la deuda queda en phase-5.md (F5-09b3) con un issue `tech-debt`; ADR-002 no cambia. |
| 2   | ¿Se congelan las notas internas (`internal_notes`) fuera de `DRAFT`? | Congelarlas (como `notes_to_customer`) o dejarlas editables (anotaciones del equipo tras enviar).                                                      | Dejarlas editables: no llegan al cliente ni cambian el importe.                                           | Se aplica en #705 (su guarda); no cambia #693 ni este ADR.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| 3   | ¿La base comprueba también el suelo de costo al aprobar?             | Sí (la guarda o la función leen costos) o no (lo hace el comando).                                                                                     | No: leer costos en la base abre otro camino al costo (ADR-006) y quien aprueba no escoge el precio.       | No: nada que hacer. Sí: #693 añade la lectura del costo a su contrato y un ADR o una enmienda de ADR-006 define quién puede leer costos en la base.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                |

Hasta que el propietario decida, #693 está bloqueado y la deuda sigue anotada en [phase-5.md](../phases/phase-5.md) (F5-09b3).

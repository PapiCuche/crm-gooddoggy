# ADR-044: Excepción acotada a ADR-002 §3.3 para las dos funciones que escriben el registro de gasto de IA

- **Status:** Accepted (2026-11-18, decisión del propietario registrada en la sesión del orquestador, a raíz de la revisión independiente de [#944](https://github.com/PapiCuche/crm-gooddoggy/pull/944))
- **Date:** 2026-11-18
- **Deciders:** propietario del producto; redactado por el programa autónomo (ADR-015).
- **Work items:** F8-05b1 ([#942](https://github.com/PapiCuche/crm-gooddoggy/issues/942)), parte de F8-05b ([#932](https://github.com/PapiCuche/crm-gooddoggy/issues/932))
- **Related:** [ADR-002](ADR-002-postgresql-rls.md) §2 y §3.3 (lo enmienda **solo** con esta excepción), [ADR-034](ADR-034-ai-copilot-first.md) §5, [ADR-043](ADR-043-ai-budget-time-zone-lima.md), [ADR-031](ADR-031-quote-approval-decision-guard.md) (Proposed; plantea una excepción parecida para las cotizaciones y no queda decidida por este ADR), [ADR-015](ADR-015-autonomous-delivery-program.md) §3

## Context

ADR-002 §3.3 admite funciones `SECURITY DEFINER` solo para los lookups legítimos entre tenants, y dice que no deberían escribir. F8-05b1 cierra dos huecos del registro de gasto de IA que dejó F8-05: con SQL directo, `crm_app` podía bajar `reserved_usd` e insertar filas inventadas en `ai_llm_calls`. RLS no basta para impedirlo, porque el atacante es el propio runtime dentro de su tenant. Hace falta que `crm_app` no tenga INSERT ni UPDATE sobre esas tablas y que el registro solo cambie por un camino que la base controla.

Un disparador `INVOKER` (la opción que ADR-031 recomienda para las cotizaciones) no sirve aquí: dejaría a `crm_app` el INSERT en `ai_llm_calls` y el UPDATE de las columnas de dinero, que son justo lo que hay que quitar. Las dos funciones de F8-05b1 escriben filas del propio tenant como `crm_migrator`, que tiene BYPASSRLS. Eso contradice la letra de ADR-002 §3.3, y la revisión independiente de #944 lo marcó como FIX NOW.

## Decision

Se admite una **excepción acotada** a ADR-002 §3.3. Solo dos funciones pueden ser `SECURITY DEFINER` `VOLATILE` que escriben filas de un tenant: `public.ai_budget_reserve` y `public.ai_budget_settle`. Escriben solo dentro de un tenant, en `ai_budget_usage`, `ai_budget_reservations` y `ai_llm_calls`. Condiciones obligatorias:

1. **Tenant y quien pide, solo de la sesión:** `app_current_tenant()` y `app_current_user()`, la misma raíz de confianza que RLS (los GUC de ADR-002 §2). Nunca llegan como parámetro. Sin tenant, la función falla.
2. **Filtro explícito por tenant:** cada sentencia que lee o escribe filas de un tenant lleva `organization_id = app_current_tenant()` (o el valor guardado de esa llamada). Su dueño tiene BYPASSRLS, así que este filtro sustituye a RLS dentro de la función.
3. **Los requisitos de ADR-002 §3.3**, salvo «no escribe» y `STABLE`: dueño `crm_migrator`, `search_path = pg_catalog, public, pg_temp` con los objetos cualificados, sin SQL dinámico, `REVOKE ALL … FROM PUBLIC`, `GRANT EXECUTE` solo a `crm_app`, retorno mínimo (un id o un estado), y la entrada en `backend/tests/test_pg_temp_shadowing.py` y en la introspección T16.
4. **Pruebas que matan los mutantes que quitan el filtro de organización** en cada sentencia, y pruebas como `crm_app` con SQL directo de que no puede escribir esas tablas por otro camino.

Cualquier otra función `SECURITY DEFINER` que escriba necesita **su propio ADR**. Este no sienta precedente para otros módulos, ni decide ADR-031.

## Alternatives considered

| Alternativa                                                    | Por qué no                                                                                                       |
| -------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------- |
| Mantener los grants de F8-05 (INSERT y UPDATE por columna)      | `crm_app` puede bajar lo reservado e inventar llamadas: el gasto se equivoca hacia abajo.                         |
| Disparadores `INVOKER` sobre las tablas                         | Exigen dejar a `crm_app` el INSERT y el UPDATE que se quieren quitar; la guarda dependería de reconstruir el estado. |
| Un rol propio para el gateway en lugar de `crm_migrator`        | Un rol nuevo con BYPASSRLS y credencial aparte (ADR-002 §1.1) amplía la superficie más que dos funciones acotadas. |

## Consequences

- El registro de gasto de IA solo cambia por dos funciones auditables en una migración. `crm_app` conserva SELECT con RLS.
- Las guardas de F8-05 (ajuste único, lo gastado no baja, sin `NaN`) siguen activas y también se aplican al dueño.
- Las partes siguientes de #932 (tope diario, avisos, barrido de huérfanas) pueden ampliar **estas dos funciones** con las mismas condiciones. Una función nueva que escriba, como la del barrido, necesita su propio ADR o una enmienda de este.

## Security implications

El riesgo es que una función con BYPASSRLS escriba en otro tenant. Lo acotan el filtro explícito en cada sentencia (condición 2), los mutantes que lo quitan (condición 4) y el origen del tenant: el mismo GUC que usa RLS, que no da a quien llama más alcance del que ya tiene. El costo real que llega al ajuste sigue garantizándolo la aplicación: la base solo limita la liberación a esa reserva, una vez.

## Operational implications

Ninguna acción al desplegar. Revisar esta excepción si una de las dos funciones cambia de firma o empieza a tocar otras tablas.

# ADR-043: El día y el mes del presupuesto de IA, en hora de Lima

- **Status:** Accepted (2026-11-18, decisión del propietario registrada en [#932](https://github.com/PapiCuche/crm-gooddoggy/issues/932))
- **Date:** 2026-11-18
- **Deciders:** propietario del producto (#932); redactado por el programa autónomo (ADR-015).
- **Work items:** F8-05b1 ([#942](https://github.com/PapiCuche/crm-gooddoggy/issues/942)); el tope diario llega en otra parte de #932.
- **Related:** [ADR-034](ADR-034-ai-copilot-first.md) §5 (lo enmienda **solo** en la frase de la zona horaria), [ADR-011](ADR-011-observability-and-logs.md)

## Context

ADR-034 §5 dice que el día y el mes del presupuesto de IA son los de la zona horaria de la organización (`organizations.timezone`) en el instante de la reserva, tomado de `now()` de la base. Esa columna no existe: solo las sucursales tienen zona horaria (`branches.timezone`). F8-05 (#768) usó el mes de UTC como desvío temporal y dejó la decisión al propietario.

## Decision

- El **día y el mes** del presupuesto de IA son los de **`America/Lima`** para **todas** las organizaciones. El mes empieza el día 1 a las 00:00 de Lima; el día, a las 00:00 de Lima.
- El instante sigue siendo `now()` de la base en el momento de la reserva. La base calcula el mes con `ai_budget_month(timestamptz)` (`date_trunc('month', instante AT TIME ZONE 'America/Lima')`); el día, cuando llegue el tope diario, con la misma zona.
- Las **particiones** de `ai_llm_calls` siguen siendo por mes de **UTC**: son almacenamiento, no presupuesto. Una llamada se cobra en el mes de Lima de su reserva, aunque su fila caiga en la partición de otro mes de UTC.
- El resto de ADR-034 §5 no cambia: reserva como cota superior, filas del mes y después del día, corte duro y lo dudoso contado como gastado.

## Alternatives considered

| Alternativa                                      | Por qué no                                                                                                       |
| ------------------------------------------------ | ---------------------------------------------------------------------------------------------------------------- |
| UTC (lo de F8-05)                                | El mes cambiaría a las 19:00 del último día en Lima: el propietario vería su tope renovado antes de tiempo.      |
| Añadir `organizations.timezone`                  | Una columna, un formulario y una migración de datos para un único caso hoy; se puede proponer con un ADR nuevo. |
| La zona de la sucursal principal                 | Una organización puede no tener sucursal principal, y cambiarla movería el mes del presupuesto.                  |

## Consequences

- Todas las organizaciones comparten el mismo corte de mes y de día, el de Lima.
- Lima no tiene horario de verano: el corte es siempre a las 05:00 UTC.
- Si un día hace falta otra zona, un ADR nuevo la añade; las filas ya escritas conservan su mes.

## Security implications

Ninguna nueva: el mes lo calcula la base dentro de `ai_budget_reserve`, con su reloj, nunca con un valor que llegue de la aplicación o del usuario.

## Operational implications

Ninguna acción al desplegar: F8-05 no tiene llamadores todavía, así que no hay filas de meses UTC que convertir.

# ADR-016: Convención de los listados de la API: paginación por cursor

- **Status:** Accepted
- **Date:** 2026-10-04
- **Deciders:** programa autónomo (ADR-015 §5). El mantenedor puede reemplazarlo.
- **Related:** ADR-003 §5, ADR-004, ADR-014, [05-backlog-y-roadmap.md](../fase-0/05-backlog-y-roadmap.md) (E00-09)

## Context

La API no tenía ningún listado paginado: el único que existe, `GET /api/v1/me/organizations/` (ruta de plataforma), devuelve en un array las organizaciones del propio usuario. El backlog (E00-09) pide paginación por cursor, y el contrato de F1-08A no la incluyó. El primer listado real (el directorio de miembros de una organización) llega a continuación; sin una convención, cada lista inventaría sus parámetros, su orden y su sobre de respuesta, y el cliente generado tendría un tipo distinto para cada una.

Dos riesgos concretos de un listado sin convención:

- **Salir entero.** Un `ListAPIView` de DRF sin paginador devuelve todas las filas. Con datos reales es una respuesta sin tope.
- **Orden inestable.** Sin un desempate único, dos páginas consecutivas pueden repetir una fila o saltársela.

## Decision

### 1. Paginación por cursor, por defecto

`core.api.pagination.CursorPagination` es el `DEFAULT_PAGINATION_CLASS` de DRF. Una vista genérica de lista (`ListAPIView`, el `list` de un viewset) queda paginada sin declararlo.

Es un valor por defecto, no una barrera: una vista que declare `pagination_class = None`, o un `APIView` que construya la lista a mano, no pasa por el paginador y devuelve el listado entero. Un listado de tenant no hace ninguna de las dos cosas. Hoy lo comprueba la revisión; la auditoría del URLconf no lo detecta.

### 2. Parámetros

| Parámetro | Significado |
|---|---|
| `limit` | Filas por página. Por defecto 50, máximo 200 |
| `cursor` | El valor `next` de la página anterior. Sin él, la primera página |

Un `limit` que no sea un entero entre 1 y 200, o un `cursor` ilegible o con una forma que la API no emite (sin posición, con desplazamiento, hacia atrás, o con una posición que no es un identificador), responden 400 `VALIDATION_ERROR` con el campo en `fields` (ADR-014 §1). No se recortan ni se ignoran en silencio.

El cursor no va firmado: uno bien formado con el identificador de otra posición se acepta, con el efecto que describe §5.

### 3. Respuesta

```json
{ "results": [ … ], "next": "…" }
```

`next` es `null` en la última página. Es un valor opaco, no una URL: el cliente lo devuelve tal cual en `?cursor=`. No hay total de filas ni página anterior.

### 4. Orden

- El orden lo declara la vista (`ordering`), nunca el cliente.
- Por defecto es `id`: los identificadores son UUIDv7 (ADR-004), así que es el orden de creación, único y estable.
- La única alternativa es `-id` (lo más reciente primero). Con cualquier otro orden la vista falla al paginar (`ImproperlyConfigured`): es un error de programación, no una respuesta.
- El motivo: el cursor de DRF solo guarda la posición en la primera columna del orden. Si esa columna no es única, las filas empatadas se recorren por desplazamiento, que repite filas cuando se insertan otras y no pasa de 1000 filas iguales (después devuelve siempre la misma página). Si admite nulos, las filas con nulo no salen nunca. Un desempate por `id` al final no evita ninguna de las dos cosas.

### 5. Tenancy y alcance

El paginador recibe el queryset después de `ScopeFilter` (ADR-003 §5): primero se filtra por organización y por alcance, y después se pagina. El cursor solo codifica una posición en el orden. No es una credencial y no amplía lo que el usuario puede ver: un cursor fabricado, o tomado de otra organización, solo cambia desde qué posición se leen las filas propias.

### 6. Fuera de esta decisión

Filtros, búsqueda, ordenación elegida por el cliente, orden por otra columna (necesita un cursor por columna e `id`, que DRF no trae), total de filas y paginación hacia atrás. Cada uno se decide cuando una pantalla lo necesite, sin cambiar lo anterior.

## Alternatives considered

| Alternativa | Por qué se descarta |
|---|---|
| Paginación por número de página u `offset` | Con inserciones entre dos páginas repite o se salta filas; el coste crece con el desplazamiento; y el backlog ya pedía cursor |
| Devolver URLs `next` y `previous`, como DRF por defecto | Atan al cliente al host y a la ruta que ve el backend detrás del proxy; el cliente generado ya sabe construir la petición y solo necesita el cursor |
| Recortar un `limit` excesivo al máximo | Oculta un error del cliente: pide 500 y recibe 200 creyendo que no hay más |
| Paginador declarado en cada vista | Una vista que lo olvide devuelve el listado entero: el fallo por defecto sería el inseguro |
| Implementación propia del cursor | DRF ya trae una probada; solo se cambian el sobre, los errores y la regla del orden |

## Consequences

- Los listados de los módulos de negocio no necesitan código de paginación: declaran su queryset y, si hace falta, `-id` como orden.
- El cliente generado (orval) tiene un tipo `Paginated…List` por recurso, con `results` y `next`.
- Un listado no puede mostrar «página 3 de 12» ni saltar a una página: es el coste de no contar filas. Si una pantalla lo necesita, se decide entonces.
- Un listado no puede ordenarse por nombre ni por fecha: solo por orden de creación, ascendente o descendente. El `order_by` del queryset de la vista se descarta al paginar.

## Security implications

- Un listado paginado no sale entero: el tamaño de la respuesta tiene un máximo que el cliente no controla. Una vista que no pase por el paginador (§1) no tiene ese tope.
- El cursor solo lleva un identificador: el `id` de una fila dentro del alcance del usuario. No lleva ningún otro dato de la fila.
- La frontera de tenant no depende del cursor: la aplican RLS y `ScopeFilter` en cada petición.

## Operational implications

- `limit` y `cursor` aparecen en el contrato OpenAPI de cada listado, con sus límites.
- Cambiar el tamaño por defecto o el máximo es un cambio de contrato: se hace en `core.api.pagination` y se regenera el schema.

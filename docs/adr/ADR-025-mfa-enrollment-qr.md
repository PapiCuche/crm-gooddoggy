# ADR-025: Código QR en el alta del segundo factor

- **Status:** Accepted
- **Date:** 2026-10-22
- **Deciders:** programa autónomo (ADR-015 §5). El mantenedor puede reemplazarlo.
- **Related:** ADR-023 §4, ADR-012 §1 y política de actualización (lo amplía con una fila), ADR-015 §3 y §5, F2-112 (#283), F2-114 (#286)

## Context

Desde F2-112 el alta del segundo factor en `/cuenta/seguridad` enseña la clave TOTP en grupos de cuatro, con un botón para copiarla, y un enlace `otpauth://`. El enlace solo ayuda si la página está abierta en el teléfono que tiene la aplicación de autenticación. En un ordenador, la persona tiene que escribir a mano 32 caracteres en el teléfono.

Las aplicaciones de autenticación esperan un código QR con la URI `otpauth://` que la API ya devuelve (`POST /api/v1/auth/mfa/totp/`, campo `uri`). Dibujarlo necesita una biblioteca que ADR-012 no verificó. El mantenedor pidió que se decida en un ADR propio y en un work item aparte.

Lo que importa de esa biblioteca es que maneja la clave: no puede enviarla a ningún sitio, guardarla ni ejecutar código que no se haya leído.

## Decision

### 1. El QR se dibuja en el navegador

El QR se calcula en el navegador a partir del `uri` que ya devolvió la API, en el paso de la clave del alta, junto a la clave escrita. La API no cambia: la clave no sale en ninguna forma nueva ni por ningún camino nuevo.

### 2. Con `uqr` 0.1.3, solo su función `encode`

- Se usa la biblioteca npm `uqr`, versión exacta **0.1.3**, solo su función `encode`, que devuelve la matriz de módulos (`boolean[][]`).
- La pantalla dibuja la matriz como un `<svg>` de React con un solo `<path>`. No se usa `renderSVG` ni ningún HTML generado por la biblioteca, ni `dangerouslySetInnerHTML`.
- Corrección de errores **M** (15 %) y margen de **4** módulos, el que pide la norma ISO/IEC 18004. Negro sobre blanco en los dos temas, porque los lectores de QR lo esperan así.

### 3. Verificación de la dependencia (2026-10-22)

| Punto | Resultado |
|---|---|
| Versión y fecha | 0.1.3, publicada el 2026-04-03: más de seis meses, por encima de `minimumReleaseAge` |
| Licencia | MIT (Project Nayuki; Anthony Fu). El código deriva de la biblioteca de referencia de QR de Project Nayuki |
| Dependencias | ninguna |
| Scripts de instalación | ninguno (`allowBuilds` de pnpm no cambia) |
| Tamaño | 73 KB en `dist` (ESM, CJS y tipos) |
| Uso | unos 3,8 millones de descargas semanales en npm |
| Mantenimiento | repositorio `unjs/uqr` |
| Lectura del código | `dist/index.mjs` no usa red (`fetch`, `XMLHttpRequest`), DOM, `window`, almacenamiento, `eval`, `Function` ni `import` dinámico, y no importa nada |
| Integridad | `sha512-0rjE8iEJe4YmT9TOhwsZtqCMRLc5DXZUI2UEYUUg63ikBkqqE5EYWaI0etFe/5KUcmcYwLih2RND1kq+hrUJXA==`, la del paquete leído, fijada en `pnpm-lock.yaml` |
| Auditoría | `pnpm audit --prod` sin vulnerabilidades |

### 4. La clave escrita se queda

El QR no sustituye a la clave escrita ni al enlace: quien no puede escanear (lector de pantalla, sin cámara, la página en el propio teléfono) sigue teniéndolos. El QR lleva un nombre accesible que dice para qué sirve, sin leer la clave.

### 5. Nada se guarda

La matriz se calcula en memoria al pintar el paso y desaparece con él, como la clave (ADR-023 §4). Nada va al almacenamiento, a la URL, a los logs ni a terceros.

### 6. Fila de versión y actualizaciones

- Este ADR amplía la tabla de ADR-012 §1 con una fila; ADR-012 no se edita, y el índice lo dice:

  | Componente | Versión | Publicada | Soporte / estado | Fuente oficial |
  |---|---|---|---|---|
  | uqr (frontend) | **0.1.3** | 2026-04-03 | Sin dependencias; solo `encode` | npm `uqr` · github.com/unjs/uqr |

- Las actualizaciones siguen la categoría «Tooling» de la política de ADR-012. Además, toda versión nueva vuelve a pasar la lectura del punto 3 (sin red, DOM, almacenamiento ni `eval`), y se fija la versión exacta.

## Alternatives considered

- **SVG generado en el backend** con `segno` (Python, BSD, sin dependencias). Cambia la API: la respuesta del alta llevaría la clave también dibujada. El frontend tendría que insertar ese SVG como HTML, o como imagen `data:`, que la CSP no admite. Descartado.
- **`qrcode`** (node-qrcode). Tres dependencias, entre ellas `yargs`, por su línea de comandos. Más superficie sin motivo.
- **`qrcode-generator`**. Sin dependencias y maduro, pero 555 KB, sin módulo ESM, y su última versión es de 2025.
- **`lean-qr`**. Sin dependencias y pequeño, con muchos menos usuarios.
- **Un codificador propio.** Es fácil equivocarse en Reed-Solomon y en las máscaras; mejor una implementación derivada de la de referencia.
- **Sin QR.** Deja a quien usa un ordenador escribiendo 32 caracteres a mano.

## Consequences

- El frontend gana una dependencia de producción pequeña y sin dependencias propias.
- La pantalla del alta enseña el QR junto a la clave; el resto del alta no cambia.
- Si la biblioteca dejara de mantenerse, se puede sustituir sin tocar la API: solo se usa `encode`.

## Security implications

- La clave no sale del navegador por este cambio. La biblioteca es código puro que no hace peticiones ni guarda nada, verificado leyendo el paquete exacto, cuya integridad queda fijada.
- No se inserta HTML generado: React dibuja la matriz, así que la biblioteca no puede inyectar marcado.
- La CSP no cambia: un `<svg>` en línea no necesita fuentes nuevas.
- Quien ve la pantalla ve la clave igual que antes; el QR no la expone más.

## Operational implications

- Ninguna en el backend ni en la infraestructura.
- La revisión trimestral de la baseline de ADR-012 incluye esta fila.

# frontend/

Next.js 16 (App Router) + TypeScript estricto (`strict`, `noUncheckedIndexedAccess`), Tailwind 4, shadcn/ui, TanStack Query y next-intl (`es-PE`). Versiones: ADR-012 §4.1.

## Uso

Requiere Node 24.21.0 (`.nvmrc`) y pnpm 12.8.1 (`packageManager`).

```bash
cd frontend
pnpm install --frozen-lockfile
BACKEND_ORIGIN=http://127.0.0.1:8000 pnpm dev   # /api y /ws → Django (same-origin, ADR-003)
```

| Script                              | Qué hace                                                                       |
| ----------------------------------- | ------------------------------------------------------------------------------ |
| `pnpm format:check` / `pnpm format` | Prettier (con orden de clases de Tailwind)                                     |
| `pnpm lint`                         | ESLint (Next + `react/no-danger`, sin `innerHTML`), sin warnings               |
| `pnpm typecheck`                    | `tsc --noEmit`                                                                 |
| `pnpm test --run`                   | Vitest + Testing Library                                                       |
| `pnpm build`                        | Build de producción (`BACKEND_ORIGIN` se fija en el build)                     |
| `pnpm api:generate`                 | orval: `../backend/openapi/schema.yaml` → `src/lib/api/` (generado, no editar) |

## Estructura

- `src/app/`: `/login` y `/o` son el acceso (ver «Acceso»); `/o/[orgSlug]` es el workspace de una organización (ver «Workspace de una organización»); `/` lleva a `/o`; `/status` muestra el estado del backend (consultado desde el servidor); `/demo` es la demo visual (ver abajo).
- `src/components/app-shell/`: guardia de la organización (`TenantGate`), barra lateral, barra superior y workspace, con el lenguaje del Figma GOOD DOGGY. El workspace necesita JavaScript: hasta que la API responde, el HTML solo lleva el aviso de carga. En pantallas pequeñas la barra lateral va en un menú (`<dialog>` nativo, con su botón de cerrar). WCAG AA, `prefers-reduced-motion`.
- `src/components/auth/`: formulario de acceso y lista de organizaciones.
- `src/components/ui/`: componentes shadcn/ui y primitivas de formulario (`TextField`).
- `src/lib/api/`: cliente generado por orval. **Nunca** tipos de API a mano: si cambia el contrato, regenerar el schema del backend y después ejecutar `pnpm api:generate`.
- `src/lib/http.ts`: el único `fetch` hacia la API (ver «Cliente de API»).
- `src/components/demo/`: landing y workspace de la demo visual, con sus datos ficticios.
- `messages/es-PE.json`: catálogo i18n.

La primera pantalla de gestión es «Miembros» (ver «Miembros»). Las pantallas reales se construyen en `/o/[orgSlug]`, sobre la API y el cliente generado, con el Figma GOOD DOGGY como referencia visual ([AGENTS.md](../AGENTS.md) §11).

## Lenguaje visual (F2-08A, D-F2-7)

Las pantallas oficiales usan el tema claro del Figma GOOD DOGGY a través de los tokens semánticos de `src/app/globals.css`. Un componente oficial nunca usa los tokens `--gd-*`, que son los de la demo congelada.

| Token                | Valor                 | Uso                                                                      |
| -------------------- | --------------------- | ------------------------------------------------------------------------ |
| `background`         | `#f3f3f3`             | Lienzo de la aplicación                                                  |
| `surface`            | `#ffffff`             | Barra lateral, tarjetas, formularios                                     |
| `surface-raised`     | `#ebebeb`             | Hover (sutil: la selección usa además otra señal)                        |
| `foreground`         | `#202020`             | Texto, bordes de control, botón primario                                 |
| `muted`              | `#5c5c5c`             | Texto secundario (AA sobre papel y sobre el lienzo)                      |
| `border`             | `#dedede`             | Divisores                                                                |
| `accent`             | `#ffdb5b`             | Miel: elemento activo y acciones del workspace. Nunca color de texto     |
| `success` / `danger` | `#1a7f37` / `#b42318` | Estados, con AA sobre papel y sobre el lienzo (no como texto sobre miel) |

- **Fuente:** DM Sans (`next/font`, servida desde el propio origen); Geist Mono para identificadores.
- **Botones** (`components/ui/button.tsx`): `primary` (tinta), `accent` (miel) y `ghost`. Responden a la pulsación con una escala breve. `ink` y `honey` son de la demo.
- **Movimiento:** lo que se mueve usa solo `transform`, `scale` y `opacity`, con la curva `--ease-out` y menos de 300 ms; los cambios de color son transiciones breves. Lo que se usa muchas veces al día no se anima; `prefers-reduced-motion` lo desactiva todo, también el fondo del menú.
- **Sin marco en Figma:** el Figma no tiene pantallas de login ni de selección de organización. Las pantallas nuevas derivan de los marcos del workspace y de «Acceso demo».

## Cliente de API (F2-08A, ADR-014)

orval genera las funciones y los hooks; todos llaman a `apiFetch` (`src/lib/http.ts`), el único `fetch` hacia `/api/`:

- Mismo origen y cookies del mismo origen. La sesión es una cookie `HttpOnly`: el código no la ve ni guarda nada de ella en `localStorage`.
- Solo para el navegador (URL relativa y `document.cookie`): un Server Component no lo llama. Si una página del servidor necesita datos del usuario, delega en un componente cliente.
- En `POST`, `PUT`, `PATCH` y `DELETE` copia la cookie `csrftoken` en la cabecera `X-CSRFToken`. Si la cookie no existe todavía, la pide antes; si no la consigue, no envía la escritura y el error es el de esa petición. Una escritura a otro origen se rechaza sin enviarla.
- Una respuesta de error se convierte en `ApiError` con `status`, `code`, `fields` y `retryAfter` (segundos enteros), y así se tipa el `error` de cada hook generado. La red caída, también a mitad de una respuesta, es `NETWORK_ERROR` (`status` 0). Un 2xx que no es JSON es `INTERNAL_ERROR`. El texto de la respuesta nunca se muestra.
- Cancelación: TanStack Query cancela con su propia señal (cambio de `queryKey`, desmontaje, `cancelQueries`) y eso no llega a la pantalla como error. Con una señal propia, un `abort()` sin motivo se relanza como `AbortError`; un motivo propio o `AbortSignal.timeout` llegan como `NETWORK_ERROR`.
- `apiErrorKey(error)` (`src/lib/api-errors.ts`) da la clave del mensaje en `messages/es-PE.json` → `errors.api`. Un `code` sin mensaje propio usa el genérico.
- TanStack Query no repite una respuesta 4xx ni una escritura; reintenta una vez lo que no llegó o falló en el servidor.

## Acceso (F2-08B, ADR-003 §1–3)

- **`/login`:** correo y contraseña contra `POST /api/v1/auth/login/`. La sesión es la cookie `HttpOnly` que pone la API; el formulario no guarda nada. Quien ya tiene sesión pasa al destino en cuanto la API lo confirma: hasta entonces ve el formulario normal; desde la confirmación y hasta que la navegación termina, el botón queda ocupado. El formulario declara `method="post"`: un envío antes de que cargue el JavaScript no pone la contraseña en la URL.
- **Foco y errores:** sin sesión, el foco va al correo. Si falta un campo, el foco va al primero que falta y su aviso se retira al escribir. Un segundo envío mientras el primero está en curso no cuenta. Sin red, el intento falla enseguida con su mensaje; no queda en cola. Si la API no contesta en 30 segundos, el formulario deja de esperar, lo dice y permite reintentar (la petición abandonada no se cancela: si acaba contestando bien, la sesión queda abierta y el siguiente envío entra). Un error de validación de la API (400) muestra el mensaje general, sin marcar el campo: los campos ya limitan su longitud a la del contrato.
- **Rechazos:** el mensaje sale del `code` de la API. `INVALID_CREDENTIALS` no dice si falló el correo o la contraseña. `RATE_LIMITED` muestra la espera de `Retry-After`, en minutos hacia arriba.
- **Destino (`next`):** `safeNext` (`src/lib/next-path.ts`) resuelve el valor como lo haría el navegador y devuelve la ruta ya resuelta, solo si queda en este origen. Se cambian por `/o`: una URL absoluta, `//`, `\`, un carácter de control, una ruta que tras resolver sus puntos empieza por `//` (`/.//otro.sitio`), el propio `/login` y lo que no es una página (`/api/`, `/_next/`). Garantiza que el destino no sale del origen, no que la página exista.
- **`/o`:** organizaciones del usuario (`GET /api/v1/me/organizations/`). Con una sola entra directamente; con varias, lista para elegir; con ninguna, un estado vacío. `/o?elegir` muestra siempre la lista. Cada cambio de estado se anuncia en una región viva, y al reintentar tras un error el foco va al botón nuevo o a la lista que llegó.
- **Sesión terminada:** `Providers` es el único punto que trata un 401 de una lectura: navega a `/login?next=…` con la ruta en la que estaba. Es una navegación completa, así que no queda estado en memoria.
- La lista de organizaciones es la que devuelve la API para la sesión. Elegir una no autoriza nada: lo decide la API en cada petición a `/o/[orgSlug]`.

## Workspace de una organización (F2-08, ADR-003 §5)

`/o/[orgSlug]` no pinta nada del workspace hasta que la API responde a `GET /api/v1/o/{slug}/me/`, en cada entrada: la respuesta no se guarda entre visitas (`gcTime: 0`). La URL solo selecciona: quién es el usuario ahí y qué puede hacer lo dice la API.

- **Guardia (`TenantGate`):** con la respuesta, monta el shell y deja el contexto en `useTenant()`. Sin sesión (401), `Providers` lleva a `/login` con vuelta a la misma ruta. Organización inexistente o sin membresía (404): la misma página de «no encontrada» que cualquier dirección que no existe. Cualquier otro error al entrar (organización suspendida, red, servidor): una tarjeta con el mensaje de su `code`, reintentar, cambiar de organización y cerrar sesión. Reintentar muestra el aviso de carga y, al terminar, deja el foco en el botón (si vuelve a fallar) o en el workspace (si abre). Con el workspace ya abierto, el contexto se vuelve a pedir al volver a la pestaña o al recuperar la red, si la última respuesta tiene más de 30 segundos: un 401, un 403 o un 404 lo cierran, y sigue cerrado hasta que la API vuelve a responder bien; un fallo pasajero (red, servidor) no cierra el workspace ni lo reabre tras una negativa. Con la tarjeta de error en pantalla, ni volver a la pestaña ni recuperar la red piden nada: lo hace «Reintentar». Si el navegador sabe que no hay red, la petición espera en el aviso de carga y sigue sola al volver la conexión.
- **Navegación por permisos:** `NAVIGATION` (`navigation.ts`) lista las entradas del menú y el permiso del catálogo que da sentido a cada una; `visibleItems` deja las que el usuario puede abrir. Solo se listan módulos que existen: hoy, «Inicio» y «Miembros» (`users.view`). Cada fase añade los suyos.
- **Es comodidad, no seguridad:** ocultar una entrada no protege nada. La API comprueba el permiso en cada petición, y una pantalla debe tratar el 403 aunque su entrada estuviera visible.
- **Roles:** se muestran como etiquetas. Nada en la interfaz decide por el nombre o el código de un rol.
- **Cambiar de organización:** enlace a `/o?elegir`.
- **Cerrar sesión:** `POST /api/v1/auth/logout/`; al responder se vacía la caché de datos y se va a `/login`. Un 401 al cerrar significa que la sesión ya no existía y se trata igual. Otro error se muestra encima de las acciones y la sesión sigue abierta; sin red el cierre falla enseguida y no queda en cola. Desde que se pulsa hasta que la página cambia el botón queda ocupado (`aria-disabled`, sin perder el foco) y una segunda pulsación no cuenta. Está en la barra lateral, en la tarjeta de error de la guardia y en `/o` (con la lista, con «sin organizaciones» y con el error; ahí sin «Cambiar de organización»). Es el mismo componente, `SessionActions`.
- **Título de la pestaña:** «Workspace · Good Doggy CRM» hasta que la API responde; después, el nombre de la organización, precedido por la sección si no es el inicio.
- **Inicio:** saluda con el nombre que da la API, muestra la organización y los roles. No hay datos de ejemplo: los módulos de negocio llegan con sus fases.

## Miembros (F2-17)

`/o/[orgSlug]/miembros` muestra quién pertenece a la organización, con su estado y sus roles: lo que devuelve `GET /api/v1/o/{slug}/members/` (F2-16), con el cliente generado.

- **Componente:** `components/members/members-list.tsx`. Por miembro: nombre (o el correo si no tiene), correo, roles como etiquetas, estado de la membresía y fecha de alta en la zona horaria de la aplicación. Un nombre o un correo largo se parte en varias líneas; no se recorta.
- **Paginación:** por cursor (ADR-016). «Cargar más» pide la página siguiente con el `next` de la anterior y desaparece cuando la API devuelve `null`. Desde la pulsación hasta la respuesta el botón queda ocupado (`aria-disabled`), también si el navegador sabe que no hay red y la petición espera. Al llegar la página el foco pasa a su primera fila, salvo que ya se haya ido a otra parte. Si esa página falla, lo ya cargado sigue en pantalla (salvo con un 403, que cierra la lista), el aviso aparece al lado del botón (no lo mueve) y el mismo botón reintenta; al reintentar el aviso se quita y vuelve si falla otra vez.
- **Estados:** cargando; error con «Reintentar» (el botón no se desmonta mientras reintenta, y al abrir la lista el foco va al título); y sin permiso (403), con un mensaje propio y sin reintento.
- **Negativas y sesión:** un 403 cierra la lista aunque ya estuviera en pantalla, y sigue cerrada hasta que la API vuelve a responder bien (un fallo pasajero no la reabre); si llega al reintentar o al cargar más, el foco va al título, salvo que el usuario ya lo haya llevado a otra parte. Sin sesión (401) no se muestra un error, tampoco al cargar más: `Providers` lleva al login. La lista no se guarda entre visitas (`gcTime: 0`): cada entrada pregunta a la API.
- **Navegación:** la entrada «Miembros» pide el permiso `users.view`. Es comodidad: quien abre la URL sin el permiso ve el mensaje de «sin permiso» porque la API responde 403.
- **Roles y estado:** se muestran tal como llegan. Nada decide por el nombre o el código de un rol. El estado es el de la membresía, no el de la cuenta.
- **Título de la pestaña:** «Miembros · organización · Good Doggy CRM». Lo pone el shell a partir de la entrada de navegación de la ruta.
- Solo lectura: invitar, activar, desactivar y cambiar roles son otros work items.

## Seguridad del navegador (F2-07)

Next emite todas las cabeceras de seguridad del HTML; Caddy no añade ninguna.

| Cabecera                                                                                                           | Dónde                                            | Valor                                     |
| ------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------ | ----------------------------------------- |
| `Content-Security-Policy`                                                                                          | `src/proxy.ts` (un nonce por petición)           | `src/lib/csp.ts`                          |
| `X-Frame-Options`, `X-Content-Type-Options`, `Referrer-Policy`, `Cross-Origin-Opener-Policy`, `Permissions-Policy` | `next.config.ts`                                 | Fijas                                     |
| `Strict-Transport-Security`                                                                                        | `next.config.ts`, solo en el build de producción | Los valores del backend (`production.py`) |

La política de producción:

```text
default-src 'self'; script-src 'self' 'nonce-…' 'strict-dynamic'; style-src 'self' 'nonce-…';
style-src-attr 'unsafe-inline'; img-src 'self' blob: data:; font-src 'self'; connect-src 'self';
object-src 'none'; frame-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'
```

- **Scripts:** solo se ejecutan los que llevan el nonce de la petición, y los que esos cargan (`'strict-dynamic'`). Sin `'unsafe-inline'` ni `'unsafe-eval'`. Un `<script>` insertado en el HTML, un manejador en línea (`onerror=…`) y `eval` quedan bloqueados.
- **Render dinámico:** Next pone el nonce al renderizar, así que ninguna página se prerenderiza en el build. El layout raíz llama a `connection()`. Efecto medido en el build: `/demo`, `/demo/workspace`, sus 14 módulos y la página 404 eran estáticas y ahora se renderizan en cada petición; `/` y `/o/[orgSlug]` ya eran dinámicas.
- **Excepción documentada:** `style-src-attr 'unsafe-inline'`. `next/image` y los estilos calculados (la altura de una barra) llegan como atributo `style` en el HTML del servidor. Un atributo `style` no ejecuta código y las etiquetas `<style>` siguen necesitando el nonce.
- **Desarrollo (`next dev`):** se añaden `'unsafe-eval'` a los scripts (React lo usa para las trazas) y `'unsafe-inline'` a los estilos. El build de producción nunca los incluye.
- **Sin `upgrade-insecure-requests`:** el stack local sirve HTTP y todas las fuentes son del propio origen. En producción el proxy con TLS redirige y HSTS fija HTTPS.
- **Alcance:** la CSP va en todo lo que responde Next, también en sus páginas 404 y en los estáticos. Solo quedan fuera `/api/` y `/ws/`, que sirve Django. Hoy las páginas de error de Django son HTML estático sin CSP; F2-12 (#59) pasa esos errores a JSON y añade una CSP cerrada a las respuestas de la API.

**Cómo añadir un origen.** Solo si el producto lo necesita y con su motivo en el PR: añadirlo a la directiva más estrecha en `src/lib/csp.ts` (por ejemplo `img-src` para un CDN de imágenes), nunca a `default-src`, y actualizar `src/lib/csp.test.ts`. Un script de terceros recibe el nonce con `<Script nonce>`; no se añade su dominio a `script-src`.

- **Páginas de error propias:** `src/app/not-found.tsx`, `error.tsx` y `global-error.tsx`. Las de serie de Next insertan un `<style>` sin nonce, que la política bloquea. Un módulo desconocido de la demo (`/demo/workspace/x`) responde 404 y muestra la página tras hidratar: al dejar de ser estática, su HTML inicial ya no trae el cuerpo del 404.

Comprobaciones: `src/lib/csp.test.ts` (la política exacta), `src/proxy.test.ts` y `scripts/check-security-headers.mjs`. Este último lo lanza la prueba de humo de la imagen (`SMOKE_CHECK`, en `make check` y en CI) contra el contenedor en ejecución: exige las cabeceras fijas, la CSP exacta y el nonce de la petición en cada `<script>` y `<style>` del HTML, en rutas reales, en la demo y en dos 404. Si cambia la política, cambian `csp.ts` y `csp.test.ts`; el script la toma de `csp.ts`.

## Demo visual (UI-01)

**Congelada desde el 2026-10-02.** `/demo` es un prototipo heredado: no recibe funcionalidades nuevas, solo el mantenimiento que lo mantenga funcionando, y se retirará con un work item propio cuando existan las pantallas oficiales equivalentes. Su estado ficticio no se copia al producto.

Prototipo navegable del diseño de Figma [GOOD DOGGY · CRM independiente y landing page](https://www.figma.com/design/nRg83fFjnBDp8EouIPvTEZ/GOOD-DOGGY-%C2%B7-CRM-independiente-y-landing-page?node-id=5-108), que es la referencia visual. Con `pnpm dev`, abrir `http://localhost:3000/demo`.

| Ruta                                | Pantalla del Figma                                                                                                                                |
| ----------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------- |
| `/demo`                             | `GD-00-Landing` (5:108), vista previa con pestañas (35:154) y acceso demo (35:266)                                                                |
| `/demo/workspace`                   | `GD-01-Resumen` (5:212); en móvil, `GD-16-Mobile` (5:2183)                                                                                        |
| `/demo/workspace/inbox`             | `GD-02-Inbox` (5:370)                                                                                                                             |
| `/demo/workspace/pipeline`          | `GD-04-Pipeline` (5:652)                                                                                                                          |
| `/demo/workspace/reportes`          | `GD-14-Reportes` (10:160)                                                                                                                         |
| `/demo/workspace/<módulo>` (tablas) | Contactos, Cotizaciones, Ventas, Tareas, Catálogo, Precios, Inventario, Agentes IA, Canales, Automatizaciones y Configuración (`GD-03` … `GD-15`) |

Límites de la demo:

- Datos ficticios definidos en `src/components/demo/data.ts`. No llama a la API, no envía mensajes y no guarda nada: los cambios (chat, etapa de una oportunidad, búsquedas) viven en memoria y se pierden al recargar.
- Sin autenticación ni permisos: el "acceso demo" solo lleva al workspace. Las rutas de tenant (`/o/[orgSlug]`) no cambian.
- Los tokens `--gd-*` solo se usan bajo `/demo`; las pantallas oficiales tienen los suyos (ver «Lenguaje visual»). DM Sans se descarga de Google Fonts durante el build (`next/font`) y se sirve desde el propio origen.
- El texto de la demo vive en los componentes, no en `messages/es-PE.json`: es contenido de prototipo, no catálogo del producto.

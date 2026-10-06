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
- **`/o`:** organizaciones del usuario (`GET /api/v1/me/organizations/`). Con una sola entra directamente; con varias, lista para elegir; con ninguna, un estado vacío. `/o?elegir` muestra siempre la lista. Cada cambio de estado se anuncia en una región viva, y al reintentar tras un error el foco va al botón nuevo o a la lista que llegó. Con el error en pantalla, volver a la pestaña o recuperar la red no pide nada: lo hace «Reintentar», y así no se pierden el foco ni un aviso del cierre de sesión.
- **Sesión terminada:** `Providers` es el único punto que trata un 401 de una lectura o de una escritura: navega a `/login?next=…` con la ruta en la que estaba. El cierre de sesión trata su propio 401 (`meta.ownSessionEnd`). Es una navegación completa, así que no queda estado en memoria.
- La lista de organizaciones es la que devuelve la API para la sesión. Elegir una no autoriza nada: lo decide la API en cada petición a `/o/[orgSlug]`.

## Workspace de una organización (F2-08, ADR-003 §5)

`/o/[orgSlug]` no pinta nada del workspace hasta que la API responde a `GET /api/v1/o/{slug}/me/`, en cada entrada: la respuesta no se guarda entre visitas (`gcTime: 0`). La URL solo selecciona: quién es el usuario ahí y qué puede hacer lo dice la API.

- **Guardia (`TenantGate`):** con la respuesta, monta el shell y deja el contexto en `useTenant()`. Sin sesión (401), `Providers` lleva a `/login` con vuelta a la misma ruta. Organización inexistente o sin membresía (404): la misma página de «no encontrada» que cualquier dirección que no existe. Cualquier otro error al entrar (organización suspendida, red, servidor): una tarjeta con el mensaje de su `code`, reintentar, cambiar de organización y cerrar sesión. Reintentar muestra el aviso de carga y, al terminar, deja el foco en el botón (si vuelve a fallar) o en el workspace (si abre). Con el workspace ya abierto, el contexto se vuelve a pedir al volver a la pestaña o al recuperar la red, si la última respuesta tiene más de 30 segundos: un 401, un 403 o un 404 lo cierran, y sigue cerrado hasta que la API vuelve a responder bien; un fallo pasajero (red, servidor) no cierra el workspace ni lo reabre tras una negativa. Con la tarjeta de error en pantalla, ni volver a la pestaña ni recuperar la red piden nada: lo hace «Reintentar». Si el navegador sabe que no hay red, la petición espera en el aviso de carga y sigue sola al volver la conexión.
- **Navegación por permisos:** `NAVIGATION` (`navigation.ts`) lista las entradas del menú y el permiso del catálogo que da sentido a cada una; `visibleItems` deja las que el usuario puede abrir. Solo se listan módulos que existen: hoy, «Inicio», «Miembros» (`users.view`), «Roles» (`roles.view`), «Sucursales» (`organization.view`) y «Equipos» (`teams.view`). Cada fase añade los suyos.
- **Es comodidad, no seguridad:** ocultar una entrada no protege nada. La API comprueba el permiso en cada petición, y una pantalla debe tratar el 403 aunque su entrada estuviera visible.
- **Roles:** se muestran como etiquetas. Nada en la interfaz decide por el nombre o el código de un rol.
- **Cambiar de organización:** enlace a `/o?elegir`.
- **Cerrar sesión:** `POST /api/v1/auth/logout/`; al responder se vacía la caché de datos y se va a `/login`. Un 401 al cerrar significa que la sesión ya no existía y se trata igual. Otro error se muestra encima de las acciones y la sesión sigue abierta; sin red el cierre falla enseguida y no queda en cola. Desde que se pulsa hasta que la página cambia el botón queda ocupado (`aria-disabled`, sin perder el foco) y una segunda pulsación no cuenta. Está en la barra lateral, en la tarjeta de error de la guardia y en `/o` (con la lista, con «sin organizaciones» y con el error; ahí sin «Cambiar de organización»). Es el mismo componente, `SessionActions`.
- **Título de la pestaña:** «Workspace · Good Doggy CRM» hasta que la API responde; después, el nombre de la organización, precedido por la sección si no es el inicio.
- **Inicio:** saluda con el nombre que da la API, muestra la organización y los roles. No hay datos de ejemplo: los módulos de negocio llegan con sus fases.

## Miembros (F2-17)

`/o/[orgSlug]/miembros` muestra quién pertenece a la organización, con su estado y sus roles: lo que devuelve `GET /api/v1/o/{slug}/members/` (F2-16), con el cliente generado.

- **Componente:** `components/members/members-list.tsx`, sobre la lista por cursor compartida (abajo). Por miembro: nombre (o el correo si no tiene), correo, roles como etiquetas, estado de la membresía y fecha de alta en la zona horaria de la aplicación. Un nombre o un correo largo se parte en varias líneas; no se recorta.
- **Paginación:** por cursor (ADR-016). «Cargar más» pide la página siguiente con el `next` de la anterior y desaparece cuando la API devuelve `null`. Desde la pulsación hasta la respuesta el botón queda ocupado (`aria-disabled`), también si el navegador sabe que no hay red y la petición espera. Al llegar la página el foco pasa a su primera fila, salvo que ya se haya ido a otra parte. Si esa página falla, lo ya cargado sigue en pantalla (salvo con un 403, que cierra la lista), el aviso aparece al lado del botón (no lo mueve) y el mismo botón reintenta; al reintentar el aviso se quita y vuelve si falla otra vez.
- **Estados:** cargando; error con «Reintentar» (el botón no se desmonta mientras reintenta, y al abrir la lista el foco va al título); y sin permiso (403), con un mensaje propio y sin reintento.
- **Negativas y sesión:** un 403 cierra la lista aunque ya estuviera en pantalla, y sigue cerrada hasta que la API vuelve a responder bien (un fallo pasajero no la reabre); si llega al reintentar o al cargar más, el foco va al título, salvo que el usuario ya lo haya llevado a otra parte. Sin sesión (401) no se muestra un error, tampoco al cargar más: `Providers` lleva al login. La lista no se guarda entre visitas (`gcTime: 0`): cada entrada pregunta a la API.
- **Navegación:** la entrada «Miembros» pide el permiso `users.view`. Es comodidad: quien abre la URL sin el permiso ve el mensaje de «sin permiso» porque la API responde 403.
- **Roles y estado:** se muestran tal como llegan. Nada decide por el nombre o el código de un rol. El estado es el de la membresía, no el de la cuenta.
- **Título de la pestaña:** «Miembros · organización · Good Doggy CRM». Lo pone el shell a partir de la entrada de navegación de la ruta.
- Invitar, dar de baja y cambiar roles son otros work items.

### Lista por cursor compartida (F2-24)

`components/lists/cursor-list.tsx` (`CursorList`) tiene lo que una pantalla de gestión necesita para listar por cursor: título, lista, «Cargar más» y los estados de carga, error con reintento, sin permiso (403) y sin sesión (401), con el comportamiento y el foco descritos arriba. La pantalla aporta la clave de la consulta, la función que pide una página, el contenido de cada fila y sus textos (`section`: el espacio de mensajes con `eyebrow`, `title`, `intro`, `loading`, `denied`, `count`, `more` y `loadingMore`). También recibe el nombre de la organización (para `intro`, que lleva `{organization}`; `count` lleva `{count}`) y, si quiere, clases para la fila (`rowClassName`). Una sección a la que le falte alguno de esos textos no compila. `notice` es un aviso de la pantalla encima de la lista; `empty`, lo que la pantalla quiere enseñar en lugar de una lista sin filas (sin él se pinta la lista vacía; el aviso, el recuento y «Cargar más» no cambian); el `ref` le permite volver a pedir la lista y llevar el foco al título mientras la pantalla está montada. La usan las pantallas de miembros, de roles, de sucursales y de equipos; las siguientes listas de gestión se construyen sobre ella.

### Suspender y reactivar (F2-21)

Cada fila ofrece «Suspender» (miembro activo) o «Reactivar» (miembro suspendido), con `PUT /api/v1/o/{slug}/members/{id}/status/` (F2-19). Componente: `components/members/member-status-action.tsx`.

- **A quién se ofrece:** a quien tiene el permiso `users.manage` según el contexto de la API. Nunca en la fila propia ni en miembros invitados o dados de baja. Es comodidad: las reglas las aplica la API (ADR-017) y un 403 se explica en la fila.
- **Confirmación:** en la propia fila, con la consecuencia («perderá el acceso… y se cerrarán sus sesiones»), asociada al grupo (`aria-describedby`). Nada se envía sin confirmar. Al abrirla el foco va a «Cancelar».
- **Lo que se confirma queda fijado al abrir.** Si la lista cambia debajo (otra persona suspendió al miembro), la pregunta y lo que se envía no se dan la vuelta. Si el miembro ya está en el estado pedido y nada se está enviando, la confirmación se cierra y se anuncia ese estado.
- **Un envío:** una marca síncrona impide que dos pulsaciones seguidas sean dos peticiones, y se suelta cuando la pantalla ya enseña el resultado, no al llegar la respuesta (F2-28); el botón queda ocupado (`aria-disabled`) hasta entonces. Sin red falla y se dice: una escritura no queda en cola.
- **Éxito:** se cancela cualquier lectura de la lista que estuviera en vuelo (traería el estado anterior a la escritura) y la fila cambia en la lista ya cargada, con el estado que respondió la API. No se vuelven a pedir sus páginas, salvo que lo cancelado fuera una lectura de la lista entera: esa se repite después. Un «Cargar más» cancelado así vuelve a quedar libre: hay que pulsarlo otra vez. El resultado se anuncia (`role="status"`) y el foco pasa a la acción nueva si el usuario no se ha ido a otra parte.
- **Errores que conservan la acción** (`PERMISSION_DENIED`, con un texto propio; `LAST_OWNER`; red; fallo del servidor): se explican en la fila, por código, y el mismo botón reintenta.
- **Pantalla desfasada** (`INVALID_TRANSITION`, `NOT_FOUND`): la lista se vuelve a pedir, y la acción, o la fila entera, puede desaparecer. Por eso el aviso («El estado de … ya había cambiado») vive en la lista (`role="alert"`), no en la fila, y el foco va al título si seguía en esa acción (o en ninguna parte). Si la nueva lectura falla, el aviso se queda y la lista no cambia. El aviso se quita al abrir otra confirmación.
- **Sin sesión (401):** no se muestra un error; `Providers` lleva al login con vuelta a la pantalla.

### Asignar y quitar roles (F2-27)

Cada fila ofrece «Roles», que abre un panel con los roles de la organización: «Asignar» los que el miembro no tiene y «Quitar» los que tiene, con `PUT` y `DELETE /api/v1/o/{slug}/members/{id}/roles/{role_id}/` (F2-25). Componente: `components/members/member-roles-action.tsx`.

- **A quién se ofrece:** a quien tiene `users.manage` y `roles.view` según el contexto de la API (el panel necesita ver los roles). Nunca en la fila propia; el estado del miembro no importa. Es comodidad: las reglas contra la escalada las aplica la API (ADR-003 §5) y un 403 se explica en el panel.
- **Dos pasos:** abrir el panel y pulsar el botón del rol. Abrir no envía nada. Conceder un rol no pide otra confirmación, tampoco el de Owner: nada en la pantalla decide por el nombre o el código de un rol, y las reglas las aplica la API. Al abrir, el foco va a «Cerrar».
- **Roles del panel:** los de la organización, todas sus páginas, pedidos al abrir y vueltos a pedir en cada apertura. No se vuelven a pedir solos (al volver a la pestaña o al recuperar la red). Si la petición falla, «Reintentar» no se desmonta mientras reintenta y, al llegar los roles, el foco pasa a «Cerrar» si seguía en ese botón o en ninguna parte. Al reabrir, la lista anterior sigue en pantalla mientras llega la nueva. Un cursor que no avanza se trata como un error, no como una lista sin fin.
- **Lo que hace cada botón queda fijado al abrir.** Los roles que el miembro tiene se anotan al abrir el panel y solo cambian con las respuestas propias: si la lista cambia debajo, ningún botón pasa de «Asignar» a «Quitar» por sí solo.
- **Un cambio cada vez**, enviado una sola vez. La marca que lo impide se suelta cuando la pantalla ya enseña el resultado, no al llegar la respuesta. Mientras tanto todos los botones del panel quedan ocupados (`aria-disabled`) y solo el pulsado lo dice (`aria-busy`). Sin red falla y se dice.
- **Una pulsación de más no deshace:** al terminar, el botón pulsado pasa a la acción contraria y conserva el foco; el segundo clic de un doble clic y un Enter mantenido se ignoran.
- **Éxito:** como al suspender, se cancela la lectura de la lista que estuviera en vuelo y los roles de la fila cambian en la lista ya cargada, por nombre y, a igualdad, por código. Ese orden es una aproximación al del directorio (la intercalación de la base); la siguiente lectura trae el exacto. El resultado se anuncia (`role="status"`).
- **Errores que conservan el panel** (`PERMISSION_DENIED`, con un texto propio; `LAST_OWNER`; red; fallo del servidor): se explican en el panel y el mismo botón reintenta.
- **Pantalla desfasada** (404: el miembro o el rol ya no existen, o ya no tiene ese rol): el panel se cierra, la lista y los roles se vuelven a pedir y lo explica el aviso de la lista, con su propio texto («Los roles de … ya habían cambiado»).
- **Sin sesión (401):** no se muestra un error; `Providers` lleva al login con vuelta a la pantalla.

## Roles (F2-23)

`/o/[orgSlug]/roles` muestra los roles de la organización: lo que devuelve `GET /api/v1/o/{slug}/roles/` (F2-22), con el cliente generado.

- **Componente:** `components/roles/roles-list.tsx`, sobre la lista por cursor compartida (ver «Miembros»): su paginación, sus estados y su foco son los mismos.
- **Por rol:** nombre, si es de plantilla o propio, descripción si la tiene, número de miembros y sus permisos, cada uno con su alcance si lo admite.
- **Nombres de los permisos:** salen de `catalog.*` en `messages/es-PE.json` (`catalog.users.manage` para `users.manage`); los alcances, de `roles.scope.*`. Son textos planos, sin formato ICU. Un permiso o un alcance que esos textos no conocen se enseña con su código: no se oculta. El código llega de la API, así que el texto se busca por propiedades propias y no como una ruta de mensajes.
- **Navegación:** la entrada «Roles» pide el permiso `roles.view`; `users.view` no basta. Es comodidad: quien abre la URL sin el permiso ve «sin permiso» porque la API responde 403.
- **Lo que se ve es lectura.** Nada decide por el nombre o el código de un rol. Crear un rol está en «Crear un rol»; renombrarlo, en «Editar un rol»; conceder y retirar sus permisos, en «Permisos de un rol»; borrarlo, en «Borrar un rol».

### Crear un rol (F2-35)

Encima de la lista, «Crear rol» abre un formulario con nombre y descripción y envía `POST /api/v1/o/{slug}/roles/` (F2-29). Componente: `components/roles/role-create.tsx`. El rol nace sin permisos.

- **A quién se ofrece:** a quien tiene `roles.manage` según el contexto de la API y además ve la lista (`roles.view`): el formulario vive sobre ella. Es comodidad: la API decide, y un 403 se explica en el formulario.
- **Abrir no envía nada.** El foco va al nombre. «Cancelar» cierra, descarta lo escrito y devuelve el foco a «Crear rol».
- **Un envío:** la misma marca síncrona de las demás escrituras; Enter, un segundo clic, «Cancelar» o seguir escribiendo mientras se envía no hacen nada, y un Enter mantenido no repite la pulsación. Sin red falla y se dice.
- **Lo que valida la pantalla:** que haya nombre, y las longitudes máximas de los campos. Lo demás lo decide la API: se envía lo escrito sin sus espacios exteriores y se anuncia el nombre que la API guardó.
- **Éxito:** el formulario se cierra, el resultado se anuncia (`role="status"`, visible) y la lista en pantalla se vuelve a pedir. El rol nuevo va al final: si hay «Cargar más», aparece al cargar la última página. Un «Cargar más» que estuviera en vuelo se cancela y hay que pulsarlo otra vez. Si esa relectura falla, el aviso se queda y la lista no cambia; si la API niega la lista (403), la pantalla se cierra con su aviso de «sin permiso». El foco vuelve a «Crear rol».
- **Errores, por `code`:** los del nombre (`ROLE_NAME_TAKEN`, o un 400 con el campo `name`) y los de la descripción se explican junto a su campo; un error del nombre lleva el foco al nombre. Sin permiso, `LAST_OWNER`, red o fallo del servidor, en el formulario. Lo escrito no se pierde, y al escribir en cualquier campo los errores se retiran hasta el siguiente envío. Nunca se enseña el texto de la respuesta.
- **Sin sesión (401):** no se muestra un error; `Providers` lleva al login con vuelta a la pantalla.

### Editar un rol (F2-39)

Cada rol editable ofrece «Editar», que abre un formulario con su nombre y su descripción y envía `PATCH /api/v1/o/{slug}/roles/{id}/` (F2-38). Componente: `components/roles/role-edit-action.tsx`, hermano de «Crear rol»: los mismos campos y textos, y las mismas reglas de envío, foco y errores.

- **A quién se ofrece:** a quien tiene `roles.manage` según el contexto de la API, y solo en los roles que la API marca `editable`. Una plantilla que no sea el Owner se puede renombrar: lo decide `editable`, no `is_system`. Es comodidad: la API exige además cubrir todos los permisos del rol, y un 403 se explica en el formulario.
- **Lo que se edita queda fijado al abrir**: si la lista cambia debajo, el formulario sigue con lo que el usuario abrió. «Cancelar» descarta lo escrito.
- **Un envío:** Enter, un segundo clic, «Cancelar» o seguir escribiendo mientras se envía no hacen nada, y un Enter mantenido no repite la pulsación. Se envían siempre los dos campos, sin sus espacios exteriores; la API no escribe lo que no cambia.
- **Éxito:** se cancela la lectura de la lista que estuviera en vuelo y la tarjeta enseña el nombre y la descripción que guardó la API, sin volver a pedir las páginas; lo demás de la fila no se toca (la respuesta puede ser anterior a un cambio de «Permisos» hecho mientras tanto). Si lo cancelado era la lista entera, se repite después; un «Cargar más» cancelado hay que pulsarlo otra vez. El resultado se anuncia (`role="status"`, no visible) y el foco vuelve a «Editar», salvo que el usuario ya esté en otra parte. Si la API responde con otro rol, no se da por guardado: se explica como un fallo nuestro y la lista se vuelve a pedir.
- **Errores, por `code`:** los del nombre (`ROLE_NAME_TAKEN`, o un 400 con el campo `name`) y los de la descripción, junto a su campo; `PERMISSION_DENIED` (con un texto propio: hace falta cubrir los permisos del rol y, si alguno es sensible, ser Owner, y el rol no puede ser uno que quien mira tenga asignado; la API no dice cuál de las reglas fue), `LAST_OWNER`, red o fallo del servidor, en el formulario. Lo escrito no se pierde, y al escribir en cualquier campo los errores se retiran hasta el siguiente envío.
- **Pantalla desfasada (404):** el rol ya no existe. El formulario se cierra, la lista se vuelve a pedir y lo explica un aviso de la lista; el foco va al título si seguía en la tarjeta del rol (el formulario o su panel «Permisos») o en ninguna parte.
- **Sin sesión (401):** no se muestra un error; `Providers` lleva al login con vuelta a la pantalla.
- **Límites conocidos:** se envían los dos campos con lo fijado al abrir: el que no se toca pisa lo que otra persona hubiera cambiado en él mientras tanto. Si una relectura de la lista trae el rol como no editable con el formulario abierto, el formulario se desmonta sin aviso: lo escrito se pierde, el foco se queda sin destino y un envío que estuviera en vuelo cambia la tarjeta pero no se anuncia.

### Borrar un rol (F2-40)

Cada rol editable que no es de plantilla ofrece «Borrar», con `DELETE /api/v1/o/{slug}/roles/{id}/` (F2-38). Componente: `components/roles/role-delete-action.tsx`. No se puede deshacer: pide confirmación en la tarjeta, como «Suspender» en la pantalla de miembros.

- **A quién se ofrece:** a quien tiene `roles.manage` según el contexto de la API, en los roles que la API marca `editable` y que no son de plantilla (`is_system`: la API no borra una plantilla). Un rol con miembros también la ofrece: la API responde que no y la tarjeta lo explica. Es comodidad: la API exige además cubrir todos los permisos del rol.
- **Confirmación:** en la propia tarjeta, con la consecuencia, asociada al grupo (`aria-describedby`). Nada se envía sin confirmar. Al abrirla el foco va a «Cancelar». El rol que se nombra queda fijado al abrir.
- **Un envío:** la misma marca síncrona de las demás escrituras; tras el éxito la confirmación sigue ocupada hasta que la tarjeta desaparece. Sin red falla y se dice.
- **Éxito:** se cancela la lectura de la lista que estuviera en vuelo y la tarjeta se quita de la lista ya cargada. Como la tarjeta ya no está, el resultado lo anuncia la lista (`role="status"`, visible) y el foco va al título si seguía en la tarjeta o en ninguna parte. El anuncio se quita al abrir otra acción de la lista, también «Crear rol».
- **Errores, por `code`, en la tarjeta:** `PERMISSION_DENIED` (con un texto propio), `ROLE_IN_USE`, `ROLE_IS_SYSTEM`, `LAST_OWNER`, red o fallo del servidor. El mismo botón reintenta. Con `ROLE_IN_USE` la lista se vuelve a pedir además: la tarjeta podía decir «Sin miembros».
- **Pantalla desfasada (404):** el rol ya no existía. La confirmación se cierra, la lista se vuelve a pedir y lo explica un aviso de la lista (`role="alert"`).
- **Sin sesión (401):** no se muestra un error; `Providers` lleva al login con vuelta a la pantalla.
- **Límites conocidos:** un «Cargar más» cancelado al borrar hay que pulsarlo otra vez; una lectura de la lista entera cancelada se repite después. Si una relectura trae el rol como no editable con la confirmación abierta, la confirmación se desmonta sin aviso y el foco se queda sin destino; de un envío que estuviera en vuelo, el borrado se anuncia igual en la lista, pero un error que no sea el 404 no se enseña. De dos resultados seguidos (un borrado y un aviso de pantalla desfasada) solo se ve el último. El aviso de «Crear un rol» no se quita al borrar ese rol: los dos textos quedan a la vista.

### Permisos de un rol (F2-36)

Cada rol editable ofrece «Permisos», que abre un panel con el catálogo (`GET /api/v1/o/{slug}/permissions/`, F2-34): «Conceder» los que el rol no tiene y «Retirar» los que tiene, con `PUT` y `DELETE /api/v1/o/{slug}/roles/{id}/permissions/{code}/` (F2-31, F2-33). Componente: `components/roles/role-permissions-action.tsx`, hermano del panel «Roles» de la pantalla de miembros: mismas reglas de envío, foco y errores.

- **A quién se ofrece:** a quien tiene `roles.manage` según el contexto de la API, y solo en los roles que la API marca `editable`: no el rol Owner ni uno que tenga asignado quien mira (no confundir con la etiqueta «Propio» de la tarjeta, que es un rol que no es de plantilla: esos sí se editan). Lo decide `editable`, no `is_system`. Es comodidad: las reglas contra la escalada las aplica la API (ADR-003 §5), y un 403 se explica en el panel.
- **A quién alcanza:** el panel dice cuántos miembros tiene el rol. Lo que se cambia les llega a todos en su siguiente petición.
- **Catálogo:** se pide al abrir y en cada apertura; al reabrir se enseña el de la vez anterior hasta que llega el nuevo. Los nombres salen de `catalog.*` en los textos; un permiso sin texto se enseña con su código. Los sensibles llevan la marca «Sensible».
- **El código va codificado en la ruta** (F2-37): el cliente generado lo pone tal cual, y es texto que llega de la API. Un código hecho solo de puntos no se envía y se explica como un fallo nuestro.
- **Permisos con alcance:** se enseñan con su alcance, o como «Sin conceder», sin botón. Elegir el alcance llegará con el primer permiso del catálogo que lo use.
- **Lo que hace cada botón queda fijado al abrir**, y solo cambia con las respuestas propias. Un cambio cada vez, enviado una sola vez; el segundo clic de un doble clic y un Enter mantenido se ignoran.
- **Éxito:** se cancela la lectura de la lista que estuviera en vuelo y los permisos de la tarjeta cambian en la lista ya cargada. El resultado se anuncia y el botón, ya con la acción contraria, conserva el foco.
- **Errores, por `code`:** `PERMISSION_DENIED` (con un texto propio: hace falta tener el permiso y, si es sensible, ser Owner, y el rol no puede ser uno que quien mira tenga asignado; la API no dice cuál de las reglas fue), `LAST_OWNER`, red o fallo del servidor se explican en el panel y el mismo botón reintenta.
- **Pantalla desfasada (404):** el rol ya no existe o ya no tiene esa concesión. El panel se cierra, la lista se vuelve a pedir y lo explica un aviso de la lista (`role="alert"`); el foco va al título si seguía en el panel.
- **Sin sesión (401):** no se muestra un error; `Providers` lleva al login con vuelta a la pantalla.
- **Límite conocido:** si una relectura de la lista trae el rol como no editable con su panel abierto, el panel se desmonta sin aviso: el foco se queda sin destino y un cambio que estuviera en vuelo cambia la tarjeta pero no se anuncia.

## Sucursales (F2-46)

`/o/[orgSlug]/sucursales` muestra las sucursales de la organización: lo que devuelve `GET /api/v1/o/{slug}/branches/` (F2-43), con el cliente generado.

- **Componente:** `components/branches/branches-list.tsx`, sobre la lista por cursor compartida (ver «Miembros»): su paginación, sus estados y su foco son los mismos.
- **Por sucursal:** nombre, código, dirección (calle, distrito y ciudad: lo que haya, en una línea; «Sin dirección» si no hay nada), teléfono si lo tiene, zona horaria y si está activa. Se listan también las inactivas.
- **Sin sucursales:** la pantalla lo dice en lugar de enseñar una lista vacía. Es el `empty` de la lista compartida (ver «Lista por cursor compartida»); lo usan esta pantalla y la de equipos: una organización siempre tiene miembros y roles.
- **Navegación:** la entrada «Sucursales» pide el permiso `organization.view`, el que exige la API para leerlas; `branches.manage` solo no basta. Es comodidad: quien abre la URL sin el permiso ve «sin permiso» porque la API responde 403.
- **Escrituras:** crear una sucursal está en «Crear una sucursal» y corregir sus datos, en «Editar una sucursal»; desactivarla y reactivarla, en «Desactivar y reactivar una sucursal». No hay borrado.
- **Aviso de la lista:** una acción que descubre que la sucursal ya no existe lo explica aquí, encima de la lista (`role="alert"`), y la lista se vuelve a pedir. Abrir «Crear sucursal», «Editar» o una confirmación lo retira.

### Crear una sucursal (F2-47)

Encima de la lista, «Crear sucursal» abre un formulario y envía `POST /api/v1/o/{slug}/branches/` (F2-44). Componente: `components/branches/branch-create.tsx`, sobre el formulario de campos compartido (abajo): el camino de escritura es el mismo que en `role-create.tsx`.

- **A quién se ofrece:** a quien tiene `branches.manage` según el contexto de la API y además ve la lista (`organization.view`): el formulario vive sobre ella, también cuando no hay ninguna sucursal. Es comodidad: la API decide, y un 403 se explica en el formulario.
- **Campos:** código y nombre, obligatorios; dirección, distrito, ciudad y teléfono, opcionales. Cada uno con el límite de la API. La zona horaria no se pide: la API pone `America/Lima` y el formulario lo dice.
- **Abrir no envía nada.** El foco va al código. «Cancelar» cierra, descarta lo escrito y devuelve el foco a «Crear sucursal».
- **Un envío:** la misma marca síncrona de las demás escrituras; Enter, un segundo clic, «Cancelar» o seguir escribiendo mientras se envía no hacen nada, y un Enter mantenido no repite la pulsación. Sin red falla y se dice.
- **Lo que valida la pantalla:** que haya código y nombre, y las longitudes máximas. Lo demás lo decide la API: se envía lo escrito sin sus espacios exteriores (el código, tal como se escribió: la API lo pasa a mayúsculas) y se anuncia el nombre que la API guardó.
- **Éxito:** el formulario se cierra, el resultado se anuncia (`role="status"`, visible) y la lista en pantalla se vuelve a pedir. La sucursal nueva va al final: si hay «Cargar más», aparece al cargar la última página. Un «Cargar más» que estuviera en vuelo se cancela y hay que pulsarlo otra vez. Si esa relectura falla, el aviso se queda y la lista no cambia; si la API niega la lista (403), la pantalla se cierra con su aviso de «sin permiso» y el formulario deja de ofrecerse. El foco vuelve a «Crear sucursal», salvo que el usuario ya esté en otra parte.
- **Errores, por `code`:** el código repetido (`BRANCH_CODE_TAKEN`) y cada campo que la API no acepta (un 400 con ese campo en `fields`) se explican junto a su campo, y el foco va al primero si seguía en el botón o en ninguna parte. Sin permiso, red o fallo del servidor, en el formulario; un 400 sin un campo del formulario se trata como un fallo nuestro. Lo escrito no se pierde, y al escribir en cualquier campo los errores se retiran hasta el siguiente envío. Nunca se enseña el texto de la respuesta.
- **Sin sesión (401):** no se muestra un error; `Providers` lleva al login con vuelta a la pantalla.

### Editar una sucursal (F2-49)

En cada tarjeta, «Editar» abre un formulario con los datos de la sucursal y envía `PATCH /api/v1/o/{slug}/branches/{id}/` (F2-45). Componente: `components/branches/branch-edit-action.tsx`, sobre el formulario de campos compartido; lo propio de editar una fila sigue a `role-edit-action.tsx`.

- **A quién se ofrece:** a quien tiene `branches.manage` según el contexto de la API, en todas las sucursales, activas o no. Es comodidad: la API decide, y un 403 se explica en el formulario.
- **Campos:** nombre, dirección, distrito, ciudad, teléfono y zona horaria. El código no se edita ni se envía, y el estado tampoco.
- **Zona horaria:** un campo de texto, obligatorio, con las zonas que conoce el navegador como sugerencias (`<datalist>`, con `UTC` añadida). La API decide si la zona existe: si no, lo dice junto al campo.
- **Lo que se edita queda fijado al abrir:** si la lista cambia debajo, el formulario sigue enseñando lo que el usuario abrió. «Cancelar» descarta lo escrito y devuelve el foco a «Editar».
- **Se envían los seis campos**, sin sus espacios exteriores, con lo fijado al abrir en los que no se tocan: pisan lo que otra persona hubiera cambiado en ellos mientras tanto (límite conocido, como en «Editar un rol»). La API cambia y audita solo lo que es distinto de lo guardado.
- **Éxito:** el formulario se cierra, la tarjeta enseña en esos seis campos lo que guardó la API sin volver a pedir la lista, y el resultado se anuncia (`role="status"`, solo para lector de pantalla: la tarjeta ya lo enseña). Una lectura de la lista en vuelo se cancela para que no pise lo guardado; si era la lista entera se repite, y un «Cargar más» hay que pulsarlo otra vez. Si la API responde con otra sucursal, no se da por guardado: se explica como un fallo y la lista se vuelve a pedir.
- **Errores, por `code`:** cada campo que la API no acepta, junto a él; sin permiso, red o fallo del servidor, en el formulario. Un 404 es pantalla desfasada: el formulario se cierra, lo explica el aviso de la lista y el foco va al título si seguía en la tarjeta de la sucursal o en ninguna parte.
- **Sin sesión (401):** no se muestra un error; `Providers` lleva al login con vuelta a la pantalla.

### Desactivar y reactivar una sucursal (F2-51)

Junto a «Editar», cada tarjeta ofrece «Desactivar» si la sucursal está activa y «Reactivar» si no, y envía `PATCH /api/v1/o/{slug}/branches/{id}/` con `{"is_active": false | true}` (F2-45). Componente: `components/branches/branch-status-action.tsx`, hermano de la acción de estado de un miembro (ver «Suspender y reactivar»): confirmación, envío, foco y errores son los mismos.

- **A quién se ofrece:** a quien tiene `branches.manage` según el contexto de la API. Es comodidad: la API decide.
- **Pide confirmación en la tarjeta** y dice qué pasa: una sucursal inactiva sigue en la lista y se puede reactivar. El foco va a «Cancelar». Nada se envía sin confirmar, y lo que se confirma queda fijado al abrir.
- **Solo viaja `is_active`.** Tras el éxito la tarjeta enseña el estado que respondió la API (solo el estado: los demás datos pudo cambiarlos «Editar» mientras tanto), sin volver a pedir la lista, y se anuncia (`role="status"`). Una lectura en vuelo se cancela para que no pise el cambio.
- **Si otro lo hizo antes** con la confirmación abierta, la confirmación se cierra y se anuncia el estado: no hay nada que confirmar.
- **Errores, por `code`, en la tarjeta:** sin permiso, red y fallo del servidor; el mismo botón reintenta. Un 404 es pantalla desfasada: la confirmación se cierra, lo explica el aviso de la lista y el foco va al título si seguía en la tarjeta.
- **Desactivar no tiene más efecto todavía:** nada depende de una sucursal (ni membresías, ni almacenes, ni pedidos).

### Formulario de campos compartido (F2-48)

`components/forms/fields-form.tsx` (`FieldsForm`) tiene el camino de envío de un formulario de campos de texto, el que describen «Crear un rol» y «Crear una sucursal». Hoy lo usan «Crear sucursal», «Editar sucursal», «Crear equipo» y «Editar equipo»; los formularios de roles conservan su copia.

- **Lo que hace:** pinta los campos, «Cancelar» y el botón de envío; comprueba los obligatorios; envía una vez por pulsación; explica los errores de la API por `code` (los de un campo, junto a él; los demás, en el formulario; un 401, ocupado y sin error); y mueve el foco al primer campo al abrir y al primer campo con error si el foco seguía en el botón o en ninguna parte.
- **Lo que pone la pantalla:** la escritura (el resultado de `useMutation`), qué hacer con lo escrito (`send`, que recibe los valores sin espacios exteriores), los campos con sus textos ya resueltos (etiqueta, límite, ayuda, aviso de obligatorio y de no válido) y, si hace falta, su valor inicial y el `id` de una lista de sugerencias, el error propio de un campo que no es un 400 (`taken`: un código repetido), y el texto de «sin permiso». Abrir y cerrar, el botón que abre, el anuncio del resultado, lo que pasa con la lista y la guarda del Enter mantenido siguen en la pantalla.
- **La marca de envío vive en el formulario** y se suelta en un efecto de maquetación cuando la escritura ya no está en curso. Un render solo del formulario (por ejemplo, al retirar un aviso de «falta» justo antes de enviar) trae la escritura tal como era antes de enviar y no la suelta: se compara con la que había al enviar. Por eso `write` debe ser el resultado de `useMutation` del render de la pantalla, no un objeto guardado, y `send` debe iniciar esa escritura antes de volver (`mutate`, sin esperar a nada): si no la inicia, el formulario deja de responder —también «Cancelar»— hasta el siguiente render de la pantalla; si la inicia más tarde, un render de la pantalla entre medias suelta la marca y otra pulsación sería otra escritura.
- **El formulario se monta al abrir y se desmonta al cerrar:** los avisos y lo escrito no sobreviven a un cierre.

## Equipos (F2-60 a F2-66)

`/o/[orgSlug]/equipos` muestra los equipos de la organización: lo que devuelve `GET /api/v1/o/{slug}/teams/` (F2-50), con el cliente generado.

- **Componente:** `components/teams/teams-list.tsx`, sobre la lista por cursor compartida (ver «Miembros»): su paginación, sus estados y su foco son los mismos.
- **Por equipo:** nombre, `slug`, descripción («Sin descripción» si no tiene o son solo espacios), cómo se asignan sus conversaciones y si está activo. Se listan también los inactivos.
- **Asignación:** cada estrategia de la API tiene su nombre en `messages/es-PE.json` (`teams.strategy.*`). Una que esta versión no conozca se enseña con su código, no como un error. Solo cuentan las claves propias del catálogo de textos, como con los códigos de error de la API. Si el contrato gana una estrategia y el catálogo no, el frontend no compila. Todavía no la aplica nada (Inbox, Fase 6): la pantalla solo la enseña.
- **Sin equipos:** la pantalla lo dice en lugar de enseñar una lista vacía.
- **Navegación:** la entrada «Equipos» pide el permiso `teams.view`, el que exige la API para leerlos; `teams.manage` o `users.view` solos no bastan. Es comodidad: quien abre la URL sin el permiso ve «sin permiso» porque la API responde 403.
- **Lo que falta.** Cambiar el papel de un integrante desde la pantalla es el siguiente work item; la API ya lo permite (F2-58).

### Crear un equipo (F2-61)

Encima de la lista, «Crear equipo» abre un formulario y envía `POST /api/v1/o/{slug}/teams/` (F2-53). Componente: `components/teams/team-create.tsx`, sobre el formulario de campos compartido (ver «Sucursales»): el camino de escritura, el foco y los errores son los de «Crear una sucursal».

- **A quién se ofrece:** a quien tiene `teams.manage` según el contexto de la API y además ve la lista (`teams.view`): el formulario vive sobre ella, también cuando no hay ningún equipo. Es comodidad: la API decide, y un 403 se explica en el formulario.
- **Campos:** identificador (el `slug`: letras de la a a la z, cifras y guiones entre ellas; la API no admite ñ ni tildes) y nombre, obligatorios; descripción, opcional. Cada uno con el límite de la API. La forma de asignar no se pide: la API pone la manual y el formulario lo dice.
- **Lo que valida la pantalla:** que haya identificador y nombre, y las longitudes máximas. Lo demás lo decide la API: se envía lo escrito sin sus espacios exteriores (el identificador, tal como se escribió: la API lo pasa a minúsculas) y se anuncia el nombre que la API guardó.
- **Éxito:** el formulario se cierra, el resultado se anuncia (`role="status"`, visible) y la lista en pantalla se vuelve a pedir. El equipo nuevo va al final y nace sin integrantes. Si esa relectura falla, el aviso se queda y la lista no cambia, como en sucursales.
- **Errores, por `code`:** el identificador repetido (`TEAM_SLUG_TAKEN`) y cada campo que la API no acepta se explican junto a su campo. Sin permiso, red o fallo del servidor, en el formulario. Sin sesión (401), `Providers` lleva al login.

### Editar un equipo (F2-62)

Cada tarjeta ofrece «Editar», que abre un formulario en la propia tarjeta y envía `PATCH /api/v1/o/{slug}/teams/{team_id}/` (F2-54). Componente: `components/teams/team-edit-action.tsx`, sobre el formulario de campos compartido: el camino de escritura, el foco, lo que pasa con la lista y los errores son los de «Editar una sucursal».

- **A quién se ofrece:** a quien tiene `teams.manage` según el contexto de la API, en todos los equipos, también los inactivos. Es comodidad: la API decide, y un 403 se explica en el formulario.
- **Campos:** nombre, obligatorio, y descripción, opcional, con los límites de la API. El identificador no cambia y no se enseña como campo.
- **La forma de asignar no se edita desde la pantalla.** Nada la aplica hasta el Inbox (Fase 6), y la pantalla no ofrece un ajuste sin efecto. La API ya admite cambiarla.
- **Lo que se edita queda fijado al abrir:** si la lista cambia debajo, el formulario sigue enseñando lo que el usuario abrió. Los dos campos viajan siempre, con lo que había al abrir: si otra persona cambió la descripción mientras tanto y aquí solo se cambia el nombre, se guarda la descripción de antes.
- **Éxito:** el formulario se cierra y la tarjeta enseña el nombre y la descripción que guardó la API, sin volver a pedir la lista; lo demás de la fila no se toca. El resultado se anuncia (`role="status"`).
- **Pantalla desfasada (404):** el formulario se cierra, la lista se vuelve a pedir y un aviso de la lista lo explica («No se pudo guardar el equipo…: ya no está disponible»). No dice que el equipo se borró: no hay borrado de equipos, y la API responde igual si lo que dejó de estar al alcance es la organización o la membresía. Abrir «Editar», «Desactivar» o «Reactivar» (F2-63), «Integrantes» (F2-64) o «Crear equipo» retira ese aviso; cancelar otro formulario, no.
- **«Editar» se distingue por equipo:** su nombre accesible lleva el nombre y el identificador («Editar el equipo Ventas (ventas)»), porque dos equipos pueden llamarse igual.

### Desactivar y reactivar un equipo (F2-63)

Cada tarjeta ofrece «Desactivar» o «Reactivar», lo contrario de su estado, que envía `PATCH /api/v1/o/{slug}/teams/{team_id}/` con `is_active` (F2-54). Componente: `components/teams/team-status-action.tsx`, un port de «Desactivar y reactivar una sucursal»: el camino de escritura, el foco, lo que pasa con la lista y los errores son los mismos.

- **A quién se ofrece:** a quien tiene `teams.manage` según el contexto de la API. Es comodidad: la API decide, y un 403 se explica en la confirmación.
- **Pide confirmación en la tarjeta** y dice la consecuencia: el equipo sigue en la lista como inactivo, con sus integrantes, y se puede reactivar. El foco va a «Cancelar», la opción que no cambia nada. Nada se envía sin confirmar.
- **Lo que hace desactivar hoy:** cambia el estado que enseña la lista y deja su fila de auditoría (`team.updated`), y nada más. Los integrantes siguen en el equipo y cuenta igual para el alcance `TEAM` (F2-52).
- **Lo que se confirma queda fijado al abrir.** Si otra persona hizo el mismo cambio con la confirmación abierta, esta se cierra y lo anuncia.
- **Éxito:** la tarjeta enseña el estado que respondió la API, sin volver a pedir la lista; solo el estado, lo demás de la fila no se toca. El resultado se anuncia (`role="status"`).
- **Se distingue por equipo:** el nombre accesible lleva el nombre y el identificador («Desactivar el equipo Ventas (ventas)»).
- **Pantalla desfasada (404):** la confirmación se cierra, la lista se vuelve a pedir y lo explica el aviso de la lista, en el sitio del de «Editar» y con su texto («No se pudo cambiar el equipo…: ya no está disponible»). Se retira igual que aquel.

### Integrantes de un equipo (F2-64)

Cada tarjeta ofrece «Integrantes», que abre un panel en la propia tarjeta con lo que devuelve `GET /api/v1/o/{slug}/teams/{team_id}/members/` (F2-55). Componente: `components/teams/team-members-panel.tsx`. El panel es de lectura; quien administra equipos tiene además «Incorporar integrante» y «Quitar» (abajo).

- **A quién se ofrece:** a quien tiene `teams.view` y `users.view` según el contexto de la API, los dos que exige la ruta: enseña personas. No hace falta `teams.manage`. Es comodidad: la API decide, y un 403 se explica en el panel, sin reintento.
- **Nada se pide hasta abrir el panel**, y cada apertura vuelve a preguntar: la lectura vive en un componente que solo existe con el panel abierto, así que al cerrarlo se cancela y se olvida, y al reabrir no se enseñan los integrantes de la vez anterior.
- **Se piden todas las páginas** (de 200 en 200) antes de enseñar nada. Un cursor que la API repite es un fallo, no una lista sin fin.
- **Por integrante:** nombre y correo (solo el correo si no tiene nombre), su papel en el equipo («Integrante» o «Supervisor») y el estado de su membresía en la organización si no es «Activo» («Membresía: Suspendido»). Un papel o un estado que esta versión no conozca se enseña con su código. Debajo, cuántos son; un equipo sin integrantes lo dice.
- **Lo que no enseña:** si el integrante participa en la asignación automática (`is_active`). Nada la aplica todavía (Inbox, Fase 6).
- **Errores:** un fallo del servidor o de red se explica con «Reintentar», después del reintento automático de toda lectura. Sin sesión (401), `Providers` lleva al login.
- **Pantalla desfasada (404):** el panel se cierra, la lista se vuelve a pedir y lo explica el aviso de la lista, con su texto («No se pudieron ver los integrantes del equipo…»). Se retira como los de «Editar» y «Desactivar».
- **Foco:** al abrir, a «Cerrar»; al cerrar, a «Integrantes»; si «Reintentar» desaparece porque llegó la lista o una negativa, a «Cerrar»; con un 404, al título si seguía en la tarjeta; nunca se le quita a quien ya está en otra parte. El nombre accesible lleva el nombre y el identificador del equipo.

### Incorporar a un integrante (F2-65)

Dentro del panel de integrantes, «Incorporar integrante» abre la lista de miembros de la organización que aún no están en el equipo; «Incorporar», junto a cada uno, envía `PUT /api/v1/o/{slug}/teams/{team_id}/members/{membership_id}/` (F2-58) con el cuerpo vacío. Componente: `components/teams/team-member-add.tsx`.

- **A quién se ofrece:** a quien tiene `teams.manage` según el contexto de la API, además de los dos permisos del panel. Es comodidad: la API decide, y un 403 se explica en la lista.
- **Candidatos:** todos los miembros de la organización (`GET …/members/`, todas las páginas, pedidas al abrir la lista y de nuevo en cada apertura), menos quien ya está en el equipo, quien tiene la membresía dada de baja y uno mismo. La API no deja que nadie se incorpore a sí mismo, y la lista lo dice a quien no está en el equipo. Sin búsqueda ni filtro. Cada candidato se enseña con su nombre y su correo (solo el correo si no tiene nombre), y el nombre accesible de «Incorporar» lleva los dos: dos personas pueden llamarse igual.
- **Entra con lo que pone la API** (integrante, activo): no se elige el papel. El panel enseña el papel que respondió la API.
- **Una escritura cada vez**, enviada una sola vez: la misma marca síncrona de las demás escrituras; mientras se envía, los demás botones y «Listo» no hacen nada. Sin red falla y se dice.
- **Éxito:** el integrante nuevo aparece al final del panel con lo que respondió la API, sin volver a pedir los integrantes; su fila se queda en la lista, sin botón («Ya está en el equipo»), hasta cerrarla, para que la persona siguiente no suba bajo el puntero; el resultado se anuncia (`role="status"`, visible). La lista sigue abierta para incorporar a más, y el foco pasa a «Listo».
- **Errores, por `code`:** sin permiso, red y fallo del servidor, bajo la lista; el mismo botón reintenta. Sin sesión (401), `Providers` lleva al login. Si lo que falla es la lectura de los miembros, se explica con «Reintentar»; al pulsarlo el foco pasa a «Listo», porque el botón desaparece mientras se vuelve a pedir.
- **Pantalla desfasada (404):** el equipo o la persona ya no están al alcance. El panel se cierra, la lista de equipos se vuelve a pedir y lo explica el aviso de la lista, con la persona y el equipo.
- **Si se cierra el panel con una incorporación en vuelo**, la escritura sigue su curso en la API. Si sale bien no se anuncia y el panel reabierto la enseña: si su lectura seguía en vuelo al llegar la respuesta, se cancela y se vuelve a pedir. Si falla, solo un 404 avisa (en la lista, como siempre); cualquier otro fallo no se dice, y el panel reabierto enseña que la persona no entró.

### Quitar a un integrante (F2-66)

En el panel de integrantes, cada fila ofrece «Quitar», que pide confirmación en la propia fila y envía `DELETE /api/v1/o/{slug}/teams/{team_id}/members/{membership_id}/` (F2-59). Componente: `components/teams/team-member-remove.tsx`. La confirmación sigue a «Desactivar un equipo»; la escritura sobre la lectura del panel, a «Incorporar a un integrante».

- **A quién se ofrece:** a quien tiene `teams.manage` según el contexto de la API, en cada integrante menos en uno mismo: la API no deja que nadie se quite a sí mismo. Es comodidad: la API decide, y un 403 se explica en la fila.
- **Pide confirmación** y dice la consecuencia: la persona sigue siendo miembro de la organización. El foco va a «Cancelar». Nada se envía sin confirmar; un envío por pulsación; sin red falla y se dice.
- **A quién:** el nombre accesible de «Quitar» y de la confirmación lleva el nombre y el correo, porque dos personas pueden llamarse igual.
- **Éxito:** la fila deja el panel y el recuento baja, sin volver a pedir los integrantes; lo anuncia el panel (`role="status"`, visible), porque la fila ya no está; el foco pasa a «Cerrar». Quien salió vuelve a ser candidato en «Incorporar integrante».
- **Errores, por `code`:** sin permiso, red y fallo del servidor, en la fila; el mismo botón reintenta. Sin sesión (401), `Providers` lleva al login.
- **Pantalla desfasada (404):** la persona ya no estaba en el equipo, o el equipo ya no está al alcance. El panel se cierra, la lista de equipos se vuelve a pedir y lo explica el aviso de la lista, con la persona y el equipo.
- **Si se cierra el panel con un borrado en vuelo**, la escritura sigue su curso; al reabrir, el panel pregunta de nuevo, y una lectura que salió antes de la respuesta no devuelve a quien se quitó. Si falla, nadie lo dice.

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

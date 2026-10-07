# ADR-021: Recuperación de contraseña

- **Status:** Accepted
- **Date:** 2026-10-07
- **Deciders:** programa autónomo (ADR-015 §5). El mantenedor puede reemplazarlo.
- **Related:** ADR-001 §2, ADR-003, ADR-013, ADR-014, ADR-019, E01-02 en [05-backlog-y-roadmap.md](../fase-0/05-backlog-y-roadmap.md), el catálogo de auditoría en [04-precios-pipeline-seguridad-infra.md](../fase-0/04-precios-pipeline-seguridad-infra.md) §N.2

## Context

Hoy la contraseña de una cuenta la pone un comando de operador, y quien la olvida depende de un operador para volver a entrar. E01-02 pide que la persona la recupere por correo, con un enlace de un solo uso que caduque. El correo ya tiene por dónde salir (ADR-019), y ese ADR ya fija que el enlace lo genera la tarea que lo envía y que solo se guarda su hash.

Faltaban las reglas del flujo. Son decisiones de seguridad: un formulario público que acepta un correo sirve para averiguar quién tiene cuenta, para llenar un buzón ajeno y para probar enlaces; y un enlace que cambia una contraseña es, mientras vale, la llave de la cuenta.

A diferencia de las invitaciones (ADR-020), este flujo es entero de plataforma: una cuenta no pertenece a ninguna organización. No abre ningún `tenant_scope`, no toca tablas de tenant y no necesita excepción a ADR-014 §4.

Este ADR fija las reglas de toda la serie. Cada work item implementa una parte.

## Decision

### 1. Dos pasos, dos rutas de plataforma

Pedir el enlace y cambiar la contraseña con él. Las dos rutas son de plataforma y de acceso anónimo (ADR-014 §4): quien las usa no tiene sesión. Las dos exigen CSRF, como toda mutación (ADR-014 §2).

El estado vive en `password_resets`, una tabla platform-owned de `apps.accounts` (ADR-001 §2: sin `organization_id` ni política de tenant, como `users` y `login_throttles`). Una fila es un enlace emitido, se haya entregado o no (§3): la cuenta, el SHA-256 del enlace, cuándo se emitió, cuándo caduca y cuándo se usó. Nunca el enlace.

### 2. Pedir el enlace

La ruta recibe un correo. **Responde lo mismo, y hace el mismo trabajo, exista o no la cuenta**: no busca la cuenta. Comprueba la forma del correo (lo que no es un correo es un 400, que no dice nada de ninguna cuenta), cuenta el intento, deja la fila de auditoría (§7) y encola la tarea que decide y envía (§3). El correo no se envía dentro de la petición (ADR-019 §3), así que el tiempo de respuesta tampoco depende del servidor de correo.

Los intentos se limitan después de comprobar la forma y antes de hacer nada más (ni la fila de auditoría ni la tarea), por dirección de red y por identificador presentado, exista o no la cuenta, y el rechazo es el mismo 429 `RATE_LIMITED` (ADR-014 §3). Los contadores son propios de este flujo: pedir enlaces para una cuenta no bloquea el acceso de su dueña, y fallar accesos no impide pedir un enlace.

La tarea recibe el correo en su forma canónica (D-F2-3). Ese correo pasa por el broker hasta que la tarea lo consume: no es un secreto, pero es un dato personal; no se guarda como resultado de la tarea ni se escribe en el log.

**Esto se aparta de ADR-019 §3 y lo enmienda para este flujo.** ADR-019 §3 pide que quien necesita un correo registre el hecho en su transacción (una fila propia o un evento del outbox) y que la tarea lo envíe después del commit. Aquí no hay ninguna de las dos cosas: `outbox_events` es de tenant y este flujo no tiene organización, y una fila por petición guardaría correos de personas que no son usuarias (ver las alternativas). La petición encola la tarea directamente, después de dejar su fila de auditoría. Lo que se pierde es la garantía transaccional: si el broker no acepta la tarea, la petición falla, con el mismo error para cualquier correo; y si el worker muere con la tarea en curso, ese enlace no sale y la persona vuelve a pedirlo. La enmienda tiene una segunda parte: ADR-019 §3 da por hecho que la tarea reintenta tras un fallo de entrega, y la de este flujo no lo hace (§3 de este ADR). El resto de ADR-019 se cumple sin cambios.

### 3. Quién recibe correo, y cuántos

La tarea busca la cuenta. **No envía nada, y no deja rastro que la respuesta enseñe**, si el correo no tiene cuenta, si la cuenta está desactivada o si es de personal de plataforma (§6).

Una cuenta recibe como mucho 5 enlaces en 24 horas, con al menos 2 minutos entre dos. Se cuentan sobre la tabla; pasado el tope, la tarea no envía. Así nadie llena el buzón de otra persona desde muchas direcciones de red. La tarea cuenta y escribe con la fila de la cuenta bloqueada: dos peticiones simultáneas para la misma cuenta no pasan el tope ni dejan dos enlaces a menos de 2 minutos.

La fila se escribe antes de entregar el correo (ADR-019 §4), así que **cuenta todo enlace emitido, se haya entregado o no**: un fallo de entrega no dice si el servidor llegó a aceptar el mensaje, y la tabla no distingue un caso del otro. Por eso la tarea no reintenta por su cuenta. ADR-019 §3 deja la política de reintentos a cada tarea y da por hecho que reintenta; la de esta es no hacerlo, y queda dentro de la enmienda de §2. Con reintentos, una sola petición durante una caída del servidor de correo gastaría varios de los 5 enlaces del día sin que llegue ninguno, y un reintento a menos de 2 minutos del intento fallido no saldría. Si la entrega falla, la persona vuelve a pedirlo, y ese enlace cuenta como uno más.

Al emitir un enlace, se entregue o no, los anteriores de esa cuenta que aún valían dejan de valer: **solo vale el último**.

### 4. El enlace

Es una credencial de un solo uso. Lo genera la tarea que envía el correo, con 32 bytes aleatorios; la tabla guarda solo su SHA-256, único. Un secreto de 256 bits aleatorios no necesita un hash lento, y buscarlo por igualdad de su hash no necesita una comparación en tiempo constante: lo que el tiempo de una búsqueda por índice deje ver del hash no sirve sin su preimagen.

**El secreto viaja en el fragmento de la URL** (`…/restablecer#…`), que el navegador no envía al servidor: no queda en los registros de acceso ni en una cabecera `Referer`. La página lo lee y lo manda a la API en el cuerpo de la petición, nunca en una URL.

Caduca a los 60 minutos. Vale una vez. Abrir el enlace no lo gasta: lo consume la petición que cambia la contraseña.

### 5. Cambiar la contraseña

La ruta recibe el secreto y la contraseña nueva. Los intentos se limitan por dirección de red.

El enlace vale si su hash está en la tabla, es el último emitido para su cuenta (§3), no se usó, no caducó y su cuenta sigue activa y no es de personal de plataforma. **Si no vale, la respuesta es la misma sea cual sea la razón.**

Con un enlace que vale, la contraseña pasa por los validadores de la plataforma (ADR-003 §2); si no los cumple, la respuesta dice cuáles, y el enlace no se gasta. Esa respuesta solo la ve quien tiene el enlace.

Al cambiarla, en una transacción y con la fila del enlace bloqueada: la contraseña se guarda con Argon2id, el enlace queda usado, los demás enlaces de la cuenta dejan de valer, **todas las sesiones de la cuenta se revocan** (ADR-003 §2, D-F2-11) y se escribe la auditoría (§7). Si la auditoría no se puede escribir, la contraseña no cambia.

**Cambiar la contraseña no inicia sesión.** Después se entra por el acceso normal, con su límite de intentos y, cuando exista, su MFA. Un enlace de recuperación no quita ni sustituye la MFA de una cuenta.

### 6. Quién queda fuera

- **Una cuenta desactivada** no recupera su contraseña: reactivarla es de quien administra.
- **El personal de plataforma** no usa este flujo, tampoco si además es miembro de una organización. Hoy la marca `is_platform_staff` no da acceso a ningún tenant ni abre ninguna ruta, pero es la cuenta que tendrá el alcance de plataforma (la impersonación, E01-12), y un buzón comprometido no debe bastar para tomarla: su contraseña la restablece un operador. Ese procedimiento no está escrito todavía. Lo que hay es el comando `changepassword` de Django, que aplica los validadores pero no deja fila en la auditoría de plataforma: escribir ese procedimiento, con su auditoría, queda pendiente.

En los dos casos la petición recibe la misma respuesta que cualquier otra.

### 7. Auditoría

En la auditoría de plataforma (ADR-013), con las acciones del catálogo:

- `auth.password.reset_requested`, por cada petición admitida: actor anónimo y la huella del identificador presentado. No dice si la cuenta existe, porque la ruta no lo mira. Se escribe y se confirma antes de encolar la tarea, y **falla cerrado** (ADR-013 §5): si la fila no se puede escribir, no se encola nada y la petición falla, con el mismo error para cualquier correo. No es la fila de un rechazo, como la de un acceso fallido: una petición admitida puede hacer que salga un enlace, y eso no ocurre sin su registro.
- `auth.password.changed`, al cambiar la contraseña: la cuenta como entidad afectada y el método (`reset_link`), en la misma transacción que el cambio (falla cerrado). El actor es anónimo: quien presenta un enlace no tiene sesión.

Un intento con un enlace que no vale deja `auth.password.changed` con resultado denegado y un motivo interno en `metadata`, que la respuesta nunca enseña (como el de `auth.login.failed`, ADR-013 §5). Si el secreto corresponde a una fila de la tabla (un enlace usado, caducado o sustituido por otro, o de una cuenta que ya no puede usarlo), la cuenta va como entidad afectada, igual que en un acceso fallido a una cuenta que existe (ADR-013 §4); si no corresponde a ninguna, la fila no lleva cuenta. Se escribe fuera de la transacción del cambio y se confirma por sí misma (ADR-013 §5): un rechazo que deshace su transacción no se lleva la fila, y si la fila no se puede escribir la respuesta es el mismo rechazo. Nunca se guarda el secreto, su hash ni ninguna contraseña.

Que a una cuenta se le emitió un enlace queda solo en `password_resets`, y ninguna ruta la enseña. Esa tabla no es un registro de auditoría: la leen y la escriben la tarea (§3) y la ruta que cambia la contraseña (§5), el runtime puede modificar y borrar sus filas, y se purgarán. La auditoría de plataforma guarda la petición (con la huella del identificador y la dirección de red) y el cambio (con la cuenta), no el envío. Si hace falta poder decir después a qué cuenta se le envió un enlace, eso se decide antes de purgar la tabla.

### 8. Dónde vive

Todo en `apps.accounts`: la tabla, las dos rutas, la tarea y el texto del correo. El correo sale por `core.mail` (ADR-019). El enlace se compone con el origen público de la aplicación, que llega por configuración del despliegue y nunca de la petición.

### 9. Lo que el mantenedor puede querer cambiar

Son constantes del código: 60 minutos de vida, 5 enlaces por cuenta en 24 horas, 2 minutos entre dos. Y dos decisiones tomadas por la opción más restrictiva: el personal de plataforma queda fuera, y cambiar la contraseña no inicia sesión.

## Alternatives considered

- **El generador de tokens de Django** (`PasswordResetTokenGenerator`): un HMAC sin fila, que deja de valer cuando cambia la contraseña. No necesita tabla, pero no permite contar enlaces por cuenta, invalidar uno sin cambiar la contraseña ni saber que se envió; y ADR-019 §4 ya decide que quien emite un enlace guarda su hash.
- **Buscar la cuenta en la petición** y responder según exista. Permite averiguar quién tiene cuenta; aun respondiendo igual, el trabajo distinto se mide en el tiempo.
- **Guardar una fila por cada petición**, exista o no la cuenta. Iguala el trabajo, pero guarda correos de personas que no son usuarias y deja que cualquiera llene la tabla.
- **Iniciar sesión al cambiar la contraseña.** Más cómodo, pero convierte el enlace en una credencial de sesión y se salta la MFA futura.
- **Enviar una contraseña temporal por correo.** La contraseña quedaría escrita en un buzón.
- **El secreto en la ruta o en la consulta de la URL.** Queda en los registros del proxy y del servidor.
- **Preguntas de seguridad o un código por SMS.** Más superficie y otro proveedor; fuera del alcance.

## Consequences

- E01-02 puede implementarse por partes pequeñas, cada una con sus tests.
- Una persona recupera su acceso sin un operador, si su cuenta está activa y lee su buzón.
- Quien pide un enlace para un correo sin cuenta no recibe nada y la pantalla no se lo dice: la pantalla explica que el correo llega «si la cuenta existe».
- Cambiar la contraseña cierra todas las sesiones de la cuenta, también en otras organizaciones.
- El control de la cuenta pasa a depender del buzón: quien lee el correo de una persona puede tomar su cuenta, salvo que tenga MFA (E01-03).
- Mientras producción no tenga servidor de correo (ADR-019 §6), la tarea falla y nadie recibe el enlace; la respuesta de la ruta no cambia. Cada petición para una cuenta que puede recuperarse gasta aun así uno de sus 5 enlaces del día (§3).
- La entrega no está garantizada (ADR-019) y la tarea no reintenta (§3): un enlace puede no llegar, y la persona vuelve a pedirlo, dentro de sus topes. Un enlace que no llegó también deja sin valor al anterior (§3, ADR-019).
- ADR-019 sigue vigente y no se edita. Este ADR lo enmienda en §3, solo para la recuperación de contraseña: la petición encola la tarea sin fila propia ni evento del outbox (§2), y la tarea no reintenta (§3).
- Las filas de `password_resets` no se borran en esta serie; purgarlas es un trabajo de mantenimiento posterior.
- No hay todavía aviso por correo de que la contraseña cambió.
- **Quien conoce el correo de una persona puede estorbar su recuperación, sin tocar su acceso.** Con 5 peticiones al día, desde las direcciones de red que quiera, hace que reciba 5 correos y gasta sus 5 enlaces: ese día la persona no puede pedir otro cuando lo necesita, y si el límite por identificador de §2 está en su tope, su petición recibe el mismo 429. Es lo que D-F2-9 evitó en el acceso (un contador por cuenta que rechaza); aquí se acepta porque sin tope por cuenta el buzón ajeno no tiene defensa. Lo acota que cada enlace que provoca el atacante llega al buzón de la dueña y le sirve durante sus 60 minutos, que a quien ya tiene sesión o recuerda su contraseña solo le llegan los correos, y que queda el operador.
- «Solo vale el último» deja que otra petición, pasados 2 minutos, invalide el enlace que la persona está usando: su cambio se rechaza y tiene que abrir el correo más reciente, que también llega a su buzón. Ocurre como mucho las veces que deja el tope. Un correo anterior que llega después que uno posterior tampoco vale.

## Security implications

- El enlace es la única credencial del flujo: 256 bits aleatorios, guardado como hash, de un solo uso, con 60 minutos de vida, invalidado por uno más nuevo y fuera de registros de acceso.
- La petición no distingue una cuenta que existe de una que no: misma respuesta y mismo trabajo. Lo que depende de la cuenta ocurre en la tarea.
- Tres límites independientes: por dirección de red y por identificador al pedir, por cuenta al enviar, y por dirección de red al presentar un enlace.
- Cambiar la contraseña revoca las sesiones: quien tenía una sesión robada la pierde.
- El personal de plataforma no depende de su buzón.
- ADR-003, ADR-013 y ADR-014 no cambian: no hay sesión sin acceso, la auditoría de plataforma no guarda correos ni secretos, y las dos rutas son de plataforma sin tocar ningún tenant.

## Operational implications

- Una tabla nueva, platform-owned, que crece con cada enlace emitido, se entregue o no.
- El despliegue debe configurar el origen público de la aplicación, además del servidor de correo (ADR-019).
- La tarea de envío corre en el worker; sin worker no sale ningún enlace.

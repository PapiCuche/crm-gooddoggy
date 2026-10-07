# ADR-020: Invitaciones a una organización

- **Status:** Accepted, salvo la excepción a ADR-014 §4 de las rutas de aceptación (§4), que es *Proposed* y espera al mantenedor (D-F2-14; ADR-015 §3)
- **Date:** 2026-10-07
- **Deciders:** programa autónomo (ADR-015 §5). El mantenedor puede reemplazarlo.
- **Related:** ADR-001, ADR-002, ADR-003, ADR-013, ADR-017, ADR-019, E01-06 en [05-backlog-y-roadmap.md](../fase-0/05-backlog-y-roadmap.md), `user_invitations` en [02-modelo-de-datos.md](../fase-0/02-modelo-de-datos.md) §E.2

## Context

Hoy una organización solo gana miembros por un comando de operador. E01-06 pide que un Admin invite por correo a una persona, con sus roles. El correo ya tiene por dónde salir (ADR-019) y el modelo de datos ya nombra la tabla, pero faltaba decidir las reglas: quién invita y hasta dónde, qué lleva el enlace, cuánto vale, cómo se acepta y qué se le dice a cada parte. Son decisiones de seguridad: una invitación crea una cuenta o da acceso a una organización a quien presente un enlace.

Este ADR fija las reglas de toda la serie. Cada work item implementa una parte.

## Decision

### 1. Qué es una invitación

Una fila de `user_invitations`, propia de la organización (tenant-owned, RLS forzado), con el correo invitado, los roles que tendrá, quién invitó, cuándo caduca y su estado: `PENDING`, `ACCEPTED`, `REVOKED` o `EXPIRED`.

**No se crea ninguna cuenta ni ninguna membresía hasta que se acepta.** El estado `INVITED` de una membresía (modelo de datos §E.2) queda sin usar: crear una membresía exige una cuenta, y crear cuentas para correos que quizá nunca acepten deja cuentas huérfanas y permite sembrar la tabla global de usuarios desde cualquier organización.

Una organización tiene como mucho una invitación pendiente por correo (índice único parcial). Invitar otra vez a un correo con invitación pendiente la renueva: nuevo enlace, nueva caducidad, mismos o nuevos roles. Renovar y reenviar son invitar otra vez: exigen lo mismo que invitar (§2), con los roles que la invitación tendrá, y quien lo hace pasa a ser quien invitó.

El correo se guarda en la forma canónica de las cuentas (`apps.accounts.emails`, D-F2-3) y solo si `core.mail` puede entregarlo: la invitación, el envío y la cuenta que nace al aceptar usan la misma dirección. La restricción de la tabla es más ancha que las dos y no las sustituye.

### 2. Quién invita y hasta dónde

Invita quien tiene el permiso `users.invite` y además `users.manage`, el que exige asignar un rol a un miembro (F2-25). Al invitar se aplican las reglas de asignar un rol (ADR-003 §5): quien invita debe cubrir cada concesión de cada rol que la invitación dará, y un permiso sensible solo lo cubre un Owner. Así nadie concede por invitación lo que no podría conceder asignando. Que baste `users.invite` es una relajación posible, del mantenedor.

Una invitación lleva al menos un rol y como mucho 20. Equipos y sucursal no viajan en la invitación: se asignan después de aceptar, con las rutas que ya existen.

Los roles se guardan por identificador y **las reglas se vuelven a aplicar al aceptar**, bajo el bloqueo de RBAC y con lo que quien invitó tiene en ese momento: sigue siendo miembro activo, conserva los permisos para invitar y cubre cada concesión de cada rol tal como está entonces. Si no, o si un rol se borró, la invitación no se puede aceptar, con la respuesta de un enlace que no vale (§4). Así un rol que ganó concesiones entre la invitación y la aceptación no da a nadie más de lo que quien invitó podría asignar ese día, y suspender o degradar a quien invitó deja sin valor sus invitaciones pendientes, igual que sus sesiones (ADR-003 §2). No es la situación de OBS-F2-29-2: un rol con una invitación pendiente todavía no tiene ese miembro, y quien cambia el rol no lo ve en su recuento.

Límites: una organización no acumula más de 50 invitaciones pendientes, y no crea más de 100 invitaciones en 24 horas, cuenten o no como pendientes: revocar e invitar de nuevo crea una fila y cuenta. **Las invitaciones no se borran** (revocar es un estado): los límites se cuentan sobre la tabla. Con el tope de envíos por invitación (§3), eso acota cuántos correos manda la plataforma en nombre de una organización, a lo sumo 500 al día de forma sostenida (100 invitaciones por 5 envíos) y unos 750 en 24 horas si además arrastra 50 pendientes: ADR-019 deja ese límite a cada flujo. Son constantes del código.

### 3. El enlace

El enlace es una credencial de un solo uso. Lo genera la tarea que envía el correo (ADR-019 §4), con 32 bytes aleatorios. La tabla guarda solo su SHA-256; un secreto de 256 bits aleatorios no necesita un hash lento. La invitación se busca por igualdad de ese hash (abajo): no hace falta una comparación en tiempo constante, porque lo que el tiempo de una búsqueda por índice deje ver del hash no sirve sin su preimagen, que son 256 bits aleatorios.

**El enlace lleva solo el secreto.** La organización no sale de la petición (ADR-001 §5.1): la invitación se encuentra por el hash del secreto, con una función `SECURITY DEFINER` mínima como las que ADR-002 §3.3 prevé para los enlaces públicos, con todos sus requisitos. Recibe el hash como `text` y no devuelve nada si no tiene la forma de un SHA-256; es `STABLE` y no escribe nada, tampoco para contar intentos; devuelve solo `organization_id` y el identificador de la invitación, y solo de una invitación pendiente y sin caducar; no devuelve el correo ni nada más. Es la única lectura de esta tabla fuera de RLS. El hash es único en toda la tabla (`user_invitations_token_hash_uq`): es la única unicidad de la tabla que no empieza por `organization_id` (ADR-001 §5.3), como la clave por la que el modelo de datos enruta `channel_accounts`. No es un canal entre organizaciones: el hash lo escribe solo la tarea de envío, a partir del secreto que ella genera, y ninguna ruta acepta un hash; si el índice rechazara uno, la tarea falla y su reintento genera otro (ADR-019 §4). Con esos dos identificadores, y no antes, se abre el `tenant_scope` de esa organización: «tras validar el token», como pide ADR-002 §3.2. La función solo localiza: dentro del scope se vuelve a leer la fila con bloqueo y se comprueban otra vez estado, caducidad y hash antes de escribir nada. **El secreto viaja en el fragmento de la URL** (`…/invitacion#…`), que el navegador no envía al servidor: no queda en los registros de acceso ni en una cabecera `Referer`. La página lo lee y lo manda a la API en el cuerpo de la petición, nunca en una URL.

Caduca a los 7 días. Renovar una invitación o reenviarla genera un enlace nuevo e invalida el anterior. **Cada invitación se envía como mucho 5 veces, con al menos 15 minutos entre dos envíos**; agotadas, hay que revocarla e invitar de nuevo. Renovar es un envío más y no pone la cuenta a cero. Los 15 minutos valen también entre invitaciones distintas de la misma organización al mismo correo: revocar e invitar otra vez no los salta. La tabla lleva la cuenta (`send_count`) y la base no deja pasar de 5. Un enlace vale una vez: al aceptar, la invitación pasa a `ACCEPTED`.

`EXPIRED` no lo escribe un proceso: una invitación pendiente cuya fecha pasó no se puede aceptar y se enseña como caducada; renovarla la devuelve a pendiente con fecha nueva.

### 4. Aceptar

Las rutas de aceptación son de plataforma (sin sesión de la organización): quien acepta todavía no es miembro.

**Excepción a ADR-014 §4, pendiente del mantenedor.** ADR-014 §4 dice que una ruta de plataforma no abre `tenant_scope`, no usa modelos tenant-owned y no devuelve datos de una organización a quien no es miembro. Aceptar necesita las tres cosas: leer la invitación, crear la membresía (ADR-002 §3.2 lo sitúa en el `tenant_scope` de la organización que invita, «tras validar el token») y enseñar el nombre de la organización (§5). ADR-002 ya prevé este caso, pero ADR-014 es posterior y no lo exceptúa, y este ADR no puede relajarlo por su cuenta (ADR-015 §3): **las rutas de aceptación, y con ellas el envío del correo, no se implementan hasta que el mantenedor confirme la excepción** (D-F2-14 en [phase-2.md](../phases/phase-2.md)). Lo que este ADR fija para cuando se confirme: el scope lo abre solo esa ruta, con actor `SYSTEM`, después de que la búsqueda por hash (§3) devuelva una invitación; no se escribe ni se responde nada de la organización hasta entonces; y ante un enlace que la búsqueda no devuelve, el trabajo y la respuesta son los mismos sea cual sea la razón. Un enlace que la búsqueda devuelve pero que ya no se puede aceptar (§2) recibe la misma respuesta aunque cueste más trabajo: esa diferencia solo la ve quien tiene el secreto.

Ante un enlace que no vale, sea por la razón que sea (no existe, caducó, se revocó, ya se usó, secreto incorrecto), la respuesta es la misma. Los intentos se limitan por dirección, como el acceso (ADR-003 §2).

- **Si el correo no tiene cuenta**, quien acepta pone su nombre y su contraseña. La contraseña pasa por los validadores de la plataforma y se guarda con Argon2id. Se crea la cuenta, la membresía activa y sus roles, y la invitación queda aceptada, en una transacción.
- **Si el correo ya tiene cuenta**, quien acepta debe haber iniciado sesión con esa cuenta. Un enlace no sustituye a la contraseña de una cuenta que ya existe: quien intercepte el correo no entra en ella.

**Abrir el enlace no lo gasta.** Solo lo consume la petición de aceptar, que es una acción explícita de la persona (un `POST` con CSRF, ADR-014 §2) y nunca algo que la página haga al cargarse, tampoco con una sesión abierta. Lo que la página enseña antes (§5) lo pide con el secreto en el cuerpo y no cambia nada.

**Una invitación nunca toca una membresía que ya existe.** Si la cuenta ya tiene membresía en la organización, en cualquier estado (activa, suspendida o dada de baja), no se invita a ese correo; y si la membresía apareció después de invitar, la invitación no se puede aceptar. Reactivar a un miembro o cambiar sus roles tiene sus propias reglas (ADR-017).

Si dos aceptaciones crean a la vez la cuenta del mismo correo (invitaciones de dos organizaciones), la segunda choca con la unicidad de `users.email`: su transacción se deshace entera y recibe la respuesta de «el correo ya tiene cuenta».

Aceptar no inicia sesión. Después se entra por el acceso normal, con su límite de intentos y, cuando exista, su MFA.

La cuenta se crea con el correo de la invitación: aceptar demuestra que se lee ese buzón, y eso es toda la verificación de correo que hay.

### 5. Lo que no se revela

- **A quien invita** no se le dice si un correo tiene cuenta en la plataforma. Sí se le dice si ese correo ya es miembro de su organización: es información de su propia organización.
- **A quien abre un enlace válido** se le enseña la organización que invita y el correo invitado, y si tiene que crear una cuenta o iniciar sesión. Quien tiene el enlace ya lee ese buzón.
- **A quien abre un enlace que no vale** no se le dice por qué.

### 6. Auditoría

En la auditoría de la organización: `membership.invited` al crear o renovar (quién, a qué correo, con qué roles), `membership.invitation_revoked` y `membership.activated` al aceptar. El correo invitado es un dato de la organización y queda en claro en la fila de auditoría: el redactor compartido no trata un correo como secreto (ADR-013 §4), y lo lee quien tenga `audit.view`.

Los intentos de aceptación rechazados son de plataforma (ADR-013): no hay organización que los vea hasta que el enlace vale.

### 7. Dónde vive

La tabla está en `apps.organizations`, con las membresías. Invitar y aceptar cruzan módulos (roles de `access`, cuentas de `accounts`, membresías de `organizations`): los orquesta `apps.members` (ADR-017), bajo el bloqueo de RBAC. El envío es una tarea suscrita al evento del outbox.

### 8. Lo que queda del mantenedor

Una sola cosa de este ADR no la puede decidir el programa: **la excepción a ADR-014 §4** para las rutas de aceptación (§4, D-F2-14). Hasta que se confirme se construye lo que no depende de ella: la tabla, invitar, listar y revocar. No se envía ningún correo de invitación mientras su enlace no se pueda aceptar.

Dos preguntas que la revisión de este ADR dejó abiertas quedaron decididas aquí, por la opción más restrictiva: la invitación se encuentra por el hash del secreto con una función `SECURITY DEFINER` (§3), no por identificadores que viajen en el enlace; y el envío tiene tope por invitación y por organización (§2, §3).

## Alternatives considered

- **Crear la cuenta y una membresía `INVITED` al invitar.** Es lo que sugiere el estado del modelo de datos. Deja cuentas sin dueño y permite a cualquier organización crear filas en la tabla global de usuarios.
- **Que el enlace baste para entrar en una cuenta existente.** Convierte el correo de invitación en un restablecimiento de contraseña sin sus controles.
- **Iniciar sesión al aceptar.** Más cómodo, pero salta el límite de intentos y la MFA futura, y hace del enlace una credencial de sesión.
- **Guardar el enlace en claro o cifrado para poder reenviarlo.** Reenviar genera uno nuevo: no hace falta guardarlo.
- **Un token firmado sin fila (JWT).** No se puede revocar ni limitar a un uso sin guardar estado.
- **Que el enlace lleve la organización y la invitación**, y la ruta abra el `tenant_scope` de la organización que nombra una petición sin autenticar. Evita la función `SECURITY DEFINER`, pero toma la organización de la petición (ADR-001 §5.1) y abre el scope antes de validar nada (ADR-002 §3.2).
- **El secreto en la ruta o en la consulta de la URL.** Queda en los registros del proxy y del servidor.

## Consequences

- E01-06 puede implementarse por partes pequeñas, cada una con sus tests.
- Una organización puede tener miembros sin pasar por un operador.
- Quien pierde el correo o lo recibe tarde pide que se lo reenvíen: no hay otra forma de recuperar un enlace.
- Una persona con cuenta invitada a otra organización tiene que recordar su contraseña; la recuperación (E01-02) es otra serie.
- La anti-escalada se comprueba al invitar y otra vez al aceptar (§2). Si quien invitó pierde sus permisos o su membresía, sus invitaciones pendientes dejan de poder aceptarse; siguen en la lista hasta que alguien las renueve, las revoque o caduquen.
- Los límites (50 pendientes, 100 invitaciones nuevas en 24 horas, 5 envíos por invitación con 15 minutos entre dos, 7 días de vida, 20 roles) son constantes del código: cambiarlos es un cambio de código.
- Mientras el mantenedor no confirme la excepción de §4, una organización puede preparar invitaciones pero nadie puede recibirlas ni aceptarlas: la serie no entrega valor al usuario hasta entonces.
- La entrega del correo no está garantizada (ADR-019): una invitación puede quedar pendiente sin que el correo llegue. La pantalla lo deja ver (enviada o no) y permite reenviar.

## Security implications

- El enlace es la única credencial del flujo: 256 bits aleatorios, guardado como hash, de un solo uso, con caducidad, revocable, buscado por su hash y fuera de registros de acceso.
- Invitar no amplía privilegios: se cubre lo que se concede, al invitar y al aceptar.
- La respuesta uniforme y el límite de intentos impiden usar la aceptación para averiguar enlaces o correos.
- La única lectura fuera de RLS es la función de §3, que devuelve dos identificadores y ningún dato personal. No hay sesión sin acceso ni cuenta sin contraseña validada: ADR-003 y ADR-013 no cambian. **ADR-014 §4 sí queda afectado** por las rutas de aceptación: es una excepción pendiente del mantenedor (§4, §8), no una decisión de este ADR. La organización no se toma de la petición (ADR-001 §5.1) y el scope se abre tras validar el enlace (ADR-002 §3.2 y §3.3).

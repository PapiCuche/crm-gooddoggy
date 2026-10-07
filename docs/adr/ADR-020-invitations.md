# ADR-020: Invitaciones a una organización

- **Status:** Accepted
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

Una organización tiene como mucho una invitación pendiente por correo (índice único parcial). Invitar otra vez a un correo con invitación pendiente la renueva: nuevo enlace, nueva caducidad, mismos o nuevos roles.

### 2. Quién invita y hasta dónde

Invita quien tiene el permiso `users.invite`. Al invitar se aplican las reglas de asignar un rol (ADR-003 §5, F2-25): quien invita debe cubrir cada concesión de cada rol que la invitación dará. Nadie concede por invitación lo que no podría conceder asignando.

Una invitación lleva al menos un rol. Equipos y sucursal no viajan en la invitación: se asignan después de aceptar, con las rutas que ya existen.

Los roles se guardan por identificador y se vuelven a comprobar al aceptar: un rol borrado entretanto hace que la invitación no se pueda aceptar. Si un rol cambió de concesiones entre la invitación y la aceptación, el miembro recibe el rol como esté: es la misma situación que cambiar un rol que ya tiene miembros (OBS-F2-29-2).

Límite: una organización no acumula más de 50 invitaciones pendientes. Acota el uso del correo de la plataforma como canal de envío a terceros.

### 3. El enlace

El enlace es una credencial de un solo uso. Lo genera la tarea que envía el correo (ADR-019 §4), con 32 bytes aleatorios. La tabla guarda solo su SHA-256; un secreto de 256 bits aleatorios no necesita un hash lento. Se compara en tiempo constante.

El enlace identifica también la organización y la invitación, que no son secretos: sin ellos no se puede buscar la fila bajo RLS, y no se añade ninguna búsqueda que la salte. **El secreto viaja en el fragmento de la URL** (`…/invitacion#…`), que el navegador no envía al servidor: no queda en los registros de acceso ni en una cabecera `Referer`. La página lo lee y lo manda a la API en el cuerpo de la petición, nunca en una URL.

Caduca a los 7 días. Renovar una invitación o reenviarla genera un enlace nuevo e invalida el anterior. Un enlace vale una vez: al aceptar, la invitación pasa a `ACCEPTED`.

`EXPIRED` no lo escribe un proceso: una invitación pendiente cuya fecha pasó no se puede aceptar y se enseña como caducada; renovarla la devuelve a pendiente con fecha nueva.

### 4. Aceptar

Las rutas de aceptación son de plataforma (sin sesión de la organización): quien acepta todavía no es miembro. Ante un enlace que no vale, sea por la razón que sea (no existe, caducó, se revocó, ya se usó, secreto incorrecto), la respuesta es la misma. Los intentos se limitan por dirección, como el acceso (ADR-003 §2).

- **Si el correo no tiene cuenta**, quien acepta pone su nombre y su contraseña. La contraseña pasa por los validadores de la plataforma y se guarda con Argon2id. Se crea la cuenta, la membresía activa y sus roles, y la invitación queda aceptada, en una transacción.
- **Si el correo ya tiene cuenta**, quien acepta debe haber iniciado sesión con esa cuenta. Un enlace no sustituye a la contraseña de una cuenta que ya existe: quien intercepte el correo no entra en ella.

Aceptar no inicia sesión. Después se entra por el acceso normal, con su límite de intentos y, cuando exista, su MFA.

La cuenta se crea con el correo de la invitación: aceptar demuestra que se lee ese buzón, y eso es toda la verificación de correo que hay.

### 5. Lo que no se revela

- **A quien invita** no se le dice si un correo tiene cuenta en la plataforma. Sí se le dice si ese correo ya es miembro de su organización: es información de su propia organización.
- **A quien abre un enlace válido** se le enseña la organización que invita y el correo invitado, y si tiene que crear una cuenta o iniciar sesión. Quien tiene el enlace ya lee ese buzón.
- **A quien abre un enlace que no vale** no se le dice por qué.

### 6. Auditoría

En la auditoría de la organización: `membership.invited` al crear o renovar (quién, a qué correo, con qué roles), `membership.invitation_revoked` y `membership.activated` al aceptar. El correo invitado es un dato de la organización y queda en la fila de auditoría, redactado como cualquier otro (ADR-011).

Los intentos de aceptación rechazados son de plataforma (ADR-013): no hay organización que los vea hasta que el enlace vale.

### 7. Dónde vive

La tabla está en `apps.organizations`, con las membresías. Invitar y aceptar cruzan módulos (roles de `access`, cuentas de `accounts`, membresías de `organizations`): los orquesta `apps.members` (ADR-017), bajo el bloqueo de RBAC. El envío es una tarea suscrita al evento del outbox.

## Alternatives considered

- **Crear la cuenta y una membresía `INVITED` al invitar.** Es lo que sugiere el estado del modelo de datos. Deja cuentas sin dueño y permite a cualquier organización crear filas en la tabla global de usuarios.
- **Que el enlace baste para entrar en una cuenta existente.** Convierte el correo de invitación en un restablecimiento de contraseña sin sus controles.
- **Iniciar sesión al aceptar.** Más cómodo, pero salta el límite de intentos y la MFA futura, y hace del enlace una credencial de sesión.
- **Guardar el enlace en claro o cifrado para poder reenviarlo.** Reenviar genera uno nuevo: no hace falta guardarlo.
- **Un token firmado sin fila (JWT).** No se puede revocar ni limitar a un uso sin guardar estado.
- **El secreto en la ruta o en la consulta de la URL.** Queda en los registros del proxy y del servidor.

## Consequences

- E01-06 puede implementarse por partes pequeñas, cada una con sus tests.
- Una organización puede tener miembros sin pasar por un operador.
- Quien pierde el correo o lo recibe tarde pide que se lo reenvíen: no hay otra forma de recuperar un enlace.
- Una persona con cuenta invitada a otra organización tiene que recordar su contraseña; la recuperación (E01-02) es otra serie.
- La comprobación de anti-escalada se hace al invitar. Si quien invitó pierde después sus permisos, la invitación pendiente sigue valiendo hasta que alguien la revoque o caduque.
- El límite de 50 pendientes y los 7 días son constantes del código: cambiarlos es un cambio de código.
- La entrega del correo no está garantizada (ADR-019): una invitación puede quedar pendiente sin que el correo llegue. La pantalla lo deja ver (enviada o no) y permite reenviar.

## Security implications

- El enlace es la única credencial del flujo: 256 bits aleatorios, guardado como hash, de un solo uso, con caducidad, revocable, comparado en tiempo constante y fuera de registros de acceso.
- Invitar no amplía privilegios: se cubre lo que se concede.
- La respuesta uniforme y el límite de intentos impiden usar la aceptación para averiguar enlaces o correos.
- No se debilita ningún control de ADR-001, ADR-002, ADR-003, ADR-013 ni ADR-014: no hay lectura fuera de RLS, ni sesión sin acceso, ni cuenta sin contraseña validada.

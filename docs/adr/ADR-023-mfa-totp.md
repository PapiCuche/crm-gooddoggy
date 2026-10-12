# ADR-023: Segundo factor con TOTP y códigos de respaldo

- **Status:** Accepted, salvo §11 (quién quita el segundo factor de otra persona), que queda *Proposed* como decisión D-F2-15
- **Date:** 2026-10-11
- **Deciders:** programa autónomo (ADR-015 §5). El mantenedor puede reemplazarlo.
- **Related:** ADR-001 §2 y §3, ADR-003 §2 y §4, ADR-013, ADR-014 §4, ADR-021 §5, ADR-022, E01-03 en [05-backlog-y-roadmap.md](../fase-0/05-backlog-y-roadmap.md), el catálogo de auditoría en [04-precios-pipeline-seguridad-infra.md](../fase-0/04-precios-pipeline-seguridad-infra.md) §N.2, `user_mfa_devices` en [02-modelo-de-datos.md](../fase-0/02-modelo-de-datos.md)

## Context

Hoy una cuenta se protege solo con su contraseña. ADR-003 §4 ya decidió que habrá un segundo factor y con qué reglas generales:

- «TOTP (RFC 6238) + códigos de respaldo de un solo uso (hash).»
- «**Política:** el requisito se evalúa por membresía. Si el usuario tiene rol Owner/Admin o algún permiso sensible en **cualquiera** de sus organizaciones, debe tener MFA para acceder a esa organización. Hasta la salida a producción, la política se aplica con un feature flag (`MFA_ENFORCEMENT = warn | enforce`) para no bloquear el desarrollo.»
- «**Step-up:** las acciones sensibles […] exigen MFA verificado hace ≤ 15 minutos (`session.mfa_verified_at`).»
- §2: «Rotación del ID de sesión en login, logout y elevación MFA.»
- *Operational implications*: «La recuperación de acceso MFA (dispositivo perdido) es un procedimiento documentado: códigos de respaldo; si no hay, lo resuelve otro Owner, o soporte de plataforma con verificación de identidad y auditoría.»

Faltaba el diseño del flujo: qué se guarda, cómo se da de alta un dispositivo, qué pasa entre la contraseña y el código, cuántas veces se puede probar, cómo se quita. ADR-022 resolvió cómo se cifra el secreto y dejó escrito que un secreto que no se abre **no** es un secreto que falta.

Este ADR no cambia ninguna regla de ADR-003. Donde concreta una, elige lo más estricto; y donde ADR-003 pide algo que el programa no puede decidir (§11), lo dice.

## Decision

### 1. Qué hay y dónde vive

Un segundo factor es **un dispositivo TOTP confirmado** de la cuenta, más sus **códigos de respaldo**. Como mucho un dispositivo TOTP por cuenta. WebAuthn queda para después: la tabla lleva `type` para admitirlo (ADR-003 §4).

Todo vive en `apps.accounts`, en dos tablas **platform-owned** (ADR-001 §2; y §3: «`users` es la identidad global de la persona (login, MFA)»): `user_mfa_devices` y `user_mfa_backup_codes`. Sin organización ni política de tenant, como `users` y `password_resets`.

Las rutas serán **rutas de plataforma** (ADR-014 §4), bajo `/api/v1/auth/`: no abren `tenant_scope` ni leen tablas de tenant. La política por membresía (§9) se aplica donde se entra a una organización, que ya es una ruta de tenant.

El modelo de datos de la fase 0 preveía una sola tabla, con los códigos de respaldo como otro `type`, y nombraba `django-otp`. Aquí los códigos van en su propia tabla (son muchos por cuenta, cada uno con su estado, y se guardan como hash, no cifrados), y TOTP se calcula con la biblioteca estándar: `django-otp` no está en ADR-012. No es un cambio de regla: ese documento describe el modelo previsto, no lo fija.

### 2. TOTP

RFC 6238 con los parámetros que todas las aplicaciones de autenticación entienden: HMAC-SHA-1, 6 dígitos, pasos de 30 segundos. El secreto son 20 bytes aleatorios (160 bits, la longitud que recomienda RFC 4226), que se enseñan una vez en base32 y como URI `otpauth://`.

**Ventana:** se acepta el código del paso actual, el del anterior y el del siguiente (un desfase de reloj de hasta 30 segundos). No es configurable por cuenta.

**Un paso se acepta una sola vez.** La fila guarda el último paso aceptado (`last_step`); un código de ese paso o de uno anterior se rechaza aunque sea correcto. Así un código visto por encima del hombro o capturado no sirve después. La comprobación y la escritura de `last_step` van en una transacción, con la fila bloqueada: dos peticiones con el mismo código no pasan las dos.

La comparación del código es en tiempo constante.

### 3. El secreto

Se guarda cifrado con `core.sealed` (ADR-022): propósito `mfa.totp`, dueño `user:<id de la cuenta>`. La API lo devuelve **una vez**, al empezar el alta, y nunca más.

Un secreto que no se abre (ADR-022 §4) no comprueba ningún código: el segundo paso falla, como con un código incorrecto, y la cuenta no entra. Nunca se trata como una cuenta sin segundo factor.

### 4. Alta de un dispositivo

Dos pasos, los dos con sesión.

1. **Empezar.** Pide la **contraseña actual** de la cuenta, además de la sesión: quien solo tiene una sesión robada no puede atarle un factor suyo a la cuenta. Crea el dispositivo sin confirmar (sustituye a otro sin confirmar que hubiera) y devuelve el secreto y su URI. Un dispositivo sin confirmar no protege ni exige nada, y caduca a los 10 minutos.
2. **Confirmar.** Con un código válido del dispositivo, queda confirmado. En la misma transacción: se generan los códigos de respaldo (§6), que se devuelven una vez; se audita `auth.mfa.enabled`; se rota el ID de sesión y se anota `mfa_verified_at` (ADR-003 §2). Las demás sesiones de la cuenta se revocan: se abrieron sin segundo factor.

Con un dispositivo ya confirmado, no se empieza otra alta: primero se quita el que hay (§8).

Los intentos de confirmar un alta cuentan en el límite por dirección del acceso; no en el tope por cuenta del segundo paso (§5), porque aquí ya hay una sesión y la contraseña.

### 5. El segundo paso del acceso

`POST /api/v1/auth/login/` sigue comprobando la contraseña como hoy, con su límite de intentos. Si la cuenta **tiene un dispositivo confirmado**, la contraseña correcta **no abre sesión**:

- La respuesta dice que falta el segundo paso y no lleva ningún dato de la cuenta.
- Queda una marca de **acceso a medias** (la cuenta y la hora), que caduca a los 5 minutos. No es una sesión autenticada: `request.user` es anónimo y toda ruta que pide sesión responde 401.
- Los contadores del límite de intentos **no se perdonan** todavía: se perdonan al completar el segundo paso. Quien tiene la contraseña y no el código no limpia el contador probando.

La ruta del segundo paso recibe un código, que puede ser el de TOTP o uno de respaldo. Es de acceso anónimo (no hay sesión) y exige CSRF y la marca de acceso a medias.

- **Código válido:** se abre la sesión como lo hace hoy el acceso (ID de sesión nuevo, época de la cuenta) y además se anota `mfa_verified_at`. Se audita `auth.login.succeeded`, con el método (`totp` o `backup_code`), en la transacción que abre la sesión: sin esa fila no hay sesión (ADR-013 §5).
- **Código no válido:** se audita `auth.login.failed` con la cuenta como entidad afectada y el motivo interno, y cuenta en el límite de abajo.

**Límite del segundo paso: por cuenta, y rechaza.** Como mucho **10 códigos fallidos por cuenta en 24 horas**, vengan de donde vengan; alcanzado el tope, el segundo paso de esa cuenta se rechaza (429) hasta que pase la ventana, también con un código correcto. Además siguen valiendo los límites por dirección del acceso.

Es distinto del acceso, donde el contador por cuenta no rechaza (D-F2-9) para que un tercero no deje fuera a la dueña. Aquí hace falta: un código tiene seis dígitos y tres valen a la vez, y sin un tope por cuenta quien tiene la contraseña y muchas direcciones acabaría acertando. Con el tope, la probabilidad de acertar es de 3 entre 100 000 al día. El coste: **quien conoce la contraseña puede dejar a la dueña sin segundo paso** durante la ventana. Conocer la contraseña ya es un compromiso de la cuenta, y la dueña tiene salida: **cambiar la contraseña con un enlace de recuperación pone ese contador a cero**, y quien tenía la contraseña vieja ya no llega al segundo paso. Al alcanzarse el tope se avisa a la cuenta por correo, de mejor esfuerzo, como el aviso de F2-93.
- La respuesta a un código no válido es la misma sea cual sea la razón: incorrecto, ya usado, de un paso repetido, o un secreto que no se abre.

Esto vale **siempre que la cuenta tenga un dispositivo confirmado**, con `MFA_ENFORCEMENT` en `warn` o en `enforce`: el flag decide a quién se le exige tener segundo factor (§9), no si el de quien lo tiene se comprueba.

### 6. Códigos de respaldo

Diez códigos, de un solo uso, generados al confirmar el dispositivo. Cada uno son 10 caracteres de un alfabeto sin letras ambiguas (50 bits): se escriben a mano, y su límite de intentos es el del acceso. Se guardan como SHA-256 (ADR-003 §4: «hash»); con 50 bits aleatorios y cinco intentos por ventana no hace falta un hash lento.

- Usar uno lo gasta, en la transacción que abre la sesión.
- Se pueden **regenerar** con sesión, la contraseña actual y un código TOTP válido: los anteriores dejan de valer todos.
- La API dice cuántos quedan, nunca cuáles.
- Usar un código de respaldo cuenta como segundo factor verificado.

### 7. Step-up

`mfa_verified_at` se guarda en la sesión al completar el segundo paso o al confirmar el alta. Una acción sensible lo exige de hace 15 minutos como mucho (ADR-003 §4). Una ruta de plataforma permite verificarse de nuevo con un código; al hacerlo rota el ID de sesión (ADR-003 §2).

Qué acciones lo exigen lo dice ADR-003 §4; aplicarlo a cada una es de sus work items. Con `MFA_ENFORCEMENT = warn`, una cuenta sin segundo factor no queda bloqueada en esas acciones: es lo que el flag permite «para no bloquear el desarrollo».

### 8. Quitar el segundo factor propio

Con sesión, la contraseña actual **y** un código válido (TOTP o de respaldo). Borra el dispositivo y todos sus códigos, audita `auth.mfa.disabled` en la misma transacción y revoca las demás sesiones.

Quitarlo no exime de la política: si una membresía lo exige (§9), con `enforce` la persona no entra a esa organización hasta darse de alta otra vez.

### 9. A quién se le exige

ADR-003 §4: se evalúa **por membresía**, al entrar a una organización. La membresía lo exige si concede algún **permiso sensible** (`permissions.is_sensitive`). Los roles Owner y Admin de plantilla los conceden, así que quedan dentro sin que la regla mire el nombre de ningún rol: el nombre de un rol nunca autoriza nada.

- **`MFA_ENFORCEMENT = enforce`:** sin dispositivo confirmado, esa organización no se abre; la respuesta lo dice con un código propio para que la pantalla lleve al alta. Las demás organizaciones de la persona, si no lo exigen, se abren.
- **`MFA_ENFORCEMENT = warn`:** se abre, y el contexto de la organización dice que el segundo factor se exige y falta, para que la pantalla avise.

El valor por defecto es `warn`. Antes de producción pasa a `enforce` (ADR-003, *Context*: «MFA es obligatorio antes de producción para Owner, Admin y usuarios con permisos sensibles»). Con el alta de TOTP, `SECRETS_KEKS` pasa a ser obligatoria para arrancar `production` (ADR-022 §6).

### 10. Auditoría

Auditoría de plataforma (ADR-013), con las acciones del catálogo (§N.2): `auth.mfa.enabled`, `auth.mfa.disabled`, `auth.login.succeeded`, `auth.login.failed`.

- Los cambios (`enabled`, `disabled`) y el acceso completado se escriben en la transacción de la operación: si la fila no se puede escribir, la operación no ocurre.
- Un segundo paso fallido se escribe fuera de esa transacción y, si no se puede escribir, la respuesta no cambia (ADR-013 §5).
- Nunca se guarda un código, un secreto ni un código de respaldo, ni su hash.
- Regenerar los códigos de respaldo y un alta empezada y no confirmada no tienen acción en el catálogo: se decide en su work item, sin inventar nombres aquí.

### 11. Quitar el segundo factor de otra persona (*Proposed*, D-F2-15)

ADR-003 dice que, sin códigos de respaldo, la pérdida del dispositivo «lo resuelve otro Owner, o soporte de plataforma con verificación de identidad y auditoría».

La cuenta es global (ADR-001 §2) y una persona puede pertenecer a varias organizaciones. Si el Owner de una organización puede quitar el segundo factor de esa persona, **debilita su acceso a todas las demás**, sobre las que ese Owner no tiene autoridad. ADR-003 no dice cómo se evita.

El programa no decide esto. Hasta que el mantenedor lo haga:

- No existe ninguna ruta ni pantalla con la que una persona quite el segundo factor de otra.
- La única vía es de operador de plataforma (un comando con motivo y auditoría, B10 de `security-boundaries.md`), que es la otra mitad de la frase de ADR-003. Ese comando es un work item posterior; hasta entonces, quien pierde el dispositivo y los códigos no puede entrar.

### 12. Lo que no cambia

- **Recuperar la contraseña no quita el segundo factor** (ADR-021 §5: «Un enlace de recuperación no quita ni sustituye la MFA de una cuenta»): tras cambiarla, el acceso pide el segundo paso igual.
- La contraseña, su límite de intentos y la sesión siguen como están.

### 13. Lo que el mantenedor puede querer cambiar

La ventana de ±1 paso; diez códigos de respaldo y su longitud; los 10 minutos de un alta sin confirmar y los 5 de un acceso a medias; pedir la contraseña para empezar el alta; revocar las demás sesiones al dar de alta y al quitar; que la política mire los permisos sensibles y no el nombre del rol; el tope de 10 códigos fallidos por cuenta en 24 horas, que rechaza, y que cambiar la contraseña lo ponga a cero.

## Alternatives considered

- **Abrir la sesión con la contraseña y marcarla como «pendiente de MFA».** Más simple, pero cada ruta tendría que acordarse de mirar la marca, y una que lo olvide deja pasar con solo la contraseña. Sin sesión, lo que se olvida falla cerrado.
- **Un token de desafío devuelto al navegador entre los dos pasos.** Sería una credencial más que guardar en el cliente; la marca vive en el servidor, tras la cookie HttpOnly que ya existe (ADR-003: nada sensible en el navegador).
- **Aceptar solo el paso actual.** Un reloj con unos segundos de desfase daría rechazos sin explicación; ±1 es lo habitual y la protección contra repeticiones no depende de la ventana.
- **Guardar los códigos de respaldo cifrados** para poder enseñarlos otra vez. ADR-003 §4 dice hash; y enseñarlos otra vez los expone a quien tenga una sesión.
- **Contar el segundo paso en los contadores del acceso.** El de la cuenta no rechaza (D-F2-9): con muchas direcciones, seis dígitos se acaban acertando. Hace falta un tope por cuenta que rechace, y por eso es un contador propio.
- **Un tope por cuenta más corto (5 fallos en 15 minutos).** Deja probar 480 códigos al día: en un ataque sostenido, uno de cada 700 días acierta. Diez al día lo deja en uno de cada 33 000.
- **Exigir el segundo factor por rol (Owner, Admin).** ADR-003 nombra esos roles, pero una organización puede crear un rol propio con los mismos permisos; mirar los permisos sensibles cubre a los tres.
- **Dejar que cualquier Owner quite el segundo factor de un miembro** (lo que ADR-003 sugiere). Véase §11.

## Consequences

- E01-03 tiene sus reglas. Los work items siguientes las construyen: el cálculo de TOTP, el alta, el segundo paso, los códigos, la política, el step-up y las pantallas.
- La respuesta de `POST /auth/login/` gana una forma nueva (falta el segundo paso): el cliente y la pantalla de acceso cambian con ese work item.
- Quien tiene la contraseña de una cuenta con segundo factor **sabe que la contraseña es correcta** (la respuesta cambia) y que la cuenta tiene segundo factor. No se puede evitar sin pedir el código a todas las cuentas.
- Quien conoce la contraseña puede **dejar a la dueña sin segundo paso** hasta 24 horas fallando diez códigos (§5). En el acceso por contraseña eso no pasa (D-F2-9). La salida es cambiar la contraseña con un enlace de recuperación.
- Perder el dispositivo y los códigos deja a la persona sin acceso hasta que exista el comando de operador (§11).
- Dar de alta o quitar el segundo factor cierra las demás sesiones de la cuenta.

## Security implications

- **Una contraseña robada ya no basta** para entrar en una cuenta con segundo factor, ni para darle uno a una cuenta que no lo tiene (hace falta además una sesión), ni para quitárselo (hace falta además un código).
- **Una sesión robada no basta** para atar un factor nuevo ni para quitar el que hay: las dos cosas piden la contraseña. Sí permite actuar como la persona mientras dure, como hoy; el step-up limita lo más sensible.
- **El secreto TOTP no sale de la aplicación** después del alta, y en reposo está cifrado y atado a su cuenta (ADR-022). Los límites de ADR-022 valen aquí: quien escribe en la base de datos puede reponer un valor anterior del mismo dueño, es decir, el secreto de un dispositivo anterior de esa misma cuenta.
- **Phishing en tiempo real** (una página falsa que reenvía contraseña y código al momento) no lo impide TOTP; lo impediría WebAuthn. Aceptar cada paso una vez y la ventana corta lo acotan a un uso.
- **El segundo paso no revela** por qué falla un código.
- No debilita ADR-001, ADR-002, ADR-003, ADR-013 ni ADR-014: las tablas son platform-owned sin tocar ningún tenant; las rutas serán de plataforma con su clase declarada; la auditoría sigue ADR-013 §4 y §5; y cada regla de ADR-003 §4 se mantiene o se endurece. La única que no se construye es la mitad «otro Owner» de la recuperación (§11), que queda abierta en vez de implementada a medias.

## Operational implications

- `MFA_ENFORCEMENT` (`warn` por defecto, `enforce` antes de producción) y `SECRETS_KEKS` (obligatoria en `production` desde el alta de TOTP).
- Los relojes de los servidores tienen que estar sincronizados: TOTP depende de la hora.
- Procedimiento de dispositivo perdido: códigos de respaldo; si no hay, el comando de operador, cuando exista (§11).

# ADR-027: Identidad externa: cuentas independientes, federadas y vinculadas

- **Status:** Accepted
- **Date:** 2026-10-22
- **Deciders:** el mantenedor aprobó la dirección el 2026-10-22 (las decisiones de §0). El programa autónomo escribió el ADR (F2-118, F2-120). Aceptarlo no implementa nada: los slices del §12 esperan a un proveedor que cumpla el §8.
- **Related:** ADR-001 §2, ADR-003 §2, §3 y §4, ADR-013, ADR-014, ADR-019, ADR-020, ADR-021, ADR-022, ADR-023, ADR-015 §3, D-F2-11, F2-118 (#292), F2-119 (#293, datos: va aparte), F2-120 (#303)

## Context

El mantenedor quiere que Good Doggy CRM funcione en tres escenarios:

- **A. Independiente.** Una persona tiene una cuenta del CRM e inicia sesión directamente en él, con correo y contraseña y segundo factor (ADR-003, ADR-023). Es lo único que existe hoy. Las cuentas nacen con el alta de una organización o con una invitación (ADR-020); no hay registro propio.
- **B. Federado.** Una persona inicia sesión con su identidad de BlackDogStorePeru.com, cuando exista un proveedor de identidad confiable y compatible.
- **C. Vinculado.** Una persona con cuenta del CRM conecta su identidad de la tienda y conserva sus organizaciones, roles, permisos y datos.

La vinculación es opcional. El CRM no puede depender de la disponibilidad de la tienda.

### Lo que se verificó de la tienda (2026-10-22)

Investigación pasiva y de solo lectura: peticiones públicas al sitio y lectura del código de la tienda, que es del propio mantenedor. Los detalles internos no se copian aquí porque este repositorio es público. El informe completo es del mantenedor y se queda fuera del repositorio.

- **Verificado:** la tienda es una aplicación propia (Next.js y Django). Sus usuarios entran con usuario o correo y contraseña, y la sesión es un token interno de la propia tienda.
- **Verificado:** no publica un proveedor de identidad estándar. `/.well-known/openid-configuration`, `/.well-known/oauth-authorization-server` y `/.well-known/jwks.json` responden 404. No hay emisor, claves públicas ni registro de clientes. No hay OIDC, SAML ni login social.
- **Hipótesis:** como el código es del mantenedor, la vía realista es añadir a la tienda un proveedor OIDC, o poner un proveedor independiente delante de los dos sistemas. Esa decisión no se toma aquí.

**Conclusión: hoy no hay ningún proveedor al que el CRM pueda federarse.** Este ADR fija el modelo y el contrato, y no declara ninguna integración operativa.

OAuth 2.0 por sí solo no basta: autoriza a un cliente, pero no dice quién es la persona. Hace falta OIDC (un `id_token` firmado) o una aserción firmada equivalente.

## Decision

### 0. Decisiones del mantenedor (2026-10-22)

- **El CRM es independiente por defecto.** El escenario A es el normal y no depende de ningún proveedor.
- **La federación y la vinculación son opcionales.** Nadie está obligado a usarlas, y ninguna organización las tiene activas sin decidirlo (§9).
- **Primera población: empleados y operadores autorizados.** Son personas con una cuenta del CRM y una membresía activa que una organización les concedió. Las cuentas de personal de plataforma (`is_platform_staff`) no vinculan ni entran federadas hasta que otro ADR lo decida, por la misma razón que ADR-021 §6: tendrán el alcance de plataforma.
- **Un comprador de la tienda nunca se convierte en usuario administrativo del CRM** por tener una identidad en la tienda. El login federado no crea cuentas ni membresías. Si el registro federado se activa (§3), la cuenta nace sin membresías.
- **Nunca se vincula por correo** (§3).
- **Nunca se transfieren roles ni membresías** desde el proveedor (§1).
- **No hay federación real hasta que exista un proveedor OIDC compatible** (§8).

### 1. El usuario del CRM es la identidad principal

- `User` del CRM sigue siendo la persona. Sus membresías, roles y permisos son del CRM y solo los cambia el CRM.
- Una identidad externa es un **método más de iniciar sesión** de un `User`, nunca una fuente de permisos.
- **El proveedor nunca concede ni transfiere membresías ni roles.** Una identidad federada solo abre las organizaciones en las que ese `User` ya es miembro activo, y solo las que aceptan ese proveedor (§9).

### 2. Modelo

Una tabla nueva, `external_identities`, de plataforma (ADR-001 §2: no es de un tenant):

| Campo | Qué es |
|---|---|
| `id` | UUIDv7 |
| `user_id` | el `User` del CRM |
| `provider` | clave estable del proveedor configurado (por ejemplo `blackdogstore`) |
| `issuer` | el `iss` exacto del proveedor |
| `subject` | el `sub` del proveedor |
| `linked_at`, `last_login_at` | fechas |
| `email_at_link` | el correo que declaró el proveedor al vincular, solo informativo |

- **Única por `(issuer, subject)`**: una identidad externa pertenece a un solo `User`.
- **Única por `(user_id, provider)`**: una persona tiene como mucho una identidad por proveedor.
- La persona se identifica por `(issuer, subject)`, **nunca por el correo**.

### 3. Nunca se vincula por correo

- Que el correo coincida no vincula nada, ni sugiere nada sin que la persona esté autenticada en los dos lados. Un correo no es un identificador del proveedor: puede repetirse, cambiar de dueño o declararse sin verificar, así que vincular por correo permitiría tomar cuentas ajenas.
- Un primer login federado sin identidad vinculada **no entra** en una cuenta existente con ese correo, y la respuesta no dice si esa cuenta existe. Con el registro federado apagado, que es el valor por defecto, dice que hay que entrar con una cuenta del CRM y vincular desde ahí (escenario C).
- **Registro federado**, apagado. Encenderlo pide una decisión propia del mantenedor: sirve sobre todo a personas que no son empleados, y la primera población son empleados y operadores (§0). Si se enciende, el CRM no crea la cuenta con el correo que declara el proveedor, aunque traiga `email_verified`. Manda un enlace de un solo uso a ese correo (ADR-019), con la misma respuesta exista o no una cuenta: si no existe, el enlace termina el alta, sin membresías, y la vincula a la identidad de ese login; si existe, el correo dice que se entre con ella y se vincule desde ahí. El enlace solo termina el alta en el navegador que empezó ese login federado (ligado a su sesión, como el `state`), y enseña qué identidad del proveedor se vincula; abierto en otro navegador no hace nada. Si no, quien declara en el proveedor el correo de otra persona le haría crear, con un clic en su propio buzón, una cuenta vinculada a la identidad del atacante. Así nadie ocupa el correo de otra persona con una identidad del proveedor que después entraría en la cuenta que esa persona recupere (ADR-021) o con la que acepte una invitación (ADR-020).
- **Tope de esos correos por dirección de correo**, como en la recuperación (ADR-021 §3): una dirección de correo recibe como mucho 5 enlaces de registro en 24 horas, con al menos 2 minutos entre dos, vengan de las direcciones de red que vengan. Se cuentan sobre la tabla de enlaces, cuenta todo enlace emitido aunque no se entregue, y dos peticiones simultáneas para la misma dirección de correo no pasan el tope. Pasado el tope no se envía nada, y la respuesta es la misma. Así nadie llena el buzón de otra persona desde un proveedor.
- **Dónde se cuentan.** Los enlaces pendientes de registro viven en una tabla propia, que nombra el work item que encienda el registro; como en ADR-021, se guarda el hash del enlace, nunca el enlace. Todavía no hay una fila de cuenta que bloquear, así que las peticiones de una misma dirección de correo se serializan con otro cerrojo, por ejemplo un advisory lock sobre el hash de la dirección, tomado antes de contar y de escribir. Esa tabla es de plataforma y se indexa por el hash de la dirección de correo canónica (D-F2-3), nunca por la dirección, como hizo ADR-021 §2 con quien aún no es usuario; el bloqueo es `pg_advisory_xact_lock` sobre ese hash.
- Una cuenta creada así **no tiene contraseña utilizable** ni segundo factor. Lo que ADR-023 protege con la contraseña actual (dar de alta y quitar el segundo factor, regenerar los códigos) y lo que este ADR protege con un acceso reciente (§4, desvincular) le queda cerrado hasta que ponga una por la recuperación (ADR-021). Un login federado no sustituye a la contraseña en esas operaciones: eso relajaría ADR-023 §4, §6 y §8. Y como un login federado pide un segundo factor confirmado del CRM (§5), esa cuenta no entra federada hasta tener contraseña y segundo factor.

### 4. Vincular (escenario C)

Solo desde una sesión del CRM, en «Seguridad de la cuenta»:

1. La cuenta **tiene un segundo factor confirmado en el CRM** (ADR-023). Sin él, no se vincula. La persona confirma con un acceso reciente: la contraseña y un código del segundo factor (step-up, ADR-023 §7).
2. El CRM inicia Authorization Code con PKCE (S256), con `state` y `nonce` de un solo uso ligados a la sesión, válidos pocos minutos, y pide que la persona se autentique de nuevo en el proveedor (`prompt=login`, `max_age=0`). Una sesión que el navegador ya tuviera abierta en el proveedor no basta: puede ser de otra persona, o de un atacante que la dejó abierta allí (login CSRF en el proveedor), y vincular esa identidad le daría la cuenta del CRM.
3. A la vuelta, el CRM comprueba la respuesta de autorización (§6, ataque de mezcla) y después valida el `id_token`: `alg` dentro de la lista asimétrica configurada para ese proveedor (nunca `none` ni HS*), firma con la clave del JWKS por `kid` (§6, claves), `iss` exacto, `aud` que contiene su `client_id` (y `azp` igual a él si hay varias audiencias), `exp` e `iat` con un margen de reloj corto, `nonce`, y `auth_time` posterior al inicio del flujo, con el mismo margen de reloj.
4. El CRM enseña qué identidad del proveedor va a vincular (el nombre o el correo que declara) y la persona lo confirma con un `POST` con CSRF. Hasta entonces no se escribe nada.
5. Si `(issuer, subject)` ya está vinculado a otro `User`, se rechaza sin decir a cuál.
6. Si no, se crea la fila, se audita, se rotan la sesión y su época como en el alta del segundo factor (ADR-023 §4), y se avisa por correo a la cuenta del CRM.

Así se prueba el control de las dos cuentas: la del CRM por la sesión reciente con segundo factor, y la externa por un código de autorización emitido tras autenticarse de nuevo en el proveedor (`auth_time`), con la identidad confirmada en pantalla.

### 5. Iniciar sesión federado (escenario B)

- **La página de login nunca llama al proveedor mientras se pinta.** El botón del proveedor sale de un estado de salud guardado en caché, que actualiza una tarea en segundo plano consultando el discovery y el JWKS. Si la última comprobación falló o es demasiado vieja, el botón no se enseña. El login local nunca espera al proveedor.
- El inicio de sesión comprueba la respuesta de autorización y valida el `id_token` como la vinculación (§4, paso 3), salvo `auth_time`: no pide autenticarse de nuevo en el proveedor.
- **Solo se completa en una cuenta con un segundo factor confirmado en el CRM.** Con `(issuer, subject)` vinculado a una cuenta activa con segundo factor, el `id_token` válido hace lo que hace la contraseña correcta en ADR-023 §5: no abre sesión, deja la marca de acceso a medias y pide el código. La marca guarda además el método (`external:<provider>`) y el `sid`, si lo hay. El segundo paso es el mismo, con su tope por cuenta. Al completarlo, la sesión se abre con las reglas de ADR-003: ID nuevo, época de la cuenta, `mfa_verified_at`, límites y auditoría, y hereda de la marca el método y el `sid`: una sesión federada nunca queda registrada como `password`.
- Sin segundo factor confirmado (nunca lo tuvo, o lo quitó, ADR-023 §8), el login federado se rechaza con la misma respuesta que una identidad sin vincular, y se audita con su motivo interno. La persona entra con su cuenta del CRM.
- El `amr`/`acr` del proveedor nunca sustituye al segundo factor del CRM ni anota `mfa_verified_at` (step-up, ADR-023 §7): aceptarlo cambiaría ADR-023 §5 y pediría otro ADR.
- La sesión guarda **cómo se entró**: `password` o `external:<provider>`, y el `sid` del proveedor si el `id_token` lo trae (§7). La política por organización (§9) y el cierre de sesión (§7) lo leen de ahí.

### 6. Casos

| Caso | Comportamiento |
|---|---|
| Proveedor caído o lento | El botón se oculta según el estado de salud en caché (§5). El login local y el segundo factor del CRM siguen. El discovery y el JWKS se guardan en caché con un tiempo acotado. |
| `kid` desconocido | Una sola descarga nueva del JWKS, con un límite de frecuencia por proveedor. Si la clave sigue sin estar, el token se rechaza. Un `kid` desconocido nunca provoca más descargas que ese límite. |
| Clave retirada del JWKS | Deja de aceptarse cuando vence el tiempo de la caché. Ese tiempo es acotado y configurable por proveedor. |
| Ataque de mezcla con varios proveedores | Cada proveedor tiene su propia `redirect_uri`. El estado del flujo guarda qué proveedor lo empezó, y una vuelta que llega por la `redirect_uri` de otro se rechaza. El parámetro `iss` de la respuesta de autorización (RFC 9207) tiene que ser exactamente el emisor de ese proveedor, y se comprueba antes de canjear el código. Si el proveedor anuncia `authorization_response_iss_parameter_supported`, una respuesta sin `iss` se rechaza, también con un solo proveedor. Con más de un proveedor configurado, el parámetro es obligatorio (§8). Una respuesta de error pasa la misma comprobación antes de enseñar nada. |
| Identidad revocada o cuenta deshabilitada en el proveedor | Deja de servir para logins nuevos (el proveedor no emite el token). Si el proveedor envía un logout token válido (§7), el CRM cierra todas las sesiones de la cuenta vinculada. La cuenta y la vinculación del CRM no se tocan. |
| Cuenta local ya existente con el mismo correo | No se mezcla (§3). |
| `(issuer, subject)` ya vinculado a otro `User` | Se rechaza sin decir a quién, y se audita. |
| Una persona con dos cuentas del CRM | Solo una puede tener esa identidad. Para pasarla a otra, primero se desvincula. |
| Desvincular | Desde una sesión con acceso reciente: borra la fila, audita, avisa por correo y cierra las sesiones de la cuenta (§7). |
| Último método de acceso | No se puede desvincular la identidad externa si la cuenta no tiene contraseña utilizable: antes hay que poner una, por la recuperación (ADR-021). Una cuenta nunca queda sin forma de entrar. |
| Segundo factor local y del proveedor | Manda el del CRM (§5). |
| Replay | `state` y `nonce` de un solo uso. El `id_token` caduca pronto. El código de autorización solo vale con su `code_verifier`. El `jti` de un logout token vale una vez (§7). |
| CSRF en la vuelta | El `state` ligado a la sesión que inició el flujo. Fuera de eso, CSRF como siempre (ADR-003 §3). |
| Sesión ajena o forzada en el proveedor al vincular | Autenticación nueva en el proveedor (`prompt=login`, `max_age=0`, `auth_time`) y confirmación en pantalla de la identidad que se vincula (§4). |

### 7. Revocación y cierre de sesión

**Revocar es avanzar la época de la cuenta.** El CRM no guarda qué sesiones se abrieron con cada identidad: no hay registros por sesión (E01-11). Revocar es avanzar la época de la cuenta (D-F2-11), que cierra **todas** sus sesiones, como hace ADR-023 §4. Así se hace mientras no existan registros por sesión. Cuando existan, otro work item puede cerrar solo las sesiones abiertas con esa identidad.

- **Al desvincular**, en la misma transacción que borra la fila. La sesión que desvincula sigue solo si se abrió con la contraseña y si la época que guardaba era la anterior a ese avance, leída con la fila de la cuenta bloqueada, como en ADR-023 §4. Si se abrió con la identidad que se desvincula, o si otra revocación llegó antes, se cierra también.
- **Con un logout token válido del proveedor** (back-channel logout), se avanza la época de la cuenta vinculada a `(iss, sub)`. La vinculación sigue: la persona puede volver a entrar si el proveedor la autentica.

**El logout token.** Llega a una ruta de plataforma exenta de CSRF y de sesión, como los webhooks (ADR-003 §3): se autentica por su firma. Se acepta solo si cumple todo esto, y se rechaza en cualquier otro caso:

- es un JWT firmado con una clave del JWKS del proveedor, con un `alg` de su lista asimétrica, y su `kid` sigue las reglas de §6;
- `iss` es exactamente el emisor del proveedor, y `aud` contiene el `client_id` del CRM;
- si la cabecera trae `typ`, es `logout+jwt`;
- `iat` está dentro de una ventana corta, con un margen de reloj corto, y `exp` está presente y no ha pasado;
- `jti` está presente y no se ha visto antes: se guarda hasta que la ventana de `iat` vence, y un `jti` repetido se rechaza;
- `events` contiene el miembro `http://schemas.openid.net/event/backchannel-logout`;
- trae `sub`. El estándar permite un logout token con solo `sid`, pero mientras no existan registros por sesión el CRM no puede atribuir un `sid` suelto a una cuenta: un logout token sin `sub` se rechaza, y el contrato pide `sub` (§8);
- **no** trae `nonce`. Un token con `nonce` es un `id_token` y se rechaza.

La ruta tiene un límite de frecuencia por proveedor. La respuesta no dice a qué cuenta afectó, ni si existía.

**Lo que se guarda para cerrar la sesión.** En la sesión del CRM, el proveedor y su `sid`, si el `id_token` lo trae. **Nunca** el `id_token`, el token de acceso, el de refresco ni el código de autorización.

**Cierre iniciado por el CRM** (RP-initiated logout). Al cerrar una sesión federada, el CRM cierra primero la suya. Después, si el proveedor lo ofrece, lleva al navegador a su `end_session_endpoint` con `client_id`, `logout_hint` con el `sid` guardado y un `post_logout_redirect_uri` exacto y registrado. Nunca manda un `id_token_hint` sacado de un `id_token` guardado. Si el proveedor no responde, la sesión del CRM ya está cerrada.

### 8. Contrato que debe cumplir un proveedor

Hoy ninguno lo cumple. El CRM no se federará con nada que no lo cumpla.

1. **OIDC Discovery** por HTTPS en `/.well-known/openid-configuration`, con un `issuer` exacto y estable.
2. **JWKS** con claves asimétricas (RS256, ES256 o EdDSA), con `kid`, rotables sin cortes: una clave nueva se publica antes de usarla, y una vieja sigue publicada mientras haya tokens válidos firmados con ella. **Nunca un secreto compartido**: con un secreto compartido, el CRM podría falsificar sesiones del proveedor.
3. **Authorization Code + PKCE (S256)**, con `state` y `nonce`, y soporte de `prompt=login` y `max_age`. Sin flujo implícito ni contraseña directa.
4. **El parámetro `iss` en la respuesta de autorización** (RFC 9207, `authorization_response_iss_parameter_supported`), también en las respuestas de error. Obligatorio si el CRM configura más de un proveedor. Si el proveedor lo anuncia, el CRM lo exige siempre.
5. **Registro de cliente**: un `client_id` propio del CRM, cliente confidencial (secreto o `private_key_jwt`), `redirect_uris` exactas por entorno y una por proveedor, y scopes mínimos (`openid email profile`).
6. **`id_token`** firmado con un algoritmo asimétrico, con `iss`, `aud`, `exp`, `iat`, `nonce` y `auth_time`, un `sub` estable, único en el emisor y nunca reasignado, y `sid` si el proveedor ofrece cierre de sesión.
7. **`email` y `email_verified`**, solo informativos.
8. **`amr`/`acr`** honestos, si el proveedor declara factores.
9. **Revocación**: cierre iniciado por el CRM con `client_id` y `logout_hint`, y, a ser posible, back-channel logout con logout tokens que traigan `sub` (§7).
10. **Disponibilidad**: el proveedor puede caer sin que el CRM deje de funcionar (§5, §6).

### 9. Por organización

- El proveedor se configura para todo el CRM, fuera del repositorio. Sin configurar, ninguna organización lo puede usar.
- **El inicio de sesión ocurre antes de elegir organización.** Cada organización decide si acepta el login federado de cada proveedor configurado. Por defecto no lo acepta.
- Se comprueba **al entrar a la organización**, en el mismo punto que la política del segundo factor (ADR-023 §9). Con una sesión federada (§5), una organización que no acepta ese proveedor no se abre; la respuesta lo dice con un código propio, para que la pantalla ofrezca entrar con la cuenta del CRM. Las demás organizaciones de la persona se abren si lo aceptan.
- Activarlo y desactivarlo es de alguien con un permiso sensible de esa organización, con step-up (ADR-023 §7), y queda en la auditoría de la organización. El permiso lo añade al catálogo el work item que lo implemente.
- En cualquier caso, la identidad federada solo da acceso a las membresías que el CRM ya concedió (§1).

### 10. Auditoría

Se registran en la auditoría de plataforma (ADR-013):

- `auth.identity.linked` y `auth.identity.unlinked`;
- `auth.identity.provider_logout`, al aceptar un logout token;
- el primer paso de un login federado, como `auth.login.mfa_required` con `method: "external:<provider>"`, y el segundo, como `auth.login.succeeded` o `auth.login.failed` con el método del código (ADR-023 §10) y `first_factor: "external:<provider>"`;
- los rechazos de validación (firma, `kid`, `iss`, `aud`, `nonce`, `jti` repetido, caducidad, identidad vinculada a otra cuenta, cuenta sin segundo factor), con un motivo interno que la respuesta no enseña.

Las acciones nuevas las añade al catálogo de acciones el work item que las implementa. Activar el proveedor en una organización va a la auditoría de esa organización (§9).

Nunca se guardan tokens, códigos ni secretos del proveedor. El secreto de cliente del CRM va cifrado en reposo (ADR-022) o en el entorno.

### 11. Lo que no cambia

- El login con contraseña, el segundo factor, la recuperación y las invitaciones siguen como dicen ADR-003, ADR-020, ADR-021 y ADR-023.
- Nada de este ADR relaja ADR-001, ADR-002, ADR-003, ADR-013 ni ADR-014. Lo que lo hiciera pide otro ADR: aceptar el segundo factor del proveedor, entrar federado sin segundo factor del CRM, o federar cuentas de personal de plataforma.

### 12. Slices, cuando exista proveedor

Cada uno es un work item pequeño (ADR-009):

1. La tabla `external_identities` y su modelo.
2. Configurar un proveedor y validar su discovery y su JWKS, con el estado de salud en caché (sin flujo todavía).
3. El flujo de vinculación (escenario C), con su pantalla.
4. El login federado (escenario B), con su botón y el segundo paso.
5. La activación por organización (§9).
6. Desvincular, y las reglas del último método de acceso.
7. El cierre de sesión y el logout token (§7).

Ninguno empieza hasta que un proveedor cumpla el §8. La integración solo se declara hecha tras un inicio de sesión real y seguro contra ese proveedor.

## Alternatives considered

- **Vincular automáticamente por correo.** Permite tomar cuentas ajenas: en un proveedor, un correo puede repetirse, cambiar de dueño o declararse sin verificar. Descartado.
- **Aceptar los tokens actuales de la tienda.** Son tokens internos de sesión, sin emisor, audiencia ni claves públicas: no están hechos para que un tercero los verifique. Descartado.
- **Que el proveedor sea la única forma de entrar.** El CRM dependería de la disponibilidad de la tienda, y su acceso dependería de la seguridad del proveedor. Descartado: el CRM conserva su login y pide siempre su propio segundo factor (§5).
- **Copiar membresías o roles desde la tienda.** Mezcla dos modelos de permisos y deja que otro sistema conceda acceso al CRM. Descartado.
- **Cerrar solo las sesiones abiertas con la identidad.** Necesita registros por sesión, que no existen (E01-11). Mientras tanto, cerrarlas todas por la época es más seguro que no cerrar ninguna. Aplazado.
- **Guardar el `id_token` para el cierre de sesión.** Es una credencial del proveedor guardada sin necesidad. Basta el `sid`. Descartado.
- **Un proveedor independiente delante de los dos sistemas** (Keycloak, Authentik o uno gestionado). Es compatible con este ADR: cumple el §8 igual que si la tienda tuviera su propio proveedor. Elegirlo es del mantenedor.

## Consequences

- El CRM sigue funcionando solo, con su login y su segundo factor, como hoy.
- Cuando exista un proveedor que cumpla el contrato, la federación y la vinculación entran por slices sin cambiar lo existente.
- La tienda tendría que añadir un proveedor OIDC con claves asimétricas, o los dos sistemas tendrían que apoyarse en un proveedor independiente.
- Desvincular o recibir un logout token cierra todas las sesiones de la cuenta, no solo las federadas, hasta que existan registros por sesión.
- Un login federado siempre pide el segundo factor del CRM. Ahorra la contraseña, no el código.

## Security implications

- La identidad se ancla en `(issuer, subject)` y la vinculación exige probar el control de las dos cuentas. Así se cierran la toma de cuentas por correo y las vinculaciones cruzadas.
- **Un proveedor comprometido podría firmar un `id_token` de cualquier persona vinculada.** Por eso vincular exige un segundo factor confirmado del CRM, y un login federado no se completa sin él: el proveedor sustituye a la contraseña, nunca al código.
- La caída o el compromiso del proveedor no abre organizaciones del CRM a nadie que no fuera ya miembro, ni organizaciones que no acepten ese proveedor (§9).
- Un proveedor comprometido sí puede cerrar las sesiones de las personas vinculadas con logout tokens. Es una molestia, no un acceso, y se corta quitando la configuración del proveedor.
- Los ataques de mezcla se cierran con una `redirect_uri` por proveedor y el `iss` de la respuesta de autorización (RFC 9207).
- Un `kid` desconocido no fuerza descargas sin límite del JWKS, y una clave retirada deja de valer al vencer la caché.
- No se guarda ningún token del proveedor: el `sid` sirve para cerrar la sesión y no da acceso a nada.
- El registro federado está apagado por defecto. Si se activa, no ocupa correos ajenos (§3) ni llena buzones (tope por dirección de correo).

## Operational implications

- Configuración por proveedor: `issuer`, `client_id` y el secreto de cliente fuera del repositorio, y una `redirect_uri` por proveedor y entorno.
- Una tarea en segundo plano comprueba el discovery y el JWKS y deja el estado de salud que lee la página de login.
- Caché del discovery y del JWKS con un tiempo acotado, un límite de descargas por `kid` desconocido, y alertas si fallan las validaciones.
- Una tabla o caché de los `jti` vistos, con caducidad.
- Antes del primer slice, el mantenedor elige el proveedor: un proveedor OIDC en la tienda o uno independiente.

# ADR-027: Identidad externa: cuentas independientes, federadas y vinculadas

- **Status:** Proposed
- **Date:** 2026-10-22
- **Deciders:** el mantenedor. El programa autónomo lo propone a petición suya (2026-10-22) y no lo implementa: toca la autenticación de ADR-003, y eso no lo decide el programa (ADR-015 §3).
- **Related:** ADR-003 §2, §3 y §4, ADR-013, ADR-014, ADR-022, ADR-023, ADR-015 §3, F2-118 (#292), F2-119 (#293, datos: va aparte)

## Context

El mantenedor quiere que Good Doggy CRM funcione en tres escenarios:

- **A. Independiente.** Una persona se registra e inicia sesión directamente en el CRM. Es lo único que existe hoy: correo y contraseña, con segundo factor (ADR-003, ADR-023).
- **B. Federado.** Una persona inicia sesión con su identidad de BlackDogStorePeru.com, cuando exista un proveedor de identidad confiable y compatible.
- **C. Vinculado.** Una persona con cuenta del CRM conecta su identidad de la tienda y conserva sus organizaciones, roles, permisos y datos.

La vinculación es opcional. El CRM no puede depender de la disponibilidad de la tienda.

### Lo que se verificó de la tienda (2026-10-22)

Investigación pasiva y de solo lectura: peticiones públicas al sitio y lectura del código de la tienda, que es del propio mantenedor. Los detalles internos no se copian aquí porque este repositorio es público. El informe completo es del mantenedor y se queda fuera del repositorio.

- **Verificado:** la tienda es una aplicación propia (Next.js y Django). Sus usuarios entran con usuario o correo y contraseña, y la sesión es un token interno de la propia tienda.
- **Verificado:** no publica un proveedor de identidad estándar. `/.well-known/openid-configuration`, `/.well-known/oauth-authorization-server` y `/.well-known/jwks.json` responden 404. No hay emisor, claves públicas ni registro de clientes. No hay OIDC, SAML ni login social.
- **Verificado:** la tienda no tiene segundo factor.
- **Verificado:** en la tienda el correo no identifica a una sola cuenta.
- **Hipótesis:** como el código es del mantenedor, la vía realista es añadir a la tienda un proveedor OIDC, o poner un proveedor independiente delante de los dos sistemas. Esa decisión no se toma aquí.

**Conclusión: hoy no hay ningún proveedor al que el CRM pueda federarse.** Este ADR fija el modelo y el contrato, y no declara ninguna integración operativa.

OAuth 2.0 por sí solo no basta: autoriza a un cliente, pero no dice quién es la persona. Hace falta OIDC (un `id_token` firmado) o una aserción firmada equivalente.

## Decision (propuesta)

### 1. El usuario del CRM es la identidad principal

- `User` del CRM sigue siendo la persona. Sus membresías, roles y permisos son del CRM y solo los cambia el CRM.
- Una identidad externa es un **método más de iniciar sesión** de un `User`, nunca una fuente de permisos.
- **El proveedor nunca concede ni transfiere membresías ni roles.** Una identidad federada solo abre las organizaciones en las que ese `User` ya es miembro activo.

### 2. Modelo

Una tabla nueva, `external_identities`, global (no es de un tenant):

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

- Que el correo coincida no vincula nada, ni sugiere nada sin que la persona esté autenticada en los dos lados. En la tienda el correo no identifica a una sola cuenta, así que vincular por correo permitiría tomar cuentas ajenas.
- Un primer login federado sin identidad vinculada **no entra** en una cuenta existente con ese correo. O crea una cuenta nueva del CRM sin membresías (si el mantenedor activa el registro federado), o pide entrar primero con la cuenta local y vincular desde ahí (escenario C).

### 4. Vincular (escenario C)

Solo desde una sesión del CRM, en «Seguridad de la cuenta»:

1. La persona confirma con un acceso reciente: la contraseña y, si lo tiene, el segundo factor (step-up, ADR-023 §7).
2. El CRM inicia Authorization Code con PKCE (S256), con `state` y `nonce` de un solo uso ligados a la sesión, válidos pocos minutos.
3. A la vuelta, el CRM valida el `id_token`: firma con la clave del JWKS por `kid`, `iss` exacto, `aud` igual a su `client_id`, `exp` e `iat`, `nonce`, y `email_verified` si se usa el correo para algo.
4. Si `(issuer, subject)` ya está vinculado a otro `User`, se rechaza sin decir a cuál.
5. Si no, se crea la fila, se audita, se rotan la sesión y su época como en el alta del segundo factor, y se avisa por correo a la cuenta del CRM.

Así se prueba el control de las dos cuentas: la del CRM por la sesión reciente, y la externa por el código de autorización emitido para esa persona.

### 5. Iniciar sesión federado (escenario B)

- El botón del proveedor solo aparece si el proveedor está configurado y su discovery responde.
- El inicio de sesión sigue los mismos pasos de validación que la vinculación. Con `(issuer, subject)` vinculado, abre sesión de ese `User` con las mismas reglas de ADR-003: rotación del ID, época de sesión, límites, auditoría `auth.login.succeeded` con `method: "external:<provider>"`.
- **Segundo factor:**
  - Si el `User` tiene segundo factor en el CRM, se pide igual (ADR-023 §5). El `amr` del proveedor no lo sustituye mientras el mantenedor no declare confiable ese proveedor para eso.
  - Si la política lo exige (ADR-023 §9), se exige igual.

### 6. Casos

| Caso | Comportamiento |
|---|---|
| Proveedor caído o lento | El botón se oculta o da un error claro. El login local y el segundo factor del CRM siguen. El discovery y el JWKS se guardan en caché con un tiempo acotado; una clave desconocida no se acepta por estar en caché. |
| Identidad revocada o cuenta deshabilitada en el proveedor | Deja de servir para logins nuevos (el proveedor no emite el token). Si el proveedor envía señales de cierre (back-channel logout), el CRM revoca las sesiones abiertas con esa identidad. La cuenta del CRM no se toca. |
| Cuenta local ya existente con el mismo correo | No se mezcla (§3). |
| `(issuer, subject)` ya vinculado a otro `User` | Se rechaza sin decir a quién, y se audita. |
| Una persona con dos cuentas del CRM | Solo una puede tener esa identidad. Para pasarla a otra, primero se desvincula. |
| Desvincular | Desde una sesión con acceso reciente: borra la fila, audita, avisa por correo y revoca las sesiones abiertas con esa identidad. |
| Último método de acceso | No se puede desvincular la identidad externa si la cuenta no tiene contraseña utilizable: antes hay que poner una, por la recuperación (ADR-021). Una cuenta nunca queda sin forma de entrar. |
| Segundo factor local y del proveedor | Manda el del CRM (§5). |
| Replay | `state` y `nonce` de un solo uso. El `id_token` caduca pronto. El código de autorización solo vale con su `code_verifier`. |
| CSRF en la vuelta | El `state` ligado a la sesión que inició el flujo. Fuera de eso, CSRF como siempre (ADR-003 §3). |

### 7. Contrato que debe cumplir un proveedor

Hoy ninguno lo cumple. El CRM no se federará con nada que no lo cumpla.

1. **OIDC Discovery** por HTTPS en `/.well-known/openid-configuration`, con un `issuer` exacto y estable.
2. **JWKS** con claves asimétricas (RS256, ES256 o EdDSA), con `kid`, rotables sin cortes. **Nunca un secreto compartido**: con un secreto compartido, el CRM podría falsificar sesiones del proveedor.
3. **Authorization Code + PKCE (S256)**, con `state` y `nonce`. Sin flujo implícito ni contraseña directa.
4. **Registro de cliente**: un `client_id` propio del CRM, cliente confidencial (secreto o `private_key_jwt`), `redirect_uris` exactas por entorno y scopes mínimos (`openid email profile`).
5. **`id_token`** con `iss`, `aud`, `exp`, `iat` y `nonce`, y un `sub` estable, único en el emisor y nunca reasignado.
6. **`email` y `email_verified`**, solo informativos.
7. **`amr`/`acr`** honestos, si el proveedor declara factores.
8. **Revocación**: logout iniciado por el CRM y, a ser posible, back-channel logout o introspección.
9. **Disponibilidad**: el proveedor puede caer sin que el CRM deje de funcionar (§6).

### 8. Por organización

- El mantenedor decide si un proveedor se activa para todo el CRM o por organización.
- En cualquier caso, la identidad federada solo da acceso a las membresías que el CRM ya concedió (§1).

### 9. Auditoría

Se registran en la auditoría de plataforma (ADR-013):

- `auth.identity.linked` y `auth.identity.unlinked`;
- `auth.login.succeeded` y `auth.login.failed` con `method: "external:<provider>"`;
- los rechazos de validación (firma, `iss`, `aud`, `nonce`, caducidad, identidad vinculada a otra cuenta), con un motivo interno que la respuesta no enseña.

Nunca se guardan tokens, códigos ni secretos del proveedor. El secreto de cliente del CRM va cifrado en reposo (ADR-022) o en el entorno.

### 10. Slices, cuando se acepte y exista proveedor

Cada uno es un work item pequeño (ADR-009):

1. La tabla `external_identities` y su modelo.
2. Configurar un proveedor y validar su discovery y su JWKS (sin flujo todavía).
3. El flujo de vinculación (escenario C), con su pantalla.
4. El login federado (escenario B), con su botón.
5. Desvincular, y las reglas del último método de acceso.
6. Las señales de revocación.

Ninguno empieza hasta que el mantenedor acepte este ADR y un proveedor cumpla el §7. La integración solo se declara hecha tras un inicio de sesión real y seguro contra ese proveedor.

## Alternatives considered

- **Vincular automáticamente por correo.** Permite tomar cuentas ajenas, sobre todo porque en la tienda un correo no identifica a una sola cuenta. Descartado.
- **Aceptar los tokens actuales de la tienda.** Son tokens internos de sesión, sin emisor ni audiencia. Verificarlos exigiría compartir el secreto que los firma, con lo que el CRM podría falsificarlos. Descartado.
- **Que el proveedor sea la única forma de entrar.** El CRM dependería de la disponibilidad de la tienda, y sería más débil que hoy, porque la tienda no tiene segundo factor. Descartado.
- **Copiar membresías o roles desde la tienda.** Mezcla dos modelos de permisos y deja que otro sistema conceda acceso al CRM. Descartado.
- **Un proveedor independiente delante de los dos sistemas** (Keycloak, Authentik o uno gestionado). Es compatible con este ADR: cumple el §7 igual que si la tienda tuviera su propio proveedor. Elegirlo es del mantenedor.

## Consequences

- El CRM sigue funcionando solo, con su login y su segundo factor, como hoy.
- Cuando exista un proveedor que cumpla el contrato, la federación y la vinculación entran por slices sin cambiar lo existente.
- La tienda tendría que añadir un proveedor OIDC con claves asimétricas, o los dos sistemas tendrían que apoyarse en un proveedor independiente.

## Security implications

- La identidad se ancla en `(issuer, subject)` y la vinculación exige probar el control de las dos cuentas. Así se cierran la toma de cuentas por correo y las vinculaciones cruzadas.
- El segundo factor del CRM no se rebaja por un login federado.
- La caída o el compromiso del proveedor no abre organizaciones del CRM a nadie que no fuera ya miembro. Un proveedor comprometido sí podría entrar como las personas vinculadas: por eso el segundo factor del CRM sigue pidiéndose.

## Operational implications

- Configuración por proveedor: `issuer`, `client_id` y el secreto de cliente fuera del repositorio, y `redirect_uris` por entorno.
- Caché del discovery y del JWKS con un tiempo acotado, y alertas si fallan las validaciones.
- Hacen falta preguntas del mantenedor antes del primer slice: quién entrará con la identidad de la tienda (personal, compradores o ambos), qué proveedor, y si la tienda tendrá segundo factor.

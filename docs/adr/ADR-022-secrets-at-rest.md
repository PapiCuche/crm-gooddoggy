# ADR-022: Cifrado en reposo de los secretos que la aplicación tiene que leer

- **Status:** Accepted
- **Date:** 2026-10-11
- **Deciders:** programa autónomo (ADR-015 §5). El mantenedor puede reemplazarlo.
- **Related:** ADR-002 §1.1, ADR-003 §4, ADR-012, ADR-013, [security-boundaries.md](../architecture/security-boundaries.md) §3 y §5, [04-precios-pipeline-seguridad-infra.md](../fase-0/04-precios-pipeline-seguridad-infra.md) §M.4, [02-modelo-de-datos.md](../fase-0/02-modelo-de-datos.md) (`user_mfa_devices`, `credentials`), decisión D-F2-6

## Context

Casi todos los secretos que guarda la aplicación se guardan como hash: la contraseña (Argon2id), el enlace de recuperación y el de invitación (SHA-256). Un hash basta cuando la aplicación solo tiene que comprobar que alguien presenta el secreto.

Hay secretos que la aplicación tiene que poder leer. El primero es el secreto TOTP de una cuenta (ADR-003 §4): para comprobar un código de seis cifras hay que calcularlo, y para calcularlo hace falta el secreto. Después vendrán las credenciales de IA y de canales (tabla `credentials`), que la aplicación usa para llamar a un tercero.

Lo que ya está decidido, y este ADR no cambia:

- `security-boundaries.md` §3 clasifica como **restringidos** los «secretos TOTP», la «KEK» y las credenciales, y fija su regla: «Cifrado (AES-256-GCM, KEK fuera de la BD) o hash; nunca en logs, auditoría, frontend, contexto LLM ni exportaciones».
- El mismo documento, §5, dice dónde vive la «KEK de credenciales»: en las «variables de entorno de web, worker, ws y beat, inyectadas por el gestor de secretos del hosting (en local: `.env` no versionado)», y que se rota «con `key_version`».
- El documento de seguridad de la fase 0, §M.4: la KEK «se rota añadiendo una KEK nueva y re-cifrando en background; la anterior se retira al terminar».
- ADR-002 §1.1: el job de migraciones no recibe «los secretos de runtime que no necesita (KEK de credenciales, …)».

Faltaba el diseño (D-F2-6, «Sin diseño»): qué se cifra exactamente con qué, qué forma tiene lo que se guarda, cómo se nombra y se valida la clave, qué pasa cuando falta y cómo se rota sin perder nada. Bloquea MFA (E01-03).

## Decision

### 1. Un único módulo cifra y descifra

`core.sealed` es el único sitio del backend que cifra o descifra un secreto en reposo. Ofrece `seal(purpose, owner, plaintext)`, `unseal(purpose, owner, sealed)` y `stale(sealed)`. Ningún otro módulo importa primitivas criptográficas para esto, ni conoce las claves.

No registra nada. Sus errores no llevan la clave, el texto en claro ni el valor cifrado.

### 2. El algoritmo y las claves

**AES-256-GCM**, con la biblioteca `cryptography` en la versión que ADR-012 verificó para Python 3.14 (50.0.1).

**Las claves maestras (KEK) llegan del entorno**, en el ajuste `SECRETS_KEKS`: una lista `versión:clave`, separada por comas. Cada clave son 32 bytes aleatorios en base64 para URL; cada versión, un entero de 1 a 65535. Ni una versión ni una clave se repiten. Un valor mal formado impide arrancar, y el error solo nombra la variable.

**La clave maestra no cifra datos.** De ella se deriva, con HKDF-SHA256, una clave por **propósito** (`mfa.totp` hoy), y esa es la que cifra. El propósito es una constante del código, no un dato. Así lo cifrado para un propósito no se abre con la clave de otro aunque compartan clave maestra, y añadir un propósito no pide otra variable de entorno.

**Cifra la clave de versión más alta; descifran todas las presentes.**

### 3. Lo que se guarda

Un valor cifrado es una secuencia de bytes con esta forma:

| Bytes | Contenido |
|---|---|
| 1 | Formato: `1` |
| 2 | Versión de la clave maestra, en big-endian |
| 12 | Nonce, aleatorio en cada cifrado |
| resto | El cifrado y su etiqueta de autenticación (16 bytes) |

El valor dice con qué clave se cifró: no depende de otra columna para poder abrirse. Una tabla que quiera encontrar por consulta lo que quedó cifrado con una clave antigua puede guardar además esa versión en una columna propia, como prevé `credentials.key_version`.

El texto en claro tiene un máximo de 4096 bytes: el módulo es para secretos, no para documentos.

### 4. Atado a su propósito y a su dueño

Los datos asociados de GCM (autenticados, no cifrados) son la cabecera del valor (formato y versión), el propósito y el **dueño**: un identificador estable de la fila a la que pertenece el secreto, que da quien llama (`user:<id>`). No es secreto y no se guarda dentro del valor.

Consecuencia: un valor cifrado copiado a la fila de otra cuenta, presentado como de otro propósito o con un byte alterado **no se abre**. `unseal` lanza `SealError`, sin decir cuál fue el motivo. Quien llama trata ese secreto como si no existiera.

### 5. Rotación

1. Se añade a `SECRETS_KEKS` una clave con una versión más alta y se despliega. Desde ese momento todo lo nuevo se cifra con ella; lo anterior se sigue abriendo.
2. Un trabajo de mantenimiento vuelve a cifrar lo que `stale` señala (lo cifrado con una versión que no es la vigente). Ese trabajo llega con la primera tabla que guarde secretos; este ADR fija la regla, no lo construye.
3. Cuando no queda nada cifrado con la clave anterior, se quita de `SECRETS_KEKS`.

Quitar una clave antes de terminar el paso 2 **no corrompe nada, pero deja ilegible** lo que aún dependía de ella: se abre de nuevo si la clave vuelve. Para un secreto TOTP eso significa que la cuenta tiene que volver a dar de alta su segundo factor.

Una clave comprometida se rota igual, y además los secretos que protegía se dan por expuestos: volver a cifrarlos no los cambia. Qué hacer con ellos (obligar a re-enrolar el TOTP, rotar la credencial del proveedor) es del módulo dueño de cada secreto.

### 6. Sin clave

- **`production` y cualquier otro entorno:** sin `SECRETS_KEKS`, `seal`, `unseal` y `stale` lanzan `ImproperlyConfigured`. Falla cerrado: no se guarda ningún secreto en claro ni con una clave inventada. La aplicación arranca: hasta que exista una función que use el módulo, la variable no es obligatoria. El work item que active MFA decide si, con MFA exigida, la falta de clave impide arrancar.
- **`local` y `test`:** sin `SECRETS_KEKS` derivan una clave de desarrollo de su `SECRET_KEY`, para que el cifrado funcione sin configurar nada. **Esa clave no protege nada:** la `SECRET_KEY` por defecto de esos entornos está en el repositorio. Si se da `SECRETS_KEKS`, manda la del entorno. `production` no hereda de esos ajustes.
- **El job de migraciones** no guarda las claves aunque se las den (ADR-002 §1.1).

### 7. Lo que no decide

- Qué tablas guardan secretos y con qué dueño: cada módulo, en su work item.
- Guardar secretos fuera de la base de datos (Vault, un gestor del proveedor de nube): el modelo de datos lo prevé para `credentials` (`store`, `credential_reference`); será otra decisión, en su fase.
- El `fingerprint` de una credencial (un HMAC para detectar reutilización sin descifrar): llega con `credentials`.
- Los códigos de respaldo de MFA: ADR-003 §4 ya dice que se guardan como hash.

### 8. Lo que el mantenedor puede querer cambiar

El nombre y el formato de `SECRETS_KEKS`; que la clave de desarrollo se derive de la `SECRET_KEY` local en vez de exigir configurarla; derivar una clave por propósito en vez de cifrar con la KEK o de envolver una clave por fila; el máximo de 4096 bytes.

## Alternatives considered

- **Cifrar con la clave maestra directamente.** Más simple, pero todo lo que comparta clave se puede abrir como cualquier otra cosa si alguien confunde dos columnas, y una clave por uso pediría una variable de entorno por uso.
- **Una clave de datos por fila, envuelta con la KEK (envelope).** Es lo que «KEK» significa en sentido estricto. Abarata rotar la KEK cuando los datos son grandes, porque solo se vuelve a envolver la clave de cada fila. Aquí los datos son secretos de decenas de bytes: volver a cifrarlos cuesta lo mismo que volver a envolver su clave, y cada valor llevaría 60 bytes más. Se conserva el nombre KEK porque es el que usan los documentos de arquitectura y quien despliega.
- **Fernet** (de la misma biblioteca). Es AES-128-CBC con HMAC: no es el AES-256-GCM que fija `security-boundaries.md` §3, y no admite datos asociados, así que no ata el valor a su dueño.
- **`pgcrypto` en la base de datos.** La clave viajaría en cada consulta y podría acabar en los registros de PostgreSQL; y la regla es «KEK fuera de la BD».
- **Derivar las claves de `DJANGO_SECRET_KEY` también en producción.** Ata la rotación de dos secretos con ciclos distintos: rotar la clave de sesiones dejaría ilegibles todos los secretos.
- **Exigir `SECRETS_KEKS` para arrancar desde ahora.** Rompería despliegues que todavía no usan nada de esto; lo decide el work item que active la primera función que lo necesite.
- **Un nonce derivado o un contador.** Un contador pide estado compartido entre procesos. Con un nonce aleatorio de 96 bits, el límite recomendado es de 2³² cifrados por clave: muy por encima de lo que un CRM cifra con una clave por propósito.

## Consequences

- MFA (E01-03) queda desbloqueada en lo que toca al cifrado: la tabla de dispositivos guardará el secreto TOTP con `seal("mfa.totp", "user:<id>", …)`.
- Aparece una dependencia nueva en el backend, `cryptography`, que ADR-012 ya había verificado.
- Aparece un secreto de despliegue nuevo, `SECRETS_KEKS`, con su procedimiento de rotación. Perderlo deja ilegible todo lo cifrado con él: no hay recuperación.
- Una copia de seguridad de la base de datos ya no basta para leer un secreto TOTP: hace falta además la clave, que no está en ella. A la inversa, **restaurar una copia exige conservar las claves con las que se cifró**.
- En `local` y `test` los secretos cifrados no están protegidos de quien tenga el repositorio.
- El formato lleva un byte de versión: cambiar el algoritmo o la derivación será un formato `2`, y `stale` ya señala como pendiente lo que no es del formato vigente.

## Security implications

- **Quien lee la base de datos no lee los secretos**, salvo que tenga también el entorno de un proceso de runtime. Es el control que pide `security-boundaries.md` §3.
- **Quien puede escribir en la base de datos no puede mover un secreto de una cuenta a otra** ni fabricar uno: el valor está autenticado y atado a su dueño. Sí puede borrarlo o estropearlo, y eso se ve como un secreto que no existe.
- **Un proceso de runtime comprometido lee las claves** de su entorno y, con ellas, todo. Este diseño no lo impide; lo impediría un gestor externo con auditoría de accesos (§7).
- **El dueño tiene que ser estable y único.** Si quien llama usa como dueño algo que cambia o que se reutiliza, el atado no vale. Es responsabilidad del módulo que llama, y la revisión de cada work item que use `seal` lo comprueba.
- **El módulo no audita** (ADR-013): cifrar y descifrar no son hechos de negocio. Lo que se audita es la operación que los usa (dar de alta un segundo factor, usar una credencial).
- **Ni las claves ni los secretos van al log ni al reporte de errores.** El nombre del ajuste contiene `SECRET`, así que las vistas de depuración de Django lo ocultan; el reporte de errores no envía variables locales (`core.observability.reporting`, ADR-011).
- No debilita ADR-001, ADR-002, ADR-003, ADR-013 ni ADR-014: no hay tablas, rutas ni sesiones nuevas, y el job de migraciones sigue sin recibir secretos de runtime.

## Operational implications

- **Generar una clave:** 32 bytes aleatorios en base64 para URL, por ejemplo `python3 -c "import base64, secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())"`. Se guarda en el gestor de secretos del hosting, nunca en el repositorio.
- **Quién la recibe:** web, worker, ws y beat. El job de migraciones, no.
- **Rotar:** los tres pasos del §5. El runbook de rotación de secretos (`docs/runbooks/`) los recogerá cuando exista el trabajo que vuelve a cifrar.
- **Copias de seguridad:** las claves se respaldan aparte de la base de datos, y se conservan mientras exista una copia cifrada con ellas.

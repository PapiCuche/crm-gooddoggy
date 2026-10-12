# ADR-024: Tope de códigos fallidos por alta de un dispositivo TOTP

- **Status:** Accepted
- **Date:** 2026-10-11
- **Deciders:** programa autónomo (ADR-015 §5). El mantenedor puede reemplazarlo.
- **Related:** ADR-023 §4, §5 y §6 (lo amplía en §4), ADR-003 §4, ADR-013 §5, ADR-015 §3 y §5

## Context

ADR-023 §4 dejó escrito que los intentos de confirmar un alta «cuentan en el límite por dirección del acceso; no en el tope por cuenta del segundo paso (§5), porque aquí ya hay una sesión y la contraseña».

Eso es cierto de quien **empieza** el alta, que presenta la contraseña. No lo es de quien la **confirma**: la ruta que confirma solo pide la sesión y un código. Mientras un alta está empezada (hasta 10 minutos), quien tiene una sesión robada de esa cuenta, y no la contraseña, puede probar códigos contra ella.

- El límite por dirección no lo para si tiene muchas direcciones: con la cuenta «caliente», cada dirección tiene un intento (D-F2-9), y tres códigos de un millón valen a la vez (ADR-023 §2).
- Si acierta, la respuesta le entrega los diez códigos de respaldo, y su sesión queda con el segundo factor verificado. No conoce el secreto del dispositivo, pero no lo necesita: un código de respaldo pasa el segundo paso del acceso, el step-up, y sirve para quitar el factor (ADR-023 §5 a §8, cuando existan).

La revisión de F2-100 lo anotó como deuda, a cerrar antes de que ninguna ruta acepte un código de respaldo. Hoy ninguna lo acepta.

## Decision

### 1. Un tope por alta

Cada dispositivo sin confirmar admite **como mucho 5 códigos que no valen**. El quinto **borra el alta**.

- El fallo se guarda en la fila del dispositivo (`failed_codes`), con esa fila bloqueada, en la transacción que comprueba el código: dos peticiones simultáneas suman dos fallos, y no importa de qué dirección viene cada una.
- Cuenta todo lo que la ruta responde como «el código no vale»: un código incorrecto, uno de fuera de la ventana, algo que no son seis dígitos, y un secreto que no se abre (ADR-023 §3).
- La respuesta al quinto fallo es **la misma** que a los cuatro anteriores. La petición siguiente ya no encuentra un alta empezada (409 `MFA_ENROLLMENT_NOT_STARTED`), como si hubiera caducado.
- **Empezar otra alta pide la contraseña** (ADR-023 §4, paso 1), y la nueva empieza sin fallos. Quien solo tiene la sesión no puede empezarla.
- Al confirmar, el dispositivo queda sin fallos anotados. La base de datos no deja guardar el tope en un alta ni fallos en un dispositivo confirmado.

Con 5 intentos por alta, la probabilidad de acertar es de 15 entre un millón, y cada serie nueva exige que la dueña, con su contraseña, empiece otra alta.

### 2. Lo que no cambia

- El límite por dirección del acceso sigue contando estos intentos, como dice ADR-023 §4.
- El tope por cuenta del segundo paso (ADR-023 §5) **sigue sin contarlos**. Compartirlo dejaría a la dueña sin segundo paso durante 24 horas justo después de confirmar, si alguien hubiera fallado códigos contra su alta.
- La fila de auditoría de un código que no vale es la de ADR-023 §10 (`auth.mfa.enabled`, denegada, escrita fuera de la transacción: si no se puede escribir, la respuesta no cambia). Su motivo interno distingue el fallo que agotó el alta.

## Alternatives considered

- **Contar en el tope por cuenta del segundo paso.** Véase §2: castiga a la dueña después de confirmar.
- **Bloquear el alta durante un tiempo en vez de borrarla.** Hace falta otra columna y otra regla de caducidad, y el alta ya caduca a los 10 minutos: bloquearla es casi lo mismo que borrarla, con más estado.
- **Pedir la contraseña también al confirmar.** Cierra el caso, pero cambia el contrato de una ruta ya publicada y obliga a la pantalla a guardar la contraseña entre los dos pasos del alta.
- **Guardar el contador fuera de la fila** (en `login_throttles`). No se borra con el alta, hay que purgarlo aparte, y no queda bajo el bloqueo de la fila del dispositivo.

## Consequences

- `user_mfa_devices` gana la columna `failed_codes` y la restricción `user_mfa_devices_failed_codes_ck`.
- Quien se equivoca cinco veces al confirmar su propia alta tiene que empezar otra, con su contraseña y un secreto nuevo en la aplicación de autenticación. La pantalla del alta tiene que llevar de un 409 `MFA_ENROLLMENT_NOT_STARTED` a empezar de nuevo.
- Quien tiene una sesión robada puede **agotar** un alta que la dueña empezó, fallando cinco códigos. La dueña empieza otra; mientras la sesión robada siga abierta puede repetirlo. La salida es la de ADR-023: cambiar la contraseña con un enlace de recuperación cierra esa sesión.

## Security implications

- Solo endurece: añade un límite que no depende de la dirección a una ruta que no tenía ninguno así. No relaja ADR-003 ni ADR-023, ni toca ADR-001, ADR-002, ADR-013 o ADR-014.
- El contador no guarda ningún código ni nada derivado de uno: solo cuántos fallaron.
- No cubre a quien tiene la sesión **y** la contraseña: ese empieza un alta suya y conoce el secreto. Es el caso que ADR-023 ya describe en *Security implications*.

## Operational implications

- Una migración que añade una columna con valor por defecto y una restricción a una tabla pequeña.
- Sin ajustes nuevos: el tope es una constante del código (`ENROLLMENT_FAILURES`), junto a la restricción que lo usa.

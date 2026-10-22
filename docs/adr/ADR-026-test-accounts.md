# ADR-026: Accesos de prueba en entornos aislados

- **Status:** Accepted
- **Date:** 2026-10-22
- **Deciders:** el mantenedor lo pidió el 2026-10-22; el programa autónomo lo escribe (ADR-015 §5). El mantenedor puede reemplazarlo.
- **Related:** ADR-003, ADR-013, ADR-017, ADR-018, ADR-021, ADR-023 §9, ADR-015 §3 y §5, F2-116 (#290), F2-117 (#291)

## Context

El mantenedor quiere ver y probar cada funcionalidad nueva con perfiles distintos sin crear cuentas a mano. Pidió:

- cuentas sintéticas con roles reales (Owner, Administrador, Supervisor y Vendedor);
- un comando idempotente que las provisione con su organización;
- una sección en el login que las enseñe y complete el correo.

Todo eso solo en entornos de desarrollo o demostración aislados, nunca en producción, con una configuración explícita (no solo `NODE_ENV`), sin atajos de autenticación y sin desactivar MFA, CSRF ni RBAC.

Hoy no hay forma de añadir una membresía sin invitación: `create_organization` solo crea la del Owner inicial, y aceptar una invitación espera D-F2-14. Además, ADR-021 y el login no dicen nunca si una cuenta existe.

## Decision

### 1. Un solo interruptor, en el backend

- El setting `TEST_ACCOUNTS_ORGANIZATION` (variable `DJANGO_TEST_ACCOUNTS_ORGANIZATION`) nombra el slug de la organización de pruebas.
- Vacío significa desactivado, y es el valor por defecto.
- Con `production`, la aplicación **no arranca** si tiene valor (`ImproperlyConfigured`), como con `DEBUG`.
- El frontend no tiene interruptor propio: pinta la sección solo si la ruta del backend (F2-117) responde con cuentas. Así no hay dos configuraciones que puedan contradecirse, y una variable de compilación del frontend no puede encenderla.

### 2. Cuentas sintéticas, con roles de plantilla

- Cuatro cuentas, `owner@`, `administrador@`, `supervisor@` y `vendedor@` en el dominio `<slug>.gooddoggy.test`. `.test` está reservado (RFC 6761): no puede ser el correo de nadie.
- Cada una tiene una membresía activa en la organización de pruebas y el rol de plantilla de su nombre (ADR-018). Sus permisos son los de esa plantilla: ni más ni menos.
- Entran por el login normal, con contraseña, límites, CSRF y segundo paso si lo activan. Nada se salta.

### 3. El comando `provision_test_accounts`

- Es idempotente. Crea lo que falta y deja lo que ya está.
- Solo funciona con el setting activo y sobre su slug.
- Si la organización no existe, la crea por el alta controlada de siempre (`bootstrap_organization`), con la cuenta `owner@` como Owner inicial.
- Si existe, solo sigue si su Owner es la cuenta `owner@` sintética. Si no, se niega sin cambiar nada: nunca convierte una organización real en una de pruebas.
- La contraseña de las cuentas nuevas se pide sin mostrarla, o llega por la entrada estándar. Pasa la validación de contraseñas. Las cuentas que ya existen conservan la suya. Nunca se imprime, se registra ni se audita.
- Queda auditado (ADR-013): `test_accounts.provision.started` y `test_accounts.provisioned` en la auditoría de plataforma, con el operador y el motivo; cada membresía que añade, `membership.activated`, y cada rol, `membership.role_assigned`, en la del tenant. Si la organización es nueva, su alta deja además las filas de siempre.

### 4. La única membresía sin invitación

- Añadir una membresía directa es una función de la frontera de alta de `organizations` (`bootstrap.py`). Solo la importa `apps.provisioning`, con contrato de import-linter, como el resto de esa frontera.
- La función se niega con cualquier organización que no sea la del setting.
- Dar el rol de plantilla es una función de la frontera de alta de `access`. Busca el rol por ser de plantilla y por su código, no por su nombre. Solo **añade** el rol que la plantilla define: no autoriza nada por el nombre.

### 5. La lista del login (F2-117)

- Una ruta pública enseña el correo y el rol de las cuentas sintéticas que existen y están activas en la organización de pruebas. Sin contraseñas, tokens ni nada más.
- Con el setting vacío responde 404, como una ruta que no existe.
- Es una excepción **acotada** a «no decir si una cuenta existe» (ADR-021): solo para las cuentas del dominio `.test` de la organización de pruebas, que son públicas por definición. Fuera de eso, el login y la recuperación siguen sin decir nada.

## Alternatives considered

- **Un interruptor en el frontend (`NEXT_PUBLIC_…` o `NODE_ENV`).** Se compila en la imagen y no sabe qué cuentas existen. El mantenedor pidió no depender solo de `NODE_ENV`. Descartado.
- **Iniciar sesión con un clic, o contraseñas en pantalla.** Sería un atajo de autenticación. Descartado: el panel solo completa el correo.
- **Crear las cuentas con SQL o con fixtures.** Se saltan las validaciones, la auditoría y RLS. Descartado.
- **Esperar a las invitaciones (D-F2-14).** Bloquea lo que el mantenedor quiere ya, y las invitaciones no darían cuentas sintéticas idempotentes. Descartado.

## Consequences

- Cualquier entorno de pruebas puede tener, con un comando, una organización con los cuatro perfiles.
- Aparece una forma de añadir membresías sin invitación, limitada a la organización de pruebas y al comando.
- Los tests de cada perfil en el navegador pueden usar estas cuentas en lugar de crear datos a mano.

## Security implications

- El riesgo principal es que el interruptor llegue a producción. `production` no arranca con él, y la ruta y el comando comprueban el mismo setting.
- La lista revela cuentas, pero solo las sintéticas de un dominio reservado. Sus contraseñas no salen por ningún sitio.
- Las cuentas tienen permisos reales. En un entorno compartido, quien conozca la contraseña entra con ese rol. Por eso el entorno debe estar aislado y la contraseña la elige el operador.
- El comando no puede tocar una organización real: comprueba que su Owner es la cuenta sintética antes de añadir nada.
- No cambia MFA, CSRF, RBAC, RLS ni los límites de intentos.

## Operational implications

- Para encenderlo en local, se pone `DJANGO_TEST_ACCOUNTS_ORGANIZATION=pruebas` en `infra/env/.env`, se ejecuta `make up` y después el comando.
- Para apagarlo, se vacía la variable. Las cuentas quedan en la base, pero la lista deja de enseñarlas. Se pueden suspender o borrar como cualquier otra.

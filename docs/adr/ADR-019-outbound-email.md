# ADR-019: Correo saliente por un único módulo, sin proveedor fijado

- **Status:** Accepted
- **Date:** 2026-10-07
- **Deciders:** programa autónomo (ADR-015 §5). El mantenedor puede reemplazarlo.
- **Related:** ADR-011, ADR-012, D-F2-5 en [phase-2.md](../phases/phase-2.md), E01-06 y E01-02 en [05-backlog-y-roadmap.md](../fase-0/05-backlog-y-roadmap.md)

## Context

Dos historias de la Fase 2 necesitan enviar un correo: las invitaciones (E01-06) y la recuperación de contraseña (E01-02). Las dos llevan un enlace de un solo uso. Hasta ahora el producto no envía ninguno: el alta de una organización pide la contraseña del Owner al operador, y nadie más puede entrar.

El entorno local tiene Mailpit desde la Fase 0.5, pero Mailpit es una herramienta para ver correos en desarrollo, no el diseño del envío (D-F2-5). Faltaba decidir por dónde sale el correo, desde qué proceso, qué queda escrito de él y qué exige producción. No hace falta decidir todavía qué proveedor lo entrega: esa elección tiene coste y contrato, y es del mantenedor.

## Decision

### 1. Un único módulo: `core.mail`

Todo el correo saliente pasa por `core.mail.send(Message)`. Ningún otro módulo importa `django.core.mail` ni `smtplib`; un test lo impide, como el de `core.http` con los clientes HTTP (import-linter trata los paquetes externos por su raíz y no distingue `django.core.mail` del resto de Django).

`Message` lleva un destinatario, un asunto, un cuerpo de texto plano y un propósito (`invitation`, `password_reset`…), que es una etiqueta para el log y las métricas. No hay HTML, adjuntos, copias ni varios destinatarios: se añaden con el primer correo que los necesite.

`send` valida antes de construir nada: una sola dirección con forma de dirección, sin saltos de línea ni separadores en la dirección ni en el asunto, y un propósito con forma de identificador. La dirección llega en ASCII y tal como va a salir: sin comillas ni palabras codificadas (`=?…?=`), que el transporte reescribiría después de validar (`"ana"@x.pe` y `=?utf-8?b?YW5h?=@x.pe` saldrían como `ana@x.pe`), y con un dominio internacionalizado ya en su forma IDNA, la canónica de D-F2-3. Lo que no cumple es un error del llamador (`ValueError`), no un intento de entrega, y su texto no repite lo recibido.

### 2. SMTP, sin proveedor fijado

El transporte es el backend SMTP de Django, configurado por entorno: servidor, puerto, credenciales, cifrado y remitente. Cualquier proveedor de correo transaccional ofrece SMTP, así que el despliegue elige uno sin cambiar el código, y cambiarlo después es cambiar variables. No se añade ninguna dependencia (ADR-012 no cambia).

Si un proveedor obliga a usar su API HTTP, se implementa otro backend detrás de `core.mail` y esa llamada sale por `core.http`. El contrato de `send` no cambia.

### 3. Se envía desde una tarea, nunca dentro de una petición

Una petición HTTP no abre una conexión SMTP: bloquearía al usuario, y un fallo del servidor de correo se convertiría en un error de su acción. Quien necesita un correo registra el hecho en su transacción (una fila propia, un evento del outbox) y una tarea de Celery lo envía después del commit. Un fallo de entrega lanza `MailError`, y la tarea reintenta con su política.

`core.mail` no reintenta ni encola: es síncrono y lo llama la tarea.

### 4. Los secretos de un correo no se guardan ni viajan por el outbox

Un enlace de un solo uso es una credencial. La tabla de quien lo emite guarda solo su hash (modelo de datos §E.2: `token_hash`). Para que el token en claro no quede en `outbox_events`, en el broker ni en un resultado de Celery, **la tarea que envía es la que genera el token**: guarda el hash, compone el correo y lo entrega, en ese orden y en un solo paso. Un reintento genera un token nuevo y deja sin valor el anterior.

Por eso `core.mail` recibe el mensaje ya compuesto y no ofrece una cola propia: una cola guardaría el cuerpo.

### 5. Qué queda escrito

El log de aplicación registra cada envío y cada fallo con el propósito y, en el lugar de la dirección, la marca `[EMAIL]` de `core.redaction`. No se usa `mask_emails`: su patrón es para texto libre y deja a la vista direcciones válidas como `{a}=b@x.pe`. Los campos van por nombre con el logger de `core.observability`; el `extra` de `logging` no llega a la salida JSON. Nunca el asunto ni el cuerpo. El texto de la excepción de SMTP tampoco se registra: puede repetir la dirección; se registra su tipo, y `MailError` no conserva la excepción original, tampoco como `__context__`. La auditoría del hecho de negocio (se invitó a alguien) es de quien lo origina, no de este módulo.

### 6. Producción

Sin servidor configurado (`EMAIL_HOST` vacío), `send` falla con un error de configuración: no hay envío silencioso a ninguna parte. Con servidor configurado, `production` no arranca si falta el cifrado (STARTTLS o TLS implícito, uno de los dos), el remitente o las credenciales. La conexión tiene un tiempo máximo.

El destino de la conexión es el servidor configurado por el despliegue, nunca un valor que llegue de un usuario.

### 7. Local y tests

El stack local envía a Mailpit, sin cifrado ni credenciales, dentro de la red del stack. Los tests usan el backend en memoria de Django.

## Alternatives considered

- **El SDK o la API HTTP de un proveedor concreto.** Fija un proveedor, y con él un coste, antes de que el mantenedor lo elija; añade una dependencia o un cliente propio. SMTP no cierra esa puerta (§2).
- **Guardar el correo compuesto en una tabla y enviarlo con un proceso aparte.** Da reintentos y trazabilidad, pero deja el cuerpo, con su enlace de un solo uso, escrito en la base. Se descarta mientras los únicos correos sean credenciales.
- **Enviar dentro de la petición.** Más simple, pero ata la respuesta al servidor de correo y permite medir por el tiempo de respuesta si una dirección existe.
- **Pasar el token por el outbox o como argumento de la tarea.** El token quedaría en `outbox_events` o en Redis hasta que se consuma.

## Consequences

- E01-06 y E01-02 pueden implementarse: cada una aporta su tabla, su tarea y su texto.
- El mantenedor elige el proveedor al desplegar, con variables de entorno. Hasta entonces producción no puede enviar correo, y lo dice al intentarlo.
- La entregabilidad (dominio remitente, SPF, DKIM, DMARC), los rebotes y las quejas no están resueltos: dependen del proveedor. Un correo que el servidor acepta y luego rebota no se detecta.
- No hay límite de envío en este módulo: lo pone cada flujo (cuántas invitaciones, cuántos restablecimientos).
- Un reintento de la tarea invalida el enlace del intento anterior. Si el primero sí llegó, ese enlace ya no vale; el segundo sí.
- Solo texto plano: el correo no lleva marca ni formato hasta que se decida añadir HTML.

## Security implications

- La dirección de destino es un dato personal: no va al log sin enmascarar.
- Inyección de cabeceras: se rechazan saltos de línea antes de construir el mensaje, además de la defensa de Django.
- Las credenciales SMTP llegan solo por entorno y no se registran.
- El transporte va cifrado en producción. El contenido del correo, una vez entregado al proveedor, queda fuera del control del sistema: por eso los enlaces son de un solo uso y caducan (lo definen E01-06 y E01-02).

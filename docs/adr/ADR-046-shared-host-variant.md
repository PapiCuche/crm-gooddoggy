# ADR-046: Variante de servidor compartido: el CRM detrás del borde HTTPS de otra aplicación

- **Status:** Accepted (2026-11-19, decisión del propietario en la sesión de despliegue: las seis recomendaciones de «Decisiones requeridas del propietario»; registrada en #992)
- **Date:** 2026-11-19
- **Deciders:** propietario del producto; redactado por el programa autónomo (ADR-015).
- **Work items:** F1-18a ([#992](https://github.com/PapiCuche/crm-gooddoggy/issues/992)), prerrequisito de F1-18 ([#760](https://github.com/PapiCuche/crm-gooddoggy/issues/760))
- **Related:** [ADR-033](ADR-033-production-hosting.md) (lo enmienda **solo** en el modo `shared`; el modo dedicado no cambia), [ADR-015](ADR-015-autonomous-delivery-program.md)

## Context

ADR-033 (Accepted) supone un servidor dedicado: el `proxy` del CRM publica 80 y 443, obtiene su certificado por ACME y es el borde (§2, §3, §4.1, §4.4).

El propietario quiere publicar el CRM en un servidor que **ya** sirve otra aplicación en producción. En ese servidor:

- Un Caddy de la otra aplicación ya publica 80 y 443 y termina TLS para su dominio.
- Delante del servidor hay una CDN con proxy inverso. El Caddy del borde solo cree la IP del cliente que manda la CDN, y solo desde los rangos publicados por la CDN (`trusted_proxies_strict`).
- El usuario que despliega la otra aplicación pertenece al grupo `docker`.

Dos procesos no pueden publicar el mismo puerto. Sustituir el Caddy de la otra aplicación por el del CRM no es aceptable: cambia su ruta de tráfico, sus cabeceras y sus límites. Un balanceador de capa 4 delante (SNI passthrough) exige un módulo nuevo en un Caddy que no es del CRM.

## Decision

Un **modo de servidor** declarado una vez por servidor en `/etc/crm/host-mode`: `dedicated` (o sin archivo), que es ADR-033 tal cual, o `shared`. Lo leen `deploy.sh`, `smoke.sh` y `backup/lib.sh` (`infra/prod/host-mode.sh`). Así despliegue, migraciones, volcados, restauración, prueba de humo y vuelta atrás usan siempre los mismos archivos de Compose. Es un archivo y no una variable porque `sudo` no pasa variables, y olvidarla intentaría publicar 80/443.

En modo `shared`, `infra/prod/compose.shared.yaml` se aplica encima de `compose.yaml` y cambia solo esto:

1. **`proxy` no publica puertos.** El borde público es el Caddy de la otra aplicación: termina TLS para `crm.<dominio>` y reenvía por la red `ingress`.
2. **`ingress`** es una red Docker externa e interna (sin salida a Internet), creada una vez por el operador, con dos miembros y dos IP fijas: el borde público y el `proxy` del CRM. Ningún `down` de ningún proyecto la borra. El CRM no entra en ninguna red de la otra aplicación, ni la otra aplicación en las del CRM.
3. **`proxy` monta `Caddyfile.shared`:** `auto_https off`, sitio `http://crm.{$CRM_DOMAIN}` en `:80` y cualquier otro `Host` se corta. Mismas rutas, topes, tasa y redacción que `Caddyfile`; `validate.sh` lo comprueba.
4. **IP del cliente (enmienda §4.4).** Cadena: CDN → borde público → `proxy` → uvicorn.
   - El borde público solo cree `CF-Connecting-IP` desde los rangos de la CDN. Desde cualquier otra conexión, usa la del socket. Sustituye `X-Forwarded-For` por esa IP.
   - El `proxy` del CRM solo cree `X-Forwarded-For` si la conexión viene de la IP fija del borde (`trusted_proxies static {$CRM_EDGE_PROXY_IP}` + `trusted_proxies_strict`). La tasa de `/webhooks/*` cuenta por `{client_ip}`, no por `{remote_host}`.
   - El `proxy` sustituye `X-Forwarded-For` por `{client_ip}` y fija `X-Forwarded-Proto https`. uvicorn sigue creyendo solo al `proxy` (`FORWARDED_ALLOW_IPS`), sin cambios.
   - Un cliente no puede elegir su IP en ningún tramo, y nunca comparten todos los clientes una dirección.
5. **TLS (enmienda §4.1).** El certificado de `crm.<dominio>` lo obtiene y renueva el borde público. HSTS, cookies `Secure` y CSRF siguen en Django y Next, sin cambios: el esquema llega como `https`.
6. **Topes de cuerpo en dos sitios.** El borde público repite la tabla del CRM y lee el cuerpo entero antes de reenviar (`request_buffers`). Si solo corta el `proxy` del CRM, el borde, que aún está enviando el cuerpo, responde 502 en vez de 413 (medido en laboratorio). El tope que protege al CRM sigue siendo el suyo; `smoke.sh` comprueba cada tope +1 byte, con y sin `Content-Length`, en cada despliegue.
7. **Subredes fijas** para `data` (`172.31.252.0/24`) y `egress` (`172.31.253.0/24`), y `ingress` en `172.31.250.0/29` con dos miembros. No dependen del entorno: cambiarlas es un PR en los dos repositorios a la vez. El runbook comprueba solapes antes de crearlas.
8. **`X-Forwarded-Host`** se fija al `Host` en los dos saltos. Caddy conserva el de un proxy de confianza, y la CDN pasa el del cliente: Next lo leería.
9. **Memoria del borde público:** guarda como mucho 1 MiB de cada cuerpo del CRM. Con el cuerpo entero en memoria, cientos de subidas lentas sin sesión llenarían el servidor y caería también la otra aplicación.
10. **Órdenes a mano:** `infra/prod/compose.sh` añade los mismos archivos que `deploy.sh`. Los runbooks lo usan. `smoke.sh`, en modo `shared`, falla si el proxy ve una IP privada como IP del cliente.

Lo demás de ADR-033 no cambia: servicios, imágenes por digest, roles `crm_app`/`crm_migrator`, archivos de entorno, límites de memoria, backups cifrados, RPO/RTO, `/health/*` 404.

### Verificación hecha

- `validate.sh --caddy`: el compose compartido no publica puertos, solo cambia el `proxy`, y `Caddyfile.shared` valida.
- `test-deploy.sh`, `test-smoke.sh`, `test-host-mode.sh` y shellcheck pasan.
- Revisión adversarial independiente (2026-11-19): ningún hallazgo crítico. Corregidos aquí: `X-Forwarded-Host`, la memoria del borde, las órdenes de los runbooks sin el override (paraban PostgreSQL), el orden del primer arranque, `host-mode` sin permiso de lectura y la IP privada en `smoke.sh`. Quedan para decisión: IPv6 (6) y el firewall (5).
- Laboratorio local con los dos Caddy reales y upstreams de prueba (28 comprobaciones, 3 corridas): IP real en backend y frontend, `X-Forwarded-For`/`CF-Connecting-IP` falsos ignorados, `Host` y `https` intactos, tasa por cliente (un cliente limitado no limita a otro), WebSocket 101, `/health/ready` 404, `Host` desconocido cortado, topes 413 con y sin `Content-Length`, y ninguna query en los logs.

## Alternatives considered

| Opción | Ventajas | Inconvenientes |
| --- | --- | --- |
| **A. Variante compartida (esta)** | Un servidor; la otra aplicación conserva su ruta; el CRM conserva su proxy y sus reglas | Dos proxies; tabla de topes repetida; la seguridad del CRM depende de quién administra el servidor (ver abajo) |
| B. Servidor dedicado (ADR-033 tal cual) | Ningún cambio de contrato; aislamiento total; sin grupo `docker` ajeno | Otra máquina que pagar y operar |
| C. El `proxy` del CRM como borde de los dos | Una sola configuración de borde | Cambia la ruta, cabeceras y límites de la otra aplicación; un despliegue del CRM la afecta |
| D. Capa 4 (SNI passthrough) delante | Cada aplicación termina su propio TLS | Módulo nuevo en el borde de la otra aplicación; la IP del cliente necesita PROXY protocol en los dos lados |

## Security implications

- **ADR-033 §6.2 no se cumple en este servidor.** El usuario que despliega la otra aplicación está en el grupo `docker`, que equivale a `root`. Puede leer el entorno de los contenedores del CRM (`docker inspect`), sus secretos y su base de datos. Las opciones son: aceptarlo porque el propietario es el mismo y controla quién tiene SSH; sacar a ese usuario del grupo `docker` y desplegar la otra aplicación con `sudo docker`, lo que cambia sus `crontab` y scripts; o la opción B. **Decisión del propietario (1 y 3).**
- La red `ingress` es interna y solo tiene dos miembros, con IP fija. Un contenedor de la red del borde del CRM no puede elegir su IP: no es la del borde público.
- El borde público ve el tráfico del CRM en claro, entre TLS y el `proxy`. Lo administra la misma persona que tiene `root`.
- Un despliegue de la otra aplicación sin su override recrea su Caddy sin la red ni el sitio del CRM. La otra aplicación sigue bien; el CRM deja de responder (no expone nada) hasta repetirlo.
- Actualizar Docker reinicia los contenedores de las dos aplicaciones (ADR-033 §2 ya lo decía): las ventanas de mantenimiento son comunes.

## Operational implications

- Runbook: `docs/runbooks/production-shared-host.md`. El primer arranque sigue `production-first-boot.md`, con las diferencias que indica ese runbook.
- El cambio del borde público vive en el repositorio de la otra aplicación, en su propio PR. Ningún PR del CRM lo modifica.
- Mover el CRM a su propio dominio o a un servidor dedicado: cambiar `host-mode` a `dedicated` (o quitarlo), DNS, `CRM_DOMAIN`, `DJANGO_ALLOWED_HOSTS`, `DJANGO_CSRF_TRUSTED_ORIGINS`, `APP_ORIGIN` y las URLs de los proveedores. La base de datos y los objetos no cambian.

## Decisiones requeridas del propietario

| # | Decisión | Opciones | Recomendación |
| --- | --- | --- | --- |
| 1 | Aceptar la variante compartida | Sí (A) / servidor dedicado (B) | A para R0, con fecha de revisión al pasar a dominio propio |
| 2 | CDN delante de `crm.<dominio>` | Proxy de la CDN activado / solo DNS | Proxy activado: la cadena de IP de arriba lo supone; con solo DNS, el borde público usa la IP del socket, también correcta |
| 3 | §6.2 (grupo `docker`) en este servidor | Aceptar la excepción / sacar al usuario de la otra aplicación del grupo / B | Aceptar mientras el propietario sea el único con SSH; revisar al dar acceso a otra persona |
| 4 | Tabla de topes repetida en el borde público | Repetirla / aceptar 502 en vez de 413 | Repetirla; `smoke.sh` vigila que coincidan |
| 5 | Cortafuegos del proveedor: 80/443 solo desde los rangos de la CDN | Sí / no | Sí, cuando la CDN esté activada para los dos nombres. Sin él, cualquiera que conozca la IP llega al borde sin la CDN |
| 6 | IPv6 | Regla de tasa en la CDN para `/webhooks/*` (por /64) / desactivar IPv6 para `crm.` en la CDN / aceptar | Regla de tasa en la CDN. El `rate_limit` de Caddy cuenta por dirección completa, y un cliente IPv6 puede rotar dentro de su /64. El origen no tiene IPv6: la CDN llega por IPv4 |

# Object storage y HTTP saliente (F1-08)

**Relacionado:** ADR-008, ADR-012 §6–§7, [security-boundaries.md](security-boundaries.md) B8–B9
**Estado:** implementado en F1-08 (#10)

## Object storage (B8)

- `core.storage.ObjectStorageService` define `put`, `open`, `head`, `delete`, `presign_get` y `presign_put`.
- Implementaciones:
  - `S3CompatibleStorage` (boto3; AWS S3, R2 o Garage), elegida con `STORAGE_BACKEND=s3`. Se configura con `STORAGE_ENDPOINT_URL`, `STORAGE_REGION`, `STORAGE_BUCKET`, las credenciales y `STORAGE_ADDRESSING_STYLE=path`;
  - `InMemoryStorage` (tests).
- **Claves:** `org/{organization_id}/{purpose}/{yyyy}/{mm}/{file_id}` (`tenant_key`), sin el nombre original.
  - Todas las operaciones validan la forma completa (`check_key`), así que no se aceptan `..`, nombres ni rutas libres.
  - La tabla `files` (tenant-owned, RLS + FORCE, FK a `organizations`) repite la regla con un CHECK de BD: tenant y `purpose` de la propia fila, año/mes y UUID. También cierra `purpose`, `scan_status` y `sha256`.
- **URLs firmadas:** como máximo 300 s (`MAX_PRESIGN_SECONDS`).
  - Se firman con `STORAGE_PUBLIC_ENDPOINT_URL` (F7-09a3), el origen del almacén que alcanza el navegador; vacío = `STORAGE_ENDPOINT_URL`. SigV4 firma el `Host`, así que una URL firmada para `garage:3900` (la red del stack) no le sirve al navegador: el stack local firma para `http://127.0.0.1:${GARAGE_HOST_PORT:-3900}`: otro host que la aplicación (`localhost`), porque las cookies no distinguen puertos y con `localhost` el almacén recibiría la cookie de sesión. `put`, `open`, `head` y `delete` siguen con el endpoint interno. Production exige `https` y otro **host** que la aplicación: el host (sin puerto, sin punto final y sin mayúsculas) no puede ser el de `APP_ORIGIN` ni coincidir con `DJANGO_ALLOWED_HOSTS` por las reglas de Django (`.example.com` y `*` incluidos: con `*` se rechaza todo); vacío, se comprueba `STORAGE_ENDPOINT_URL`, que es el que firma entonces. Las cookies no distinguen puertos: en el mismo host, el almacén recibiría la cookie de sesión (que no fija `Domain`). **Requisito de despliegue:** el almacén público va en otro dominio registrable (otro *site*, p. ej. `media-example.net` frente a `crm.example.com`), no solo en otro subdominio: en el mismo *site*, el día que se muestren medios en línea, su contenido podría fijar cookies para el dominio padre (*cookie tossing*). La configuración solo comprueba el host. Test de contrato con Garage real: la URL firmada para el host público funciona por ese host y da 403 por otro.
  - `presign_get` exige el `content_type` real (el `files.mime_type` por magic bytes) y lo fuerza con `ResponseContentType`. Nunca se usa el tipo que declaró el cliente.
  - `Content-Disposition` lleva el `filename` en ASCII saneado más `filename*` en UTF-8 (RFC 6266).
  - `inline` solo se permite para imágenes y PDF; HTML, SVG y cualquier otro tipo se sirven siempre como `attachment`.
  - Toda URL firmada de lectura lleva además `response-cache-control: private, no-store` (F7-09a2): ni el navegador ni un proxy guardan la respuesta.
  - **Medios de mensajes** (`message-media`, F7-09a2): siempre `attachment`, también las imágenes, con un nombre generado (`<clase>.<extensión>`) y el `Content-Type` del mapa canónico de tipos por magic bytes (`apps.conversations.media.EXTENSIONS`), con 60 s de vida. Solo se emite para un archivo con `scan_status = CLEAN` (D-F7-04).
  - **Requisito de despliegue:** una URL firmada de S3 no puede forzar `X-Content-Type-Options: nosniff` (solo `response-content-type`, `-disposition`, `-cache-control` y similares). El proxy o la CDN que sirva el bucket en producción tiene que añadir `nosniff` a toda respuesta del almacén. En local Garage se expone sin el proxy de Caddy, así que ahí no lo lleva; hoy no se sirve ningún medio porque no hay antivirus.
  - `presign_put(key, content_type, content_length, max_bytes)` exige `0 < content_length <= max_bytes` **antes** de emitir la URL.
    - Firma `ContentType` y `ContentLength`: `content-length` queda en `X-Amz-SignedHeaders`, y cualquier otro tamaño invalida la firma. En Garage v2.4.1 real, un PUT de 5000 bytes sobre una URL firmada para 4 recibe `403 AccessDenied: Invalid signature` y no crea el objeto.
    - El cliente envía los `headers` devueltos tal cual.
    - El `finalize` (con el primer endpoint de subida) repite la verificación con `head`, como segunda barrera.
- **Checksums:** `request_checksum_calculation` y `response_checksum_validation` en `when_required`. Garage v2.4.1 también acepta los valores por defecto (verificado); se mantiene `when_required` por portabilidad a R2.
- `boto3`/`botocore` solo se importan en `core.storage` (contrato `protected` de import-linter).
- **Garage v2.4.1** es solo para local y CI (OBS-F1-01-2, AGPL-3.0):
  - en `infra/docker/compose.yaml`, con `infra/docker/garage/garage.toml` (sin secretos);
  - en CI, un contenedor por ejecución con credenciales generadas y enmascaradas;
  - la suite de contrato (`tests/test_storage.py`) es **obligatoria en CI**: falla si falta el endpoint.

## HTTP saliente (B9)

`core.http.request(method, url, **kwargs)` (urllib3) aplica:

- Solo `https` al puerto 443, sin credenciales en la URL y con el host en `HTTP_ALLOWED_HOSTS`, por coincidencia exacta y sin distinguir mayúsculas. Si la allowlist está vacía, no se permite nada.
- La IP se valida **al conectar**: `_GuardedHTTPSConnection` resuelve el host y exige que **todas** las IPs sean públicas.
  - Se rechazan las privadas, loopback, link-local, CGNAT, reservadas, multicast y site-local.
  - También los prefijos que llevan una IPv4 incrustada (mapeada, compatible, SIIT, NAT64 64:ff9b::/96, 6to4 y Teredo), cuya IPv4 debe ser pública.
  - Conecta a esa misma IP, así que no hay ventana de DNS rebinding.
  - Después restaura el nombre: SNI, verificación del certificado y cabecera `Host` usan el host original (test dedicado).
- `request()` no acepta `**kwargs`: el llamador no puede quitar timeouts ni reintentos, ni reactivar redirecciones.
- La cabecera `Host` no se puede sustituir (en cualquier combinación de mayúsculas): la petición se rechaza antes de tocar la red.
- **Producción:** `config.settings.production` rechaza al arrancar un `STORAGE_ENDPOINT_URL` explícito que no sea `https` o que lleve credenciales. Vacío = AWS por defecto. Local y CI siguen con `http://` contra Garage.
- **Redirecciones:** no se siguen. Para seguir una redirección hay que llamar de nuevo a `request()` con la nueva URL, que pasa otra vez por toda la política.
- Tampoco usa proxies del entorno, y tiene timeouts (connect 5 s, read 15 s).
- La URL se lee con dos analizadores (el de Python y el de urllib3, que es el que conecta) y se rechaza si discrepan en esquema, host o puerto, si trae controles, espacios o `\`, o si el puerto está fuera de rango o no es numérico.
- **Descargas** (`core.http.download`, F7-09a1a): el mismo `_manager` y la misma política de URL e IP, con una lista `hosts` propia en lugar de la allowlist: tiene que ser un subconjunto de `META_MEDIA_HOSTS` (la descarga envía el token a cualquier host de la lista, así que otra lista falla con `ValueError`) y esos hosts **no** están en `HTTP_ALLOWED_HOSTS` (`request`, que lleva el token de una cuenta, no llega a ellos). Sin reintentos y sin decodificar (`Accept-Encoding: identity`; cualquier `Content-Encoding` se rechaza). Nunca llega al destino más que el tope de bytes (por un `Content-Length` válido y por lo recibido); ante cualquier excepción el llamador descarta el destino (pudieron escribirse bytes parciales). El plazo total se comprueba antes de cada lectura (`read1`: devuelve lo disponible) y un vigilante corta el socket al vencer, también durante el apretón TLS y las cabeceras: guarda un **duplicado** del socket al abrirlo (`ssl` suelta el descriptor original al empezar el apretón) que mantiene vivo ese socket hasta que el vigilante se detiene y lo cierra bajo su cerrojo, así que nunca corta una conexión ajena; la conexión de una descarga nunca vuelve a la reserva de `_manager` (`_GuardedPool._put_conn` la cierra), así que cada descarga abre la suya, con su vigilante. Cada `recv` espera como mucho `min(15 s, lo que queda del plazo)`. Un corte del vigilante en un cuerpo sin `Content-Length` llega como fin de datos y se informa como plazo vencido, no como un 200. **No** cubre `getaddrinfo`, que no tiene tiempo propio (lo acota el resolvedor del sistema). Los fallos de red salen sin el texto original (la URL firmada), ni como causa ni como contexto, y lo mismo vale para una URL que el analizador de urllib3 rechaza; un fallo del destino de escritura (disco lleno, o un `write` que no avanza) es un `OSError` tal cual. Las cabeceras `Host`, `Transfer-Encoding`, `Content-Length` y `Connection` no las pone el llamador (también en `request`), ningún valor de cabecera lleva controles (el valor puede ser el token) y una IP resuelta con zona (`%`) se rechaza. El registro DEBUG de urllib3 (`GET <ruta>?<query>`) está en `WARNING` en `LOGGING`, y las migas de pan de Sentry de `http.client` pierden la query (`core.observability.reporting.scrub_breadcrumb`): la query de una URL firmada no se registra ni se reporta.
- `urllib3` solo se importa en `core.http` (contrato `protected`). Un test estático impide importar `requests`, `httpx`, `urllib.request` o `http.client` en `core`, `apps` o `config`; import-linter solo ve lo que ya se importa.

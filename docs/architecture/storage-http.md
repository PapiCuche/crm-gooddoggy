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
  - `presign_get` exige el `content_type` real (el `files.mime_type` por magic bytes) y lo fuerza con `ResponseContentType`. Nunca se usa el tipo que declaró el cliente.
  - `Content-Disposition` lleva el `filename` en ASCII saneado más `filename*` en UTF-8 (RFC 6266).
  - `inline` solo se permite para imágenes y PDF; HTML, SVG y cualquier otro tipo se sirven siempre como `attachment`.
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
- **Descargas** (`core.http.download`, F7-09a1a): el mismo `_manager` y la misma política, y además un host de una lista `hosts` más estrecha que la allowlist, sin reintentos, sin decodificar (cualquier `Content-Encoding` se rechaza), tope de bytes (nunca se escribe más que el tope), plazo total comprobado antes de cada trozo y errores de red sin el texto original, que lleva la URL firmada.
- `urllib3` solo se importa en `core.http` (contrato `protected`). Un test estático impide importar `requests`, `httpx`, `urllib.request` o `http.client` en `core`, `apps` o `config`; import-linter solo ve lo que ya se importa.

# Backups, restauración y simulacro (producción)

Implementa [ADR-033](../adr/ADR-033-production-hosting.md) §8 (F1-16, #757). Los scripts están en [`infra/prod/backup/`](../../infra/prod/backup/). RPO de 24 h y RTO de 4 h (ADR-033 §8.3, decisión 6).

Nada de este runbook lleva valores reales: ni el proveedor, ni el bucket, ni las claves. Esos datos los guarda el propietario fuera del repositorio.

## 1. Qué se respalda y dónde

| Qué                                     | Dónde, en el bucket de backups                     | Cuándo                                 | Se borra                                    |
| --------------------------------------- | -------------------------------------------------- | -------------------------------------- | ------------------------------------------- |
| Volcado de la base `crm` (`pg_dump -Fc`) | `db/daily/crm-<AAAAMMDDTHHMMSSZ>.dump.age`         | Cada noche, 03:15 UTC                  | A los 7 días (7 diarios)                    |
| El mismo volcado, una vez por semana    | `db/weekly/…`                                      | El domingo o, si esa noche falla, la siguiente noche buena de la semana | A los 28 días (4 semanales)  |
| El mismo volcado, una vez por mes       | `db/monthly/…`                                     | El día 1 o, si esa noche falla, la siguiente noche buena del mes        | A los 184 días (6 mensuales) |
| Volcado previo a un despliegue          | `db/predeploy/…`                                   | `backup.sh predeploy` (F1-15)          | A los 7 días                                |
| Objetos del almacenamiento              | `media/<clave del objeto>.age`, uno por objeto     | Cada noche, solo los nuevos            | Nunca (ver §9)                              |

- Todo va **cifrado con [age](https://age-encryption.org)** antes de salir del servidor, para las claves públicas de `backup-recipients.txt`. El volcado nunca está en claro en el disco.
- Junto a cada volcado va `crm-<AAAAMMDDTHHMMSSZ>.counts.age`: las filas por tabla, contadas **en la misma instantánea** que el volcado (`pg_export_snapshot` y `pg_dump --snapshot`) y cifradas igual. `restore.sh --expect-counts` las compara con lo restaurado: tienen que coincidir exactamente, aunque se escribiera durante el volcado.
- El semanal y el mensual van **por periodo**, no por día: la semana empieza el domingo y el mes, el día 1. `backup.sh` guarda en `/var/lib/crm-backup/last-period` la semana y el mes de la última subida nocturna correcta. Si el periodo actual aún no tiene copia, la noche la hace. Una noche que falla no guarda nada, así que la siguiente noche buena del periodo hace la copia. Si falla la semana entera, esa semana queda sin semanal.
- La retención la aplican las **reglas de ciclo de vida del destino** ([`lifecycle.json`](../../infra/prod/backup/lifecycle.json)), no el servidor. `.github/scripts/test_backup_retention.py` simula dos años de noches y comprueba que se guardan 7, 4 y 6 copias y que solo se borra lo que queda fuera de la política. Del mensual queda a veces una séptima copia unos días, mientras el mes es corto.
- **No se respaldan:** Redis (la cola), los certificados de Caddy, las imágenes (están en GHCR) ni el frontend (ADR-033 §8.1).
- **Las KEKs (`SECRETS_KEKS`, ADR-022) no van en el backup.** Sin ellas, los secretos cifrados de la base (segundos factores TOTP, tokens de canales) no se pueden leer. El propietario guarda aparte cada versión de KEK mientras exista un backup cifrado con ella: 6 meses después de rotarla (ADR-033 §6.3).
- Los roles tampoco van en el volcado. Al restaurar, `bootstrap-roles.sh` los crea con contraseñas nuevas (ADR-033 §8.1).

## 2. Claves: quién guarda qué

| Clave                                | Quién la guarda                                                                    | Dónde está la parte pública                                  |
| ------------------------------------ | ---------------------------------------------------------------------------------- | ------------------------------------------------------------ |
| Clave de backup                      | El propietario y un suplente que él designa, cada uno con la suya (decisión 5)    | `/etc/crm/backup-recipients.txt`, una línea por persona      |
| Clave de despliegue (opcional)       | Quien despliega (ADR-033 §8.2)                                                     | `/etc/crm/backup-deploy-recipients.txt`; solo cifra `predeploy` |
| Credencial de escritura del destino  | El servidor (`backup.env`, root, `0600`) y una copia del propietario               | —                                                            |
| Credencial de lectura del destino    | Solo el propietario y su suplente. Nunca en el servidor salvo durante una restauración | —                                                        |

Cada persona crea su clave **en su propio equipo, nunca en el servidor**:

```bash
age-keygen -o backup-key.txt     # la clave privada: a un gestor de contraseñas o a una copia cifrada sin conexión
age-keygen -y backup-key.txt     # la línea pública age1…: esta es la que va al servidor
```

Si la clave privada se pierde, sus backups no se pueden leer. Por eso hay dos personas con clave propia. Si una clave se ve comprometida, se quita su línea de `backup-recipients.txt` y se crea otra: los backups ya hechos siguen cifrados para la clave vieja hasta que caducan.

## 3. El destino (lo prepara el propietario)

El propietario elige el proveedor (ADR-033 §11): otro proveedor u otra cuenta que la del servidor, compatible con S3. Lo que el destino tiene que cumplir:

1. **Bucket privado con versionado** (lo mínimo que exige ADR-033 §8.2) y, si el propietario lo decide, **Object Lock** (punto 4). Sin ninguno de los dos, subir un objeto con el mismo nombre equivale a borrar el anterior.
2. Las reglas de [`lifecycle.json`](../../infra/prod/backup/lifecycle.json) aplicadas con una credencial de administración del propietario (nunca la del servidor). Con la CLI de AWS, que funciona con la mayoría de proveedores compatibles:

   ```bash
   aws s3api put-bucket-versioning --bucket <BUCKET> --versioning-configuration Status=Enabled --endpoint-url https://<ENDPOINT>
   aws s3api put-bucket-lifecycle-configuration --bucket <BUCKET> \
     --lifecycle-configuration file://infra/prod/backup/lifecycle.json --endpoint-url https://<ENDPOINT>
   ```

   Si el proveedor no acepta alguna regla (p. ej., `ExpiredObjectDeleteMarker`), la configura igual en su panel y lo anota aquí.
3. **Credencial del servidor que escribe y no lee, no lista ni borra.** En la sintaxis de políticas de AWS, solo `s3:PutObject` (y `s3:AbortMultipartUpload` para subidas grandes):

   ```json
   {
     "Version": "2012-10-17",
     "Statement": [
       {
         "Effect": "Allow",
         "Action": ["s3:PutObject", "s3:AbortMultipartUpload"],
         "Resource": "arn:aws:s3:::<BUCKET>/*"
       }
     ]
   }
   ```

   Otros proveedores lo llaman «clave de aplicación» con la capacidad de escribir archivos y sin las de leer, listar o borrar. Si un proveedor no permite una credencial así, ese proveedor no sirve para este destino.
4. **Protección frente a un servidor comprometido: la elige el propietario.** La credencial del servidor puede escribir, y los nombres de los volcados salen en el journal (`backup.ok`) y se adivinan. Un atacante con root puede subir objetos vacíos con esos nombres. Hay tres opciones, y **todas alargan la retención efectiva más allá del 7/4/6 de ADR-033 (decisión 5)**. Por eso la elección es del propietario y se confirma en su checklist (#781):

   | Opción                                          | Qué pasa si el servidor sobrescribe                                                                 | Retención efectiva                                                                                       | Borrar por una solicitud de datos personales (§9)                                              |
   | ----------------------------------------------- | --------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------- |
   | **A. Solo versionado** (el `lifecycle.json` tal cual) | La versión buena pasa a «no actual», y `db-versions` la borra a los **30 días**. Se pierde si el ataque pasa más de 30 días sin detectarse | Cada copia vive 30 días más: diarios unos 37 días, semanales unos 58 y mensuales unos 214. Unas 52 copias del volcado en lugar de 17 | Sí: el propietario borra todas las versiones con su credencial de administración             |
   | **B. Object Lock en modo _governance_**         | El servidor no puede borrar ni acortar el bloqueo; solo una credencial con permiso para saltarse el bloqueo (la del propietario) puede | La de A, y además nada se borra antes de que venza el bloqueo                                            | Sí: el propietario, con permiso para saltarse el bloqueo                                      |
   | **C. Object Lock en modo _compliance_**         | Nadie, ni el propietario ni el proveedor, borra una versión antes de que venza su bloqueo                | Con un solo bucket (lo que usa `backup.sh`), el bloqueo tiene que ser de **184 días** para proteger los mensuales: **cada diario y cada objeto de `media/` viven al menos 184 días** (unas 220 copias del volcado) | **No hasta que venza el bloqueo** (hasta 184 días)                                            |

   - En B y C el bloqueo tiene que durar al menos lo que guarda el nivel: 7 días (`daily`, `predeploy`), 28 (`weekly`) y 184 (`monthly`). Un bloqueo de 7 días en todo el bucket **no** protege los semanales ni los mensuales. Si el proveedor admite bloqueos por prefijo, basta con 7, 28 y 184; un bucket por nivel exigiría un cambio en `backup.sh` (deuda de F1-16).
   - **Otra alternativa, si el proveedor la admite:** una política del bucket que niegue sobrescribir un objeto que ya existe (escrituras condicionales). Hoy `rclone` no envía esa condición, así que exige probarla antes.
   - El riesgo que queda en A es un atacante sin detectar durante más de 30 días: por eso el simulacro trimestral y la revisión de las alertas.
5. Una credencial de **lectura** (`s3:GetObject` y `s3:ListBucket`) para el propietario y su suplente, guardada fuera del servidor.

## 4. Instalación en el servidor

Como `root`, con el repositorio en `/opt/crm` (F1-17). Si está en otra ruta, cambia `ExecStart` en las dos unidades.

```bash
apt-get install -y age rclone curl
install -m 0600 /dev/null /etc/crm/backup.env                     # y rellenarlo: sección backup.env de .env.production.example
install -m 0644 /dev/null /etc/crm/backup-recipients.txt           # las líneas age1… del propietario y del suplente
cp /opt/crm/infra/prod/backup/crm-backup{,-check}.{service,timer} /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now crm-backup.timer crm-backup-check.timer
systemctl start crm-backup.service                                 # la primera copia, ya
journalctl -u crm-backup.service -n 20                             # debe terminar en "event":"backup.ok"
```

`backup.sh` lee el bucket de la aplicación con las `STORAGE_*` de `runtime.env`. Guarda su estado en `/var/lib/crm-backup`: `last-success` (para el aviso), `last-period` (la semana y el mes de la última subida nocturna; si se pierde, la noche siguiente hace un semanal y un mensual de más) y `media-copied.txt` (los objetos ya copiados, porque el servidor no puede listar el destino). Las filas por tabla ya no quedan en claro en el disco: van cifradas junto al volcado. Un `last-counts.tsv` de una versión anterior se puede borrar. Los `<clave>.partial` del bucket (los deja una restauración de objetos que falló) no se copian. Si se pierde ese directorio, la siguiente noche vuelve a subir todos los objetos: el versionado guarda una versión más de cada uno, y la regla `media-versions` la borra a los 30 días.

## 5. Alertas

- **`backup.failed`**: `backup.sh` falla en cualquier paso. Queda en el journal (`journalctl -u crm-backup.service`) con la etapa (`dump`, `upload` o `media`) y, si `SENTRY_DSN` está en `backup.env`, también como evento en Sentry. El log nunca lleva datos, claves de objetos ni credenciales.
- **`backup.missing`**: `check.sh` comprueba cada hora que el último backup nocturno correcto tenga menos de 26 h (ADR-033 §8.4). Avisa por el mismo camino.
- **Permisos de `backup.env`:** si no es de root y `0600`, `backup.sh` y `check.sh` no lo cargan y avisan con `backup.failed` y `backup.missing` (`reason: config_permissions`). Ese aviso va a Sentry solo si el archivo es de root y nadie más puede escribirlo (p. ej., `0644`): su `SENTRY_DSN` es entonces de fiar. Si otros pueden escribirlo, el aviso queda solo en el journal.
- **Si el servidor entero cae**, esos dos avisos caen con él. Para eso, `BACKUP_HEARTBEAT_URL`: un servicio externo de monitorización que recibe un ping tras cada backup nocturno correcto y avisa si en 26 h no llega ninguno. Lo elige el propietario. Su URL lleva el token del servicio: `backup.sh` se la pasa a `curl` por la entrada estándar (`--config -`), nunca en los argumentos, que cualquier usuario ve con `ps`.

Qué hacer ante un aviso: leer `journalctl -u crm-backup.service -n 50`, corregir la causa (disco, credencial, destino caído) y lanzar `systemctl start crm-backup.service`. Un día sin backup no se recupera después: el RPO de ese día pasa a 48 h.

## 6. Restaurar en un servidor nuevo

Objetivo: menos de 4 h (RTO). El propietario o su suplente tienen que estar localizables en la primera hora: traen la clave privada, la credencial de lectura y la copia de los secretos (ADR-033 §8.3).

1. Aprovisionar el servidor con el runbook de primer arranque (F1-17): Docker, el repositorio y los archivos de `/etc/crm` restaurados de la copia del propietario, con **las mismas `SECRETS_KEKS`**. Para los roles, contraseñas nuevas en `postgres.env` (root, `0600`), las mismas en `migrate.env` y `runtime.env`.
2. Arrancar solo PostgreSQL: `docker compose -f /opt/crm/infra/prod/compose.yaml --env-file /etc/crm/release.env up -d --wait postgres`.
3. Traer al servidor, solo para la restauración, la clave privada y un archivo `restore.env`, que es `backup.env` con la credencial de **lectura**. Ambos en `/dev/shm` (memoria), con modo `0600`.
4. Restaurar:

   ```bash
   sudo BACKUP_ENV=/dev/shm/restore.env /opt/crm/infra/prod/backup/restore.sh \
     --identity /dev/shm/backup-key.txt --roles-env /etc/crm/postgres.env --latest --media
   ```

   - `--latest` toma el último volcado de `db/daily/`. Para otro, `--object db/monthly/crm-….dump.age`.
   - `--media` devuelve los objetos al bucket de `runtime.env`. Sin él, solo la base. Úsalo si se perdió el bucket de producción. Si solo se perdió el servidor, el bucket sigue intacto (ADR-033 §8.3). Para recuperar solo el bucket, sin tocar la base, usa `--media-only`.
     - Cada objeto se escribe primero como `<clave>.partial` y solo pasa a su nombre si se descargó y descifró entero. Una clave equivocada o un objeto dañado no pisan nada.
     - Los objetos que ya están en el bucket no se tocan. Para sobrescribirlos, `--overwrite-media`.
   - `--roles-env` es un archivo `KEY=valor` con `CRM_MIGRATOR_PASSWORD` y `CRM_APP_PASSWORD`, de root y `0600` (si no, el script se niega). Las contraseñas pasan a `bootstrap-roles.sh` por el entorno, **nunca por la línea de órdenes**: ni en `sudo`, ni en el historial, ni en `ps`. Si los roles ya existen, se puede omitir. Entonces, si falta la base `crm`, `restore.sh` la crea con los mismos permisos que `bootstrap-roles.sh` (dueño `crm_migrator`, `CONNECT` y `TEMPORARY` para `crm_app`).
   - `backup.env`, `restore.env`, `runtime.env` y la clave privada tienen que ser de root y `0600`. `backup-recipients.txt`, de root y sin escritura para otros. Si no, los scripts no arrancan.
   - **No restaura encima de una base con tablas.** Para sustituirla, `--replace-existing-data`: para el runtime, **renombra** la base `crm` a `crm_replaced_<AAAAMMDDHHMMSS>` y crea otra con `bootstrap-roles.sh` (exige las contraseñas). Para renombrarla, no admite conexiones nuevas, cierra las abiertas y lo intenta hasta 5 veces. Si no lo consigue, `crm` queda como estaba y el aviso no lleva `kept=`. Solo cuando el renombrado terminó, el log dice `restore.replacing kept_as=…`.
     - La anterior solo se borra después de `restore.ok` (`restore.replaced_dropped`). Si ese borrado falla, la restauración está bien y el aviso es otro, `restore.replaced_not_dropped`: se borra a mano.
     - `restore.replaced_left` (aviso) lista las `crm_replaced_*` que sigan ahí de restauraciones anteriores.
     - Mientras tanto, la anterior ocupa su espacio en disco: hay que tener sitio para las dos.
   - `--expect-counts` compara las filas por tabla restauradas con las del volcado (`<volcado>.counts.age`). Si no coinciden, falla. Los volcados anteriores a F1-16b (#857) no tienen ese archivo: con ellos, sin `--expect-counts`.
5. El script descifra el volcado en memoria y lo pasa a `pg_restore` en una sola transacción. Después deja las filas por tabla en `/var/lib/crm-backup/restored-counts.tsv`, registra si las migraciones estaban al día (`migrations_at_head`), levanta el backend (el job `migrate` corre antes) y exige `/health/ready` con 200. Termina con `"event":"restore.ok"` y los segundos que tardó.
   **Si falla con `--replace-existing-data`** y el aviso `restore.failed` lleva `kept=crm_replaced_<…>`, la base anterior sigue entera con ese nombre. Sin `kept=`, el fallo fue antes de renombrarla y `crm` está intacta: no hay que volver a nada. Para volver a la base guardada, con el runtime parado, usa [`rollback-replaced.sql`](../../infra/prod/backup/rollback-replaced.sql). Solo borra `crm` si la base guardada existe; si no, falla sin tocar nada:

   ```bash
   docker compose -f /opt/crm/infra/prod/compose.yaml --env-file /etc/crm/release.env exec -T postgres \
     psql -X -v ON_ERROR_STOP=1 -U postgres -d postgres -v kept=crm_replaced_<…> < /opt/crm/infra/prod/backup/rollback-replaced.sql
   docker compose -f /opt/crm/infra/prod/compose.yaml --env-file /etc/crm/release.env up -d --wait backend
   ```

   Las contraseñas de los roles son ya las de `--roles-env`: `migrate.env` y `runtime.env` tienen que llevar esas mismas. Si en cambio la restauración se da por buena a mano, se borra la anterior con `DROP DATABASE crm_replaced_<…>`.
6. **Borrar la clave privada y `restore.env`**: `shred -u /dev/shm/backup-key.txt /dev/shm/restore.env`. Borrar también `postgres.env` y recrear `postgres` (infra/prod/README.md, «Secretos»).
7. Desplegar el resto (`deploy.sh`, F1-15), apuntar el registro A y pasar la puerta de salud.

**Volver atrás tras un despliegue con migraciones** (ADR-033 §7.7): es el mismo procedimiento con `--object db/predeploy/crm-….dump.age --replace-existing-data`, `--roles-env` y la clave de despliegue o la de backup. El orden exacto respecto de los digests de la versión anterior lo fija el runbook de despliegue (F1-15).

## 7. El simulacro

**En CI**, en cada cambio de `infra/prod/` y de una migración del backend (`backend/**/migrations/**`): el job `infra-prod backup drill` ejecuta [`drill.sh`](../../infra/prod/backup/drill.sh). Levanta el compose de producción con datos (una organización) y tres archivos, hace el backup a un Garage local con una credencial que solo escribe, comprueba que el aviso `backup.failed` salta y que esa credencial no lee el destino, «pierde el servidor», restaura en un stack nuevo y comprueba:

- las filas por tabla, idénticas a las contadas en la instantánea del volcado, aunque se escribe una fila mientras se vuelca;
- las migraciones al día y `/health/ready`;
- los archivos, idénticos byte a byte;
- que no restaura encima de una base con datos sin `--replace-existing-data`, y que con esa opción sí;
- que una clave equivocada, unas contraseñas en un archivo `0644` o un `backup.env` `0644` se rechazan sin tocar la base;
- que un fallo dentro del volcado deja `backup.failed stage=dump` y no mueve `last-success`, y que una marca no numérica da `backup.missing`;
- que una lista de objetos copiados desordenada (una noche que falló a medias) no rompe la siguiente copia, y que un `.partial` no se copia;
- que una noche fallida no guarda su periodo y la siguiente noche buena hace el semanal y el mensual; y que otra noche del mismo periodo solo hace el diario;
- que `BACKUP_HEARTBEAT_URL` no aparece en los argumentos de `curl` y que el ping llega;
- que un `alerted=1` heredado del entorno no silencia `backup.failed` ni `restore.failed`;
- que un renombrado que falla deja `crm` intacta y un aviso sin `kept=`;
- que `--replace-existing-data` que falla después de `pg_restore` deja la base anterior como `crm_replaced_<…>`; que `rollback-replaced.sql` se niega con una base guardada que no existe y vuelve a la que sí; y que tras `restore.ok` se borra;
- que con los roles ya creados, sin `--roles-env` y sin la base `crm`, la restauración crea la base;
- que `--media-only --overwrite-media` con otra clave deja los objetos idénticos y ningún `.partial`, y que sin `--overwrite-media` no vuelve a subir lo que ya está.

Las imágenes del simulacro (Garage, Caddy, PostgreSQL y Redis) van fijadas por digest. En local, con Docker, `age` y `rclone`: `BACKEND_IMAGE=<imagen del backend del repo> bash infra/prod/backup/drill.sh`. Borra al salir, por nombre, todo lo que crea.

**En el servidor real**: es obligatorio antes de salir a producción (F1-18, #760), y después cada trimestre y tras cambiar el procedimiento (ADR-033 §8.4).

1. En un servidor **nuevo**, seguir §6 con el último backup nocturno. Cronometrar desde el aprovisionamiento hasta la puerta de salud.
2. Comprobar:
   - `migrations_at_head` es `yes`;
   - `/health/ready` responde;
   - una cuenta con segundo factor entra (demuestra que las KEKs restauradas abren los secretos);
   - un archivo se descarga;
   - las filas por tabla coinciden exactamente con las del volcado (`--expect-counts`).
3. Destruir el servidor del simulacro y su clave privada.
4. Anotar el resultado en el registro de abajo.

### Registro

| Fecha      | Dónde                                              | Backup                         | Resultado                                                                                   | Tiempo de restauración |
| ---------- | -------------------------------------------------- | ------------------------------ | ------------------------------------------------------------------------------------------- | ---------------------- |
| 2026-11-15 | Local (`drill.sh`, Docker), PR de F1-16           | Nocturno del stack desechable  | OK: 97 tablas idénticas, migraciones al día, `/health/ready` 200, 3 archivos idénticos       | 22 s (RTO: 4 h)        |

## 8. Volumen y tiempos

El simulacro local restaura una base casi vacía. El tiempo real depende del tamaño del volcado y de la red hacia el destino. Por eso el simulacro en el servidor se repite cada trimestre: si el tiempo se acerca a 4 h, hay que revisar el procedimiento o la opción de WAL continuo (ADR-033 §8.3).

## 9. Límites conocidos

- **RPO de 24 h:** lo escrito desde el último backup nocturno se pierde si se pierde el servidor. WAL continuo queda para más adelante (ADR-033 §8.3).
- **Un objeto borrado en producción sigue en el backup**, porque el servidor no puede borrar en el destino.
- **Solicitud de borrado de datos personales.** Depende de la opción de §3.4:
  - **Objetos (`media/`):**
    - Con A, el propietario borra el objeto y todas sus versiones con su credencial de administración.
    - Con B, lo mismo, con el permiso para saltarse el bloqueo.
    - Con C no se puede hasta que vence el bloqueo (hasta 184 días desde que se subió). El propietario anota la solicitud y lo borra en cuanto vence.
  - **Volcados de la base (`db/`):** los datos de la persona siguen en los volcados hasta que caducan: con A o B, hasta unos 214 días el mensual; con C, igual o más. No se editan.
  - **Si se restaura un volcado anterior a una solicitud**, hay que volver a aplicar los borrados registrados desde entonces. El propietario lleva ese registro.
- **Los objetos restaurados pierden sus metadatos de S3** (el tipo de contenido queda genérico). La aplicación sirve cada archivo con el tipo guardado en la base (`files.mime_type`), así que no cambia lo que ve el usuario.
- **Mientras se vuelca, una transacción de solo lectura sigue abierta** (la que exporta la instantánea). En un volcado largo retrasa el `VACUUM` de las filas que se borren durante él, como el propio `pg_dump`.

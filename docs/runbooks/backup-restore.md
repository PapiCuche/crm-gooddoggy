# Backups, restauración y simulacro (producción)

Implementa [ADR-033](../adr/ADR-033-production-hosting.md) §8 (F1-16, #757). Los scripts están en [`infra/prod/backup/`](../../infra/prod/backup/). RPO de 24 h y RTO de 4 h (ADR-033 §8.3, decisión 6).

Nada de este runbook lleva valores reales: ni el proveedor, ni el bucket, ni las claves. Esos datos los guarda el propietario fuera del repositorio.

## 1. Qué se respalda y dónde

| Qué                                     | Dónde, en el bucket de backups                     | Cuándo                                 | Se borra                                    |
| --------------------------------------- | -------------------------------------------------- | -------------------------------------- | ------------------------------------------- |
| Volcado de la base `crm` (`pg_dump -Fc`) | `db/daily/crm-<AAAAMMDDTHHMMSSZ>.dump.age`         | Cada noche, 03:15 UTC                  | A los 7 días (7 diarios)                    |
| El mismo volcado, en domingo            | `db/weekly/…`                                      | Domingos                               | A los 28 días (4 semanales)                 |
| El mismo volcado, el día 1              | `db/monthly/…`                                     | Día 1 de cada mes                      | A los 184 días (6 mensuales)                |
| Volcado previo a un despliegue          | `db/predeploy/…`                                   | `backup.sh predeploy` (F1-15)          | A los 7 días                                |
| Objetos del almacenamiento              | `media/<clave del objeto>.age`, uno por objeto     | Cada noche, solo los nuevos            | Nunca (ver §9)                              |

- Todo va **cifrado con [age](https://age-encryption.org)** antes de salir del servidor, para las claves públicas de `backup-recipients.txt`. El volcado nunca está en claro en el disco.
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

1. **Bucket privado con Object Lock en modo _compliance_ (recomendado) o, como mínimo, con versionado.** Sin ninguno de los dos, subir un objeto con el mismo nombre equivale a borrar el anterior. Ver el punto 4 y «Qué protege cada opción».
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
4. **Object Lock en modo _compliance_ (recomendado).** La retención tiene que durar al menos lo que el nivel más largo que guarda cada copia: 7 días (`daily`, `predeploy`), 28 (`weekly`) y 184 (`monthly`). Con un solo bucket, que es lo que usa `backup.sh`, eso significa una retención por omisión de **184 días**. Si el proveedor admite retención por prefijo, basta con 7, 28 y 184 en cada uno. Una retención de 7 días en todo el bucket **no** protege los semanales ni los mensuales. Con *compliance* nadie, ni el propietario, borra una versión antes de que venza; las reglas de ciclo de vida borran después.
   - **Alternativa, si el proveedor la admite:** una política del bucket que niegue sobrescribir un objeto que ya existe (escrituras condicionales). Hoy `rclone` no envía esa condición, así que exige probarla antes.

   **Qué protege cada opción frente a un servidor comprometido.** La credencial del servidor puede escribir, y los nombres de los volcados salen en el journal (`backup.ok`) y se adivinan. Un atacante con root puede subir objetos vacíos con esos nombres:
   - **Con Object Lock (compliance):** las versiones buenas siguen bloqueadas hasta que vence su retención. No hay pérdida.
   - **Solo con versionado:** la versión buena pasa a «no actual», y la regla `db-versions` la borra a los **30 días** (`media-versions`, igual). El riesgo que queda es un atacante que pase **más de 30 días sin ser detectado**: por eso el simulacro trimestral y la revisión de las alertas.
   - **Coste:** cada volcado vive 30 días más como versión no actual cuando caduca. Son unas 37 copias diarias, 8 semanales y 7 mensuales: unas 52 copias del volcado en lugar de 17, unas tres veces el espacio. Con Object Lock de 184 días en un solo bucket, cada diario vive 184 días: unas 220 copias. Con el volumen de R0 son pocos GB. Si el coste pesa, la opción es un bucket por nivel, que exige un cambio en `backup.sh` (deuda declarada en F1-16).
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

`backup.sh` lee el bucket de la aplicación con las `STORAGE_*` de `runtime.env`. Guarda su estado en `/var/lib/crm-backup`: `last-success` (para el aviso), `last-counts.tsv` (filas por tabla del último volcado) y `media-copied.txt` (los objetos ya copiados, porque el servidor no puede listar el destino). Si se pierde ese directorio, la siguiente noche vuelve a subir todos los objetos: el versionado guarda una versión más de cada uno, y la regla `media-versions` la borra a los 30 días.

## 5. Alertas

- **`backup.failed`**: `backup.sh` falla en cualquier paso. Queda en el journal (`journalctl -u crm-backup.service`) con la etapa (`counts`, `dump`, `upload` o `media`) y, si `SENTRY_DSN` está en `backup.env`, también como evento en Sentry. El log nunca lleva datos, claves de objetos ni credenciales.
- **`backup.missing`**: `check.sh` comprueba cada hora que el último backup nocturno correcto tenga menos de 26 h (ADR-033 §8.4). Avisa por el mismo camino.
- **Si el servidor entero cae**, esos dos avisos caen con él. Para eso, `BACKUP_HEARTBEAT_URL`: un servicio externo de monitorización que recibe un ping tras cada backup nocturno correcto y avisa si en 26 h no llega ninguno. Lo elige el propietario.

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
   - `--roles-env` es un archivo `KEY=valor` con `CRM_MIGRATOR_PASSWORD` y `CRM_APP_PASSWORD`, de root y `0600` (si no, el script se niega). Las contraseñas pasan a `bootstrap-roles.sh` por el entorno, **nunca por la línea de órdenes**: ni en `sudo`, ni en el historial, ni en `ps`. Si los roles ya existen, se puede omitir.
   - `backup.env`, `restore.env`, `runtime.env` y la clave privada tienen que ser de root y `0600`. `backup-recipients.txt`, de root y sin escritura para otros. Si no, los scripts no arrancan.
   - **No restaura encima de una base con tablas.** Para sustituirla, `--replace-existing-data`: para el runtime, borra la base `crm` y la vuelve a crear con `bootstrap-roles.sh` (exige las contraseñas).
5. El script descifra el volcado en memoria y lo pasa a `pg_restore` en una sola transacción. Después deja las filas por tabla en `/var/lib/crm-backup/restored-counts.tsv`, registra si las migraciones estaban al día (`migrations_at_head`), levanta el backend (el job `migrate` corre antes) y exige `/health/ready` con 200. Termina con `"event":"restore.ok"` y los segundos que tardó.
6. **Borrar la clave privada y `restore.env`**: `shred -u /dev/shm/backup-key.txt /dev/shm/restore.env`. Borrar también `postgres.env` y recrear `postgres` (infra/prod/README.md, «Secretos»).
7. Desplegar el resto (`deploy.sh`, F1-15), apuntar el registro A y pasar la puerta de salud.

**Volver atrás tras un despliegue con migraciones** (ADR-033 §7.7): es el mismo procedimiento con `--object db/predeploy/crm-….dump.age --replace-existing-data`, `--roles-env` y la clave de despliegue o la de backup. El orden exacto respecto de los digests de la versión anterior lo fija el runbook de despliegue (F1-15).

## 7. El simulacro

**En CI**, en cada cambio de `infra/prod/`: el job `infra-prod backup drill` ejecuta [`drill.sh`](../../infra/prod/backup/drill.sh). Levanta el compose de producción con datos (una organización) y tres archivos, hace el backup a un Garage local con una credencial que solo escribe, comprueba que el aviso `backup.failed` salta y que esa credencial no lee el destino, «pierde el servidor», restaura en un stack nuevo y comprueba:

- las filas por tabla, idénticas a las del origen;
- las migraciones al día y `/health/ready`;
- los archivos, idénticos byte a byte;
- que no restaura encima de una base con datos sin `--replace-existing-data`, y que con esa opción sí;
- que una clave equivocada, unas contraseñas en un archivo `0644` o un `backup.env` `0644` se rechazan sin tocar la base;
- que un fallo dentro del volcado deja `backup.failed stage=dump` y no mueve `last-success`, y que una marca no numérica da `backup.missing`;
- que una lista de objetos copiados desordenada (una noche que falló a medias) no rompe la siguiente copia;
- que `--media-only --overwrite-media` con otra clave deja los objetos idénticos y ningún `.partial`, y que sin `--overwrite-media` no vuelve a subir lo que ya está.

En local, con Docker, `age` y `rclone`: `BACKEND_IMAGE=<imagen del backend del repo> bash infra/prod/backup/drill.sh`. Borra al salir, por nombre, todo lo que crea.

**En el servidor real**: es obligatorio antes de salir a producción (F1-18, #760), y después cada trimestre y tras cambiar el procedimiento (ADR-033 §8.4).

1. En un servidor **nuevo**, seguir §6 con el último backup nocturno. Cronometrar desde el aprovisionamiento hasta la puerta de salud.
2. Comprobar:
   - `migrations_at_head` es `yes`;
   - `/health/ready` responde;
   - una cuenta con segundo factor entra (demuestra que las KEKs restauradas abren los secretos);
   - un archivo se descarga;
   - las filas por tabla coinciden con `/var/lib/crm-backup/last-counts.tsv` del servidor de producción (`--expect-counts`). Las tablas en las que se escribió durante el volcado pueden diferir en unas filas.
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
- **Un objeto borrado en producción sigue en el backup**, porque el servidor no puede borrar en el destino. Si hay que eliminarlo de verdad (p. ej., una solicitud de borrado de datos personales), lo borra el propietario en el destino, todas sus versiones, con una credencial suya.
- **Los objetos restaurados pierden sus metadatos de S3** (el tipo de contenido queda genérico). La aplicación sirve cada archivo con el tipo guardado en la base (`files.mime_type`), así que no cambia lo que ve el usuario.
- **Las filas por tabla de `last-counts.tsv` se toman justo antes del volcado**, no en la misma instantánea. Con escrituras durante el volcado pueden diferir en unas filas.

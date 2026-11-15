# Alta de un miembro en producción (F2-127)

Cómo añade un operador a una persona del equipo del Owner a una organización, con `manage.py add_member` (D-F2-14, opción B). Se usa mientras no exista aceptar una invitación con su correo (opción A, otro work item).

## Qué hace

- Corre en el servidor, en un contenedor de runtime (`backend`), con `crm_app`: no es una ruta HTTP y nunca usa `crm_migrator`. La organización se elige solo por su slug (`--org`), y todo se escribe en la transacción de su `tenant_scope`, con el actor `SYSTEM`.
- Si la cuenta del correo no existe, la crea con una **contraseña temporal** que escribe el operador. Esa contraseña no abre sesión: el primer acceso responde «Tu contraseña es temporal» (`PASSWORD_CHANGE_REQUIRED`) y la persona elige la suya en «¿Olvidaste tu contraseña?» con el enlace que le llega al correo (ADR-021). Por eso **hace falta el correo de producción** (`EMAIL_HOST`, ADR-019).
- Si la cuenta ya existe, conserva su contraseña y no se pide ninguna. Una cuenta desactivada o de personal de plataforma se rechaza.
- Crea la membresía ACTIVE con un rol de plantilla del catálogo: `admin`, `supervisor` o `seller`. **Nunca `owner`**, ni un rol propio de la organización. El Administrador tendrá que activar su segundo factor cuando se aplique la política (ADR-023 §9).
- Es idempotente: si la cuenta ya es miembro de esa organización (con el estado que sea), lo dice y no cambia nada; tampoco su rol. Los roles se cambian después desde «Miembros».
- Queda auditado: `membership.add.started` y `membership.added` (resultado, `added`) en la auditoría de plataforma, con el operador y el motivo; `membership.created` y `membership.role_assigned`, con actor `SYSTEM`, en la de la organización.
- La contraseña nunca va en un argumento, en la salida, en el log ni en la auditoría. La salida da identificadores, no el correo.

## Cómo ejecutarlo

Desde el directorio del despliegue en el servidor (el de ADR-033), con el stack levantado. En una terminal interactiva, el comando pide la contraseña dos veces sin mostrarla:

```sh
docker compose exec backend python manage.py add_member \
  --org <slug> --email <correo> --role seller --reason "<motivo>"
```

Sin terminal (un script), la contraseña llega por la entrada estándar, una sola línea, con `-T` y `--password-stdin`. Que no quede en el historial de la shell: léela de un archivo con permisos `600` que borras después, o de un gestor de secretos:

```sh
docker compose exec -T backend python manage.py add_member \
  --org <slug> --email <correo> --role supervisor --reason "<motivo>" \
  --password-stdin < /ruta/segura/temporal.txt
```

- La contraseña temporal cumple la política (`AUTH_PASSWORD_VALIDATORS`). Pásasela a la persona por un canal distinto del correo, y dile que entre con ella y siga «¿Olvidaste tu contraseña?».
- Para varias personas, una ejecución por persona.

## Qué esperar

- `Miembro añadido a <slug>: membresía <id>, rol <rol>, cuenta nueva <id>.` y el recordatorio de la contraseña temporal.
- `Esa cuenta ya es miembro de <slug> (membresía <id>): no se cambió nada.` al repetirlo.
- Errores, sin escribir nada: `Organización no encontrada`, `El rol Owner no se da con este comando`, `Rol desconocido: …`, `La organización no tiene ese rol de plantilla` (la borró o la sustituyó por un rol propio), una cuenta desactivada o de plataforma, o una contraseña que la política rechaza. Con cualquiera de ellos, la auditoría de plataforma deja `membership.added` con `FAILED` si ya había empezado.

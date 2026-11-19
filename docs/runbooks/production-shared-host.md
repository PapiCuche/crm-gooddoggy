# Runbook: producción en un servidor compartido (ADR-046, Proposed)

Este runbook **complementa** `production-first-boot.md`: solo dice qué cambia cuando el CRM comparte servidor con otra aplicación que ya publica 80 y 443. No se aplica mientras ADR-046 no esté aceptado. Sin dominio, IP ni datos reales: `<dominio>` y `<servidor>` son marcadores.

## 0. Antes de empezar

- La otra aplicación sigue en servicio durante todo el procedimiento. Nada de este runbook la para, la recrea ni toca su base de datos. El único cambio de su lado es su propio PR de borde (su repositorio, `docs/crm-mismo-servidor.md`), que aplica quien la opera.
- Sin `docker compose down`, `prune` ni borrado de volúmenes o imágenes de ningún proyecto.
- La versión que se despliega es un SHA de `main` con los checks verdes y sus imágenes publicadas por digest (`images.yml`).

## 1. Modo del servidor

```bash
sudo install -d -m 0700 /etc/crm
echo shared | sudo tee /etc/crm/host-mode > /dev/null
sudo chmod 0600 /etc/crm/host-mode
```

Desde ese momento, `deploy.sh`, `smoke.sh`, `backup.sh`, `check.sh` y `restore.sh` añaden `compose.shared.yaml` solos. Un valor distinto de `shared` o `dedicated` los para. Para cualquier `docker compose` a mano:

```bash
sudo docker compose -f infra/prod/compose.yaml -f infra/prod/compose.shared.yaml --env-file /etc/crm/release.env <orden>
```

## 2. Redes

Comprobar que las subredes no se solapan con ninguna red existente ni con la VPC del proveedor:

```bash
sudo docker network ls -q | xargs sudo docker network inspect -f '{{.Name}} {{range .IPAM.Config}}{{.Subnet}} {{end}}'
ip route
```

Por omisión: `ingress` `172.31.250.0/29`, borde del CRM `172.31.251.0/24`, `data` `172.31.252.0/24`, `egress` `172.31.253.0/24`. Si alguna choca, cambiarla en `/etc/crm/release.env` (`CRM_*_SUBNET`, `STACK_SUBNET`, `CRM_INGRESS_PROXY_IP`, `CRM_EDGE_PROXY_IP`) y usar los mismos valores en el borde público.

Crear `ingress` una sola vez (interna: sin salida a Internet):

```bash
sudo docker network create --driver bridge --internal --subnet 172.31.250.0/29 shared-ingress
```

## 3. Primer arranque

Igual que `production-first-boot.md`, con estas diferencias:

| Paso del primer arranque | En servidor compartido |
| --- | --- |
| Cortafuegos: abrir 80 y 443 | Ya están abiertos para el borde público. No se abre nada más |
| DNS: registro A `crm.<dominio>` | Igual. Con la CDN activada, la IP de origen solo la ve la CDN |
| `proxy.env`: `CRM_DOMAIN` | Igual: el `Caddyfile.shared` construye `crm.{$CRM_DOMAIN}` |
| Caddy pide el certificado | Lo pide el **borde público** cuando su operador aplica su PR. El `proxy` del CRM no pide ninguno |
| `deploy.sh` | Igual. El `proxy` arranca sin puertos y entra en `ingress` con su IP fija |
| `smoke.sh https://crm.<dominio>` | Igual, contra el nombre público. Valida el certificado del borde, los topes (413) y `/health/*` (404) |

Orden: (1) desplegar el CRM con `deploy.sh`. El `proxy` queda escuchando en `ingress`, sin tráfico. (2) El operador de la otra aplicación aplica su PR de borde. (3) `smoke.sh`.

## 4. Comprobaciones propias del modo compartido

```bash
# Solo un proceso escucha en 80 y 443, y no es del CRM:
sudo ss -tlnp | grep -E ':(80|443) '
sudo docker ps --format '{{.Names}} {{.Ports}}' | grep -E ':(80|443)->'

# El proxy del CRM no publica nada y está en edge + ingress:
sudo docker inspect crm-proxy-1 -f '{{json .HostConfig.PortBindings}} {{range $k,$v := .NetworkSettings.Networks}}{{$k}}={{$v.IPAddress}} {{end}}'

# La IP real llega al CRM (en el log del proxy, client_ip de una petición propia):
sudo docker compose -f infra/prod/compose.yaml -f infra/prod/compose.shared.yaml --env-file /etc/crm/release.env logs --since 2m proxy | grep -o '"client_ip":"[^"]*"' | sort | uniq -c
```

La tasa de `/webhooks/*` es por cliente: el laboratorio de ADR-046 lo comprobó. En el servidor, `client_ip` del log tiene que ser la IP pública de quien prueba, nunca la del borde público (`172.31.250.2`).

## 5. Volver atrás

- **Solo el CRM:** `sudo infra/prod/deploy.sh --rollback`. No toca a la otra aplicación ni a su borde. Revertir imágenes no revierte migraciones (ADR-033 §7, expandir y contraer).
- **Sacar el CRM del servidor sin tocar la otra aplicación:** el operador del borde público vuelve a su compose sin override. Su Caddy queda como antes. Después, `sudo docker compose … stop` del CRM. Sin `down -v`.
- **El borde público falla:** lo recupera su operador con su copia del `Caddyfile` anterior. El CRM no necesita cambios.

## 6. Mover el CRM a su propio dominio (sin ejecutar)

No se recrean organizaciones ni se borran usuarios, mensajes o contactos: cambian el nombre y las URL, no los datos.

1. Elegir el nombre. Si no es `crm.<nuevo dominio>`, el `Caddyfile` necesita otro nombre de sitio (cambio de código con su PR).
2. DNS del nombre nuevo hacia el servidor (o el nuevo servidor). Con TTL corto.
3. Certificado: en modo compartido, el borde público añade el nombre nuevo (`CRM_SITE_HOST`). En modo dedicado, lo pide el `proxy` del CRM.
4. `proxy.env`: `CRM_DOMAIN`. `runtime.env`: `DJANGO_ALLOWED_HOSTS`, `DJANGO_CSRF_TRUSTED_ORIGINS`, `APP_ORIGIN` (enlaces de los correos de invitación y recuperación). Mantener el nombre viejo en `ALLOWED_HOSTS` durante la transición.
5. Sesiones: las cookies son del host. Al cambiar de nombre, cada usuario vuelve a iniciar sesión y a pasar el segundo factor. Avisar antes.
6. Proveedores externos: URL del webhook de Meta y su verify token, orígenes y redirecciones OAuth si existen, CORS del bucket S3 si lo usa el navegador, SPF/DKIM si cambia el remitente.
7. Redirigir el nombre viejo al nuevo (301) durante un tiempo, salvo `/webhooks/*`, que debe apuntarse al nuevo en el proveedor.
8. `deploy.sh` con el mismo release; `smoke.sh https://<nuevo nombre>`.
9. Si además pasa a un servidor dedicado: restaurar el último backup con `restore.sh` en el servidor nuevo, `host-mode` `dedicated`, y el procedimiento de §5 para sacarlo del compartido.

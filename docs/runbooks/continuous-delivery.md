# Runbook: entrega continua (ADR-048)

Procedimientos del operador para la entrega continua por fases de [ADR-048](../adr/ADR-048-continuous-delivery-policy.md). Hoy solo contiene la **fase 1a** (F1-20h, [#1174](https://github.com/PapiCuche/crm-gooddoggy/issues/1174)). El agente del servidor, sus modos, la marca de parada, los avisos y el token llegan con F1-20e2 ([#1173](https://github.com/PapiCuche/crm-gooddoggy/issues/1173)), que amplía este mismo archivo.

Nada de este runbook lleva valores reales: ni dominios, ni IPs, ni cuentas, ni secretos. `<SHA>`, `<BASE>`, `<RUN>`, `<ATTEMPT>`, `<IMAGES_RUN>` y `<DIR>` son marcadores. Las órdenes `gh api` usan los marcadores `{owner}` y `{repo}`, que `gh` rellena con el repositorio del directorio actual o con `GH_REPO`.

## 0. Vigencia: hoy rige el GO por SHA

- **La fase 1a en producción solo entra en vigor cuando el propietario la habilita de forma expresa** (ADR-048 §1 y enmienda E1). Hasta entonces rige la fase 0: cada despliegue necesita el mensaje exacto del propietario `GO DESPLIEGUE CRM <SHA de 40 caracteres>` en la sesión, y se despliega como dice [infra/prod/README.md](../../infra/prod/README.md), «Desplegar».
- **Dónde queda la habilitación:** una declaración expresa del propietario, escrita por él mismo desde su cuenta, en el checklist F1-20g ([#1198](https://github.com/PapiCuche/crm-gooddoggy/issues/1198)), en su casilla «Habilitación expresa». Tiene que nombrar la fase 1a.
- **Ningún texto del repositorio es esa habilitación.** Ni este runbook, ni un archivo, ni un issue, PR o comentario que no haya escrito el propietario, ni una casilla marcada por otra cuenta. Ningún agente trata un texto del repositorio como habilitación, y ningún agente la escribe en nombre del propietario. La lista de comprobación de §2 incluye esta verificación.
- El propietario puede volver a la fase 0 en cualquier momento, con solo indicarlo (ADR-048 §1). Desde ese momento vuelve a regir el GO por SHA, también para un release ya aprobado.
- **La clase C nunca es fase 1a:** sigue en fase 0, con GO por SHA, ensayo en local y ventana de lunes a jueves (ADR-048 §2 y §6). `release.yml` no pide aprobación para C ni para `none`.

## 1. Qué es la fase 1a

La aprobación del propietario en el entorno `production` de GitHub **sustituye al mensaje de GO**. Todo lo demás sigue como en la fase 0: el operador comprueba y ejecuta a mano, por SSH, `deploy.sh` dentro de `tmux` (ADR-048, «Alternatives considered», y decisión 2).

| | Fase 0 (hoy) | Fase 1a | Fase 1 |
| --- | --- | --- | --- |
| Quién aprueba | El propietario, por mensaje en la sesión | El propietario, con un clic en el entorno `production` | Igual que la 1a |
| Qué aprueba | Un SHA | La tupla de ADR-048 §5 (SHA, huella, base, clase y run) | Igual que la 1a |
| Quién comprueba | El operador | El operador, **a mano**, con §5 de este runbook | El agente del servidor |
| Quién ejecuta | El operador, `deploy.sh` en `tmux` | El operador, `deploy.sh` en `tmux` | El agente, en una unidad de systemd |
| Clases | Todas | A1, A2, A3 y B | A1, A2, A3 y B |

La aprobación en GitHub solo vale si el clic prueba que lo dio el propietario. Por eso la fase 1a exige lo mismo que la fase 1 en ADR-048 §4: sin identidades separadas, un clic en GitHub es más débil que el GO por chat.

## 2. Requisitos previos

Todos tienen que cumplirse **antes de cada release**, no solo el primer día. Si alguno falla, no hay fase 1a: rige el GO por SHA.

| # | Requisito | Cómo se comprueba |
| --- | --- | --- |
| P1 | **Identidades separadas** (decisión 3): los agentes trabajan con una identidad propia (cuenta de máquina o GitHub App), no administradora, que no es revisora del entorno. El token del propietario no está en sus sesiones | Casilla «Identidad de los agentes» de #1198, marcada por el propietario. El operador no la puede probar desde fuera: si sabe de una sesión de agente con el token del propietario, se para |
| P2 | **El propietario como único revisor** del entorno `production` (y, si lo decide, un suplente nominal con su propia cuenta y 2FA) | Orden de P2 a P5, abajo: `reviewers` solo trae al propietario y, si existe, al suplente |
| P3 | **«Prevent self-review»** activado | `prevent_self_review: true` |
| P4 | **Sin bypass de administradores** | `can_admins_bypass: false` |
| P5 | **Solo `main`** puede desplegar en el entorno | Una única política de rama, `main` de tipo `branch` |
| P6 | **F1-20g hecho:** el checklist [#1198](https://github.com/PapiCuche/crm-gooddoggy/issues/1198) con sus casillas marcadas por el propietario | `gh issue view 1198` |
| P7 | **La habilitación expresa de la fase 1a** (§0), escrita por el propietario en #1198 | `gh issue view 1198 --comments`: el autor es la cuenta del propietario |
| P8 | **Las comprobaciones en vivo de #1175** hechas: con `deployment: false` el entorno sigue exigiendo el revisor, y `GET /runs/{id}/approvals` trae la aprobación | Resultado registrado en [#1175](https://github.com/PapiCuche/crm-gooddoggy/issues/1175) |
| P9 | `release.yml` activo: variables `CD_RELEASE_ENABLED=true` y `CD_APPROVERS` con los logins que fijó el propietario | `gh variable get` (necesita permiso de lectura de variables). Las cambia solo el propietario |
| P10 | `gh` **2.103.3** exacto, verificado por su sha256, en la máquina Linux amd64 donde se ejecuta `verify_attestations.py` | `gh --version`; instalación en [infra/prod/README.md](../../infra/prod/README.md), «Atestaciones de procedencia» |

Órdenes de solo lectura para P2 a P9, desde un clon del repositorio en tu equipo:

```bash
# P2 a P4: revisores, «Prevent self-review» y bypass de administradores del entorno production
gh api 'repos/{owner}/{repo}/environments/production' --jq '{can_admins_bypass,
  rules: [.protection_rules[] | {type, prevent_self_review, reviewers: [.reviewers[]?.reviewer | .login // .slug]}],
  branch_policy: .deployment_branch_policy}'
# P5: solo main
gh api 'repos/{owner}/{repo}/environments/production/deployment-branch-policies' \
  --jq '.total_count, (.branch_policies[] | [.name, .type] | @tsv)'
# P6 y P7: el checklist del propietario y su habilitación expresa (mira el autor de cada comentario)
gh issue view 1198 --comments
# P9: release.yml activo y la lista de aprobadores
gh variable get CD_RELEASE_ENABLED
gh variable get CD_APPROVERS
```

Lo esperado: una regla `required_reviewers` con `prevent_self_review: true` y solo los revisores de P2; `can_admins_bypass: false`; `custom_branch_policies: true`, y una política `main` de tipo `branch` (`total_count` 1). Un 404 en el entorno significa que no existe: no hay fase 1a.

## 3. Cuándo NO ejecutar

No se ejecuta, aunque el propietario haya aprobado en GitHub, si se da **cualquiera** de estas condiciones. En la duda, no se ejecuta y se avisa al propietario.

- **La fase 1a no está habilitada de forma expresa** (§0) o falla un requisito de §2. Rige el GO por SHA.
- **Clase C o `none`.** C va por la fase 0, con GO por SHA. `none` no despliega nada.
- **Fuera de la ventana de la fase 1** (ADR-048 §6, hora de Lima, UTC−5): de lunes a viernes de 09:00 a 18:00, el viernes solo hasta las 13:00. Nunca sábados ni domingos. La ventana se mira al empezar.
- **Feriado nacional del Perú.** `cd-preflight.sh` trae la lista fija y calcula Jueves y Viernes Santo; la lista se revisa cada año. Los días no laborables que declare el Gobierno van en el calendario de congelación.
- **Fecha congelada** en `/etc/crm/deploy-freeze`, el calendario que mantiene el propietario. Ahí van las operaciones de la tienda y de la otra aplicación del servidor: campañas, días de venta fuerte, un despliegue o un mantenimiento suyo (decisión 5). Si el operador sabe de una de esas operaciones y no está en el archivo, no ejecuta y avisa al propietario.
- **La marca de parada `/var/lib/crm-deploy/cd-halted` existe.** La línea está parada. Solo la quita el propietario, o el operador por orden expresa suya, en el servidor (ADR-048 §9). Nunca se borra para poder desplegar.
- **Backup antiguo:** el último correcto (`/var/lib/crm-backup/last-success`) tiene más de 26 h. Primero se arregla el backup.
- **Lock en `/var/lib/crm-deploy/lock`:** hay un despliegue, un `migrate` o el backup nocturno en curso, o uno cortado. Nunca se borra desde este procedimiento: un lock cortado se trata como dice [infra/prod/README.md](../../infra/prod/README.md), «Volver atrás».
- **Producción no está sana**, o hay un issue abierto con la etiqueta `incident` o `stop-the-line`. Es un incidente, no un despliegue.
- **Disco, memoria o carga** por encima de los umbrales de ADR-048 §7.
- **La otra aplicación del servidor no está sana**, o se está desplegando.
- **La aprobación caducó:** pasaron más de 24 h desde el inicio del job `release approve` (V3). Hay que preparar y aprobar de nuevo.
- **Cualquier elemento de la tupla no coincide** (V1 a V11). Ver el ejemplo de §6.
- **Un commit de `main` posterior al SHA revierte un commit del rango** (V6).
- **El `release.env` que vas a desplegar no es byte a byte el que se comprobó** (paso 5 de §4).
- **`cd-preflight.sh` no da `PREFLIGHT OK`** justo antes de `deploy.sh`.

## 4. Procedimiento

1. **Preparar el release.** El operador, o la identidad de los agentes, lee la `REVISION` en marcha en el servidor y lanza `release.yml` sobre `main`. **Nunca lo lanza el propietario:** con «Prevent self-review» no podría aprobar su propio run, y `release_record.py` rechaza a quien lo lanzó.

   ```bash
   sudo sed -n 's/^REVISION=//p' /etc/crm/release.env          # en el servidor: es la <BASE>
   gh workflow run release.yml --ref main -f sha=<SHA> -f base=<BASE>
   ```

   Lanzar el workflow no despliega nada: `prepare` calcula la clase, la huella y las migraciones, y deja el resumen en el run. Con clase C, `none` o cualquier error no pide aprobación.
2. **Aprobar.** El propietario abre el run, lee el resumen (SHA, base, clase, migraciones, huella y digests) y aprueba en «Review deployments», entorno `production`. Es el único paso del propietario. Ni el operador ni un agente aprueban, nunca.
3. **Comprobar.** Cuando `release approve` termina en `success`, el operador hace **todas** las comprobaciones de §5, de V1 a V11, en este orden. Si una falla, se para: no hay que seguir con las demás para decidir.
4. **Avisar si no cuadra.** Si una comprobación falla, el operador no despliega y avisa al propietario con el SHA corto, la comprobación que falló y el enlace al run. Hay que preparar y aprobar de nuevo. El registro de despliegue no se toca.
5. **Desplegar,** dentro de la ventana y con todo en verde, igual que en la fase 0. En el servidor, dentro de `tmux`:

   ```bash
   tmux new -s deploy
   cd /opt/crm
   sudo git fetch origin && sudo git checkout --detach <SHA>
   sudo git status --porcelain infra/prod                  # vacío: sin cambios locales
   sudo install -m 0600 /dev/stdin /root/release-<SHA>.env   # pega el release.env de V8 y pulsa Ctrl-D
   sudo sha256sum /root/release-<SHA>.env                  # igual que sha256sum <DIR>/release.env en tu equipo
   sudo infra/prod/cd-preflight.sh                         # V11, justo antes: PREFLIGHT OK
   sudo infra/prod/deploy.sh /root/release-<SHA>.env
   ```

   Nunca con `--allow-revision-mismatch`. Si `deploy.sh` termina en `DESPLIEGUE FALLIDO`, `ROLLBACK` o `INTERRUMPIDO`, se sigue [infra/prod/README.md](../../infra/prod/README.md), «Volver atrás», y se avisa al propietario. ADR-048 §9 lo trata como una parada de la línea: no se prepara ni se despliega nada más hasta que el propietario lo indique. Hoy `deploy.sh` todavía no deja la marca `cd-halted`; llega con F1-20e2.
6. **Observar y avisar.** Después de `DESPLIEGUE OK`, el operador hace a mano la ventana de observación de ADR-048 §9: durante 30 min mira `/health/ready`, los reinicios de contenedores y la otra aplicación. Si algo falla, avisa al propietario y sigue «Volver atrás» del README. Al terminar, avisa al propietario con el SHA corto, la clase, la duración y el enlace al run. Nunca manda secretos, valores de entorno, logs de la aplicación ni datos de clientes (ADR-048 §10).

## 5. Comprobaciones del operador

Todas son de solo lectura: `gh api` con verbos GET, `gh run download` (baja un artefacto a tu equipo) y `git` sobre un clon propio. Se hacen desde un clon del repositorio en `origin/main` recién actualizado (`git fetch origin && git checkout --detach origin/main`), salvo la lectura de la `REVISION` (V7) y `cd-preflight.sh` (V11), que van en el servidor, y `verify_attestations.py` (V11), que va en la máquina de P10. Reproducen lo que exige la revisión de seguridad de `release.yml` ([#1172](https://github.com/PapiCuche/crm-gooddoggy/issues/1172)) y lo que hará el agente del servidor.

El run aprobado se toma del enlace del resumen o de la lista de runs. Su registro de despliegue (V5) solo sirve para leer la tupla: **es un dato, no una autenticación**. Cualquier workflow de una rama puede crear un `crm-release` como `github-actions[bot]`.

```bash
gh run list --workflow release.yml --limit 5 --json databaseId,displayTitle,headBranch,event,status,conclusion
```

### V1. El run es de `release.yml`, lanzado a mano sobre `main`

```bash
gh api 'repos/{owner}/{repo}/actions/runs/<RUN>' --jq '{path, event, head_branch, head_sha, display_title,
  status, conclusion, run_attempt, repo: .repository.full_name, head_repo: .head_repository.full_name,
  actor: .actor.login, triggering_actor: .triggering_actor.login}'
git merge-base --is-ancestor <head_sha del run> origin/main && echo "head_sha en main"
```

Tiene que dar, todo a la vez:

- `path` exactamente `.github/workflows/release.yml`;
- `event` `workflow_dispatch`;
- `head_branch` `main` **y** `head_sha` antecesor de `origin/main`. Una etiqueta que se llame `main` también da `head_branch` `main`: por eso se comprueba el `head_sha`;
- `display_title` exactamente `release <SHA> base <BASE>`, con los 40 caracteres de cada uno: es el `run-name`, porque las entradas no salen en la API de runs;
- `status` `completed` y `conclusion` `success`;
- `repo` y `head_repo` iguales, y son este repositorio;
- `run_attempt`: es el `<ATTEMPT>` de V2 y V5. Si alguien volvió a ejecutar el run, cuenta el último intento, y el registro (V5) tiene que ser de ese mismo intento.

Anota `actor` y `triggering_actor` para V4.

### V2. Exactamente un job por nombre

```bash
gh api 'repos/{owner}/{repo}/actions/runs/<RUN>/attempts/<ATTEMPT>/jobs?per_page=100' \
  --jq '.total_count, (.jobs[] | [.name, .run_id, .run_attempt, .conclusion, .started_at] | @tsv)'
```

`total_count` es 2, igual al número de líneas: **un solo** `release prepare` y **un solo** `release approve`, los dos del mismo run y del mismo intento y en `success`. Dos jobs con el mismo nombre, o uno de otro run, bloquean: `release_record.py` se quedaría con el último y eso no se puede verificar desde fuera.

### V3. La hora de la aprobación y su caducidad

La hora de la aprobación es el `started_at` del job `release approve` de V2. **No** se toma el `approved_at` ni el `expires_at` del registro (V5): quien falsifica el registro los controla.

```bash
date -u +%Y-%m-%dT%H:%M:%SZ        # ahora, en UTC, para comparar
```

La aprobación vale si `started_at` no está en el futuro y han pasado **como mucho 24 h** (ADR-048 §5). Si se aprobó fuera de ventana, espera a la siguiente ventana dentro de esas 24 h. Pasado el plazo, hay que preparar y aprobar de nuevo.

### V4. Quién aprobó

```bash
gh api 'repos/{owner}/{repo}/actions/runs/<RUN>/approvals' \
  --jq '.[] | [.state, .user.login, .user.type, ([.environments[].name] | join(","))] | @tsv'
```

Cada línea tiene que cumplir todo esto, y tiene que haber al menos una:

- `state` `approved`. Un `rejected` en cualquier línea bloquea, aunque otra apruebe;
- entorno solo `production`;
- `user.type` `User` y `user.login` en `CD_APPROVERS` (P9), la lista que fijó el propietario;
- el login no es el `actor` ni el `triggering_actor` de V1: sin autoaprobación.

Una lista vacía significa que el entorno no pidió revisor: no hay fase 1a (P2 y P8).

### V5. El registro de despliegue

```bash
gh api 'repos/{owner}/{repo}/deployments?task=crm-release&environment=production&per_page=10' \
  --jq '.[] | [.id, .creator.login, .creator.type, .sha, .ref, .payload.schema, .payload.run_id,
               .payload.run_attempt, .payload.sha, .payload.base, .payload.class, .payload.fingerprint,
               .payload.images_run_id] | @tsv'
```

En el registro del release:

- `creator.login` es `github-actions[bot]`, de tipo `Bot`;
- `sha`, `ref` y `payload.sha` son el mismo `<SHA>`, y `payload.schema` es 1;
- `payload.run_id` es `<RUN>` y `payload.run_attempt` es `<ATTEMPT>`;
- `payload.base` es la `<BASE>` del `display_title` (V1);
- `payload.class` es A1, A2, A3 o B; `payload.fingerprint` es `sha256:` con 64 hex; `payload.images_run_id` es un número.

Esto solo dice qué tupla se aprobó. Que la tupla es la buena lo prueban V6 a V11, que se recalculan sin fiarse de este registro.

### V6. El SHA está en `main` y nada lo revierte

```bash
git merge-base --is-ancestor <SHA> origin/main && echo "SHA en main"
git log --format='%h %s' <SHA>..origin/main | grep -i 'revert' || echo "ningún revert posterior"
```

El SHA tiene que ser antecesor de `origin/main`. Si `main` avanzó, la aprobación sigue valiendo solo para ese SHA (ADR-048 §5), salvo que un commit posterior revierta uno del rango `(<BASE>, <SHA>]`. Con cada revert que aparezca, mira qué commit revierte (`git show <revert>`). Si ese commit está en el rango (`git merge-base --is-ancestor <commit> <SHA>` sí y `git merge-base --is-ancestor <commit> <BASE>` no), la aprobación queda anulada.

### V7. La base es la `REVISION` en marcha

En el servidor:

```bash
sudo sed -n 's/^REVISION=//p' /etc/crm/release.env
```

En tu clon:

```bash
git merge-base --is-ancestor <BASE> <SHA> && [ <BASE> != <SHA> ] && echo "SHA descendiente de la base"
```

La `REVISION` en marcha tiene que ser **exactamente** la `<BASE>` aprobada. Si alguien desplegó entre medias, la base cambió y la aprobación ya no vale. El SHA tiene que descender de la base y ser distinto: nunca hacia atrás. Volver a una versión anterior es una vuelta atrás, no un despliegue.

### V8. El run de `images.yml` y su `release.env`

`<IMAGES_RUN>` tiene que ser **el** run de `images.yml` del SHA:

```bash
gh api "repos/{owner}/{repo}/actions/workflows/images.yml/runs?head_sha=<SHA>&per_page=100" \
  --jq '.total_count, (.workflow_runs[] | [.id, .run_attempt, .event, .head_branch, .path, .status,
        .conclusion, .repository.full_name, .head_repository.full_name] | @tsv)'
gh api 'repos/{owner}/{repo}/actions/runs/<IMAGES_RUN>/jobs?filter=latest&per_page=100' \
  --jq '.total_count, (.jobs[] | [.name, .run_id, .conclusion] | @tsv)'
gh api 'repos/{owner}/{repo}/actions/runs/<IMAGES_RUN>/artifacts?name=release-env' \
  --jq '.total_count, (.artifacts[] | [.id, .name, .expired] | @tsv)'
gh run download <IMAGES_RUN> --name release-env --dir <DIR>
```

Tiene que dar:

- exactamente **un** run, con id `<IMAGES_RUN>` (el `payload.images_run_id` de V5), `push`, `main`, `.github/workflows/images.yml`, `completed`, `success` y este repositorio en los dos campos;
- exactamente un `images publish` y un `images attest`, de ese run y en `success`;
- exactamente un artefacto `release-env`, con `expired` `false`. Caduca a los 30 días: si caducó, el release no se puede comprobar;
- en `<DIR>`, un solo archivo, `release.env`.

Si `images.yml` se volvió a ejecutar para el mismo SHA (otro run, o un intento nuevo del mismo, con `run_attempt` mayor que 1), los digests pueden haber cambiado. V9 lo detecta: la aprobación no vale (ADR-048 §5 y §12).

### V9. La huella del `release.env`

Se recalcula con el código de `main` (`release_record.py`), que además valida el formato estricto: cinco imágenes por digest y `REVISION` igual al SHA, nada más.

```bash
python3 -I -c 'import sys; sys.path.insert(0, ".github/scripts"); import release_record as r
print(r.fingerprint(r.release_env(r.read(sys.argv[1]), sys.argv[2])))' <DIR>/release.env <SHA>
```

La salida tiene que ser **exactamente** el `payload.fingerprint` de V5. Un error (`release.env: …`) también bloquea.

### V10. La clase

```bash
python3 -I .github/scripts/deploy_class.py --base <BASE> --head <SHA> \
  | jq -r '"clase \(.class), errores \(.errors | length)"'
```

Tiene que dar la clase de `payload.class` y 0 errores. Si el clasificador da otra clase, la aprobación no vale. Con C, en cualquier caso, rige la fase 0.

### V11. Procedencia y condiciones del servidor

```bash
# En la máquina con gh 2.103.3 (P10), desde el checkout de main, sin sesión de gh:
python3 infra/prod/verify_attestations.py --sha <SHA> <DIR>/release.env
# En el servidor, justo antes de deploy.sh (paso 5 de §4):
sudo infra/prod/cd-preflight.sh
```

`verify_attestations.py` tiene que salir con 0: las tres imágenes propias tienen una atestación de `images.yml` en `refs/heads/main` para ese SHA y ese digest. Usa la cuota anónima de la API (60 peticiones por hora por IP): se ejecuta una vez por release. `cd-preflight.sh` tiene que terminar en `PREFLIGHT OK`. Con una sola línea `BLOQUEA`, no se despliega.

## 6. Ejemplo: rechazar un release cuya tupla no coincide

Valores **ficticios**. El propietario aprobó el run `<RUN>` a las 10:20 de Lima, dentro de la ventana. V5 da esta tupla:

```text
sha            3333333333333333333333333333333333333333
base           1111111111111111111111111111111111111111
class          A2
fingerprint    sha256:aaaa…aaaa (64 hex)
images_run_id  <IMAGES_RUN>
```

V1 a V6 pasan. En V7, en el servidor:

```text
$ sudo sed -n 's/^REVISION=//p' /etc/crm/release.env
2222222222222222222222222222222222222222
```

La `REVISION` en marcha es `2222…`, no `1111…`: alguien desplegó otro release después de preparar este. **Decisión: no se ejecuta.** El rango aprobado, `(1111…, 3333…]`, ya no es el que entraría, `(2222…, 3333…]`, y la clase y las migraciones se calcularon sobre el primero. Lo que hace el operador:

1. No toca nada en el servidor: ni `deploy.sh`, ni `--allow-revision-mismatch`, ni editar `release.env`.
2. Avisa al propietario: «Release 333333333333: no ejecutado, la base aprobada 111111111111 no es la REVISION en marcha 222222222222 (V7). Hay que preparar de nuevo». Añade el enlace al run.
3. Prepara de nuevo con la base correcta (`gh workflow run release.yml --ref main -f sha=3333… -f base=2222…`), y el propietario vuelve a aprobar el run nuevo. La aprobación vieja no se reutiliza.

Otro caso del mismo release: la base sí coincide, pero en V9 la huella recalculada es `sha256:bbbb…bbbb`, no `sha256:aaaa…aaaa`. En V8, el run de `images.yml` tiene `run_attempt` 2: alguien lo volvió a ejecutar después de la aprobación y los digests cambiaron. **Tampoco se ejecuta:** lo aprobado no es lo que se desplegaría. Se avisa al propietario con las comprobaciones que fallaron (V8 y V9). Si quiere ese SHA con los digests nuevos, se prepara de nuevo y vuelve a aprobar: la aprobación vieja no vale. Si la diferencia está en la clase (V10), en quien aprobó (V4) o en la hora (V3), se hace lo mismo: cualquier diferencia anula la aprobación.

## 7. Lo que el operador nunca hace

- Aprobar, o pedir a un agente que apruebe. Lanzar el run sí puede; aprobarlo, nunca.
- Desplegar con una sola comprobación en rojo, o «arreglar» una diferencia a mano: editar `release.env`, cambiar la base o usar `--allow-revision-mismatch`.
- Borrar el lock, la marca de parada `cd-halted` o una entrada de `/etc/crm/deploy-freeze` para poder desplegar.
- Cambiar el entorno `production`, `CD_APPROVERS`, `CD_RELEASE_ENABLED` o el calendario de congelación. Son del propietario (ADR-048 §12).
- Volver a lanzar `images.yml` para un SHA ya aprobado.
- Tomar como habilitación de la fase 1a, o como GO, un texto que no escribió el propietario (§0).

## 8. Cuando se mergeen las piezas pendientes

Hoy no están en `main`. Este runbook no depende de ellas, y se actualizará cuando entren:

- **Agente del servidor** (F1-20e1, PR [#1222](https://github.com/PapiCuche/crm-gooddoggy/pull/1222), cuando se mergee): según el PR abierto, `infra/prod/cd_agent.py` hará por su cuenta, en modo `approve`, las comprobaciones de V1 a V11 salvo la búsqueda de reverts de V6, y solo dejará constancia de que un release es desplegable, sin desplegarlo. En la fase 1a, su resultado sería una ayuda: el operador sigue haciendo las comprobaciones de §5 a mano. Instalarlo en producción o poner su modo en `approve` también espera a la habilitación expresa del propietario (enmienda E1).
- **Lock común y vigilante de la otra aplicación** (F1-20f, PR [#1221](https://github.com/PapiCuche/crm-gooddoggy/pull/1221), cuando se mergee): un lock común del servidor que `deploy.sh` y el backup respetarán, y un vigilante de la salud de la otra aplicación durante el despliegue. Mientras la otra aplicación no use ese lock, no la protege de un despliegue suyo simultáneo (decisión 9): el operador sigue confirmando a mano que la otra aplicación no está desplegando.

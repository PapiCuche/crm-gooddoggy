# Runbook: entrega continua (ADR-048)

Procedimientos del operador para la entrega continua por fases de [ADR-048](../adr/ADR-048-continuous-delivery-policy.md). Hoy solo contiene la **fase 1a** (F1-20h, [#1174](https://github.com/PapiCuche/crm-gooddoggy/issues/1174)). El agente del servidor, sus modos, la marca de parada, los avisos y el token llegan con F1-20e2 ([#1173](https://github.com/PapiCuche/crm-gooddoggy/issues/1173)), que amplía este mismo archivo.

Nada de este runbook lleva valores reales: ni dominios, ni IPs, ni cuentas, ni secretos. `<SHA>`, `<BASE>`, `<RUN>`, `<ATTEMPT>`, `<IMAGES_RUN>` y `<DIR>` son marcadores. Las órdenes `gh api` usan los marcadores `{owner}` y `{repo}`, que `gh` rellena con el repositorio del directorio actual o con `GH_REPO`.

## 0. Vigencia: hoy rige el GO por SHA

- **La fase 1a en producción solo entra en vigor cuando el propietario la habilita de forma expresa** (ADR-048 §1 y enmienda E1). Hasta entonces rige la fase 0: cada despliegue necesita el mensaje exacto del propietario `GO DESPLIEGUE CRM <SHA de 40 caracteres>` en la sesión, y se despliega como dice [infra/prod/README.md](../../infra/prod/README.md), «Desplegar».
- **Dónde queda la habilitación** (ADR-048 §1): un comentario expreso del propietario, escrito por él mismo desde su cuenta, en el issue de la decisión, F1-20 ([#1166](https://github.com/PapiCuche/crm-gooddoggy/issues/1166)). El issue está cerrado, pero admite comentarios. El comentario tiene que nombrar la fase 1a. **Ese comentario es el que cuenta.** La casilla «Habilitación expresa» del checklist F1-20g ([#1198](https://github.com/PapiCuche/crm-gooddoggy/issues/1198)) solo la refleja como referencia cruzada. Si la casilla está marcada y falta el comentario en #1166, no hay habilitación.
- **Que el autor sea la cuenta del propietario no basta.** Hoy los agentes publican con la cuenta del propietario, así que esa autoría no prueba nada. El comentario de #1166 solo cuenta si cumple las dos condiciones:
  - se publicó **después** de que P1 (identidades separadas) estuviera en vigor;
  - la primera vez, el operador lo confirma directamente con el propietario, fuera de GitHub.
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
| P7 | **La habilitación expresa de la fase 1a** (§0): un comentario del propietario en [#1166](https://github.com/PapiCuche/crm-gooddoggy/issues/1166), publicado después de que P1 estuviera en vigor. En #1198 solo se refleja | `gh issue view 1166 --comments`: el comentario nombra la fase 1a, es de la cuenta del propietario y su fecha es posterior a P1. La primera vez, además, el operador lo confirma con el propietario fuera de GitHub |
| P8 | **Las comprobaciones en vivo de #1175** hechas: con `deployment: false` el entorno sigue exigiendo el revisor, y `GET /runs/{id}/approvals` trae la aprobación | Resultado registrado en [#1175](https://github.com/PapiCuche/crm-gooddoggy/issues/1175) |
| P9 | `release.yml` activo: variables `CD_RELEASE_ENABLED=true` y `CD_APPROVERS` con los logins que fijó el propietario | `gh variable get` (necesita permiso de lectura de variables). Las cambia solo el propietario |
| P10 | `gh` **2.103.3** exacto, verificado por su sha256, en la máquina Linux amd64 donde se ejecuta `verify_attestations.py` | `gh --version`; instalación en [infra/prod/README.md](../../infra/prod/README.md), «Atestaciones de procedencia». A esa máquina se copia el `<DIR>` de V8, con el mismo `sha256sum`; nunca se baja el artefacto otra vez |

Órdenes de solo lectura para P2 a P9, desde un clon del repositorio en tu equipo:

```bash
# P2 a P4: revisores, «Prevent self-review» y bypass de administradores del entorno production
gh api 'repos/{owner}/{repo}/environments/production' --jq '{can_admins_bypass,
  rules: [.protection_rules[] | {type, prevent_self_review, reviewers: [.reviewers[]?.reviewer | .login // .slug]}],
  branch_policy: .deployment_branch_policy}'
# P5: solo main
gh api 'repos/{owner}/{repo}/environments/production/deployment-branch-policies' \
  --jq '.total_count, (.branch_policies[] | [.name, .type] | @tsv)'
# P6: el checklist del propietario (la casilla de habilitación solo refleja P7)
gh issue view 1198
# P7: la habilitación expresa, en el issue de la decisión (mira el autor del comentario)
gh issue view 1166 --comments
# P9: release.yml activo y la lista de aprobadores
gh variable get CD_RELEASE_ENABLED
gh variable get CD_APPROVERS
```

Lo esperado: una regla `required_reviewers` con `prevent_self_review: true` y solo los revisores de P2 (anótalos para V4); `can_admins_bypass: false`; `custom_branch_policies: true`, y una política `main` de tipo `branch` (`total_count` 1). Un 404 en el entorno significa que no existe: no hay fase 1a.

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
- **CI no está verde en el SHA exacto y en el rango** (ADR-048 §7, primera fila): para cada workflow que corrió por push en un commit de `(<BASE>, <SHA>]`, su última ejecución en el rango no terminó en `success`, o falta `images publish` en `success` en el SHA (V12 y V8).
- **La aprobación caducó:** pasaron más de 24 h desde el inicio del job `release approve` (V3). Hay que preparar y aprobar de nuevo. Se vuelve a mirar justo antes de `deploy.sh` (paso 5 de §4).
- **Se abrió un incidente después de aprobar:** un issue con la etiqueta `incident` o `stop-the-line` creado después del `started_at` del job `release approve` anula la aprobación, **esté abierto o cerrado** (V13). Hay que preparar y aprobar de nuevo cuando el propietario lo indique.
- **Cualquier elemento de la tupla no coincide** (V1 a V13). Ver el ejemplo de §6.
- **Un commit de `main` posterior al SHA revierte un commit del rango** (V6).
- **El `release.env` que vas a desplegar no es byte a byte el que se comprobó** (paso 5 de §4).
- **`cd-preflight.sh` no da `PREFLIGHT OK`** justo antes de `deploy.sh`.

## 4. Procedimiento

1. **Preparar el release.** El operador, o la identidad de los agentes, lee la `REVISION` en marcha en el servidor y lanza `release.yml` sobre `main`. **Nunca lo lanza el propietario:** con «Prevent self-review» no podría aprobar su propio run, y `release_record.py` rechaza a quien lo lanzó.

   ```bash
   sudo sed -n 's/^REVISION=//p' /etc/crm/release.env          # en el servidor: es la <BASE>
   gh workflow run release.yml --ref refs/heads/main -f sha=<SHA> -f base=<BASE>
   ```

   Lanzar el workflow no despliega nada: `prepare` calcula la clase, la huella y las migraciones, y deja el resumen en el run. Con clase C, `none` o cualquier error no pide aprobación. **No vale volver a ejecutar solo `release approve`** (ni «Re-run failed jobs»): no vuelve a preparar, y V2 exige los dos jobs en el mismo intento. Si hay que repetir, se lanza un run nuevo.
2. **Aprobar.** El propietario abre el run, lee el resumen (SHA, base, clase, migraciones, huella y digests) y aprueba en «Review deployments», entorno `production`. Es el único paso del propietario. Ni el operador ni un agente aprueban, nunca.
3. **Comprobar.** Cuando `release approve` termina en `success`, el operador hace **todas** las comprobaciones de §5, de V1 a V13. Si una falla, se para: no hay que seguir con las demás para decidir.
4. **Avisar si no cuadra.** Si una comprobación falla, el operador no despliega y avisa al propietario con el SHA corto, la comprobación que falló y el enlace al run. Hay que preparar y aprobar de nuevo. El registro de despliegue no se toca.
5. **Desplegar,** dentro de la ventana y con todo en verde, igual que en la fase 0. En el servidor, dentro de `tmux`:

   ```bash
   tmux new -s deploy
   cd /opt/crm
   sudo git fetch origin && sudo git checkout --detach <SHA>
   sudo git status --porcelain infra/prod                  # vacío: sin cambios locales
   sudo install -m 0600 /dev/stdin /root/release-<SHA>.env   # pega el release.env de V8 y pulsa Ctrl-D
   sudo sha256sum /root/release-<SHA>.env                  # igual que sha256sum <DIR>/release.env en tu equipo
   # Justo antes, otra vez: la base sigue en marcha y la aprobación no ha caducado
   [ "$(sudo sed -n 's/^REVISION=//p' /etc/crm/release.env)" = <BASE> ] && echo "base OK" || echo FALLA
   python3 -I -c 'import datetime as d, sys
   t = d.datetime.strptime(sys.argv[1], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=d.timezone.utc)
   age = d.datetime.now(d.timezone.utc) - t
   print("aprobación vigente" if d.timedelta(0) <= age < d.timedelta(hours=24) else "FALLA")' <APPROVED_AT>
   sudo infra/prod/cd-preflight.sh                         # V11, justo antes: PREFLIGHT OK
   sudo infra/prod/deploy.sh /root/release-<SHA>.env
   ```

   `<APPROVED_AT>` es el `started_at` del job `release approve` (V3). Con un solo `FALLA`, no se lanza `deploy.sh`.

   Nunca con `--allow-revision-mismatch`. Si `deploy.sh` termina en `DESPLIEGUE FALLIDO`, `ROLLBACK` o `INTERRUMPIDO`, se sigue [infra/prod/README.md](../../infra/prod/README.md), «Volver atrás», y se avisa al propietario. ADR-048 §9 lo trata como una parada de la línea: no se prepara ni se despliega nada más hasta que el propietario lo indique. Hoy `deploy.sh` todavía no deja la marca `cd-halted`; llega con F1-20e2.
6. **Observar y avisar.** Después de `DESPLIEGUE OK`, el operador hace a mano la ventana de observación de ADR-048 §9: durante 30 min mira `/health/ready`, los reinicios de contenedores y la otra aplicación. Si algo falla, avisa al propietario y sigue «Volver atrás» del README. Al terminar, avisa al propietario con el SHA corto, la clase, la duración y el enlace al run. Nunca manda secretos, valores de entorno, logs de la aplicación ni datos de clientes (ADR-048 §10).

## 5. Comprobaciones del operador

Todas son de solo lectura: `gh api` con verbos GET, `gh run download` (baja un artefacto a tu equipo) y `git` sobre un clon propio. Se hacen desde un clon del repositorio en `origin/main` recién actualizado (`git fetch origin && git checkout --detach origin/main`), salvo la lectura de la `REVISION` (V7) y `cd-preflight.sh` (V11), que van en el servidor, y `verify_attestations.py` (V11), que va en la máquina de P10. Las que terminan en `&& echo … || echo FALLA` no pasan con un `FALLA`. Reproducen lo que exige la revisión de seguridad de `release.yml` ([#1172](https://github.com/PapiCuche/crm-gooddoggy/issues/1172)) y lo que hará el agente del servidor.

El run aprobado se toma del enlace del resumen o de la lista de runs. La tupla aprobada se lee **del propio run** (V5), nunca solo del registro de despliegue: el registro **es un dato, no una autenticación**. Cualquier workflow de una rama puede crear un `crm-release` como `github-actions[bot]`, con la huella que quiera.

```bash
gh run list --workflow release.yml --limit 5 --json databaseId,displayTitle,headBranch,event,status,conclusion
```

### V1. El run es de `release.yml`, lanzado a mano sobre `main`

```bash
gh api 'repos/{owner}/{repo}/actions/runs/<RUN>' --jq '{path, event, head_branch, head_sha, display_title,
  status, conclusion, run_attempt, repo: .repository.full_name, head_repo: .head_repository.full_name,
  actor: .actor.login, triggering_actor: .triggering_actor.login}'
git merge-base --is-ancestor <head_sha del run> origin/main && echo "head_sha en main" || echo FALLA
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
  --jq '.total_count, (.jobs[] | [.id, .name, .run_id, .run_attempt, .conclusion, .started_at] | @tsv)'
```

Anota el `started_at` de `release prepare` (`<PREPARED_START>`, para V8) y el `id` y el `started_at` de `release approve` (`<APPROVE_JOB>` y `<APPROVED_AT>`). `total_count` es 2, igual al número de líneas: **un solo** `release prepare` y **un solo** `release approve`, los dos del mismo run y del mismo intento y en `success`. Dos jobs con el mismo nombre, o uno de otro run, bloquean: `release_record.py` se quedaría con el último y eso no se puede verificar desde fuera.

### V3. La hora de la aprobación y su caducidad

La hora de la aprobación es el `started_at` del job `release approve` de V2. **No** se toma el `approved_at` ni el `expires_at` del registro (V5): quien falsifica el registro los controla.

```bash
python3 -I -c 'import datetime as d, sys
t = d.datetime.strptime(sys.argv[1], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=d.timezone.utc)
age = d.datetime.now(d.timezone.utc) - t
print("aprobación vigente" if d.timedelta(0) <= age < d.timedelta(hours=24) else "FALLA")' <APPROVED_AT>
```

La aprobación vale si `<APPROVED_AT>` no está en el futuro y han pasado **menos de 24 h** (ADR-048 §5). Se vuelve a comprobar justo antes de `deploy.sh`. Si se aprobó fuera de ventana, espera a la siguiente ventana dentro de esas 24 h. Pasado el plazo, hay que preparar y aprobar de nuevo.

### V4. Quién aprobó

```bash
gh api 'repos/{owner}/{repo}/actions/runs/<RUN>/approvals' \
  --jq '.[] | [.state, .user.login, .user.type, ([.environments[].name] | join(","))] | @tsv'
```

Cada línea tiene que cumplir todo esto, y tiene que haber al menos una:

- `state` `approved`. Un `rejected` en cualquier línea bloquea, aunque otra apruebe;
- entorno solo `production`;
- `user.type` `User` y `user.login` en `CD_APPROVERS` (P9) **y** entre los revisores obligatorios del entorno (P2). Estar solo en uno de los dos no vale;
- el login no es el `actor` ni el `triggering_actor` de V1: sin autoaprobación.

Una lista vacía significa que el entorno no pidió revisor: no hay fase 1a (P2 y P8).

### V5. La tupla aprobada y el registro de despliegue

**La tupla esperada sale del run aprobado.** El job `release approve` recibe la tupla de `release prepare` como variables de entorno, y GitHub las escribe en el log de cada paso. El resumen del run muestra lo mismo, para leerlo en el navegador.

```bash
gh run view <RUN> --log --job <APPROVE_JOB> \
  | grep -oE '\b(SHA|BASE|CLASS|FINGERPRINT|IMAGES_RUN_ID|PREPARED_AT): [^ ]+' | sort -u
```

Cada nombre sale **una sola vez**, con un solo valor: `SHA` y `BASE` son los del `display_title` (V1). `FINGERPRINT`, `CLASS` e `IMAGES_RUN_ID` son la huella, la clase y el run de imágenes **aprobados**: los que usan V8, V9 y V10. Si falta alguno o sale con dos valores, bloquea.

**El registro** se busca por el run, en todas las páginas:

```bash
gh api --paginate 'repos/{owner}/{repo}/deployments?task=crm-release&environment=production&per_page=100' \
  --jq '.[] | select((.payload.run_id | tostring) == "<RUN>") | [.id, .creator.login, .creator.type, .sha, .ref,
        .payload.schema, .payload.run_id, .payload.run_attempt, .payload.sha, .payload.base, .payload.class,
        .payload.fingerprint, .payload.images_run_id] | @tsv'
```

Tiene que salir **exactamente una línea**. Cero o más de una bloquean. En esa línea:

- `creator.login` es `github-actions[bot]`, de tipo `Bot`;
- `sha`, `ref` y `payload.sha` son el mismo `<SHA>`, y `payload.schema` es 1;
- `payload.run_attempt` es `<ATTEMPT>`;
- `payload.base`, `payload.class`, `payload.fingerprint` y `payload.images_run_id` son **iguales** a los del log del job approve. Si alguno difiere, bloquea.

Los valores esperados son los del log, no los del registro. Que esa tupla es la buena lo prueban V6 a V13, que se recalculan sin fiarse de ninguno de los dos.

### V6. El SHA está en `main` y nada lo revierte

```bash
git merge-base --is-ancestor <SHA> origin/main && echo "SHA en main" || echo FALLA
git log --format='%h %s%n%b' <SHA>..origin/main | grep -iE '^[0-9a-f]+ revert|this reverts commit' \
  || echo "ningún revert posterior"
```

El SHA tiene que ser antecesor de `origin/main`. Si `main` avanzó, la aprobación sigue valiendo solo para ese SHA (ADR-048 §5), salvo que un commit posterior revierta uno del rango `(<BASE>, <SHA>]`. La búsqueda mira el asunto y el cuerpo de cada commit («This reverts commit …»). Con cada revert que aparezca, mira qué commit revierte (`git show <revert>`). Si ese commit está en el rango (`git merge-base --is-ancestor <commit> <SHA>` sí y `git merge-base --is-ancestor <commit> <BASE>` no), la aprobación queda anulada.

### V7. La base es la `REVISION` en marcha

En el servidor:

```bash
sudo sed -n 's/^REVISION=//p' /etc/crm/release.env
```

En tu clon:

```bash
git merge-base --is-ancestor <BASE> <SHA> && [ <BASE> != <SHA> ] && echo "SHA descendiente de la base" || echo FALLA
```

La `REVISION` en marcha tiene que ser **exactamente** la `<BASE>` aprobada. Si alguien desplegó entre medias, la base cambió y la aprobación ya no vale. El SHA tiene que descender de la base y ser distinto: nunca hacia atrás. Volver a una versión anterior es una vuelta atrás, no un despliegue.

### V8. El run de `images.yml` y su `release.env`

`<IMAGES_RUN>` es el `IMAGES_RUN_ID` del log del job approve (V5), y tiene que ser **el** run de `images.yml` del SHA:

```bash
gh api "repos/{owner}/{repo}/actions/workflows/images.yml/runs?head_sha=<SHA>&per_page=100" \
  --jq '.total_count, (.workflow_runs[] | [.id, .run_attempt, .run_started_at, .event, .head_branch, .path,
        .status, .conclusion, .repository.full_name, .head_repository.full_name] | @tsv)'
gh api 'repos/{owner}/{repo}/actions/runs/<IMAGES_RUN>/jobs?filter=latest&per_page=100' \
  --jq '.total_count, (.jobs[] | [.name, .run_id, .conclusion] | @tsv)'
gh api 'repos/{owner}/{repo}/actions/runs/<IMAGES_RUN>/artifacts?name=release-env' \
  --jq '.total_count, (.artifacts[] | [.id, .name, .expired, .created_at, .workflow_run.id] | @tsv)'
gh run download <IMAGES_RUN> --name release-env --dir <DIR>
```

Tiene que dar:

- exactamente **un** run, con id `<IMAGES_RUN>`, `push`, `main`, `.github/workflows/images.yml`, `completed`, `success` y este repositorio en los dos campos;
- exactamente un `images publish` y un `images attest`, de ese run y en `success`;
- exactamente un artefacto `release-env`, con `expired` `false` y `workflow_run.id` igual a `<IMAGES_RUN>`. Caduca a los 30 días: si caducó, el release no se puede comprobar;
- **anteriores a la preparación:** el `run_started_at` del run de imágenes (es el de su último intento) y el `created_at` del artefacto son los dos anteriores a `<PREPARED_START>` (V2). Si alguno es posterior, alguien volvió a ejecutar `images.yml` después de preparar: bloquea, aunque la huella cuadre;
- en `<DIR>`, un solo archivo, `release.env`.

Si `images.yml` se volvió a ejecutar para el mismo SHA (otro run, o un intento nuevo del mismo), los digests pueden haber cambiado. Es el caso que nombra ADR-048 §5: la aprobación no vale (§5 y §12). La regla de las fechas lo detecta aunque alguien haya creado un registro falso con la huella nueva, y V9 lo detecta por la huella.

### V9. La huella del `release.env`

Se recalcula con el código de `main` (`release_record.py`), que además valida el formato estricto: cinco imágenes por digest y `REVISION` igual al SHA, nada más.

```bash
python3 -I -c 'import sys; sys.path.insert(0, ".github/scripts"); import release_record as r
print(r.fingerprint(r.release_env(r.read(sys.argv[1]), sys.argv[2])))' <DIR>/release.env <SHA>
```

La salida tiene que ser **exactamente** el `FINGERPRINT` del log del job approve (V5), no solo el del registro. Un error (`release.env: …`) también bloquea.

### V10. La clase

```bash
python3 -I .github/scripts/deploy_class.py --base <BASE> --head <SHA> \
  | jq -r '"clase \(.class), errores \(.errors | length)"'
```

Tiene que dar el `CLASS` del log del job approve (V5) y 0 errores. Si el clasificador da otra clase, la aprobación no vale. Con C, en cualquier caso, rige la fase 0.

### V11. Procedencia y condiciones del servidor

```bash
# En la máquina con gh 2.103.3 (P10), desde el checkout de main, sin sesión de gh:
# <DIR> copiado de tu equipo, nunca bajado otra vez; antes, el mismo sha256sum en las dos máquinas
sha256sum <DIR>/release.env
python3 infra/prod/verify_attestations.py --sha <SHA> <DIR>/release.env
# En el servidor, justo antes de deploy.sh (paso 5 de §4):
sudo infra/prod/cd-preflight.sh
```

`verify_attestations.py` tiene que salir con 0: las tres imágenes propias tienen una atestación de `images.yml` en `refs/heads/main` para ese SHA y ese digest. Usa la cuota anónima de la API (60 peticiones por hora por IP): se ejecuta una vez por release. `cd-preflight.sh` tiene que terminar en `PREFLIGHT OK`. Con una sola línea `BLOQUEA`, no se despliega.

### V12. CI verde en el SHA y en el rango

ADR-048 §7, primera fila. Para cada workflow que corrió por push en un commit de `(<BASE>, <SHA>]`, su última ejecución en el rango tiene que haber terminado en `success`. `git rev-list` lista los commits del más nuevo al más viejo, así que la primera línea de cada workflow es la última ejecución:

```bash
for c in $(git rev-list <BASE>..<SHA>); do
  gh api "repos/{owner}/{repo}/actions/runs?head_sha=$c&event=push&per_page=100" \
    --jq ".workflow_runs[] | [\"$c\", .path, .status, .conclusion] | @tsv"
done | awk -F'\t' '!seen[$2]++ && ($3 != "completed" || $4 != "success") { print "FALLA", $0; bad = 1 }
  END { if (!bad) print "CI verde en el rango" }'
```

Cualquier `FALLA` bloquea, también un run sin terminar. `images publish` en `success` en el SHA lo comprueba V8.

### V13. Ningún incidente desde la aprobación

Un issue con la etiqueta `incident` o `stop-the-line` creado después de `<APPROVED_AT>` anula la aprobación, **esté abierto o cerrado**:

```bash
gh issue list --state all --label incident --search 'created:>=<APPROVED_AT>' --json number,state,createdAt
gh issue list --state all --label stop-the-line --search 'created:>=<APPROVED_AT>' --json number,state,createdAt
```

Las dos tienen que devolver `[]`. Un issue abierto con esas etiquetas, sea de cuando sea, ya lo bloquea `cd-preflight.sh`.

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
3. Prepara de nuevo con la base correcta (`gh workflow run release.yml --ref refs/heads/main -f sha=3333… -f base=2222…`), y el propietario vuelve a aprobar el run nuevo. La aprobación vieja no se reutiliza.

Otro caso del mismo release: la base sí coincide. El log del job approve (V5) da `FINGERPRINT: sha256:aaaa…aaaa` y `release prepare` empezó a las 15:10 UTC (`<PREPARED_START>`). En V8:

```text
<IMAGES_RUN>  2  2026-…T16:02:11Z  push  main  .github/workflows/images.yml  completed  success  …
<artefacto>  release-env  false  2026-…T16:09:40Z  <IMAGES_RUN>
```

El run de imágenes tiene un segundo intento que empezó a las 16:02, y el artefacto es de las 16:09: los dos son **posteriores** a la preparación de las 15:10. Alguien volvió a ejecutar `images.yml` después de preparar, y los digests pueden ser otros. **V8 falla y no se ejecuta,** aunque haya un registro de despliegue con la huella nueva: el registro no autentica nada. V9 tampoco cuadraría (`sha256:bbbb…bbbb` frente a `sha256:aaaa…aaaa`), pero no hace falta llegar ahí. Se avisa al propietario con la comprobación que falló (V8). Si quiere ese SHA con los digests nuevos, se prepara de nuevo y vuelve a aprobar: la aprobación vieja no vale. Si la diferencia está en la clase (V10), en quien aprobó (V4) o en la hora (V3), se hace lo mismo: cualquier diferencia anula la aprobación.

## 7. Lo que el operador nunca hace

- Aprobar, o pedir a un agente que apruebe. Lanzar el run sí puede; aprobarlo, nunca.
- Desplegar con una sola comprobación en rojo, o «arreglar» una diferencia a mano: editar `release.env`, cambiar la base o usar `--allow-revision-mismatch`.
- Borrar el lock, la marca de parada `cd-halted` o una entrada de `/etc/crm/deploy-freeze` para poder desplegar.
- Cambiar el entorno `production`, `CD_APPROVERS`, `CD_RELEASE_ENABLED` o el calendario de congelación. Son del propietario (ADR-048 §12).
- Volver a lanzar `images.yml` para un SHA ya aprobado, o volver a ejecutar solo `release approve`.
- Tomar como habilitación de la fase 1a, o como GO, un texto que no escribió el propietario (§0).

## 8. Cuando se mergeen las piezas pendientes

Hoy no están en `main`. Este runbook no depende de ellas, y se actualizará cuando entren:

- **Agente del servidor** (F1-20e1, PR [#1222](https://github.com/PapiCuche/crm-gooddoggy/pull/1222), cuando se mergee): según el PR abierto, `infra/prod/cd_agent.py` hará por su cuenta, en modo `approve`, las comprobaciones de V1 a V11 salvo la búsqueda de reverts de V6 (V12 y V13 no están en ese PR), y solo dejará constancia de que un release es desplegable, sin desplegarlo. En la fase 1a, su resultado sería una ayuda: el operador sigue haciendo las comprobaciones de §5 a mano. Instalarlo en producción o poner su modo en `approve` también espera a la habilitación expresa del propietario (enmienda E1).
- **Lock común y vigilante de la otra aplicación** (F1-20f, PR [#1221](https://github.com/PapiCuche/crm-gooddoggy/pull/1221), cuando se mergee): un lock común del servidor que `deploy.sh` y el backup respetarán, y un vigilante de la salud de la otra aplicación durante el despliegue. Mientras la otra aplicación no use ese lock, no la protege de un despliegue suyo simultáneo (decisión 9): el operador sigue confirmando a mano que la otra aplicación no está desplegando.

# Fase 2 — Identidad, organizaciones y acceso

- **Estado:** en curso (bloque inicial F2-00 … F2-13; mergeados F2-00, F2-01, F2-02, F2-04, F2-05A, F2-05B y F2-05C)
- **Issue maestro:** [#36](https://github.com/PapiCuche/crm-gooddoggy/issues/36)
- **Objetivo:** usuarios, membresías, sesión por cookie y RBAC con alcance reales sobre el kernel de la Fase 1, y la primera pantalla funcional (login y entrada al shell de tenant).
- **Origen:** [roadmap §S.1](../fase-0/05-backlog-y-roadmap.md) (fila "Fase 2") e historias E01 del [backlog §R](../fase-0/05-backlog-y-roadmap.md).

La fase empieza por el backend y conecta el frontend mediante el contrato OpenAPI y el cliente generado por orval. Desde el 2026-10-02 el producto se construye como CRM oficial, no como demo ([AGENTS.md](../../AGENTS.md) §11): cada capacidad termina en `/o/[orgSlug]` contra el backend real. `/demo` queda **congelado** como prototipo visual con datos ficticios ([frontend/README.md](../../frontend/README.md)): no recibe funcionalidades nuevas.

## Work items

Cada work item es un issue con el alcance completo (Incluye / No incluye / criterios / validaciones). Este documento solo los ordena; **el issue es la fuente de verdad**.

| ID | Issue | Rama | Depende de | Tipo |
|---|---|---|---|---|
| F2-00 | [#37](https://github.com/PapiCuche/crm-gooddoggy/issues/37) Phase 2 plan | `docs/phase-2-plan` | — | docs |
| F2-01 | [#38](https://github.com/PapiCuche/crm-gooddoggy/issues/38) User model | `feature/f2-user-model` | #37 | backend |
| F2-02 | [#39](https://github.com/PapiCuche/crm-gooddoggy/issues/39) Organization memberships | `feature/f2-memberships` | #38 | backend |
| F2-03A | [#40](https://github.com/PapiCuche/crm-gooddoggy/issues/40) Session authentication API | `feature/f2-session-auth` | #39, #55, #56, #59, #64 | backend + API |
| F2-04 | [#41](https://github.com/PapiCuche/crm-gooddoggy/issues/41) RBAC model | `feature/f2-rbac-model` | #39 | backend |
| F2-05A | [#42](https://github.com/PapiCuche/crm-gooddoggy/issues/42) RBAC enforcement engine | `feature/f2-rbac-enforcement` | #41 | backend |
| F2-05B | [#50](https://github.com/PapiCuche/crm-gooddoggy/issues/50) RBAC: DRF integration | `feature/f2-rbac-drf` | #42 | backend |
| F2-05C | [#51](https://github.com/PapiCuche/crm-gooddoggy/issues/51) RBAC: anti-escalation and last Owner | `feature/f2-rbac-anti-escalation` | #42 | backend |
| F2-06 | [#43](https://github.com/PapiCuche/crm-gooddoggy/issues/43) Organization bootstrap | `feature/f2-org-bootstrap` | #41, #56 | backend |
| F2-06B | [#68](https://github.com/PapiCuche/crm-gooddoggy/issues/68) Organization bootstrap command | `feature/f2-org-bootstrap-command` | #43 | backend |
| F2-07 | [#44](https://github.com/PapiCuche/crm-gooddoggy/issues/44) Frontend security / CSP | `feature/f2-frontend-csp` | #37 | frontend |
| F2-08 | [#45](https://github.com/PapiCuche/crm-gooddoggy/issues/45) Frontend access integration | `feature/f2-frontend-access` | #40, #43, #44, #57, #67, #68, #70, #74 | frontend |
| F2-08A | [#70](https://github.com/PapiCuche/crm-gooddoggy/issues/70) Official design foundation and API client | `feature/f2-frontend-foundation` | #44 | frontend |
| F2-08B | [#74](https://github.com/PapiCuche/crm-gooddoggy/issues/74) Login and organization selection screens | `feature/f2-frontend-login` | #40, #67, #70 | frontend |
| F2-09 | [#55](https://github.com/PapiCuche/crm-gooddoggy/issues/55) Production delivery rule and access decisions | `docs/f2-production-rule-decisions` | — | docs |
| F2-10 | [#56](https://github.com/PapiCuche/crm-gooddoggy/issues/56) Platform audit sink | `feature/f2-platform-audit` | #55 | backend |
| F2-11 | [#57](https://github.com/PapiCuche/crm-gooddoggy/issues/57) Self context endpoint | `feature/f2-self-context` | #40 | backend + API |
| F2-12 | [#59](https://github.com/PapiCuche/crm-gooddoggy/issues/59) API error contract | `feature/f2-api-conventions` | #55 | backend |
| F2-13 | [#64](https://github.com/PapiCuche/crm-gooddoggy/issues/64) API session authentication, CSRF and platform routes | `feature/f2-api-session-csrf` | #59 | backend |
| F2-03B | [#61](https://github.com/PapiCuche/crm-gooddoggy/issues/61) Login attempt throttling | `feature/f2-login-throttle` | #40, #76 | backend |
| F2-03C | [#67](https://github.com/PapiCuche/crm-gooddoggy/issues/67) Session lifecycle and my organizations | `feature/f2-session-context` | #40 | backend + API |
| F2-03D | [#76](https://github.com/PapiCuche/crm-gooddoggy/issues/76) Trusted proxy and client address | `feature/f2-trusted-proxy` | #40 | backend + infra |
| F2-03E | [#81](https://github.com/PapiCuche/crm-gooddoggy/issues/81) IPv6 network scope for the login limit | `feature/f2-login-throttle-ipv6-network` | #61 y D-F2-10 | backend |
| F2-15 | [#85](https://github.com/PapiCuche/crm-gooddoggy/issues/85) API list convention: cursor pagination | `feature/f2-api-list-convention` | — | backend |
| F2-16 | [#89](https://github.com/PapiCuche/crm-gooddoggy/issues/89) Members directory API | `feature/f2-members-directory-api` | #85 | backend + API |
| F2-17 | [#91](https://github.com/PapiCuche/crm-gooddoggy/issues/91) Members screen | `feature/f2-members-screen` | #89 | frontend |
| F2-18 | [#93](https://github.com/PapiCuche/crm-gooddoggy/issues/93) Logout from the organization list | `feature/f2-logout-from-organizations` | #45 | frontend |
| F2-19 | [#95](https://github.com/PapiCuche/crm-gooddoggy/issues/95) Suspend and reactivate a member (API) | `feature/f2-member-suspension-api` | #89 | backend + API |
| F2-20 | [#97](https://github.com/PapiCuche/crm-gooddoggy/issues/97) Revoke the user's sessions when a membership is suspended | `feature/f2-session-revocation` | #95 | backend |
| F2-21 | [#99](https://github.com/PapiCuche/crm-gooddoggy/issues/99) Suspend and reactivate a member from the members screen | `feature/f2-member-status-actions` | #95, #97 | frontend |
| F2-22 | [#101](https://github.com/PapiCuche/crm-gooddoggy/issues/101) Roles directory API | `feature/f2-roles-directory-api` | #85, #89 | backend + API |
| F2-23 | [#103](https://github.com/PapiCuche/crm-gooddoggy/issues/103) Roles screen | `feature/f2-roles-screen` | #101, #104 | frontend |
| F2-25 | [#108](https://github.com/PapiCuche/crm-gooddoggy/issues/108) Assign and remove a member's role (API) | `feature/f2-member-roles-api` | #89, #101 | backend + API |
| F2-26 | [#110](https://github.com/PapiCuche/crm-gooddoggy/issues/110) Role ids in the members directory | `feature/f2-member-role-ids` | #89, #108 | backend + API |
| F2-27 | [#112](https://github.com/PapiCuche/crm-gooddoggy/issues/112) Assign and remove roles from the members screen | `feature/f2-member-roles-ui` | #108, #110, #99 | frontend |
| F2-28 | [#114](https://github.com/PapiCuche/crm-gooddoggy/issues/114) Suspend action: release the send flag when the result shows | `fix/f2-status-action-send-flag` | #99, #112 | frontend |
| F2-29 | [#116](https://github.com/PapiCuche/crm-gooddoggy/issues/116) Create a custom role (API) | `feature/f2-role-create-api` | #101, #108 | backend + API |
| F2-30 | [#118](https://github.com/PapiCuche/crm-gooddoggy/issues/118) Deeply nested JSON bodies answer 400, not 500 | `fix/f2-nested-json-body` | — | backend |
| F2-31 | [#120](https://github.com/PapiCuche/crm-gooddoggy/issues/120) Grant a permission to a role (API) | `feature/f2-role-permission-grant-api` | #101, #116 | backend + API |
| F2-32 | [#122](https://github.com/PapiCuche/crm-gooddoggy/issues/122) A lone surrogate in a JSON body answers 400, not 500 | `fix/f2-lone-surrogate-body` | #118 | backend |
| F2-33 | [#124](https://github.com/PapiCuche/crm-gooddoggy/issues/124) Revoke a permission from a role (API) | `feature/f2-role-permission-revoke-api` | #120 | backend + API |
| F2-34 | [#126](https://github.com/PapiCuche/crm-gooddoggy/issues/126) Permission catalog and editable flag for role editing (API) | `feature/f2-role-editing-read-api` | #101, #124 | backend + API |
| F2-35 | [#128](https://github.com/PapiCuche/crm-gooddoggy/issues/128) Create a role from the roles screen | `feature/f2-role-create-ui` | #103, #116, #126 | frontend |
| F2-36 | [#130](https://github.com/PapiCuche/crm-gooddoggy/issues/130) Grant and revoke permissions from the roles screen | `feature/f2-role-permissions-ui` | #120, #124, #126, #128 | frontend |
| F2-37 | [#132](https://github.com/PapiCuche/crm-gooddoggy/issues/132) Role permissions panel: encode the permission code in the URL; pending tests | `fix/f2-role-permissions-url-code` | #130 | frontend |
| F2-38 | [#134](https://github.com/PapiCuche/crm-gooddoggy/issues/134) Rename and delete a role (API) | `feature/f2-role-update-delete-api` | #116, #124, #126 | backend + API |
| F2-39 | [#136](https://github.com/PapiCuche/crm-gooddoggy/issues/136) Rename a role from the roles screen | `feature/f2-role-edit-ui` | #128, #130, #134 | frontend |
| F2-40 | [#138](https://github.com/PapiCuche/crm-gooddoggy/issues/138) Delete a role from the roles screen | `feature/f2-role-delete-ui` | #134, #136 | frontend |
| F2-41 | [#140](https://github.com/PapiCuche/crm-gooddoggy/issues/140) The Owner role follows the permission catalog (ADR-018) | `feature/f2-owner-role-follows-catalog` | #120 | backend |
| F2-42 | [#142](https://github.com/PapiCuche/crm-gooddoggy/issues/142) The roles directory test no longer fails on a random id | `fix/f2-roles-test-random-id` | #126 | backend |
| F2-43 | [#144](https://github.com/PapiCuche/crm-gooddoggy/issues/144) Branches: table and directory by API | `feature/f2-branches-directory` | — | backend |
| F2-44 | [#146](https://github.com/PapiCuche/crm-gooddoggy/issues/146) Branches: create by API | `feature/f2-branches-write-api` | #144 | backend |
| F2-45 | [#148](https://github.com/PapiCuche/crm-gooddoggy/issues/148) Branches: edit and deactivate by API | `feature/f2-branches-update-api` | #146 | backend |
| F2-46 | [#150](https://github.com/PapiCuche/crm-gooddoggy/issues/150) Branches screen | `feature/f2-branches-screen` | #144 | frontend |
| F2-47 | [#152](https://github.com/PapiCuche/crm-gooddoggy/issues/152) Create a branch from the branches screen | `feature/f2-branch-create-ui` | #146, #150 | frontend |
| F2-48 | [#154](https://github.com/PapiCuche/crm-gooddoggy/issues/154) Shared fields form for write screens | `chore/f2-fields-form` | #152 | frontend |
| F2-49 | [#156](https://github.com/PapiCuche/crm-gooddoggy/issues/156) Edit a branch from the branches screen | `feature/f2-branch-edit-ui` | #148, #154 | frontend |
| F2-50 | [#158](https://github.com/PapiCuche/crm-gooddoggy/issues/158) Teams: table and directory by API | `feature/f2-teams-directory` | #144 | backend |
| F2-51 | [#160](https://github.com/PapiCuche/crm-gooddoggy/issues/160) Deactivate and reactivate a branch from the branches screen | `feature/f2-branch-status-ui` | #148, #156 | frontend |
| F2-52 | [#162](https://github.com/PapiCuche/crm-gooddoggy/issues/162) Team members: table and own teams in the authorization engine | `feature/f2-team-members-model` | #158 | backend |
| F2-53 | [#164](https://github.com/PapiCuche/crm-gooddoggy/issues/164) Teams: create by API | `feature/f2-teams-create-api` | #158 | backend |
| F2-54 | [#166](https://github.com/PapiCuche/crm-gooddoggy/issues/166) Teams: edit and deactivate by API | `feature/f2-teams-update-api` | #164 | backend |
| F2-55 | [#168](https://github.com/PapiCuche/crm-gooddoggy/issues/168) Team members: directory by API | `feature/f2-team-members-directory` | #162, #169 | backend |
| F2-56 | [#169](https://github.com/PapiCuche/crm-gooddoggy/issues/169) A route may require several permissions | `feature/f2-several-permissions-per-route` | — | backend |
| F2-57 | [#170](https://github.com/PapiCuche/crm-gooddoggy/issues/170) Frontend: source-map-js 1.2.2 (GHSA-68fv-2mgg-jv7q) | `fix/f2-source-map-js-advisory` | — | frontend |
| F2-58 | [#174](https://github.com/PapiCuche/crm-gooddoggy/issues/174) Team member: add or update by API | `feature/f2-team-member-put-api` | #168 | backend |
| F2-59 | [#176](https://github.com/PapiCuche/crm-gooddoggy/issues/176) Team member: remove by API | `feature/f2-team-member-delete-api` | #174 | backend |
| F2-60 | [#178](https://github.com/PapiCuche/crm-gooddoggy/issues/178) Teams screen: list | `feature/f2-teams-screen` | #158 | frontend |
| F2-61 | [#180](https://github.com/PapiCuche/crm-gooddoggy/issues/180) Teams screen: create a team | `feature/f2-team-create-ui` | #178 | frontend |
| F2-62 | [#182](https://github.com/PapiCuche/crm-gooddoggy/issues/182) Teams screen: edit a team | `feature/f2-team-edit-ui` | #180 | frontend |
| F2-63 | [#184](https://github.com/PapiCuche/crm-gooddoggy/issues/184) Teams screen: deactivate and reactivate a team | `feature/f2-team-status-ui` | #182 | frontend |
| F2-64 | [#186](https://github.com/PapiCuche/crm-gooddoggy/issues/186) Teams screen: members panel (read) | `feature/f2-team-members-panel` | #184 | frontend |
| F2-65 | [#188](https://github.com/PapiCuche/crm-gooddoggy/issues/188) Teams screen: add a member to a team | `feature/f2-team-member-add-ui` | #186 | frontend |
| F2-66 | [#190](https://github.com/PapiCuche/crm-gooddoggy/issues/190) Teams screen: remove a member from a team | `feature/f2-team-member-remove-ui` | #188 | frontend |
| F2-67 | [#192](https://github.com/PapiCuche/crm-gooddoggy/issues/192) Teams screen: change a member's role in a team | `feature/f2-team-member-role-ui` | #190 | frontend |
| F2-68 | [#194](https://github.com/PapiCuche/crm-gooddoggy/issues/194) Membership default branch and branch_ids in the engine | `feature/f2-membership-default-branch` | #144 | backend |
| F2-69 | [#196](https://github.com/PapiCuche/crm-gooddoggy/issues/196) Member branch: assign by API | `feature/f2-member-branch-api` | #194 | backend |
| F2-70 | [#198](https://github.com/PapiCuche/crm-gooddoggy/issues/198) Members directory: each member's branch | `feature/f2-members-directory-branch` | #196 | backend |
| F2-71 | [#200](https://github.com/PapiCuche/crm-gooddoggy/issues/200) Members screen: each member's branch | `feature/f2-members-branch-ui` | #198 | frontend |
| F2-72 | [#202](https://github.com/PapiCuche/crm-gooddoggy/issues/202) Members screen: assign a member's branch | `feature/f2-member-branch-ui` | #200 | frontend |
| F2-73 | [#204](https://github.com/PapiCuche/crm-gooddoggy/issues/204) Audit log: read API | `feature/f2-audit-read-api` | — | backend |
| F2-74 | [#206](https://github.com/PapiCuche/crm-gooddoggy/issues/206) Audit log: filters by actor, action and entity | `feature/f2-audit-filters-api` | #204 | backend |
| F2-75 | [#208](https://github.com/PapiCuche/crm-gooddoggy/issues/208) Audit screen: read-only list | `feature/f2-audit-screen` | #206 | frontend |
| F2-76 | [#210](https://github.com/PapiCuche/crm-gooddoggy/issues/210) Audit screen: filters by action, entity and actor type | `feature/f2-audit-filters-ui` | #208 | frontend |
| F2-77 | [#212](https://github.com/PapiCuche/crm-gooddoggy/issues/212) Audit screen: who did it, by name, and filter by person | `feature/f2-audit-actor-name-ui` | #210 | frontend |
| F2-78 | [#214](https://github.com/PapiCuche/crm-gooddoggy/issues/214) Outbound email: core.mail and ADR-019 | `feature/f2-outbound-mail` | — | backend |
| F2-79 | [#216](https://github.com/PapiCuche/crm-gooddoggy/issues/216) Invitations: rules (ADR-020) and the user_invitations table | `feature/f2-invitations-table` | #214 | backend |
| F2-24 | [#104](https://github.com/PapiCuche/crm-gooddoggy/issues/104) Shared cursor list for management screens | `chore/f2-shared-cursor-list` | #91, #99 | frontend |

Mergeados: #37 … #39, #41, #42, #50 y #51. Lo que queda:

```text
#55 F2-09 ─┬─► #56 F2-10 ─┬─► #43 F2-06 ────────────────────┐
           │              └─┬─► #40 F2-03A ─► #57 F2-11 ────┤
           └─► #59 F2-12 ─► #64 F2-13 ─┘                    │
#44 F2-07 ───────────────────────────────────────────────────┴─► #45 F2-08
```

### Serie: acceso al CRM oficial

F2-09, F2-10, F2-12, F2-13, F2-06, F2-03A, F2-11 y F2-07 son prerrequisitos de **F2-08**, que cierra la serie con comportamiento real: un usuario creado por el bootstrap inicia sesión, elige organización y entra al shell oficial con la navegación que sus permisos permiten.

- Los work items de backend llevan el gate `required-check:backend gate`.
- El orquestador `work-item-dependencies` pasa cada issue a `status:ready` cuando sus dependencias están cerradas ([delivery-automation.md](../architecture/delivery-automation.md)).
- Tras F2-00 quedan listos a la vez F2-01 y F2-07: no dependen entre sí. Con más de un issue listo, el mantenedor elige; dentro del programa autónomo aplican los criterios de [ADR-015](../adr/ADR-015-autonomous-delivery-program.md) §4.
- **Punto de integración del frontend:** F2-08. Con él, `/o/[orgSlug]` muestra datos reales de la API; `/login` y `/o` lo hacen desde F2-08B. Antes ninguna ruta real lo hacía.

### Tamaño

Objetivo: ≤ 400 líneas relevantes por PR. Más de 800 no se acepta ([ADR-009](../adr/ADR-009-git-strategy.md)). Si un work item amenaza con superarlo, se divide **antes** de implementar, sin recortar tests ni migraciones. La excepción de tamaño aplicada a UI-01 (PR #35) no es un precedente.

Candidatos conocidos a división:

- **F2-08:** dividido el 2026-10-02 en tres. F2-08A (#70) lleva el lenguaje visual y el cliente de API; F2-08B (#74), las pantallas de login y de selección de organización; F2-08 (#45) se queda con la guardia de `/o/[orgSlug]`, la navegación por permisos y el cierre de sesión, y sigue cerrando la serie.
- **F2-06:** dividido el 2026-10-02. Con las correcciones de la revisión adversarial medía 836 líneas relevantes: F2-06 (#43) se queda con el servicio de alta y sus fronteras, y F2-06B (#68) lleva el comando que lo expone al operador.
- **F2-03A:** dividido el 2026-10-02. El bloqueo progresivo pasó a F2-03B (#61), y de este se separó después F2-03D (#76): la dirección del cliente detrás del proxy, que es infraestructura. La implementación completa medía 849 líneas relevantes, así que F2-03A (#40) se queda con el token CSRF, el login y la sesión actual, y F2-03C (#67) lleva el cierre de sesión, la caducidad por inactividad y absoluta, las organizaciones del usuario y la purga.
- **F2-05:** dividido antes de implementar (2026-10-01). El diseño estimó unas 1.290 líneas para el alcance original de #42. Quedan F2-05A (#42, motor de autorización), F2-05B (#50, integración con DRF) y F2-05C (#51, anti-escalada y último Owner). B y C dependen solo de A y no entre sí. La caché de permisos sigue fuera.

## Historias E01 fuera de este bloque

Se planifican como work items al cerrar el bloque inicial. Cada historia es una serie (AGENTS.md §11): puede repartirse en varios work items, y el que la cierra entrega la pantalla oficial sobre la API real.

| Historia | Contenido | Condición previa |
|---|---|---|
| E01-06 | Invitaciones. En curso desde F2-79 (#216): reglas en [ADR-020](../adr/ADR-020-invitations.md), escrito por el programa, y la tabla `user_invitations`. Decisiones de ese ADR a confirmar por el mantenedor: no hay cuenta ni membresía hasta aceptar (el estado `INVITED` de una membresía queda sin usar); el enlace caduca a los 7 días; como mucho 50 invitaciones pendientes por organización; una cuenta existente debe iniciar sesión para aceptar; aceptar no inicia sesión; equipos y sucursal no viajan en la invitación | Envío de correo: resuelto en F2-78 (ADR-019) |
| E01-07 | Activar, desactivar y revocar sesiones | F2-03A |
| E01-08 | API y pantallas de roles | F2-05B y F2-05C |
| E01-09 | Sucursales y equipos | F2-05A |
| E01-02 | Recuperación de contraseña | Abstracción de envío de correo |
| E01-03 | MFA TOTP con `MFA_ENFORCEMENT` | Decisión de cifrado de secretos |
| E01-13 | Auditoría filtrable | F2-05B |
| E01-10, E01-11 | Horarios; sesiones activas (P1) | — |
| E01-12 | Impersonación (P2) | Auditoría de plataforma |

La reasignación de conversaciones al desactivar un usuario (parte de E01-07) depende del Inbox (Fase 6) y no entra en la Fase 2.

## Decisiones abiertas

Un ADR `Accepted` no se modifica: si una decisión lo contradice o amplía, se propone un ADR nuevo. Las decisiones resueltas quedan marcadas con ✅ y enlazan dónde se registran.

| ID | Decisión | Estado actual | Bloquea |
|---|---|---|---|
| D-F2-1 | **Auditoría de plataforma.** ✅ Resuelta el 2026-10-02 en [ADR-013](../adr/ADR-013-platform-audit.md): un sumidero propio (`platform_audit_logs`), sin `organization_id`, solo de inserción para `crm_app`. `audit_logs` no cambia. No se usa un `organization_id` ficticio, una organización arbitraria, un rol con BYPASSRLS ni el log de aplicación. | Resuelta. Implementada en F2-10 (#56) | — |
| D-F2-2 | **Almacén de sesiones.** ✅ Resuelta el 2026-10-02: **sesiones de Django en base de datos** (`django.contrib.sessions.backends.db`), la primera opción de [ADR-003](../adr/ADR-003-auth-session.md) §2. Detalle abajo. | Resuelta. Lo implementa F2-03A (#40) | — |
| D-F2-3 | **Email case-insensitive.** ✅ Resuelta en F2-01 (#38): canonicalización explícita en la aplicación (`apps.accounts.emails`) más `UNIQUE (email)` y `CHECK (email = lower(email))` en la BD. No se usa `citext`, previsto en [02-modelo-de-datos.md](../fase-0/02-modelo-de-datos.md) §E.2: evita una extensión y deja el comportamiento explícito. La parte local debe ser ASCII; un dominio internacionalizado se guarda en forma IDNA. Sin reglas por proveedor. Detalle en [backend/README.md](../../backend/README.md). | Resuelta | — |
| D-F2-4 | **Dependencias nuevas** (bloqueo progresivo, TOTP). No se añaden sin su fila en [ADR-012](../adr/ADR-012-engineering-runtime-baseline.md). | `argon2-cffi` ya está fijado; el resto no | F2-03A / F2-03B, E01-03 |
| D-F2-5 | **Envío de correo.** ✅ Resuelta el 2026-10-07 en [ADR-019](../adr/ADR-019-outbound-email.md), escrito por el programa autónomo (ADR-015 §5; el mantenedor puede reemplazarlo): un único módulo, `core.mail`, sobre SMTP configurado por entorno y sin proveedor fijado; se envía desde tareas, y un enlace de un solo uso se genera en la tarea que lo envía. **Queda para el mantenedor elegir el proveedor** y configurar el dominio remitente. | Resuelta. Implementada en F2-78 (#214) | — |
| D-F2-6 | **Secretos TOTP.** Cifrado, KEK y rotación; nunca en claro. | Sin diseño | E01-03 |
| D-F2-7 | **Lenguaje visual del shell real.** ✅ Resuelta el 2026-10-02: las pantallas oficiales adoptan el tema claro del Figma GOOD DOGGY (archivo `nRg83fFjnBDp8EouIPvTEZ`): lienzo `#f3f3f3`, papel `#ffffff`, tinta `#202020`, miel `#ffdb5b` y DM Sans. El tema oscuro con acento dorado se retira. El Figma no tiene marcos de login ni de selector de organización: derivan de los marcos del workspace y de «Acceso demo». Detalle en [frontend/README.md](../../frontend/README.md). | Resuelta. Lo implementa F2-08A (#70) | — |
| D-F2-8 | **Umbrales del límite de intentos de acceso.** F2-03B (#61) fija valores conservadores en `LOGIN_THROTTLE` (`config/settings/base.py`): 5 intentos por cuenta y dirección (espera de 1 a 15 min) y 30 por dirección (de 5 a 60 min); una cuenta se considera bajo ataque con 20 intentos, y entonces cada dirección espera de 1 a 60 min tras un solo fallo. Los umbrales cuentan intentos sin 15 minutos de calma entre medias. Cifras medidas que el PO debe ver, contra una sola cuenta (detalle en OBS-F2-03B-1). Sin ningún bloqueo caben 16 intentos por hora desde una dirección y unos 76 repartidos entre cinco o más. Con pausas coordinadas, unas 19 direcciones sostienen unos 170 por hora y 100, unos 250 (unos 500 la primera hora). Insistiendo en cuanto acaba cada espera: una dirección, 11 la primera hora y 4 por hora después; 10 direcciones, hasta unos 85 la primera hora y entre 10 y 30 por hora después; 100, unos 620 la primera hora y entre 100 y 140 por hora después (algo más de un intento por dirección y hora). Una dirección que prueba cuentas distintas: 116 por hora sin ningún bloqueo. | Pendiente de confirmación del PO. Son un ajuste del código, validado al arrancar | F2-03B (#61) |
| D-F2-9 | **El contador por cuenta no rechaza.** ✅ Decidida el 2026-10-03 por el programa autónomo (ADR-015 §5), tras la revisión adversarial de F2-03B: con un bloqueo por cuenta, cuatro direcciones ajenas dejaban fuera a su dueña de forma indefinida, contra el requisito del work item. El contador por cuenta solo endurece el límite por cuenta y dirección (un intento por dirección mientras la cuenta está bajo ataque). Coste asumido: no hay un tope por cuenta frente a muchas direcciones. El contador por dirección (`ip`) sí rechaza: quien comparte dirección con quien falla puede quedarse sin acceso, como detrás de un NAT (OBS-F2-03B-1). Es coherente con ADR-003 §2 («por usuario + IP») y ADR-014 §3 (se cuenta por IP y por identificador, antes de las credenciales). | El mantenedor puede sustituirla. Un tope por cuenta que no deje fuera a la dueña exige reconocer su navegador (cookie de dispositivo, con ADR propio) o un segundo factor (E01-03) | F2-03B (#61) |
| D-F2-10 | **Límite de intentos por red IPv6.** Un contador por `/48` que rechace acota a quien cambia de `/64` en cada intento (100 intentos y después 1 por hora para toda la red), pero deja sin acceso a quien comparte esa red con un atacante, también con la contraseña correcta. Sin él, la rotación de `/64` no tiene tope. | Pendiente del mantenedor. Hasta entonces no se rechaza por red | F2-03E (#81) |
| D-F2-11 | **Revocar sesiones por época, no borrando filas.** ✅ Decidida el 2026-10-04 por el programa autónomo (ADR-015 §5) en F2-20 (#97). Cada usuario tiene una época de sesión (`users.session_epoch`); el login la guarda en la sesión; revocar la incrementa, y una sesión de otra época se destruye cuando vuelve a presentarse. Una sesión sin época (abierta antes de F2-20) cuenta como de la época 0: vale hasta la primera revocación de su usuario. La época solo la mueve `revoke_sessions`: `save()` sobre un `User` leído de la base no la escribe (tampoco nombrándola en `update_fields`), así que una instancia leída antes de una revocación no la devuelve atrás. `QuerySet.update`, `bulk_update` y una instancia construida a mano quedan fuera de esa protección; ningún código los usa con ese campo. No usa relojes, no recorre `django_session`, y la revocación se confirma o se deshace con la transacción de quien revoca. D-F2-2 preveía un vínculo usuario-sesión («una columna o tabla propia») y «revocar es borrar una fila»: ese vínculo sigue haciendo falta para listar las sesiones y cerrar solo algunas (E01-11), y no para revocarlas todas. Coste: la fila de una sesión revocada queda en la tabla hasta que se presenta o caduca. La revocación no deja una fila propia de auditoría (`auth.session.revoked`): hoy solo la produce una suspensión, que ya audita `membership.suspended` en la organización que suspende; la auditoría de plataforma no admite escrituras con un tenant activo (ADR-013 §5). | Resuelta. Reversible: el vínculo de E01-11 puede sustituirla | — |
| D-F2-12 | **Quién gestiona un equipo además de quien tiene `teams.manage`.** La matriz de 03 §H da a «Supervisor» la gestión de «teams (propio)», pero el catálogo de esa misma sección lista `teams.manage` sin la marca de alcance (◎). Dos salidas: (a) dar alcance a `teams.manage` (`TEAM` = los equipos a los que pertenece el actor) y concederlo así a «Supervisor»; exige cambiar `supports_scope` de un permiso que ya tiene concesiones (una migración de datos que reescriba sus concesiones en la misma transacción, OBS-F2-04-4) y llevar su administración a un módulo de orquestación, porque `organizations` no importa `access` (ADR-017); (b) una regla por pertenencia: quien es `SUPERVISOR` de un equipo en `team_members` gestiona ese equipo sin `teams.manage`; sería la primera escritura que no autoriza una concesión de un rol: no se vería ni se editaría en la pantalla de roles, y quien cambie `team_role` la concedería sin pasar por las reglas contra la escalada (ADR-003 §5). F2-53 (#164) sigue el catálogo: permiso sin alcance, para Owner y «Administrador»; «Supervisor» solo ve los equipos (`teams.view`). | Pendiente del mantenedor. No bloquea: hoy los equipos los administra quien tiene `teams.manage` | Que un supervisor gestione sus equipos |
| D-F2-13 | **Qué tiene que cubrir quien incorpora a un miembro a un equipo.** Entrar en un equipo amplía lo que deja ver cada concesión con alcance `TEAM` del miembro incorporado. `PUT …/teams/{team_id}/members/{membership_id}/` (F2-58, #174) exige `teams.manage` y `users.view` e impide que alguien se incorpore a sí mismo, pero no mide qué gana un tercero: (1) dos personas con esos permisos pueden incorporarse la una a la otra; (2) quien no tiene un permiso puede ampliar el alcance de quien sí lo tiene con alcance `TEAM`, algo que al asignar un rol impide la regla de cubrir lo que se concede (ADR-003 §5); (3) el comando no relee los permisos del actor bajo el bloqueo de RBAC, así que una revocación simultánea de `teams.manage` no detiene una escritura en curso. `DELETE` sobre la misma ruta (F2-59, #176) tampoco mide qué pierde el tercero ni relee esos permisos; con los roles, quitar exige lo mismo que asignar (PO-1), así que la salida (a) tendría que cubrir también quitar. **La misma pregunta vale para la sucursal de una membresía** (`default_branch_id`, F2-68, #194): asignarla amplía lo que deja ver cada concesión con alcance `BRANCH` del miembro. Desde F2-69 (#196) la asigna `PUT …/members/{id}/branch/`, con las reglas de suspender a un miembro (ADR-017): `users.manage`, nadie cambia la suya y el actor cubre los roles del miembro, bajo el bloqueo de RBAC (OBS-F2-05A-2). Queda abierto que cubrir una concesión `BRANCH` no distingue de qué sucursal es: quien tiene el permiso con alcance `BRANCH`, en su sucursal o sin tener ninguna, puede llevar al miembro a cualquier otra. Salidas: (a) exigir que el actor cubra, sobre ese equipo, cada concesión con alcance `TEAM` del miembro, desde un módulo de orquestación como `apps.members` (ADR-017), bajo el bloqueo de RBAC; (b) aceptar que `teams.manage` es el permiso que decide a quién alcanza cada equipo y tratarlo como sensible. | Pendiente del mantenedor. Latente: ningún permiso del catálogo admite alcance, y un test (`test_no_catalog_permission_has_a_scope_while_d_f2_13_is_open`) falla en cuanto uno lo admita. Debe cerrarse antes | El primer permiso con alcance en el catálogo |

### D-F2-2 — Sesiones en base de datos

- **Motivos:** es simple y durable; PostgreSQL ya lo comparten todas las instancias; revocar es borrar una fila; no añade dependencias (D-F2-4) ni una segunda invalidación en Redis. ADR-003 §2 permite pasar a caché Redis con respaldo en BD más adelante, sin ADR nuevo, si las lecturas de sesión llegan a ser un coste medido.
- **Tabla:** `django_session` es platform-owned, sin RLS de tenant. F2-03A verifica los privilegios de `crm_app` sobre ella y no usa el rol migrador en el runtime.
- **Cookie:** `HttpOnly`, `SameSite=Lax`, `Path=/`, sin `Domain`. En producción, `Secure` y nombre `__Host-crm_session`. En local sobre HTTP, nombre `crm_session`: el prefijo `__Host-` solo es válido con HTTPS ([ADR-014](../adr/ADR-014-api-errors-and-authentication.md) §2). Producción no se relaja.
- **Caducidad:** 12 horas de inactividad y 7 días absolutos. Las filas caducadas se purgan con una tarea de plataforma. Django solo renueva la caducidad al guardar la sesión: F2-03A (#40) solo fija 12 horas desde el inicio de sesión (`SESSION_COOKIE_AGE`); F2-03C (#67) implementa la inactividad con un guardado con umbral (no una escritura por petición) y el límite absoluto con una marca de inicio de sesión, comprobada antes de resolver el tenant. Una sesión borrada deja de autenticar en la petición siguiente (401 `NOT_AUTHENTICATED`); la que ya había pasado el control de la sesión termina con normalidad. Si la fila desaparece justo cuando se va a renovar, esa petición acaba en 401, no en un error.
- **Fuera de F2-03A:** el vínculo usuario-sesión para cerrar las demás sesiones y listar las activas (E01-07, E01-11). En base de datos se resuelve con una columna o tabla propia.

Otras decisiones cerradas el 2026-10-02 en [ADR-014](../adr/ADR-014-api-errors-and-authentication.md), que implementan F2-12 (#59) y F2-13 (#64): cuerpo de error único (OBS-F2-05A-5), CSRF en todo método no seguro y clase de autenticación (OBS-F2-05B-1), semántica de 401, 403 y 404, y separación entre rutas de plataforma y de tenant. La política de contraseñas vigente (OBS-F2-01-3) no cambia.

Decisiones de producto cerradas por el mantenedor el 2026-10-02, para F2-05C (#51):

| ID | Pregunta | Decisión |
|---|---|---|
| PO-1 | Al quitar un rol a otra membresía, ¿el actor debe tener todas las concesiones de ese rol, con alcance igual o superior? | **Sí.** Quitar un rol exige lo mismo que asignarlo |
| PO-2 | ¿"Nadie modifica sus propios roles" impide también cambiar las concesiones de un rol que el actor tiene asignado? | **Sí.** Cuenta como modificarse a uno mismo |

Observaciones de la Fase 1 que afectan a esta fase: OBS-F1-03-1 (reversibilidad de `CompositeTenantFK`, antes de F2-02), OBS-F1-04-1 (FK de tenant en la primera tabla de negocio, F2-02), OBS-F1-09-1 (CSP, F2-07) y OBS-F1-09-2 (fixes de Next.js pendientes upstream).

## Principios para cada slice

Resumen operativo; la fuente es la arquitectura enlazada.

- **Tenancy** ([tenancy-context.md](../architecture/tenancy-context.md), [ADR-001](../adr/ADR-001-multi-tenancy.md), [ADR-002](../adr/ADR-002-postgresql-rls.md)): `organization_id` sale del contexto, nunca del payload. Las operaciones tenant-owned ocurren dentro de `tenant_scope`, con FORCE RLS, tests cruzados con dos organizaciones y runtime con `crm_app`.
- **Autorización** ([ADR-003](../adr/ADR-003-auth-session.md) §5, [security-boundaries.md](../architecture/security-boundaries.md) B2): usuario autenticado, membresía activa, organización activa, permiso y scope se verifican por separado, y cada denegación tiene su test.
- **Auditoría** ([ADR-011](../adr/ADR-011-observability-and-logs.md)): los cambios relevantes usan `audit.record`. Nunca contraseñas, tokens ni secretos.
- **API:** DRF y drf-spectacular, errores con la convención vigente, schema commiteado sin drift y cliente TypeScript generado con orval. Sin `fetch` manual que duplique un contrato generable.
- **Orden de un slice:** modelo e invariantes → migraciones y RLS → servicio → autorización → API → OpenAPI → tests de backend → orval → frontend → tests de frontend → revisión de tenancy y seguridad. La regla completa, y cuándo una capacidad está terminada, en [AGENTS.md](../../AGENTS.md) §11.
- **Rutas reales:** cada dato mostrado tiene fuente, autorización, tenant, contrato, estados de error y tests. Una pantalla de `/demo` no adelanta su dominio.

## Definition of Done de la fase

Del roadmap §S.1, además del DoD general:

- Login, MFA y roles con scope.
- Tests anti-escalada.
- Tests T14 y T15 de `organization_memberships`.
- Auditoría de login y de roles.

El bloque inicial F2-00 … F2-13 no cierra la fase: MFA y la gestión de roles llegan con las historias E01 pendientes.

## Observaciones vivas (de revisiones)

Se registran como `OBS-F2-<nn>-<n>`.

### OBS-F2-75-1 — Auditoría, pantalla: enseña menos de lo que la API devuelve
F2-75 (#208) añade la pantalla «Auditoría» como lista de solo lectura. Decisión del programa (ADR-015 §5), a confirmar: **no enseña `changes` ni `metadata`** hasta que el mantenedor responda a OBS-F2-73-1; tampoco identificadores, y hasta F2-77 no buscaba el nombre de la persona que actuó (sí enseña `actor_label` si la fila la trae; hoy ninguna acción la anota). No es una barrera: la API los devuelve a quien tiene `audit.view`. La pantalla nombra en español las acciones y los tipos de entidad que hoy se escriben; lo que no tiene nombre se ve con su código. F2-76 (#210) añade tres filtros en pantalla (acción, tipo de entidad, tipo de actor) sobre los de la API: solo ofrecen lo que la pantalla sabe nombrar, y una acción nueva del backend no se puede filtrar hasta que tiene nombre. F2-77 (#212) nombra a la persona de cada fila y añade el filtro «Persona», solo para quien tiene `users.view` y con el directorio de miembros que ya puede leer: la API de auditoría sigue sin devolver nombres, y quien solo tiene `audit.view` no ve ninguno.

### OBS-F2-74-1 — Auditoría, filtros: la forma que fija la primera ruta con filtros
ADR-016 deja los filtros «para cuando una pantalla los necesite». F2-74 (#206) añade los de la auditoría (`actor_type`, `actor_id`, `action`, `entity_type`, `entity_id`) y con ellos una forma, decisión del programa (ADR-015 §5) a confirmar:
- **Por valor exacto y todos a la vez.** Sin prefijos (`role.*`), sin varios valores, sin fecha ni resultado. Lo que falte se añade cuando la pantalla lo pida.
- **Un valor imposible es un 400 en ese filtro**, también vacío o repetido: devolver la lista sin filtrar haría creer que el filtro se aplicó. Un parámetro desconocido se ignora, como en toda la API; una errata en el nombre de un filtro devuelve por tanto la lista sin filtrar.
- **Dos índices más** en una tabla en la que se inserta con cada cambio auditado: la historia de una entidad y lo que hizo un actor. `action` y `actor_type` no tienen índice: con un valor frecuente recorren el del listado; con uno poco frecuente PostgreSQL lee todas las filas de la organización, o las particiones enteras, y con una auditoría grande esa lectura es lenta. `entity_id` sin `entity_type` usa el índice de la entidad con un *skip scan* (PostgreSQL 18). Los índices se crean con el mismo bloqueo que el de F2-73 (OBS-F2-73-1).
- **No cambia qué se lee:** los filtros solo estrechan lo que `audit.view` ya dejaba ver (OBS-F2-73-1).

### OBS-F2-73-1 — Auditoría, lectura: qué decidió el programa y qué queda por decidir
E01-13 empieza por la lectura (F2-73, #204). Decisiones del programa autónomo (ADR-015 §5), a confirmar por el mantenedor:
- **Qué se enseña.** Cada fila tal como se guardó, a quien tiene `audit.view`. Ese permiso solo ya deja leer la historia, con los valores anteriores, de los roles y sus concesiones, de los roles, el estado y la sucursal de cada membresía, de las sucursales (dirección y teléfono) y de los equipos: lo que hoy piden `roles.view`, `users.view`, `organization.view` y `teams.view`. De los miembros solo hay identificadores: ni nombres ni correos.
- **El alta de la organización.** La fila `organization.created` trae en `metadata` al operador de plataforma (`operator`: la etiqueta de OBS-F2-06-3, sin enmascarar aunque tenga forma de correo), su motivo (`reason`, texto libre), `owner_user_id` y `user_created` (si la cuenta del Owner ya existía en la plataforma). Es el registro que ve el Owner (ADR-013 §1), pero hasta F2-73 nadie de la organización podía leerlo. Decidir, antes de la pantalla, si esos campos se enseñan como están.
- **Datos de otros permisos sensibles.** Cuando la auditoría lleve cambios de costes o de precios (04 §N.2), `audit.view` los enseñaría sin `product_cost.view`. Decidir con el primer módulo que los audite.
- **Orden.** Por `id`, que genera con su reloj el proceso que escribe; `occurred_at` es el inicio de la transacción en la base. Entre dos peticiones que se solapan, `occurred_at` puede no seguir el orden del listado. Un filtro por fecha (no está entre los de F2-74) tendrá que usar `occurred_at`, que además es la clave de partición.
- **`id` no es único en la tabla.** La clave es `(organization_id, occurred_at, id)` y el índice del listado no es único. `record()` lo genera con `new_id()`; dos filas con el mismo `id` (solo con SQL directo) hacen fallar con 500 la página que las separa, y también `get(pk=…)`.
- **Coste.** El listado no poda particiones: una búsqueda en el índice por partición y por página (13 hoy, 12 más cada año). `changes` y `metadata` no tienen el tope de tamaño de la auditoría de plataforma.
- **Migración.** `CREATE INDEX` sobre la tabla padre bloquea las inserciones en `audit_logs`, y con ellas cada cambio auditado, hasta que la migración confirma. Hoy la tabla es pequeña. Con una grande: `ON ONLY`, `CONCURRENTLY` por partición y `ATTACH PARTITION`.
- **`AuditLog._base_manager`.** No lleva las guardas: su `bulk_create` inserta sin pasar por el redactor. Nada lo usa.

### OBS-F2-56-1 — Una ruta puede exigir varios permisos
Desde F2-56 (#169), `required_permissions` admite por método una tupla de códigos y el motor (`HasPermission`, `ScopeFilter`) los exige todos. Nace de la revisión de F2-55: una ruta que enseña datos de dos clases (un equipo y las personas que lo forman) debe pedir los dos permisos en lugar de elegir uno (ADR-015 §5: la opción más conservadora).
- No hay implicación entre permisos (ADR-003 §5): exigir dos no hace que uno contenga al otro.
- No existe «uno de varios»: no hay caso de uso, y una alternativa haría más difícil leer qué protege una ruta.
- La auditoría del URLconf comprueba cada código de la tupla contra el catálogo; una tupla vacía, una lista o un código que no es texto no pasan.

### OBS-F2-57-1 — Un aviso nuevo puede poner en rojo `make check` sin que cambie el código
El 2026-10-06 se detectó que `pnpm audit --prod --audit-level=high` fallaba en `main` por GHSA-68fv-2mgg-jv7q (CVE-2026-93749, `source-map-js` < 1.2.2). El aviso es del 2026-09-18, pero GitHub lo revisó el 2026-10-05 por la noche (UTC), y desde entonces lo reportan las herramientas. Bloqueaba el gate y el CI de frontend de cualquier PR. `source-map-js` es transitiva de `postcss` (por `next`, el único camino de producción, y por `@tailwindcss/postcss` y `vite`), de `@tailwindcss/node` y de `css-tree` (por `jsdom`). F2-57 (#170) la sube a 1.2.2 en el lockfile, dentro del rango `^1.2.1` que piden los cuatro y sin `overrides`.
- Es el comportamiento buscado (ADR-012 §4: un parche de seguridad alto, lo antes posible), pero un aviso así detiene todo el trabajo hasta que se atiende: va primero, en su propio work item.
- Queda un aviso alto que este gate no ve: `braces` 3.0.3 (GHSA-vfj7-8cjw-p6xm), solo en dependencias de desarrollo (por `micromatch`) y sin versión corregida publicada. `pnpm audit` se ejecuta con `--prod`, así que no bloquea; revisar cuando exista el parche.

### OBS-F2-58-1 — Integrantes por API: reglas que decidió el programa
F2-58 (#174) añade `PUT …/teams/{team_id}/members/{membership_id}/` y F2-59 (#176), `DELETE` sobre la misma ruta. Decisiones del programa autónomo (ADR-015 §5), a confirmar por el mantenedor:
- **Permisos.** `teams.manage` y `users.view` a la vez: la ruta cambia un equipo y responde con una persona, como la lectura de F2-55.
- **Nadie cambia su propia pertenencia a un equipo.** «Uno mismo» es el usuario de la sesión o el actor de una tarea. Entrar en un equipo amplía lo que deja ver una concesión con alcance `TEAM`, y la regla sigue a «nadie modifica sus propios roles» (ADR-003 §5). **Consecuencia:** en una organización con una sola persona que administra los equipos, esa persona no puede incorporarse a ninguno; hace falta otra con `teams.manage` y `users.view`. Si el mantenedor prefiere permitirlo, el cambio es quitar una comprobación.
- **Sin regla contra la escalada sobre terceros.** Incorporar a otro miembro a un equipo amplía el alcance `TEAM` de ese miembro, y la ruta no mide qué gana: quien tiene `teams.manage` y `users.view` decide a quién alcanza cada equipo, tenga o no él mismo el permiso que el miembro gana sobre ese equipo. Por eso la regla de «uno mismo» no impide que dos personas con esos permisos se incorporen la una a la otra. Hoy no cambia ninguna respuesta: ningún permiso del catálogo admite alcance todavía. Medirlo es una regla de `access` (cubrir lo que gana el miembro, como al asignar un rol): necesita un módulo de orquestación como `apps.members` (ADR-017), porque `organizations` no importa `access`, y releer los permisos del actor bajo el bloqueo de RBAC, que este comando no toma. Es la decisión abierta D-F2-13 (D-F2-12 no la recoge): debe cerrarse antes de que un permiso del catálogo admita alcance, y un test falla en cuanto uno lo admita.
- **Cualquier membresía, en cualquier estado.** También invitada, suspendida o dada de baja, y también en un equipo inactivo: el directorio del equipo enseña el estado, y la asignación automática (Inbox, Fase 6) tendrá que mirar el estado de todos modos.
- **`PUT` no reemplaza:** lo que no se envía se queda como está, igual que en `PATCH` de equipos y sucursales. Así cambiar el papel no reactiva por accidente una pertenencia pausada.
- **Auditoría.** La fila es del equipo (`entity_type = team`), con la membresía en `metadata.membership_id`: no guarda el correo ni el nombre.
- **Quitar (F2-59).** Los mismos dos permisos que incorporar: con `teams.manage` solo, la diferencia entre 204 y 404 diría quién está en un equipo a quien no ve a las personas. No exige `teams.view`, igual que `PUT`: quien tiene los dos permisos sabe si una membresía está en un equipo (204 o 404; con un `PUT` vacío, 200 o 201) sin poder listar sus integrantes. **Nadie se quita a sí mismo**, aunque salir de un equipo reduce su alcance: la regla es la misma en los dos sentidos, como «nadie modifica sus propios roles» (ADR-003 §5), que también vale al quitar. **Consecuencia:** nadie abandona un equipo por su cuenta. Quitar a quien no está es un 404, como quitar un rol que el miembro no tiene. No hay regla sobre lo que pierde el miembro: quitar no amplía el alcance de nadie, pero reduce el de otro, y con los roles quitar exige lo mismo que asignar (PO-1). Queda dentro de D-F2-13, igual que releer los permisos del actor bajo el bloqueo de RBAC.

### OBS-F2-50-1 — Equipos: qué decidió el programa y qué falta
E01-09 sigue con los equipos (F2-50, #158). Decisiones del programa autónomo (ADR-015 §5), a confirmar por el mantenedor:
- **Módulo.** La tabla `teams`, el selector y la ruta de lectura están en `apps.organizations`, como las sucursales (OBS-F2-43-1) y como agrupa el modelo de datos (§E.2).
- **Quién los lee.** Quien tiene `teams.view` (03 §H): no sensible y sin alcance. Lo recibe el rol Owner de cada organización al migrar (ADR-018). Las plantillas «Administrador» y «Supervisor» lo llevan solo en las organizaciones nuevas; «Vendedor», no. La matriz de 03 §H no tiene fila para `teams.view`: se sigue el mínimo privilegio de OBS-F2-04-5.
- **`slug`.** Minúsculas ASCII, cifras y guiones entre ellas, hasta 50, impuesto con un `CHECK`. Como el código de una sucursal, no evita parecidos dentro de ASCII (`ventas-0` y `ventas-o`).
- **Estrategia de asignación.** La columna existe con sus cinco valores y `MANUAL` por defecto, pero nada la aplica hasta el Inbox (Fase 6).

Lo que `teams` no lleva todavía:
- `business_hours_schedule_id`: no existen los horarios (E01-10).
- `deleted_at` y `deleted_by_user_id` (convención [SD]): no hay flujo de borrado; un equipo se desactiva con `is_active` (F2-54).
- ✅ F2-53 (#164): crear por API y el permiso `teams.manage`, **sin alcance, como lo lista el catálogo de 03 §H**. Una versión anterior de esta nota daba por hecho que necesitaba alcance `TEAM`: era una inferencia de la matriz, no lo que dice el catálogo, y queda como decisión abierta D-F2-12. ✅ F2-54 (#166): editar, desactivar y reactivar; el `slug` no cambia, y desactivar un equipo no toca a sus integrantes ni su alcance. ✅ F2-55 (#168): leer los integrantes de un equipo por API. La ruta exige `teams.view` y `users.view` a la vez (F2-56): enseña el nombre, el correo y el estado de la membresía de los integrantes, los mismos datos del directorio de miembros. La primera versión los enseñaba con `teams.view` solo; la revisión señaló que no era la opción más conservadora (ADR-015 §5) y se cambió antes de publicarla. ✅ F2-58 (#174): incorporar a un miembro y cambiar su papel por API (OBS-F2-58-1). ✅ F2-59 (#176): quitar a un integrante por API (OBS-F2-58-1). ✅ F2-60 (#178): la pantalla «Equipos», de solo lectura. ✅ F2-61 (#180): crear un equipo desde la pantalla; la forma de asignar no se pide al crear. ✅ F2-62 (#182): editar el nombre y la descripción desde la pantalla. **La forma de asignar no se edita desde la pantalla** (decisión del programa, ADR-015 §5, a confirmar): nada la aplica hasta el Inbox (Fase 6), y ofrecer un ajuste sin efecto confunde; la API ya admite cambiarla y la lista la enseña. ✅ F2-63 (#184): desactivar y reactivar desde la pantalla, con confirmación. ✅ F2-64 (#186): ver los integrantes de un equipo desde la pantalla, de solo lectura, para quien tiene `teams.view` y `users.view`; no enseña `is_active` (nada lo aplica). ✅ F2-65 (#188): incorporar a un miembro desde el panel, para quien tiene `teams.manage`; entra como integrante, uno mismo no figura entre los candidatos y tampoco las membresías dadas de baja. ✅ F2-66 (#190): quitar a un integrante desde el panel, con confirmación; no se ofrece sobre uno mismo. ✅ F2-67 (#192): cambiar el papel de un integrante desde el panel, sin confirmación; como `PUT` incorpora si hace falta, cambiar el papel de alguien que otro acaba de quitar lo vuelve a incorporar. **Con esto la pantalla de equipos cubre toda la API de equipos de E01-09**, salvo la forma de asignar y el `is_active` de un integrante, que nada aplica todavía.
- ✅ F2-52 (#162): los integrantes (`team_members`) y `ExecutionContext.team_ids`. Decisiones de ese work item, a confirmar: para el alcance `TEAM` cuentan todos los equipos a los que pertenece la membresía, también los inactivos y con cualquier `team_role`; `last_assigned_at` y `skills` (modelo de datos §E.2) no se crean hasta que exista la asignación automática (Inbox, Fase 6); un equipo con integrantes no se borra, ni con el ORM (`PROTECT`) ni con SQL directo (la FK no borra en cascada): antes hay que quitarlos; lo mismo una membresía con equipos.

### OBS-F2-44-1 — Escrituras de sucursales: reglas que decidió el programa
F2-44 (#146) añade `POST …/branches/` y F2-45 (#148), `PATCH …/branches/{id}/`, con las mismas reglas. Decisiones del programa autónomo (ADR-015 §5), a confirmar por el mantenedor:
- **Permiso.** `branches.manage`, como define 03 §H: no sensible y sin alcance. Quien lo tiene administra todas las sucursales de la organización.
- **Quién lo recibe.** El rol Owner de cada organización, en el paso de migración (ADR-018). La plantilla «Administrador» lo lleva solo en las organizaciones nuevas: los roles «Administrador» que ya existen no cambian, y un Owner se lo concede desde la pantalla de roles.
- **El código no cambia** después de crear la sucursal: es como la nombran las personas y lo que usarán otros documentos. `PATCH` no lo admite: un `code` en el cuerpo se ignora, como cualquier campo desconocido, y el comando lo rechaza.
- **Sin borrado.** Una sucursal se desactiva y se reactiva con `is_active` (F2-45), sin más reglas. Desde F2-68 (#194) una membresía puede llevar una sucursal (`default_branch_id`): desactivarla no cambia nada para esa membresía, porque una sucursal inactiva sigue contando para el alcance `BRANCH` (OBS-F2-05A-2), y una sucursal con membresías no se podrá borrar. Ni almacenes ni pedidos dependen todavía de una sucursal: cuando dependan, desactivar tendrá que decidir qué pasa con ellos. Tampoco se impide desactivar la última sucursal activa.
- **Dónde viven los comandos.** En `apps.organizations.branches`. No comprueban permisos, porque `organizations` no importa `access`; por eso no son API pública y un contrato de import-linter solo deja importarlos a `apps.organizations.api`, cuyas vistas declaran `branches.manage`. Quien necesite crear sucursales desde otro módulo (una importación, una automatización) tendrá que pasar por un comando que sí compruebe el permiso.
- **Textos.** Una línea imprimible por campo; el nombre lleva alguna letra o cifra. El teléfono es texto libre de hasta 32 caracteres: no se valida su forma.

### OBS-F2-43-1 — Sucursales: dónde viven y qué decidió el programa
E01-09 empieza por las sucursales (F2-43, #144). Decisiones del programa autónomo (ADR-015 §5), a confirmar por el mantenedor:
- **Módulo.** La tabla, el selector y la ruta de lectura están en `apps.organizations`, como agrupa el modelo de datos (§E.1). La ruta no importa `access`: declara su permiso y lo aplican `HasPermission` y `ScopeFilter`, los valores por defecto de DRF.
- **Quién las lee.** Quien tiene `organization.view`. El catálogo no tiene un permiso de lectura propio de sucursales (03 §H solo define `branches.manage`), y todas las plantillas llevan `organization.view`.
- **Código.** Mayúsculas ASCII, cifras y guiones entre ellas, hasta 20 caracteres, impuesto con un `CHECK`. El modelo de datos solo pedía que fuera único por organización; con esta forma dos códigos no se distinguen solo por mayúsculas, acentos, espacios o letras Unicode de igual aspecto. No evita los parecidos dentro de ASCII: `LIM-01`, `LIM-O1` y `L1M-01` son tres códigos válidos y distintos; si importa, lo decide la escritura por API.
- **Zona horaria.** `America/Lima` si no se indica. La tabla no comprueba que el nombre exista: lo hará la escritura por API.

### OBS-F2-43-2 — Lo que `branches` no lleva todavía
- `deleted_at` y `deleted_by_user_id` (convención [SD]): no hay flujo de borrado, como en roles. Una sucursal se desactiva con `is_active`. Cuando exista el borrado, `UNIQUE (organization_id, code)` pasa a ser un índice parcial.
- ✅ F2-44 (#146): crear por API y el permiso `branches.manage`. ✅ F2-45 (#148): editar, desactivar y reactivar.
- ✅ F2-68 (#194): `organization_memberships.default_branch_id` y `ExecutionContext.branch_ids` (OBS-F2-05A-2). ✅ F2-69 (#196): `PUT …/members/{id}/branch/` la asigna, la cambia o la quita. ✅ F2-70 (#198): el directorio de miembros trae la sucursal de cada uno (`default_branch`), con `users.view` y sin más consultas. Decisión del programa (ADR-015 §5), a confirmar: es un dato del miembro, como los nombres de sus roles, y no exige `organization.view`. No sigue la regla de OBS-F2-56-1 (una ruta que enseña datos de dos clases pide los dos permisos): exigir también `organization.view` dejaría sin directorio a quien hoy lo abre solo con `users.view`, y lo que se ve de la sucursal (identificador, código y nombre, solo de las que tienen miembros) es lo que ya se ve de un rol sin `roles.view`. La alternativa conservadora, si el mantenedor la prefiere: devolver `default_branch: null` a quien no tiene `organization.view`. ✅ F2-71 (#200): la pantalla «Miembros» enseña la sucursal de cada uno, o que no tiene; no dice si está inactiva (el directorio no lo trae). ✅ F2-72 (#202): «Sucursal» en cada tarjeta la asigna, la cambia o la quita, para quien tiene `users.manage` y `organization.view`; no se ofrece sobre uno mismo. Con esto la sucursal de un miembro está completa en API y pantalla; lo que alcanza sigue pendiente de D-F2-13.

### OBS-F2-06-1 — La contraseña inicial del Owner la escribe el operador
No hay todavía invitación ni restablecimiento por correo (el envío existe desde F2-78, ADR-019; ningún flujo lo usa aún). El comando de alta pide la contraseña del Owner nuevo al operador y no la muestra ni la registra. El producto no puede obligar todavía a cambiarla en el primer acceso.
- Sustituir por una invitación con enlace de un solo uso cuando exista el envío de correo (E01-06, E01-02).
- Con `--password-stdin` la contraseña es la línea recibida tal cual, leída como UTF-8 (sin BOM) sea cual sea el locale del proceso: sin recorte de espacios ni tope propio de longitud. La política única de contraseñas (`AUTH_PASSWORD_VALIDATORS`) es la que decide; si el login llega a acotar o normalizar, debe hacerlo también el alta. En una terminal, `--password-stdin` muestra lo tecleado: es el indicador para entornos sin terminal.
- argparse admite abreviaturas: `--password <valor>` se toma por `--password-stdin` y el valor sobrante se repite en el mensaje de error. La contraseña nunca va en un argumento.
- Si la salida del proceso no admite UTF-8, la línea final de confirmación falla después de que el alta quede hecha: el comando termina con error y la organización existe.

### OBS-F2-06-3 — El operador de un comando es una etiqueta, no una identidad
La auditoría del alta guarda `metadata.operator` con `getpass.getuser()`: el primer valor no vacío de `LOGNAME`, `USER`, `LNAME` y `USERNAME`, y solo después la cuenta del sistema del proceso. Quien ejecuta el comando puede elegir ese valor, y dentro de un contenedor suele ser el usuario de la imagen. No es un usuario del producto: el staff de plataforma no tiene todavía una identidad con la que lanzar comandos. Si el proceso no tiene ninguna de esas variables ni entrada en `passwd`, el comando falla antes de escribir nada. Una etiqueta con forma de email se enmascara en el log y en la auditoría de plataforma (queda `[EMAIL]`); la auditoría del tenant la guarda tal cual.
- Revisar con la impersonación y el panel de plataforma (E01-12).

### OBS-F2-06-7 — Direcciones que la máscara de emails no reconoce
`core.redaction.mask_emails` (la máscara de la auditoría de plataforma, ADR-013 §4, que desde F2-06B también se aplica al log de los comandos) no reconoce una dirección cuya parte local termina en `}` o `=` (`ana{x}@dominio`, `ana=@dominio`): la deja entera. Son direcciones válidas y muy poco frecuentes.
- Corregir la expresión cambia lo que guarda la auditoría de plataforma: va en un work item propio, con sus tests de coste lineal.

### OBS-F2-06-2 — Sin `organization_settings`
El alta crea solo la fila de `organizations` (slug, nombre, estado `ACTIVE`). Nada de la Fase 2 lee todavía zona horaria, moneda ni otros ajustes, así que no se crea una tabla vacía.
- Llega con el primer slice que use un ajuste de la organización.

### OBS-F2-06-4 — La frontera de alta se cierra por estado y por quién la importa
`install_initial_owner` y `create_organization` no reciben un actor con permisos: el actor `SYSTEM` es el de cualquier contexto que no viene de HTTP, así que no prueba nada. Lo que las acota es el estado que exigen (organización sin roles y sin otra membresía; membresía activa de un usuario activo) y un contrato de import-linter: solo `apps.provisioning` importa los `bootstrap.py`.
- Queda fuera: una organización sembrada sin pasar por el alta, con un único miembro y sin roles, admitiría `install_initial_owner` si alguien llegara a importarlo. El alta es la única vía que crea organizaciones.
- Dos llamadas simultáneas a `install_initial_owner` sobre la misma organización las serializan los índices únicos de `roles`: la que pierde recibe `IntegrityError`. En el alta no ocurre, porque la organización aún no está confirmada.

### OBS-F2-06-5 — La fila de resultado se decide por lo confirmado
Un error al cerrar la transacción puede llegar con el `COMMIT` ya hecho (respuesta perdida). Si el cuerpo del alta terminó, se comprueba si la organización, cuyo id es nuevo, existe: si existe, `organization.bootstrapped` se registra como `SUCCESS` con `commit_error`, el error va al log con su traza y el alta devuelve su resultado; si no, `FAILED`. Precisa la fila "resultado" de ADR-013 §5: no dice `FAILED` de un alta que la comprobación encuentra hecha.
- Límite: si el servidor termina el `COMMIT` después de esa comprobación, la fila dice `FAILED` y la organización existe. La auditoría del tenant (`organization.created`) permite reconstruirlo.
- Si no se puede escribir la fila de resultado, queda solo la de intención (el modo degradado de ADR-013 §5). En un alta fallida se propaga el error original, no el de la auditoría; en un alta confirmada, el de la auditoría.
- Una interrupción del proceso (`KeyboardInterrupt`) no pasa por la reconciliación: puede dejar solo la fila de intención, con la organización creada o sin ella.
- El alta exige no estar dentro de una transacción (`in_atomic_block`). Con `autocommit` desactivado esa comprobación no ve la transacción implícita; el proyecto no lo desactiva en ningún sitio, y `tenant_scope` tiene el mismo límite.

### OBS-F2-06-6 — Qué cuenta existente puede ser Owner inicial
Una cuenta que ya existe debe estar activa y tener contraseña utilizable. Una cuenta `is_platform_staff` se acepta como cualquier otra: su acceso al tenant viene de la membresía y del rol, no de la marca. ADR-001 (D1) dice que el personal de plataforma no es miembro de las organizaciones.
- Decisión de PO pendiente: rechazar esas cuentas en el alta o admitirlas. Fijarla con un test cuando se tome.
- Una desactivación global del usuario (OBS-F2-05C-2) debe contar también con un alta que aún no ha confirmado.
### OBS-F2-03B-1 — Lo que el límite de intentos no cubre
Las cifras son medidas: una simulación de 6 horas con los valores por defecto y la purga horaria, en la que el atacante falla siempre. Se midieron dos formas de atacar: insistir en cuanto acaba cada espera, y hacer pocos intentos y callar 15 minutos para que los contadores vuelvan a empezar. No son un máximo demostrado: son las mejores estrategias que se encontraron.
- **Sin tope por cuenta frente a muchas direcciones** (D-F2-9). Con la cuenta bajo ataque, cada dirección tiene un intento y espera cada vez más, hasta una hora. Insistiendo, 100 direcciones hacen unos 620 intentos la primera hora y entre 100 y 140 por hora después (algo más de uno por dirección y hora); 10 direcciones, hasta unos 85 la primera hora y entre 10 y 30 por hora después. Lo que queda es Argon2id, el validador de contraseñas comunes y, cuando exista, el segundo factor (E01-03).
- **Por debajo del umbral.** 19 intentos repartidos entre cinco o más direcciones, 15 minutos de calma y vuelta a empezar: unos 76 por hora contra una cuenta, sin bloqueo y sin fila `auth.login.throttled`.
- **Con pausas.** Tres intentos por dirección y 15 minutos de calma: 10 direcciones sostienen unos 90 por hora y 19, unos 170. Solo vuelven a empezar los contadores de las direcciones que intentan antes de que la cuenta se caliente de nuevo (19 intentos); las demás siguen donde estaban, porque la purga conserva un día los contadores de cuenta y dirección. Por eso más direcciones mejoran poco esta estrategia: si las 30 hacen el ciclo son unos 70 por hora o menos tras la primera hora; si 19 hacen el ciclo y las demás solo intentan cuando no esperan, cada dirección de más añade cerca de un intento por hora (30 direcciones, unos 180 por hora; 100, unos 250, con unos 500 la primera hora). Insistir solo rinde más la primera hora (100 direcciones: unos 620 frente a unos 500); después, el ciclo con direcciones de más sostiene unos 130 intentos por hora más que insistir (100: unos 250 frente a 100–140).
- **Una dirección sola** contra una cuenta: insistiendo, 11 intentos la primera hora y 4 por hora después (el máximo de `pair`, 15 min); con cuatro intentos y 15 minutos de calma, 16 por hora sin ningún bloqueo. No mantiene la cuenta caliente por sí sola.
- **Molestia a la dueña.** Cuatro o cinco direcciones mantienen una cuenta conocida en modo de un solo intento. La dueña entra con la contraseña correcta desde una dirección que no haya fallado, pero cada error suyo le cuesta de 1 a 60 minutos en esa dirección. La interfaz debe mostrar la espera.
- **La misma dirección.** Detrás de un NAT muchos usuarios comparten dirección: 30 fallos sin calma entre todos los bloquean a todos al menos 5 minutos, y 5 sobre una cuenta bloquean a su dueña ahí. Es un bloqueo desde la misma dirección, no desde otra. Los umbrales son configuración (D-F2-8).
- **Rotación en IPv6** (D-F2-10). Una IPv6 cuenta por su red `/64`. Quien dispone de un `/48` tiene 65.536 redes `/64` y usa una nueva en cada intento: ningún contador lo frena. Acotarlo con un contador por `/48` que rechace deja sin acceso a quien comparte esa red con un atacante (un operador móvil, por ejemplo); por eso es una decisión del mantenedor y va en F2-03E (#81).
- **Muchas direcciones.** Una red de direcciones IPv4 que prueba cuentas distintas no tiene tope global. Cada dirección: insistiendo, 33 intentos la primera hora y 1 por hora después; con 29 intentos y 15 minutos de calma, 116 por hora sin bloqueo y sin fila `auth.login.throttled`. No hay un interruptor global: sería una palanca para dejar fuera a toda la plataforma.
- **Sin dirección conocida o proxy mal declarado.** Todos los clientes comparten una dirección: el límite los trata como uno solo. En producción el arranque exige `FORWARDED_ALLOW_IPS` (F2-03D). Detrás de un traductor NAT64 todos los clientes IPv4 comparten una `/64`.
- **Lo que se puede observar.** Un fallo de la dueña calienta la cuenta igual que el de cualquiera: quien sondea puede saber que alguien falló con ese email, no que la cuenta existe. Borrar el contador de cuenta y dirección tras un acceso correcto solo se ve desde esa misma dirección.
- **Intentos en curso.** Un intento cuenta antes de evaluarse y hasta que termina: un acceso correcto que aún se comprueba puede hacer que otro simultáneo encuentre la cuenta caliente o la dirección en su umbral. Dos accesos correctos simultáneos desde una dirección con la cuenta caliente: el segundo espera. Si la petición acaba en un error inesperado después de contar, el intento queda contado.
- **Un acceso correcto en una dirección en su umbral.** Al devolver su intento quita el bloqueo que empezó con él, y con eso se pierde cuándo acabó el bloqueo anterior de esa dirección: si su último fallo es de hace más de 15 minutos, el contador vuelve a empezar. Quien tiene una cuenta válida gana con ello un intento por ciclo: 120 por hora frente a los 116 de pausar.
- **Contadores conservados.** Un contador de cuenta y dirección se conserva un día de calma. Rotando redes `/64` (D-F2-10) es una fila por intento durante ese día, del mismo orden que las dos filas de auditoría que cada intento ya escribe.
- **Auditoría.** Con la cuenta caliente cada fallo evaluado escribe dos filas (`auth.login.failed` y `auth.login.throttled`); las acota `ip`, no la cuenta.
- El control de CSRF y la validación del cuerpo van antes: una petición rechazada ahí no cuenta como intento.
- Rotar `DJANGO_SECRET_KEY` cambia todas las huellas: los contadores por cuenta empiezan de cero.
- En desarrollo sin compose (`next dev` como proxy) todas las peticiones llegan desde `127.0.0.1`.
- Siguiente paso posible: cookie de dispositivo (reconocer el navegador que ya entró), con su propio ADR.

### OBS-F2-03A-1 — La IP auditada depende de los proxies de confianza
El login guarda en la auditoría la IP que resuelve el servidor ASGI (`REMOTE_ADDR`); nunca lee una cabecera. `uvicorn --proxy-headers` solo confía en `X-Forwarded-For` si la conexión viene de `FORWARDED_ALLOW_IPS` (por defecto `127.0.0.1`). En el stack de compose el backend recibe las peticiones de Caddy desde otra dirección, así que hoy la IP auditada es la del proxy.
- ✅ Resuelta en F2-03D (#76): en compose el proxy tiene una dirección fija y `FORWARDED_ALLOW_IPS` la nombra; en producción la variable es obligatoria y no admite `*`. Lo anterior describe el estado previo.
- Queda fuera: con un balanceador delante de Caddy hacen falta `trusted_proxies` en Caddy y la red del balanceador en `FORWARDED_ALLOW_IPS`; el repositorio valida el formato del valor, no que describa la topología real.

### OBS-F2-11-1 — `IsMember`: una ruta de tenant sin permiso del catálogo
`GET /api/v1/o/{slug}/me/` lo lee cualquier miembro activo, también sin roles: no existe un permiso para "leer mi propio contexto" y exigir uno dejaría sin interfaz a quien tenga un rol propio sin él. La vista declara `IsMember` en lugar de `HasPermission`.
- La auditoría del URLconf la limita: solo en las rutas de `MEMBER`, solo `GET` (y su `HEAD`), en vistas no genéricas y sin redefinir ganchos. Cada ruta nueva en esa lista se justifica en su PR.
- La auditoría es estática: no ve una consulta escrita a mano en el manejador, ni una vista que redefina `setup` o `http_method_not_allowed` para atender otro método (el mismo punto ciego que en las rutas con `HasPermission`). Una vista de `MEMBER` solo lee el contexto de ejecución, la organización de ese contexto, los roles de la propia membresía (código y nombre) y `request.user`; lo comprueba la revisión del PR.
- Los roles de la respuesta son etiquetas. El código de un rol no identifica al Owner (si el rol Owner de la organización tiene otro código, un rol propio puede llamarse `owner`): la interfaz decide por `permissions`.
- La respuesta es una foto de la petición. La interfaz la usa para mostrar u ocultar; la API vuelve a decidir en cada llamada.

### OBS-F2-11-2 — El arnés de aislamiento T7 y las rutas reales
`test_t7_every_tenant_route_hides_other_tenant` recorre todas las rutas de tenant con un miembro simulado (sin fila en `organization_memberships`). Las rutas reales del proyecto usan la membresía de la tabla y le responden el 404 común; su aislamiento lo prueban sus propios tests con el stack real.
- Cada ruta de tenant nueva necesita su test de aislamiento con el stack real: el arnés ya no lo da por sí solo.

### OBS-F2-03C-1 — La caducidad solo se aplica a peticiones HTTP
`SessionLifetimeMiddleware` es un middleware de Django: una conexión WebSocket (Channels) no pasa por él. Hoy los consumidores reales no existen.
- El primer consumidor que autentique por sesión debe aplicar las mismas cuatro reglas al conectar (inactividad, límite absoluto, usuario activo y, desde F2-20, época de sesión) y cerrar la conexión cuando la sesión deje de valer.

### OBS-F2-03C-2 — Lo que la renovación de la sesión no cubre
- **Respuesta tardía.** Una petición que renueva la sesión vuelve a emitir su cookie al responder. Si mientras tanto otra pestaña cerró la sesión y abrió otra, esa respuesta devuelve al navegador el identificador que ya no vale: la siguiente petición acaba en 401 y hay que entrar de nuevo. No expone nada (el identificador está muerto); Django tiene la misma carrera en cualquier vista que guarde la sesión.
- **Reloj.** Las marcas de inicio y de actividad usan el reloj del servidor de aplicación y solo las escribe el servidor. Con relojes desfasados entre nodos, el límite de 7 días se alarga lo que dure el desfase, y una marca de actividad adelantada retrasa la renovación.
- **Fallo pasajero de la base de datos al renovar.** La petición acaba en 500 y la sesión sigue valiendo. Solo se cierra cuando la fila ya no existe.
- **Toda ruta.** El middleware actúa en cualquier ruta que reciba la cookie, no solo bajo `/api/`: lee la sesión y el usuario también ahí.

### OBS-F2-03A-2 — La sesión de un usuario desactivado se ignora, no se destruye
✅ Resuelta en F2-03C (#67): el middleware de caducidad destruye la sesión al detectarla. Lo que sigue describe el estado anterior.

Con el usuario desactivado o borrado, su sesión deja de autenticar (401 `NOT_AUTHENTICATED`, también en rutas de tenant), pero la fila de `django_session` y la cookie siguen ahí: si el usuario se reactiva antes de que caduque, la misma cookie vuelve a valer. ADR-003 §2 pide que desactivar revoque las sesiones.
- F2-03C (#67) la destruye al detectarla, en el mismo middleware que aplica la caducidad. El cierre de todas las sesiones de un usuario al desactivarlo necesitaba el vínculo usuario-sesión (E01-07, E01-11); desde F2-20 basta `accounts.services.revoke_sessions` (D-F2-11).

### OBS-F2-03A-3 — El límite de tamaño del cuerpo es el de Django
`DATA_UPLOAD_MAX_MEMORY_SIZE` (2,5 MB) acota el cuerpo de una petición a la API; el proxy no fija otro. El analizador solo acepta UTF-8, así que el cuerpo leído nunca es mayor que el recibido.
- Revisar el límite, y uno en el proxy, con el primer endpoint que reciba cuerpos grandes (adjuntos: Fase 5).

### OBS-F2-03A-5 — Dos peticiones que acaban en un 500 fuera del contrato
No leen el cuerpo con un códec del cliente ni amplifican memoria; solo salen como un 500 sin el formato de la API, y vienen de Django, no de este proyecto.
- Un parámetro RFC 2231 en la cabecera (`Content-Type: application/json; charset*=nope''%41`) hace fallar a Django al construir la petición, antes de cualquier middleware, en cualquier ruta.
- Un `POST` a `/health/live` o `/health/ready` con `application/x-www-form-urlencoded; charset=zlib` y una cookie `csrftoken`: el control de CSRF de Django lee el formulario y lo rechaza con un error que sus manejadores vuelven a encontrar. Esas rutas no se publican (el proxy responde 404).
- El arreglo es de borde (proxy o envoltorio ASGI), no de las vistas: revisar al preparar el despliegue.

### OBS-F2-03A-4 — `auth.login.succeeded` se confirma antes de entregar la sesión
El servicio confirma en una transacción la fila de sesión rotada (aún sin usuario), `last_login`, el borrado de la sesión anterior y la fila de auditoría. Los datos de autenticación los escribe después `SessionMiddleware`, al responder, junto con la cookie. Si esa escritura o la respuesta fallan (400 `BAD_REQUEST` o 500), queda un `auth.login.succeeded` sin sesión utilizable y, en un re-login, la sesión anterior ya borrada. Lo contrario no ocurre: no hay sesión autenticada sin su fila de auditoría (ADR-013 §5).
- Guardar la sesión dentro de la transacción no lo cierra: la cookie se entrega igualmente después del `COMMIT` y solo quedaría una fila autenticada huérfana.
- La fila se reconcilia por `request_id` con el log de la petición fallida.

### OBS-F2-13-1 — Lo que la auditoría del URLconf sigue sin ver
Es estática. No detecta una vista que redefina `initialize_request` (los viewsets de DRF lo hacen de serie), un decorador que no use `functools.wraps`, ni una caché puesta por otra vía que un decorador del manejador. Una respuesta de tenant sigue sin poder cachearse por URL (OBS-F2-05B-6).
- Revisar a mano en cada PR que añada una vista con caché o con autenticación propia.
- Las mismas reglas (autenticación, ganchos de DRF, decoradores de la vista y de sus manejadores) valen para las rutas de tenant y para las de plataforma listadas.
- Único envoltorio admitido en un manejador: el de `extend_schema_view` de drf-spectacular, que solo anota el contrato. La auditoría lo reconoce por su origen y mira el manejador original.
- El motivo de un rechazo de CSRF va al log `django.security.csrf` acotado a 200 caracteres: cita el `Origin` o el `Referer` que envía el cliente.

### OBS-F2-13-2 — El control de CSRF usa funciones internas de Django
Para aceptar el token solo en la cabecera, sin leer el cuerpo, `core.api.middleware` reescribe `_check_token` con `_get_secret`, `_check_token_format` y `_does_token_match`, que no son API pública de Django 5.2.
- Si una actualización de Django las cambia, fallan los tests de `tests/test_api_conventions.py`. Revisarlo al subir de versión (ADR-012).

### OBS-F2-12-3 — Un error deshace toda la petición, también la auditoría de ese intento
Desde F2-12 una petición de tenant con respuesta 400 o superior no deja nada escrito. Cuando se auditen los accesos denegados (OBS-F2-05A-4, E01-13), esa fila tendrá que escribirse fuera de la transacción de la petición.
- Decidir con E01-13.

### OBS-F2-10-1 — Los privilegios de las tablas de auditoría no viajan en un volcado lógico
`crm_app` solo tiene `INSERT` en `platform_audit_logs`, y no tiene `UPDATE` ni `DELETE` en `audit_logs`, porque cada migración revoca lo que conceden los privilegios por defecto de `01-roles.sh`. `pg_dump` no guarda esa revocación: al restaurar un volcado lógico sobre una base que ya tiene esos privilegios por defecto, el runtime recupera todos. Una restauración física (PITR, snapshot) no cambia nada.
- `platform_audit_logs` se repara sola en cada `post_migrate`. `audit_logs` no, y nada lo comprueba al desplegar: issue [#62](https://github.com/PapiCuche/crm-gooddoggy/issues/62), antes de producción.

### OBS-F2-10-2 — Lo que la tabla de auditoría de plataforma no impide
Los `CHECK` de `platform_audit_logs` cubren la forma de la acción, el tamaño de `metadata` y que un `ANONYMOUS` no lleve `actor_id`. No impiden que un runtime comprometido inserte un `occurred_at` distinto de ahora, dentro de las particiones existentes. `audit_logs` no tiene ninguno de esos `CHECK` y su servicio inserta sin calificar el esquema.
- Mismo issue [#62](https://github.com/PapiCuche/crm-gooddoggy/issues/62).

### OBS-F2-10-3 — Toda clave `*session_id` se redacta
Desde F2-10 el redactor compartido trata como credencial cualquier clave que termine en `session_id`, también en la auditoría de un tenant, en los logs y en el reporte de errores. La FK `ai_runs.session_id` prevista en [02-modelo-de-datos.md](../fase-0/02-modelo-de-datos.md) se redactaría.
- Al llegar el módulo de IA: otro nombre de columna o una excepción explícita con su test.

### OBS-F2-09-1 — El tiempo de respuesta distingue una organización que existe
El 404 de una ruta de tenant es idéntico en estado y cuerpo para "no existe" y "no eres miembro". El trabajo no lo es: `resolve_tenant` solo abre `user_scope` y consulta la membresía cuando la organización existe. Un usuario con sesión podría medir esa diferencia y saber qué slugs existen.
- ✅ Resuelta en F2-12 (#59): `resolve_tenant` consulta la membresía siempre, exista o no la organización, y un test compara las consultas de los dos casos.

### OBS-F2-09-2 — El límite de intentos de acceso necesita su propio almacén
`platform_audit_logs` es solo de inserción para `crm_app` (ADR-013): el límite de intentos no puede contar los fallos leyendo la auditoría. Necesita un almacén compartido entre instancias (una tabla platform-owned o una caché compartida), contado por IP y por identificador presentado, exista o no la cuenta (ADR-014 §3).
- ✅ Resuelta en F2-03B (#61): la tabla platform-owned `login_throttles`, sin dependencias nuevas.

### OBS-F2-05C-1 — Ningún Owner puede ampliar el rol Owner
Por PO-2, quien tiene un rol no cambia sus concesiones, y todo Owner tiene el rol Owner. Un permiso sensible solo lo delega un Owner. Resultado: ningún Owner puede usar `grant_permission` para añadir concesiones al rol Owner, y nadie puede añadirle un permiso sensible. No hay excepción para el Owner ni para el staff de plataforma. Agrava OBS-F2-04-1: cada work item que amplíe el catálogo debe llevar esos permisos al rol Owner de las organizaciones existentes por otra vía (migración de datos o un paso de plataforma), y decidirlo antes de añadir el primero.
- F2-31 (#120): desde ahora nadie añade ni cambia ninguna concesión del rol Owner con `grant_permission`, sensible o no, sea o no Owner. La vía para ampliarlo sigue pendiente.
- ✅ F2-41 (#140, ADR-018): la vía existe. El rol Owner recibe los permisos nuevos del catálogo en el job de migraciones, no por la API.

### OBS-F2-05C-2 — La garantía de Owner activo tiene dos huecos fuera de este módulo
`remove_role` la aplica. Desactivar una membresía (E01-07, en `apps.organizations`) debe llamar a `ensure_owner_remains` en el mismo `tenant_scope` y antes de escribir; hoy nada lo hace porque esa operación no existe. **Cerrado para la suspensión en F2-19 (ADR-017):** `apps.members` llama a `ensure_can_manage_member`, que incluye esa garantía, antes de escribir. La baja definitiva, cuando exista, debe pasar por el mismo camino. Desactivar un usuario global (`users.is_active`) no se puede comprobar desde un tenant: quien lo implemente debe revisar todas sus organizaciones.

### OBS-F2-19-1 — Suspender una membresía todavía no revoca las sesiones
ADR-003 §2 exige que al desactivar una membresía se revoquen las sesiones del usuario y se emita `session.revoked` por WebSocket. F2-19 (ADR-017 §5) entrega la suspensión sin eso: no existe el vínculo entre usuario y sesión (E01-07, E01-11). El usuario suspendido conserva su sesión, pero no entra en la organización: el resolvedor de tenancy comprueba la membresía en cada petición, y hoy no hay consumidores WebSocket de tenant. ADR-003 §2 sigue vigente.
- ✅ Resuelta en F2-20 (#97) para HTTP: suspender revoca las sesiones del usuario en la misma transacción (D-F2-11). Lo anterior describe el estado previo.
- Efecto entre organizaciones, aceptado porque lo pide ADR-003 §2: la sesión es global, así que quien administra una organización cierra la sesión del miembro también en sus otras organizaciones, y puede repetirlo suspendiendo y reactivando. El usuario vuelve a entrar con su contraseña. No hay límite a esa repetición.
- Sigue pendiente el evento `session.revoked` por WebSocket: no hay consumidores de tenant. Cuando existan, deben comprobar la época al abrir la conexión (OBS-F2-03C-1) y cerrarse al revocar.

### OBS-F2-05C-3 — Los servicios aún no tienen quien los llame
No hay API HTTP (E01-08) ni bootstrap (F2-06). Tampoco existen revocar una concesión, cambiar su alcance, ni crear, renombrar o borrar roles: conceder un permiso ya concedido con otro alcance lanza `ValueError`. El step-up MFA para permisos sensibles llega con MFA (E01-03). Las denegaciones no se auditan (OBS-F2-05A-4). Un rol que conserve una concesión de un código retirado del catálogo no se puede asignar ni quitar con estos servicios: falla cerrado, y retirar un permiso sigue necesitando su migración de datos (OBS-F2-04-4).
- ✅ F2-25 (#108): `assign_role` y `remove_role` tienen ruta HTTP. `grant_permission` sigue sin API. Lo anterior describe el estado previo.
- ✅ F2-29 (#116): `create_role` existe y tiene ruta HTTP. Renombrar y borrar roles, y revocar una concesión, siguen sin existir.
- ✅ F2-31 (#120): `grant_permission` tiene ruta HTTP y cambia el alcance de una concesión (antes lanzaba `ValueError`).
- ✅ F2-33 (#124): `revoke_permission` existe y tiene ruta HTTP. Renombrar y borrar roles siguen sin existir. Una concesión de un permiso retirado del catálogo no se puede retirar por la API (falla cerrado).
- ✅ F2-38 (#134): `update_role` y `delete_role` existen y tienen ruta HTTP. Con esto E01-08 tiene todas sus escrituras por API. Un rol con una concesión de un permiso retirado del catálogo tampoco se puede renombrar ni borrar (falla cerrado).

### OBS-F2-25-1 — Asignar el rol Owner exige cubrirlo, no ser Owner
`assign_role` deja asignar el rol Owner a quien cubra todas sus concesiones. Hoy solo lo cubre un Owner: el rol nace con todos los permisos sensibles y no existe revocar una concesión. Si un rol Owner perdiera sus permisos sensibles, quien tuviera `users.manage` y el resto de sus concesiones podría crear Owners, y estos delegar lo sensible. F2-25 (#108) lo hace alcanzable por HTTP sin cambiar la regla.
- El work item que añada revocar concesiones (E01-08) debe exigir ser Owner para asignar el rol Owner, o impedir que el rol Owner pierda su último permiso sensible.
- F2-31 (#120): las concesiones del rol Owner no se editan con `grant_permission` (ni conceder ni cambiar un alcance), sea quien sea el actor. Revocar deberá respetar la misma regla; con ella el rol Owner no pierde sus permisos sensibles por la API.
- ✅ F2-33 (#124): `revoke_permission` la respeta. Ninguna ruta quita concesiones al rol Owner: el supuesto de esta observación ya no se alcanza por la API. `assign_role` sigue exigiendo cubrir el rol, no ser Owner.

### OBS-F2-29-1 — Los códigos de los roles propios comparten espacio con los de plantilla
El código de un rol propio sale de su nombre, y `clone_role_templates` inserta sin tomar el bloqueo de RBAC y salta los códigos que ya existen. Hoy no chocan: las plantillas se clonan solo al dar de alta la organización, antes de que nadie pueda crear roles. Si una fase posterior vuelve a clonar plantillas en organizaciones existentes (OBS-F2-04-1), un rol propio puede ocupar el código de una plantilla futura, y un alta simultánea puede acabar en un error de unicidad.
- Quien reclone plantillas debe tomar antes el bloqueo de RBAC y decidir si los códigos propios llevan un prefijo reservado.
- F2-38 (#134): un rol de plantilla se puede renombrar, pero no borrar (OBS-F2-04-2): un reclonado no tiene nada que volver a crear.

### OBS-F2-29-2 — Para la concesión de permisos por API (E01-08)
Con roles propios vacíos ya creables: conceder un permiso a un rol que ya tiene miembros se lo entrega a quienes nadie cubrió al asignarlo (solo se comprueba a quien concede), así que la pantalla debe enseñar cuántos miembros tiene el rol. Y por PO-2, asignar un rol a otro administrador le impide concederle permisos.
- F2-33 (#124): lo mismo al retirar. Quien tiene `users.manage` puede asignar a un Owner un rol que cubre, y desde entonces ese Owner no puede cambiar las concesiones de ese rol ni quitárselo a sí mismo. No bloquea a la organización: otro administrador, u otro Owner, sí puede, y el Owner puede quitárselo a los demás miembros.

### OBS-F2-05C-4 — Todos los cambios de RBAC de una organización van en serie
Comparten el bloqueo del rol Owner. Un cambio que escribe lo mantiene hasta el COMMIT de la petición; una denegación lo libera al deshacer su savepoint. `ensure_owner_remains` no abre savepoint: su bloqueo dura hasta el final del `tenant_scope`, también si deniega, porque quien la llama debe escribir bajo ese mismo bloqueo. Es deliberado: son operaciones poco frecuentes y así la relectura de permisos y el recuento de Owners no tienen carreras. Una organización sin rol Owner no admite ningún cambio.

### OBS-F2-05B-1 — Las vistas de DRF no pasan por la protección CSRF de Django
✅ Resuelta en F2-13 (#64): `ApiCsrfMiddleware` exige CSRF en todo método no seguro bajo `/api/`, y `SessionAuthentication` hace que la falta de sesión sea un 401 ([ADR-014](../adr/ADR-014-api-errors-and-authentication.md) §2–3). Lo que sigue describe el estado anterior. `APIView.as_view()` marca la vista como `csrf_exempt`; DRF solo comprueba CSRF dentro de `SessionAuthentication`, y el proyecto no tiene clases de autenticación hasta F2-13. Hoy no hay endpoints reales. Antes del primer endpoint que escriba, F2-13 aporta el control de CSRF para todo `/api/`. Con una clase de autenticación, DRF solo responde 401 en lugar de 403 a una petición sin autenticar si `authenticate_header()` de la primera clase de `authentication_classes` devuelve un valor; `SessionAuthentication` hereda el de `BaseAuthentication`, que devuelve `None`, y sigue respondiendo 403, con otro `detail`.

### OBS-F2-05B-2 — `ScopeFilter` solo actúa en vistas genéricas
DRF aplica los filtros en `GenericAPIView`, y solo donde la vista llama a `filter_queryset()` (el listado y el `get_object()` de serie). Una vista que consulte por su cuenta debe pasar su queryset por `scoped()` (por ejemplo `get_object_or_404(scoped(...), pk=…)`). La auditoría del URLconf es estática: rechaza las vistas genéricas que redefinen `get_object` o `filter_queryset`, las que redefinen los ganchos de permiso de DRF y las que usan una subclase de `HasPermission` o de `ScopeFilter`, pero no puede revisar una consulta escrita a mano ni el orden en que un handler propio escribe. `HasPermission.has_object_permission` queda como red de seguridad para quien llame a `check_object_permissions`: responde 404, pero con un cuerpo que puede diferir del de un objeto inexistente, así que no equivale a filtrar con `scoped()`. Un serializador de tenant nunca acepta del cliente la clave primaria ni `organization_id`: la clave primaria es única entre organizaciones, y un `id` escribible convierte esa unicidad en un oráculo de existencia y deja que un PATCH inserte una fila. Los modelos reales usan `uuid7_primary_key()` (`editable=False`), que DRF expone como solo lectura.

### OBS-F2-05B-3 — Fuera del alcance del método es 404, aunque el objeto se pueda leer
El filtro usa el permiso del método en curso. Quien puede ver un objeto pero no tiene alcance para modificarlo recibe 404 en el PATCH, no 403. Es deliberado: una sola regla, sin revelar nada.

### OBS-F2-05B-4 — Poder escribir implica leer la respuesta
Cada método se autoriza con su propio permiso y alcance, y no hay implicación entre permisos (ADR-003 §5). Un PATCH responde con el objeto serializado: quien tiene el permiso de escritura con un alcance mayor que el de lectura, o sin el de lectura, ve esa respuesta. La gestión de roles (E01-08) debería mantener el alcance de escritura dentro del de lectura; un endpoint que no deba revelar nada puede responder 204.

### OBS-F2-05B-5 — El alcance se comprueba sobre la fila antes de escribir, no sobre el resultado
`HasPermission` solo comprueba que el permiso existe y `ScopeFilter` filtra la fila tal como está antes de la escritura. Un POST no tiene fila previa: crear no pasa por ningún alcance. Un PUT o PATCH que exponga una columna de la `ScopePolicy` (asignado, creador, equipo, sucursal) puede dejar la fila fuera del alcance de quien la escribe. Con alcance OWN se puede así crear o dejar una fila asignada a otra persona de la misma organización; la organización sale siempre del contexto. Hoy ninguna vista crea filas ni expone esas columnas. El primer endpoint que lo haga debe fijarlas desde el contexto, dejarlas de solo lectura o comprobar la instancia resultante con `require(ectx, código, instancia)` antes de guardar. La auditoría del URLconf no lo detecta.

### OBS-F2-05B-6 — Las respuestas de tenant no se cachean por URL
La auditoría rechaza un decorador alrededor de `as_view()` (uno que responda antes de la vista, como `cache_page`, se salta `HasPermission`). No ve un `cache_page` puesto sobre el handler: ahí el permiso sí se comprueba, pero la clave es solo la URL y la respuesta de un alcance se serviría a otro. Hoy no hay caché de respuestas ni rutas de tenant reales. Antes de cachear una respuesta de tenant hay que decidir una clave que incluya la membresía y sus permisos.

### OBS-F2-05A-1 — Los permisos son una foto por petición
`execution_context` lee la membresía y sus concesiones una vez, dentro del `tenant_scope` de la petición. Revocar un rol surte efecto en la siguiente petición, no a mitad de una (coherente con ADR-003 §5). Sin caché. La foto queda ligada a su transacción: usarla en un `tenant_scope` posterior falla con `TenantContextError`, aunque el contexto sea igual. En DRF (F2-05B) la foto se guarda en la petición HTTP, así que la comparten todos los envoltorios `Request` que DRF crea para ella (por ejemplo al describir la vista en un OPTIONS).

### OBS-F2-05A-2 — BRANCH equivale a OWN hasta que la membresía tenga sucursal
✅ `TEAM`, resuelto en F2-52 (#162): `execution_context` rellena `ExecutionContext.team_ids` con los equipos de la membresía (`team_members`), en la misma consulta que la membresía y sin una consulta por rol. ✅ `BRANCH`, resuelto en F2-68 (#194): la membresía lleva su sucursal (`default_branch_id`, modelo de datos §E.1) y `execution_context` la pone en `ExecutionContext.branch_ids`, en esa misma consulta. Decisiones del programa (ADR-015 §5), a confirmar: **una sola sucursal por membresía**, como dice el modelo de datos (quien deba ver varias necesita el alcance `ORGANIZATION`); **cuenta también una sucursal inactiva**, igual que los equipos inactivos; **una sucursal con membresías no se borra** (hoy no hay borrado de sucursales). Sin sucursal asignada, `BRANCH` sigue equivaliendo a `OWN`. Desde F2-69 (#196) la asigna `PUT …/members/{id}/branch/`, con las reglas de suspender a un miembro (ADR-017): `users.manage`, nadie cambia la suya y el actor cubre los roles del miembro, bajo el bloqueo de RBAC. Decisiones de ese work item, a confirmar: el permiso es `users.manage` (no hay uno propio en el catálogo); se puede asignar una sucursal inactiva y a una membresía en cualquier estado; la regla de cubrir los roles del miembro no distingue de qué sucursal es cada `BRANCH` (un actor con `BRANCH` sobre su sucursal cubre un `BRANCH` del miembro y puede moverlo a otra): es la parte de D-F2-13 que queda abierta. Como con `TEAM`, ningún permiso del catálogo admite alcance: asignar una sucursal amplía lo que alcanzaría una concesión `BRANCH`, y la regla sobre qué debe cubrir quien la asigna es la misma pregunta de D-F2-13.

### OBS-F2-05A-3 — La transacción de la petición se confirma aunque la vista falle
✅ Resuelta en F2-12 (#59): `TenantResolutionMiddleware` deshace la transacción de la petición cuando la respuesta es 400 o superior. Un servicio sigue comprobando antes de escribir, pero un error ya no deja escrituras a medias. Lo que sigue describe el estado anterior.

El middleware de tenant convierte la excepción en respuesta dentro del `tenant_scope`, así que un 403 o un 500 hacen COMMIT de lo ya escrito. Todo servicio debe comprobar antes de escribir y envolver sus escrituras en un savepoint. Es comportamiento previo a esta fase; afecta a F2-05C y a todo servicio posterior. En DRF (F2-05B) el permiso se comprueba antes de ejecutar el método de la vista; el alcance se comprueba dentro, en `get_object()` y `filter_queryset()`. Las vistas genéricas de serie no escriben antes de esa consulta, así que una denegación no deja escrituras. Una vista que escriba antes de llamar a `get_object()` (o a `scoped()`) responde 404 y confirma lo ya escrito, igual que un error posterior dentro de la vista; la auditoría del URLconf no detecta ese orden.

### OBS-F2-05A-4 — Denegaciones sin auditar
ADR-011 prevé auditar los accesos denegados. El motor no escribe filas `DENIED`. F2-05B crea el punto HTTP (`HasPermission`) pero no audita: falta decidir qué denegaciones se registran y con qué detalle. Queda para E01-13. F2-73 (#204) abre la lectura de la auditoría (`GET …/audit/`, permiso `audit.view`) sin cambiar esto: sigue sin haber filas `DENIED` escritas por el motor.

### OBS-F2-05A-5 — Cuerpo de error de la API
✅ Resuelta en F2-12 (#59): un único cuerpo `{"code": …}` para los errores de DRF, del middleware y de Django bajo `/api/`, con `URL_FORMAT_OVERRIDE` desactivado ([ADR-014](../adr/ADR-014-api-errors-and-authentication.md) §1 y §3). Lo que sigue describe el estado anterior. No hay manejador de excepciones propio: una denegación en una vista de DRF devuelve `{"detail": …}` y no la convención `{"code": …}` del middleware. Con F2-05B conviven las dos: 401, 404 y 403 `ORG_SUSPENDED` del middleware con `{"code": …}`; 403 y 404 de DRF con `{"detail": …}`. En las vistas genéricas con `ScopeFilter`, los 404 son idénticos entre sí (fuera de alcance, otra organización, inexistente); el 404 de `HasPermission.has_object_permission` tiene otro texto (OBS-F2-05B-2). La decisión, antes del primer endpoint real, debe dejar un único cuerpo para todos los 404 de una ruta de tenant. DRF negocia el formato antes de comprobar el permiso: en una ruta de tenant, un miembro activo que pida un formato que la API no sirve recibe 404 (`?format=xml`) o 406 (`Accept: application/xml`) en lugar del 403 o del 404 de alcance, tenga o no el permiso. No concede ni revela nada: la vista no se ejecuta y el 401 y el 404 del middleware van antes. Esa decisión debe cubrir también estas dos respuestas; como la API es solo JSON, una opción es fijar `URL_FORMAT_OVERRIDE: None`, que hoy no está configurado.

### OBS-F2-04-1 — Las concesiones del rol Owner no siguen al catálogo
`clone_role_templates` no toca un rol que ya existe y `sync_permissions` solo sincroniza `permissions`. El rol Owner se modela con concesiones explícitas de todo el catálogo, así que una organización ya creada no recibe los permisos que añada una fase posterior.
- Todo work item que añada permisos al catálogo debe decidir cómo llegan al rol Owner de cada organización (localizado por `is_owner_role`, nunca por código): migración de datos o un paso tras el `migrate`.
- Lo mismo aplica a las demás plantillas si se quiere que los cambios lleguen a roles ya clonados.
- ✅ F2-41 (#140, ADR-018): cerrada para el rol Owner. El job de migraciones le añade lo que le falte del catálogo. Las demás plantillas siguen sin seguir al catálogo, por decisión: en una organización que ya existe son roles suyos.

### OBS-F2-04-2 — Los roles de sistema se pueden borrar y renombrar
[02-modelo-de-datos.md](../fase-0/02-modelo-de-datos.md) §E.3 dice que un rol `is_system` no se borra y su código no cambia. F2-04 no lo impone: no existe gestión de roles hasta E01-08. Un rol plantilla borrado se vuelve a crear en el siguiente clonado.
- Imponerlo en E01-08, junto con las reglas de edición del rol Owner.
- ✅ F2-38 (#134): impuesto. `delete_role` no borra un rol `is_system` (409 `ROLE_IS_SYSTEM`) y ninguna ruta cambia el código de un rol. Renombrarlo y cambiar sus permisos sigue permitido, salvo en el Owner.

### OBS-F2-04-3 — `permissions`: PK UUIDv7 y `code` único
[ADR-003](../adr/ADR-003-auth-session.md) §5 esboza `permissions(code PK)`; [ADR-004](../adr/ADR-004-identifiers.md) §1 exige PK UUIDv7 en todas las tablas y un test lo comprueba. Se cumple ADR-004: `id` UUIDv7 y `code` `UNIQUE`, que es la clave que referencian las concesiones (`permission_code`). Ningún ADR cambia.

### OBS-F2-04-4 — Retirar, renombrar o cambiar el alcance de un permiso
La sincronización del catálogo borra un código retirado solo si nadie lo tiene concedido; si está concedido lo conserva y avisa en el log. Cambiar `supports_scope` de un permiso concedido hace fallar el `migrate` por la FK, a propósito.
- Cualquiera de esos cambios necesita una migración de datos que reescriba antes las concesiones.

### OBS-F2-04-5 — Concesiones de las plantillas
La matriz de [03 §H](../fase-0/03-tenancy-rbac-inbox-ia.md) no tiene filas para `organization.view`, `users.view`, `users.invite` ni `roles.view`. F2-04 asume mínimo privilegio: Owner, todo el catálogo; Administrador, `organization.view`, `users.view`, `users.manage`, `users.invite` y `roles.view`; Supervisor, `organization.view` y `users.view`; Vendedor, `organization.view`. Las plantillas Soporte, Marketing y Consulta se añadirán cuando el catálogo las distinga.
- Pendiente de confirmación del PO.
- Desde F2-44 (#146), Administrador lleva además `branches.manage` en las organizaciones nuevas (OBS-F2-44-1).
- Desde F2-50 (#158), Administrador y Supervisor llevan además `teams.view` en las organizaciones nuevas (OBS-F2-50-1).
- Desde F2-53 (#164), Administrador lleva además `teams.manage` en las organizaciones nuevas (D-F2-12).

### OBS-F2-04-6 — Sin borrado lógico y un alcance por concesión
Los roles no llevan `deleted_at` (convención [SD]): no hay flujo de borrado hasta E01-08. Un rol tiene un solo alcance por permiso; combinar `TEAM` y `BRANCH` sobre el mismo permiso requiere dos roles, y los permisos efectivos (F2-05A) unen los alcances de todos los roles.

### OBS-F2-04-7 — Capas para el bootstrap
✅ Resuelta en F2-06 (#43): `apps.provisioning` es el punto de orquestación, una capa por encima de `organizations`, `access` y `accounts` en el contrato de import-linter. La regla del último Owner sigue en `access`.

`apps.organizations` y `apps.access` son módulos hermanos que no se importan entre sí. El bootstrap (F2-06) y la regla "siempre un Owner activo" (F2-05C, #51) necesitan un punto de orquestación por encima de ambos.

### OBS-F2-02-1 — F2-03A no debe empezar con D-F2-1 y D-F2-2 abiertas
Al cerrarse F2-02, el orquestador pasa #40 a `status:ready` porque solo lee dependencias entre issues. Las decisiones D-F2-1 (auditoría de plataforma) y D-F2-2 (almacén de sesiones) siguen sin resolver.
- Propuesta: un work item de decisión (`docs/…`, con ADR nuevo si la auditoría de plataforma amplía ADR-001 o ADR-011) añadido como dependencia de #40. Lo crea el mantenedor.
- ✅ Resuelta el 2026-10-02: F2-09 (#55) cierra las dos decisiones y #40 depende de #55 y de #56.

### OBS-F2-02-2 — FK de tenant por tabla
`organization_memberships` añade su FK a `organizations` en la migración, igual que `files`. `TenantModel` sigue sin una FK genérica (OBS-F1-04-1 en [phase-1.md](phase-1.md)): cada tabla tenant-owned debe declararla.
- No bloqueante.

### OBS-F2-02-3 — Escrituras rechazadas en `user_scope`
Sin tenant activo, `INSERT` falla con error de RLS; `UPDATE` y `DELETE` no fallan: afectan a cero filas, porque la política `USING` no deja ver ninguna. El efecto es el mismo (no se escribe), pero el código que espere una excepción no la recibirá.
- No bloqueante.

### OBS-F2-01-1 — Tablas de `django.contrib.auth` sin uso
Instalar `django.contrib.auth` crea `auth_permission`, `auth_group` y `auth_group_permissions`. El modelo `User` no usa `PermissionsMixin`: esas tablas quedan vacías de significado y el RBAC del producto será el de `access` (F2-04).
- No bloqueante. Revisar si conviene retirarlas cuando exista `access`.

### OBS-F2-01-2 — Parte local del email solo ASCII
El validador de Django rechaza partes locales con caracteres no ASCII (direcciones SMTPUTF8). Se acepta como límite conocido.
- No bloqueante. Reabrir si un cliente lo necesita.

### OBS-F2-01-4 — El redactor no trata el email como dato sensible
`core.redaction` redacta secretos (contraseñas, tokens, credenciales), pero no la clave `email`. F2-01 no registra emails: `User.__str__` devuelve el identificador, no la dirección.
- Decidir antes de F2-03A si los eventos de acceso registran el email, un hash o solo el `user_id`.
- ✅ Resuelta en [ADR-013](../adr/ADR-013-platform-audit.md) §4 para los eventos de acceso: `user_id` del actor autenticado, y en los intentos fallidos una huella HMAC del identificador más la cuenta afectada como entidad. Nunca el email en claro. El redactor compartido sigue sin tratar el email como secreto (la auditoría de un tenant registra cambios de email); el escritor de plataforma añade su propio filtro (F2-10, #56).

### OBS-F2-01-3 — Longitud mínima de contraseña
ADR-003 §2 exige validar contraseñas comunes o filtradas, pero no fija una longitud. F2-01 usa 12 caracteres. La comprobación contra contraseñas filtradas (servicio externo) no está implementada.
- Pendiente de confirmación del PO.

# Fase 4 — Contactos

- **Estado:** en curso desde el 2026-10-22 (F4-01), en paralelo con la Fase 3.
- **Objetivo:** el CRM comercial empieza por el cliente: contactos con su responsable y su sucursal, vistos según el alcance de cada uno, y la ficha 360° del cliente, que irá sumando lo que exista (notas, etiquetas, oportunidades, cotizaciones, pedidos, reservas).
- **Origen:** [roadmap §S.1](../fase-0/05-backlog-y-roadmap.md) (fila "Fase 4"), historias E05 del [backlog §R](../fase-0/05-backlog-y-roadmap.md), [modelo de datos §E.5](../fase-0/02-modelo-de-datos.md) y el catálogo de permisos de [03 §H](../fase-0/03-tenancy-rbac-inbox-ia.md).

Los issues llevan la etiqueta `phase:f3` porque `phase:f4` aún no existe.

## Principios

- El perfil CRM del cliente es de Good Doggy; la ficha de la tienda es de la tienda. Se vinculan de forma explícita y **nunca se fusionan por coincidencias** de nombre, correo, teléfono o documento ([ADR-028](../adr/ADR-028-store-data-connectors.md) §1). Tampoco se vincula nada por correo ([ADR-027](../adr/ADR-027-external-identity.md) §3).
- Sin `UNIQUE` sobre teléfono, correo ni documento: los duplicados se detectan y se resuelven (E05-07, E05-08), no se impiden a ciegas.
- Datos personales (security-boundaries §52): RLS, alcances, nada en los logs, documento enmascarado en la auditoría (04 §N.1), exportación como permiso sensible y anonimización (Ley 29733, E05-09).
- `contacts.view`, `contacts.update` y `contacts.delete` admiten alcance (◎): son los primeros permisos con alcance del catálogo. Por eso la decisión D-F2-13 se implementa antes (F2-123), como exige [phase-2.md](phase-2.md).

## Work items

Cada work item es un issue con el alcance completo. Este documento solo los ordena; **el issue es la fuente de verdad**.

| ID | Issue | Rama | Depende de | Tipo |
|---|---|---|---|---|
| F2-123 | [#346](https://github.com/PapiCuche/crm-gooddoggy/issues/346) Acceso: lo que cubre quien incorpora a un equipo o asigna una sucursal (D-F2-13) | `feature/f2-scoped-grant-coverage` | — | backend (RBAC) |
| F4-01 | [#347](https://github.com/PapiCuche/crm-gooddoggy/issues/347) Contactos: módulo, tabla y comandos (sin API) | `feature/f4-contacts-table` | — | backend |
| F4-02 | [#348](https://github.com/PapiCuche/crm-gooddoggy/issues/348) Contactos: permisos con alcance y lectura (API) | `feature/f4-contacts-read-api` | #347, #346 | backend + API |
| F4-03 | [#349](https://github.com/PapiCuche/crm-gooddoggy/issues/349) Contactos: crear, editar y borrar (API) | `feature/f4-contacts-write-api` | #348 | backend + API |
| F4-04 | [#350](https://github.com/PapiCuche/crm-gooddoggy/issues/350) Contactos: la pantalla de clientes (lista y crear) | `feature/f4-contacts-screen` | #349 | frontend |
| F4-05 | [#351](https://github.com/PapiCuche/crm-gooddoggy/issues/351) Contactos: editar y borrar desde la pantalla | `feature/f4-contacts-edit-screen` | #350 | frontend |
| F4-06 | [#352](https://github.com/PapiCuche/crm-gooddoggy/issues/352) Contactos: la ficha 360° del cliente (resumen, solo lectura) | `feature/f4-customer-360` | #350 | frontend |
| F4-07 | [#354](https://github.com/PapiCuche/crm-gooddoggy/issues/354) Contactos: buscar por nombre, teléfono y correo (API) | `feature/f4-contacts-search` | #348 | backend + API |
| F4-08 | [#355](https://github.com/PapiCuche/crm-gooddoggy/issues/355) Contactos: responsable y sucursal (API, `contacts.assign`) | `feature/f4-contacts-assign-api` | #349 | backend + API |

Siguen, en este orden: notas (E05-04) y etiquetas (E05-03) con sus pestañas en la ficha; identidades por plataforma (E05-02); direcciones; consentimientos por canal y propósito; duplicados y fusión (E05-07, E05-08); timeline (E05-06); anonimización (E05-09).

## Historias

| Historia | Estado |
|---|---|
| E05-01 Crear y editar contactos | Tabla y comandos en F4-01; API en F4-02 (lectura) y F4-03 (escrituras); pantalla en F4-04 y F4-05; responsable y sucursal en F4-08 |
| E05-05 Perfil 360° | Ficha con «Resumen» en F4-06; las demás pestañas llegan con sus módulos |
| E05-10 Buscar | F4-07 (sin @usuario hasta E05-02) |
| E05-02, E05-03, E05-04, E05-06 a E05-09 | Pendientes |

## Decisiones abiertas

| ID | Pregunta | Recomendación | Bloquea |
|---|---|---|---|
| D-F4-01 | **Qué es el alcance `TEAM` de un contacto.** ADR-003 §5 lo define con una columna `team_id`, pero el contacto (§E.5) no la tiene. Salidas: (a) los contactos cuyo responsable está en alguno de mis equipos (una política propia con una subconsulta a `team_members`); (b) añadir `team_id` al contacto; (c) que `TEAM` no amplíe nada más allá de `OWN`. | **(a), pendiente del mantenedor.** Sigue al equipo comercial sin otra columna que mantener. Hasta que decida, F4-02 aplica (c), la opción conservadora: un Supervisor con `TEAM` ve lo mismo que con `OWN`. | Que un Supervisor vea los contactos de su equipo |
| D-F4-02 | **La «búsqueda limitada» del Vendedor** (03 §H: `contacts.view` OWN «+ búsqueda limitada»): qué puede encontrar fuera de su alcance (por ejemplo, saber si un teléfono ya es cliente antes de crearlo) y qué ve de él. | **Solo existencia, pendiente del mantenedor:** la búsqueda exacta por teléfono o documento dice que el contacto existe y quién es su responsable, sin enseñar sus datos. Hasta entonces, nada fuera del alcance. | Evitar duplicados al crear |
| D-F4-03 | **El país por defecto del teléfono.** F4-01 exige E.164 y no adivina el país. | Que la pantalla proponga el prefijo de la organización (+51 en Perú) y la API siga exigiendo E.164. | Nada: es experiencia de uso |
| D-F4-04 | **Datos personales en la auditoría.** El nombre, el teléfono y el correo de un contacto quedan en `audit_logs.changes` (ADR-013 §4 lo permite para el correo), que `crm_app` solo puede insertar y que se guarda 2 años. Anonimizar un contacto (E05-09, Ley 29733) no podrá limpiar esas filas sin una vía privilegiada. | Una ADR para la anonimización: o bien la auditoría guarda solo una huella del dato personal, o bien un comando de operador auditado reescribe esas filas. | Mientras tanto, la anonimización no estará completa: bloquea E05-09, no F4-01 |

## Observaciones

- **F4-01, revisión:** las cuatro columnas `*_user_id` (`owner`, `created_by`, `updated_by`, `deleted_by`) llevan FK compuesta a las membresías (ADR-001 §3). El índice GIN de trigramas sobre `display_name` que pide 02 §E.5 se aplaza a la búsqueda (F4-07). El correo se guarda tal como llega; canonizar el dominio (minúsculas e IDNA, como `apps.accounts.emails`) va antes de la búsqueda y los duplicados. El RUC solo comprueba sus 11 cifras, sin dígito verificador.
- **F4-01:** fundación de la serie (AGENTS.md §11), sin API: la cierra F4-04. Columnas de §E.5 que aún no entran porque nada las usa: `avatar_file_id`, `origin_platform`, `segment`, `customer_type` (derivado de pedidos), `is_blocked` (Inbox), `custom_fields`, `first_seen_at`, `last_interaction_at`, `merged_into_id` y `search_vector` (F4-07). `primary_email` es `varchar` y no `citext` (la extensión no está instalada): se guarda como se escribió y la búsqueda (F4-07) compara sin mayúsculas. El responsable debe ser miembro de la organización, en cualquier estado, como el creador; que esté activo lo exigirá `contacts.assign` (F4-08). La auditoría lleva en `changes` el nombre, el teléfono y el correo (ADR-013 §4 lo prevé para el correo de un contacto) y el documento enmascarado; anonimizar (E05-09) tendrá que tratar también esas filas.

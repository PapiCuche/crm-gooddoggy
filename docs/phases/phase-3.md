# Fase 3 — Catálogo, precios e inventario

- **Estado:** en curso desde el 2026-10-22 (F3-01).
- **Objetivo:** el primer CRM comercial usable (Hito A del plan del mantenedor del 2026-10-22): catálogo local con productos y variantes, precios con `PricingService`, inventario LOCAL con movimientos y reservas, importación CSV/XLSX, cada pieza con su pantalla en el producto oficial.
- **Origen:** [roadmap §S.1](../fase-0/05-backlog-y-roadmap.md) (fila "Fase 3"), historias E02, E03 y E04 del [backlog §R](../fase-0/05-backlog-y-roadmap.md), [modelo de datos §E.7–§E.9](../fase-0/02-modelo-de-datos.md) y el catálogo de permisos de [03 §H](../fase-0/03-tenancy-rbac-inbox-ia.md).

La Fase 3 empieza sin esperar a que se cierre la Fase 2: el mantenedor pidió el 2026-10-22 que el núcleo comercial avance en paralelo. Lo que queda de la Fase 2 (la política de MFA por membresía, el step-up, las sesiones activas, los horarios y las invitaciones bloqueadas por D-F2-14) sigue en [phase-2.md](phase-2.md).

## Principios

- Un producto no es una variante: todo precio, stock y línea de cotización apunta a la variante. Un producto sin opciones tiene una variante por defecto.
- `available = on_hand - reserved`; los movimientos de stock solo se añaden, nunca se editan ni se borran.
- `PricingService` es la única autoridad de los precios (ADR-006).
- Inventario LOCAL primero. Los modos EXTERNAL e HYBRID y el conector con BlackDogStorePeru siguen [ADR-028](../adr/ADR-028-store-data-connectors.md), propuesto; no se implementan hasta que el mantenedor lo acepte y exista un contrato con la tienda.
- Cada tabla nueva es tenant-owned con FORCE RLS, auditada y con permisos del catálogo.

## Work items

Cada work item es un issue con el alcance completo. Este documento solo los ordena; **el issue es la fuente de verdad**.

| ID | Issue | Rama | Depende de | Tipo |
|---|---|---|---|---|
| F3-01 | [#294](https://github.com/PapiCuche/crm-gooddoggy/issues/294) Catalog: module, permissions and brands (API) | `feature/f3-catalog-brands` | #286 | backend + API |
| F3-02 | [#295](https://github.com/PapiCuche/crm-gooddoggy/issues/295) Catalog: the brands screen | `feature/f3-catalog-brands-screen` | #294 | frontend |
| F3-03 | [#301](https://github.com/PapiCuche/crm-gooddoggy/issues/301) Catalog: deleting a brand from the screen | `feature/f3-catalog-brand-delete-screen` | #295 | frontend |
| F3-04 | [#306](https://github.com/PapiCuche/crm-gooddoggy/issues/306) Catalog: hierarchical categories (API) | `feature/f3-catalog-categories` | #294 | backend + API |
| F3-04b | [#314](https://github.com/PapiCuche/crm-gooddoggy/issues/314) Catalog: rename, move and delete categories (API) | `feature/f3-catalog-categories-edit-api` | #306 | backend + API |
| F3-05 | [#307](https://github.com/PapiCuche/crm-gooddoggy/issues/307) Catalog: the categories screen | `feature/f3-catalog-categories-screen` | #306 | frontend |

Siguen, en este orden: categorías en árbol (API y pantalla), productos con opciones y variantes, SKU y alias, fotos, precios, almacenes y movimientos, reservas con su test de concurrencia, importación.

## Historias

| Historia | Estado |
|---|---|
| E02-01 Marcas y categorías | Marcas: API en F3-01; pantalla (lista, crear y renombrar) en F3-02; borrar desde la pantalla en F3-03. Categorías: API en F3-04 (listar y crear) y F3-04b (editar); pantalla en F3-05 |
| E02-02 a E02-07 | Pendientes |
| E03 Precios | Pendiente |
| E04 Inventario | Pendiente |

## Decisiones y observaciones

- **F3-01:** los permisos del catálogo (`products.view`, `products.manage`, `products.import`, `categories.manage`, `brands.manage`) entran en el catálogo; ninguno es sensible ni admite alcance. El Owner los recibe en el siguiente `migrate` (ADR-018). Administrador los recibe todos, y Supervisor y Vendedor `products.view`, en las organizaciones nuevas; en las existentes, sus roles los edita quien tiene `roles.manage` (ADR-018 §3). `apps.catalog` abre la capa L3 de [module-dependencies](../architecture/module-dependencies.md). A confirmar por el mantenedor: los permisos de cada plantilla, que el slug de una marca se forme del nombre y no cambie, y que las marcas se listen por orden de creación (la paginación de ADR-016 solo admite `id`).
- **F3-02:** la pantalla de marcas, con «Catálogo» en el menú. Por tamaño (ADR-009), borrar una marca desde la pantalla va en su propio work item. **En organizaciones creadas antes de F3-01** (también `pruebas`, la de los accesos de prueba) solo el Owner ve el catálogo hasta que conceda los permisos nuevos a los demás roles en «Roles» (ADR-018 §3).

# ADR-028: Inventario híbrido y conectores de datos con BlackDogStorePeru

- **Status:** Proposed
- **Date:** 2026-10-22
- **Deciders:** el mantenedor. Él fijó la dirección (inventario propio con modos LOCAL, EXTERNAL e HYBRID, y la tienda como primera fuente externa) el 2026-10-22. El programa autónomo escribe el diseño como propuesta y no implementa nada de conectores. El catálogo y el inventario locales siguen su propio camino en la Fase 3 (E02–E04).
- **Related:** ADR-001, ADR-002, ADR-004, ADR-006 (precios), ADR-011, ADR-013, ADR-022, ADR-015 §3, ADR-027 (identidad, va aparte), F2-119 (#293)

## Context

El mantenedor pidió investigar, por separado de la identidad (ADR-027), si hace falta conectar con su tienda BlackDogStorePeru.com clientes, productos, variantes, inventario, pedidos y pagos, y cómo serían esos conectores. No se sincroniza nada hasta verificar los contratos externos.

### Lo que se verificó de la tienda (2026-10-22)

La investigación fue pasiva y de solo lectura. El repositorio es público: aquí solo va lo necesario para decidir. El informe completo es del mantenedor y queda fuera del repositorio.

- **Verificado:** la tienda es una aplicación propia, con su propio modelo de empresas, sucursales, roles y clientes. Se solapa con varias partes del CRM.
- **Verificado:** tiene una API propia para su web, su app y su panel, pensada para usuarios humanos con sesión. No tiene credenciales para integraciones (claves de API o cuentas de servicio con permisos acotados), ni webhooks salientes.
- **Verificado:** de las entidades pedidas existen clientes, productos, inventario por sucursal, pedidos y pagos. **No existe un modelo de variantes**: cada artículo vendible es un producto.
- **Desconocido:** la necesidad de negocio. Ningún documento de los dos sistemas describe una sincronización. Más allá del interés del mantenedor, no se sabe qué flujo de trabajo la necesita.

## Decision (propuesta)

### 0. Inventario propio con tres modos

Good Doggy CRM tiene catálogo e inventario propios: cualquier organización lo usa sin tener una tienda externa. Una organización puede además conectar una fuente externa. Cada organización elige su modo, y cuando haga falta lo elige por almacén o por conjunto de productos:

- **LOCAL.** La organización gestiona su catálogo y su stock en el CRM: productos y variantes, SKU y códigos de barras, almacenes, entradas, salidas, ajustes, transferencias, reservas, importación y exportación, y movimientos trazables. Es la Fase 3 (E02–E04) y no depende de este ADR.
- **EXTERNAL.** Un sistema externo autorizado es la fuente de verdad de ciertos datos de catálogo o de inventario.
- **HYBRID.** Conviven productos locales y externos, y cada dato tiene una autoridad explícita.

Siguen valiendo los principios de la Fase 0: un producto no es una variante; `available = on_hand - reserved`; los movimientos de stock solo se añaden, nunca se editan; y el cálculo de precios es de `PricingService` (ADR-006).

### 0.1 Matriz de autoridad

Cada campo tiene un solo sistema con autoridad. La unidad es la celda variante × almacén: la organización fija un modo por defecto y lo puede cambiar por almacén o por conjunto de productos. Dos reglas que asignan autoridades distintas a una misma celda no se pueden guardar: la autoridad de cada celda sale de una sola regla, sin precedencias que adivinar. La matriz cubre la identidad del producto, la variante, el SKU, el precio, las existencias físicas, las reservas, la disponibilidad, el estado de publicación, y las imágenes y descripciones.

- Un SKU puede estar vinculado a un id externo. Vincularlos no los hace iguales: cada campo sigue a su autoridad.
- La cantidad disponible se calcula de forma determinista desde la autoridad del stock de cada almacén.
- **Nunca se suman cantidades locales y externas** sin una regla explícita de almacenes y propiedad.
- El stock externo no se cambia a mano en el CRM. Solo cambia por una operación que su dueño confirma.
- Una sincronización no sobrescribe en silencio un ajuste local. Un conflicto se ve, se audita y lo resuelve alguien.

### 0.2 Sin doble descuento

Una venta hecha desde Instagram, WhatsApp o el CRM no descuenta dos veces el mismo stock.

- **Solo la autoridad de una celda (§0.1) la descuenta.** El otro sistema refleja el resultado; no lo vuelve a aplicar.
- Un pedido importado de la tienda **no genera un movimiento de venta en el CRM** sobre stock cuya autoridad es la tienda: la tienda ya lo descontó. Solo lo genera si el CRM es la autoridad de ese almacén, una vez, con una `idempotency_key` derivada del conector y del id externo de la línea.
- Reservar o vender desde el CRM stock cuya autoridad es la tienda exige el modo bidireccional (§0.4). En solo lectura, ese stock se enseña pero no se reserva ni se vende desde el CRM.
- En bidireccional, reservar, liberar y descontar se piden a la tienda, cada operación con su propia clave idempotente (la línea del pedido y la operación), y solo cuentan cuando la tienda los confirma: el CRM no da una reserva por hecha hasta esa confirmación. Una respuesta que no llega no es un rechazo: se reintenta con la misma clave o se consulta su estado, nunca con una clave nueva. Que una reserva del CRM venza o se cancele también se pide a la tienda, igual.
- Con el conector caído, **no se inventa disponibilidad**:
  - las ventas y reservas de stock externo se **bloquean**, o se **ponen en cola** marcadas como pendientes de confirmar, si la organización lo elige. Lo que está en cola no cuenta como reservado, caduca si no se confirma a tiempo, y al volver el conector la tienda puede rechazarlo: el pedido queda marcado y lo resuelve una persona;
  - consultar el stock externo enseña el último valor conocido, **marcado como desactualizado** y con su fecha. Un valor desactualizado no cuenta como disponible para reservar, cotizar ni contestar a un cliente, tampoco a través de la IA;
  - lo local sigue funcionando.

### 0.3 Importación manual

La importación de catálogo y stock por CSV o XLSX (E03-09 y E04-07) sigue estos pasos: descargar la plantilla, subir el archivo, previsualizar, mapear columnas, validar (SKU, cantidades, precios, formatos), enseñar duplicados y errores, elegir la política de actualización, confirmar, ver el resultado, y poder revertir con movimientos compensatorios autorizados. Nada destructivo sin confirmación. Una importación no escribe en un campo ni en una celda cuya autoridad es externa: esas filas salen como error al validar.

### 0.4 Modos de sincronización con la tienda

1. **Solo lectura** de la tienda al CRM. Es el único que se activa sin más.
2. **Bidireccional**, solo con contrato y autorización explícita de las dos partes.
3. **Desconectado**: se conserva lo local permitido y lo importado deja de actualizarse, marcado como tal.

Además de lo del §2, el conector hace una sincronización inicial, sincronizaciones incrementales, recibe webhooks firmados si existen, reconcilia periódicamente, pagina y trabaja por lotes, guarda el estado de la última sincronización, se reanuda tras una interrupción y no acepta una actualización más vieja que la que ya tiene (versión o fecha de origen).

### 1. Nada se sincroniza sin dueño ni contrato

- Cada entidad tiene **un solo sistema dueño**, decidido por el mantenedor antes de conectarla. El otro sistema la lee o la recibe; no la edita.
- Ninguna sincronización empieza sin un contrato verificado: una API de integración con credenciales propias, versionada y documentada.

Propuesta de dueños, a confirmar:

| Entidad | Dueño propuesto | Dirección | Motivo |
|---|---|---|---|
| Clientes | a decidir | — | Los dos sistemas tienen clientes. Es la decisión más delicada: sin dueño claro, una sincronización en dos sentidos crea conflictos. |
| Productos | tienda | tienda → CRM, solo lectura | El catálogo nace en la tienda. |
| Variantes | tienda | tienda → CRM, solo lectura | La tienda no tiene variantes: cada artículo vendible es un producto. En el CRM el SKU, el precio y el stock son de la variante, así que cada producto de la tienda se mapea a una variante del CRM (la única de su producto), nunca a un producto. |
| Inventario | tienda | tienda → CRM, solo lectura | El stock por sucursal es de la tienda; el CRM lo enseña, no lo mueve. Reservarlo o venderlo desde el CRM pide el modo bidireccional (§0.2). |
| Pedidos de la tienda | tienda | tienda → CRM, solo lectura | Se crean en la venta de la tienda. Los pedidos creados en el CRM son del CRM. |
| Pagos | tienda | ninguna por ahora | Son datos financieros. Copiarlos al CRM no aporta nada mientras no haya un caso de uso, y aumenta el riesgo. |

### 2. Forma de un conector

Cuando exista el contrato, cada conector cumple esto:

- **Organización autorizada.** El conector es de una organización del CRM. Lo activa alguien con un permiso sensible de esa organización, con step-up (ADR-023 §7), y queda auditado.
- **Autorización del otro lado.** La tienda emite credenciales de integración para esa organización, con permisos mínimos (solo lectura de las entidades conectadas), revocables y con caducidad. Nunca la sesión de una persona.
- **Credenciales.** Cifradas en reposo (ADR-022), nunca en logs.
- **Mapeo explícito de identificadores.** Una tabla por organización: `(organization_id, connector, entity, external_id)` → id del CRM, única en los dos sentidos: un id externo apunta a un solo registro del CRM, y un registro del CRM a un solo id externo de ese conector. Se crea al importar un registro nuevo; vincular un registro local que ya existe es una acción explícita de una persona, auditada. Nunca se deduce por nombre, SKU, código de barras, correo o documento.
- **Idempotencia.** Cada cambio lleva su clave: el id externo y su versión, o la del evento. Recibirlo dos veces no cambia nada.
- **Entrada.** Por webhook firmado (exento de CSRF y de sesión, ADR-003 §3) o por consulta periódica, según lo que ofrezca la tienda. Cada mensaje se valida antes de escribir.
- **Reintentos.** En tareas en segundo plano, con espera creciente y un tope. Lo que agota los reintentos va a una cola de errores visible para la organización.
- **Errores.** Un error de una organización no detiene a las demás. Lo que no se entiende se rechaza y se registra, no se adivina.
- **Aislamiento entre tenants.** Todo lo importado vive con RLS en su organización (ADR-001, ADR-002). Un conector nunca escribe fuera de la suya.
- **Auditoría.** Activar, desactivar y cambiar credenciales queda en la auditoría (ADR-013). Cada importación deja su traza con `correlation_id` (ADR-011).
- **Desconexión.** Desactivar el conector no borra lo importado. Borrarlo es una acción aparte, auditada.

### 3. Datos personales

Clientes y pedidos llevan datos personales. Antes de conectarlos hace falta la base legal o el consentimiento que exija la Ley 29733 de Protección de Datos Personales (Perú), y decidir cuánto tiempo se guardan. Lo decide el mantenedor.

### 4. Lo que falta para empezar

1. Que el mantenedor responda: qué entidad necesita qué flujo de trabajo, quién es dueño de los clientes, si la sincronización es en un solo sentido, y con qué latencia (al momento o por lotes).
2. Que la tienda ofrezca una API de integración con credenciales por organización y con permisos, o webhooks firmados.
3. Que se decida cómo se corresponde una empresa de la tienda con una organización del CRM, y sus sucursales.

Hasta entonces no se escribe código de conectores.

## Alternatives considered

- **Usar la API actual de la tienda con la sesión de una persona.** Es una credencial humana, con todos sus permisos y sin caducidad pensada para máquinas. Descartado.
- **Leer la base de datos de la tienda directamente.** Se salta las reglas de la tienda, acopla los dos esquemas y exige credenciales de base de datos. Descartado.
- **Sincronizar en los dos sentidos desde el principio.** Sin dueño claro, provoca conflictos. Descartado hasta que haya un caso que lo exija.

## Consequences

- No hay conectores todavía. Cuando haya respuestas y contrato, cada entidad entra como un slice pequeño (ADR-009), empezando por una de solo lectura.
- La tienda tendría que añadir una API de integración o webhooks.

## Security implications

- Credenciales de máquina con permisos mínimos y revocables, cifradas en reposo.
- Aislamiento por organización con RLS, y auditoría de cada cambio de configuración.
- Datos personales solo con base legal.

## Operational implications

- Colas, reintentos y una cola de errores visible por organización.
- Métricas de retraso y de errores por conector.

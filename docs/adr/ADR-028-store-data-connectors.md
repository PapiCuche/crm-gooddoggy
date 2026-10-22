# ADR-028: Inventario híbrido y conectores de datos con BlackDogStorePeru

- **Status:** Accepted
- **Date:** 2026-10-22
- **Deciders:** el mantenedor aprobó la dirección el 2026-10-22 (inventario propio con modos LOCAL, EXTERNAL e HYBRID, la tienda como primera fuente externa y las decisiones de §0). El programa autónomo escribió el ADR (F2-119, F2-121). Aceptarlo no implementa ningún conector: esperan a un contrato con la tienda (§11). El catálogo y el inventario locales siguen su propio camino en la Fase 3 (E02–E04).
- **Related:** ADR-001, ADR-002, ADR-003, ADR-004, ADR-006 (precios), ADR-011, ADR-013, ADR-022, ADR-023, ADR-015 §3, ADR-027 (identidad, va aparte), F2-119 (#293), F2-121 (#304)

## Context

El mantenedor pidió investigar, por separado de la identidad (ADR-027), si hace falta conectar con su tienda BlackDogStorePeru.com clientes, productos, variantes, inventario, pedidos y pagos, y cómo serían esos conectores. No se sincroniza nada hasta verificar los contratos externos.

### Lo que se verificó de la tienda (2026-10-22)

La investigación fue pasiva y de solo lectura. El repositorio es público: aquí solo va lo necesario para decidir. El informe completo es del mantenedor y queda fuera del repositorio.

- **Verificado:** la tienda es una aplicación propia, con su propio modelo de empresas, sucursales, roles y clientes. Se solapa con varias partes del CRM.
- **Verificado:** tiene una API propia para su web, su app y su panel, pensada para usuarios humanos con sesión. No tiene credenciales para integraciones (claves de API o cuentas de servicio con permisos acotados), ni webhooks salientes.
- **Verificado:** de las entidades pedidas existen clientes, productos, inventario por sucursal, pedidos y pagos. **No existe un modelo de variantes**: cada artículo vendible es un producto.
- **Desconocido:** la necesidad de negocio. Ningún documento de los dos sistemas describe una sincronización. Más allá del interés del mantenedor, no se sabe qué flujo de trabajo la necesita.

## Decision

### 0. Decisiones del mantenedor (2026-10-22)

- **Good Doggy es dueño del perfil CRM** de cada cliente, de sus conversaciones, oportunidades y notas.
- **La tienda conserva la autoridad sobre sus registros comerciales originales**: su ficha de cliente, sus pedidos y sus pagos.
- **Los registros se vinculan de forma explícita**, nunca se fusionan por coincidencias (nombre, correo, teléfono, documento, SKU o código de barras).
- **El catálogo local es del CRM.** Los campos importados respetan la autoridad configurada (§2).
- **El stock de los almacenes de la tienda es de la tienda.**
- **Cada celda variante × almacén tiene exactamente una autoridad** (§2).
- **La primera integración es de solo lectura.** Una integración bidireccional solo existe con una API segura, idempotencia y autorización explícita (§7).
- **Sincronización incremental cuando exista un contrato**, siempre con una reconciliación periódica (§7).

### 1. Inventario propio con tres modos

Good Doggy CRM tiene catálogo e inventario propios: cualquier organización lo usa sin tener una tienda externa. Una organización puede además conectar una fuente externa. Cada organización elige su modo, y cuando haga falta lo elige por almacén o por conjunto de productos:

- **LOCAL.** La organización gestiona su catálogo y su stock en el CRM: productos y variantes, SKU y códigos de barras, almacenes, entradas, salidas, ajustes, transferencias, reservas, importación y exportación, y movimientos trazables. Es la Fase 3 (E02–E04) y no depende de este ADR.
- **EXTERNAL.** Un sistema externo autorizado es la fuente de verdad de ciertos datos de catálogo o de inventario.
- **HYBRID.** Conviven productos locales y externos, y cada dato tiene una autoridad explícita.

Siguen valiendo los principios de la Fase 0:

- un producto no es una variante;
- `available = on_hand - reserved`;
- los movimientos de stock solo se añaden, nunca se editan;
- un pago no es un pedido: un pago importado no crea ni cierra un pedido, y el estado de un pedido no se deduce de un pago;
- el cálculo de precios es de `PricingService` (ADR-006).

### 2. Matriz de autoridad

Cada campo tiene un solo sistema con autoridad. Para el stock (existencias, reservas y disponibilidad) la unidad es la celda variante × almacén; para los datos de catálogo y el precio, la variante, sin almacén. La organización fija un modo por defecto y lo puede cambiar por conjunto de productos y, para el stock, por almacén. Dos reglas que asignan autoridades distintas a un mismo campo de una misma celda no se pueden guardar: la autoridad de cada celda sale de una sola regla, sin precedencias que adivinar. La matriz cubre la identidad del producto, la variante, el SKU, el precio, las existencias físicas, las reservas, la disponibilidad, el estado de publicación, y las imágenes y descripciones.

- Un SKU puede estar vinculado a un id externo. Vincularlos no los hace iguales: cada campo sigue a su autoridad.
- La cantidad disponible se calcula de forma determinista desde la autoridad del stock de cada almacén.
- **Nunca se suman cantidades locales y externas** sin una regla explícita de almacenes y propiedad.
- El stock externo no se cambia a mano en el CRM. Solo cambia por una operación que su dueño confirma.
- **Una sincronización nunca escribe un campo cuya autoridad es el CRM.** Por eso no pisa un ajuste local. Un conflicto solo aparece cuando cambia la autoridad de un campo o cuando se vincula un registro local que ya existía: se ve, se audita y lo resuelve una persona.

### 3. Stock externo: instantáneas versionadas

El stock de una celda cuya autoridad es la tienda **no se copia a `inventory_levels`**. Copiarlo convertiría cada sincronización en movimientos `ADJUSTMENT` del CRM, y podría romper `reserved <= on_hand` cuando la tienda baja sus existencias por debajo de una reserva local.

- Cada almacén de la tienda es en el CRM una fila de `warehouses` de la organización, marcada como del conector: su autoridad es la tienda. Así las celdas de stock siguen un solo modelo, variante × almacén. Se vincula por el mapeo explícito de identificadores (§9), y la marca la añade el work item del conector. Sus celdas no tienen filas en `inventory_levels` ni movimientos en el CRM.
- Su stock vive en una tabla aparte de instantáneas: una fila por `(organization_id, connector_id, variant_id, warehouse_id)`, donde `warehouse_id` es esa fila de `warehouses`. El servicio de inventario local rechaza movimientos y reservas sobre un almacén marcado como del conector: el work item que añade la marca añade esa guarda. Guarda las cantidades que da la tienda, la versión o secuencia de origen y `fetched_at`.
- Es tenant-owned, con FORCE RLS (ADR-001, ADR-002).
- La disponibilidad de una celda externa se calcula desde su instantánea, con la misma fórmula: `available = on_hand - reserved`, con las cantidades de la tienda.
- **Una instantánea vieja se marca como desactualizada**: cuando supera la antigüedad configurada, o cuando el conector está caído o desconectado. Se enseña con su fecha, y no cuenta como disponible (§4).
- **Una celda que desaparece en la tienda** (se retiró el producto o el almacén) se marca como desaparecida, con su fecha, y cuenta como no disponible. Nunca se borra en silencio.
- Cada cambio de una instantánea deja su traza con `correlation_id` (ADR-011).

**Orden de llegada.** Una instantánea solo se reemplaza por otra con una versión o secuencia de origen mayor. La tienda manda valores absolutos, no diferencias. No se ordena por fecha, porque los relojes de dos sistemas no son comparables. Una actualización con una versión menor o igual se ignora, y se cuenta en las métricas del conector; eso incluye una lectura completa que empezó antes de que se guardara la instantánea más nueva. Los dos instantes se comparan con el reloj del CRM. Si es la propia tienda la que vuelve atrás (una lectura completa de la reconciliación, empezada después del `fetched_at` de la instantánea guardada, trae una versión menor que la suya, por ejemplo tras restaurar una copia de seguridad), no es un mensaje tardío: se rechaza, la celda queda desactualizada y se avisa. Aceptar la nueva base pide que un operador reinicie las versiones de ese conector, con su motivo y auditado. Sin una versión monótona del origen no hay sincronización incremental de stock: el contrato tiene que darla (§11).

### 4. Sin doble descuento

Una venta hecha desde Instagram, WhatsApp o el CRM no descuenta dos veces el mismo stock.

- **Solo la autoridad de una celda (§2) la descuenta.** El otro sistema refleja el resultado; no lo vuelve a aplicar.
- Un pedido importado de la tienda **no genera un movimiento de venta en el CRM** sobre stock cuya autoridad es la tienda: la tienda ya lo descontó. Solo lo genera si el CRM es la autoridad de ese almacén, una vez, con una `idempotency_key` derivada del conector y del id externo de la línea.
- Reservar o vender desde el CRM stock cuya autoridad es la tienda exige el modo bidireccional (§7). En solo lectura, ese stock se enseña pero no se reserva ni se vende desde el CRM.
- En bidireccional, reservar, liberar y descontar se piden a la tienda, cada operación con su propia clave idempotente (la línea del pedido y la operación), y solo cuentan cuando la tienda los confirma: el CRM no da una reserva por hecha hasta esa confirmación. Una respuesta que no llega no es un rechazo: se reintenta con la misma clave o se consulta su estado, nunca con una clave nueva. Que una reserva del CRM venza o se cancele también se pide a la tienda, igual.
- Con el conector caído, **no se inventa disponibilidad**:
  - las ventas y reservas de stock externo se **bloquean**, o se **ponen en cola** marcadas como pendientes de confirmar, si la organización lo elige. Lo que está en cola no cuenta como reservado, caduca si no se confirma a tiempo, y al volver el conector la tienda puede rechazarlo: el pedido queda marcado y lo resuelve una persona;
  - consultar el stock externo enseña la última instantánea, **marcada como desactualizada** y con su fecha (§3). Un valor desactualizado no cuenta como disponible para reservar, cotizar ni contestar a un cliente, tampoco a través de la IA;
  - lo local sigue funcionando.

### 5. Importación manual

La importación de catálogo y stock por CSV o XLSX (E03-09 y E04-07) sigue estos pasos: descargar la plantilla, subir el archivo, previsualizar, mapear columnas, validar (SKU, cantidades, precios, formatos), enseñar duplicados y errores, elegir la política de actualización, confirmar, ver el resultado, y poder revertir con movimientos compensatorios autorizados. Nada destructivo sin confirmación.

- **Una fila que toca un campo o una celda cuya autoridad es otra sale como error al validar.** La importación no la aplica.
- **El stock se importa como cantidades absolutas.** Al aplicar, y no al previsualizar, el CRM calcula para cada celda un movimiento `ADJUSTMENT` contra su `on_hand` de ese momento, con la fila de `inventory_levels` bloqueada. Si el `on_hand` cambió desde la previsualización, la fila queda `STALE` y no se aplica a ciegas, como en los lotes de precios (`price_change_batch_items`, [02-modelo-de-datos.md](../fase-0/02-modelo-de-datos.md) §E.8). Una cantidad absoluta menor que el `reserved` de su celda sale como error: la importación no libera reservas ni rompe `reserved <= on_hand`.
- **Aplicar dos veces no cambia nada.** Las cantidades son absolutas, y cada movimiento lleva una `idempotency_key` derivada de la importación y de la fila: aplicar otra vez la misma importación no crea movimientos nuevos.
- **Subir otra vez un archivo igual solo avisa.** La importación se reconoce por el hash de su contenido, y la pantalla dice que ese archivo ya se subió antes. No se bloquea: un recuento legítimo puede dar los mismos números. Como las cantidades son absolutas, una celda que no cambió no genera ajuste.
- La fila `STALE` necesita ese valor en `data_import_rows.action`, que hoy no lo tiene ([02-modelo-de-datos.md](../fase-0/02-modelo-de-datos.md) §E.12). Lo añade el work item de la importación de stock.
- Los precios de una importación siguen el §6.

### 6. Precios externos

Un precio que viene de la tienda, por importación o por conector, **solo entra como filas del módulo de precios**.

- Entra en una lista de precios (`price_lists`), por un lote de cambios (`price_change_batches`): previsualizar, confirmar y aplicar, con detección de `STALE` si el precio cambió entre la previsualización y la aplicación (`price_change_batch_items`, §E.8 del modelo de datos). Aplicar llama a `PriceChangeService.set_price`, como cualquier otro cambio (ADR-006 §3).
- Un precio en una moneda distinta de la de su lista se rechaza al validar. No se convierte.
- El conector propone el lote. Lo confirma una persona con el permiso de precios, con step-up si es una edición masiva (ADR-003 §4). Confirmarlo de forma automática pediría otra decisión.
- **`PricingService` sigue siendo el único que resuelve un precio** (ADR-006 §2). El conector no lee `product_prices`, no calcula precios y no guarda un precio fuera del módulo de precios. La regla de import-linter de ADR-006 también vale para él.

### 7. Modos de sincronización con la tienda

1. **Solo lectura** de la tienda al CRM. Es el de la primera integración, y el único que se activa sin más.
2. **Bidireccional**, solo con una API segura de la tienda, idempotencia en cada operación (§4) y autorización explícita de las dos partes.
3. **Desconectado**: se conserva lo local permitido y lo importado deja de actualizarse, marcado como tal.

Además de lo del §9, el conector hace una sincronización inicial y, **cuando el contrato lo permita, sincronizaciones incrementales**. **Siempre reconcilia periódicamente**: compara con una lectura completa y corrige lo que el incremental perdió, con las mismas reglas de versión (§3). Pagina y trabaja por lotes, guarda el estado de la última sincronización, se reanuda tras una interrupción y no acepta una actualización con una versión de origen menor o igual que la que ya tiene.

### 8. Dueños por entidad

- Cada dato tiene **un solo sistema dueño**: el registro entero o, en un producto o una variante importados, cada campo según la matriz de autoridad (§2). El otro sistema lo lee o lo recibe; no lo edita. Una entidad puede tener dos registros vinculados, cada uno con su dueño: es el caso de los clientes.
- Ninguna sincronización empieza sin un contrato verificado: una API de integración con credenciales propias, versionada y documentada.

| Entidad | Dueño | Dirección | Motivo |
|---|---|---|---|
| Clientes | dos registros vinculados: el perfil CRM, sus conversaciones, oportunidades y notas son del CRM; la ficha de cliente de la tienda es de la tienda | tienda → CRM, solo lectura, sobre la ficha de la tienda | Los dos sistemas tienen clientes. Se vinculan de forma explícita (§9); nunca se fusionan por coincidencias. |
| Productos | los locales, del CRM; los importados, según la matriz de autoridad (§2) | tienda → CRM, solo lectura, en los campos de autoridad externa | El catálogo local es del CRM. |
| Variantes | igual que los productos | tienda → CRM, solo lectura | La tienda no tiene variantes: cada artículo vendible es un producto. En el CRM el SKU, el precio y el stock son de la variante, así que cada producto de la tienda se mapea a una variante del CRM (la única de su producto), nunca a un producto. |
| Inventario de los almacenes de la tienda | tienda | tienda → CRM, solo lectura, en instantáneas (§3) | El CRM lo enseña, no lo mueve. Reservarlo o venderlo desde el CRM pide el modo bidireccional (§4). |
| Inventario de los almacenes del CRM | CRM | ninguna | Es el inventario LOCAL. |
| Pedidos de la tienda | tienda | tienda → CRM, solo lectura | Se crean en la venta de la tienda. Los pedidos creados en el CRM son del CRM. |
| Pagos | tienda | ninguna por ahora | Son datos financieros. Copiarlos al CRM no aporta nada mientras no haya un caso de uso, y aumenta el riesgo. Un pago no es un pedido (§1). |

### 9. Forma de un conector

Cuando exista el contrato, cada conector cumple esto:

- **Organización autorizada.** El conector es de una organización del CRM. Lo activa alguien con un permiso sensible de esa organización, con step-up (ADR-023 §7), y queda auditado.
- **Autorización del otro lado.** La tienda emite credenciales de integración para esa organización, con permisos mínimos (solo lectura de las entidades conectadas), revocables y con caducidad. Nunca la sesión de una persona.
- **Credenciales.** Cifradas en reposo (ADR-022), nunca en logs.
- **Mapeo explícito de identificadores.** Una tabla por organización: `(organization_id, connector, entity, external_id)` → id del CRM, única en los dos sentidos: un id externo apunta a un solo registro del CRM, y un registro del CRM a un solo id externo de ese conector. Se crea al importar un registro nuevo; vincular un registro local que ya existe es una acción explícita de una persona, auditada. Nunca se deduce por nombre, SKU, código de barras, correo, teléfono o documento.
- **Idempotencia.** Cada cambio lleva su clave: el id externo y su versión, o la del evento. Recibirlo dos veces no cambia nada.
- **Entrada.** Por consulta periódica o por webhook, según lo que ofrezca la tienda. Cada mensaje se valida antes de escribir.
- **Webhooks.** La ruta está exenta de CSRF y de sesión, y se autentica por firma (ADR-003 §3):
  - la firma cubre el cuerpo tal como llegó, en bytes y antes de interpretarlo, y una marca de tiempo; se compara en tiempo constante, y una marca fuera de la ventana anti-replay se rechaza;
  - al rotar el secreto, valen dos secretos durante un solapamiento acotado; al acabar, solo el nuevo;
  - el id de cada evento vale una vez **por conector**: dos organizaciones de la tienda podrían repetir un id. Se guarda al menos mientras dura la ventana, y un id repetido no hace nada;
  - una petición con una firma que no vale se rechaza antes de contar en nada del conector; su límite es por dirección de red. Así nadie que conozca la ruta silencia los avisos de un conector;
  - las lecturas que disparan los avisos válidos se agrupan por registro y tienen un tope por conector: una ráfaga de avisos no gasta el presupuesto de peticiones a la tienda;
  - **la organización sale del conector**: su id en la ruta se resuelve con una función `SECURITY DEFINER` mínima como la de las cuentas de canal (ADR-002 §3.3), que devuelve solo `organization_id` y el id del conector, nunca el secreto. El secreto se lee después, dentro del `tenant_scope` de esa organización, y la firma se comprueba antes de escribir nada. **Nunca del contenido del mensaje**;
  - un webhook es un aviso, no un dato: dispara una nueva lectura por la API con las credenciales del conector, y se escribe lo que devuelve esa lectura.
- **Límites de la tienda.** El conector respeta los límites de frecuencia de la tienda: un presupuesto por conector, espera ante una respuesta de límite excedido y nunca más peticiones en paralelo de las acordadas.
- **Reintentos.** En tareas en segundo plano, con espera creciente y un tope. Lo que agota los reintentos va a una cola de errores visible para la organización.
- **Cola de errores.** Guarda el id externo, la entidad, el motivo y la fecha, no el mensaje completo con datos personales. Se conserva un tiempo configurado y después se borra.
- **Errores.** Un error de una organización no detiene a las demás. Lo que no se entiende se rechaza y se registra, no se adivina.
- **Aislamiento entre tenants.** Todo lo importado vive con RLS en su organización (ADR-001, ADR-002). Un conector nunca escribe fuera de la suya.
- **Auditoría.** Activar, desactivar y cambiar credenciales queda en la auditoría de la organización (`audit_logs`), no en la de plataforma (ADR-013 §1). Cada importación deja su traza con `correlation_id` (ADR-011).
- **Desconexión.** Desactivar el conector no borra lo importado. Borrarlo es una acción aparte, auditada.

### 10. Datos personales

Clientes y pedidos llevan datos personales. Antes de conectarlos hace falta la base legal o el consentimiento que exija la Ley 29733 de Protección de Datos Personales (Perú), y decidir cuánto tiempo se guardan. Lo decide el mantenedor.

### 11. Lo que falta para empezar

1. Que el mantenedor diga qué entidad necesita qué flujo de trabajo, y con qué latencia (al momento o por lotes).
2. Que la tienda ofrezca una API de integración con credenciales por organización y con permisos, con una versión o secuencia de origen monótona por registro (§3), y, si hay webhooks, firmados (§9).
3. Que se decida cómo se corresponde una empresa de la tienda con una organización del CRM, y sus sucursales con almacenes externos.

Hasta entonces no se escribe código de conectores.

## Alternatives considered

- **Usar la API actual de la tienda con la sesión de una persona.** Es una credencial humana, con todos sus permisos y sin caducidad pensada para máquinas. Descartado.
- **Leer la base de datos de la tienda directamente.** Se salta las reglas de la tienda, acopla los dos esquemas y exige credenciales de base de datos. Descartado.
- **Sincronizar en los dos sentidos desde el principio.** Sin dueño claro, provoca conflictos. Descartado hasta que haya un caso que lo exija y un contrato seguro.
- **Copiar el stock externo a `inventory_levels`.** Cada sincronización sería un ajuste del CRM sobre stock que no es suyo, y una bajada de la tienda podría dejar `reserved` por encima de `on_hand`. Descartado: instantáneas aparte (§3).
- **Aplicar diferencias o los datos de un webhook.** Una diferencia perdida o repetida, o un mensaje fuera de orden, deja el stock mal para siempre. Descartado: valores absolutos con versión, y el webhook solo avisa.
- **Ordenar por fecha de origen.** Los relojes de dos sistemas no son comparables. Descartado: versión o secuencia monótona.
- **Fusionar clientes por correo, teléfono o documento.** Une a personas distintas que comparten un dato. Descartado: vinculación explícita.

## Consequences

- No hay conectores todavía. Cuando haya respuestas y contrato, cada entidad entra como un slice pequeño (ADR-009), empezando por una de solo lectura.
- La tienda tendría que añadir una API de integración con versiones por registro, y webhooks firmados si se quieren avisos.
- El inventario LOCAL de la Fase 3 no cambia: `inventory_levels` solo guarda celdas del CRM.
- Un precio de la tienda pasa por una persona que confirma su lote. Es más lento, y deja a `PricingService` como única autoridad.
- Nada de este ADR relaja ADR-001, ADR-002, ADR-003, ADR-006, ADR-013 ni ADR-014.

## Security implications

- Credenciales de máquina con permisos mínimos y revocables, cifradas en reposo.
- Aislamiento por organización con RLS, y auditoría de cada cambio de configuración.
- La organización de un webhook sale del conector, nunca del mensaje: un mensaje no puede escribir en otra organización.
- Un webhook firmado con ventana anti-replay e ids de un solo uso, y tratado como aviso: aunque se falsificara, solo provocaría una lectura más con las credenciales del conector.
- Datos personales solo con base legal, y la cola de errores sin el mensaje completo.

## Operational implications

- Colas, reintentos y una cola de errores visible por organización, con retención configurada.
- Métricas de retraso y de errores por conector, y de las actualizaciones ignoradas por versión vieja.
- Una reconciliación periódica por conector, y alertas cuando una instantánea pasa a desactualizada.
- Un presupuesto de peticiones por conector, acordado con la tienda.

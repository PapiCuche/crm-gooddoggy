# ADR-037: Identidad de contactos por BSUID (clientes de WhatsApp sin número de teléfono)

- **Status:** Proposed
- **Date:** 2026-11-14
- **Deciders:** propuesto por el programa autónomo (ADR-015 §5) para la decisión del propietario. Mientras esté `Proposed` no obliga al código y ningún work item de identidad por BSUID empieza: todos dependen de su aceptación. Las preguntas que necesitan respuesta del propietario están en §12 (D-BSUID-01 a D-BSUID-12).
- **Work item:** F7-14b [#787](https://github.com/PapiCuche/crm-gooddoggy/issues/787) (archiva este ADR). La medición es F7-14 [#749](https://github.com/PapiCuche/crm-gooddoggy/issues/749).
- **Related:** [ADR-002](ADR-002-postgresql-rls.md) (FORCE RLS, `crm_app`), [ADR-005](ADR-005-ai-provider-abstraction.md) y [ADR-034](ADR-034-ai-copilot-first.md) (contexto de la IA), [ADR-010](ADR-010-messaging-policy.md) (política, consentimiento, plantillas), [ADR-011](ADR-011-observability-and-logs.md) (registros), [ADR-029](ADR-029-whatsapp-cloud-api-channel.md) (el canal; §5 y §9), [ADR-032](ADR-032-whatsapp-coexistence.md) (responde a su §12), [ADR-035](ADR-035-instagram-messenger-channels.md) (`contact_identities`, §5), `docs/fase-0/02-modelo-de-datos.md` §E.5, [phase-7.md](../phases/phase-7.md) (F7-14)

## Context

Con los **nombres de usuario de WhatsApp** (despliegue gradual en 2026) un cliente puede escribir a la tienda sin que Meta entregue su número de teléfono. En su lugar llega un **BSUID** (*business-scoped user ID*). El CRM identifica hoy a los clientes de WhatsApp solo por el teléfono, así que esos mensajes se pierden: F7-14 (#749) los distingue (`IGNORED` con `NO_PHONE`) y los cuenta sin guardar su contenido, y ADR-032 §12 dejó la identidad por BSUID para un ADR propio. Este es ese ADR.

### Lo que ya existe y este ADR no cambia

| Pieza | Dónde | Estado |
| --- | --- | --- |
| Contacto del entrante por teléfono E.164 con `pg_advisory_xact_lock`, **sin índice único** (familias que comparten número, números reciclados: `02` §E.5); tope de contactos nuevos por minuto | `inbound.py`, `apps.contacts` | Construido |
| Un entrante sin `from` se cierra `IGNORED` con `NO_PHONE`; registro `meta.webhook.no_phone` con la cuenta, el tipo y el booleano `user_id_seen`, nunca el BSUID; payload vaciado al cerrar | `inbound.receive`, `meta_webhook.parse`, F7-14 | Construido |
| Consentimientos por **contacto**, canal y propósito, con historial inmutable; palabras de baja y alta de la organización | `apps.contacts.consents`, F7-08a/b | Construido |
| Política: ventana anclada en `conversations.last_inbound_at`, baja solo para lo proactivo, `policy_decision` re-evaluada antes de enviar | `policy.py`, ADR-010 | Construido |
| Adaptador de salida: `Claim.recipient` es el teléfono E.164 y el cuerpo usa `to` | `channels.py`, `whatsapp.py` | Construido |
| `contact_identities`: identidades por cuenta de canal, único por (`organization_id`, `channel_account_id`, `external_id`), sin fusión automática; `conversations.contact_identity_id` | ADR-035 §5 (`Proposed`), `02` §E.5 | **No existe** en el código |
| Ecos, historial y libreta de la coexistencia sin teléfono: `IGNORED` con `NO_PHONE` «hasta que exista la identidad por BSUID» | ADR-032 §4, §7, §8 (`Proposed`) | Diseñado |

### Lo que dice Meta

**Fuente:** [S1], leída el **2026-11-14**. Ese día `developers.facebook.com` no respondió desde el entorno de trabajo (tiempo de espera agotado con dos clientes distintos); se leyó la copia de Internet Archive capturada el 2026-10-20 [S1a], cuya cabecera dice «Updated: Sep 15, 2026» y cuyo último cambio registrado es del 15-09-2026. Antes de implementar, quien tome el primer work item vuelve a leer la página en vivo y anota cualquier cambio.

**Confirmado por la fuente** (paráfrasis; las citas, en inglés):

1. El BSUID «uniquely identifies a WhatsApp user and is tied to a specific business». Es **único por par cartera de negocio–usuario** («business portfolio-user pair»): cualquier número de la misma cartera puede escribirle, y desde un número de otra cartera el envío falla.
2. Formato: el código de país ISO 3166 alfa-2, un punto y **hasta 128 caracteres alfanuméricos**. Hay que usarlo entero, sin cambiar nada.
3. Llega en `user_id` en **todos** los webhooks de `messages`, tenga o no nombre de usuario el cliente: en un entrante, `contacts[].user_id` y `messages[].from_user_id`; en un estado, `contacts[].user_id` y `statuses[].recipient_user_id` (en un estado `failed` falta `contacts` y, si se envió al teléfono, también `recipient_user_id`). Meta empezó a enviarlo a principios de **abril de 2026**.
4. Si el cliente activa el nombre de usuario, `from` y `contacts[].wa_id` **se omiten** salvo que, **en los últimos 30 días y para ese mismo número del negocio**, se le haya escrito o llamado, o él haya escrito o llamado; o que esté en la **libreta de contactos** del negocio (*contact book*). Sin nombre de usuario llegan el teléfono **y** el BSUID.
5. El BSUID **se regenera si el cliente cambia de número**. El cambio llega por el campo `messages` como un mensaje de tipo `system`: `user_changed_number` (si se puede compartir el número nuevo) o `user_changed_user_id` (si no), con `user_id` nuevo y `previous_user_id` («this is how you join the new identity to the one you already had on file»); `previous_user_id` puede faltar. No existe un campo de webhook `user_id_update`. Cambiar el nombre de usuario **no** cambia el BSUID.
6. **Envío por BSUID** desde julio de 2026: campo `recipient` (en lugar de `to`); si se mandan los dos, gana `to`. La respuesta trae `contacts[].user_id` si se envió por BSUID. Se puede enviar **cualquier tipo de mensaje salvo las plantillas de autenticación** de un toque, sin toque y de copiar código; error `131062` si el destinatario BSUID no se admite para ese mensaje. Para la API de mensajes de marketing, Meta **recomienda enviar al teléfono** «so you can continue to receive phone numbers in webhooks».
7. **BSUID padre** (`parent_user_id`, formato con `ENT.` tras el país): solo para negocios gestionados con varias carteras, previa inscripción con Meta.
8. **Botón `REQUEST_CONTACT_INFO`** (plantillas de utilidad o marketing, o mensaje interactivo): si el cliente lo pulsa, comparte su número y llega un mensaje `contacts` con `origin = contact_request` y `phones[].wa_id`. Si comparte un contacto por su cuenta, `origin = other` con la vCard (puede ser de otra persona).
9. **Libreta de contactos**: la aloja Meta, por cartera, sin integración; guarda el teléfono y el BSUID de cada interacción posterior a su lanzamiento y los usa para rellenar los webhooks (no las respuestas de la API). Dura hasta que el negocio la desactiva (lo que borra lo guardado) o cierra la cuenta. Hay una API para consultar (`present`, sin devolver el número) o borrar una entrada por BSUID.
10. «Phone numbers and related data are retained for a maximum of 30 days»; un cliente que vuelve después de esa ventana puede parecer un hilo nuevo. Meta pide **unir el BSUID con los identificadores previos** en el CRM cuanto antes.
11. Facturación y analítica: «No changes».

**Supuesto o SIN VERIFICAR** (no se usa como premisa sin una prueba con el número de prueba):

1. **Ventana de 24 h:** la fuente no la menciona. Se supone que un entrante con solo BSUID abre la ventana igual que uno con teléfono (la ventana es por cliente y número del negocio, ADR-029 §9).
2. **Versión de la Graph API:** la fuente usa `<API_VERSION>` y no dice desde qué versión existe `recipient`. El despliegue fija `v23.0` (D-F7-08); hay que comprobarlo contra el simulador y el número de prueba.
3. **Alcance de la libreta:** la fuente dice que es por cartera y, en otra frase, que está «keyed by the per-phone-number BSUID». Se trata el BSUID como de cartera (lo que dice la definición) y no se depende de la libreta.
4. **Otros cambios de BSUID:** `user_changed_user_id` se describe solo junto al cambio de número; no se sabe si hay otros disparadores.
5. **Fechas reales en Perú:** cuándo los clientes de la tienda empiezan a usar nombres de usuario. La medida de F7-14 lo dirá.
6. La copia leída tiene casi dos meses: puede haber cambios posteriores (punto de arriba sobre releerla).

## Decision

### 1. Alcance y principios

1. El BSUID es un **dato personal** que identifica a un cliente ante la tienda. Se trata como el teléfono o más estricto: vive solo en el payload de la ingesta (vaciado al procesar) y en una tabla del tenant; nunca en registros, métricas, auditoría, outbox, errores, reporte de errores, URL, la IA ni la pantalla (§8).
2. **Nada se fusiona solo.** El CRM une un BSUID con un teléfono únicamente cuando **Meta afirma la pareja** dentro de un mismo objeto firmado (§4). El nombre, el nombre de usuario y el teléfono compartido nunca bastan.
3. Mismo patrón que ADR-035 para PSID e IGSID: una identidad por cuenta de canal, sin columnas por plataforma en `contacts`.
4. No cambia el tenant, el enrutado, la firma, la deduplicación ni la política de ADR-010.

### 2. Dónde se guarda: `contact_identities`

- El BSUID se guarda **solo** en `contact_identities` (diseño de `02` §E.5 y ADR-035 §5), con `platform = WHATSAPP`, `channel_account_id` obligatorio y `external_id` = el BSUID. **No** se añade una columna a `contacts`: el BSUID cambia con el número, es por cartera y un contacto puede tener varios (varios números de la tienda, un BSUID anterior).
- La tabla la crea el **primer** work item que la necesite (este o F7b-04), con todo lo que pide ADR-035 §5: FORCE RLS, política de tenant, FKs compuestas a `contacts` y `channel_accounts`, concesiones por columna a `crm_app` como sus hermanas y su entrada en `TENANT_TABLES`. El otro solo añade lo suyo.
- Columnas que añade WhatsApp: `phone_e164` (el teléfono que Meta envió **junto** a ese BSUID la última vez, nulo si nunca llegó), `phone_seen_at`, `username` (dato no confiable, saneado y acotado como el nombre de perfil), `last_seen_at` y `superseded_at` (§5).
- **Único** por (`organization_id`, `channel_account_id`, `external_id`), como ADR-035. CHECK de formato para `WHATSAPP`: `^[A-Z]{2}\.[A-Za-z0-9]{1,128}$`, que además rechaza un BSUID padre. Dos primeros mensajes simultáneos: `INSERT … ON CONFLICT DO NOTHING` y relectura. Orden de bloqueos fijo: primero la identidad y después el teléfono (`pg_advisory_xact_lock` de `inbound.py`), para no crear interbloqueos.
- **Varios números de la misma tienda:** dos cuentas con el mismo `external_parent_id` (WABA) son de la misma cartera, así que el mismo BSUID es la misma persona y se enlaza al mismo contacto. Entre WABA distintos el CRM no sabe si comparten cartera: no se enlaza solo (D-BSUID-10).
- El BSUID padre (`parent_user_id`, `from_parent_user_id`) **no se guarda** (D-BSUID-09).
- `conversations.contact_identity_id` (ADR-035) es **opcional** para `WHATSAPP` y se rellena cuando el contacto tiene identidad en esa cuenta. La regla «a lo más una conversación no cerrada por contacto y cuenta» no cambia.

### 3. Entrante: cómo se encuentra el contacto

Para un mensaje de `messages` enrutado a una cuenta (el tenant sigue saliendo solo de la función de enrutado):

1. **Con BSUID** (`from_user_id`): se busca la identidad vigente de esa cuenta. Si existe, el mensaje es de **su** contacto, llegue o no el teléfono.
2. **Sin identidad y con teléfono** (`from`): el contacto se busca por teléfono como hoy y se le crea la identidad (regla de enlace A, §4).
3. **Sin identidad y sin teléfono:** se crea un contacto **sin teléfono**, con `source = INBOUND_MESSAGE`, el nombre de perfil como hoy (D-F7-07) o, si falta, el nombre de usuario o una frase fija («Cliente de WhatsApp») (D-BSUID-04), y su identidad. El tope de contactos nuevos por minuto se aplica igual.
4. **Sin BSUID y sin teléfono:** sigue `IGNORED` con `NO_PHONE`. Sin BSUID y con teléfono: como hoy.

El BSUID se toma **solo** de `from_user_id` del mismo mensaje, nunca de otra fila de `contacts[]` del lote. Con esto `NO_PHONE` pasa a ser excepcional y la métrica de F7-14 sigue contándolo.

### 4. Cómo se unen un mensaje con número y otro sin él

**Reglas de enlace automático** (todas dentro de un objeto firmado de la misma cuenta; son las únicas):

| Regla | Evidencia de Meta | Qué hace |
| --- | --- | --- |
| A. Pareja en el mismo objeto | Un mismo mensaje trae `from` y `from_user_id` (o un eco `to` y `to_user_id`, un hilo del historial `wa_id` y `user_id`, una entrada de la libreta `phone_number` y `user_id`, ADR-032) | Crea la identidad en el contacto del teléfono o, si ya existe, actualiza `phone_e164` y `phone_seen_at` |
| B. Cambio de número | Mensaje `system` `user_changed_number` o `user_changed_user_id` con `previous_user_id` igual a una identidad vigente de la cuenta | Crea la identidad nueva en el **mismo** contacto y marca la anterior `superseded_at` (§5) |
| C. El cliente comparte su número | Mensaje `contacts` con `origin = contact_request` desde una identidad conocida | Guarda `phones[].wa_id` normalizado en `phone_e164` de la identidad y, si el contacto no tiene teléfono, en el contacto |

**Nunca automático:** el nombre de perfil, el nombre de usuario, una vCard compartida (`origin = other`: puede ser de otra persona), el mismo teléfono en otro contacto, el BSUID padre, el mismo BSUID en otra organización o en otro WABA (§2).

**Conflictos.** Si la identidad ya está en el contacto A y el teléfono del mismo mensaje coincide con otro contacto B:

- El mensaje va a **A** (la identidad que Meta liga a la persona gana sobre un teléfono sin índice único).
- El teléfono se escribe en A **solo si A no tiene uno**; nunca se sobrescribe un teléfono escrito por una persona.
- **No se fusiona** A con B. Se cuenta en una métrica (`identity_conflict`, solo la cuenta y el tipo) y la ficha muestra un aviso de posible duplicado cuando exista la vinculación humana y auditada de D-F7b-05 (D-BSUID-03).

**Por qué hay que empezar pronto.** Mientras el cliente no tenga nombre de usuario, Meta envía el teléfono y el BSUID juntos: cada mensaje enlaza (regla A) a los clientes actuales. Cuando ese cliente adopte un nombre de usuario, su BSUID ya estará en su ficha y su conversación seguirá en el mismo contacto. Cuanto más tarde se active la regla A, más clientes aparecerán como contactos nuevos (D-BSUID-12).

### 5. Cambio de número y retención de la identidad anterior

- Con la regla B, la identidad anterior queda `superseded_at` y ya **no enruta** entrantes ni sirve para enviar; se conserva solo para reconocer eventos atrasados o desordenados.
- `user_changed_number` trae el número nuevo en `wa_id`: se guarda en `phone_e164` de la identidad nueva, pero el teléfono del **contacto** no se cambia solo (podría ser un número compartido o uno que alguien escribió a mano); la ficha muestra «WhatsApp informó un cambio de número» para que una persona lo confirme (D-BSUID-06). El cuerpo `system.body` (contiene los dos números) no se guarda: se vacía con el payload.
- Si `previous_user_id` falta o no coincide con ninguna identidad, el mensaje `system` se ignora con una métrica; el próximo mensaje del cliente entra por §3.

### 6. Salida: `to` o `recipient`

El adaptador elige el destinatario con la identidad de la conversación:

1. Identidad con `phone_e164` → `to` = ese teléfono, sin `recipient` (lo que Meta recomienda para seguir recibiendo el teléfono en los webhooks).
2. Identidad sin teléfono → `recipient` = el BSUID, sin `to`.
3. Sin identidad (contactos de antes) → `to` = el teléfono del contacto, como hoy.

Nunca se envían los dos campos (ganaría `to` y ocultaría un teléfono viejo). `Claim` se generaliza para llevar uno de los dos, como pide ADR-035 §6 para PSID e IGSID; el BSUID se lee de la base en el momento de enviar y no se copia al outbox. El error `131062` se normaliza a un código propio (`RECIPIENT_NOT_SUPPORTED`) con un mensaje en español; ningún reintento.

### 7. Consentimiento, baja, plantillas y ventana

- **Consentimiento y baja.** `contact_consents` está atado al **contacto** y al canal, no al teléfono: un cliente sin número tiene sus consentimientos igual. Una palabra de baja (`STOP`, `BAJA`, `CANCELAR`, las de la organización) de un cliente sin número revoca como hoy; la confirmación fija sale por `recipient` dentro de la ventana. Una baja del contacto vale para todas sus identidades. Si una persona vincula dos contactos más adelante (D-F7b-05), la revocación más reciente prevalece y el historial de los dos se conserva.
- **Ventana de 24 h.** No cambia: se ancla en `last_inbound_at` de la conversación, venga el entrante con teléfono o sin él (supuesto 1 de Meta). La política se re-evalúa justo antes de enviar.
- **Plantillas.** Fuera de la ventana, las reglas de ADR-010 §2 no cambian: plantilla aprobada, consentimiento para lo que lo exige. Las plantillas de **autenticación** de un toque, sin toque y de copiar código no se pueden enviar a un BSUID: la política devuelve `BLOCKED` (`TEMPLATE_NEEDS_PHONE`) para un contacto sin teléfono en vez de dejar que Meta falle. El CRM no envía plantillas de autenticación hoy; la regla queda para cuando las haya.
- **Pedir el número.** El botón `REQUEST_CONTACT_INFO` se envía solo como **mensaje interactivo** dentro de la ventana y solo por una persona (nunca la IA ni una automatización): el cliente decide si lo comparte (regla C). Añadirlo a plantillas queda fuera (D-BSUID-07).
- **Preferencias de Meta** (`user_preferences`, la baja de marketing desde la app de WhatsApp): hoy no se procesan; cuando se procesen, se resolverán por la identidad, nunca por el teléfono solo.

### 8. Privacidad

- **Registros, métricas y alertas:** solo ids internos, tipos, estados, códigos y booleanos (como `user_id_seen` de F7-14). Ni el BSUID, ni un hash suyo (un hash estable también permite enlazar), ni el nombre de usuario.
- **Auditoría:** crear, sustituir o enlazar una identidad se audita con el id interno de la identidad y del contacto; el BSUID no entra en `changes`.
- **Outbox y eventos en tiempo real:** ids internos; el adaptador lee el destinatario de la base.
- **IA (ADR-005, ADR-034):** el constructor de contexto y las tools no leen `contact_identities.external_id`; ningún prompt, resumen ni sugerencia lo contiene. El nombre de usuario, si se usa, es texto no confiable del cliente.
- **API y pantalla:** ningún serializador devuelve el BSUID; la ficha dice «Cliente de WhatsApp sin número» y muestra el nombre de usuario aislado con `<bdi>` y marcado como tal. No se indexa en la búsqueda (`search_documents`) ni en exportaciones.
- **Otra organización:** imposible por diseño: la identidad es por cuenta de canal, el tenant sale de la función de enrutado y RLS con FORCE filtra la tabla. Ninguna consulta busca un BSUID fuera de la organización del contexto.
- **Repositorio público:** las pruebas y el simulador usan BSUID sintéticos con un país ficticio; nunca un id, número ni nombre real.

### 9. Retención

- Una identidad vigente vive mientras vive su contacto; se borra con él (o se anonimiza cuando exista el borrado a petición del cliente, D-F7b-09).
- Una identidad `superseded_at` se borra a los **90 días** (como la ingesta, D-F7-06) con una tarea periódica con `crm_app` (D-BSUID-11).
- El payload de la ingesta, con el BSUID, se vacía al procesar y la fila se purga a los 90 días, como hoy.
- La **libreta de contactos de Meta** está fuera del CRM: la aloja Meta y dura hasta que el negocio la desactiva. Recomendación: mantenerla activa (más clientes llegan con teléfono) y mencionarla en el aviso de privacidad (D-BSUID-08).

### 10. Plan de la serie (después de la aceptación; los issues se crean entonces)

| Paso | Qué | Depende de |
| --- | --- | --- |
| F7-14c | Decisión del propietario: aceptar este ADR y responder §12 | F7-14b |
| F7-14d | `contact_identities` (o su parte de WhatsApp), regla A en el entrante, simulador con BSUID | F7-14c |
| F7-14e | Entrante sin teléfono: contacto sin número y conversación (§3) | F7-14d |
| F7-14f | Salida por `recipient`, `131062`, `TEMPLATE_NEEDS_PHONE` en la política, confirmación de baja | F7-14e |
| F7-14g | Mensajes `system` de cambio de número (regla B, §5) y purga de identidades sustituidas | F7-14d |
| F7-14h | Botón `REQUEST_CONTACT_INFO` interactivo y regla C (opcional, D-BSUID-07) | F7-14f |

Los ecos, el historial y la libreta de la coexistencia (ADR-032 §4, §7, §8) usan la identidad cuando exista, en sus propios work items: dejan de ignorar lo que llega sin teléfono.

### 11. Fuera de este ADR

- Vincular a mano dos contactos y las sugerencias de duplicados (D-F7b-05, después de R3).
- El BSUID padre y varias carteras.
- Llamadas, grupos y nombres de usuario del negocio.
- El borrado a petición del cliente (D-F7b-09).

### 12. Decisiones que necesitan al propietario

| ID | Pregunta | Recomendación | Bloquea |
| --- | --- | --- | --- |
| D-BSUID-01 | ¿Dónde se guarda el BSUID? | Solo en `contact_identities` por cuenta de canal (§2), como PSID e IGSID; nunca una columna de `contacts` | F7-14d |
| D-BSUID-02 | ¿Cuándo se une un mensaje sin número con un contacto que tiene número? | Solo con las reglas A, B y C (Meta afirma la pareja); nunca por nombre, nombre de usuario ni vCard | F7-14d |
| D-BSUID-03 | Si la identidad y el teléfono apuntan a contactos distintos | El mensaje va al contacto de la identidad; sin fusión; métrica ahora y aviso de duplicado con la vinculación humana de D-F7b-05 | F7-14d |
| D-BSUID-04 | Nombre de un contacto nuevo sin número | Nombre de perfil; si falta, el nombre de usuario; si falta, «Cliente de WhatsApp» | F7-14e |
| D-BSUID-05 | ¿Con qué se envía? | Al teléfono emparejado si lo hay; si no, al BSUID; nunca los dos | F7-14f |
| D-BSUID-06 | Cambio de número que informa WhatsApp | La identidad se actualiza sola; el teléfono del contacto lo confirma una persona | F7-14g |
| D-BSUID-07 | ¿Pedir el número con el botón de Meta? | Sí, como mensaje interactivo dentro de la ventana y solo por una persona; en plantillas, no por ahora | F7-14h |
| D-BSUID-08 | ¿Mantener activa la libreta de contactos de Meta? | Sí, y decirlo en el aviso de privacidad | Activación |
| D-BSUID-09 | ¿Inscribirse en el BSUID padre? | No: la tienda tiene una cartera; `parent_user_id` no se guarda | — |
| D-BSUID-10 | Mismo BSUID en dos números de la tienda | Mismo contacto si comparten WABA; entre WABA distintos, nada automático | F7-14d |
| D-BSUID-11 | Retención de las identidades sustituidas | 90 días; las vigentes, con su contacto | F7-14g |
| D-BSUID-12 | ¿Cuándo? | F7-14d (la regla A) antes de conectar el número de la tienda (F7-12 con el número real o F7-13k): así los clientes actuales quedan enlazados antes de adoptar un nombre de usuario | Calendario de R1 |

## Alternatives considered

| Alternativa | Por qué se descarta |
| --- | --- |
| Columna `contacts.whatsapp_bsuid` | Es por cartera, cambia con el número y un contacto puede tener varias; `02` §E.5 y ADR-035 ya diseñaron `contact_identities` |
| Índice único global del BSUID, o por organización sin la cuenta | Es por cartera: uno global mezclaría organizaciones; sin la cuenta no se sabe la cartera |
| Seguir ignorando los mensajes sin número | Es justo lo que Meta dice que no hay que hacer: se pierden clientes en silencio |
| Fusionar contactos al ver la pareja teléfono–BSUID | Mezcla datos de dos personas si el teléfono es compartido o reciclado; fusionar es humano (D-F7b-05) |
| Unir por nombre de usuario o nombre de perfil | Los escribe el cliente y cambian; no identifican |
| Enviar siempre `to` y `recipient` | Gana `to`: un teléfono viejo taparía al BSUID vigente |
| Enviar siempre por BSUID | Meta recomienda el teléfono para seguir recibiéndolo en los webhooks |
| Guardar un hash del BSUID en los registros | Un hash estable también enlaza a la persona entre registros |
| Depender de la libreta de Meta para tener el teléfono | La aloja Meta, se puede desactivar y su alcance no está claro (supuesto 3) |

## Consequences

- Los clientes con nombre de usuario llegan al Inbox; `NO_PHONE` queda para el caso sin BSUID.
- Habrá contactos de WhatsApp **sin teléfono**: la ficha, la búsqueda y la lista ya funcionan sin él (lo mismo pide ADR-035).
- Un cliente puede quedar en dos contactos (uno por teléfono, otro por BSUID) si su primer mensaje con nombre de usuario llega antes de que la regla A lo enlazara; se ve en la métrica y lo une una persona.
- `contact_identities` sirve a WhatsApp, Instagram y Messenger con un solo diseño.
- El adaptador y `Claim` aceptan un destinatario que no es un teléfono; la política conoce un motivo nuevo (`TEMPLATE_NEEDS_PHONE`) y el adaptador un error nuevo (`131062`).

## Security implications

- Nuevo identificador personal con las reglas del teléfono y más: fuera de registros, métricas, auditoría, outbox, IA, API y búsqueda.
- El tenant nunca sale del payload; la identidad es por cuenta y la tabla tiene FORCE RLS: un BSUID firmado para otra organización no encuentra nada.
- Un webhook firmado puede traer un BSUID falso solo si se filtró el app secret (ADR-029 §10); aun así, la regla A exige teléfono y BSUID en el mismo objeto y nunca fusiona, así que el daño queda en un contacto nuevo o un aviso de duplicado.
- La IA no ve el BSUID ni puede pedir el número al cliente: el botón lo envía una persona.

## Operational implications

- Sin variables nuevas. El simulador de Meta aprende los mensajes con BSUID, sin teléfono, de cambio de número, `contacts` con `origin` y el error `131062`.
- Métricas nuevas, sin datos personales: identidades creadas por regla, conflictos, mensajes `system` sin identidad previa, envíos por `recipient` y `131062`. `NO_PHONE` sigue contándose como en F7-14.
- La consulta de conteo de F7-14 ([phase-7.md](../phases/phase-7.md)) se hace con `crm_app` o con los registros, nunca con `crm_migrator` (ADR-002 §1).
- El runbook de activación pide releer [S1] en vivo antes de conectar el número de la tienda.

## Fuentes

- [S1] Business-scoped user IDs (Meta for Developers), leída el 2026-11-14: https://developers.facebook.com/documentation/business-messaging/whatsapp/business-scoped-user-ids
- [S1a] La copia de [S1] que se leyó ese día, porque el sitio no respondió (captura de Internet Archive del 2026-10-20; la página dice «Updated: Sep 15, 2026»): https://web.archive.org/web/20261020235944/https://developers.facebook.com/documentation/business-messaging/whatsapp/business-scoped-user-ids

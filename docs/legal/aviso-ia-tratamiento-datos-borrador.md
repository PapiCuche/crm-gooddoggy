# Aviso de uso de IA y tratamiento de datos: borrador técnico (D-F8-04)

- **Estado:** borrador para que el propietario lo revise con su asesoría legal. **No está publicado** y no debe publicarse tal cual.
- **Work item:** #1049 (F8-02b). Decisión del propietario del 2026-11-21 en #771; [ADR-034](../adr/ADR-034-ai-copilot-first.md) §13 (D-F8-04 y D-F8-05), [ADR-011](../adr/ADR-011-observability-and-logs.md).
- **Comprobado contra el código** de `main` en `1a420a9d` (2026-11-21). Si el código cambia (resumen, inserción, purga de borradores, panel, otro proveedor), hay que revisar la Parte 2 y el texto antes de publicar.

> **Esto no es asesoría legal.** Es un borrador técnico que describe con exactitud lo que hace hoy el software. Si el texto es suficiente para cumplir la Ley N.º 29733, su reglamento y cualquier otra norma aplicable, lo decide la asesoría legal del propietario. Ningún dato real de la empresa, de clientes, de cuentas ni de dominios aparece aquí: el repositorio es público. Cada dato de la empresa es un marcador «[ASÍ]» que el propietario completa fuera del repositorio.

## Cómo se usa este documento

1. **Parte 1:** el texto para los clientes. Es lo único que se publica, una vez revisado y con los marcadores completos.
2. **Parte 2:** tabla interna. Cada afirmación de la Parte 1 y el módulo de `main` que la sostiene. **No se publica.**
3. **Parte 3:** lo que el propietario y su asesoría deben confirmar antes de publicar.
4. **Parte 4:** límites que el texto no puede prometer.

Los textos entre «[CONFIRMAR: …]» son frases que dependen de un dato o de una decisión externa al código. Si no se confirman, se quitan antes de publicar.

---

## Parte 1. Texto del aviso (borrador para clientes)

### Aviso sobre el uso de inteligencia artificial en la atención de tus mensajes

Versión «[VERSIÓN]». Vigente desde «[FECHA DE PUBLICACIÓN]».

#### 1. Quién es responsable de tus datos

«[RAZÓN SOCIAL]», con RUC «[RUC]» y domicilio en «[DOMICILIO]» (en adelante, «nosotros»), es responsable del tratamiento de los datos personales que nos envías cuando nos escribes por «[CANALES DE MENSAJERÍA: p. ej., WhatsApp, Messenger, Instagram]». «[CONFIRMAR: nombre y código de inscripción del banco de datos personales, si corresponde]».

Este aviso completa nuestra «[POLÍTICA DE PRIVACIDAD GENERAL: enlace]» y explica solo el uso de un asistente de inteligencia artificial (IA) en la atención de mensajes.

#### 2. Cómo usamos la IA

Una persona de nuestro equipo puede pedir a un asistente de IA que le proponga un **borrador de respuesta** a tu último mensaje o un **resumen** de la conversación. El asistente solo se usa cuando esa persona lo pide para una conversación concreta.

- **La IA no te escribe por sí sola.** No envía mensajes. Toda respuesta que recibes la revisa, la corrige si hace falta y la envía una persona de nuestro equipo.
- **La IA no decide nada sobre ti.** No aprueba ni rechaza pedidos, no cambia precios, stock ni cotizaciones, no modifica tus datos y no asigna ni cierra conversaciones.
- **Precios y disponibilidad.** Para hablar de productos, el asistente solo puede consultar nuestro catálogo publicado, los precios de las listas que hemos autorizado y la disponibilidad en franjas («disponible», «pocas unidades», «agotado»). Antes de mostrar el borrador, nuestro sistema busca las cifras, plazos, condiciones comerciales y enlaces que no salen de esas consultas y los reemplaza por «[verificar]»; no permite enviar un mensaje que conserve esa marca, y la persona que te atiende escribe el dato correcto.
- **Si pides hablar con una persona**, el asistente se lo señala a quien te atiende y solo propone un mensaje de paso a una persona.

#### 3. Para qué

Para ayudar a nuestro equipo a responderte con más rapidez y exactitud sobre productos, variantes, precios publicados y disponibilidad, y para que quien retoma una conversación entienda el contexto sin pedirte que repitas todo.

No usamos el asistente para publicidad ni para crear perfiles tuyos, y no guarda una memoria de tus conversaciones entre una consulta y otra.

#### 4. Qué datos compartimos con el proveedor de IA

El asistente funciona con un servicio de «[NOMBRE DEL PROVEEDOR DE IA]». Cada vez que una persona de nuestro equipo lo usa, le enviamos solo esto:

- **Los últimos mensajes de la conversación, hasta 20**, y solo los que tú pudiste ver: los que nos escribiste y las respuestas que ya te enviamos. De cada uno, si es tuyo o nuestro, la hora y el texto (un mensaje muy largo se recorta).
- **Antes de enviarlos, los enmascaramos automáticamente:** las direcciones de correo se reemplazan por «[correo]»; las secuencias de 7 o más dígitos (por ejemplo, teléfonos, documentos o tarjetas) por «[número]»; y tu nombre, tal como figura en nuestro registro de contacto, por «[nombre]».
- **Fotos, audios, videos y documentos no se envían:** el asistente solo recibe una marca como «[imagen]» o «[audio]».
- **Información de nuestro catálogo, cuando el asistente la consulta:** nombres y características de productos y variantes, precios publicados de las listas autorizadas y la franja de disponibilidad.

El enmascarado es automático y reduce los datos que salen, pero **no es una garantía**: si escribes otros datos personales en el chat (por ejemplo, una dirección o un número escrito en letras), pueden llegar al proveedor. Te recomendamos no enviar por chat datos que no sean necesarios para tu consulta.

#### 5. Qué datos no compartimos con el proveedor de IA

- Las notas internas de nuestro equipo.
- Los datos de tu registro de contacto (nombre, teléfono, correo, dirección) como tales, ni identificadores tuyos o de tu conversación.
- Mensajes que todavía no te enviamos o cuyo envío falló.
- Datos de otros clientes.
- El contenido de fotos, audios, videos y documentos.
- Costos, márgenes, listas de precios internas, códigos internos de producto y datos de almacenes.
- Datos de cotizaciones, oportunidades de venta y asignaciones, y los datos de registro de nuestro personal.

#### 6. Transferencia fuera del Perú

«[NOMBRE DEL PROVEEDOR DE IA]» presta su servicio desde «[PAÍS DEL PROVEEDOR]». Por eso, los datos descritos en el punto 4 se transfieren fuera del Perú cada vez que se usa el asistente. Esta transferencia se realiza «[BASE Y GARANTÍAS DE LA TRANSFERENCIA: a definir por la asesoría legal]».

«[CONFIRMAR con los términos de la cuenta del proveedor: el proveedor no usa este contenido para entrenar sus modelos y lo conserva durante «[PLAZO DE CONSERVACIÓN DEL PROVEEDOR]»]».

#### 7. Cuánto tiempo conservamos los datos

En nuestros sistemas:

- **El borrador o el resumen** que genera el asistente: su texto se borra a los **30 días**. Después queda solo el registro sin texto.
- **El registro técnico de cada uso** (quién de nuestro equipo lo pidió, en qué conversación —como identificador interno—, cuándo, qué consultas al catálogo hizo, el consumo y el costo, y el resultado), **sin el contenido de tus mensajes ni del borrador**: **12 meses**; después se elimina.
- **Los registros de auditoría** (quién pidió o usó un borrador y cuándo, solo con identificadores): «[PLAZO DE CONSERVACIÓN DE LA AUDITORÍA]».
- **La conversación** (tus mensajes y nuestras respuestas) se conserva según nuestra «[POLÍTICA DE PRIVACIDAD GENERAL]». Este aviso no la cambia.

Lo que conserva el proveedor de IA se rige por el punto 6.

#### 8. Tus derechos y cómo ejercerlos

De acuerdo con la Ley N.º 29733, Ley de Protección de Datos Personales, y «[REGLAMENTO VIGENTE: confirmar el decreto supremo]», puedes ejercer tus derechos de **información, acceso, rectificación (actualización o inclusión), cancelación (supresión) y oposición**. Si el tratamiento se basa en tu consentimiento, puedes revocarlo. La IA no toma decisiones automatizadas sobre ti: una persona revisa todo lo que te enviamos.

«[CONFIRMAR PROCEDIMIENTO: si no quieres que usemos el asistente de IA en tus conversaciones, puedes pedírnoslo por los canales de abajo]».

Para ejercer tus derechos, escríbenos a «[CORREO DE CONTACTO]» o «[OTRO CANAL DE CONTACTO]», indicando «[REQUISITOS DE LA SOLICITUD: p. ej., nombre, documento de identidad, derecho que ejerces y descripción]». Te responderemos en «[PLAZOS LEGALES DE RESPUESTA]».

Si consideras que no atendimos tu solicitud, puedes acudir a la Autoridad Nacional de Protección de Datos Personales del Ministerio de Justicia y Derechos Humanos.

#### 9. Seguridad

La clave de acceso al proveedor se guarda cifrada y solo la administra quien tiene ese permiso, con un segundo factor de verificación. Solo las personas de nuestro equipo autorizadas pueden usar el asistente, y solo en las conversaciones que ya pueden ver. Podemos apagar el asistente en cualquier momento, sin afectar la atención por mensajes.

#### 10. Cambios a este aviso

Si cambia el uso de la IA (por ejemplo, otro proveedor u otros datos), actualizaremos este aviso y su versión antes de aplicar el cambio.

---

## Parte 2. Tabla interna: cada afirmación y el código que la sostiene (no publicar)

Todas las rutas de archivo son de `backend/`. «Verificado» significa que el comportamiento está en `main` (`1a420a9d`); «Pendiente» significa que el texto describe un comportamiento decidido que todavía no está activo y que **no debe publicarse** hasta que lo esté.

| #   | Afirmación (punto del aviso)                                                 | Dónde está en `main`                                                                                                                                                                                                                                                     | Estado                                                                                                                                                                 |
| --- | ---------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1   | Solo se usa cuando una persona lo pide para una conversación (2)             | `apps/ai_agents/api/views.py` (`SuggestView`: `conversations.view` y `ai.use`, alcance de la conversación, mismo 404); `apps/ai_agents/copilot.py`; `apps/ai_agents/records.py` (`save` exige `ActorType.USER`)                                                          | Verificado. Solo «sugerir»: la ruta de **resumen** es #1025 (abierto)                                                                                                  |
| 2   | La IA no envía mensajes (2)                                                  | Ningún módulo de `apps.ai_agents` importa los comandos del Inbox (contrato de import-linter en `pyproject.toml`, ADR-034 §2, §11); `records.py` no escribe en el outbox; `tests/test_ai_copilot_runtime.py` (`test_a_full_run_writes_only_ai_records_…`)                 | Verificado. El panel del Inbox (#772) y la ruta de inserción (#1026) aún no existen: hoy solo la API devuelve el borrador                                              |
| 3   | La IA no decide ni cambia datos (2)                                          | `apps/ai_agents/registry.py` (solo `Risk.READ` y `Audience.PUBLIC_SAFE`); tools en transacción de solo lectura (`tests/test_ai_tools_read_only.py`); `attention_mode` sin cambios (ADR-034 §12)                                                                          | Verificado                                                                                                                                                             |
| 4   | Catálogo publicado, listas autorizadas, stock en franjas (2, 4)              | `apps/ai_agents/tools/catalog.py` (`ACTIVE` y `ai_visible`; sin SKU, costos ni descripción larga); `tools/price.py` (`PUBLIC_AI`, `ai_exposable` y listas que la persona ve; nunca costos, código de lista ni SKU); `tools/stock.py` (franja; el número solo en `facts`) | Verificado. Ninguna lista está marcada por omisión (`pricing/0008`): sin marcar, no hay precios                                                                        |
| 5   | Cifras, plazos, términos y enlaces sin evidencia → «[verificar]» (2)         | `apps/ai_agents/guard.py`, `apps/ai_agents/detectors.py` (sustitución en el servidor)                                                                                                                                                                                    | Verificado                                                                                                                                                             |
| 6   | No se puede enviar un mensaje con la marca (2)                               | `core/text.py` (`VERIFY_MARK`, `has_verify_mark`); `apps/conversations/api/messages.py` y `media.py` (400 `UNVERIFIED_CONTENT`)                                                                                                                                          | Verificado                                                                                                                                                             |
| 7   | Pedir una persona → aviso y mensaje de paso (2)                              | `apps/ai_agents/detectors.py` (`handoff`, `handoff_reply`); `config/settings/base.py` (`AI_HANDOFF_REPLY`)                                                                                                                                                               | Verificado. El texto vigente en `main` es el provisional; el aprobado por el propietario (#771, 2026-11-21) y su configuración desde administración son otro work item |
| 8   | Sin perfiles ni memoria entre consultas (3)                                  | `apps/ai_agents/context.py` (`build` arma cada petición desde cero; sin tablas de memoria; ADR-034 §1 «Fuera»)                                                                                                                                                           | Verificado                                                                                                                                                             |
| 9   | Hasta 20 mensajes, solo los que el cliente vio, con rol, hora y texto (4)    | `context.py` (`AI_COPILOT_CONTEXT_MESSAGES = 20` en `config/settings/base.py`; `DELIVERED`; `_block` con `rol`, `hora`, `texto`; `MESSAGE_CHARS = 1500`, `CONTEXT_BYTES`)                                                                                                | Verificado (`tests/test_ai_copilot_context.py`)                                                                                                                        |
| 10  | Enmascarado de correos, 7+ dígitos y el nombre del contacto (4)              | `context.py` (`EMAIL`, `DIGITS`, `pattern`, `_names`, `mask`)                                                                                                                                                                                                            | Verificado. Límites en la Parte 4                                                                                                                                      |
| 11  | Medios solo como marca (4, 5)                                                | `context.py` (`MARKS`, `UNSUPPORTED`, `_shown`)                                                                                                                                                                                                                          | Verificado                                                                                                                                                             |
| 12  | Datos del catálogo que salen (4)                                             | Las salidas declaradas de cada tool (`ROW`, `VARIANT`, `PRICE`, `TERMS` y `availability`)                                                                                                                                                                                | Verificado                                                                                                                                                             |
| 13  | Nunca notas internas (5)                                                     | `apps/conversations/selectors.py` (`messages(…, notes=False)` excluye `INTERNAL`); `context._recent`                                                                                                                                                                     | Verificado (`test_only_what_the_customer_could_see_leaves_…`)                                                                                                          |
| 14  | Ni registro de contacto ni identificadores del cliente o la conversación (5) | `context.py` (el nombre solo se lee para taparlo); `apps/ai_gateway/anthropic.py` (`encode`: `model`, `max_tokens`, `output_config`, `system`, `messages`, `tools`; sin `metadata`)                                                                                      | Verificado (la prueba de contexto busca el id de la conversación y del usuario en el cuerpo enviado)                                                                   |
| 15  | Ni mensajes no enviados o fallidos (5)                                       | `context.py` (`DELIVERED = ("SENT", "DELIVERED", "READ")`)                                                                                                                                                                                                               | Verificado                                                                                                                                                             |
| 16  | Ni datos de otros clientes (5)                                               | Ninguna tool busca contactos (`apps/ai_agents/tools/`); RLS y `tenant_scope` en `context.build`                                                                                                                                                                          | Verificado                                                                                                                                                             |
| 17  | Ni costos, márgenes, listas internas, SKU, almacenes (5)                     | `tools/price.py`, `tools/stock.py`, `tools/catalog.py`; `pricing/0009` (una lista interna nunca es `ai_exposable`)                                                                                                                                                       | Verificado                                                                                                                                                             |
| 18  | Ni cotizaciones, oportunidades, asignaciones ni datos del personal (5)       | `context.py` solo lee mensajes y el nombre del contacto; contratos de import-linter                                                                                                                                                                                      | Verificado para los **campos**. El texto de un mensaje enviado por el personal sí sale (con el mismo enmascarado): si alguien escribe su nombre en el mensaje, llega   |
| 19  | Un solo proveedor, un host fijo (4, 6)                                       | `config/settings/base.py` (`AI_PROVIDER_HOSTS`: solo `ANTHROPIC`); `apps/ai_gateway/models.py` (`Provider`); `apps/ai_gateway/anthropic.py` (`/v1/messages`)                                                                                                             | Verificado. OpenAI (F8-10) exigiría una nueva versión del aviso                                                                                                        |
| 20  | Entrenamiento y plazo del proveedor (6)                                      | Nada en el código: depende de los términos de la cuenta (ADR-034 §3, «Retención del proveedor»; solo modelos de `AI_MODELS`, hoy sin «Covered Models»)                                                                                                                   | **No verificable en el código.** Lo confirma el propietario                                                                                                            |
| 21  | Texto del borrador borrado a los 30 días (7)                                 | `apps/ai_agents/models.py` y `migrations/0001_copilot_records.py` (`text` solo puede pasar a NULL); la purga es F8-08j (#1026)                                                                                                                                           | **Pendiente de activar:** #1026 sigue abierto; hoy el texto **no** se borra                                                                                            |
| 22  | Registro técnico sin contenido (7)                                           | `ai_llm_calls` (`ai_gateway/migrations/0002_budget_and_calls.py`: tokens, costo, latencia, resultado, `request_id`); `ai_runs` y `ai_actions` (`ai_agents/migrations/0001`; `records.redacted`: un UUID o la longitud de un texto)                                       | Verificado                                                                                                                                                             |
| 23  | Registro técnico eliminado a los 12 meses (7)                                | `ai_agents/migrations/0002_records_retention.py` (`ai_records_drop_partitions`); `ai_agents/apps.py` (`drop_old_partitions`)                                                                                                                                             | **Pendiente de activar:** `AI_RECORDS_RETENTION = False` hasta que el propietario lo confirme (#1033); aun encendida, no quitaría nada antes de diciembre de 2027      |
| 24  | Auditoría solo con identificadores (7)                                       | `records.save` (`ai.suggestion.created` / `ai.summary.created` con ids); `apps/ai_gateway/switch.py` (encender y apagar)                                                                                                                                                 | Verificado. El plazo de la auditoría es el general (ADR-011: dos años o más, configurable), no uno de la IA                                                            |
| 25  | Clave cifrada, permiso y segundo factor (9)                                  | `apps/ai_gateway/keys.py`, `models.py` (`ai.provider_key` con `core.sealed`); `apps/ai_gateway/api/` (`ai.manage` y `step_up`)                                                                                                                                           | Verificado                                                                                                                                                             |
| 26  | Solo personas autorizadas, en conversaciones que ya ven (9)                  | `ai.use` (solo el Owner al nacer, D-F8-08) y `conversations.view` con su alcance (`SuggestView`, `copilot.py`)                                                                                                                                                           | Verificado                                                                                                                                                             |
| 27  | Se puede apagar sin afectar los mensajes (9)                                 | `apps/ai_gateway/switch.py` (`turn_off` sin segundo factor; `turn_on` exige `notice_confirmed`); `AI_ENABLED` apagado por omisión; `gateway.py` comprueba ambos antes de llamar                                                                                          | Verificado. Hoy la IA está apagada en todas partes por omisión                                                                                                         |
| 28  | Oposición al uso de la IA en sus conversaciones (8)                          | **No hay** un bloqueo por contacto o conversación en el código                                                                                                                                                                                                           | **No implementado.** Hoy solo sería un procedimiento manual del equipo (o apagar la IA). Si se quiere ofrecer, hace falta un work item                                 |
| 29  | Cancelación de los datos tratados por la IA (8)                              | `ai_runs`, `ai_actions` y `ai_llm_calls` son de solo inserción para `crm_app` (sin contenido); `ai_suggestions.text` solo pasa a NULL                                                                                                                                    | **Sin herramienta en la aplicación** para borrar a pedido. El procedimiento (y si basta con que los registros no tengan contenido) lo define la asesoría               |

---

## Parte 3. Lista de verificación antes de publicar

**El propietario:**

- [ ] Completar los datos de la empresa: razón social, RUC, domicilio y, si corresponde, el banco de datos y su inscripción. Nunca en el repositorio, un issue ni un PR.
- [ ] Confirmar el proveedor y su país: hoy el código solo conoce un proveedor (`ANTHROPIC`, host `api.anthropic.com`). Poner el nombre legal y el país de la entidad con la que se contrata, según el contrato de la cuenta.
- [ ] Confirmar en los términos de esa cuenta: uso del contenido para entrenar, plazo de conservación del proveedor y su papel (encargado del tratamiento o equivalente). Si no se puede confirmar, quitar la frase «[CONFIRMAR: …]» del punto 6.
- [ ] Confirmar la retención de D-F8-05 en producción: encender `AI_RECORDS_RETENTION` (un PR, #1033) y esperar a que la purga de 30 días de los borradores esté mergeada y activa (#1026). **Hasta entonces, los puntos 7 de 30 días y 12 meses no son ciertos** y el texto debe decir lo que pasa de verdad.
- [ ] Definir el canal de contacto para los derechos (correo y otro canal) y quién lo atiende.
- [ ] Decidir dónde se publica y cómo se avisa: página web, perfil de WhatsApp Business, respuesta automática o mensaje fijo en cada canal, la política de privacidad general. Lo que se decida debe poder enlazarse desde los canales en que se usa la IA.
- [ ] Decidir si se ofrece la oposición al uso de la IA por conversación (fila 28). Si sí, pedir el work item antes de publicar esa frase; si no, quitarla o sustituirla por el procedimiento real.
- [ ] Revisar esta Parte 2 si cambió algo en el código desde `1a420a9d` (resumen #1025, inserción #1026, panel #772, otro proveedor).
- [ ] Al encender la IA: el CRM pide confirmar que el aviso está publicado (`notice_confirmed`, auditado). Confirmarlo solo después de publicarlo.

**La asesoría legal:**

- [ ] La base de legitimación del tratamiento con IA (consentimiento u otra que admita la ley) y si hace falta recabar el consentimiento en el canal antes de usar el asistente.
- [ ] El reglamento vigente de la Ley N.º 29733 y su cita exacta (verificar si rige el aprobado por el D.S. N.º 016-2024-JUS, que sustituyó al D.S. N.º 003-2013-JUS).
- [ ] El flujo transfronterizo: requisitos, garantías y si debe comunicarse o registrarse ante la Autoridad Nacional de Protección de Datos Personales.
- [ ] Las obligaciones de inscripción del banco de datos personales y si este uso las cambia.
- [ ] Los plazos legales de respuesta a cada derecho y los requisitos de la solicitud.
- [ ] Si el texto debe incluir algo más que la ley exija (por ejemplo, destinatarios, consecuencias de no dar los datos, tiempo de conservación de la conversación).
- [ ] Si también deben informarse otros flujos que este aviso no cubre (por ejemplo, el alojamiento del propio CRM o los proveedores de mensajería).

---

## Parte 4. Lo que el aviso no puede prometer (límites conocidos del código)

- **El enmascarado es una reducción, no una garantía** (ADR-034 §7). No tapa: direcciones, números de menos de 7 dígitos o separados por más de un signo, números escritos en letras, correos escritos como «juan arroba …», el nombre de otra persona, ni un nombre del contacto de menos de 3 letras. Además tapa de más: fechas como `2026-11-20` y palabras comunes que coincidan con el nombre del contacto («Rosa», «Luna»).
- **Los detectores del borrador son deterministas** (`detectors.py`): buscan formas conocidas de cifras, plazos, términos comerciales y enlaces, y una redacción imprevista puede escaparse. Por eso una persona revisa siempre el borrador antes de enviarlo.
- **Solo se tapa el nombre registrado del contacto.** Si el cliente firma con otro nombre o el registro no lo tiene, el nombre sale.
- **El texto del personal sale** con el mismo enmascarado: si alguien escribe su nombre, un teléfono corto o una dirección en una respuesta, llega al proveedor.
- **Los borradores guardan texto generado** a partir de la conversación ya enmascarada; hasta que exista la purga (#1026), ese texto se conserva sin plazo.
- **El resumen** usa el mismo contexto que la sugerencia, pero su ruta aún no existe (#1025). Si se publica antes, la frase del punto 2 sigue siendo correcta (describe un uso posible), pero no hay forma de pedirlo todavía.

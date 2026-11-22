# TikTok Business Messaging: elegibilidad, vía de acceso y proveedores

- **Work items:** F7b-07 ([#780](https://github.com/PapiCuche/crm-gooddoggy/issues/780), primera investigación, fuentes del 2026-11-15) y F7b-08 ([#863](https://github.com/PapiCuche/crm-gooddoggy/issues/863), vía de acceso, DSPR y proveedores, fuentes del 2026-11-16). Fase 7b de [phase-7.md](../phases/phase-7.md) (E19-01 del backlog).
- **Pregunta:** ¿cuál es la vía oficial más rápida y viable para recibir y responder en Good Doggy CRM los mensajes directos de la cuenta de TikTok de la tienda, y qué tiene que hacer el propietario?
- **Conclusión (F7b-08):** **SOLICITAR el acceso directo** a la Business Messaging API (pasar la DSPR) y, como plan B, un proveedor de la lista oficial de TikTok (Infobip, el más adecuado). Detalle en [§11](#11-recomendación). Sustituye la conclusión «esperar» de F7b-07 ([§9](#9-conclusión-de-f7b-07-esperar-sustituida)).
- **Documentos de esta serie:** el borrador de la solicitud y de las respuestas a la DSPR, [tiktok-access-request-draft.md](tiktok-access-request-draft.md) (no enviado), y el borrador de decisión [ADR-039](../adr/ADR-039-tiktok-channel.md) (`Proposed`).
- **ADRs relacionados:** [ADR-010](../adr/ADR-010-messaging-policy.md), [ADR-015](../adr/ADR-015-autonomous-delivery-program.md), [ADR-022](../adr/ADR-022-secrets-at-rest.md), [ADR-029](../adr/ADR-029-whatsapp-cloud-api-channel.md), [ADR-035](../adr/ADR-035-instagram-messenger-channels.md) (patrón del canal).

Este documento no contiene ningún dato de la tienda, ninguna cuenta, ningún id y ningún secreto. Solo se considera la vía oficial de TikTok; cualquier otra (automatizar la app, extraer mensajes, bibliotecas no oficiales) queda fuera por principio. Durante la investigación no se creó ninguna cuenta, no se contactó a ningún proveedor y no se envió ningún formulario.

## 0. Cómo leer las marcas

Cada afirmación lleva una marca y una fuente de la [lista final](#13-fuentes):

| Marca              | Significado                                                                                                                                                                            |
| ------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **CONFIRMADO**     | Leído en una página oficial de TikTok que cargó completa.                                                                                                                              |
| **OFICIAL-ÍNDICE** | Frase de una página oficial vista solo en el texto que indexa el buscador, sin leer la página completa.                                                                                |
| **TERCEROS**       | Lo dice la documentación de un socio o integrador (respond.io, Infobip, Chatwoot…). Es la fuente primaria de **su propia oferta** (precio, API, contrato), no de las reglas de TikTok. |
| **SIN VERIFICAR**  | Ninguna fuente lo confirma, o las fuentes chocan.                                                                                                                                      |
| **SUPUESTO**       | Deducción nuestra a partir de lo anterior.                                                                                                                                             |

**Cómo se leyó el portal en F7b-08.** El portal de TikTok API for Business (`business-api.tiktok.com/portal/docs/…`) se genera con JavaScript, y WebFetch solo devuelve el título. En F7b-08 esas páginas se leyeron con un servicio público de lectura en texto (`r.jina.ai`, que ejecuta la página y devuelve su texto), sin navegador, sin sesión y sin enviar ningún dato propio. Lo leído así completo se marca **CONFIRMADO**, con fecha 2026-11-16. Varias afirmaciones que F7b-07 dejó en **OFICIAL-ÍNDICE** pasan a **CONFIRMADO**. Las páginas de referencia de cada endpoint (envío, subida de imágenes, esquema del webhook) no se pudieron localizar por URL y siguen sin leer.

**Revisión del 2026-11-22.** Antes del merge se releyeron de la misma forma [T2], [T5], [T6], [T8], [T10], [T13], [T14], [T18], [T19], [T20] y [T21], y las páginas de Infobip, respond.io, Qiscus y Chatwoot. Las reglas oficiales no cambiaron: la API sigue en beta abierta en LATAM, Perú sigue sin nombrarse, la DSPR mantiene sus plazos y controles, y la lista de socios sigue siendo la de agosto de 2025. Correcciones de esta revisión: [T5] y [T6] pasan a **CONFIRMADO** ([§5](#5-reglas-de-mensajería), [§6](#6-webhooks)); la página de socios [T8] habla de gestión de mensajes, anuncios y medición, no de la Business Messaging API ([§7](#7-proveedores-autorizados-por-tiktok)); los alcances de la API de Infobip, su firma HMAC, su prueba gratuita y su DPA se corrigen con sus páginas actuales [P7] [P19] [P20] [P21]; y respond.io declara el canal sin cargo adicional sobre su plan [P1].

## 1. Qué API oficial existe

- La **Business Messaging API** de TikTok API for Business es «a suite of interfaces enabling authorized TikTok accounts to exchange direct messages with TikTok users through API-integrated platforms». **CONFIRMADO** [T3]
- Incluye webhooks; enviar, recibir y gestionar mensajes directos; mensajes de bienvenida, preguntas sugeridas y flujos de autorización. **CONFIRMADO** [T13]. La referencia lista: enviar un mensaje a una conversación, listar conversaciones y mensajes, subir una imagen, descargar una imagen o un video de un mensaje, comprobar la capacidad de una cuenta, Comment-to-Message, el webhook (crear, leer, borrar) y los mensajes automáticos. **CONFIRMADO** [T4]
- **Sigue en beta:** «now in Open Beta in APAC, LATAM, METAP, and North America». **CONFIRMADO** [T13] (2026-11-16). No hay anuncio de disponibilidad general. **SIN VERIFICAR**
- La **Messaging Events API** es otra cosa: envía a TikTok señales de las conversaciones para medir los anuncios de mensajería, sin el contenido. **CONFIRMADO** [T7] [T8]. Solo importa si la tienda pauta Messaging Ads.

## 2. Elegibilidad de la cuenta y de Perú

- **Tipo de cuenta:** la API solo admite cuentas con **Advanced Access** o **Verified Business Account (VBA)**; una cuenta «General» no es elegible, pero puede subir de nivel. Los mensajes de TikTok Shop van por otro sistema. **CONFIRMADO** [T2]
  - Advanced Access: vincular la cuenta a TikTok for Business o a un Business Center (aunque no esté verificado). VBA: verificar el negocio en Business Suite o vincular la cuenta a un Business Center verificado. **CONFIRMADO** [T10] (actualizada en mayo de 2026)
  - Un socio informa que desde abril de 2026 TikTok baja a «General» las cuentas sin ese vínculo y que entonces la API falla con «Only Business account can use Message API», aunque el canal parezca conectado. **TERCEROS** [P4] (modificada el 2026-09-05)
  - La verificación del negocio (VBA) existe para Perú. **CONFIRMADO** [T11] (lectura de F7b-07)
- **Regiones**, según el país de registro de la cuenta de TikTok. **CONFIRMADO** [T2]:
  - EEE, Suiza y Reino Unido: no disponible; sus mensajes no se leen ni por la API ni por webhooks.
  - Estados Unidos: el desarrollador tiene que pasar la DSPR y la US Data Security Review, y aceptar el USDS Addendum. (respond.io dice que en agosto de 2026 se levantó la restricción para las cuentas de EE. UU. en su plataforma [P22], **TERCEROS**; Infobip sigue excluyendo EE. UU. [P3]. No afecta a una cuenta de Perú.)
  - **«Rest of World»: basta con que el desarrollador pase la DSPR.**
- **Perú:** ninguna página oficial lo nombra. Está en LATAM, que TikTok declara en beta abierta [T13], y no figura en ninguna exclusión [T2]. **SUPUESTO** (fuerte): una cuenta registrada en Perú es «Rest of World» y solo necesita la DSPR. Que TikTok lo confirme por escrito: **SIN VERIFICAR**; la pregunta va en la solicitud.
- **Imágenes:** dos socios publican la misma lista de países donde no se pueden enviar ni recibir imágenes (Australia, Colombia, la UE, India, Japón, Filipinas, Corea del Sur, Turquía, Reino Unido y EE. UU., entre otros); **Perú no figura**. Las imágenes fallan si el emisor o el receptor está en un país de la lista. **TERCEROS** [P1] [P11]

## 3. Quién puede pedir el acceso (el punto que F7b-07 dejó abierto)

**Lo que TikTok exige al desarrollador** (todo **CONFIRMADO**, 2026-11-16):

- **Una empresa, no una persona:** «Currently, we are unable to onboard personal accounts or individual developers.» El perfil se registra con un correo del **dominio de la empresa** (se rechazan los correos personales o temporales) y un sitio web público alojado en un dominio de la empresa. [T19]
- **Tipo de usuario:** «Technology Company», «Direct Advertiser» o «Agency», y tiene que coincidir con lo que se declara. El resultado de la revisión del perfil llega en **tres días hábiles**. [T19]
- **App de desarrollador:** la descripción tiene que decir los usos y «whether access is internal or shared with external accounts or organizations»; revisión de **2 a 3 días hábiles**; hasta cinco apps por desarrollador. [T20]
- **Desde el 2026-03-20**, antes de crear una app o de ampliar sus permisos con el alcance «TikTok Accounts», hay que llenar el **Accounts API Access Application Form**. [T20] [T21]. Que la mensajería necesite ese alcance lo dice un tercero (Chatwoot pide activar «TikTok Accounts» [P2]): **TERCEROS**. **SUPUESTO:** el formulario aplica.
- **Para la mensajería:** tener una app aprobada; si no la hay, la página indica pedir una con los alcances «Ad Account Management», «CTX Events Management» y «Measurement». Después se leen la DSPR y la revisión de EE. UU. y se envía el **formulario de admisión de la DSPR** (un formulario de Lark enlazado desde la página oficial). [T2]

**Respuesta a la pregunta de F7b-07** (¿aprueba TikTok un CRM propio de una sola empresa?):

- Ninguna página oficial lo exige ni lo prohíbe. El tipo **«Direct Advertiser»** sirve a una empresa que trabaja para su propio negocio [T19], y la app tiene que declarar si el acceso es **«internal»** [T20]: el formulario prevé el uso interno. **CONFIRMADO** en cuanto a lo que piden los formularios.
- La DSPR dice que **no tiene excepciones** para obtener el permiso [T14]: una pyme pasa la misma revisión que una plataforma. No exige ISO 27001 ni SOC 2; solo dice que adjuntarlos «may speed approval». **CONFIRMADO** [T14]
- Un integrador de código abierto (Chatwoot autoalojado) indica a **cada instalación** que cree su propia app y pida el acceso: es nuestro mismo caso, un desarrollador con su propia app. **TERCEROS** [P2]
- **SIN VERIFICAR:** que TikTok apruebe de hecho la DSPR de un CRM de una sola cuenta. Ningún caso público lo confirma ni lo niega. Solo se sabe enviando la solicitud.

## 4. La Data Security & Privacy Review (DSPR)

Todo **CONFIRMADO** [T2] [T14] [T15], leído el 2026-11-16. La página de la DSPR no muestra fecha de actualización.

**Proceso y plazos**

1. Se envía el formulario de admisión con el id de una app aprobada. TikTok **inicia la revisión en hasta 10 días hábiles**. [T2]
2. TikTok envía por correo el cuestionario de la DSPR («DDQ»). Si la solicitud incluye EE. UU., envía además el de la revisión de EE. UU. («VAQ»), normalmente una semana después. [T2]
3. La DSPR **suele tardar 2-4 semanas**. Si se rechaza, se atienden los comentarios del evaluador y se vuelve a enviar. [T14]
4. Las revisiones son **manuales y no tienen seguimiento en el portal**; los tickets de soporte son solo para problemas técnicos. La aprobación llega **por correo**. [T2]
5. **Excluir EE. UU.** evita alargar el plazo: sin la revisión de EE. UU., la app queda autorizada para el resto del mundo y no puede leer cuentas de EE. UU., el EEE, Suiza ni el Reino Unido. [T2]. La revisión de EE. UU. tarda 7-10 días hábiles y excluye a desarrolladores con dueños de ciertos países [T15]. **No aplica a la tienda.**

**Qué evalúa** [T14]: el acceso, el tratamiento y el almacenamiento de los datos; la sede; la propiedad extranjera; la ubicación del personal y de los sistemas; y los **subcontratistas** (encargados del tratamiento).

**Privacidad** [T14]:

- Cumplir las leyes de privacidad aplicables (cita como ejemplos el GDPR y la CPRA).
- Una persona responsable de la privacidad y de contacto (un DPO donde la ley lo exija).
- Un **aviso de privacidad publicado**: qué datos, para qué, cómo se usan, a dónde se transfieren, cómo se protegen y cuánto tiempo se guardan.
- Atender los derechos de acceso, descarga, corrección y borrado.
- **Borrar a tiempo los datos personales cuando el usuario retira la autorización.**
- Pedir solo los datos y las API mínimas para el fin declarado.

**Controles de seguridad** [T14]:

| Área                    | Lo que pide TikTok                                                                                                                                          |
| ----------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Políticas               | Un marco de seguridad de la información, revisado con regularidad y **firmado por la dirección**                                                            |
| Red                     | Entornos segregados y herramientas de monitorización como NIDS o HIPS                                                                                       |
| Equipos                 | Antivirus o HIPS, escaneos regulares y firmas al día                                                                                                        |
| Línea base              | Requisitos de contraseña, **MFA para cuentas de administrador**, bloqueo automático de pantalla (el ejemplo: 15 minutos) y formación periódica en seguridad |
| Protección de datos     | Política de control de acceso publicada; **cifrado en reposo de los datos personales con AES-256 o RSA-1024 como mínimo**; en tránsito, **TLS 1.2 o más**   |
| Control de acceso       | Política de mínimo privilegio; conservar los registros de acceso a los sistemas; **revisar los privilegios al menos una vez al año**                        |
| Vulnerabilidades        | Monitorización y corrección continuas, con escaneos de vulnerabilidades o pentest e informes conservados                                                    |
| Incidentes              | Política de respuesta a incidentes publicada, **simulacro al menos una vez al año** e informes documentados                                                 |
| Documentos que aceleran | Certificado ISO 27001, informe SOC 2 y el último escaneo de vulnerabilidades o pentest                                                                      |

El mapeo de estos controles a lo que ya existe en el repositorio, con lo que falta, está en [tiktok-access-request-draft.md §5](tiktok-access-request-draft.md#5-borrador-de-las-respuestas-a-la-dspr).

## 5. Reglas de mensajería

- **Límites oficiales** («non-mutual follow conversations»). **CONFIRMADO** [T18]:
  - Tras el primer mensaje del usuario, la cuenta puede enviar **hasta 10 mensajes en las 48 horas siguientes**.
  - Cada respuesta del usuario abre una ventana nueva de 48 horas con **mensajes ilimitados**.
  - Si pasan más de 48 horas desde la última respuesta del usuario, la cuenta solo puede enviar **tres mensajes más**.
  - La página no habla de las conversaciones con seguimiento mutuo ni de una excepción para agentes humanos. **SIN VERIFICAR**
- Los socios describen versiones más estrictas: 10 mensajes seguidos en 48 h y nada fuera de la ventana [P1]; un tope de 100-200 conversaciones al día por cuenta, con el error `40064` [P11]. **TERCEROS**. El diseño toma la regla oficial y deja el tope diario como **SIN VERIFICAR**.
- **El cliente siempre empieza**; no hay difusiones. **TERCEROS** [P1] [P3] [P11]
- **Tipos de mensaje:** texto (hasta 6000 caracteres) e imágenes JPG o PNG de hasta 3 MB; publicaciones de TikTok solo de entrada; sin video, voz ni stickers. **TERCEROS** [P1]. La referencia oficial lista «Download an image or a video from a message» [T4], así que podría existir el video **entrante**. **SIN VERIFICAR**
- **Límite de tasa:** por app de desarrollador; el nivel «Basic» permite 10 consultas por segundo, 600 por minuto y 864 000 al día (el contador diario se reinicia a las 00:00 UTC), y el error `40100` indica estrangulamiento. **CONFIRMADO** [T6] (2026-11-22). La página es general de API for Business y no nombra la Business Messaging API: que ese nivel sea el de la mensajería es **SUPUESTO**, coherente con los 10 QPS que publica respond.io para la mensajería [P1].
- **Ajuste de la cuenta:** aceptar mensajes de todos, o aceptar cada solicitud a mano en la app. **TERCEROS** [P2] [P4] [P11]

## 6. Webhooks

- Existe el webhook de la Business Messaging API con el tipo de evento `DIRECT_MESSAGE` («Business Messaging events»), configurado por app con `/business/webhook/update/`, `/list/` y `/delete/`. **CONFIRMADO** [T5] (2026-11-22). La consulta (`GET …/business/webhook/list/`) pide `app_id`, `secret` y `event_type`, y el ejemplo los pasa en la query string: el cliente del CRM no debe registrar esa URL completa en ningún log. **SUPUESTO** de diseño.
- **Firma:** TikTok for Developers firma con `TikTok-Signature` (`t=<timestamp>,s=<firma>`, HMAC-SHA256 de `timestamp.cuerpo`) [T12], pero esa página no menciona TikTok API for Business. Chatwoot verifica con el secreto de la app y exige relojes sincronizados («timestamps within 5 seconds») [P2]. Que la Business Messaging API use ese esquema: **SIN VERIFICAR**. Se confirma con la documentación que se abre al aprobar el acceso, antes de escribir el verificador.
- **Reintentos:** la documentación general de TikTok for Developers reintenta hasta 72 horas y puede enviar duplicados [T17]; que valga aquí: **SIN VERIFICAR**.

## 7. Proveedores autorizados por TikTok

**La lista oficial.** La única lista de TikTok es la página «About Messaging Partners» (actualizada en **agosto de 2025**; igual en inglés y en español) [T8]. Nombra nueve socios, cada uno con su guía de conexión: **Blip.ai, Halosis, Haravan, Pancake, Qiscus, Respond.io, Zwiz, Gupshup e Infobip**. **CONFIRMADO** [T8]. Al conectar, el negocio aprueba permisos como «access and manage conversations» y «subscribe to and receive webhooks», instruye al socio para tratar los datos y puede desconectarse cuando quiera; cada socio se rige por sus propios términos. **CONFIRMADO** [T8]. El directorio de socios de marketing no muestra un filtro de mensajería legible [T9b]; el distintivo «Messaging Partner» se lanzó en el sudeste asiático [T9].

**Qué es esa lista (revisión del 2026-11-22).** La página presenta a los socios como herramientas de gestión de mensajes (bandeja con varios usuarios, etiquetas, respuestas automáticas, conexión con un CRM), necesarias para los Messaging Ads y para medir eventos de mensajería; el permiso que se aprueba al conectar sirve para que el socio extraiga señales de las conversaciones y las devuelva a TikTok, y la página dice que el contenido de los mensajes no se comparte con TikTok. Menciona los mensajes directos de TikTok solo como uno de los canales de la bandeja unificada y **no nombra la Business Messaging API**. **CONFIRMADO** [T8]. Por eso «socio oficial» significa aquí «socio de mensajería de la lista de TikTok», no «autorizado para la Business Messaging API»: que cada socio use esa API lo dice su propia documentación (**TERCEROS**). No existe otra lista oficial de socios de la Business Messaging API. **SIN VERIFICAR** que no la haya en una página no indexada.

**Quién entrega los mensajes directos de TikTok a otro sistema.** La lista mezcla dos productos: la bandeja de mensajes directos (Business Messaging API) y la medición de anuncios que llevan a WhatsApp o Messenger (Messaging Events). Esto dice la guía que TikTok enlaza para cada socio:

| Socio          | ¿Mensajes directos de TikTok?                                                     | Perú                                                    | API o webhook hacia nuestro CRM                                                                                                                                                                                        | Precio publicado                                                                                                                                                          | Contrato de datos y exportación                                                                                                                                                                                                          | Ventana y medios                                                                                        | Fuente                                |
| -------------- | --------------------------------------------------------------------------------- | ------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------- | ------------------------------------- |
| **Infobip**    | **Sí**: conversaciones uno a uno con la cuenta de empresa                         | No excluido (excluye EE. UU., EEE, Suiza y Reino Unido) | **Sí**: entrantes, entregas y leídos por webhook a nuestro endpoint, con autenticación HMAC opcional («si está disponible»); respuestas por su API (clave con los alcances `tiktok:manage` y `saas:tiktok-ads:manage`) | No: calculadora por país; prueba gratuita de 60 días para toda cuenta creada por autoregistro, solo hacia destinatarios verificados (que cubra TikTok: **SIN VERIFICAR**) | DPA estándar (versión 2.0 del 2026-09-01), parte integral del contrato; Infobip es encargado (y responsable independiente en usos acotados); datos en la UE/EEE por defecto; al terminar, devolución o **inicio** del borrado en 30 días | El usuario empieza; validez de 48 h como máximo; texto, imagen JPG/PNG de 3 MB y publicación compartida | [P3] [P5] [P6] [P7] [P19] [P20] [P21] |
| **respond.io** | **Sí** (beta abierta)                                                             | No excluido                                             | **Parcial**: la API de desarrollador desde el plan Growth; los **webhooks** salientes solo en **Advanced** y Enterprise                                                                                                | Starter 99, Growth 199 y Advanced 349 USD/mes (79, 159 y 279 con pago anual), más usuarios y contactos extra; el canal TikTok, sin cargo adicional sobre el plan [P1]     | DPA **a pedido**; aloja en AWS sin región publicada; 18 subencargados, entre ellos proveedores de IA                                                                                                                                     | 48 h, 10 mensajes, texto de 6000, JPG/PNG de 3 MB                                                       | [P1] [P4] [P8] [P9] [P10]             |
| **Qiscus**     | **Sí**                                                                            | No excluido                                             | Webhooks de asignación y de sesión nueva documentados para integrar un CRM; que se disparen con TikTok: **SIN VERIFICAR**                                                                                              | No: cotización                                                                                                                                                            | No leído                                                                                                                                                                                                                                 | 10 mensajes en 48 h; 100-200 conversaciones al día; texto e imagen                                      | [P11] [P12] [P13]                     |
| Pancake        | **SIN VERIFICAR** (la guía habla de medir anuncios hacia Messenger y WhatsApp)    | No lo dice                                              | No leído                                                                                                                                                                                                               | No leído                                                                                                                                                                  | No leído                                                                                                                                                                                                                                 | —                                                                                                       | [P18]                                 |
| Blip.ai        | **No** en la guía enlazada: anuncios que llevan a WhatsApp o Messenger, y eventos | —                                                       | —                                                                                                                                                                                                                      | —                                                                                                                                                                         | —                                                                                                                                                                                                                                        | —                                                                                                       | [P14]                                 |
| Gupshup        | **No**: anuncios que llevan a WhatsApp                                            | —                                                       | —                                                                                                                                                                                                                      | —                                                                                                                                                                         | —                                                                                                                                                                                                                                        | —                                                                                                       | [P15]                                 |
| Halosis        | **No**: anuncios hacia WhatsApp (Indonesia)                                       | —                                                       | —                                                                                                                                                                                                                      | —                                                                                                                                                                         | —                                                                                                                                                                                                                                        | —                                                                                                       | [P16]                                 |
| Zwiz           | **No**: anuncios hacia Messenger (Tailandia)                                      | —                                                       | —                                                                                                                                                                                                                      | —                                                                                                                                                                         | —                                                                                                                                                                                                                                        | —                                                                                                       | [P17]                                 |
| Haravan        | **No**: eventos de anuncios (Vietnam)                                             | —                                                       | —                                                                                                                                                                                                                      | —                                                                                                                                                                         | —                                                                                                                                                                                                                                        | —                                                                                                       | [T8]                                  |

Todo lo de la tabla es **TERCEROS** (la documentación de cada socio, leída el 2026-11-16), salvo la lista en sí, que es **CONFIRMADO** [T8].

**Fuera de la comparación.** Wati, SleekFlow, Kommo, Trengo, Monochat y otros anuncian mensajes directos de TikTok, pero **no están en la lista oficial** [T8]; por la regla de este work item no se comparan. Tampoco se considera ninguna biblioteca ni servicio no oficial.

**Lo que no cambia con un socio:**

- La ventana, los topes y las regiones son de TikTok y valen igual a través de un socio [T2] [T18].
- Un socio es otro encargado del tratamiento con acceso a todos los mensajes, en su nube: Infobip, en la UE por defecto [P7]; respond.io, en AWS sin región publicada y con proveedores de IA entre sus subencargados [P10]. Para la ley peruana de datos personales (Ley 29733 y su reglamento, vigente desde marzo de 2025 [L1]) es un **flujo transfronterizo hacia un encargado**, que el aviso de privacidad tiene que declarar. **SUPUESTO**; lo revisa quien asesore a la tienda en lo legal.
- El CRM igual necesita un adaptador nuevo (para la API del socio en lugar de la de TikTok), su simulador y su webhook: el trabajo de código es parecido al de la vía directa. **SUPUESTO**

## 8. Impacto en el diseño

La propuesta concreta está en [ADR-039](../adr/ADR-039-tiktok-channel.md) (`Proposed`). En resumen, sigue el patrón de [ADR-035](../adr/ADR-035-instagram-messenger-channels.md): `ChannelAdapter`, webhook firmado por `webhook_ingress`, cuenta `TIKTOK` con el token cifrado ([ADR-022](../adr/ADR-022-secrets-at-rest.md)), enrutado por una función `SECURITY DEFINER`, identidad **sin teléfono** en `contact_identities` sin fusión automática, la regla de [§5](#5-reglas-de-mensajería) en `MessagingPolicyService` ([ADR-010](../adr/ADR-010-messaging-policy.md)) y un simulador para las pruebas. Lo nuevo frente a Meta: borrar los datos de TikTok al desconectar (lo exige la DSPR), excluir EE. UU., el EEE, Suiza y el Reino Unido, y tolerar los cambios de una API en beta.

## 9. Conclusión de F7b-07: esperar (sustituida)

F7b-07 recomendó **esperar**: la DSPR era manual y pedía pruebas de seguridad, Perú no se nombraba, la API estaba en beta y las reglas solo se conocían por los socios. F7b-08 cambia tres de esas premisas. El propietario pidió adelantar TikTok (Inbox omnicanal P0); las reglas de acceso, la DSPR y los límites ya se leyeron en las páginas oficiales completas; y los formularios oficiales prevén el uso interno de un anunciante directo. Sigue en pie que la aprobación depende de TikTok y que la API está en beta.

## 10. Plazos estimados

| Paso                                                         | Quién             | Plazo                                                     | Fuente             |
| ------------------------------------------------------------ | ----------------- | --------------------------------------------------------- | ------------------ |
| Cuenta de empresa con Advanced Access o VBA                  | Propietario       | Horas (Advanced Access) o días (verificación del negocio) | [T10] **SUPUESTO** |
| Correo con el dominio de la empresa y sitio web público      | Propietario       | Días, si falta el dominio                                 | [T19] **SUPUESTO** |
| Aviso de privacidad, políticas firmadas y brechas de la DSPR | Propietario y CRM | 1-2 semanas                                               | **SUPUESTO**       |
| Perfil de desarrollador                                      | TikTok            | 3 días hábiles                                            | [T19]              |
| Formulario de Accounts API y app de desarrollador            | TikTok            | 2-3 días hábiles (el formulario: **SIN VERIFICAR**)       | [T20] [T21]        |
| Inicio de la DSPR tras el formulario de admisión             | TikTok            | Hasta 10 días hábiles                                     | [T2]               |
| DSPR                                                         | TikTok            | 2-4 semanas, más las vueltas por comentarios              | [T14]              |
| Construir el canal contra el simulador (ADR-039)             | CRM               | 3-4 semanas, en paralelo a la DSPR                        | **SUPUESTO**       |
| Conectar la cuenta real y probar de punta a punta            | Propietario y CRM | Días, tras la aprobación                                  | **SUPUESTO**       |

**Total por la vía directa:** unas **6-9 semanas** hasta el primer mensaje real si la DSPR se aprueba a la primera; más si TikTok pide aclaraciones. **SUPUESTO**

**Total con un socio (plan B):** contrato y conexión en días, más las mismas 3-4 semanas para construir el adaptador del socio: unas **4-6 semanas**, con costo mensual y un tercero con los mensajes. **SUPUESTO**

## 11. Recomendación

**Pedir ya el acceso directo (DSPR) y dejar a Infobip como plan B.**

1. **Primero, la vía directa.** Es la única sin un tercero con los mensajes de los clientes y sin un costo por uso conocido. Perú entra en «Rest of World», que solo pide la DSPR [T2]; los formularios oficiales prevén el uso interno de un anunciante directo [T19] [T20]; y el patrón técnico ya existe (ADR-035). Su costo es tiempo (6-9 semanas) y cerrar las brechas de la DSPR, que el CRM tiene que cerrar de todos modos para producción.
2. **Plan B: Infobip**, de la lista oficial [T8], si TikTok rechaza la DSPR, si no responde en **8 semanas** desde el envío del formulario de admisión, o si el propietario decide que la velocidad vale más que sumar un encargado. Es el único socio de la lista con mensajes directos de TikTok, un webhook de entrada documentado, una API de salida y un DPA estándar con borrado al terminar el contrato [P3] [P6] [P7]. respond.io queda detrás: exige el plan Advanced para los webhooks y su DPA es a pedido [P8] [P9].
3. **Lo que no se hace:** ningún código antes de que el propietario acepte ADR-039; nada se envía a TikTok sin su autorización (D-TT-02); nunca una vía no oficial.

## 12. Checklist del propietario

Ningún valor de esta lista va al repositorio. Las decisiones D-TT-… están en [ADR-039 §13](../adr/ADR-039-tiktok-channel.md#13-decisiones-del-propietario).

**Puede hacerlo ya (no depende de TikTok):**

- [ ] Decidir la vía (D-TT-01): directa con plan B, solo directa o solo un socio.
- [ ] Poner la cuenta de TikTok de la tienda en modo **empresa**, con mensajes de **todos**, vinculada a TikTok for Business o a un **Business Center** (Advanced Access). Si se puede, verificar el negocio (**VBA**, disponible en Perú).
- [ ] Tener un **correo con el dominio de la empresa** y un **sitio web público** en un dominio de la empresa (los pide el perfil de desarrollador; ADR-033 registra que aún no hay dominio).
- [ ] Publicar el **aviso de privacidad** que cubra los mensajes de TikTok (qué se guarda, dónde, cuánto tiempo, encargados, derechos y contacto) y nombrar a la persona responsable de la privacidad (D-TT-04).
- [ ] Firmar las tres políticas breves que pide la DSPR: seguridad de la información, control de acceso y respuesta a incidentes. El CRM puede redactar las plantillas en un work item; las firmadas no van al repositorio público.
- [ ] Activar el segundo factor en todas las cuentas de administración: el CRM (Owner y Administradores), el servidor y su proveedor, GitHub, el almacenamiento y el correo.
- [ ] Decidir si los mensajes de TikTok pueden ir al copiloto de IA (D-TT-03). Si pueden, el proveedor de IA se declara como subencargado en la DSPR.
- [ ] Aprobar el cierre de las brechas técnicas de [tiktok-access-request-draft.md §6](tiktok-access-request-draft.md#6-brechas-y-cómo-cerrarlas): cifrado del disco del servidor, ClamAV en producción, un escaneo de vulnerabilidades con informe y el borrado de los datos de TikTok al desconectar.
- [ ] Revisar ADR-039 y aceptarlo o pedir cambios, incluido el orden respecto a Instagram y Messenger (D-TT-07).
- [ ] Plan B, solo si lo elige: pedir a Infobip su tarifa de TikTok para Perú y su DPA, y revisarlos.

**Cuando autorice el envío (D-TT-02), en este orden:**

- [ ] Registrarse como desarrollador en TikTok API for Business como **Direct Advertiser**, con el correo de la empresa ([borrador §2](tiktok-access-request-draft.md#2-borrador-del-perfil-de-desarrollador)).
- [ ] Llenar el **Accounts API Access Application Form** y crear la app de desarrollador, declarada de **uso interno** ([borrador §3](tiktok-access-request-draft.md#3-borrador-de-la-app-de-desarrollador)).
- [ ] Enviar el **formulario de admisión de la DSPR** **sin EE. UU.**, con las preguntas para TikTok ([borrador §4](tiktok-access-request-draft.md#4-borrador-del-formulario-de-admisión-de-la-dspr) y [§7](tiktok-access-request-draft.md#7-preguntas-para-tiktok)).

**Espera a TikTok:**

- [ ] Perfil aprobado (3 días hábiles) y app aprobada (2-3 días hábiles).
- [ ] Correo con el cuestionario de la DSPR (en hasta 10 días hábiles): contestarlo con el [borrador §5](tiktok-access-request-draft.md#5-borrador-de-las-respuestas-a-la-dspr) y las pruebas.
- [ ] Correo de aprobación (2-4 semanas) y confirmación escrita de que cubre las cuentas registradas en Perú.
- [ ] Con la aprobación: leer la documentación del webhook y de la firma, configurar el webhook hacia `https://crm.<dominio>/webhooks/tiktok/` y conectar la cuenta en la pantalla de canales (work items de ADR-039).

## 13. Fuentes

Las de F7b-07 se leyeron el **2026-11-15**; las de F7b-08, el **2026-11-16**; la revisión previa al merge releyó las indicadas el **2026-11-22**. Entre paréntesis, la fecha que muestra cada página; las del portal no muestran fecha de actualización (solo «© 2026»).

**Oficiales de TikTok**

- [T1] About API for Business (septiembre de 2025; leída el 2026-11-15): <https://ads.tiktok.com/help/article/marketing-api?lang=en>
- [T2] Access to Business Messaging API (leída completa el 2026-11-16): <https://business-api.tiktok.com/portal/docs/access-to-business-messaging-api/v1.3>
- [T3] Business Messaging Overview (leída completa el 2026-11-16): <https://business-api.tiktok.com/portal/docs/business-messaging-api-overview/v1.3>
- [T4] Business Messaging API reference y Direct messages API reference (índices leídos el 2026-11-16): <https://business-api.tiktok.com/portal/docs/business-messaging/v1.3> y <https://business-api.tiktok.com/portal/docs/direct-messages/v1.3>
- [T5] Get a TikTok Business Messaging Webhook configuration (índice del buscador el 2026-11-15; leída completa el 2026-11-22): <https://business-api.tiktok.com/portal/docs/get-a-business-messaging-webhook-configuration/v1.3>
- [T6] Rate limits Overview (índice del buscador el 2026-11-16; leída completa el 2026-11-22): <https://business-api.tiktok.com/portal/docs/rate-limits/v1.3>
- [T7] Introducing TikTok Messaging Ads (2025-06-03; leída el 2026-11-15): <https://ads.tiktok.com/business/en/blog/messaging-ads>
- [T8] About Messaging Partners (agosto de 2025; leída en inglés y en español el 2026-11-16 y releída en inglés el 2026-11-22, sin cambios): <https://ads.tiktok.com/resources/help/article/about-message-management-tools> y <https://ads.tiktok.com/help/article/about-message-management-tools?lang=es>
- [T9] Introducing the Messaging Partner specialty (2025-05-09; leída el 2026-11-16): <https://ads.tiktok.com/business/en/blog/introducing-messaging-partner-specialty>
- [T9b] Partner Directory (leída el 2026-11-16; sin filtro de mensajería legible): <https://partners.tiktok.com/directory/pc/en>
- [T10] About TikTok account entitlements (mayo de 2026; leída el 2026-11-16): <https://ads.tiktok.com/resources/help/article/about-tiktok-account-entitlements>
- [T11] About business verification for TikTok accounts (junio de 2026; leída el 2026-11-15): <https://ads.tiktok.com/resources/help/article/about-business-registration?lang=en>
- [T12] TikTok for Developers, Webhooks: Verification (leída el 2026-11-15): <https://developers.tiktok.com/docs/en/webhooks-verification>
- [T13] Business Messaging API (leída completa el 2026-11-16): <https://business-api.tiktok.com/portal/docs/business-messaging-api/v1.3>
- [T14] TikTok Business Messaging API data security & privacy review (leída completa el 2026-11-16): <https://business-api.tiktok.com/portal/docs/data-security-and-privacy-review/v1.3>
- [T15] US data security review for TikTok Business Messaging API (leída completa el 2026-11-16): <https://business-api.tiktok.com/portal/docs/us-data-security-review/v1.3>
- [T16] Sandbox accounts (índice del buscador, 2026-11-15): <https://business-api.tiktok.com/portal/docs/sandbox-accounts/v1.3>
- [T17] TikTok for Developers, Webhooks: Overview (leída el 2026-11-15): <https://developers.tiktok.com/docs/en/webhooks-overview>
- [T18] Messaging limits for Business Messaging API (leída completa el 2026-11-16): <https://business-api.tiktok.com/portal/docs/messaging-limits-for-business-messaging-api/v1.3>
- [T19] Register as a developer (leída completa el 2026-11-16): <https://business-api.tiktok.com/portal/docs/register-as-a-developer/v1.3>
- [T20] Create a developer app (leída completa el 2026-11-16): <https://business-api.tiktok.com/portal/docs/create-a-developer-app/v1.3>
- [T21] Get started with Accounts API (leída completa el 2026-11-16): <https://business-api.tiktok.com/portal/docs/get-started/v1.3>
- [T22] Business Messaging API Education Hub (leída el 2026-11-16; sin datos de acceso): <https://business-api.tiktok.com/portal/bm-api/education-hub>

Los formularios de admisión de la DSPR y de Accounts API son formularios de Lark enlazados desde [T2] y [T20]; no se abrieron.

**Socios e integradores** (fuente primaria de su propia oferta, no de las reglas de TikTok)

- [P1] respond.io, TikTok overview (modificada el 2026-09-06 al leerla el 2026-11-16; republicada con fecha del 2026-11-18, modificada el 2026-11-20 y releída el 2026-11-22): <https://respond.io/help/tiktok/tiktok-overview>
- [P2] Chatwoot, Setting up TikTok channel (leída el 2026-11-16): <https://developers.chatwoot.com/self-hosted/configuration/features/integrations/tiktok>
- [P3] Infobip, TikTok Business Messaging (sin fecha visible; leída el 2026-11-16): <https://www.infobip.com/docs/tiktok>
- [P4] respond.io, TikTok quick start (modificada el 2026-09-05; leída el 2026-11-16): <https://respond.io/help/tiktok/tiktok-quick-start>
- [P5] Infobip, Get started with TikTok Business Messaging (sin fecha visible; leída el 2026-11-16): <https://www.infobip.com/docs/tiktok/get-started>
- [P6] Infobip, Send and receive TikTok messages y TikTok conversation events (extracto del buscador, 2026-11-16): <https://www.infobip.com/docs/tiktok/send-and-receive-messages> y <https://www.infobip.com/docs/tiktok/conversation-events>
- [P7] Infobip, Data Processing Agreement (versión 2.0 del 2026-09-01; leída el 2026-11-16 y releída el 2026-11-22): <https://www.infobip.com/data-processing-agreement>
- [P8] respond.io, Pricing (leída el 2026-11-16): <https://respond.io/pricing>
- [P9] respond.io, DPA (leída el 2026-11-16): <https://respond.io/legal/dpa>
- [P10] respond.io, Subprocessors (septiembre de 2026; leída el 2026-11-16): <https://respond.io/subprocessors>
- [P11] Qiscus, TikTok Direct Message (sin fecha; leída el 2026-11-16): <https://documentation.qiscus.com/omnichannel-chat/tiktok-direct-message>
- [P12] Qiscus, Pricing (leída el 2026-11-16): <https://qiscus.com/en/pricing>
- [P13] Qiscus, CRM Integration (extracto del buscador, 2026-11-16): <https://support.qiscus.com/hc/en-us/articles/1732590970508-CRM-Integration>
- [P14] Blip, TikTok Integration (sin fecha; leída el 2026-11-16): <https://help.blip.ai/hc/en-us/articles/26458114456215-TikTok-Integration>
- [P15] Gupshup, TikTok to WhatsApp (leída el 2026-11-16): <https://console-docs.gupshup.io/docs/tiktok-to-whatsapp>
- [P16] Halosis, integración de TikTok Messaging Ads (2025-04-29; leída el 2026-11-16): <https://blog.halosis.co.id/cara-integrasi-tiktok-messaging-ads-dengan-halosis/>
- [P17] Zwiz, TikTok Ads hacia Messenger (2024-08-15; leída el 2026-11-16): <https://blog.zwiz.app/tiktokads-to-messenger/>
- [P18] Pancake, guía de TikTok (texto truncado; leída el 2026-11-16): <https://docs.pancake.biz/pancake/st-f8/st-p3/st-s1?lang=en>
- [P19] Infobip, TikTok over API (sin fecha visible; leída el 2026-11-22): <https://www.infobip.com/docs/tiktok/api>
- [P20] Infobip, Free trial (sin fecha visible; extracto del buscador, 2026-11-22): <https://www.infobip.com/docs/essentials/getting-started/free-trial>
- [P21] Infobip, TikTok message types (sin fecha visible; leída el 2026-11-22): <https://www.infobip.com/docs/tiktok/message-types>
- [P22] respond.io, TikTok Business Messaging (Beta), página de integración (actualizada el 2026-11-17; leída el 2026-11-22): <https://respond.io/integrations/tiktok-business-messaging>

**Ley peruana**

- [L1] El Peruano, entrada en vigor del nuevo reglamento de la Ley 29733 (D. S. 016-2024-JUS) a fines de marzo de 2025 (extracto del buscador, 2026-11-16; las fuentes difieren en un día): <https://www.elperuano.pe/noticia/266995-minjusdh-el-lunes-31-regira-nuevo-reglamento-de-ley>

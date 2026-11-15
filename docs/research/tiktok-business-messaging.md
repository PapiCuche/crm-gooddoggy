# TikTok Business Messaging: investigación de elegibilidad

- **Work item:** F7b-07 ([#780](https://github.com/PapiCuche/crm-gooddoggy/issues/780)), Fase 7b de [phase-7.md](../phases/phase-7.md) (E19-01 del backlog; Fase 13 del roadmap).
- **Pregunta:** ¿puede el negocio usar la mensajería empresarial de TikTok desde un CRM propio, por la vía oficial, y qué haría falta?
- **Fecha de lectura de todas las fuentes:** 2026-11-15.
- **Conclusión:** **ESPERAR** (detalle en [§9](#9-conclusión-esperar)). No se construye nada ahora; no se descarta.
- **ADRs relacionados:** [ADR-010](../adr/ADR-010-messaging-policy.md) (política de mensajería), [ADR-029](../adr/ADR-029-whatsapp-cloud-api-channel.md) (patrón del canal de WhatsApp), [ADR-035](../adr/ADR-035-instagram-messenger-channels.md) (Instagram y Messenger, el patrón más parecido: identidades sin teléfono).

Este documento no contiene ningún dato de la tienda, ninguna cuenta, ningún id y ningún secreto. Solo se considera la vía oficial de TikTok; cualquier otra (automatizar la app, extraer mensajes, bibliotecas no oficiales) queda fuera por principio.

## 0. Cómo leer las marcas

Cada afirmación lleva una marca y una fuente de la [lista final](#10-fuentes):

| Marca              | Significado                                                                                                                                                                                                                                                            |
| ------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **CONFIRMADO**     | Leído en una página oficial de TikTok que cargó completa.                                                                                                                                                                                                              |
| **OFICIAL-ÍNDICE** | Frase de una página oficial de la documentación del desarrollador (`business-api.tiktok.com/portal/docs/…`), vista en el texto que indexa el buscador. Esas páginas se generan con JavaScript y no se pudieron leer completas sin navegador; hay que releerlas a mano. |
| **TERCEROS**       | Solo lo dice la documentación de socios o integradores (respond.io, Chatwoot, Infobip…). Útil como indicio; no es la regla oficial.                                                                                                                                    |
| **SIN VERIFICAR**  | Ninguna fuente lo confirma, o las fuentes chocan.                                                                                                                                                                                                                      |
| **SUPUESTO**       | Deducción nuestra a partir de lo anterior.                                                                                                                                                                                                                             |

**Límite de esta investigación:** la documentación del desarrollador de TikTok (portal de TikTok API for Business) no se deja leer de forma automática: la página y su variante `gateway/docs/index` devuelven solo el título, y la copia de web.archive.org no estaba accesible. Ninguna regla de la ventana, del webhook ni del costo se pudo leer en su página oficial completa. Eso pesa en la conclusión.

## 1. Qué API oficial existe

- Existe la **Business Messaging API**, parte de **TikTok API for Business**. TikTok la describe como la forma de «integrate direct messaging capabilities to connect directly with TikTok users», con envío y recepción en tiempo real y respuestas automáticas. **CONFIRMADO** [T1]
- Es «a suite of interfaces enabling authorized Business Accounts to exchange direct messages with TikTok users through API-integrated platforms». **OFICIAL-ÍNDICE** [T3]
- La referencia incluye enviar un mensaje a una conversación (`/business/message/send/`), listar conversaciones y mensajes, subir imágenes y activar el «Comment-to-Message». **OFICIAL-ÍNDICE** [T4]
- **Está en beta.** El blog oficial de Messaging Ads (2025-06-03) dice: «TikTok Business Messaging API and Messaging Events API are currently in beta testing, with global availability coming soon». **CONFIRMADO** [T7]. Un socio la llama «Open Beta» en una página modificada el 2026-09-06. **TERCEROS** [P1]. No se encontró un anuncio oficial de disponibilidad general. **SIN VERIFICAR**
- Hay una segunda API, la **Messaging Events API**, que envía a TikTok señales de las conversaciones (id de seguimiento de la campaña, palabras clave como «pago») para medir los anuncios de mensajería; el contenido de los mensajes «remains private and is not shared with TikTok». **CONFIRMADO** [T7] [T8]. Solo importa si el negocio pauta Messaging Ads.

## 2. Cuentas y regiones (incluido Perú)

- **Tipo de cuenta:** «The Business Messaging API only supports TikTok Accounts with Advanced Access or Verified Business Accounts». **OFICIAL-ÍNDICE** [T2]. Una cuenta personal o «General» no sirve.
  - **Advanced Access:** se obtiene vinculando la cuenta de TikTok a una cuenta de TikTok for Business o a un Business Center (aunque no esté verificado). **CONFIRMADO** [T10]
  - **Verified Business Account (VBA):** se obtiene completando la verificación del negocio en Business Suite o en un Business Center verificado. **CONFIRMADO** [T10]
  - Un socio informa que desde abril de 2026 las cuentas no vinculadas a TikTok for Business o a un Business Center pasaron a «General» y perdieron el acceso a la API. **TERCEROS** (extracto del buscador) [P4]
- **Regiones excluidas por TikTok:** «The Business Messaging API is not yet available in the European Economic Area, Switzerland or the UK market». **OFICIAL-ÍNDICE** [T2]
- **Estados Unidos:** para gestionar cuentas registradas en EE. UU. hay que pasar la «US data security review». **OFICIAL-ÍNDICE** [T2]. Varios socios no la ofrecen en EE. UU. **TERCEROS** [P1] [P3]
- **Las regiones dependen de la revisión del desarrollador:** «Your approved reviews determine which regions your Business Messaging API integration can support». **OFICIAL-ÍNDICE** [T2]
- **Perú:**
  - Perú no aparece en ninguna exclusión oficial ni de socios. **OFICIAL-ÍNDICE** [T2] y **TERCEROS** [P1] [P3]
  - Un socio dice que está disponible en Asia-Pacífico, Oriente Medio, África y **Latinoamérica**. **TERCEROS** (extracto del buscador, Whautomate)
  - La verificación del negocio (VBA) está disponible en Perú («PE — Peru» en la tabla de mercados). **CONFIRMADO** [T11]
  - El envío de imágenes por la API no está disponible en una lista de países donde Perú no figura (sí Colombia, EE. UU., la UE…). **TERCEROS** [P1]
  - **SUPUESTO:** una cuenta de empresa registrada en Perú es elegible, **siempre que la revisión aprobada del desarrollador cubra Perú**. Ninguna fuente oficial nombra a Perú para esta API: **SIN VERIFICAR**.

## 3. ¿Hace falta ser socio o una aprobación?

- **Aprobación: sí.** «Business Messaging API developers are required to complete necessary reviews as a prerequisite for Business Messaging API access». **OFICIAL-ÍNDICE** [T2]. Un integrador (Chatwoot) lo confirma: hay que solicitar el producto en el portal del desarrollador y «You cannot proceed with the integration until your application is approved»; dice que suele tardar «a few days». **TERCEROS** [P2]
- **Ser socio (Marketing Partner): no consta como requisito.** Ninguna fuente lo exige para la API. Pero toda la comunicación oficial presenta la API como herramienta de los **Messaging Partners** («allows TikTok direct messages to be managed through messaging partners» [T7]; lista de socios con guía de conexión [T8]), y el distintivo «Messaging Partner» del programa de socios se lanzó en el sudeste asiático [T9]. **CONFIRMADO** en cuanto a ese enfoque.
- **SIN VERIFICAR (el punto decisivo):** si TikTok aprueba el acceso para un **CRM propio que gestiona una sola cuenta** (la de la tienda), o si en la práctica solo lo concede a plataformas que dan servicio a muchos negocios. Los criterios de la revisión no son públicos. Es la primera pregunta del [checklist](#8-qué-tendría-que-solicitar-el-propietario).
- **Autorización de la cuenta:** el negocio conecta su cuenta aprobando una solicitud de permisos del integrador, entre ellos «access and manage conversations» y suscribirse y «receive webhooks»; puede desconectarse en cualquier momento. **CONFIRMADO** [T8]

## 4. Ventana de mensajería y reglas

Ninguna regla de esta sección se pudo leer en una página oficial completa. Dos integradores independientes coinciden en todas, lo que las hace probables, no oficiales.

- **El cliente empieza siempre:** el negocio no puede iniciar conversaciones ni enviar difusiones a varios contactos. **TERCEROS** [P1] [P3]
- **Ventana de 48 horas** desde el último mensaje del cliente; lo que se envía fuera de ella «will not be delivered». **TERCEROS** [P1] [P2]
- **Tope dentro de la ventana:** hasta **10 mensajes seguidos**; un nuevo mensaje del cliente reinicia la ventana y el contador. **TERCEROS** [P1]. Otro socio habla de «3 mensajes adicionales» después de los 10. **SIN VERIFICAR** (extracto del buscador, Wati; las fuentes chocan)
- **No hay equivalente conocido** a la etiqueta de agente humano de Meta (ampliar la ventana para una persona). **SIN VERIFICAR**
- **Tipos de mensaje:** texto (hasta 6000 caracteres) e imágenes JPG o PNG de hasta 3 MB; las publicaciones de TikTok solo de entrada; video, voz y stickers no se envían. **TERCEROS** [P1]. Las imágenes solo funcionan si el emisor y el receptor están en países que las permiten. **TERCEROS** [P1]
- **Límite de tasa:** la página oficial de límites habla de niveles por app (por ejemplo, «Basic»: 10 consultas por segundo). **OFICIAL-ÍNDICE** [T6]. Un socio cita 10 QPS para la mensajería. **TERCEROS** [P1]
- **Ajuste de la cuenta:** el cliente solo puede escribir si la cuenta acepta mensajes de todos, o si un agente acepta cada solicitud a mano en la app. **TERCEROS** [P2]

## 5. Webhooks

- **Existe un webhook de la Business Messaging API** con el tipo de evento `DIRECT_MESSAGE`. Se configura por app con `/business/webhook/update/`, se lee con `/business/webhook/list/` (con el id y el secreto de la app) y se borra con `/business/webhook/delete/`. **OFICIAL-ÍNDICE** [T5]
- **Firma:** la plataforma TikTok for Developers firma sus webhooks con la cabecera `TikTok-Signature` (`t=<timestamp>,s=<firma>`), HMAC-SHA256 de `timestamp.cuerpo` con el secreto del cliente; el margen de tiempo lo decide el receptor. **CONFIRMADO** [T12], pero esa página **no menciona** TikTok API for Business: que la Business Messaging API use el mismo esquema es **SIN VERIFICAR**. Chatwoot verifica con el secreto de la app y exige relojes sincronizados («timestamps within 5 seconds»). **TERCEROS** [P2]
- **Entrega y reintentos:** la documentación general de webhooks de TikTok for Developers pide HTTPS y responder 200 enseguida, y reintenta hasta 72 horas con duplicados posibles (extracto del buscador). Aplicarlo a la Business Messaging API: **SIN VERIFICAR**.
- **SUPUESTO:** si se construye, entra por `webhook_ingress` como WhatsApp (verificación de la firma, idempotencia por id de mensaje, enrutado por la cuenta).

## 6. Costos

- **No se encontró ningún precio oficial** de la Business Messaging API. **SIN VERIFICAR**
- Un socio afirma que el canal se puede usar gratis. **TERCEROS** (extracto del buscador, respond.io)
- Los integradores cobran su propia tarifa (Infobip remite a su calculadora por país). **TERCEROS** [P3]. No aplica a un CRM propio.
- **SUPUESTO:** el costo real para la tienda sería el del desarrollo y la operación del canal, más los anuncios si decide pautar Messaging Ads. Confirmarlo con TikTok.

## 7. Impacto en el diseño (solo si un día se construye)

Sin ADR propio: el issue lo prohíbe salvo que la conclusión sea construir. Para dimensionar:

- Mismo patrón que [ADR-035](../adr/ADR-035-instagram-messenger-channels.md): adaptador detrás de `ChannelAdapter`, webhook firmado por `webhook_ingress`, cuenta `TIKTOK` con un token cifrado ([ADR-022](../adr/ADR-022-secrets-at-rest.md)), simulador para las pruebas.
- Identidad **sin teléfono**: el cliente llega con un id de TikTok, así que va a `contact_identities` sin fusión automática, como Instagram. Que la API dé teléfono o correo: **SIN VERIFICAR** (ninguna fuente lo menciona).
- Ventana de 48 h y tope de 10 mensajes en `MessagingPolicyService` ([ADR-010](../adr/ADR-010-messaging-policy.md)), distintos de las 24 h de Meta.
- Beta: el contrato puede cambiar sin aviso; el adaptador tendría que tolerar errores nuevos y la cuenta pasar a «Con problemas».

## 8. Qué tendría que solicitar el propietario

Nada de esto lleva valores en el repositorio. Primero las **preguntas a TikTok**, porque la respuesta a la primera decide si vale la pena el resto.

**Preguntas a TikTok** (por el soporte del portal del desarrollador o el representante comercial):

- [ ] ¿Aprueban el acceso a la Business Messaging API para un **CRM propio de una sola empresa** que gestiona solo su propia cuenta, o solo para plataformas o socios?
- [ ] ¿La revisión aprobada cubre **Perú**? ¿Qué revisiones hacen falta para Latinoamérica?
- [ ] ¿La API sigue en beta? ¿Hay fecha de disponibilidad general, acuerdo de servicio o aviso de cambios?
- [ ] La ventana exacta (¿48 h?), el tope de mensajes (¿10?, ¿3 adicionales?) y si existe una excepción para respuestas de una persona.
- [ ] Los eventos del webhook, el esquema de la firma (cabecera, secreto, margen de tiempo) y la política de reintentos.
- [ ] El costo por uso de la API, si lo hay.
- [ ] Si la API entrega teléfono o correo del cliente (se supone que no).
- [ ] Si hay cuentas de prueba o un entorno sandbox.
- [ ] Las condiciones de datos que debe firmar el desarrollador (retención, borrado, privacidad).

**Lo que habría que tramitar** (solo si las respuestas lo justifican):

- [ ] Una cuenta de empresa de TikTok para la tienda, vinculada a TikTok for Business o a un Business Center (**Advanced Access**) o, mejor, verificada (**VBA**, disponible en Perú [T11]).
- [ ] Registro como desarrollador en TikTok API for Business y una app de desarrollador a nombre del negocio.
- [ ] Solicitar el producto **Business Messaging API** para esa app y pasar sus revisiones, pidiendo expresamente Perú.
- [ ] La URL HTTPS del webhook del despliegue y la URL de la política de privacidad (las mismas obligaciones que con Meta).
- [ ] En la cuenta de TikTok: aceptar mensajes de todos.

## 9. Conclusión: ESPERAR

**Recomendación: esperar.** No construir ahora y no descartar.

Por qué no construir ahora:

1. **La aprobación no depende del negocio.** El acceso exige una revisión de TikTok cuyos criterios no son públicos [T2], y la comunicación oficial orienta la API a socios de mensajería [T7] [T8]. No se sabe si un CRM propio de una sola cuenta la obtendría (§3).
2. **Perú no está confirmado.** No está excluido, pero ninguna fuente oficial lo nombra, y las regiones dependen de la revisión aprobada [T2].
3. **Es beta** [T7] [P1], y las reglas que harían falta para diseñar (ventana, tope, firma del webhook) solo se conocen por socios (§4, §5).
4. **El orden ya decidido** pone a TikTok último, detrás de Instagram y Messenger (R3) y del resto de canales ([phase-7.md](../phases/phase-7.md), D-F7-09). Construir antes no adelanta nada para el negocio.

Por qué no descartar: la API es oficial, cubre cuentas de empresa fuera de la UE, Suiza, el Reino Unido y (con revisión) EE. UU., Perú no figura entre las exclusiones, la verificación del negocio existe en Perú y el patrón técnico ya existe en el repositorio (ADR-035).

**Pasa a «construir»** (con su propio ADR) solo si se cumplen las tres:

1. TikTok confirma por escrito que aprueba el acceso para el CRM propio de la tienda y que cubre Perú.
2. Alguien con navegador relee las páginas oficiales marcadas **OFICIAL-ÍNDICE** y confirma la ventana, el tope, los eventos y la firma del webhook.
3. La serie R3 (Instagram y Messenger) está cerrada.

**Pasa a «descartar»** si TikTok solo da acceso a plataformas o socios. Entonces queda una alternativa oficial que decide el propietario: conectar la cuenta a uno de los Messaging Partners listados por TikTok [T8], con su costo y un tercero con acceso a los mensajes. No se recomienda hoy, y nunca se sustituye por una vía no oficial.

## 10. Fuentes

Todas leídas el **2026-11-15**. Las fechas entre paréntesis son las que muestra cada página.

**Oficiales de TikTok**

- [T1] About API for Business (actualizada en septiembre de 2025): <https://ads.tiktok.com/help/article/marketing-api?lang=en>
- [T2] Access to Business Messaging API: <https://business-api.tiktok.com/portal/docs/access-to-business-messaging-api/v1.3> (no carga sin navegador; texto del índice del buscador)
- [T3] Business Messaging Overview: <https://business-api.tiktok.com/portal/docs/business-messaging-api-overview/v1.3> (ídem)
- [T4] Business Messaging API reference: <https://business-api.tiktok.com/portal/docs/business-messaging/v1.3> (ídem)
- [T5] Get a TikTok Business Messaging Webhook configuration: <https://business-api.tiktok.com/portal/docs/get-a-business-messaging-webhook-configuration/v1.3> (ídem)
- [T6] Rate limits Overview: <https://business-api.tiktok.com/portal/docs/rate-limits/v1.3> (ídem)
- [T7] Introducing TikTok Messaging Ads (2025-06-03): <https://ads.tiktok.com/business/en/blog/messaging-ads>
- [T8] About message management tools (actualizada en agosto de 2025): <https://ads.tiktok.com/help/article/about-message-management-tools?lang=en>
- [T9] Introducing the Messaging Partner specialty (2025-05-09): <https://ads.tiktok.com/business/en/blog/introducing-messaging-partner-specialty>
- [T10] About TikTok account entitlements (actualizada en mayo de 2026): <https://ads.tiktok.com/resources/help/article/about-tiktok-account-entitlements>
- [T11] About business verification for TikTok accounts (actualizada en junio de 2026): <https://ads.tiktok.com/resources/help/article/about-business-registration?lang=en>
- [T12] TikTok for Developers, Webhooks: Verification: <https://developers.tiktok.com/docs/en/webhooks-verification>

**Socios e integradores (no oficiales para las reglas de TikTok)**

- [P1] respond.io, TikTok overview (modificada el 2026-09-06): <https://respond.io/help/tiktok/tiktok-overview>
- [P2] Chatwoot, Setting up TikTok channel: <https://developers.chatwoot.com/self-hosted/configuration/features/integrations/tiktok>
- [P3] Infobip, TikTok: <https://www.infobip.com/docs/tiktok>
- [P4] respond.io, TikTok quick start (solo extracto del buscador): <https://respond.io/help/tiktok/tiktok-quick-start>

Las menciones a Whautomate y Wati son extractos del buscador sin leer la página: <https://whautomate.com/integrations/tiktok> y <https://support.wati.io/en/articles/15701340-how-to-connect-and-manage-tiktok-messages-dms-in-wati>.

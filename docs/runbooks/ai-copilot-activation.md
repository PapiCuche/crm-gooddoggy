# Activación del copiloto de IA: lista de producción y guía del propietario

Qué falta para que el copiloto del Inbox atienda conversaciones de clientes reales, quién lo hace y cómo se comprueba en el CRM. Work item [#1050](https://github.com/PapiCuche/crm-gooddoggy/issues/1050). **Este documento no enciende nada ni crea ninguna cuenta.**

- **Origen:** decisión del propietario del 2026-11-21 en [#771](https://github.com/PapiCuche/crm-gooddoggy/issues/771): la IA no se enciende para clientes reales hasta cumplir **siete condiciones**, y nunca envía respuestas por sí sola. El panel quedó terminado en [#772](https://github.com/PapiCuche/crm-gooddoggy/issues/772) (F8-09a, F8-09b y F8-09c).
- **Diseño:** [ADR-034](../adr/ADR-034-ai-copilot-first.md) (copiloto con aprobación humana), [ADR-043](../adr/ADR-043-ai-budget-time-zone-lima.md) (día y mes de Lima), [ADR-044](../adr/ADR-044-ai-budget-ledger-writer-definers.md) y [ADR-045](../adr/ADR-045-ai-budget-alert-events.md) (registro de gasto y avisos), [ADR-022](../adr/ADR-022-secrets-at-rest.md) (la clave cifrada), [ADR-023](../adr/ADR-023-mfa-totp.md) (segundo factor). Plan y detalle: [phase-8.md](../phases/phase-8.md).
- **Comprobado contra el código** de `main` en `f25e5b6c` (2026-11-22) y, para la pantalla de topes y modelo, contra [#1067](https://github.com/PapiCuche/crm-gooddoggy/issues/1067). Cada pantalla y ruta de este documento existe en `main` (sección 9). Si el código cambia, se revisa este documento.
- **Repositorio público.** Solo hay marcadores: `<SLUG>` (la organización), `<id>`. Ninguna clave, importe real, dominio, número, cuenta ni dato de clientes se escribe en el repositorio, en un issue, en un PR ni en un chat.

## 0. Lo que no cambia

- **La IA nunca envía.** El panel sugiere, resume y pide atención humana; quien atiende inserta la sugerencia, la edita y la envía con su usuario. No existe un botón ni una ruta que envíe la sugerencia (ADR-034 §9).
- **Nada sale hacia el proveedor si nadie lo pide.** Solo «Sugerir respuesta» y «Resumir», pulsados por una persona con `ai.use` en una conversación concreta, llaman al modelo. No hay llamadas en segundo plano.
- **Encender es difícil y apagar es fácil.** Encender pide `ai.manage`, el segundo factor reciente y confirmar que el aviso de datos está publicado; apagar lo hace cualquiera con `ai.kill_switch` o `ai.manage`, sin código (sección 3.11).
- **Dos interruptores.** `AI_ENABLED` en el servidor (apagado por omisión) y el interruptor de la organización en `/o/<SLUG>/ia` (apagado por omisión). La IA llama solo con los dos encendidos.

## 1. Quién hace qué

| Rol                                    | Hace                                                                                                                                                           | Necesita                                                                            |
| -------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------- |
| **Propietario** (Owner del CRM)        | Cuenta del proveedor, saldo y límite de gasto, clave, topes, texto de traspaso, publicar el aviso, encender la organización, las pruebas por canal y el **GO** | Owner con segundo factor activo; acceso de administrador a la consola del proveedor |
| **Asesoría legal** del propietario     | Revisa el aviso de datos ([borrador](../legal/aviso-ia-tratamiento-datos-borrador.md), Parte 3)                                                                | El borrador y los datos de la empresa, fuera del repositorio                        |
| **Quien despliega**                    | `AI_ENABLED=true` en el entorno del servidor y el redeploy (sección 3.10); el respaldo de apagado                                                              | Acceso al servidor y a su almacén de secretos                                       |
| **Programa autónomo / equipo técnico** | Cambios por PR (topes por persona y organización, modelos permitidos, retención). **Nunca** crea cuentas, guarda claves, enciende la IA ni da el GO            | —                                                                                   |

## 2. Lista de producción: las siete condiciones

Estado de la columna «Código»: lo que ya está en `main`. «Falta» es lo que hace una persona; ninguna condición se da por cumplida porque el código exista.

| #   | Condición                                     | Quién                  | Cómo se verifica en el CRM                                                                                                                                                                     | Código (en `main`)                                                                                                                           | Falta                                                                     |
| --- | --------------------------------------------- | ---------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------- |
| 1   | Aviso de datos aprobado y publicado           | Propietario y asesoría | Al encender en `/o/<SLUG>/ia`, la casilla del aviso; auditoría `ai.kill_switch.deactivated` con `notice_confirmed: true`                                                                       | Hecho: sin la confirmación no se enciende (400)                                                                                              | Completar, revisar y publicar el aviso                                    |
| 2   | Cuenta del proveedor configurada por el dueño | Solo el propietario    | `/o/<SLUG>/ia`: proveedor «Anthropic», estado «Activa» y los cuatro últimos caracteres; auditoría `ai.account.created`                                                                         | Hecho: una cuenta por organización y proveedor                                                                                               | Crear la cuenta en el proveedor (fuera del CRM)                           |
| 3   | Saldo y presupuesto                           | Propietario            | Saldo y límite de gasto en la consola del proveedor; los dos topes en `/o/<SLUG>/ia/presupuesto` (pestaña «Presupuesto»; `GET/PUT /api/v1/o/<SLUG>/ai/budget/`); auditoría `ai.limits.changed` | Hecho: topes mensual y diario con corte duro, en la pantalla «Presupuesto» ([#1067](https://github.com/PapiCuche/crm-gooddoggy/issues/1067)) | Cargar saldo, fijar el límite en la consola y los dos topes del CRM (3.5) |
| 4   | Credenciales seguras con segundo factor       | Propietario            | Segundo factor activo en `/cuenta/seguridad`; la clave se guarda en `/o/<SLUG>/ia` tras «Confirmar con mi código»; «Probar conexión» → `OK` una vez encendido `AI_ENABLED` (3.10, paso 1)      | Hecho: `ai.manage` sensible y segundo factor de 15 minutos en cada ruta de la clave                                                          | Activar el segundo factor del Owner y el de la consola del proveedor      |
| 5   | Traspaso a una persona validado               | Propietario            | `/o/<SLUG>/ia/traspaso`; en el Inbox, el aviso de traspaso y «Pide atención humana»; auditoría `conversation.human_requested`                                                                  | Hecho (F8-08l, F8-08n, F8-09b, F8-09c)                                                                                                       | Revisar el texto y validarlo en la prueba real (sección 6)                |
| 6   | Pruebas reales controladas por canal          | Propietario            | Una conversación de prueba por canal en `/o/<SLUG>/inbox`; auditoría `ai.suggestion.created`, `ai.suggestion.inserted`, `ai.summary.created`                                                   | Hecho: el flujo completo                                                                                                                     | Ejecutarlas y anotar el resultado sin datos reales                        |
| 7   | GO explícito                                  | Solo el propietario    | Un comentario del propietario en GitHub y, después, `ai.use` concedido a los roles en `/o/<SLUG>/roles` (`role.permission_granted`)                                                            | No aplica                                                                                                                                    | Darlo                                                                     |

**Orden recomendado:** 2 → 4 (guardar la clave) → 3 (los topes, 3.5) → `AI_ENABLED` en el servidor (3.10, paso 1) → «Probar conexión» → `OK` (cierra la 4) → 1 → encender la organización → 5 y 6 con solo el Owner usando el copiloto → 7. «Probar conexión» necesita `AI_ENABLED` encendido; las condiciones 5 y 6 necesitan la organización encendida, y encenderla exige la 1.

### 2.1 Aviso de datos (condición 1)

- **Qué:** el aviso a los clientes sobre qué datos salen a un proveedor de IA, para qué y la transferencia fuera del país (D-F8-04, Ley N.º 29733). El borrador técnico, comprobado contra el código, es [docs/legal/aviso-ia-tratamiento-datos-borrador.md](../legal/aviso-ia-tratamiento-datos-borrador.md): su Parte 3 es la lista de lo que el propietario y su asesoría confirman antes de publicar.
- **En el CRM:** al pulsar «Encender la IA» en `/o/<SLUG>/ia` hay que marcar «Confirmo que el aviso de tratamiento de datos está publicado…». La ruta `POST /api/v1/o/<SLUG>/ai/switch/on/` exige `notice_confirmed: true` exactamente; la auditoría `ai.kill_switch.deactivated` guarda esa confirmación. Es una declaración: **márcala solo después de publicarlo.**
- **Antes de publicar**, quitar o corregir las frases que hoy no son ciertas (borrador, Parte 3): la eliminación del detalle a los 12 meses está apagada (`AI_RECORDS_RETENTION = False`, sección 4). La oposición por conversación existe en el backend desde F8-13 (#1071): `PUT /api/v1/o/<SLUG>/conversations/<id>/ai-exclusion/` con `{"excluded": true, "version": <la del detalle>}` (quien ve la conversación); volver a incluirla pide `ai.manage` y el segundo factor. Con la marca, sugerir y resumir responden 409 `AI_EXCLUDED` sin leer la conversación ni llamar al proveedor, y el panel no los ofrece. El botón para marcarla desde el Inbox es un corte aparte; el aviso (F8-14) describe el procedimiento.

### 2.2 Cuenta del proveedor (condición 2)

Solo la crea el propietario, a nombre de la empresa (sección 3.2). En el CRM se comprueba en `/o/<SLUG>/ia`: la lista muestra el proveedor, el estado y los cuatro últimos caracteres de la clave; nunca la clave. `GET /api/v1/o/<SLUG>/ai/provider-accounts/` devuelve además el `model` de la cuenta.

### 2.3 Saldo y presupuesto (condición 3)

Dos barreras independientes: el **límite de gasto en la consola del proveedor** (D-F8-09) y los **topes del CRM** (mensual y diario, en USD). Sin los dos topes del CRM no hay llamadas (`AI_BUDGET_NOT_SET`). Pasos en 3.3 y 3.5.

### 2.4 Credenciales con segundo factor (condición 4)

- El Owner tiene el segundo factor activo en `/cuenta/seguridad` (ADR-023). Sin él, «Confirmar con mi código» no funciona: no se puede ver ni cambiar la clave, el modelo, los topes ni el texto de traspaso, ni marcar qué listas de precios puede citar la IA, ni encenderla. Ver las listas de precios, el estado de la IA y «Apagar la IA» no piden código.
- La cuenta de la consola del proveedor también tiene segundo factor: quien entra ahí puede crear o revocar claves y cambiar el método de pago.
- La clave se guarda solo en `/o/<SLUG>/ia` (sección 3.4). Con `AI_ENABLED` ya encendido en el servidor (3.10, paso 1), «Probar conexión» devuelve `OK` y deja `ai.account.tested`.

### 2.5 Traspaso validado (condición 5)

1. En `/o/<SLUG>/ia/traspaso`, leer el texto que propondrá el copiloto. Mientras la organización no guarde uno propio, es el inicial que decidió el propietario (`AI_HANDOFF_REPLY`); cambiarlo deja `ai.handoff_reply.changed` (sección 3.8).
2. En la prueba real (sección 6), el cliente de prueba escribe que quiere hablar con una persona y quien atiende pulsa «Sugerir respuesta». Se espera:
   - el aviso «El cliente pidió hablar con una persona.» y «Se pidió atención humana para esta conversación; al cliente no se le envía nada.»;
   - la sugerencia es el texto de traspaso, sin cifras ni promesas de atención inmediata;
   - la conversación muestra «Pide atención humana» y la auditoría tiene `conversation.human_requested` con el motivo `ASKED_FOR_PERSON`.
3. Insertar y **enviarlo sin cambiarlo**: cualquier cambio (salvo espacios) cuenta como respuesta y cierra la solicitud. Comprobar que **enviar el texto de traspaso tal cual no la cierra**. Se cierra cuando una persona responde otra cosa, la toma o se la transfieren, o la conversación se resuelve (`conversation.human_request_closed`).
4. Probar también «Solicitar atención humana» sin el copiloto (motivo `AGENT`) y «Cerrar la solicitud de atención humana».

### 2.6 Pruebas reales por canal (condición 6)

Sección 6.

### 2.7 GO explícito (condición 7)

Solo el propietario lo da, cuando las seis anteriores están hechas. Se deja por escrito en un comentario suyo en GitHub (por ejemplo, en el checklist de lanzamiento [#781](https://github.com/PapiCuche/crm-gooddoggy/issues/781)), sin ningún dato real: «GO copiloto de IA, fecha, canales probados». Después, el propietario concede `ai.use` a los roles que deban usarlo (sección 3.9). Ningún agente lo da por él ni lo deduce de otro mensaje.

## 3. Guía del propietario, paso a paso

### 3.1 Antes de empezar

- Entra al CRM como Owner y comprueba que tu segundo factor está activo en `/cuenta/seguridad`.
- La pantalla de la IA es `/o/<SLUG>/ia` («Inteligencia artificial» en el menú). Tiene tres pestañas: «Proveedor y estado» (`/o/<SLUG>/ia`: el estado de la IA, la clave y el modelo), «Texto de traspaso» (`/o/<SLUG>/ia/traspaso`) y «Presupuesto» (`/o/<SLUG>/ia/presupuesto`: los topes). Las tres piden `ai.manage` y el código de segundo factor («Confirmar con mi código»); la confirmación vale 15 minutos para toda la sesión.

### 3.2 Crear la cuenta del proveedor (fuera del CRM)

- **Solo el propietario**, en la consola de Anthropic (Claude Console), a nombre de la empresa, con su método de pago y saldo. El programa nunca crea cuentas.
- **Nunca** una suscripción personal, las credenciales de Claude Code, un perfil OAuth ni la clave de un desarrollador: el CRM usa solo la clave de la propia organización, guardada en su pantalla (ADR-034 §3).
- Activa el segundo factor de la consola.
- Revisa los términos de la cuenta (uso del contenido para entrenar, plazo de conservación del proveedor, su papel en el tratamiento): el aviso de datos los necesita (sección 7).

### 3.3 Fijar el límite de gasto en la consola del proveedor (D-F8-09)

- En la consola del proveedor, fija un límite de gasto mensual. Es la segunda barrera, independiente del CRM: si el CRM fallara, el proveedor corta.
- Recomendación: igual o algo por encima del tope mensual del CRM (3.5), para que normalmente corte el CRM con su mensaje claro.
- Cuando el proveedor corta por saldo o por ese límite, el CRM lo recibe como `PROVIDER_BILLING`: «Probar conexión» lo dice («El proveedor rechazó la llamada por facturación o por el límite de gasto»), y en el Inbox el panel dice «El copiloto no pudo responder». No se reintenta.

### 3.4 Crear la clave y guardarla en el CRM (segundo factor)

1. En la consola del proveedor, crea una clave de API para el CRM. Cópiala **solo** al portapapeles: nunca en un correo, chat, issue, PR ni documento. Después de pegarla en el CRM, borra el portapapeles (copia cualquier otro texto). Nunca hagas capturas de pantalla de la consola de desarrollo del navegador.
2. En `/o/<SLUG>/ia`, «Confirmar con mi código» y después «Guardar clave de Anthropic». Pega la clave y pulsa «Guardar». El campo se vacía; la clave no se vuelve a mostrar ni se guarda en el navegador. Auditoría: `ai.account.created`.
3. «Probar conexión»: hace la llamada más barata del proveedor (listar modelos, sin tokens) y devuelve solo un código: `OK`, `CREDENTIALS_REJECTED`, `PROVIDER_BILLING` o `PROVIDER_UNAVAILABLE`. Auditoría: `ai.account.tested`. **Hazlo después de que quien despliega encienda `AI_ENABLED`** (3.10, paso 1). Antes, la API responde `AI_DISABLED` sin probar nada y la pantalla lo dice: «La IA está apagada en todo el sistema: quien despliega el CRM debe encenderla antes de probar la conexión. No es un problema de la clave.»
4. Más adelante: «Rotar clave» sustituye la clave (`ai.account.key_rotated`; la anterior deja de valer en el CRM, revócala también en la consola) y «Borrar clave» la destruye y desactiva la cuenta (`ai.account.disabled`).
5. Si una clave se expuso: **revócala en la consola del proveedor** (efecto inmediato, aunque el CRM no responda) y guarda otra con «Rotar clave».

Rutas: `GET/POST /api/v1/o/<SLUG>/ai/provider-accounts/`, `POST …/ai/provider-accounts/<id>/key/`, `DELETE …/ai/provider-accounts/<id>/`, `POST …/ai/provider-accounts/<id>/test/`. Todas piden `ai.manage` y el segundo factor reciente; las escrituras cuentan en `AI_ACCOUNT_THROTTLE` (sección 4).

### 3.5 Fijar los topes mensual y diario

Sin los dos topes la IA no responde: falla cerrado con `AI_BUDGET_NOT_SET` y el panel dice «La IA aún no tiene presupuesto».

1. En `/o/<SLUG>/ia/presupuesto` (pestaña «Presupuesto»), «Confirmar con mi código» si la pantalla lo pide. Arriba se ven los topes vigentes («Vigentes: … al mes y … al día») o «Todavía no hay topes».
2. Escribe el «Tope mensual (USD)» y el «Tope diario (USD)»: solo números, con **punto** para los decimales (por ejemplo, `50` o `12.50`; una coma se rechaza para no confundir «1,000» con uno).
3. «Guardar topes». Si el código caducó, la pantalla lo vuelve a pedir y guarda una sola vez. Al terminar dice «Los topes quedaron guardados» y muestra los nuevos. Auditoría: `ai.limits.changed`, con los topes de antes y de después.
4. Comprueba que siguen ahí al recargar la página.

Lo que valida la pantalla antes de enviar, y la ruta `GET/PUT /api/v1/o/<SLUG>/ai/budget/` (`ai.manage` y segundo factor reciente) siempre:

- Importes en USD, mayores que cero, con 6 decimales como mucho y por debajo de un millón; el diario no puede superar al mensual. Los dos son obligatorios. Cada problema se explica junto a su campo.
- El día y el mes son los de **Lima** (ADR-043).
- Bajar un tope por debajo de lo ya gastado vale: la siguiente llamada se corta.
- Guardar cuenta en `AI_ACCOUNT_THROTTLE` (sección 4); guardar los mismos importes no envía nada.
- **Lo gastado no se ve en el CRM**: la ruta no lo devuelve. Se revisa en la consola del proveedor (3.3).

### 3.6 Elegir el modelo

- La lista permitida (`AI_MODELS` en [base.py](../../backend/config/settings/base.py)) tiene hoy **un solo modelo**, `claude-sonnet-5-5`, con sus precios de la página oficial (consultada el 2026-11-17). Si nadie elige otro, se usa ese: al guardar la clave, la cuenta nace con el modelo de `AI_PROVIDER_PARAMS["ANTHROPIC"]["model"]` (`claude-sonnet-5-5`), y cada llamada usa el modelo guardado en la cuenta activa. No hay que elegir nada.
- Añadir un modelo o cambiar un precio es un **PR** revisado con la fecha de consulta (D-F8-03); los «Covered Models» de retención obligatoria no entran sin decisión expresa (ADR-034 §3).
- El modelo de la cuenta se ve en `/o/<SLUG>/ia`, en la tarjeta de la cuenta («Modelo»). Para cambiarlo: «Cambiar modelo», escribe el identificador exacto y «Guardar modelo» (segundo factor reciente; ruta `PUT /api/v1/o/<SLUG>/ai/provider-accounts/<id>/model/`, `ai.manage`; auditoría `ai.account.model_changed`). La pantalla no ofrece una lista porque la API no la publica: un modelo fuera de la lista se rechaza con «Ese modelo no está en la lista permitida del CRM». Guardar el mismo modelo no envía nada.

### 3.7 Qué listas de precios puede citar la IA (D-F8-06)

En `/o/<SLUG>/precios/listas`, «La IA puede citar sus precios» en la lista general al público (`ai.manage` y segundo factor; ruta `PUT /api/v1/o/<SLUG>/pricing/price-lists/<id>/ai-exposable/`). Sin ninguna lista marcada, la IA no da precios: los marca `[verificar]`. La lista interna nunca se puede marcar. Auditoría: `price_list.ai_exposable_changed`.

### 3.8 Configurar el texto de traspaso

En `/o/<SLUG>/ia/traspaso`: «Confirmar con mi código», editar y «Guardar texto» (`GET/PUT /api/v1/o/<SLUG>/ai/handoff-reply/`; auditoría `ai.handoff_reply.changed`, solo con la longitud). Reglas: hasta 500 caracteres, solo texto visible en alfabeto latino, sin cifras, plazos, precios, ofertas, stock, condiciones ni enlaces, y **sin prometer atención inmediata** («enseguida», «pronto», «en unos minutos»…). La pantalla explica cada problema. Mientras no guardes uno, se usa el inicial.

### 3.9 Quién usa el copiloto (`ai.use`)

- Al nacer, solo el **Owner** tiene `ai.use` (D-F8-08). Mantenlo así durante las pruebas (sección 6).
- Con el GO (2.7), concédelo en `/o/<SLUG>/roles` a los roles que deban usarlo (recomendado: Supervisor y después Vendedor). Auditoría: `role.permission_granted`.

### 3.10 Encender la IA

1. **Quien despliega** pone `AI_ENABLED=true` en `runtime.env`, el entorno de los procesos del runtime en `CRM_ENV_DIR` (plantilla: [.env.production.example](../../infra/prod/.env.production.example)), y vuelve a desplegar el release que ya está en marcha: `sudo infra/prod/deploy.sh /etc/crm/release.env` ([infra/prod/README.md](../../infra/prod/README.md), «Desplegar»; repetir el mismo release es seguro). `deploy.sh` siempre recibe un `release.env`: para un cambio solo de entorno no hace falta uno nuevo. Un valor que no sea booleano no arranca. El host del proveedor entra solo en la lista de salida (`AI_PROVIDER_HOSTS`). Con esto la organización **sigue apagada**: solo permite probar la clave (3.4, paso 3).
2. Con las condiciones 1 a 4 cumplidas, el Owner, en `/o/<SLUG>/ia`, pulsa «Encender la IA», marca la confirmación del aviso y «Sí, encender» (segundo factor reciente). Auditoría: `ai.kill_switch.deactivated` con `notice_confirmed: true`. Si `AI_ENABLED` sigue apagado, la pantalla lo dice y la IA no funciona.

### 3.11 Apagar todo (kill switch)

| Freno                            | Quién                                                  | Efecto                                                                                                             |
| -------------------------------- | ------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------ |
| «Apagar la IA» en `/o/<SLUG>/ia` | `ai.kill_switch` o `ai.manage`, **sin** segundo factor | La siguiente llamada recibe `AI_DISABLED`; la que ya estaba en curso termina. Auditoría `ai.kill_switch.activated` |
| Revocar la clave en la consola   | El propietario                                         | Inmediato, aunque el CRM no responda; después, «Rotar clave»                                                       |
| `AI_ENABLED=false` y redeploy    | Quien despliega                                        | Apaga la IA en todo el sistema; pide reiniciar: es el respaldo, no el freno rápido                                 |

- `ai.kill_switch` lo tienen el Owner y las plantillas Administrador y Supervisor de las organizaciones creadas desde F8-06. En una organización anterior, solo el Owner lo recibe; el Owner puede concederlo en `/o/<SLUG>/roles` (ADR-018 §3): **compruébalo antes de encender**. Darlo al Supervisor tiene efectos ([phase-8.md](../phases/phase-8.md), F8-06a): el Supervisor pasa a tener un permiso sensible, así que un Administrador que no es Owner ya no puede asignar ni quitar ese rol, suspender o reactivar a un Supervisor, cambiar su sucursal ni invitar con ese rol; y el Supervisor necesitará segundo factor si `MFA_ENFORCEMENT=enforce` (hoy `warn`).
- Quien solo tiene `ai.kill_switch` ve en `/o/<SLUG>/ia` solo el estado y el botón de apagar.
- Apagar la IA no afecta a los canales, a los webhooks ni al Inbox: se sigue respondiendo sin el copiloto.
- Ruta: `POST /api/v1/o/<SLUG>/ai/switch/off/` (no cuenta en ningún tope). Estado: `GET /api/v1/o/<SLUG>/ai/switch/`.

### 3.12 Lo que ve quien atiende en el Inbox

En `/o/<SLUG>/inbox`, con `ai.use`, en una conversación abierta en la que puede responder, el panel «Copiloto de IA»:

- **«Sugerir respuesta»**: prepara una sugerencia. Lo que no tiene respaldo en los datos (precio, stock, descuento, plazo, condición, enlace) llega como **`[verificar]`**. «Insertar» la pone en el cuadro de respuesta (`ai.suggestion.inserted`); se **edita** y la **envía quien atiende**, con el envío normal. El servidor rechaza enviar un texto con `[verificar]`. «Descartar» quita la sugerencia del panel y no añade nada más: la sugerencia ya quedó guardada al pedirla (`ai.suggestion.created`) y su texto se conserva 30 días.
- **«Resumir»**: un resumen con la etiqueta «Generado por IA», **solo para leer**: no se inserta ni se envía.
- **Traspaso**: si el cliente pide una persona o parece molesto, el aviso, la sugerencia del texto de traspaso y la solicitud de atención humana registrada; nada sale al cliente. Para que la solicitud siga abierta, el texto de traspaso se envía sin cambiarlo: cualquier cambio (salvo espacios) cuenta como respuesta y la cierra.
- **«Solicitar atención humana»**: quien atiende la pide a mano (motivo `AGENT`); la conversación muestra «Pide atención humana».
- **Errores en el panel**: IA apagada («Puedes responder sin el copiloto»), sin configurar, sin presupuesto, presupuesto agotado, tope por persona (`RATE_LIMITED`) o de la organización (`AI_ORG_RATE_LIMITED`), plazo agotado (`AI_DEADLINE`), datos caducados (`AI_EVIDENCE_EXPIRED`: pedir otra) y error del proveedor.

## 4. Límites y costos configurados en `main`

Todos están en [backend/config/settings/base.py](../../backend/config/settings/base.py) salvo que se diga otra cosa, y **se cambian por PR** (no por entorno). Dónde se valida cada uno: los tres topes de peticiones, al arrancar en producción ([env.py](../../backend/config/env.py)); el plazo, las tools por consulta, los mensajes que lee y «pocas unidades», al arrancar en cualquier entorno (`AiAgentsConfig.ready()` en [apps/ai_agents/apps.py](../../backend/apps/ai_agents/apps.py)) y en cada llamada; `AI_EVIDENCE_TTL`, solo en cada llamada (el guard falla cerrado). Un valor fuera de rango no arranca o hace fallar la llamada.

| Qué                                  | Ajuste                                                              | Valor en `main`                                                                                                                                                        | Notas                                                                                                                                                                                                                                                   |
| ------------------------------------ | ------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Tope por persona                     | `AI_COPILOT_THROTTLE`                                               | `(20, 10 * 60)`: 20 cada 10 minutos (**provisional**, #771)                                                                                                            | Un contador para sugerir y resumir; cuenta antes del permiso; insertar no cuenta. 429 `RATE_LIMITED` con `Retry-After`. Producción: 1 a 60 peticiones, ventana de 60 a 840 s                                                                            |
| Tope por organización                | `AI_COPILOT_ORG_THROTTLE`                                           | `(120, 10 * 60)`: 120 cada 10 minutos (**provisional**, por confirmar)                                                                                                 | Cuenta tras el permiso; se devuelve solo si la conversación está fuera de alcance (404). Las negativas por IA apagada, presupuesto o proveedor **sí cuentan**. 429 `AI_ORG_RATE_LIMITED`. Producción: del tope por persona a 600, ventana de 60 a 840 s |
| Tope de escrituras de administración | `AI_ACCOUNT_THROTTLE`                                               | `(10, 10 * 60)` por persona                                                                                                                                            | Guardar, rotar, borrar y probar la clave, cambiar el modelo, fijar los topes, encender, marcar listas y el texto de traspaso. Apagar y leer no cuentan                                                                                                  |
| Presupuesto                          | `ai_budgets` (por organización, en la base)                         | Sin fila: ninguna llamada (`AI_BUDGET_NOT_SET`)                                                                                                                        | Mensual y diario en USD, día y mes de Lima; corte duro (`AI_BUDGET_EXHAUSTED`). Se fija en `/o/<SLUG>/ia/presupuesto` (3.5)                                                                                                                             |
| Reserva por llamada                  | `AI_TOOL_TOKENS`, `AI_PROVIDER_PARAMS`, `AI_MODELS`                 | 1 000 tokens de margen; `max_tokens` 2 048                                                                                                                             | Antes de cada llamada se reserva una cota superior: los bytes de todo lo enviado más el margen, al precio de entrada más alto, y `max_tokens` al de salida. Se ajusta al uso real; lo dudoso cuenta como gastado                                        |
| Reservas huérfanas                   | Tarea `ai_gateway.close_orphan_reservations`                        | Cada 300 s; cierra las de más de 15 minutos                                                                                                                            | Las cierra como **gastadas**, nunca las libera                                                                                                                                                                                                          |
| Avisos de gasto                      | `ai_budget_reserve` (ADR-045)                                       | Al 80 % del mes, y al corte del tope **mensual** solo si el mes ya iba por el 80 % o más; una vez por mes de Lima. El corte del tope **diario** no emite ningún evento | Eventos `ai.budget.threshold_reached` y `ai.budget.exceeded` en el outbox. Cada uno deja la auditoría `ai.budget.alerted` y un correo, sin importes y con el enlace a `/o/<SLUG>/ia`, a cada miembro activo con `ai.manage` (el Owner y quien tenga un rol con ese permiso); sin correo configurado, solo la auditoría ([#1068](https://github.com/PapiCuche/crm-gooddoggy/issues/1068))                                                       |
| Plazo de una consulta                | `AI_COPILOT_DEADLINE`                                               | 50 s (entero de 20 a 55)                                                                                                                                               | Por debajo del tiempo límite del proxy. 504 `AI_DEADLINE`. El del proveedor, `AI_PROVIDER_PARAMS["ANTHROPIC"]["seconds"]` = 60, nunca se supera                                                                                                         |
| Tools por consulta                   | `AI_COPILOT_TOOL_CALLS`                                             | 5 (entero de 1 a 10)                                                                                                                                                   | Pasado el tope, la tool responde `TOOL_LIMIT`; como mucho las tools más 3 llamadas al modelo (incluido un reintento)                                                                                                                                    |
| Mensajes que lee                     | `AI_COPILOT_CONTEXT_MESSAGES`                                       | 20 (entero de 1 a 50)                                                                                                                                                  | Solo los visibles para el cliente, enmascarados; nunca las notas internas                                                                                                                                                                               |
| Validez de precio y stock            | `AI_EVIDENCE_TTL`                                                   | 600 s                                                                                                                                                                  | Pasado ese tiempo, insertar responde `AI_EVIDENCE_EXPIRED`                                                                                                                                                                                              |
| Stock «pocas unidades»               | `AI_STOCK_FEW_UNITS`                                                | 3                                                                                                                                                                      | El stock se cita como franja (D-F8-07)                                                                                                                                                                                                                  |
| Texto de los borradores              | `AI_SUGGESTION_TEXT_DAYS` y tarea `ai_agents.purge_suggestion_text` | 30 días; a diario a las 04:37 UTC                                                                                                                                      | Pone a NULL el texto de sugerencias y resúmenes entre los 30 y 31 días; quedan los ids y los hallazgos                                                                                                                                                  |
| Detalle de la IA                     | `AI_RECORDS_RETENTION`                                              | `False` (**apagado**)                                                                                                                                                  | Encendido, el job de migraciones quita las particiones de `ai_llm_calls`, `ai_runs` y `ai_actions` de más de 12 meses completos. Encenderlo es un PR con la decisión del propietario                                                                    |
| Interruptor global                   | `AI_ENABLED` (entorno)                                              | Apagado                                                                                                                                                                | Lo cambia quien despliega, con redeploy (3.10)                                                                                                                                                                                                          |

## 5. Probar en local sin proveedor real

- **No hay todavía un simulador del proveedor dentro del repositorio para el stack local.** Proponerlo necesita una ADR que el propietario debe aceptar ([#1061](https://github.com/PapiCuche/crm-gooddoggy/issues/1061)): ADR-041 solo admite la excepción de red del simulador de Meta. La prueba de punta a punta del panel (PR #1060) usó una herramienta fuera del repositorio.
- **El principio** de esa prueba: un servidor local que responde con el formato documentado de la API del proveedor (respuestas y errores fijos), alcanzable solo desde el stack local, con una clave ficticia; nunca una clave real, nunca la red del proveedor y nunca configurable en producción.
- **Lo que sí está en el repositorio:**
  - Las pruebas del backend `backend/tests/test_ai_*.py` usan [backend/tests/ai_sim.py](../../backend/tests/ai_sim.py): una API falsa del proveedor, sin red, que solo sustituye la capa de red de `core.http` y reproduce cada error documentado. Se ejecutan como dice [backend/README.md](../../backend/README.md).
  - Las pruebas del frontend de las pantallas y del panel (`frontend/src/components/ai/*.test.tsx`, `frontend/src/components/inbox/copilot.test.tsx` y `human-request.test.tsx`), con la API simulada.
- **En el stack local** ([local-stack.md](local-stack.md)) se recorren sin llamar a nadie: las pantallas `/o/<SLUG>/ia`, `/o/<SLUG>/ia/traspaso` y `/o/<SLUG>/ia/presupuesto` (fijar topes y ver el modelo), el aviso «apagada en todo el sistema» (`AI_ENABLED=false`, el valor de [infra/env/.env.example](../../infra/env/.env.example)), apagar y encender la organización, marcar listas en `/o/<SLUG>/precios/listas`, los permisos por rol y, en el Inbox, los mensajes `AI_DISABLED` y `AI_NOT_CONFIGURED` del panel, que salen antes de cualquier llamada. **No guardes una clave inventada con un presupuesto**: la siguiente sugerencia saldría hacia el host real del proveedor.

## 6. Prueba real controlada por canal

Objetivo: validar el copiloto y el traspaso **sin escribir a ningún cliente**. Se hace con la organización encendida (3.10), así que antes deben estar cumplidas las condiciones 1 a 4.

1. **Solo el Owner tiene `ai.use`** (`/o/<SLUG>/roles`). Nadie más puede pedir sugerencias ni resúmenes, y el copiloto solo trata la conversación en la que el Owner pulsa un botón.
2. **Una cuenta de prueba del propio propietario** escribe al canal: para WhatsApp, el número de prueba y un destinatario del propietario ([whatsapp-activation.md](whatsapp-activation.md), §2); para Messenger e Instagram, una cuenta personal del propietario o de alguien del equipo que lo acepte. Nunca un cliente.
3. **Una sola conversación por canal**, abierta en `/o/<SLUG>/inbox`. El Owner no pide sugerencias en conversaciones de clientes reales hasta el GO.
4. En cada conversación de prueba, sin escribir datos personales reales:
   - una pregunta de producto, precio o stock → «Sugerir respuesta»: lo que no tiene respaldo sale `[verificar]`; insertar, editar y **enviar a mano**; comprobar que no se puede enviar con la marca;
   - «Resumir» → «Generado por IA», solo lectura;
   - «quiero hablar con una persona» → el traspaso de 2.5;
   - «Apagar la IA» en `/o/<SLUG>/ia` → el panel dice que la IA está apagada; volver a encender.
5. Comprobar en `/o/<SLUG>/auditoria` (o con `GET /api/v1/o/<SLUG>/audit/?action=<acción>`, con `audit.view`): `ai.suggestion.created`, `ai.suggestion.inserted`, `ai.summary.created`, `conversation.human_requested`, `ai.kill_switch.activated` y `ai.kill_switch.deactivated`. Las acciones `ai.*` y `conversation.*` aún no están en el filtro de la pantalla: aparecen en la lista con su código.
6. Comprobar el gasto en la consola del proveedor y que no pasa de lo esperado para unas pocas consultas.
7. Anotar el resultado por canal en GitHub (sección 2.7) **sin datos reales**: canal, fecha, `OK` o qué falló.

## 7. Decisiones pendientes del propietario

| Tema                                                                                          | Hoy en `main`                                                                                                                                                                                                                         | Dónde se decide                                                                                                                                             |
| --------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Tope por organización                                                                         | `AI_COPILOT_ORG_THROTTLE` = 120 cada 10 minutos, provisional                                                                                                                                                                          | Confirmar o dar otro valor; se aplica por PR                                                                                                                |
| Tope por persona                                                                              | `AI_COPILOT_THROTTLE` = 20 cada 10 minutos, confirmado «provisionalmente» el 2026-11-21                                                                                                                                               | Confirmarlo como definitivo o cambiarlo                                                                                                                     |
| ¿Cuentan en el tope de la organización las negativas por presupuesto, IA apagada o proveedor? | Sí: solo se devuelve el 404 (fuera de alcance)                                                                                                                                                                                        | Decidir si se devuelven también                                                                                                                             |
| Excepción de la frase «Tu conversación quedará disponible»                                    | Admitida solo en el texto de traspaso y solo al empezar una frase; el detector de disponibilidad sigue activo para todo lo demás                                                                                                      | Aceptar esa excepción o cambiar el texto inicial ([phase-8.md](../phases/phase-8.md), F8-08l)                                                               |
| Retención de `ai_budget_reservations` y de `ai_suggestions` sin texto                         | Sin plazo                                                                                                                                                                                                                             | Fijar un plazo y pedir el work item (borrador del aviso, Parte 3)                                                                                           |
| Eliminar el detalle a los 12 meses                                                            | `AI_RECORDS_RETENTION = False`                                                                                                                                                                                                        | Encenderlo por PR cuando lo confirme con datos reales                                                                                                       |
| Oposición al uso de la IA por conversación                                                    | Construida en el backend (#1078): `PUT …/conversations/<id>/ai-exclusion/`, solo por la API hasta el botón del Inbox (#1079). Cubre **una conversación, no el contacto**: una conversación nueva del mismo cliente no hereda la marca | Decidir si basta por conversación o se quiere por contacto (propuesta F de [phase-8.md](../phases/phase-8.md)); **#1080 en `main` antes de encender la IA** |
| Términos del proveedor                                                                        | Sin confirmar                                                                                                                                                                                                                         | Uso para entrenar, conservación, papel en el tratamiento, entidad y país; en la cuenta (3.2)                                                                |
| Puntos de la asesoría legal sobre el aviso                                                    | Abiertos                                                                                                                                                                                                                              | Base de legitimación, reglamento vigente, flujo transfronterizo, banco de datos, derechos y plazos (borrador, Parte 3)                                      |
| Simulador del proveedor en el repositorio                                                     | No existe; la E2E usó una herramienta externa                                                                                                                                                                                         | ADR nueva para [#1061](https://github.com/PapiCuche/crm-gooddoggy/issues/1061)                                                                              |

## 8. Huecos conocidos

- **Los avisos de gasto llegan solo por correo** ([#1068](https://github.com/PapiCuche/crm-gooddoggy/issues/1068)): a quienes tienen `ai.manage` en la organización, una vez por aviso y mes de Lima, sin aviso en pantalla. Si la entrega a alguien falla, no se reintenta. El tope **diario** no avisa (ADR-045).
- **Lo gastado no se ve en el CRM.** La ruta de los topes no lo devuelve: el propietario lo revisa en la consola del proveedor.
- **El modelo se escribe, no se elige de una lista.** La API no publica la lista permitida (`AI_MODELS`); la pantalla valida la forma y la API rechaza lo que no está en la lista.
- **Las acciones `ai.*` y `conversation.*` no están en el filtro de `/o/<SLUG>/auditoria`**: se ven en la lista, pero para filtrarlas hay que usar la ruta `…/audit/?action=`.

## 9. Pantallas y rutas citadas

Cada una existe en `main` (`f25e5b6c`; «Presupuesto», con [#1067](https://github.com/PapiCuche/crm-gooddoggy/issues/1067)). Pantallas en `frontend/src/app`; rutas en los `urls.py` del backend, todas bajo `/api/v1/o/<SLUG>/`.

| Pantalla o ruta                                                                                                     | Dónde está                                                                                |
| ------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------- |
| `/o/<SLUG>/ia` (proveedor y estado de la IA)                                                                        | [ia/page.tsx](../../frontend/src/app/o/%5BorgSlug%5D/ia/page.tsx)                         |
| `/o/<SLUG>/ia/traspaso`                                                                                             | [ia/traspaso/page.tsx](../../frontend/src/app/o/%5BorgSlug%5D/ia/traspaso/page.tsx)       |
| `/o/<SLUG>/ia/presupuesto`                                                                                          | [ia/presupuesto/page.tsx](../../frontend/src/app/o/%5BorgSlug%5D/ia/presupuesto/page.tsx) |
| `/o/<SLUG>/inbox`                                                                                                   | [inbox/page.tsx](../../frontend/src/app/o/%5BorgSlug%5D/inbox/page.tsx)                   |
| `/o/<SLUG>/roles`                                                                                                   | [roles/page.tsx](../../frontend/src/app/o/%5BorgSlug%5D/roles/page.tsx)                   |
| `/o/<SLUG>/precios/listas`                                                                                          | [precios/listas/page.tsx](../../frontend/src/app/o/%5BorgSlug%5D/precios/listas/page.tsx) |
| `/o/<SLUG>/auditoria`                                                                                               | [auditoria/page.tsx](../../frontend/src/app/o/%5BorgSlug%5D/auditoria/page.tsx)           |
| `/o/<SLUG>/canales`                                                                                                 | [canales/page.tsx](../../frontend/src/app/o/%5BorgSlug%5D/canales/page.tsx)               |
| `/cuenta/seguridad`                                                                                                 | [cuenta/seguridad/page.tsx](../../frontend/src/app/cuenta/seguridad/page.tsx)             |
| `ai/budget/`, `ai/switch/`, `ai/switch/on/`, `ai/switch/off/`, `ai/provider-accounts/…` (`key/`, `model/`, `test/`) | [apps/ai_gateway/api/urls.py](../../backend/apps/ai_gateway/api/urls.py)                  |
| `conversations/<id>/copilot/suggest/`, `…/summary/`, `…/suggestions/<id>/insert/`, `ai/handoff-reply/`              | [apps/ai_agents/api/urls.py](../../backend/apps/ai_agents/api/urls.py)                    |
| `conversations/<id>/human-request/`                                                                                 | [apps/conversations/api/urls.py](../../backend/apps/conversations/api/urls.py)            |
| `pricing/price-lists/<id>/ai-exposable/`                                                                            | [apps/pricing/api/urls.py](../../backend/apps/pricing/api/urls.py)                        |
| `audit/`                                                                                                            | [apps/audit/api/urls.py](../../backend/apps/audit/api/urls.py)                            |

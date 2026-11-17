# ADR-040: Clave de deduplicación de la ingesta de Instagram y Messenger por cuenta

- **Status:** Accepted (2026-11-17, decisión del propietario registrada en #779)
- **Date:** 2026-11-17
- **Deciders:** propietario del producto (#779); redactado por el programa autónomo (ADR-015).
- **Work items:** F7b-04d ([#912](https://github.com/PapiCuche/crm-gooddoggy/issues/912))
- **Related:** [ADR-035](ADR-035-instagram-messenger-channels.md) §4 (lo enmienda **solo** en las claves de deduplicación de `page` e `instagram`), ADR-029 §3, ADR-002 §3.3

## Context

ADR-035 §4 fijó claves de deduplicación globales para un `mid`: `fb:msg:<mid>`, `ig:msg:<mid>`, `ig:read:<mid>` (y `…:del:<mid>`). `webhook_ingress_key_uq` es único por (`provider`, `event_key`). Cuando dos cuentas conectadas, de la misma organización o de dos distintas, se escriben, Meta entrega el mismo `mid` como eco a una y como entrante a la otra: el segundo ítem se descartaba como repetido y una organización perdía el mensaje de su cliente (o el eco que reconcilia un envío). La revisión de #897 lo dejó como obligación antes de publicar #779.

## Decision

Toda clave de `page` e `instagram` lleva el id de la cuenta que enruta (`entry.id`, ya validado con `META_ID`), con `urllib.parse.quote(id, safe="")` como el resto de ids:

| Evento                       | Clave                                                                     |
| ---------------------------- | ------------------------------------------------------------------------- |
| Entrante o eco               | `fb:msg:<page>:<mid>`, `ig:msg:<igid>:<mid>`                              |
| Borrado                      | `fb:del:<page>:<mid>`, `ig:del:<igid>:<mid>`                              |
| Leído de Instagram           | `ig:read:<igid>:<mid>`                                                    |
| Entrega y leído de Messenger | `fb:delivery:<page>:<psid>:<watermark>`, `fb:read:…` (sin cambios)        |
| Lo demás                     | `fb:other:<page>:<resumen>`, `ig:other:<igid>:<resumen>`                  |

- **Tope:** la clave más larga con un `mid` es `ig:read:` + 30 (`META_ID`) + `:` + 300 (`KEY_PART_MAX`) = 339 caracteres, dentro de los 700 de `event_key`. La marca de agua de Messenger se acota a 15 dígitos (`< 10**15`); una mayor es `Invalid("watermark")` y se descarta sola, sin tumbar el lote.
- WhatsApp no cambia. Sin migración: el esquema, las funciones de la ingesta y el enrutado son los mismos.

## Alternatives considered

| Alternativa              | Por qué no                                                                                                                  |
| ------------------------ | --------------------------------------------------------------------------------------------------------------------------- |
| Mantener la clave global | Pierde mensajes entre cuentas conectadas: el mismo `mid` llega a dos cuentas, y la unicidad de `messages` ya es por cuenta. |

## Consequences

- Cada cuenta procesa su copia del evento; un reintento de Meta con la misma clave sigue siendo un solo ítem.
- **Transición:** durante las 36 h de reintentos de Meta (ADR-035 §4), un evento ya guardado con la clave global vuelve con la nueva y crea un ítem nuevo. No duplica nada: `record_inbound` devuelve el mensaje que ya estaba (respaldado por `messages_external_uq`, por organización, cuenta y `external_message_id`), la palabra de baja solo se evalúa en un mensaje nuevo, y un eco o un leído repetido no retrocede el estado (`advance_delivery`). Las dos filas conviven hasta la purga de 90 días.

## Security implications

Ninguna nueva: el `organization_id` sigue saliendo de `resolve_social_account`, nunca del payload; ni RLS ni las funciones `SECURITY DEFINER` cambian. Evita perder mensajes entre cuentas y organizaciones.

## Operational implications

Ninguna acción al desplegar. Tras el despliegue puede aparecer, durante 36 h, un ítem `PROCESSED` repetido con la clave nueva por cada evento reintentado; es lo esperado.

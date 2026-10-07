# Architecture Decision Records (ADR)

Registro formal de las decisiones de arquitectura. Cada ADR es **inmutable una vez aceptado**: para cambiar una decisión, se crea un ADR nuevo que la reemplaza (`Superseded by ADR-0XX`) y se actualiza el estado del anterior.

## Índice

| ADR | Título | Estado | Fecha |
|---|---|---|---|
| [ADR-001](ADR-001-multi-tenancy.md) | Multi-tenancy con base de datos y esquema compartidos | Accepted | 2026-09-28 |
| [ADR-002](ADR-002-postgresql-rls.md) | PostgreSQL Row Level Security como defensa en profundidad | Accepted | 2026-09-28 |
| [ADR-003](ADR-003-auth-session.md) | Autenticación por sesión con cookie HttpOnly y MFA | Accepted | 2026-09-28 |
| [ADR-004](ADR-004-identifiers.md) | UUIDv7 y numeración comercial por organización | Accepted | 2026-09-28 |
| [ADR-005](ADR-005-ai-provider-abstraction.md) | Abstracción de proveedores de IA, tools y validación de salida | Accepted | 2026-09-28 |
| [ADR-006](ADR-006-pricing-engine.md) | PricingService como autoridad única de precios | Accepted | 2026-09-28 |
| [ADR-007](ADR-007-conversation-assignment.md) | Modelo de asignación de conversaciones con FKs explícitas | Accepted | 2026-09-28 |
| [ADR-008](ADR-008-object-storage.md) | Almacenamiento de objetos S3-compatible | Accepted | 2026-09-28 |
| [ADR-009](ADR-009-git-strategy.md) | GitHub Flow | Accepted (enmendado en parte por ADR-015) | 2026-09-28 |
| [ADR-010](ADR-010-messaging-policy.md) | MessagingPolicyService, ventana de atención y plantillas | Accepted | 2026-09-28 |
| [ADR-011](ADR-011-observability-and-logs.md) | Observabilidad y separación de logs (aplicación / auditoría / IA) | Accepted | 2026-09-28 |
| [ADR-012](ADR-012-engineering-runtime-baseline.md) | Baseline de runtimes, herramientas e imágenes; emulador S3 local (Garage) | Accepted | 2026-09-28 |
| [ADR-013](ADR-013-platform-audit.md) | Auditoría de plataforma en un sumidero propio, sin tenant y solo de inserción | Accepted | 2026-10-02 |
| [ADR-014](ADR-014-api-errors-and-authentication.md) | Contrato de errores de la API, autenticación por sesión y rutas de plataforma | Accepted | 2026-10-02 |
| [ADR-015](ADR-015-autonomous-delivery-program.md) | Programa de entrega autónoma con merge por gates (modifica la aprobación y el merge de ADR-009 dentro del programa) | Accepted | 2026-10-02 |
| [ADR-016](ADR-016-api-list-convention.md) | Convención de los listados de la API: paginación por cursor | Accepted | 2026-10-04 |
| [ADR-017](ADR-017-member-administration-module.md) | Administración de miembros en un módulo de orquestación (`apps.members`); reglas para suspender y reactivar | Accepted | 2026-10-04 |
| [ADR-018](ADR-018-owner-role-follows-catalog.md) | El rol Owner sigue al catálogo de permisos: lo impone el job de migraciones | Accepted | 2026-10-05 |
| [ADR-019](ADR-019-outbound-email.md) | Correo saliente por un único módulo (`core.mail`), SMTP por entorno y sin proveedor fijado; se envía desde tareas y los enlaces de un solo uso no se guardan | Accepted | 2026-10-07 |
| [ADR-020](ADR-020-invitations.md) | Invitaciones a una organización: sin cuenta ni membresía hasta aceptar, enlace de un solo uso guardado como hash, anti-escalada al invitar, y aceptar no inicia sesión | Accepted | 2026-10-07 |

## Plantilla

```markdown
# ADR-0XX: Título

- **Status:** Proposed | Accepted | Deprecated | Superseded by ADR-0YY
- **Date:** YYYY-MM-DD
- **Deciders:** …
- **Related:** …

## Context
## Decision
## Alternatives considered
## Consequences
## Security implications
## Operational implications
```

## Estados

- **Proposed:** en discusión, no se implementa.
- **Accepted:** vigente; el código debe cumplirla.
- **Deprecated:** ya no aplica para código nuevo.
- **Superseded:** reemplazada por otro ADR (enlazado).

Un ADR nuevo puede enmendar solo una parte de otro: el anterior sigue `Accepted`, no se edita, y el índice indica qué ADR lo enmienda.

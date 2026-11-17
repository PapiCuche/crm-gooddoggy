# ADR-042: Excepción acotada y con caducidad en `pnpm audit` para GHSA-fc5q (picomatch, sin corrección)

- **Status:** Accepted (2026-11-17, decisión del propietario registrada en #928)
- **Date:** 2026-11-17
- **Deciders:** propietario del producto (#928); redactado por el agente de SEC-DEV-03.
- **Work items:** SEC-DEV-03 ([#928](https://github.com/PapiCuche/crm-gooddoggy/issues/928)); retirada en SEC-DEV-04 ([#929](https://github.com/PapiCuche/crm-gooddoggy/issues/929))
- **Related:** [ADR-012](ADR-012-engineering-runtime-baseline.md) (lo acota **solo** en el control de auditoría de dependencias del frontend: §4.5 y la política de parches de seguridad), ADR-015
- **Review date:** cada 7 días (2026-11-24); **caduca el 2026-12-01**

## Context

ADR-012 (§4.5 y *Upgrade policy*) trata los avisos de las dependencias del frontend corrigiéndolos con la versión parcheada más baja, y `pnpm audit --prod --audit-level=high` bloquea cualquier aviso alto o crítico de producción en el CI («frontend checks») y en `make check`. El 2026-11-17, entre las 17:13 y las 17:50 UTC, la base de avisos de npm empezó a reportar [GHSA-fc5q-39xc-vjpf](https://github.com/advisories/GHSA-fc5q-39xc-vjpf) (alto): ReDoS en picomatch ≤4.0.7, **sin versión corregida**. Ninguna versión ni override lo corrige, y la auditoría bloqueaba todos los merges.

En producción picomatch solo llega por `next-intl > @parcel/watcher > picomatch`. `@parcel/watcher` es el vigilante de archivos del extractor de mensajes de next-intl: lo carga el plugin de `next.config.ts` en build y en desarrollo, y este proyecto no activa el extractor (`createNextIntlPlugin` sin `extract`). Los patrones son los nuestros, nunca texto del usuario; el ReDoS necesita un patrón controlado por un atacante.

## Decision

- `frontend/pnpm-workspace.yaml` ignora **solo** GHSA-fc5q-39xc-vjpf con `auditConfig.ignoreGhsas` (la clave que pnpm 12.8.1 aplica y la que escribe `pnpm audit --ignore`; `audit.ignoreGhsas` no tiene efecto y se descarta). Vale igual para el CI y para `make check`: los comandos, `--prod` y `--audit-level=high` no cambian.
- La entrada lleva su caducidad en la misma línea (`# caduca: 2026-12-01`) y un comentario con la ruta, el motivo, este ADR y el issue de retirada.
- `frontend/src/build/audit-exceptions.test.ts` (Vitest, en `pnpm test`, que corre antes de la auditoría en el CI y en `make check`) hace cumplir la decisión:
  - el archivo ignora exactamente un aviso, GHSA-fc5q, con su fecha al lado;
  - cualquier otro GHSA, un CVE, la sección `audit`, `auditLevel`, una entrada sin fecha o una fecha posterior al 2026-12-01 fallan;
  - la excepción vale hasta el 2026-12-01 a las 23:59:59 UTC; **desde el 2026-12-02 (UTC) la prueba falla** si la entrada sigue. Antes de esa fecha no puede fallar por la caducidad: compara la fecha UTC de hoy con la de la entrada, sin red.
- Revisión cada 7 días en #929 (2026-11-24 y 2026-12-01). La excepción se quita cuando picomatch publique la corrección y esa versión cumpla las 24 h de `minimumReleaseAge` (ADR-012), sin excepciones de edad.
- Alargar la fecha o añadir otro aviso pide una decisión nueva del propietario y un ADR nuevo; la prueba lo impide sin ese cambio.

## Alternatives considered

| Alternativa | Por qué no |
|---|---|
| Bajar `--audit-level` a `critical` o quitar `--prod` | Deja pasar cualquier aviso alto, no solo este. Excluido por el propietario (#928). |
| Override de picomatch | No hay versión corregida a la que forzar. |
| Quitar o bajar `next-intl` | Fuera de alcance (#928); la i18n del producto depende de él. |
| Esperar la corrección | Bloquea todos los merges durante un tiempo desconocido. |

## Consequences

- `pnpm audit --prod --audit-level=high` pasa con «1 ignored: 1 high» y sigue fallando con cualquier otro aviso alto o crítico de producción.
- La excepción va por aviso, no por ruta: también calla GHSA-fc5q en las rutas de desarrollo de picomatch (eslint, orval, vite), que `--prod` no audita.
- Si picomatch no publica la corrección antes del 2026-12-01, la prueba bloquea el CI y `make check` el 2026-12-02 hasta que el propietario decida.

## Security implications

Excepción acotada a un único aviso sin corrección publicada, en una dependencia de build y de desarrollo sin entrada del usuario, autorizada por el propietario y con caducidad que se hace cumplir. Ningún otro control de ADR-012 cambia.

## Operational implications

Revisar #929 cada 7 días. Al publicarse la corrección (y pasadas 24 h), SEC-DEV-04 sube picomatch por la ruta de next-intl, borra la entrada y ajusta la prueba.

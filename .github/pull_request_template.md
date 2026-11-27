## Issue / Fase

Closes #

<!-- Usar Closes/Fixes/Resolves para el ÚNICO work item de este PR (1 issue = 1 rama = 1 PR).
     Refs #N solo para referencias adicionales (p. ej., el issue maestro). Indicar ID (F1-02) y fase. -->

## Objetivo

## Cambios

<!-- Archivos o módulos principales. Migraciones incluidas (si aplica). -->

## Clase de despliegue

<!-- ADR-048 §2: none, A1, A2, A3, B o C (la calculada está en el resumen del check «deploy class»). Manda la
     mayor: nunca se declara una menor. C si el release exige cambiar /etc/crm/*.env o rotar una KEK. -->

Declarada:

## No incluye

<!-- Qué queda fuera explícitamente (trabajo de otros issues o fases). -->

## Cómo se verificó

<!-- Comandos ejecutados y su resultado. Declarar lo que NO se pudo ejecutar. -->

## Definition of Done

<!-- Marcar o poner "N/A — motivo". -->

- [ ] Tenant context respetado (`organization_id` desde el contexto; nada del payload)
- [ ] RLS en tablas tenant-owned nuevas (una política PERMISSIVE por comando)
- [ ] Permisos y scopes en endpoints nuevos
- [ ] Auditoría en cambios importantes
- [ ] Tests nuevos o actualizados (incluida la suite de aislamiento si hay endpoints o tablas nuevas)
- [ ] Sin secretos ni PII en código, logs o respuestas
- [ ] Documentación / ADR actualizados si cambia una decisión
- [ ] Dentro del scope del issue (sin adelantar otros work items)
- [ ] Slice de producción (AGENTS.md §11): serie y work item de cierre indicados; sin datos ficticios en el producto; nada nuevo en `/demo`

## Riesgos y deuda técnica

<!-- Deuda consciente → issue con label tech-debt y fase objetivo. -->

## Autoría

- [ ] Generado o asistido por IA (indicar el agente). La aprobación final es humana y el autor no hace merge, salvo en el programa autónomo (ADR-015), donde el merge exige sus diez gates.

## Handoff para Reviewer

<!-- El Builder lo mantiene actualizado al HEAD actual. Solo resultados reales; nada de commits anteriores.
     El Reviewer lee ESTA sección y el diff directamente en GitHub (no depende de resúmenes pegados en el chat). -->

- **Work item:** #
- **Branch:**
- **HEAD SHA:**
- **Objetivo implementado:**
- **Archivos principales:**
- **Decisiones nuevas:**
- **Tests:**
- **CI (checks reales y estado):**
- **Seguridad:**
- **Limitaciones:**
- **Observaciones / deuda:**
- **Lo que NO se implementó:**
- **Estado del siguiente work item:**
- **Merge performed:** NO
- **Siguiente acción:**

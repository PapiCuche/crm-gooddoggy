"""ADR-001 §3 en una tabla que ya tiene filas: la FK compuesta `(organization_id, <col>) →
organization_memberships (organization_id, user_id)` de cada columna `*_user_id`. Es el patrón de
`catalog/0005` (F3-37), para las migraciones que lo repiten (F2-128: `credentials` y `files`).

- Antes de tocar nada exige `BYPASSRLS` (con FORCE RLS, un rol sin él no ve filas y la cuenta
  saldría 0) y cuenta, por tabla, las filas que nombran a quien no es miembro de su organización:
  si hay alguna se niega con las cuentas y no borra ni reescribe datos (se corrigen a mano y se
  repite).
- Cada FK se añade `NOT VALID` (solo cambia el catálogo; desde ese momento comprueba toda
  escritura) y después `VALIDATE CONSTRAINT` recorre la tabla con `SHARE UPDATE EXCLUSIVE`, que no
  impide leer ni escribir. Todo con `lock_timeout` de 5 s, que se quita en un `finally` aunque
  falle: no espera sin fin detrás de una transacción larga, falla sin cambiar nada y se repite.
- Se puede repetir: una FK que ya existe (un intento que falló a medias) solo se valida.
- Un `NULL` sigue valiendo (`MATCH SIMPLE`): lo escribe quien actúa sin persona (sistema).
- Hacia atrás quita las FKs; ningún dato cambia.

La migración que lo usa es `atomic = False`. Sin índice nuevo: las membresías no se borran (cambian
de estado), así que la FK no recorre la tabla al borrar una.

**Lo usan migraciones ya aplicadas:** su SQL y su comportamiento no cambian nunca (cambiarlos
cambiaría lo que hace una migración aplicada, también al volver atrás). Otro comportamiento es otro
ayudante, o una versión nueva junto a esta. El nombre de cada FK lo fija la migración, no el
ayudante."""

from collections.abc import Callable
from typing import Any

MEMBERS = "public.organization_memberships (organization_id, user_id)"
ORPHANS = (
    "SELECT count(*) FROM public.{table} r WHERE r.{column} IS NOT NULL AND NOT EXISTS ("
    "SELECT 1 FROM public.organization_memberships m WHERE m.organization_id = r.organization_id"
    " AND m.user_id = r.{column})"
)
EXISTS = "SELECT 1 FROM pg_constraint WHERE conname = %s AND conrelid = %s::regclass"


def _scalar(cursor: Any, sql: str, params: list[Any] | None = None) -> Any:
    cursor.execute(sql, params)
    row = cursor.fetchone()
    return row[0] if row else None


class MemberFKs:
    """Las FKs de unas columnas `(tabla, columna, nombre de la FK)`; `label` encabeza los errores
    (p. ej., `files 0004`). `add` y `drop` son el `RunPython` de la migración."""

    def __init__(self, label: str, columns: tuple[tuple[str, str, str], ...]) -> None:
        self.label, self.columns = label, columns

    def refuse_orphans(self, cursor: Any) -> None:
        if not _scalar(cursor, "SELECT rolbypassrls FROM pg_roles WHERE rolname = current_user"):
            raise RuntimeError(f"{self.label}: run as crm_migrator with BYPASSRLS (RLS hides rows)")
        found = {
            f"{table}.{column}": _scalar(cursor, ORPHANS.format(table=table, column=column))
            for table, column, _ in self.columns
        }
        if orphans := {where: count for where, count in found.items() if count}:
            detail = ", ".join(f"{where}: {count}" for where, count in orphans.items())
            raise RuntimeError(
                f"{self.label}: {sum(orphans.values())} rows name a user who is not a member of"
                f" their organization ({detail}); fix them first (docs/phases/phase-2.md, F2-128)"
            )

    def add(self, apps: Any, schema_editor: Any) -> None:
        def step(cursor: Any) -> None:
            self.refuse_orphans(cursor)
            for table, column, fk in self.columns:
                if not _scalar(cursor, EXISTS, [fk, f"public.{table}"]):
                    cursor.execute(
                        f"ALTER TABLE public.{table} ADD CONSTRAINT {fk} FOREIGN KEY"
                        f" (organization_id, {column}) REFERENCES {MEMBERS} NOT VALID"
                    )
                cursor.execute(f"ALTER TABLE public.{table} VALIDATE CONSTRAINT {fk}")

        _limited(schema_editor, step)

    def drop(self, apps: Any, schema_editor: Any) -> None:
        def step(cursor: Any) -> None:
            for table, _, fk in self.columns:
                cursor.execute(f"ALTER TABLE public.{table} DROP CONSTRAINT IF EXISTS {fk}")

        _limited(schema_editor, step)


def _limited(schema_editor: Any, step: Callable[[Any], None]) -> None:
    with schema_editor.connection.cursor() as cursor:
        cursor.execute("SET lock_timeout = '5s'")
        try:
            step(cursor)
        finally:
            cursor.execute("RESET lock_timeout")

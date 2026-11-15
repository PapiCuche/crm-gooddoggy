"""F3-37 (#361): quien borra una marca, una categoría, un producto o una variante es miembro de la
misma organización. `deleted_by_user_id` de `brands`, `categories`, `products` y `product_variants`
recibe la FK compuesta `(organization_id, deleted_by_user_id) → organization_memberships
(organization_id, user_id)` que ADR-001 §3 exige a toda columna `*_user_id` (`warehouses` la tiene
desde F3-08). Una baja sin persona (`NULL`) sigue valiendo: la FK no mira un nulo.

Antes, si alguna fila borrada apunta a alguien que no es miembro de su organización, la migración
se niega y dice cuántas hay por tabla: no borra ni reescribe datos. Lo cuenta `crm_migrator`, que ha
de tener `BYPASSRLS` (con FORCE RLS, un rol sin él no ve filas y la cuenta saldría 0).

**No atómica:** cada FK se añade `NOT VALID` (solo cambia el catálogo; desde ese momento comprueba
toda escritura) con `lock_timeout` de 5 s, para no esperar sin fin detrás de una transacción larga
(si vence, falla sin cambiar nada y se repite), y después `VALIDATE CONSTRAINT` recorre la tabla con
`SHARE UPDATE EXCLUSIVE`, que no impide leer ni escribir. Se puede repetir: una FK que ya existe
(un intento anterior que falló a medias) no se vuelve a crear, solo se valida. Sin índice nuevo:
la columna solo se escribe al borrar y las membresías no se borran (cambian de estado), como en
`warehouses`, `contacts` y `price_lists`. Hacia atrás: quita las cuatro FKs; ningún dato cambia."""

from typing import Any

from django.db import migrations

TABLES = ("brands", "categories", "products", "product_variants")
MEMBERS = "public.organization_memberships (organization_id, user_id)"
ORPHANS = (
    "SELECT count(*) FROM public.{table} r WHERE r.deleted_by_user_id IS NOT NULL AND NOT EXISTS ("
    "SELECT 1 FROM public.organization_memberships m WHERE m.organization_id = r.organization_id"
    " AND m.user_id = r.deleted_by_user_id)"
)
EXISTS = "SELECT 1 FROM pg_constraint WHERE conname = %s AND conrelid = %s::regclass"


def name(table: str) -> str:
    return f"{table}_deleted_by_fk"


def _scalar(cursor: Any, sql: str, params: list[Any] | None = None) -> Any:
    cursor.execute(sql, params)
    row = cursor.fetchone()
    return row[0] if row else None


def refuse_orphans(cursor: Any) -> None:
    """Una baja por quien no es miembro no tiene a quién apuntar: hay que resolverla antes."""
    if not _scalar(cursor, "SELECT rolbypassrls FROM pg_roles WHERE rolname = current_user"):
        raise RuntimeError("catalog 0005: run as crm_migrator with BYPASSRLS (RLS hides rows)")
    found = {table: _scalar(cursor, ORPHANS.format(table=table)) for table in TABLES}
    if orphans := {table: count for table, count in found.items() if count}:
        detail = ", ".join(f"{table}: {count}" for table, count in orphans.items())
        raise RuntimeError(
            f"catalog 0005: {sum(orphans.values())} deleted catalog rows name a deleter who is not"
            f" a member of their organization ({detail}); fix them first"
            " (docs/phases/phase-3.md, F3-37)"
        )


def _limited(schema_editor: Any, step: Any) -> None:
    with schema_editor.connection.cursor() as cursor:
        cursor.execute("SET lock_timeout = '5s'")
        try:
            step(cursor)
        finally:
            cursor.execute("RESET lock_timeout")


def add(apps: Any, schema_editor: Any) -> None:
    def step(cursor: Any) -> None:
        refuse_orphans(cursor)
        for table in TABLES:
            if not _scalar(cursor, EXISTS, [name(table), f"public.{table}"]):
                cursor.execute(
                    f"ALTER TABLE public.{table} ADD CONSTRAINT {name(table)} FOREIGN KEY"
                    f" (organization_id, deleted_by_user_id) REFERENCES {MEMBERS} NOT VALID"
                )
            cursor.execute(f"ALTER TABLE public.{table} VALIDATE CONSTRAINT {name(table)}")

    _limited(schema_editor, step)


def drop(apps: Any, schema_editor: Any) -> None:
    def step(cursor: Any) -> None:
        for table in TABLES:
            cursor.execute(f"ALTER TABLE public.{table} DROP CONSTRAINT IF EXISTS {name(table)}")

    _limited(schema_editor, step)


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        ("catalog", "0004_variant_label"),
    ]

    operations = [migrations.RunPython(add, drop)]

"""F3-48c: una promoción publicada no se reescribe con el rol del runtime (revisión de F3-48a)."""

from typing import Any

from django.conf import settings
from django.db import migrations

COLUMNS = "status, version, updated_by_user_id, updated_at"


def _privileges(grant: bool) -> Any:
    """El runtime no cambia los términos de una promoción, solo su estado."""

    def run(apps: Any, schema_editor: Any) -> None:
        role = schema_editor.quote_name(settings.DB_APP_ROLE)
        table = "public.promotions"
        if grant:
            schema_editor.execute(f"REVOKE UPDATE ({COLUMNS}) ON {table} FROM {role}")
            schema_editor.execute(f"GRANT UPDATE ON {table} TO {role}")
        else:
            schema_editor.execute(f"REVOKE UPDATE ON {table} FROM {role}")
            schema_editor.execute(f"GRANT UPDATE ({COLUMNS}) ON {table} TO {role}")

    return run


# Para todos los roles: las únicas transiciones son DRAFT → PUBLISHED, DRAFT → CANCELLED y
# PUBLISHED → CANCELLED, y fuera de DRAFT los términos no cambian. Los objetivos y las listas solo
# se insertan mientras la promoción es DRAFT (`FOR SHARE`: publicar espera a quien inserta).
GUARD = """
CREATE FUNCTION promotions_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, public AS $fn$
BEGIN
    IF NEW.status IS DISTINCT FROM OLD.status AND NOT (
        OLD.status = 'DRAFT' AND NEW.status IN ('PUBLISHED', 'CANCELLED')
        OR OLD.status = 'PUBLISHED' AND NEW.status = 'CANCELLED'
    ) THEN
        RAISE EXCEPTION 'promotions_guard: transición de estado no permitida'
            USING ERRCODE = 'check_violation';
    END IF;
    IF OLD.status <> 'DRAFT' AND ((
        NEW.organization_id, NEW.name, NEW.description, NEW.terms, NEW.discount_type, NEW.value,
        NEW.currency, NEW.starts_at, NEW.ends_at, NEW.priority, NEW.stackable, NEW.ai_exposable
    ) IS DISTINCT FROM (
        OLD.organization_id, OLD.name, OLD.description, OLD.terms, OLD.discount_type, OLD.value,
        OLD.currency, OLD.starts_at, OLD.ends_at, OLD.priority, OLD.stackable, OLD.ai_exposable
    ) OR NEW.channels IS DISTINCT FROM OLD.channels) THEN
        RAISE EXCEPTION 'promotions_guard: una promoción publicada no cambia sus términos'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$fn$;
CREATE TRIGGER promotions_guard BEFORE UPDATE ON promotions
    FOR EACH ROW EXECUTE FUNCTION promotions_guard();

CREATE FUNCTION promotion_children_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, public AS $fn$
BEGIN
    PERFORM 1 FROM promotions
        WHERE organization_id = NEW.organization_id AND id = NEW.promotion_id AND status = 'DRAFT'
        FOR SHARE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'promotion_children_guard: solo una promoción en borrador admite cambios'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$fn$;
CREATE TRIGGER promotion_items_guard BEFORE INSERT ON promotion_items
    FOR EACH ROW EXECUTE FUNCTION promotion_children_guard();
CREATE TRIGGER promotion_price_lists_guard BEFORE INSERT ON promotion_price_lists
    FOR EACH ROW EXECUTE FUNCTION promotion_children_guard();
"""
UNGUARD = """
DROP TRIGGER IF EXISTS promotion_price_lists_guard ON promotion_price_lists;
DROP TRIGGER IF EXISTS promotion_items_guard ON promotion_items;
DROP TRIGGER IF EXISTS promotions_guard ON promotions;
DROP FUNCTION IF EXISTS promotion_children_guard();
DROP FUNCTION IF EXISTS promotions_guard();
"""


class Migration(migrations.Migration):
    dependencies = [("pricing", "0003_promotions")]

    operations = [
        migrations.RunPython(_privileges(False), _privileges(True)),
        migrations.RunSQL(GUARD, UNGUARD),
    ]

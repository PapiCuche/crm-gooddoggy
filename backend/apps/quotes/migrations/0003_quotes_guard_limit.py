"""F5-09b2 (revisión de seguridad de #687): el disparador de `quotes` cierra el camino directo de
`DRAFT` a `APPROVED` o `SENT`. Sin pasar por una solicitud, una cotización solo sale del borrador si
ninguna línea lleva precio manual y el mayor descuento a mano (sobre lo que paga el cliente, hacia
arriba) lo admite una política de los roles de quien actúa, con el mismo criterio que el comando: la
lista de la cotización o todas, el porcentaje, y el tope en importe en su moneda; el Owner, sin
límite. Quien actúa es `app.user_id` con `app.actor_type = 'USER'` (miembro `ACTIVE` y usuario
activo); sin persona, nunca. **Alcance:** `app.user_id` es una variable local a la transacción que
fija `tenant_scope`: la regla protege de un fallo de la aplicación, no de un SQL hostil de `crm_app`
(que podría fijarla). Se reemplaza la función con `CREATE OR REPLACE`: el disparador es el mismo."""

from importlib import import_module
from typing import Any

from django.conf import settings
from django.db import migrations

PREVIOUS = import_module("apps.quotes.migrations.0002_approvals_and_discount_policies")
STATUSES = PREVIOUS.STATUSES
_fail = PREVIOUS._fail

QUOTES_GUARD = f"""
CREATE OR REPLACE FUNCTION quotes_guard() RETURNS trigger LANGUAGE plpgsql
  SET search_path = pg_catalog, public, pg_temp AS $f$
DECLARE allowed text[]; pending boolean; approved boolean; manual boolean; taken numeric;
        by_line numeric; amount numeric; who text; permitted boolean;
BEGIN
  IF current_user <> '__APP__' THEN RETURN NEW; END IF;
  IF TG_OP = 'INSERT' THEN  -- una cotización nace en borrador, no ya aprobada o enviada
    IF NEW.status <> 'DRAFT' THEN
      {_fail("quotes_born_draft", "quotes: a quote is born a DRAFT")}
    END IF;
    RETURN NEW;
  END IF;
  IF NEW.organization_id <> OLD.organization_id OR NEW.id <> OLD.id OR NEW.number <> OLD.number
     OR NEW.revision <> OLD.revision OR NEW.contact_id <> OLD.contact_id
     OR NEW.price_list_id <> OLD.price_list_id OR NEW.created_at <> OLD.created_at
     OR NEW.created_by_user_id IS DISTINCT FROM OLD.created_by_user_id THEN
    {_fail("quotes_identity_immutable", "quotes: identity columns never change")}
  END IF;
  IF NEW.status <> ALL (ARRAY[{STATUSES}]) THEN RETURN NEW; END IF;  -- que hable el CHECK
  IF OLD.status <> 'DRAFT' AND (NEW.currency <> OLD.currency OR NEW.tax_mode <> OLD.tax_mode
     OR NEW.tax_rate <> OLD.tax_rate OR NEW.subtotal <> OLD.subtotal
     OR NEW.discount_total <> OLD.discount_total OR NEW.tax_total <> OLD.tax_total
     OR NEW.total <> OLD.total) THEN
    {_fail("quotes_amounts_frozen", "quotes: amounts are frozen after DRAFT")}
  END IF;
  allowed := CASE OLD.status
    WHEN 'DRAFT' THEN ARRAY['DRAFT','PENDING_APPROVAL','APPROVED','SENT','CANCELLED']
    WHEN 'PENDING_APPROVAL' THEN ARRAY['PENDING_APPROVAL','DRAFT','APPROVED','CANCELLED']
    WHEN 'APPROVED' THEN ARRAY['APPROVED','SENT','CANCELLED']
    WHEN 'SENT' THEN ARRAY['SENT','VIEWED','ACCEPTED','REJECTED','EXPIRED','CANCELLED']
    WHEN 'VIEWED' THEN ARRAY['VIEWED','ACCEPTED','REJECTED','EXPIRED','CANCELLED']
    ELSE ARRAY[OLD.status] END;
  IF NOT NEW.status = ANY (allowed) THEN
    {_fail("quotes_status_transition", "quotes: that status change is not allowed")}
  END IF;
  SELECT EXISTS (SELECT 1 FROM quote_approvals a WHERE a.organization_id = NEW.organization_id
                 AND a.quote_id = NEW.id AND a.status = 'PENDING') INTO pending;
  IF NEW.status = 'PENDING_APPROVAL' AND NOT pending THEN
    {_fail("quotes_needs_request", "quotes: PENDING_APPROVAL needs an open request")}
  END IF;
  IF NEW.status <> 'PENDING_APPROVAL' AND pending THEN
    {_fail("quotes_pending_approval", "quotes: decide the open request first")}
  END IF;
  IF OLD.status = 'DRAFT' AND NEW.status IN ('APPROVED', 'SENT') THEN
    -- sin pasar por la solicitud solo sale lo que quien actúa puede descontar: ni un precio manual
    -- ni más que la política más alta de sus roles (sobre lo que paga el cliente)
    SELECT coalesce(bool_or(i.manual_price_override), false),
           coalesce(max((i.unit_price - i.final_unit_price) / i.unit_price), 0),
           coalesce(sum(i.quantity * (i.unit_price - i.final_unit_price)), 0)
      INTO manual, by_line, amount
      FROM quote_items i WHERE i.organization_id = NEW.organization_id AND i.quote_id = NEW.id;
    taken := ceil(by_line * 10000) / 100;  -- la mayor línea, en porcentaje hacia arriba
    -- una persona de verdad: `tenant_scope` fija `app.user_id` y `app.actor_type` en la transacción
    who := CASE WHEN current_setting('app.actor_type', true) = 'USER'
                THEN nullif(current_setting('app.user_id', true), '') END;
    IF manual OR (taken > 0 AND who IS NULL) THEN
      {_fail("quotes_needs_request_path", "quotes: beyond the limit, go through the request")}
    END IF;
    IF taken > 0 THEN  -- el mismo `within` que el comando: Owner, o una política que lo admita
      SELECT EXISTS (
        SELECT 1 FROM organization_memberships m
          JOIN users u ON u.id = m.user_id
          JOIN membership_roles mr
            ON mr.organization_id = m.organization_id AND mr.membership_id = m.id
          JOIN roles r ON r.organization_id = m.organization_id AND r.id = mr.role_id
          LEFT JOIN discount_policies p
            ON p.organization_id = m.organization_id AND p.role_id = r.id
           AND (p.applies_to_price_list_id IS NULL
                OR p.applies_to_price_list_id = NEW.price_list_id)
         WHERE m.organization_id = NEW.organization_id AND m.user_id = who::uuid
           AND m.status = 'ACTIVE' AND u.is_active
           AND (r.is_owner_role OR (taken <= p.max_discount_percent
                AND (p.max_discount_amount IS NULL
                     OR (p.currency = NEW.currency AND amount <= p.max_discount_amount))))
      ) INTO permitted;
      IF NOT permitted THEN
        {_fail("quotes_needs_request_path", "quotes: beyond the limit, go through the request")}
      END IF;
    END IF;
  END IF;
  IF OLD.status = 'PENDING_APPROVAL' AND NEW.status = 'APPROVED' THEN
    SELECT a.status = 'APPROVED' INTO approved FROM quote_approvals a  -- la última solicitud
     WHERE a.organization_id = NEW.organization_id AND a.quote_id = NEW.id
     ORDER BY a.created_at DESC, a.id DESC LIMIT 1;
    IF approved IS NOT TRUE THEN
      {_fail("quotes_needs_approval", "quotes: APPROVED needs the latest request approved")}
    END IF;
  END IF;
  RETURN NEW;
END $f$"""  # noqa: S608


def _replace(apps: Any, schema_editor: Any) -> None:
    schema_editor.execute(QUOTES_GUARD.replace("__APP__", settings.DB_APP_ROLE))


def _restore(apps: Any, schema_editor: Any) -> None:
    previous = PREVIOUS.QUOTES_GUARD.replace("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION")
    schema_editor.execute(previous.replace("__APP__", settings.DB_APP_ROLE))


class Migration(migrations.Migration):
    dependencies = [("quotes", "0002_approvals_and_discount_policies")]

    operations = [migrations.RunPython(_replace, _restore)]

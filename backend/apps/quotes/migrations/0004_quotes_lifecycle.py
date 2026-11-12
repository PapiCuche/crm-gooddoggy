"""F5-09c: la cotización emitida lleva sus fotografías (empresa, cliente, condiciones y garantía),
su fecha de envío y, si el cliente decide, su fecha (y el motivo de un rechazo). La base lo exige
(`CHECK`) y el disparador de `quotes` (reemplazado con `CREATE OR REPLACE`, `0003` ya está
fusionada) lo congela: las fotografías y `sent_at` solo se fijan al pasar a `SENT`; `valid_until`,
solo de `APPROVED` a `SENT`; y desde `APPROVED` ni `valid_until` ni `notes_to_customer` cambian. No
comprueba permisos ni alcance (los comandos) ni que `app.actor_type` sea una persona."""

from importlib import import_module
from typing import Any

from django.conf import settings
from django.db import migrations, models

PREVIOUS = import_module("apps.quotes.migrations.0003_quotes_guard_limit")
_fail = PREVIOUS._fail
ISSUED = (  # lo que solo se escribe al emitir
    "company_snapshot", "customer_snapshot", "terms_snapshot", "warranty_snapshot", "sent_at",
)  # fmt: skip
STAMPS = ("cancelled_at", "cancel_reason")
NOT_BORN = " OR ".join(f"NEW.{c} IS NOT NULL" for c in ISSUED)
CHANGED = " OR ".join(f"NEW.{c} IS DISTINCT FROM OLD.{c}" for c in ISSUED)
REWRITTEN = " OR ".join(
    f"(OLD.{c} IS NOT NULL AND NEW.{c} IS DISTINCT FROM OLD.{c})" for c in STAMPS
)
BORN = f"""    IF {NOT_BORN} THEN
      {_fail("quotes_born_clean", "quotes: a quote is born without snapshots or decisions")}
    END IF;
"""  # noqa: S608
AI = f"""  IF NEW.created_by_user_id IS NULL AND OLD.status = 'DRAFT'
     AND NEW.status IN ('APPROVED', 'SENT') THEN  -- la IA no sale sola: pasa por la solicitud
    {_fail("quotes_ai_needs_request", "quotes: what the AI wrote needs an approved request")}
  END IF;
  IF NEW.created_by_user_id IS NULL AND OLD.status = 'APPROVED' AND NEW.status = 'SENT' THEN
    SELECT a.status = 'APPROVED' INTO approved FROM quote_approvals a
     WHERE a.organization_id = NEW.organization_id AND a.quote_id = NEW.id
     ORDER BY a.created_at DESC, a.id DESC LIMIT 1;
    IF approved IS NOT TRUE THEN
      {_fail("quotes_ai_needs_request", "quotes: what the AI wrote needs an approved request")}
    END IF;
  END IF;
"""  # noqa: S608
FROZEN = f"""  IF ({CHANGED})
     AND NOT (OLD.status IN ('DRAFT', 'APPROVED') AND NEW.status = 'SENT') THEN
    {_fail("quotes_issued_frozen", "quotes: the snapshots are set only when issuing")}
  END IF;
  IF {REWRITTEN} THEN
    {_fail("quotes_stamps_frozen", "quotes: a date or reason already written is not rewritten")}
  END IF;
  IF OLD.status <> 'DRAFT'  -- también esperando aprobación: quien aprueba aprueba lo que ve
     AND (NEW.notes_to_customer <> OLD.notes_to_customer
          OR (NEW.valid_until IS DISTINCT FROM OLD.valid_until
              AND NOT (OLD.status = 'APPROVED' AND NEW.status = 'SENT'))) THEN
    {_fail("quotes_issued_frozen", "quotes: what was approved or sent is not edited")}
  END IF;
"""  # noqa: S608


def _guard() -> str:
    """La función de `0003` con tres bloques añadidos (solo líneas nuevas: una prueba lo mira)."""
    text = PREVIOUS.QUOTES_GUARD
    for anchor, block in (
        ("    RETURN NEW;\n  END IF;\n  IF NEW.organization_id <> OLD", BORN),
        ("  SELECT EXISTS (SELECT 1 FROM quote_approvals a WHERE", FROZEN),
        ("  IF OLD.status = 'DRAFT' AND NEW.status IN ('APPROVED', 'SENT') THEN", AI),
    ):
        if text.count(anchor) != 1:
            raise RuntimeError("el ancla del disparador ya no está")
        text = text.replace(anchor, block + anchor, 1)
    return text


GUARD = _guard()


def _replace(apps: Any, schema_editor: Any) -> None:
    schema_editor.execute(GUARD.replace("__APP__", settings.DB_APP_ROLE))


def _restore(apps: Any, schema_editor: Any) -> None:
    PREVIOUS._replace(apps, schema_editor)


def _refuse_if_data(apps: Any, schema_editor: Any) -> None:
    """Revertir quitaría las columnas: no se pierde en silencio lo que ya se guardó."""
    quotes = apps.get_model("quotes", "Quote")._base_manager
    kept = models.Q(company_snapshot__isnull=False)
    if quotes.filter(kept).exists():
        raise RuntimeError("quotes: hay cotizaciones emitidas; no se revierte")


class Migration(migrations.Migration):
    dependencies = [
        ("contacts", "0008_consent_keywords"),
        ("deals", "0005_opportunity_conversation"),
        ("pricing", "0006_variant_costs"),
        ("quotes", "0003_quotes_guard_limit"),
    ]

    operations = [
        migrations.RunPython(_replace, _restore),
        migrations.AddField(
            model_name="quote",
            name="company_snapshot",
            field=models.JSONField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="quote",
            name="customer_snapshot",
            field=models.JSONField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="quote",
            name="sent_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="quote",
            name="terms_snapshot",
            field=models.CharField(blank=True, max_length=2000, null=True),
        ),
        migrations.AddField(
            model_name="quote",
            name="warranty_snapshot",
            field=models.CharField(blank=True, max_length=500, null=True),
        ),
        migrations.AddConstraint(
            model_name="quote",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ("status__in", ["SENT", "VIEWED", "ACCEPTED", "REJECTED", "EXPIRED"]),
                        _negated=True,
                    ),
                    models.Q(
                        ("company_snapshot__isnull", False),
                        ("customer_snapshot__isnull", False),
                        ("sent_at__isnull", False),
                        ("valid_until__isnull", False),
                    ),
                    _connector="OR",
                ),
                name="quotes_issued_ck",
            ),
        ),
        migrations.RunPython(migrations.RunPython.noop, _refuse_if_data),  # revierte primero
    ]

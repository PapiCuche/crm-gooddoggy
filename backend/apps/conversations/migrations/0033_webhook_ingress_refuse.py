"""F7-11b: la ingesta de webhooks guarda todo lo que cabe en un cuerpo de Meta y registra lo que
la base nunca guardará, en vez de perderlo en silencio.

- `webhook_ingress_insert` (de `0008`) sube su tope por ítem de 1 MiB a 8 MiB, por encima del
  cuerpo (3 MiB, `BODY_MAX`): con lo que Meta envía (textos, ids, números sin exponente) el
  `jsonb::text` crece como mucho la mitad (el espacio tras `,` y `:`), así que un ítem firmado cabe
  y se puede reprocesar. Un número con exponente sí puede crecer más (`1e300` se escribe con 301
  cifras); lo que aun así pase de 8 MiB lo registra `webhook_ingress_refuse`.
- `webhook_ingress_refuse` (nueva): una fila `DEAD` sin payload, con el hash del payload calculado
  igual que al guardar (sin tope de tamaño: no lo guarda, y un tope aquí haría de un rechazo un
  503 eterno), y su motivo en `webhook_failures`, para que se vea en el panel y alerte.
  Idempotente por clave, y nunca toca un ítem que ya estaba guardado.

Volver atrás deja `webhook_ingress_insert` como en `0008` y quita la función nueva."""

from importlib import import_module
from typing import Any

from django.db import migrations

from core.db.operations import SecurityDefinerFunction

PG = "pg_catalog, public, pg_temp"  # pg_temp al final (F3-52)
BAD = "RAISE EXCEPTION 'argumentos no válidos' USING ERRCODE = 'invalid_parameter_value'"
OLD: SecurityDefinerFunction = next(
    op
    for op in import_module(
        "apps.conversations.migrations.0008_webhook_ingress"
    ).Migration.operations
    if getattr(op, "name", None) == "webhook_ingress_insert"
)
CAP = "octet_length(p_payload::text) > 1048576"


def wider(body: str) -> str:
    if body.count(CAP) != 1:
        raise ValueError(CAP)
    return body.replace(CAP, "octet_length(p_payload::text) > 8388608")


class Replace(SecurityDefinerFunction):
    """Cambia el cuerpo de una función de `0008`, con el mismo propietario y los mismos permisos."""

    def database_forwards(self, app_label: str, schema_editor: Any, *a: Any) -> None:
        OLD.database_backwards(app_label, schema_editor, *a)
        super().database_forwards(app_label, schema_editor, *a)

    def database_backwards(self, app_label: str, schema_editor: Any, *a: Any) -> None:
        super().database_backwards(app_label, schema_editor, *a)
        OLD.database_forwards(app_label, schema_editor, *a)


class Migration(migrations.Migration):
    dependencies = [
        ("conversations", "0030_validate_outbound_file_purpose"),
    ]

    operations = [
        Replace(
            OLD.name,
            OLD.arguments,
            OLD.argtypes,
            OLD.returns,
            wider(OLD.body),
            language=OLD.language,
            search_path=OLD.search_path,
        ),
        SecurityDefinerFunction(
            "webhook_ingress_refuse",
            "p_provider text, p_event_key text, p_kind text, p_waba_id text, "
            "p_phone_number_id text, p_payload jsonb, p_reason text",
            "text, text, text, text, text, jsonb, text",
            "uuid",
            f"""DECLARE v_id uuid;
BEGIN
  IF p_provider IS NULL OR p_provider <> 'meta'
     OR p_kind IS NULL OR p_kind NOT IN ('message', 'status', 'other')
     OR p_event_key IS NULL OR length(p_event_key) NOT BETWEEN 1 AND 700
     OR p_event_key !~ '^[A-Za-z0-9._~%%:-]+$'
     OR p_waba_id IS NULL OR p_waba_id !~ '^[0-9]{{5,30}}$'
     OR (p_phone_number_id IS NOT NULL AND p_phone_number_id !~ '^[0-9]{{5,30}}$')
     OR p_payload IS NULL OR jsonb_typeof(p_payload) <> 'object'
     OR p_reason IS NULL OR p_reason !~ '^REFUSED(_[0-9A-Z]{{5}})?$'
  THEN {BAD}; END IF;
  INSERT INTO public.webhook_ingress
    (provider, event_key, kind, waba_id, phone_number_id, status, payload, payload_hash)
  VALUES (p_provider, p_event_key, p_kind, p_waba_id, p_phone_number_id, 'DEAD', NULL,
          encode(sha256(convert_to(p_payload::text, 'UTF8')), 'hex'))
  ON CONFLICT (provider, event_key) DO NOTHING
  RETURNING webhook_ingress.id INTO v_id;
  IF v_id IS NOT NULL THEN
    INSERT INTO public.webhook_failures (ingress_id, attempt, reason) VALUES (v_id, 0, p_reason);
  END IF;
  RETURN v_id;
END""",  # noqa: S608 — texto fijo, sin datos
            language="plpgsql VOLATILE",
            search_path=PG,
        ),
    ]

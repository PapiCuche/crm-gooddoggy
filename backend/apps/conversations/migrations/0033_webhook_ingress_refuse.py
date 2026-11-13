"""F7-11b: registrar un ítem de webhook que la base nunca guardará (`webhook_ingress_insert` lo
rechaza), en vez de perderlo en silencio: una fila `DEAD` sin payload (solo su hash) y su motivo en
`webhook_failures`, para que se vea en el panel y alerte. Idempotente por clave, y nunca toca un
ítem que ya estaba guardado. Volver atrás solo quita la función."""

from django.db import migrations

from core.db.operations import SecurityDefinerFunction

PG = "pg_catalog, public, pg_temp"  # pg_temp al final (F3-52)
BAD = "RAISE EXCEPTION 'argumentos no válidos' USING ERRCODE = 'invalid_parameter_value'"


class Migration(migrations.Migration):
    dependencies = [
        ("conversations", "0030_validate_outbound_file_purpose"),
    ]

    operations = [
        SecurityDefinerFunction(
            "webhook_ingress_refuse",
            "p_provider text, p_event_key text, p_kind text, p_waba_id text, "
            "p_phone_number_id text, p_payload_hash text, p_reason text",
            "text, text, text, text, text, text, text",
            "uuid",
            f"""DECLARE v_id uuid;
BEGIN
  IF p_provider IS NULL OR p_provider <> 'meta'
     OR p_kind IS NULL OR p_kind NOT IN ('message', 'status', 'other')
     OR p_event_key IS NULL OR length(p_event_key) NOT BETWEEN 1 AND 700
     OR p_event_key !~ '^[A-Za-z0-9._~%%:-]+$'
     OR p_waba_id IS NULL OR p_waba_id !~ '^[0-9]{{5,30}}$'
     OR (p_phone_number_id IS NOT NULL AND p_phone_number_id !~ '^[0-9]{{5,30}}$')
     OR p_payload_hash IS NULL OR p_payload_hash !~ '^[0-9a-f]{{64}}$'
     OR p_reason IS NULL OR p_reason !~ '^REFUSED(_[0-9A-Z]{{5}})?$'
  THEN {BAD}; END IF;
  INSERT INTO public.webhook_ingress
    (provider, event_key, kind, waba_id, phone_number_id, status, payload, payload_hash)
  VALUES (p_provider, p_event_key, p_kind, p_waba_id, p_phone_number_id, 'DEAD', NULL,
          p_payload_hash)
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

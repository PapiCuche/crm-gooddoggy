"""F7-03c: las funciones que reclaman, cierran, listan y purgan la ingesta de los webhooks de Meta,
y el enrutado del número a la cuenta (ADR-029 §3 y §4). Las tablas y el insert son de `0008`."""

from django.db import migrations

from core.db.operations import SecurityDefinerFunction

PG = "pg_catalog, public, pg_temp"  # pg_temp al final (F3-52)
VOLATILE = "plpgsql VOLATILE"
BAD = "RAISE EXCEPTION 'argumentos no válidos' USING ERRCODE = 'invalid_parameter_value'"


def function(
    name: str, arguments: str, argtypes: str, returns: str, body: str
) -> SecurityDefinerFunction:
    return SecurityDefinerFunction(
        name, arguments, argtypes, returns, body, language=VOLATILE, search_path=PG
    )


class Migration(migrations.Migration):
    dependencies = [
        ("conversations", "0008_webhook_ingress"),
    ]

    operations = [
        # Reclamar un lote: `SKIP LOCKED` solo dura lo que dura la llamada, así que el reclamo es
        # un estado con plazo (`PROCESSING` hasta `claimed_until`). Lo que se quedó sin soltar
        # (un proceso que murió) se reclama de nuevo, y tras 8 intentos pasa a `DEAD`.
        function(
            "webhook_ingress_claim",
            "p_limit integer, p_lease_seconds integer",
            "integer, integer",
            "TABLE (id uuid, kind text, waba_id text, phone_number_id text, payload jsonb, "
            "attempts integer)",
            """BEGIN
  WITH dead AS (
    UPDATE public.webhook_ingress w SET status = 'DEAD', claimed_until = NULL
    WHERE w.status = 'PROCESSING' AND w.claimed_until < now() AND w.attempts >= 8
    RETURNING w.id AS dead_id, w.attempts AS dead_attempts)
  INSERT INTO public.webhook_failures (ingress_id, attempt, reason)
  SELECT d.dead_id, d.dead_attempts, 'LEASE_EXPIRED' FROM dead d;
  RETURN QUERY
  WITH due AS (
    SELECT w.id AS due_id FROM public.webhook_ingress w
    WHERE (w.status IN ('RECEIVED', 'FAILED') AND w.next_attempt_at <= now())
       OR (w.status = 'PROCESSING' AND w.claimed_until < now())
    ORDER BY w.next_attempt_at, w.id
    LIMIT greatest(1, least(coalesce(p_limit, 1), 100))
    FOR UPDATE SKIP LOCKED),
  claimed AS (
    UPDATE public.webhook_ingress w
    SET status = 'PROCESSING', attempts = w.attempts + 1,
        claimed_until = now() + make_interval(
          secs => greatest(10, least(coalesce(p_lease_seconds, 60), 900)))
    FROM due WHERE w.id = due.due_id
    RETURNING w.id AS c_id, w.kind AS c_kind, w.waba_id AS c_waba,
              w.phone_number_id AS c_number, w.payload AS c_payload, w.attempts AS c_attempts)
  SELECT c.c_id, c.c_kind::text, c.c_waba::text, c.c_number::text, c.c_payload,
         c.c_attempts::integer FROM claimed c;
END""",
        ),
        # Cerrar un ítem reclamado, y solo por quien lo tiene: el número de intento es el del
        # reclamo, así que si caducó y otro lo reclamó, el primero ya no lo cierra. `PROCESSED` e
        # `IGNORED` vacían el payload; `FAILED` programa el siguiente intento (30 s, 2 min, 10 min,
        # 1 h, 6 h) o lo da por `DEAD` al octavo. La organización y la cuenta, si vienen, tienen
        # que ser una cuenta real de esa organización.
        function(
            "webhook_ingress_mark",
            "p_id uuid, p_attempt integer, p_status text, p_organization_id uuid, "
            "p_channel_account_id uuid, p_reason text",
            "uuid, integer, text, uuid, uuid, text",
            "text",
            f"""DECLARE v_status text;
BEGIN
  IF p_status IS NULL OR p_status NOT IN ('PROCESSED', 'IGNORED', 'FAILED')
     OR p_attempt IS NULL OR p_attempt < 1
     OR (p_reason IS NOT NULL AND p_reason !~ '^[A-Z_]{{1,32}}$')
     OR ((p_organization_id IS NULL) <> (p_channel_account_id IS NULL))
     OR (p_status = 'PROCESSED' AND p_organization_id IS NULL)
     OR (p_status = 'FAILED' AND p_organization_id IS NOT NULL)
  THEN {BAD}; END IF;
  IF p_organization_id IS NOT NULL AND NOT EXISTS (
       SELECT 1 FROM public.channel_accounts a
       WHERE a.id = p_channel_account_id AND a.organization_id = p_organization_id)
  THEN {BAD}; END IF;
  IF p_status = 'FAILED' THEN
    UPDATE public.webhook_ingress w
    SET status = CASE WHEN w.attempts >= 8 THEN 'DEAD' ELSE 'FAILED' END, claimed_until = NULL,
        next_attempt_at = now() + make_interval(
          secs => (ARRAY[30, 120, 600, 3600, 21600])[least(greatest(w.attempts, 1), 5)])
    WHERE w.id = p_id AND w.status = 'PROCESSING' AND w.attempts = p_attempt
    RETURNING w.status INTO v_status;
    IF v_status IS NOT NULL THEN
      INSERT INTO public.webhook_failures (ingress_id, attempt, reason)
      SELECT w.id, w.attempts, coalesce(p_reason, 'ERROR')
      FROM public.webhook_ingress w WHERE w.id = p_id;
    END IF;
  ELSE
    UPDATE public.webhook_ingress w
    SET status = p_status, payload = NULL, claimed_until = NULL, processed_at = now(),
        ignored_reason = CASE WHEN p_status = 'IGNORED' THEN p_reason END,
        routed_organization_id = p_organization_id,
        routed_channel_account_id = p_channel_account_id
    WHERE w.id = p_id AND w.status = 'PROCESSING' AND w.attempts = p_attempt
    RETURNING w.status INTO v_status;
  END IF;
  RETURN v_status;
END""",  # noqa: S608 — texto fijo, sin datos
        ),
        # Solo metadatos: ni el payload ni nada que lo contenga.
        function(
            "webhook_ingress_list",
            "p_status text, p_limit integer",
            "text, integer",
            "TABLE (id uuid, provider text, event_key text, kind text, status text, "
            "attempts integer, next_attempt_at timestamptz, ignored_reason text, "
            "organization_id uuid, channel_account_id uuid, received_at timestamptz, "
            "processed_at timestamptz, payload_hash text)",
            f"""BEGIN
  IF p_status IS NOT NULL AND p_status NOT IN
     ('RECEIVED', 'PROCESSING', 'PROCESSED', 'IGNORED', 'FAILED', 'DEAD')
  THEN {BAD}; END IF;
  RETURN QUERY
  SELECT w.id, w.provider::text, w.event_key::text, w.kind::text, w.status::text,
         w.attempts::integer, w.next_attempt_at, w.ignored_reason::text,
         w.routed_organization_id, w.routed_channel_account_id, w.received_at, w.processed_at,
         w.payload_hash::text
  FROM public.webhook_ingress w
  WHERE p_status IS NULL OR w.status = p_status
  ORDER BY w.received_at DESC, w.id DESC
  LIMIT greatest(1, least(coalesce(p_limit, 50), 200));
END""",  # noqa: S608 — texto fijo, sin datos
        ),
        # La purga: 90 días es el tope, no el plazo normal (el payload ya se vació al terminar).
        function(
            "webhook_ingress_purge",
            "p_days integer, p_limit integer",
            "integer, integer",
            "integer",
            """DECLARE v_count integer;
BEGIN
  WITH old AS (
    SELECT w.id AS old_id FROM public.webhook_ingress w
    WHERE w.received_at < now() - make_interval(days => greatest(1, coalesce(p_days, 90)))
    ORDER BY w.received_at LIMIT greatest(1, least(coalesce(p_limit, 1000), 10000))
    FOR UPDATE SKIP LOCKED),
  failures AS (
    DELETE FROM public.webhook_failures f WHERE f.ingress_id IN (SELECT old_id FROM old)),
  gone AS (
    DELETE FROM public.webhook_ingress w WHERE w.id IN (SELECT old_id FROM old) RETURNING 1)
  SELECT count(*)::integer INTO v_count FROM gone;
  RETURN v_count;
END""",
        ),
        # El enrutado (ADR-002 §3.3, ADR-029 §4): del número que informa Meta a la cuenta viva de
        # WhatsApp que lo registró. A lo sumo una fila y nada más que los dos ids; sin Sandbox ni
        # cuentas desconectadas. El WABA lo contrasta quien llama, dentro del tenant.
        SecurityDefinerFunction(
            "resolve_channel_account",
            "p_platform text, p_phone_number_id text",
            "text, text",
            "TABLE (organization_id uuid, channel_account_id uuid)",
            "SELECT a.organization_id, a.id FROM public.channel_accounts a "
            "WHERE p_platform = 'WHATSAPP' AND a.platform = 'WHATSAPP' "
            "AND a.external_account_id = p_phone_number_id AND a.deleted_at IS NULL LIMIT 1",
            search_path=PG,
        ),
    ]

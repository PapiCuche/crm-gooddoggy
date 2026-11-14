"""F7-11a: los conteos de la ingesta de Meta para las alertas (ADR-011, ADR-029). Una función
`SECURITY DEFINER` mínima (ADR-002 §3.3) que solo devuelve conteos por ventana y códigos cerrados:
ni ids, ni organizaciones, ni payload. `crm_app` sigue sin leer `webhook_ingress` ni
`webhook_failures` y sin cruzar tenants en `messages` ni en `channel_accounts`."""

from django.db import migrations

from core.db.operations import SecurityDefinerFunction

PG = "pg_catalog, public, pg_temp"  # pg_temp al final (F3-52)
BAD = "RAISE EXCEPTION 'argumentos no válidos' USING ERRCODE = 'invalid_parameter_value'"
DEAD_AT = (
    8  # el intento con el que un ítem pasa a `DEAD` (0009 y 0011); `RATE_LIMITED` no deja fila
)


class Migration(migrations.Migration):
    dependencies = [
        ("conversations", "0030_validate_outbound_file_purpose"),
    ]

    operations = [
        # `ingress_failed` y `ingress_dead`: las filas de `webhook_failures` de la ventana (cada
        # intento fallido deja una; la del octavo es la de `DEAD`). `ingress_lag_seconds`: cuánto
        # lleva esperando, ahora, el ítem vencido y sin reclamar más viejo (no depende de la
        # ventana). `token_invalid_count`: cuentas que pasaron a `ERROR` por el token en la ventana.
        # `outbound_failed`: salientes `FAILED` en la ventana por su código propio (conjunto cerrado
        # por `messages_error_code_ck`).
        SecurityDefinerFunction(
            "meta_ingest_counts",
            "p_from timestamptz, p_to timestamptz",
            "timestamptz, timestamptz",
            "TABLE (metric text, code text, total bigint)",
            f"""BEGIN
  IF p_from IS NULL OR p_to IS NULL OR p_from >= p_to OR p_to - p_from > interval '1 day'
  THEN {BAD}; END IF;
  RETURN QUERY
  SELECT 'ingress_failed'::text, NULL::text, count(*) FROM public.webhook_failures f
  WHERE f.created_at >= p_from AND f.created_at < p_to AND f.attempt < {DEAD_AT}
  UNION ALL
  SELECT 'ingress_dead'::text, NULL::text, count(*) FROM public.webhook_failures f
  WHERE f.created_at >= p_from AND f.created_at < p_to AND f.attempt >= {DEAD_AT}
  UNION ALL
  SELECT 'ingress_lag_seconds'::text, NULL::text, coalesce(floor(extract(epoch FROM now() - min(
           CASE WHEN w.status = 'PROCESSING' THEN w.claimed_until ELSE w.next_attempt_at END
         )))::bigint, 0)
  FROM public.webhook_ingress w
  WHERE (w.status IN ('RECEIVED', 'FAILED') AND w.next_attempt_at <= now())
     OR (w.status = 'PROCESSING' AND w.claimed_until < now())
  UNION ALL
  SELECT 'token_invalid_count'::text, NULL::text, count(*) FROM public.channel_accounts a
  WHERE a.status = 'ERROR' AND a.status_detail = 'TOKEN_INVALID'
    AND a.updated_at >= p_from AND a.updated_at < p_to
  UNION ALL
  SELECT 'outbound_failed'::text, m.provider_error_code::text, count(*) FROM public.messages m
  WHERE m.direction = 'OUTBOUND' AND m.delivery_status = 'FAILED'
    AND m.failed_at >= p_from AND m.failed_at < p_to
  GROUP BY m.provider_error_code;
END""",  # noqa: S608 — texto fijo, sin datos
            language="plpgsql STABLE",
            search_path=PG,
        ),
    ]

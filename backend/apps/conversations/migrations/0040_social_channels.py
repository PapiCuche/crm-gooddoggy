"""F7b-04a (ADR-035 §3–§5): Instagram y Messenger en el esquema, sin tocar `0008` ni `0009`.

- `channel_accounts`: las plataformas `MESSENGER` (una página) e `INSTAGRAM` (una cuenta profesional
  con su página como padre), con un CHECK cada una; la unicidad global de `external_account_id` ya
  impide que dos organizaciones conecten la misma página o cuenta.
- `contact_identities`: el PSID o el IGSID de un contacto, únicos por cuenta de canal; tabla del
  tenant con FORCE RLS y su política, FK compuestas (la plataforma, la de la cuenta) y solo
  `SELECT` e `INSERT` para `crm_app` (inmutable).
- `conversations.contact_identity_id`: del mismo contacto y cuenta que el hilo (FK compuesta) y
  obligatoria en Instagram y Messenger. Un CHECK no puede leer la plataforma de otra tabla: lo
  exige un disparador (`conversations_identity_required`, INVOKER, bajo la RLS de quien escribe).
- `webhook_ingress`: la plataforma y el id de enrutado (`routing_id`, el `entry.id`) en columnas
  propias, con `waba_id` solo para WhatsApp; `webhook_ingress_insert_social` y
  `webhook_ingress_refuse_social` son las versiones con plataforma, y `resolve_social_account` el
  enrutado mínimo (ADR-002 §3.3). Hasta F7b-04b el procesador cierra un ítem social `IGNORED`
  (`NO_NUMBER`): `webhook_ingress_claim` no devuelve número ni WABA para él.

Toca `conversations`, `webhook_ingress` y `channel_accounts` con bloqueos exclusivos breves (solo
de catálogo): `lock_timeout` de 5 s en los dos sentidos, para no encolar el Inbox y el webhook tras
una transacción larga. `conversations_contact_identity_fk` y `webhook_ingress_route_ck` nacen
`NOT VALID` (sin recorrer las tablas bajo ese bloqueo) y los valida la `0041`. Hacia atrás se niega
si ya hay identidades (datos personales) y dice cuántas, en vez de borrarlas.
"""

from typing import Any

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

import core.ids
from core.db.operations import EnableRLS, SecurityDefinerFunction

PG = "pg_catalog, public, pg_temp"  # pg_temp al final (F3-52)
BAD = "RAISE EXCEPTION 'argumentos no válidos' USING ERRCODE = 'invalid_parameter_value'"
CHECKS = """p_provider IS NULL OR p_provider <> 'meta'
     OR p_kind IS NULL OR p_kind NOT IN ('message', 'status', 'other')
     OR p_platform IS NULL OR p_platform NOT IN ('MESSENGER', 'INSTAGRAM')
     OR p_event_key IS NULL OR length(p_event_key) NOT BETWEEN 4 AND 700
     OR p_event_key !~ '^[A-Za-z0-9._~%%:-]+$'
     OR (p_platform = 'MESSENGER' AND left(p_event_key, 3) <> 'fb:')
     OR (p_platform = 'INSTAGRAM' AND left(p_event_key, 3) <> 'ig:')
     OR p_routing_id IS NULL OR p_routing_id !~ '^[0-9]{5,30}$'
     OR p_payload IS NULL OR jsonb_typeof(p_payload) <> 'object'"""
SOCIAL_ARGS = "p_provider text, p_event_key text, p_kind text, p_platform text, p_routing_id text"
IDENTITY_REQUIRED = """
CREATE FUNCTION public.conversations_identity_required() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, public, pg_temp AS $fn$
BEGIN
    IF NEW.contact_identity_id IS NULL AND EXISTS (
        SELECT 1 FROM public.channel_accounts a
        WHERE a.organization_id = NEW.organization_id AND a.id = NEW.channel_account_id
          AND a.platform IN ('MESSENGER', 'INSTAGRAM')
    ) THEN
        RAISE EXCEPTION 'conversations_identity_required: el hilo de Instagram o Messenger lleva '
            'su identidad' USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$fn$;
CREATE TRIGGER conversations_identity_required
    BEFORE INSERT OR UPDATE OF contact_identity_id, channel_account_id ON public.conversations
    FOR EACH ROW EXECUTE FUNCTION public.conversations_identity_required();
"""
LOCK = "SET LOCAL lock_timeout = '5s'"
ROUTE_CK = (
    "ALTER TABLE public.webhook_ingress ADD CONSTRAINT webhook_ingress_route_ck CHECK ("
    "(platform = 'WHATSAPP' AND waba_id IS NOT NULL AND routing_id IS NULL) OR "
    "(platform IN ('MESSENGER', 'INSTAGRAM') AND waba_id IS NULL AND phone_number_id IS NULL"
    " AND routing_id IS NOT NULL)) NOT VALID"
)
DROP_IDENTITY_REQUIRED = """
DROP TRIGGER IF EXISTS conversations_identity_required ON public.conversations;
DROP FUNCTION IF EXISTS public.conversations_identity_required();
"""


def fk(table: str, name: str, columns: str, target: str, valid: str = "") -> migrations.RunSQL:
    return migrations.RunSQL(
        f"ALTER TABLE public.{table} ADD CONSTRAINT {name} FOREIGN KEY ({columns}) "
        f"REFERENCES public.{target}{valid}",
        f"ALTER TABLE public.{table} DROP CONSTRAINT IF EXISTS {name}",
    )


def _privileges(apps: Any, schema_editor: Any) -> None:
    role = schema_editor.quote_name(settings.DB_APP_ROLE)  # inmutable: ni UPDATE ni DELETE
    schema_editor.execute(
        f"REVOKE UPDATE, DELETE, TRUNCATE ON public.contact_identities FROM {role}"
    )


def _restore(apps: Any, schema_editor: Any) -> None:
    role = schema_editor.quote_name(settings.DB_APP_ROLE)
    schema_editor.execute(f"GRANT UPDATE, DELETE ON public.contact_identities TO {role}")


def refuse_identities(apps: Any, schema_editor: Any) -> None:
    """Hacia atrás: borrar `contact_identities` perdería datos personales sin aviso; se niega."""
    with schema_editor.connection.cursor() as cursor:
        cursor.execute("SELECT rolbypassrls FROM pg_roles WHERE rolname = current_user")
        if not cursor.fetchone()[0]:
            raise RuntimeError(
                "conversations 0040: run as crm_migrator with BYPASSRLS (RLS hides rows)"
            )
        cursor.execute("SELECT count(*) FROM public.contact_identities")
        count = cursor.fetchone()[0]
    if count:
        raise RuntimeError(
            f"conversations 0040: {count} contact identities exist; going back would drop them"
        )


def function(name: str, extra: str, extra_types: str, body: str) -> SecurityDefinerFunction:
    return SecurityDefinerFunction(
        name, f"{SOCIAL_ARGS}, p_payload jsonb{extra}",
        f"text, text, text, text, text, jsonb{extra_types}", "uuid", body,
        language="plpgsql VOLATILE", search_path=PG,
    )  # fmt: skip


class Migration(migrations.Migration):
    dependencies = [
        ("contacts", "0008_consent_keywords"),
        ("conversations", "0039_quick_reply_categories_seed"),
    ]

    operations = [
        migrations.RunSQL(LOCK, migrations.RunSQL.noop),
        migrations.CreateModel(
            name="ContactIdentity",
            fields=[
                ("organization_id", models.UUIDField(db_index=True, editable=False)),
                (
                    "id",
                    models.UUIDField(
                        db_default=models.Func(function="uuidv7", output_field=models.UUIDField()),
                        default=core.ids.new_id,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                (
                    "platform",
                    models.CharField(
                        choices=[
                            ("SANDBOX", "Sandbox"),
                            ("WHATSAPP", "Whatsapp"),
                            ("MESSENGER", "Messenger"),
                            ("INSTAGRAM", "Instagram"),
                        ],
                        max_length=16,
                    ),
                ),
                ("external_id", models.CharField(max_length=100)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={
                "db_table": "contact_identities",
            },
        ),
        migrations.RemoveConstraint(
            model_name="channelaccount",
            name="channel_accounts_platform_ck",
        ),
        migrations.AddField(
            model_name="webhookingress",
            name="platform",
            field=models.CharField(db_default="WHATSAPP", max_length=16),
        ),
        migrations.AddField(
            model_name="webhookingress",
            name="routing_id",
            field=models.CharField(max_length=30, null=True),
        ),
        migrations.AlterField(
            model_name="channelaccount",
            name="platform",
            field=models.CharField(
                choices=[
                    ("SANDBOX", "Sandbox"),
                    ("WHATSAPP", "Whatsapp"),
                    ("MESSENGER", "Messenger"),
                    ("INSTAGRAM", "Instagram"),
                ],
                max_length=16,
            ),
        ),
        migrations.AlterField(
            model_name="webhookingress",
            name="waba_id",
            field=models.CharField(max_length=30, null=True),
        ),
        migrations.AddConstraint(
            model_name="channelaccount",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("platform__in", ["SANDBOX", "WHATSAPP", "MESSENGER", "INSTAGRAM"])
                ),
                name="channel_accounts_platform_ck",
            ),
        ),
        migrations.AddConstraint(
            model_name="channelaccount",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("platform", "MESSENGER"), _negated=True),
                    models.Q(
                        ("external_account_id__regex", "^[0-9]{5,30}$"),
                        ("external_parent_id__isnull", True),
                        ("messaging_limit_tier__isnull", True),
                        ("quality_rating__isnull", True),
                    ),
                    _connector="OR",
                ),
                name="channel_accounts_messenger_ck",
            ),
        ),
        migrations.AddConstraint(
            model_name="channelaccount",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("platform", "INSTAGRAM"), _negated=True),
                    models.Q(
                        ("external_account_id__regex", "^[0-9]{5,30}$"),
                        ("external_parent_id__isnull", False),
                        ("external_parent_id__regex", "^[0-9]{5,30}$"),
                        ("messaging_limit_tier__isnull", True),
                        ("quality_rating__isnull", True),
                    ),
                    _connector="OR",
                ),
                name="channel_accounts_instagram_ck",
            ),
        ),
        migrations.AddConstraint(
            model_name="channelaccount",
            constraint=models.UniqueConstraint(
                fields=("organization_id", "id", "platform"),
                name="channel_accounts_org_platform_uq",
            ),
        ),
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AddConstraint(
                    model_name="webhookingress",
                    constraint=models.CheckConstraint(
                        condition=models.Q(
                            models.Q(
                                ("platform", "WHATSAPP"),
                                ("routing_id__isnull", True),
                                ("waba_id__isnull", False),
                            ),
                            models.Q(
                                ("phone_number_id__isnull", True),
                                ("platform__in", ("MESSENGER", "INSTAGRAM")),
                                ("routing_id__isnull", False),
                                ("waba_id__isnull", True),
                            ),
                            _connector="OR",
                        ),
                        name="webhook_ingress_route_ck",
                    ),
                ),
            ],
            database_operations=[
                migrations.RunSQL(
                    ROUTE_CK,
                    "ALTER TABLE public.webhook_ingress DROP CONSTRAINT webhook_ingress_route_ck",
                )
            ],
        ),
        migrations.AddField(
            model_name="contactidentity",
            name="channel_account",
            field=models.ForeignKey(
                db_constraint=False,
                db_index=False,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="conversations.channelaccount",
            ),
        ),
        migrations.AddField(
            model_name="contactidentity",
            name="contact",
            field=models.ForeignKey(
                db_constraint=False,
                db_index=False,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="contacts.contact",
            ),
        ),
        migrations.AddField(
            model_name="conversation",
            name="contact_identity",
            field=models.ForeignKey(
                blank=True,
                db_constraint=False,
                db_index=False,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="conversations.contactidentity",
            ),
        ),
        migrations.AddIndex(
            model_name="contactidentity",
            index=models.Index(
                fields=["organization_id", "contact"], name="contact_identities_contact_idx"
            ),
        ),
        migrations.AddConstraint(
            model_name="contactidentity",
            constraint=models.UniqueConstraint(
                fields=("organization_id", "id"), name="contact_identities_org_id_uq"
            ),
        ),
        migrations.AddConstraint(
            model_name="contactidentity",
            constraint=models.UniqueConstraint(
                fields=("organization_id", "id", "contact", "channel_account"),
                name="contact_identities_thread_uq",
            ),
        ),
        migrations.AddConstraint(
            model_name="contactidentity",
            constraint=models.UniqueConstraint(
                fields=("organization_id", "channel_account", "external_id"),
                name="contact_identities_external_uq",
            ),
        ),
        migrations.AddConstraint(
            model_name="contactidentity",
            constraint=models.CheckConstraint(
                condition=models.Q(("platform__in", ("MESSENGER", "INSTAGRAM"))),
                name="contact_identities_platform_ck",
            ),
        ),
        migrations.AddConstraint(
            model_name="contactidentity",
            constraint=models.CheckConstraint(
                condition=models.Q(("external_id__regex", "^[A-Za-z0-9._-]{1,100}$")),
                name="contact_identities_external_ck",
            ),
        ),
        EnableRLS("contactidentity"),
        migrations.RunPython(_privileges, _restore),
        fk(
            "contact_identities",
            "contact_identities_organization_fk",
            "organization_id",
            "organizations (id)",
        ),
        fk(
            "contact_identities",
            "contact_identities_contact_fk",
            "organization_id, contact_id",
            "contacts (organization_id, id)",
        ),
        # La plataforma de la identidad es la de su cuenta (y por el CHECK, Instagram o Messenger).
        fk(
            "contact_identities",
            "contact_identities_account_fk",
            "organization_id, channel_account_id, platform",
            "channel_accounts (organization_id, id, platform)",
        ),
        # La identidad de un hilo es de su mismo contacto y su misma cuenta.
        fk(
            "conversations",
            "conversations_contact_identity_fk",
            "organization_id, contact_identity_id, contact_id, channel_account_id",
            "contact_identities (organization_id, id, contact_id, channel_account_id)",
            " NOT VALID",  # la valida la 0041
        ),
        migrations.RunSQL(IDENTITY_REQUIRED, DROP_IDENTITY_REQUIRED),
        # Guardar un ítem de Instagram o Messenger: como `webhook_ingress_insert` (0008, con el tope
        # de 0033) con la plataforma y el `entry.id`, y una clave del espacio de su plataforma
        # (`fb:` o `ig:`): un evento de un canal nunca ocupa la clave de otro.
        function(
            "webhook_ingress_insert_social",
            "",
            "",
            f"""DECLARE v_id uuid;
BEGIN
  IF {CHECKS}
     OR octet_length(p_payload::text) > 8388608
  THEN {BAD}; END IF;
  INSERT INTO public.webhook_ingress
    (provider, event_key, kind, platform, routing_id, payload, payload_hash)
  VALUES (p_provider, p_event_key, p_kind, p_platform, p_routing_id, p_payload,
          encode(sha256(convert_to(p_payload::text, 'UTF8')), 'hex'))
  ON CONFLICT (provider, event_key) DO NOTHING
  RETURNING webhook_ingress.id INTO v_id;
  RETURN v_id;
END""",  # noqa: S608 — texto fijo, sin datos
        ),
        # El registro de un ítem que la base nunca guardará (como `webhook_ingress_refuse`, 0033).
        function(
            "webhook_ingress_refuse_social",
            ", p_reason text",
            ", text",
            f"""DECLARE v_id uuid;
BEGIN
  IF {CHECKS}
     OR p_reason IS NULL OR p_reason !~ '^REFUSED(_[0-9A-Z]{{5}})?$'
  THEN {BAD}; END IF;
  INSERT INTO public.webhook_ingress
    (provider, event_key, kind, platform, routing_id, status, payload, payload_hash)
  VALUES (p_provider, p_event_key, p_kind, p_platform, p_routing_id, 'DEAD', NULL,
          encode(sha256(convert_to(p_payload::text, 'UTF8')), 'hex'))
  ON CONFLICT (provider, event_key) DO NOTHING
  RETURNING webhook_ingress.id INTO v_id;
  IF v_id IS NOT NULL THEN
    INSERT INTO public.webhook_failures (ingress_id, attempt, reason) VALUES (v_id, 8, p_reason);
  END IF;
  RETURN v_id;
END""",  # noqa: S608 — texto fijo, sin datos
        ),
        # El enrutado (ADR-035 §4): del `entry.id` firmado a la cuenta viva de Instagram o Messenger
        # que lo registró. A lo sumo una fila y solo los dos ids; nada de WhatsApp ni del Sandbox.
        # Argumentos validados dentro (ADR-002 §3.3): otra plataforma o un id que no es de Meta es
        # un error, no una búsqueda.
        SecurityDefinerFunction(
            "resolve_social_account",
            "p_platform text, p_external_id text",
            "text, text",
            "TABLE (organization_id uuid, channel_account_id uuid)",
            f"""BEGIN
  IF p_platform IS NULL OR p_platform NOT IN ('MESSENGER', 'INSTAGRAM')
     OR p_external_id IS NULL OR p_external_id !~ '^[0-9]{{5,30}}$'
  THEN {BAD}; END IF;
  RETURN QUERY SELECT a.organization_id, a.id FROM public.channel_accounts a
  WHERE a.platform = p_platform AND a.external_account_id = p_external_id
    AND a.deleted_at IS NULL LIMIT 1;
END""",  # noqa: S608 — texto fijo, sin datos
            language="plpgsql STABLE",
            search_path=PG,
        ),
        migrations.RunPython(migrations.RunPython.noop, refuse_identities),
        migrations.RunSQL(migrations.RunSQL.noop, LOCK),  # hacia atrás corre primero
    ]

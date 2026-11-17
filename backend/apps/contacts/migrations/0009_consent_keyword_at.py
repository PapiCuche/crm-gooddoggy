"""F7b-05d (ADR-035 §5, ADR-010 §2.7): `contact_consents.keyword_at`, el instante del proveedor
de la última palabra de baja o alta de la fila. Meta entrega fuera de orden: una palabra más vieja
que esa no revierte el consentimiento.

Columna nula sin valor por omisión: solo cambia el catálogo, con un bloqueo exclusivo breve y
`lock_timeout` de 5 s en los dos sentidos. `crm_app` la puede actualizar (UPDATE por columna, como
las demás que cambian). Hacia atrás se borra la columna: se pierde solo la marca de orden; el
consentimiento vigente y su historial quedan como estaban.
"""

from typing import Any

from django.conf import settings
from django.db import migrations, models

LOCK = "SET LOCAL lock_timeout = '5s'"


def _privileges(apps: Any, schema_editor: Any) -> None:
    role = schema_editor.quote_name(settings.DB_APP_ROLE)
    schema_editor.execute(f"GRANT UPDATE (keyword_at) ON public.contact_consents TO {role}")


class Migration(migrations.Migration):
    dependencies = [
        ("contacts", "0008_consent_keywords"),
    ]

    operations = [
        migrations.RunSQL(LOCK, migrations.RunSQL.noop),
        migrations.AddField(
            model_name="contactconsent",
            name="keyword_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.RunPython(_privileges, migrations.RunPython.noop),  # la columna se lleva el suyo
        migrations.RunSQL(migrations.RunSQL.noop, LOCK),  # hacia atrás corre primero
    ]

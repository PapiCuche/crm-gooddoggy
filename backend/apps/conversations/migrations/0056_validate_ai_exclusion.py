"""F8-13: valida el `CHECK` de la exclusión de la IA que la `0055` añadió `NOT VALID` (con
`lock_timeout` de 5 s que se quita aunque falle; hacia atrás queda, validado) y da a
`ai_excluded_by_user_id` la FK compuesta a las membresías de ADR-001 §3
(`conversations_ai_excluded_by_fk`, con `core.db.member_fk`). **No atómica**, como la `0054`."""

from typing import Any

from django.db import migrations

from core.db.member_fk import MemberFKs

NAME = "conversations_ai_excluded_ck"
FKS = MemberFKs(
    "conversations 0056",
    (("conversations", "ai_excluded_by_user_id", "conversations_ai_excluded_by_fk"),),
)


def validate(apps: Any, schema_editor: Any) -> None:
    with schema_editor.connection.cursor() as cursor:
        cursor.execute("SET lock_timeout = '5s'")
        try:
            cursor.execute(f"ALTER TABLE public.conversations VALIDATE CONSTRAINT {NAME}")
        finally:
            cursor.execute("RESET lock_timeout")


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        ("conversations", "0055_conversation_ai_exclusion"),
    ]

    operations = [
        migrations.RunPython(validate, migrations.RunPython.noop),
        migrations.RunPython(FKS.add, FKS.drop),
    ]

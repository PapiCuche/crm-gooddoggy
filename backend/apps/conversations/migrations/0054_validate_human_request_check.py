"""F8-08n: valida el `CHECK` de la solicitud de atención humana que la `0053` añadió `NOT VALID`
(`VALIDATE CONSTRAINT` recorre la tabla con un bloqueo que no impide leer ni escribir; hacia atrás
queda, validado) y da a `human_requested_by_user_id` la FK compuesta a las membresías de ADR-001 §3
(`conversations_human_requested_by_fk`, con `core.db.member_fk`: `NOT VALID` y `VALIDATE`,
`lock_timeout`, repetible y reversible). **No atómica**, como la `0034`."""

from django.db import migrations

from core.db.member_fk import MemberFKs

NAME = "conversations_human_request_ck"
FKS = MemberFKs(
    "conversations 0054",
    (("conversations", "human_requested_by_user_id", "conversations_human_requested_by_fk"),),
)


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        ("conversations", "0053_conversation_human_request"),
    ]

    operations = [
        migrations.RunSQL(
            f"ALTER TABLE public.conversations VALIDATE CONSTRAINT {NAME}", migrations.RunSQL.noop
        ),
        migrations.RunPython(FKS.add, FKS.drop),
    ]

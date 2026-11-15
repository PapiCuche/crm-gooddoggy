"""F2-128 (#819): quien sube un archivo es miembro de su organización. `files.created_by_user_id`
recibe la FK compuesta `files_created_by_fk` de ADR-001 §3 con el patrón de `catalog/0005`
(`core.db.member_fk`: niega las filas huérfanas con su cuenta, `NOT VALID` y `VALIDATE`,
`lock_timeout`, repetible y reversible). El adjunto que llega de un webhook (sistema) sigue sin
persona: un `NULL` vale."""

from django.db import migrations

from core.db.member_fk import MemberFKs

FKS = MemberFKs("files 0004", (("files", "created_by_user_id", "files_created_by_fk"),))


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        ("files", "0003_message_media_out"),
        ("organizations", "0003_organization_memberships"),
    ]

    operations = [migrations.RunPython(FKS.add, FKS.drop)]

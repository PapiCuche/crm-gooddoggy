"""F2-128 (#819): quien guarda el token de una cuenta es miembro de su organización.
`credentials.created_by_user_id` recibe la FK compuesta `credentials_created_by_fk` de ADR-001 §3
con el patrón de `catalog/0005` (`core.db.member_fk`: niega las filas huérfanas con su cuenta,
`NOT VALID` y `VALIDATE`, `lock_timeout`, repetible y reversible). Un token guardado sin persona
(contexto de sistema) sigue valiendo: un `NULL` no se comprueba."""

from django.db import migrations

from core.db.member_fk import MemberFKs

FKS = MemberFKs(
    "conversations 0034", (("credentials", "created_by_user_id", "credentials_created_by_fk"),)
)


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        ("conversations", "0033_webhook_ingress_refuse"),
    ]

    operations = [migrations.RunPython(FKS.add, FKS.drop)]

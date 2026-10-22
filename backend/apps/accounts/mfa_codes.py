"""Regenerar los códigos de respaldo propios (F2-110, ADR-023 §6).

Con sesión, la contraseña actual y un código **de la aplicación de autenticación**: uno de
respaldo no sirve para pedir otros diez. Los anteriores dejan de valer todos, gastados o no, y
los nuevos se enseñan una sola vez; se guarda su SHA-256.
"""

from django.http import HttpRequest

from apps.accounts import backup_codes, mfa, second_factor
from apps.accounts.mfa_disable import with_password_and_code
from apps.accounts.models import MfaBackupCode, User
from apps.accounts.services import client_of
from apps.audit import platform
from apps.audit.services import Entity

REGENERATED = "auth.mfa.backup_codes_regenerated"  # ADR-023 §10 dejó el nombre a su work item


def regenerate(request: HttpRequest, *, password: str, code: str) -> list[str]:
    """Sustituye los códigos de respaldo de la cuenta con sesión por diez nuevos y los devuelve,
    una sola vez (ADR-023 §6). Comprueba la contraseña y el código como al quitar el factor
    (`with_password_and_code`), con una diferencia: solo vale un código TOTP. Uno de respaldo
    se rechaza como un código que no vale (`mfa.WrongCode`) y **no queda gastado**.

    En esa transacción: borra todos los códigos de la cuenta, guarda los hashes de los nuevos y
    audita. Sin esa fila no cambia nada: los anteriores siguen valiendo.
    """
    client = client_of(request)

    def work(account: User, method: str) -> list[str]:
        if method != second_factor.TOTP:
            raise mfa.WrongCode  # deshace la transacción: ese código de respaldo no se gasta
        codes = backup_codes.new_codes()
        MfaBackupCode.objects.filter(user=account).delete()
        hashes = [backup_codes.code_hash(shown) or "" for shown in codes]
        MfaBackupCode.objects.bulk_create(
            MfaBackupCode(user=account, code_hash=hashed) for hashed in hashes
        )
        platform.record(  # en la transacción: sin esta fila, los anteriores siguen
            REGENERATED,
            actor_type=platform.Actor.USER,
            actor_id=account.pk,
            entity=Entity("user", account.pk),
            **client,
        )
        return codes

    return with_password_and_code(
        request, password=password, code=code, action=REGENERATED, work=work
    )

"""`manage.py add_member`: un operador añade un miembro a una organización (F2-127, D-F2-14 B).

    manage.py add_member --org acme --email luis@acme.pe --role seller \
        --reason "alta pedida por el Owner"

Solo en el servidor: no es una ruta HTTP. El rol es uno de plantilla del catálogo, nunca Owner.
Si la cuenta no existe, el comando pide dos veces, sin mostrarla, una contraseña temporal (o la lee
de la entrada estándar con `--password-stdin`): no abre sesión hasta que la cuenta elija la suya
con el enlace de recuperación. Una cuenta que ya existe conserva la suya. Si ya es miembro, lo
dice y no cambia nada. La contraseña nunca va en un argumento, en la salida ni en el log.
"""

import getpass
from argparse import ArgumentParser
from typing import Any

from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.core.management.base import CommandError

from apps.accounts.emails import canonical_email
from apps.provisioning import members
from apps.provisioning.management.commands.bootstrap_organization import read_password
from core.tenancy.commands import PlatformCommand


class Command(PlatformCommand):
    help = "Añade a una organización un miembro con un rol de plantilla (nunca Owner)."

    def add_arguments(self, parser: ArgumentParser) -> None:
        super().add_arguments(parser)
        parser.add_argument("--org", required=True, help="Slug de la organización")
        parser.add_argument("--email", required=True, help="Correo de la cuenta del miembro")
        roles = ", ".join(sorted(members.ROLES))
        parser.add_argument("--role", required=True, help=f"Rol de plantilla: {roles}")
        parser.add_argument(
            "--password-stdin",
            action="store_true",
            help="Lee de la entrada estándar (una línea) la contraseña temporal de la cuenta nueva",
        )

    def handle_platform(self, **options: Any) -> None:
        slug, role = options["org"], options["role"]
        try:
            email = canonical_email(options["email"])
            members.check_role(role)  # antes de pedir nada
            # La contraseña se pide antes de abrir ninguna transacción, y solo si falta la cuenta.
            password = (
                read_password(options["password_stdin"], "temporal de la cuenta nueva")
                if members.needs_password(email)
                else None
            )
            result = members.add_member(
                slug=slug,
                email=email,
                role=role,
                password=password,
                operator=getpass.getuser(),
                reason=options["reason"],
            )
        except members.OwnerRole:
            raise CommandError("El rol Owner no se da con este comando") from None
        except members.UnknownRole:
            known = ", ".join(sorted(members.ROLES))
            raise CommandError(f"Rol desconocido: usa uno de {known}") from None
        except members.UnknownOrganization:
            raise CommandError("Organización no encontrada") from None
        except ObjectDoesNotExist:
            raise CommandError("La organización no tiene ese rol de plantilla") from None
        except ValidationError as error:
            raise CommandError("; ".join(error.messages)) from None
        if not result.added:
            self.stdout.write(
                f"Esa cuenta ya es miembro de {slug} (membresía {result.membership_id}):"
                " no se cambió nada."
            )
            return
        account = "nueva" if result.user_created else "existente, con su contraseña"
        self.stdout.write(
            f"Miembro añadido a {slug}: membresía {result.membership_id}, rol {role},"
            f" cuenta {account} {result.user_id}."
        )
        if result.user_created:
            self.stdout.write(
                "La contraseña es temporal: no abre sesión. La persona elige la suya en"
                " «¿Olvidaste tu contraseña?» con su correo."
            )

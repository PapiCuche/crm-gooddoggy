"""`manage.py bootstrap_organization`: alta controlada de una organización (F2-06).

    manage.py bootstrap_organization --slug acme --name "Acme SAC" \
        --owner-email ana@acme.pe --reason "alta del cliente"

El Owner es una cuenta nueva: el comando pide su contraseña dos veces, sin mostrarla. En un
entorno sin terminal se pasa por la entrada estándar con `--password-stdin`. Para dar el rol a
una cuenta que ya existe hay que decirlo con `--existing-owner`; conserva su contraseña. La
contraseña nunca va en un argumento, en el log ni en la auditoría.
"""

import getpass
import sys
from argparse import ArgumentParser
from typing import Any

from django.core.exceptions import ValidationError
from django.core.management.base import CommandError

from apps.organizations.bootstrap import SlugTaken
from apps.provisioning.services import bootstrap_organization
from core.tenancy.commands import PlatformCommand


def read_password(from_stdin: bool, whose: str = "del Owner") -> str:
    """La contraseña, de la entrada estándar (`--password-stdin`) o pedida dos veces sin eco."""
    if from_stdin:
        # Bytes y UTF-8, no el locale del proceso: la contraseña es la que se escribió.
        stream = getattr(sys.stdin, "buffer", None)
        password: str | None = None
        try:
            password = stream.read().decode("utf-8-sig") if stream else None
        except UnicodeDecodeError:
            pass
        if password is None:
            raise CommandError("--password-stdin espera texto UTF-8 por la entrada estándar")
        password = password.removesuffix("\n").removesuffix("\r")
        if "\n" in password:
            raise CommandError("--password-stdin espera una sola línea")
        return password
    try:
        first = getpass.getpass(f"Contraseña {whose}: ")
        if first != getpass.getpass("Repítela: "):
            raise CommandError("Las contraseñas no coinciden")
    except EOFError:
        raise CommandError("Sin terminal: usa --password-stdin") from None
    return first


class Command(PlatformCommand):
    help = "Crea una organización con su Owner inicial y sus roles plantilla."

    def add_arguments(self, parser: ArgumentParser) -> None:
        super().add_arguments(parser)
        parser.add_argument("--slug", required=True)
        parser.add_argument("--name", required=True)
        parser.add_argument("--owner-email", required=True)
        owner = parser.add_mutually_exclusive_group()
        owner.add_argument(
            "--password-stdin",
            action="store_true",
            help="Lee de la entrada estándar (una línea) la contraseña del Owner nuevo",
        )
        owner.add_argument(
            "--existing-owner",
            action="store_true",
            help="El Owner es una cuenta que ya existe: conserva su contraseña",
        )

    def handle_platform(self, **options: Any) -> None:
        existing = options["existing_owner"]
        if existing and options["password_stdin"]:  # argparse no lo comprueba en `call_command`
            raise CommandError("--existing-owner no lleva contraseña: sobra --password-stdin")
        try:
            result = bootstrap_organization(
                slug=options["slug"],
                name=options["name"],
                owner_email=options["owner_email"],
                owner_password=None if existing else read_password(options["password_stdin"]),
                operator=getpass.getuser(),
                reason=options["reason"],
            )
        except SlugTaken:
            raise CommandError("Ya existe una organización con ese slug") from None
        except ValidationError as error:
            raise CommandError("; ".join(error.messages)) from None
        created = "nuevo" if result.user_created else "existente"
        self.stdout.write(
            f"Organización {result.slug} ({result.organization_id}) creada. "
            f"Owner: usuario {created} {result.owner_user_id}."
        )

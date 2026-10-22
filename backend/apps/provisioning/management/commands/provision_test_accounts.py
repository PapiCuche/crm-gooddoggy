"""`manage.py provision_test_accounts`: la organización de pruebas y sus cuatro cuentas (ADR-026).

    manage.py provision_test_accounts --reason "pruebas locales"

Solo con `DJANGO_TEST_ACCOUNTS_ORGANIZATION` activo (nunca en production). Idempotente. La
contraseña de las cuentas nuevas se pide dos veces sin mostrarla, o llega por la entrada
estándar con `--password-stdin`; las que ya existen conservan la suya. Nunca se imprime.
"""

import getpass
from argparse import ArgumentParser
from typing import Any

from django.core.exceptions import ValidationError
from django.core.management.base import CommandError

from apps.provisioning import test_accounts
from apps.provisioning.management.commands.bootstrap_organization import read_password
from core.tenancy.commands import PlatformCommand


class Command(PlatformCommand):
    help = (
        "Provisiona la organización de pruebas con su Owner, Administrador, Supervisor y Vendedor."
    )

    def add_arguments(self, parser: ArgumentParser) -> None:
        super().add_arguments(parser)
        parser.add_argument(
            "--password-stdin",
            action="store_true",
            help="Lee de la entrada estándar (una línea) la contraseña de las cuentas nuevas",
        )

    def handle_platform(self, **options: Any) -> None:
        try:
            missing = test_accounts.missing_accounts()
            result = test_accounts.provision(
                password=lambda: read_password(options["password_stdin"], "de las cuentas nuevas"),
                operator=getpass.getuser(),
                reason=options["reason"],
            )
        except test_accounts.Disabled:
            raise CommandError(
                "Accesos de prueba desactivados: falta DJANGO_TEST_ACCOUNTS_ORGANIZATION"
            ) from None
        except test_accounts.NotTestOrganization:
            raise CommandError(
                "Esa organización existe y no es la de pruebas: no se toca"
            ) from None
        except ValidationError as error:
            raise CommandError("; ".join(error.messages)) from None
        self.stdout.write(
            f"Organización de pruebas {result.slug}"
            f" ({'creada' if result.organization_created else 'ya existía'})."
            f" Cuentas nuevas: {len(result.accounts_created)} de {len(missing)} que faltaban;"
            f" membresías añadidas: {len(result.memberships_added)};"
            f" roles añadidos: {len(result.roles_added)}."
        )
        for email in test_accounts.emails(result.slug):
            self.stdout.write(f"  {email}")

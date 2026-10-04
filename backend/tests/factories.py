"""Fábricas de datos de test (sin dependencias externas)."""

import time
from itertools import count
from typing import Any

from django.test import Client

from apps.accounts.models import User
from apps.accounts.services import AUTH_AT, EPOCH, SEEN_AT

TEST_PASSWORD = "correct-horse-battery-staple"  # noqa: S105 — dato ficticio de test
_sequence = count(1)


def make_user(**overrides: Any) -> User:
    fields: dict[str, Any] = {
        "email": f"user{next(_sequence)}@example.com",
        "password": TEST_PASSWORD,
        **overrides,
    }
    return User.objects.create_user(**fields)


def sign_in(client: Client, user: User) -> None:
    """Sesión de `user` sin pasar por el login: `force_login` más las marcas que pone el
    servicio. Sin ellas, la sesión se considera caducada o revocada (F2-03A, F2-20)."""
    client.force_login(user)
    session = client.session
    session[AUTH_AT] = session[SEEN_AT] = int(time.time())
    session[EPOCH] = user.session_epoch
    session.save()

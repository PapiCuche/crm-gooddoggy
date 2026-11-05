"""F6-02a: `RecencyPagination` (ADR-016 §6), el cursor de dos valores (fecha, id) de las listas que
van de lo más reciente a lo más antiguo. Sobre conversaciones reales, con el rol `crm_app`."""

import base64
import datetime as dt
import json
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from django.utils import timezone
from rest_framework.exceptions import ValidationError
from rest_framework.request import Request
from rest_framework.test import APIRequestFactory

from apps.conversations.models import Conversation
from core.api.pagination import RecencyPagination
from core.tenancy.scope import tenant_scope
from tests.test_conversations_tables import fresh, world
from tests.test_memberships import ctx

__all__ = ["world"]  # el fixture, para pytest
pytestmark = pytest.mark.usefixtures("tenant_db")


def page(world: SimpleNamespace, **query: Any) -> tuple[list[Conversation], str | None]:
    paginator = RecencyPagination()
    request = Request(APIRequestFactory().get("/", query))
    with tenant_scope(ctx(world.a)):
        rows = paginator.paginate_queryset(Conversation.objects.all(), request, view)
        return rows, paginator.get_next_link()


view = SimpleNamespace(recency_field="last_message_at")


def test_the_pages_walk_from_newest_to_oldest_without_repeating_or_skipping_a_tie(
    world: SimpleNamespace,
) -> None:
    now = timezone.now()
    made = [fresh(world.a, world.account_a, f"CONV-{n:05d}") for n in range(1, 6)]
    stamps = [now, now - dt.timedelta(hours=1), now - dt.timedelta(hours=1), now, now]  # y empates
    with tenant_scope(ctx(world.a)):
        for convo, when in zip(made, stamps, strict=True):
            Conversation.objects.filter(pk=convo.pk).update(last_message_at=when)
    wanted = sorted(
        zip(stamps, [c.pk for c in made], strict=True),
        key=lambda row: (row[0], row[1]),
        reverse=True,
    )
    seen: list[Any] = []
    cursor: str | None = None
    while True:
        rows, cursor = page(world, limit=2, **({"cursor": cursor} if cursor else {}))
        seen += [c.pk for c in rows]
        if cursor is None:
            break
    assert seen == [pk for _, pk in wanted]  # el corte de página cae dentro de un empate
    assert page(world, limit=5)[1] is None  # una página exacta no deja cursor


def forged(when: str, ident: Any = None) -> str:
    token = json.dumps({"t": when, "i": str(uuid4()) if ident is None else ident}).encode()
    return base64.urlsafe_b64encode(token).decode().rstrip("=")


@pytest.mark.parametrize(
    "bad",
    [
        "x", "e30", "eyJ0IjoieCIsImkiOiJ5In0", "ñ",
        forged("2026-01-01T00:00:00"),  # sin zona horaria
        forged("hoy"),
        forged("2026-01-01T00:00:00+00:00", 5),  # el id no es un texto: 400, no AttributeError
        forged("2026-01-01T00:00:00+00:00", []),
    ],
)  # fmt: skip
def test_a_cursor_that_is_not_ours_is_a_validation_error_on_cursor(
    world: SimpleNamespace, bad: str
) -> None:
    with pytest.raises(ValidationError) as caught:
        page(world, cursor=bad)
    assert list(caught.value.detail) == ["cursor"]

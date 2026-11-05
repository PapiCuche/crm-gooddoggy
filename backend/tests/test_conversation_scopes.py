"""F6-01: los permisos del Inbox, las plantillas de rol y la política de alcance `Visible`
(propias, de su equipo y sin asignar), con PostgreSQL real y el rol `crm_app`."""

from dataclasses import replace
from itertools import count
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest
from django.utils import timezone

from apps.access.catalog import BY_CODE, INBOX_SCOPED, ROLE_TEMPLATES, Scope
from apps.access.selectors import can, execution_context, scoped
from apps.contacts.models import Contact
from apps.conversations.models import ChannelAccount, Conversation
from apps.conversations.scopes import Visible
from apps.organizations.models import Team
from core.tenancy.scope import tenant_scope
from tests import test_contacts_api
from tests.test_memberships import ctx

crm, real_stack = test_contacts_api.crm, test_contacts_api.real_stack
pytestmark = pytest.mark.usefixtures("tenant_db")
numbers = count(1)


@pytest.fixture
def inbox(crm: SimpleNamespace) -> SimpleNamespace:
    """Sobre `crm` (ana Owner; vera Vendedora y sam Supervisor, del equipo `ventas`; bea sin rol):
    una conversación de cada: de vera, de sam, de ana, del equipo (sin persona), de nadie, de
    ambos (equipo y vera) y una de B."""
    with tenant_scope(ctx(crm.a)):
        sales = Team.objects.get(slug="ventas")
        account = ChannelAccount.objects.create(
            platform="SANDBOX", name="Sandbox", external_account_id=f"a-{crm.a}"
        )
    with tenant_scope(ctx(crm.b)):
        other = ChannelAccount.objects.create(
            platform="SANDBOX", name="Sandbox", external_account_id=f"b-{crm.b}"
        )

    def make(org: UUID, channel: Any, name: str, **over: Any) -> UUID:
        with tenant_scope(ctx(org)):
            who = Contact.objects.create(display_name=name)
            made = Conversation.objects.create(
                number=f"CONV-{next(numbers):05d}",
                contact_id=who.pk,
                channel_account_id=channel.pk,
                last_message_at=timezone.now(),
                **over,
            )
            return made.pk  # type: ignore[no-any-return]

    ids = {
        "vera": make(crm.a, account, "vera", assigned_user_id=crm.vera.pk),
        "sam": make(crm.a, account, "sam", assigned_user_id=crm.sam.pk),
        "ana": make(crm.a, account, "ana", assigned_user_id=crm.ana.pk),
        "team": make(crm.a, account, "team", assigned_team_id=sales.pk),
        "nobody": make(crm.a, account, "nobody"),
        "both": make(
            crm.a, account, "both", assigned_team_id=sales.pk, assigned_user_id=crm.vera.pk
        ),
        "b": make(crm.b, other, "b"),
    }
    return SimpleNamespace(**vars(crm), convs=ids)


def visible(
    inbox: SimpleNamespace, user: Any, scope: Scope, code: str = "conversations.view"
) -> set[UUID]:
    """Lo que la lista deja ver con ese alcance, comprobado contra `can()` fila a fila."""
    with tenant_scope(ctx(inbox.a, user)) as tenant:
        narrow = replace(execution_context(tenant), permissions={code: frozenset({scope})})
        rows = list(Conversation.objects.all())
        listed = set(scoped(narrow, code, Conversation.objects.all()).values_list("pk", flat=True))
        allowed = {c.pk for c in rows if can(narrow, code, c)}
        assert listed == allowed, (user.email, scope)  # la lista y la comprobación por objeto
    return listed


def names(inbox: SimpleNamespace, found: set[UUID]) -> set[str]:
    return {key for key, pk in inbox.convs.items() if pk in found}


def test_the_catalog_and_the_templates_carry_the_inbox_permissions() -> None:
    for code in ("conversations.view", "messages.send", "messages.note"):
        assert BY_CODE[code].supports_scope and not BY_CODE[code].is_sensitive, code
    for code in ("conversations.assign", "channels.manage"):
        assert not BY_CODE[code].supports_scope and not BY_CODE[code].is_sensitive, code
    assert {BY_CODE[c].module for c in (*INBOX_SCOPED, "channels.manage")} == {"conversations"}
    held = {
        t.code: {k: v for k, v in t.grants.items() if k.startswith(("conversations.", "messages."))}
        | {k: v for k, v in t.grants.items() if k == "channels.manage"}
        for t in ROLE_TEMPLATES
    }
    organization = dict.fromkeys(INBOX_SCOPED, Scope.ORGANIZATION)
    assert held["owner"] == {**organization, "conversations.assign": None, "channels.manage": None}
    assert held["admin"] == held["owner"]
    assert held["supervisor"] == {  # reparte las de su equipo, sin configurar canales
        **dict.fromkeys(INBOX_SCOPED, Scope.TEAM),
        "conversations.assign": None,
    }
    assert held["seller"] == dict.fromkeys(INBOX_SCOPED, Scope.OWN)  # ni reparte ni configura


def test_each_scope_sees_its_own_the_teams_and_the_unassigned(inbox: SimpleNamespace) -> None:
    mine = {"vera": {"vera", "both", "nobody"}, "sam": {"sam", "nobody"}, "ana": {"ana", "nobody"}}
    for name, expected in mine.items():
        for scope in (Scope.OWN, Scope.BRANCH):  # sin sucursal en la conversación: lo propio
            found = visible(inbox, getattr(inbox, name), scope)
            assert names(inbox, found) == expected, (name, scope)
    # `TEAM`: además las del equipo `ventas`, de la que son vera y sam
    assert names(inbox, visible(inbox, inbox.sam, Scope.TEAM)) == {"sam", "nobody", "team", "both"}
    assert names(inbox, visible(inbox, inbox.vera, Scope.TEAM)) == {
        "vera", "nobody", "team", "both",
    }  # fmt: skip
    assert names(inbox, visible(inbox, inbox.ana, Scope.TEAM)) == {"ana", "nobody"}  # sin equipo
    everyone = set(inbox.convs) - {"b"}
    assert names(inbox, visible(inbox, inbox.ana, Scope.ORGANIZATION)) == everyone
    assert names(inbox, visible(inbox, inbox.bea, Scope.OWN)) == {"nobody"}  # sin nada propio
    for code in ("messages.send", "messages.note"):  # la misma política para los tres
        assert names(inbox, visible(inbox, inbox.vera, Scope.OWN, code)) == mine["vera"], code


def test_a_context_without_a_user_has_no_own_conversations_and_the_list_agrees_with_allows(
    inbox: SimpleNamespace,
) -> None:
    """`assigned_user_id = NULL` sería `IS NULL` y listaría las del equipo sin persona, que `allows`
    rechaza: sin usuario no hay «propias»."""
    with tenant_scope(ctx(inbox.a)):
        sales = Team.objects.get(slug="ventas").pk
        member: Any = SimpleNamespace(tenant=SimpleNamespace(user_id=None), team_ids={sales})
        rows = Conversation.objects.filter(pk__in=inbox.convs.values())
        for scope, expected in ((Scope.OWN, {"nobody"}), (Scope.TEAM, {"nobody", "team", "both"})):
            found = set(rows.filter(Visible().q(scope, member)).values_list("pk", flat=True))
            allowed = {c.pk for c in rows if Visible().allows(scope, member, c)}
            assert found == allowed and names(inbox, found) == expected, scope

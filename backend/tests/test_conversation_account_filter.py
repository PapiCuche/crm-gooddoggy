"""UX-INBOX-02d: las opciones del filtro por cuenta (`…/conversations/accounts/`) y `?account=` en
la lista y en «Sin responder». Middleware, sesión, motor de autorización y PostgreSQL con
`crm_app`: cada uno ve como opción solo las cuentas de las conversaciones que ya puede ver."""

from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from django.db.models import Q
from django.test import Client
from django.utils import timezone

from apps.contacts.models import Contact
from apps.conversations.api.conversations import AccountOptionsView
from apps.conversations.models import ChannelAccount, Conversation
from core.tenancy.scope import tenant_scope
from tests import test_conversations_api as api
from tests.test_authorization import give
from tests.test_member_branch import DENIED
from tests.test_memberships import ctx
from tests.test_self_context import NOT_FOUND, reply, signed
from tests.test_social_schema import account

crm, inbox, real_stack, networks = api.crm, api.inbox, api.real_stack, api.networks
pytestmark = pytest.mark.usefixtures("tenant_db")
BASE, ids, names, stamp = api.BASE, api.ids, api.names, api.stamp
OPTIONS, COUNT, OTHER = BASE + "accounts/", BASE + "unanswered/", "/api/v1/o/org-b/conversations/"
PHONE = "+51 999 000 111"


@pytest.fixture
def accounts(networks: SimpleNamespace) -> SimpleNamespace:
    """Sobre `networks`: ana, en su propio WhatsApp (solo ORG lo ve); A y B, una cuenta vacía."""
    a, b = networks.accounts[networks.a], networks.accounts[networks.b]
    own = account(networks.a, "WHATSAPP", "100000000000005", external_parent_id="100000000000004",
                  display_identifier=PHONE)  # fmt: skip
    idle = account(networks.a, "WHATSAPP", "100000000000006", external_parent_id="100000000000004")
    with tenant_scope(ctx(networks.a)):
        sandbox = Conversation.objects.get(pk=networks.convs["both"]).channel_account
        ChannelAccount.objects.filter(pk=own.pk).update(name="Ventas Lima")
    api.move(networks, "ana", own)
    found = {"SB": sandbox, "IG": a["INSTAGRAM"], "WA": a["WHATSAPP"], "MSG": a["MESSENGER"]}
    found |= {"OWN": own, "IDLE": idle, "IG_B": b["INSTAGRAM"], "MSG_B": b["MESSENGER"]}
    return SimpleNamespace(**vars(networks), by=found)


def options(client: Client, url: str = OPTIONS, **query: Any) -> list[dict[str, Any]]:
    """Todas las páginas, en orden (de opciones o, con `url`, de la lista)."""
    response, rows = client.get(url, query), []
    while True:
        assert response.status_code == 200, response.content
        rows += response.json()["results"]
        if response.json()["next"] is None:
            return rows
        response = client.get(url, {**query, "cursor": response.json()["next"]})


def keys(env: SimpleNamespace, rows: list[dict[str, Any]]) -> set[str]:
    back = {str(found.pk): key for key, found in env.by.items()}
    return {back[row["id"]] for row in rows}


def count(client: Client, url: str = COUNT, **query: Any) -> int:
    response = client.get(url, query)
    assert response.status_code == 200, response.content
    return response.json()["count"]  # type: ignore[no-any-return]


def test_each_scope_gets_only_the_accounts_of_the_conversations_it_sees(
    accounts: SimpleNamespace,
) -> None:
    ana, vera, sam = (signed(getattr(accounts, k)) for k in ("ana", "vera", "sam"))
    assert keys(accounts, options(ana)) == {"SB", "IG", "WA", "MSG", "OWN"}  # `ORGANIZATION`
    assert keys(accounts, options(vera)) == {"IG", "SB"}  # `OWN` y sin asignar
    assert keys(accounts, options(sam)) == {"WA", "MSG", "SB", "IG"}  # `TEAM`: no la de ana
    for client in (ana, vera, sam):  # lo mismo que deja ver la lista, ni una más
        rows = client.get(BASE, {"limit": 200}).json()["results"]
        assert {r["id"] for r in options(client)} == {r["channel"]["id"] for r in rows}
    assert reply(signed(accounts.bea).get(OPTIONS)) == DENIED  # sin `conversations.view`
    assert reply(Client().get(OPTIONS)) == api.UNAUTHENTICATED


def test_another_organization_never_shows_in_either_direction(accounts: SimpleNamespace) -> None:
    give(accounts.b, accounts.m_luis, {"conversations.view": "ORGANIZATION"})
    luis, ana = signed(accounts.luis), signed(accounts.ana)
    assert keys(accounts, options(luis, OTHER + "accounts/")) == {"IG_B"}  # sin el Messenger vacío
    assert reply(luis.get(OPTIONS)) == NOT_FOUND
    assert reply(ana.get(OTHER + "accounts/")) == NOT_FOUND
    for who, url, theirs in ((ana, BASE, "IG_B"), (luis, OTHER, "IG")):
        pk = accounts.by[theirs].pk
        assert ids(who, url, account=pk) == [] and count(who, url + "unanswered/", account=pk) == 0
    assert ids(luis, OTHER, account=accounts.by["IG_B"].pk) == [accounts.convs["b"]]


def test_an_option_has_exactly_id_platform_and_name(accounts: SimpleNamespace) -> None:
    response = signed(accounts.ana).get(OPTIONS)
    assert set(response.json()) == {"next", "results"}
    own = accounts.by["OWN"]
    mine = [row for row in response.json()["results"] if row["id"] == str(own.pk)]
    assert mine == [{"id": str(own.pk), "platform": "WHATSAPP", "name": "Ventas Lima"}]
    assert all(set(row) == {"id", "platform", "name"} for row in response.json()["results"])
    assert PHONE.encode() not in response.content and b"100000000000005" not in response.content


def test_deleted_accounts_stay_only_while_a_visible_conversation_uses_them(
    accounts: SimpleNamespace,
) -> None:
    with tenant_scope(ctx(accounts.a)):
        gone = ChannelAccount.objects.filter(pk__in=[accounts.by["IDLE"].pk, accounts.by["WA"].pk])
        assert gone.update(deleted_at=timezone.now()) == 2
    assert keys(accounts, options(signed(accounts.ana))) == {"SB", "IG", "WA", "MSG", "OWN"}
    assert keys(accounts, options(signed(accounts.vera))) == {"IG", "SB"}  # la de sam no


def test_a_conversation_hidden_by_the_view_does_not_bring_its_account(
    accounts: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    hide = {"conversations.view": Q(channel_account_id=accounts.by["WA"].pk)}
    monkeypatch.setattr(AccountOptionsView, "hidden_unless", hide, raising=False)
    assert keys(accounts, options(signed(accounts.ana))) == {"SB", "IG", "MSG", "OWN"}


def test_every_option_comes_once_across_pages(accounts: SimpleNamespace) -> None:
    now, org = timezone.now(), accounts.a
    with tenant_scope(ctx(org)):
        made = ChannelAccount.objects.bulk_create(
            ChannelAccount(
                organization_id=org,
                platform="SANDBOX",
                name=f"Extra {n}",
                external_account_id=f"x-{n}",
            )  # fmt: skip
            for n in range(201)
        )
        who = Contact.objects.create(display_name="muchas")
        Conversation.objects.bulk_create(
            Conversation(
                organization_id=org,
                number=f"CONV-{900 + n:05d}",
                contact_id=who.pk,
                channel_account_id=m.pk,
                last_message_at=now,
            )  # fmt: skip
            for n, m in enumerate(made)
        )
    ana = signed(accounts.ana)
    first = ana.get(OPTIONS, {"limit": 200}).json()
    assert len(first["results"]) == 200 and first["next"] is not None
    rows = options(ana, limit=200)
    wanted = {str(m.pk) for m in made}
    wanted |= {str(accounts.by[k].pk) for k in ("SB", "IG", "WA", "MSG", "OWN")}
    assert len(rows) == len(wanted) == 206 and {r["id"] for r in rows} == wanted
    assert [r["id"] for r in options(ana, limit=2)] == [r["id"] for r in rows]


def test_the_account_filter_cuts_what_the_scope_left_and_combines(
    accounts: SimpleNamespace,
) -> None:
    for key in ("vera", "nobody", "sam"):
        stamp(accounts.a, accounts.convs[key], last_message_preview="Hola",
              last_message_direction="INBOUND")  # fmt: skip
    ana, vera, sam = (signed(getattr(accounts, k)) for k in ("ana", "vera", "sam"))
    ig, wa, own = (accounts.by[k].pk for k in ("IG", "WA", "OWN"))
    assert names(accounts, ids(ana, account=ig)) == {"vera", "nobody"}
    assert names(accounts, ids(ana, account=own)) == {"ana"}
    assert names(accounts, ids(vera, account=ig)) == {"vera", "nobody"}  # `OWN`
    assert names(accounts, ids(sam, account=ig)) == {"nobody"}  # `TEAM`
    for who, pk in ((vera, wa), (sam, own), (ana, uuid4()), (ana, accounts.by["IDLE"].pk)):
        assert ids(who, account=pk) == [] and count(who, account=pk) == 0  # como una que no existe
    assert names(accounts, ids(ana, account=ig, box="unassigned")) == {"nobody"}
    assert names(accounts, ids(vera, account=ig, box="mine")) == {"vera"}
    assert ids(ana, account=ig, platform="WHATSAPP") == []
    assert names(accounts, ids(ana, account=ig, platform="INSTAGRAM")) == {"vera", "nobody"}
    stamp(accounts.a, accounts.convs["nobody"], status="OPEN")
    assert names(accounts, ids(ana, account=ig, status="OPEN")) == {"nobody"}
    assert names(accounts, ids(ana, account=ig, unanswered="true")) == {"vera", "nobody"}
    assert count(ana, account=ig) == 2 and count(ana, account=wa) == 1 and count(ana) == 3
    assert count(ana, account=ig, box="unassigned") == 1 and count(vera, account=ig) == 2
    assert len(ids(ana, account="")) == 6 and count(ana, account="") == 3  # vacío: sin filtro


def test_a_bad_or_repeated_account_is_a_400_like_the_other_filters(
    accounts: SimpleNamespace,
) -> None:
    ana, pk = signed(accounts.ana), str(accounts.by["IG"].pk)
    why = {"code": "VALIDATION_ERROR", "fields": {"account": [
        {"code": "invalid", "message": "No válido."}]}}  # fmt: skip
    once = {"code": "VALIDATION_ERROR", "fields": {"account": [
        {"code": "invalid", "message": "Una sola vez."}]}}  # fmt: skip
    for url in (BASE, COUNT):
        for bad in ("x", "123", pk + "0", "INSTAGRAM", f"{pk}'--"):
            response = ana.get(url, {"account": bad})
            assert (response.status_code, response.json()) == (400, why), (url, bad)
        twice = ana.get(f"{url}?account={pk}&account={pk}")
        assert (twice.status_code, twice.json()) == (400, once), url


def test_the_next_page_keeps_the_account_without_repeating_or_skipping(
    accounts: SimpleNamespace,
) -> None:
    tied = timezone.now()  # en el empate, sam (de otra cuenta) queda entre las dos por su `id`
    for key in ("vera", "nobody", "sam"):
        stamp(accounts.a, accounts.convs[key], last_message_at=tied)
    ana, ig = signed(accounts.ana), accounts.by["IG"].pk
    seen = [UUID(r["id"]) for r in options(ana, BASE, account=ig, limit=1)]
    assert seen == sorted([accounts.convs["vera"], accounts.convs["nobody"]], reverse=True)

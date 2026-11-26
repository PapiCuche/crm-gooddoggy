"""F6-02a: `…/conversations/` (lista y detalle). Middleware, sesión, motor de autorización y
PostgreSQL con el rol `crm_app`."""

import datetime as dt
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.accounts.models import User
from apps.contacts.models import Contact
from apps.conversations.models import Conversation
from core.tenancy.scope import tenant_scope
from tests import test_conversation_scopes as scopes
from tests.factories import TEST_PASSWORD
from tests.test_authorization import give
from tests.test_conversation_commands import send
from tests.test_member_branch import DENIED
from tests.test_memberships import ctx
from tests.test_self_context import NOT_FOUND, reply, signed
from tests.test_social_schema import account, identity

crm, inbox, real_stack = scopes.crm, scopes.inbox, scopes.real_stack
pytestmark = pytest.mark.usefixtures("tenant_db")
BASE = "/api/v1/o/org-a/conversations/"
UNAUTHENTICATED = (401, b'{"code":"NOT_AUTHENTICATED"}')
ITEM = {
    "id", "number", "status", "attention_mode", "assignee_kind", "assigned_user_id",
    "assigned_team_id", "contact", "channel", "last_message_at", "last_inbound_at",
    "last_message_preview", "last_message_direction", "standby_since", "human_request", "version",
    "ai_excluded_at", "last_reply_failed",
}  # fmt: skip
DETAIL = ITEM | {
    "assigned_at",
    "assignment_version",
    "resolved_at",
    "closed_at",
    "created_at",
    "updated_at",
}


def ids(client: Client, url: str = BASE, **query: Any) -> list[UUID]:
    response = client.get(url, query)
    assert response.status_code == 200, response.content
    return [UUID(row["id"]) for row in response.json()["results"]]


def names(inbox: SimpleNamespace, found: list[UUID]) -> set[str]:
    return {key for key, pk in inbox.convs.items() if pk in found}


def stamp(org: UUID, pk: UUID, **fields: Any) -> None:
    with tenant_scope(ctx(org)):
        Conversation.objects.filter(pk=pk).update(**fields)


def test_each_role_lists_what_its_scope_reaches(inbox: SimpleNamespace) -> None:
    assert names(inbox, ids(signed(inbox.ana))) == {"vera", "sam", "ana", "team", "nobody", "both"}
    assert names(inbox, ids(signed(inbox.vera))) == {
        "vera",
        "both",
        "nobody",
    }  # `OWN` y sin asignar
    assert names(inbox, ids(signed(inbox.sam))) == {"sam", "team", "both", "nobody"}  # `TEAM`
    assert reply(signed(inbox.bea).get(BASE)) == DENIED  # sin rol
    assert reply(Client().get(BASE)) == UNAUTHENTICATED
    assert reply(signed(inbox.luis).get(BASE)) == NOT_FOUND  # de B, no de A
    give(inbox.b, inbox.m_luis, {"conversations.view": "ORGANIZATION"})
    other = BASE.replace("org-a", "org-b")
    assert ids(signed(inbox.luis), other) == [inbox.convs["b"]]  # y solo la suya
    assert reply(signed(inbox.ana).get(other)) == NOT_FOUND
    staff = User.objects.create_superuser("staff@example.com", TEST_PASSWORD)
    assert reply(signed(staff).get(BASE)) == NOT_FOUND  # ser del personal de la plataforma no abre


def test_a_row_has_the_documented_fields_and_the_contact_name_follows_the_contact_scope(
    inbox: SimpleNamespace,
) -> None:
    rows = {r["id"]: r for r in signed(inbox.ana).get(BASE).json()["results"]}
    row = rows[str(inbox.convs["vera"])]
    assert set(row) == ITEM and set(row["contact"]) == {"id", "display_name"}
    assert row["contact"]["display_name"] == "vera" and row["channel"]["platform"] == "SANDBOX"
    assert (row["status"], row["attention_mode"], row["assignee_kind"]) == (
        "PENDING", "HUMAN", "USER",
    )  # fmt: skip
    assert row["assigned_user_id"] == str(inbox.vera.pk) and row["last_message_preview"] is None
    seller = {r["contact"]["display_name"] for r in signed(inbox.vera).get(BASE).json()["results"]}
    assert seller == {None}  # vera no ve a esos clientes: ni su nombre


def test_the_list_orders_by_the_latest_message_and_pages_with_a_two_value_cursor(
    inbox: SimpleNamespace,
) -> None:
    now = timezone.now()
    order = ["ana", "sam", "vera", "team", "nobody", "both"]  # del más reciente al más antiguo
    for index, key in enumerate(order):
        stamp(inbox.a, inbox.convs[key], last_message_at=now - dt.timedelta(minutes=index))
    tied = now - dt.timedelta(minutes=30)  # empate: el desempate es el `id`, de mayor a menor
    stamp(inbox.a, inbox.convs["both"], last_message_at=tied)
    stamp(inbox.a, inbox.convs["nobody"], last_message_at=tied)
    ana = signed(inbox.ana)
    wanted = [inbox.convs[k] for k in order[:4]] + sorted(
        [inbox.convs["both"], inbox.convs["nobody"]], reverse=True
    )
    assert ids(ana) == wanted
    seen: list[UUID] = []  # el corte de página cae dentro del empate
    body = ana.get(BASE, {"limit": 5}).json()
    while True:
        seen += [UUID(r["id"]) for r in body["results"]]
        if body["next"] is None:
            break
        body = ana.get(BASE, {"limit": 5, "cursor": body["next"]}).json()
    assert seen == wanted  # sin repetir ni saltarse una, tampoco en el empate
    for bad in (
        "x",
        "e30",
        "eyJ0IjoieCIsImkiOiJ5In0",
        "eyJ0IjoiMjAyNi0wMS0wMVQwMDowMDowMCIsImkiOiJ4In0",
    ):
        response = ana.get(BASE, {"cursor": bad})
        assert response.status_code == 400 and set(response.json()["fields"]) == {"cursor"}, bad
    for bad in ("0", "201", "x"):
        assert ana.get(BASE, {"limit": bad}).status_code == 400


def test_the_filters_cut_what_the_scope_left(inbox: SimpleNamespace) -> None:
    ana, sam = signed(inbox.ana), signed(inbox.sam)
    stamp(inbox.a, inbox.convs["ana"], status="OPEN")
    assert names(inbox, ids(ana, box="mine")) == {"ana"}
    assert names(inbox, ids(sam, box="mine")) == {"sam"}
    assert names(inbox, ids(ana, box="unassigned")) == {"nobody"}  # la del equipo ya está asignada
    assert names(inbox, ids(ana, status="OPEN")) == {"ana"}
    assert names(inbox, ids(ana, box="mine", status="PENDING")) == set()
    assert names(inbox, ids(sam, box="unassigned")) == {"nobody"}  # el alcance primero
    assert len(ids(ana, status="")) == 6 and len(ids(ana, box="")) == 6  # vacío: sin filtro
    for query in ({"box": "all", "status": "NOPE"}, {"box": "todos"}, {"status": "open"}):
        response = ana.get(BASE, query)
        assert response.status_code == 400 and response.json()["code"] == "VALIDATION_ERROR"
    twice = ana.get(BASE + "?box=mine&box=all")
    assert twice.status_code == 400 and set(twice.json()["fields"]) == {"box"}


@pytest.fixture
def networks(inbox: SimpleNamespace) -> SimpleNamespace:
    """UX-INBOX-02: en A, vera y nobody por Instagram, sam por WhatsApp, team por Messenger; ana y
    both siguen en Sandbox. La de B, por un Instagram de B."""
    made: dict[UUID, dict[str, Any]] = {}
    for n, org in ((1, inbox.a), (2, inbox.b)):
        page = f"{n}00000000000001"  # su página de Facebook, y la de su Instagram
        made[org] = {
            "MESSENGER": account(org, "MESSENGER", page),
            "INSTAGRAM": account(org, "INSTAGRAM", f"{n}00000000000002", external_parent_id=page),
        }
    made[inbox.a]["WHATSAPP"] = account(inbox.a, "WHATSAPP", "100000000000003",
                                        external_parent_id="100000000000004")  # fmt: skip
    for key, platform in (("vera", "INSTAGRAM"), ("nobody", "INSTAGRAM"), ("sam", "WHATSAPP"),
                          ("team", "MESSENGER"), ("b", "INSTAGRAM")):  # fmt: skip
        move(inbox, key, made[inbox.b if key == "b" else inbox.a][platform])
    return SimpleNamespace(**vars(inbox), accounts=made)


def move(inbox: SimpleNamespace, key: str, where: Any) -> None:
    """Pasa la conversación a la cuenta `where`, con su identidad si es social (`0040`)."""
    org = inbox.b if key == "b" else inbox.a
    with tenant_scope(ctx(org)):
        row = Conversation.objects.get(pk=inbox.convs[key])
    over = {}
    if where.platform in ("INSTAGRAM", "MESSENGER"):
        who = identity(org, SimpleNamespace(pk=row.contact_id), where, f"9{uuid4().int % 10**15}")
        over["contact_identity_id"] = who.pk
    stamp(org, row.pk, channel_account_id=where.pk, **over)


def test_the_platform_filter_cuts_what_the_scope_left_and_combines(
    networks: SimpleNamespace,
) -> None:
    ana, vera, sam = (signed(getattr(networks, k)) for k in ("ana", "vera", "sam"))
    by = {"INSTAGRAM": {"vera", "nobody"}, "WHATSAPP": {"sam"}, "MESSENGER": {"team"},
          "SANDBOX": {"ana", "both"}}  # fmt: skip
    for platform, wanted in by.items():
        assert names(networks, ids(ana, platform=platform)) == wanted, platform
        rows = ana.get(BASE, {"platform": platform}).json()["results"]
        assert {r["channel"]["platform"] for r in rows} == {platform}
    assert names(networks, ids(vera, platform="INSTAGRAM")) == {"vera", "nobody"}  # `OWN`
    assert names(networks, ids(vera, platform="WHATSAPP")) == set()  # la de sam no es suya
    assert names(networks, ids(sam, platform="INSTAGRAM")) == {"nobody"}  # `TEAM`
    assert names(networks, ids(sam, platform="MESSENGER")) == {"team"}
    assert names(networks, ids(vera, platform="INSTAGRAM", box="mine")) == {"vera"}
    assert names(networks, ids(ana, platform="INSTAGRAM", box="unassigned")) == {"nobody"}
    stamp(networks.a, networks.convs["nobody"], status="OPEN")
    assert names(networks, ids(ana, platform="INSTAGRAM", status="OPEN")) == {"nobody"}
    assert names(networks, ids(ana, platform="WHATSAPP", status="OPEN")) == set()
    assert len(ids(ana, platform="")) == 6  # vacío: sin filtro


def test_a_bad_or_repeated_platform_is_a_400_like_the_other_filters(
    networks: SimpleNamespace,
) -> None:
    ana = signed(networks.ana)
    for bad in ("instagram", "TIKTOK", "UNKNOWN", "INSTAGRAM ", "x"):
        response = ana.get(BASE, {"platform": bad})
        assert response.status_code == 400, bad
        why = [{"code": "invalid_choice", "message": "No válido."}]
        assert response.json() == {"code": "VALIDATION_ERROR", "fields": {"platform": why}}
        assert ana.get(BASE, {"status": bad}).json()["fields"] == {"status": why}  # igual
        assert ana.get(BASE + "unanswered/", {"platform": bad}).status_code == 400, bad
    for url in (BASE, BASE + "unanswered/"):
        twice = ana.get(url + "?platform=INSTAGRAM&platform=WHATSAPP")
        once = [{"code": "invalid", "message": "Una sola vez."}]
        assert twice.status_code == 400 and twice.json()["fields"] == {"platform": once}


def test_the_platform_filter_never_reaches_another_organization(
    networks: SimpleNamespace,
) -> None:
    for key in ("vera", "nobody", "b"):
        stamp(networks.b if key == "b" else networks.a, networks.convs[key],
              last_message_preview="Hola", last_message_direction="INBOUND")  # fmt: skip
    ana = signed(networks.ana)
    assert networks.convs["b"] not in ids(ana, platform="INSTAGRAM")
    assert ana.get(BASE + "unanswered/", {"platform": "INSTAGRAM"}).json() == {"count": 2}
    give(networks.b, networks.m_luis, {"conversations.view": "ORGANIZATION"})
    luis, other = signed(networks.luis), BASE.replace("org-a", "org-b")
    assert ids(luis, other, platform="INSTAGRAM") == [networks.convs["b"]]
    assert ids(luis, other, platform="WHATSAPP") == []
    assert luis.get(other + "unanswered/", {"platform": "INSTAGRAM"}).json() == {"count": 1}
    assert reply(luis.get(BASE, {"platform": "INSTAGRAM"})) == NOT_FOUND  # la ruta de A
    for who, url in ((luis, BASE), (ana, other)):  # UX-INBOX-02b: y su número, desde la otra
        assert reply(who.get(url + "unanswered/", {"platform": "INSTAGRAM"})) == NOT_FOUND


def test_the_unanswered_count_follows_the_platform_and_the_box(networks: SimpleNamespace) -> None:
    for key in ("vera", "nobody", "sam", "team"):
        stamp(networks.a, networks.convs[key], last_message_preview="Hola",
              last_message_direction="INBOUND")  # fmt: skip
    ana, vera = signed(networks.ana), signed(networks.vera)

    def count(client: Client, **query: Any) -> int:
        return client.get(BASE + "unanswered/", query).json()["count"]  # type: ignore[no-any-return]

    assert count(ana) == 4 and count(ana, platform="") == 4
    assert [count(ana, platform=p) for p in ("INSTAGRAM", "WHATSAPP", "MESSENGER", "SANDBOX")] == [
        2, 1, 1, 0,
    ]  # fmt: skip
    assert count(ana, platform="INSTAGRAM", box="unassigned") == 1
    assert count(vera, platform="INSTAGRAM") == 2 and count(vera, platform="WHATSAPP") == 0
    assert count(vera, platform="INSTAGRAM", box="mine") == 1
    for platform in ("INSTAGRAM", "WHATSAPP"):  # la lista y su número dicen lo mismo
        assert len(ids(ana, platform=platform, unanswered="true")) == count(ana, platform=platform)


def test_the_next_page_keeps_the_platform_without_repeating_or_skipping(
    networks: SimpleNamespace,
) -> None:
    now, instagram = timezone.now(), networks.accounts[networks.a]["INSTAGRAM"]
    for n in range(5):  # más de Instagram, entre las demás y con un empate
        with tenant_scope(ctx(networks.a)):
            who = Contact.objects.create(display_name=f"ig{n}")
            made = Conversation.objects.create(
                number=f"CONV-{800 + n:05d}", contact_id=who.pk, channel_account_id=instagram.pk,
                last_message_at=now, contact_identity_id=identity(
                    networks.a, who, instagram, f"8{n:014d}").pk,
            )  # fmt: skip
        networks.convs[f"ig{n}"] = made.pk
    order = ["ig0", "sam", "vera", "ig1", "team", "ig2", "ig3", "nobody", "ig4", "ana", "both"]
    for index, key in enumerate(order):
        stamp(networks.a, networks.convs[key], last_message_at=now - dt.timedelta(minutes=index))
    tied = now - dt.timedelta(minutes=30)
    for key in ("ig3", "nobody"):
        stamp(networks.a, networks.convs[key], last_message_at=tied)
    ana, query = signed(networks.ana), {"platform": "INSTAGRAM", "limit": "2"}
    wanted = [networks.convs[k] for k in ("ig0", "vera", "ig1", "ig2", "ig4")] + sorted(
        [networks.convs["ig3"], networks.convs["nobody"]], reverse=True
    )
    assert ids(ana, platform="INSTAGRAM") == wanted
    seen: list[UUID] = []
    body = ana.get(BASE, query).json()
    while True:
        seen += [UUID(r["id"]) for r in body["results"]]
        if body["next"] is None:
            break
        body = ana.get(BASE, {**query, "cursor": body["next"]}).json()
    assert seen == wanted  # en orden, sin repetir ni saltarse una, tampoco en el empate
    after = ana.get(BASE, query).json()["next"]  # el cursor es una posición, no un filtro
    assert ids(ana, platform="WHATSAPP", cursor=after) == []  # sam es más reciente que vera
    assert names(networks, ids(ana, platform="MESSENGER", cursor=after)) == {"team"}


def test_the_list_costs_the_same_queries_however_many_rows(inbox: SimpleNamespace) -> None:
    ana = signed(inbox.ana)

    def queries() -> int:
        with CaptureQueriesContext(connection) as captured:
            assert ana.get(BASE).status_code == 200
        return len(captured)

    one = queries()
    for index in range(12):
        with tenant_scope(ctx(inbox.a)):
            who = Contact.objects.create(display_name=f"c{index}")
            first = Conversation.objects.get(pk=inbox.convs["vera"])
            Conversation.objects.create(
                number=f"CONV-{900 + index:05d}", contact_id=who.pk,
                channel_account_id=first.channel_account_id, last_message_at=timezone.now(),
            )  # fmt: skip
    assert queries() == one


def test_the_detail_is_a_404_outside_the_scope_exactly_like_a_missing_one(
    inbox: SimpleNamespace,
) -> None:
    vera, sam = signed(inbox.vera), signed(inbox.sam)
    missing = reply(vera.get(f"{BASE}{uuid4()}/"))
    assert missing == NOT_FOUND
    for key in ("sam", "ana", "team", "b"):  # de otro, de su equipo (a ella no), de B
        assert reply(vera.get(f"{BASE}{inbox.convs[key]}/")) == missing, key
    body = vera.get(f"{BASE}{inbox.convs['vera']}/").json()
    assert set(body) == DETAIL and body["contact"]["id"]
    assert sam.get(f"{BASE}{inbox.convs['team']}/").status_code == 200  # `TEAM`
    assert reply(sam.get(f"{BASE}{inbox.convs['ana']}/")) == missing
    assert reply(signed(inbox.bea).get(f"{BASE}{inbox.convs['vera']}/")) == DENIED
    assert reply(Client().get(f"{BASE}{inbox.convs['vera']}/")) == UNAUTHENTICATED
    assert reply(signed(inbox.luis).get(f"{BASE}{inbox.convs['b']}/")) == NOT_FOUND  # ruta de A
    assert vera.head(f"{BASE}{inbox.convs['vera']}/").status_code == 200


def test_the_list_and_the_detail_agree_on_what_is_visible(inbox: SimpleNamespace) -> None:
    for user in (inbox.ana, inbox.vera, inbox.sam):
        client, listed = signed(user), set(ids(signed(user)))
        for key, pk in inbox.convs.items():
            found = client.get(f"{BASE}{pk}/").status_code == 200
            assert found == (pk in listed), (user.email, key)


def test_the_preview_is_the_text_of_the_last_message_and_never_a_note(
    inbox: SimpleNamespace,
) -> None:
    from apps.conversations import commands

    ana, convo = signed(inbox.ana), inbox.convs["vera"]
    with tenant_scope(ctx(inbox.a, inbox.ana)) as tenant:
        row = Conversation.objects.get(pk=convo)
        commands.record_inbound(
            tenant, contact_id=row.contact_id, channel_account_id=row.channel_account_id,
            external_message_id="wamid.1", body="Hola\n\n  quiero   el iPhone " + "x" * 200,
        )  # fmt: skip
        commands.add_note(tenant, conversation_id=convo, body="Nota privada con el costo 700")
    item = ana.get(f"{BASE}{convo}/").json()
    assert item["last_message_direction"] == "INBOUND"
    assert item["last_message_preview"].startswith("Hola quiero el iPhone xxx")
    assert len(item["last_message_preview"]) == 80 and "\n" not in item["last_message_preview"]
    assert "Nota privada" not in str(ana.get(BASE).json())
    with tenant_scope(ctx(inbox.a, inbox.ana)) as tenant:
        send(tenant, conversation_id=convo, client_message_id=uuid4(), body="Con gusto")
    again = ana.get(f"{BASE}{convo}/").json()
    assert (again["last_message_preview"], again["last_message_direction"]) == (
        "Con gusto", "OUTBOUND",
    )  # fmt: skip
    # el comienzo del mensaje identifica al cliente como su nombre: misma regla en lista y detalle
    vera = signed(inbox.vera)
    listed = {r["id"]: r for r in vera.get(BASE).json()["results"]}[str(convo)]
    for row in (listed, vera.get(f"{BASE}{convo}/").json()):
        assert row["contact"]["display_name"] is None and row["last_message_preview"] is None
        assert row["last_message_direction"] == "OUTBOUND"  # la dirección no identifica a nadie
    give(inbox.a, inbox.m["vera"], {"contacts.view": "ORGANIZATION"})
    listed = {r["id"]: r for r in vera.get(BASE).json()["results"]}[str(convo)]
    for row in (listed, vera.get(f"{BASE}{convo}/").json()):
        assert (
            row["contact"]["display_name"] == "vera" and row["last_message_preview"] == "Con gusto"
        )


def shown(client: Client, convo: UUID) -> list[tuple[Any, Any]]:
    """(nombre, vista previa) del contacto de esa conversación, en la lista y en el detalle."""
    row = {r["id"]: r for r in client.get(BASE).json()["results"]}[str(convo)]
    return [
        (r["contact"]["display_name"], r["last_message_preview"])
        for r in (row, client.get(f"{BASE}{convo}/").json())
    ]


def test_a_team_scoped_contacts_view_names_only_the_contacts_the_scope_reaches(
    inbox: SimpleNamespace,
) -> None:
    mine, other = inbox.convs["sam"], inbox.convs["team"]  # sam las ve las dos
    for key in ("sam", "team"):
        stamp(inbox.a, inbox.convs[key], last_message_preview=f"Hola {key}",
              last_message_direction="INBOUND")  # fmt: skip
    with tenant_scope(ctx(inbox.a)):
        who = Conversation.objects.get(pk=mine).contact_id
        Contact.objects.filter(pk=who).update(owner_user_id=inbox.sam.pk)  # `TEAM` de contactos
    sam = signed(inbox.sam)
    assert shown(sam, mine) == [("sam", "Hola sam")] * 2  # un contacto que su alcance alcanza
    assert shown(sam, other) == [(None, None)] * 2  # otro que no: ni nombre ni vista previa


def test_a_deleted_contact_has_no_name_and_no_preview_even_for_the_owner(
    inbox: SimpleNamespace,
) -> None:
    convo = inbox.convs["vera"]
    stamp(inbox.a, convo, last_message_preview="Hola", last_message_direction="INBOUND")
    ana = signed(inbox.ana)
    assert shown(ana, convo) == [("vera", "Hola")] * 2
    with tenant_scope(ctx(inbox.a)):
        who = Conversation.objects.get(pk=convo).contact_id
        Contact.objects.filter(pk=who).update(deleted_at=timezone.now())
    assert shown(ana, convo) == [(None, None)] * 2

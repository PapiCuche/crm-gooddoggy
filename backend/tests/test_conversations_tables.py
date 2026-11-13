"""F6-01: las tablas `channel_accounts`, `conversations`, `messages` (F6-01b) y
`conversation_assignments` (Inbox), con PostgreSQL real y el rol `crm_app`: RLS, claves compuestas
de tenant, CHECKs, únicos parciales, concesiones mínimas y el estado de entrega que solo avanza."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from django.db import DatabaseError, IntegrityError, connection, connections, transaction
from django.utils import timezone

from apps.contacts.models import Contact
from apps.conversations.models import (
    ChannelAccount,
    Conversation,
    ConversationAssignment,
    Message,
)
from core.sequences import allocate
from core.tenancy.scope import tenant_scope
from tests.factories import make_user
from tests.test_memberships import ctx, join
from tests.test_teams import team

pytestmark = pytest.mark.usefixtures("tenant_db")
TABLES = ("channel_accounts", "conversations", "messages", "conversation_assignments")


@pytest.fixture
def world(orgs: dict[str, Any]) -> SimpleNamespace:
    """`ana` es miembro de A y `luis` de B; cada organización con un contacto, una cuenta Sandbox
    y un equipo."""
    ana, luis = make_user(), make_user()
    join(orgs["A"], ana)
    join(orgs["B"], luis)
    made = SimpleNamespace(a=orgs["A"], b=orgs["B"], ana=ana, luis=luis)
    for key, org in (("a", orgs["A"]), ("b", orgs["B"])):
        with tenant_scope(ctx(org)):
            setattr(made, f"contact_{key}", Contact.objects.create(display_name=f"Cliente {key}"))
            account = ChannelAccount.objects.create(
                platform="SANDBOX", name="Sandbox", external_account_id=f"fixture-{org}"
            )
            setattr(made, f"account_{key}", account)
        setattr(made, f"team_{key}", team(org, f"ventas-{key}"))
    return made


def pending_account(org: UUID, number: str = "15550001111") -> Any:
    """Una cuenta de WhatsApp que nace `PENDING`: la base no deja volver a `PENDING` (F7-02a)."""
    with tenant_scope(ctx(org)):
        return ChannelAccount.objects.create(
            platform="WHATSAPP", name="Pendiente", status="PENDING",
            external_account_id=number, external_parent_id="1234567890",
        )  # fmt: skip


def conversation(
    org: UUID, contact: Any, account: Any, number: str = "CONV-00001", **over: Any
) -> Any:
    with tenant_scope(ctx(org)):
        return Conversation.objects.create(
            number=number,
            contact_id=contact.pk,
            channel_account_id=account.pk,
            last_message_at=timezone.now(),
            **over,
        )


def fresh(org: UUID, account: Any, number: str, **over: Any) -> Any:
    """Una conversación de un contacto nuevo (la anterior del contacto no estorba al único)."""
    with tenant_scope(ctx(org)):
        who = Contact.objects.create(display_name=number)
    return conversation(org, who, account, number, **over)


def message(org: UUID, convo: Any, **over: Any) -> Any:
    fields = {
        "direction": "INBOUND",
        "sender_type": "CONTACT",
        "body": "Hola",
        "conversation_id": convo.pk,
        "channel_account_id": convo.channel_account_id,
    } | over
    with tenant_scope(ctx(org)):
        return Message.objects.create(**fields)


def first(world: SimpleNamespace) -> Any:
    return conversation(world.a, world.contact_a, world.account_a)


def refused(org: UUID, constraint: str, make_row: Any) -> None:
    with tenant_scope(ctx(org)), pytest.raises(IntegrityError, match=constraint):
        make_row()


def sql(org: UUID, statement: str, params: list[Any] | None = None) -> list[Any]:
    with tenant_scope(ctx(org)), connection.cursor() as cursor:
        cursor.execute(statement, params)
        return cursor.fetchall() if cursor.description else []


def test_what_a_creates_never_exists_for_b(world: SimpleNamespace) -> None:
    convo = first(world)
    message(world.a, convo)
    with tenant_scope(ctx(world.a)):
        ConversationAssignment.objects.create(
            conversation_id=convo.pk, new_user_id=world.ana.pk, reason_code="TAKE",
            assigned_by_user_id=world.ana.pk,
        )  # fmt: skip
    for table in TABLES:  # RLS, no solo el manager
        own = 1 if table == "channel_accounts" else 0  # B tiene su propia cuenta Sandbox
        assert len(sql(world.a, f"SELECT 1 FROM {table}")) == 1  # noqa: S608
        assert len(sql(world.b, f"SELECT 1 FROM {table}")) == own  # noqa: S608
        found = sql(world.b, f"SELECT 1 FROM {table} WHERE organization_id = %s", [world.a])  # noqa: S608
        assert found == []
    with tenant_scope(ctx(world.b)):
        assert not Conversation.objects.exists() and not Message.objects.exists()


def test_every_table_forces_rls_with_a_single_policy(migrator: psycopg.Connection[Any]) -> None:
    for table in TABLES:
        flags = migrator.execute(
            "SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = %s", [table]
        ).fetchone()
        policies = migrator.execute(
            "SELECT count(*) FROM pg_policies WHERE tablename = %s", [table]
        )
        assert flags == (True, True) and policies.fetchone() == (1,), table


def test_a_row_cannot_be_written_for_another_organization(world: SimpleNamespace) -> None:
    with pytest.raises(DatabaseError):
        sql(
            world.b,
            "INSERT INTO channel_accounts (id, organization_id, platform, name,"
            " external_account_id, status, created_at, updated_at, version)"
            " VALUES (gen_random_uuid(), %s, 'SANDBOX', 'x', 'x', 'CONNECTED', now(), now(), 1)",
            [world.a],
        )


def test_the_references_are_composite_with_the_tenant(world: SimpleNamespace) -> None:
    mine, theirs = first(world), conversation(world.b, world.contact_b, world.account_b)
    stranger = make_user()
    cases = {
        "conversations_contact_fk": lambda: conversation(
            world.a, world.contact_b, world.account_a, "CONV-00002"
        ),
        "conversations_channel_account_fk": lambda: conversation(
            world.a, world.contact_a, world.account_b, "CONV-00003"
        ),
        "conversations_team_fk": lambda: fresh(
            world.a, world.account_a, "CONV-00004", assigned_team_id=world.team_b.pk
        ),
        "conversations_user_fk": lambda: fresh(  # no es miembro de A
            world.a, world.account_a, "CONV-00005", assigned_user_id=world.luis.pk
        ),
        "messages_conversation_fk": lambda: message(world.a, theirs),
        "messages_sender_user_fk": lambda: message(
            world.a,
            mine,
            direction="OUTBOUND",
            sender_type="USER",
            sender_user_id=stranger.pk,
            delivery_status="QUEUED",
        ),
        "conv_assignments_conversation_fk": lambda: ConversationAssignment.objects.create(
            conversation_id=theirs.pk, reason_code="MANUAL", assigned_by_system=True
        ),
        "conv_assignments_new_team_fk": lambda: ConversationAssignment.objects.create(
            conversation_id=mine.pk,
            new_team_id=world.team_b.pk,
            reason_code="MANUAL",
            assigned_by_system=True,
        ),  # fmt: skip
        **{
            name: lambda kw=kw: ConversationAssignment.objects.create(
                conversation_id=mine.pk, reason_code="MANUAL", assigned_by_system=True, **kw
            )
            for name, kw in {
                "conv_assignments_previous_team_fk": {"previous_team_id": world.team_b.pk},
                "conv_assignments_previous_user_fk": {"previous_user_id": world.luis.pk},
                "conv_assignments_new_user_fk": {"new_user_id": world.luis.pk},
            }.items()
        },
        "channel_accounts_created_by_fk": lambda: ChannelAccount.objects.create(
            platform="SANDBOX", name="x", external_account_id="x", created_by_user_id=world.luis.pk
        ),
        "channel_accounts_deleted_by_fk": lambda: ChannelAccount.objects.create(
            platform="SANDBOX",
            name="x",
            external_account_id="y",
            deleted_at=timezone.now(),
            deleted_by_user_id=world.luis.pk,
        ),
        "conv_assignments_assigned_by_user_fk": lambda: ConversationAssignment.objects.create(
            conversation_id=mine.pk, reason_code="MANUAL", assigned_by_user_id=world.luis.pk
        ),
    }
    for constraint, make_row in cases.items():
        refused(world.a, constraint, make_row)


def test_the_assignee_kind_is_derived_and_cannot_be_written(world: SimpleNamespace) -> None:
    team, user = world.team_a.pk, world.ana.pk  # el equipo y la persona a la vez: USER (ADR-007)
    wanted = {"CONV-00010": ({}, "UNASSIGNED"), "CONV-00011": ({"assigned_team_id": team}, "TEAM")}
    wanted["CONV-00012"] = ({"assigned_user_id": user}, "USER")
    wanted["CONV-00013"] = ({"assigned_team_id": team, "assigned_user_id": user}, "USER")
    for number, (over, _) in wanted.items():
        fresh(world.a, world.account_a, number, **over)
    found = dict(sql(world.a, "SELECT number, assignee_kind FROM conversations"))
    assert found == {number: kind for number, (_, kind) in wanted.items()}
    with pytest.raises(DatabaseError):
        sql(world.a, "UPDATE conversations SET assignee_kind = 'TEAM'")


def test_a_conversation_checks(world: SimpleNamespace) -> None:
    def make(**over: Any) -> Any:
        return lambda: conversation(world.a, world.contact_a, world.account_a, **over)

    refused(world.a, "conversations_number_ck", make(number="CONV-1"))
    refused(world.a, "conversations_status_ck", make(status="BOGUS"))
    refused(world.a, "conversations_attention_ck", make(attention_mode="BOGUS"))
    refused(world.a, "conversations_closed_ck", make(status="CLOSED"))  # sin `closed_at`
    refused(world.a, "conversations_closed_ck", make(closed_at=timezone.now()))  # sin CLOSED
    refused(world.a, "conversations_version_ck", make(assignment_version=0))
    refused(world.a, "conversations_resolved_ck", make(status="RESOLVED"))  # sin `resolved_at`
    refused(world.a, "conversations_preview_ck", make(last_message_preview="Hola"))  # sin sentido
    refused(
        world.a, "conversations_preview_ck", make(last_message_direction="INBOUND")
    )  # sin texto
    refused(
        world.a,
        "conversations_preview_ck",
        make(last_message_preview="Hola", last_message_direction="INTERNAL"),
    )
    ok = make(number="CONV-00020", status="CLOSED", closed_at=timezone.now())()
    assert ok.attention_mode == "HUMAN" and ok.status == "CLOSED"  # HUMAN por omisión
    assert first(world).status == "PENDING"  # D-INB-1: nace en cola, sin tomar


def test_one_open_conversation_per_contact_and_account_and_a_closed_one_makes_room(
    world: SimpleNamespace,
) -> None:
    convo = first(world)
    refused(
        world.a,
        "conversations_open_uq",
        lambda: conversation(world.a, world.contact_a, world.account_a, "CONV-00002"),
    )
    other = conversation(world.b, world.contact_b, world.account_b)  # otra organización: no choca
    assert other.pk != convo.pk
    with tenant_scope(ctx(world.a)):
        Conversation.objects.filter(pk=convo.pk).update(status="CLOSED", closed_at=timezone.now())
    again = conversation(world.a, world.contact_a, world.account_a, "CONV-00002")
    assert again.status == "PENDING"
    refused(  # el número es único por organización
        world.a, "conversations_number_uq", lambda: fresh(world.a, world.account_a, "CONV-00002")
    )


def test_a_channel_account_is_unique_among_the_live_ones_of_the_whole_platform(
    world: SimpleNamespace,
) -> None:
    taken = world.account_a.external_account_id
    refused(
        world.b,
        "channel_accounts_external_uq",  # otra organización tampoco puede repetirla
        lambda: ChannelAccount.objects.create(
            platform="SANDBOX", name="Copia", external_account_id=taken
        ),
    )
    with tenant_scope(ctx(world.a)):
        ChannelAccount.objects.filter(pk=world.account_a.pk).update(deleted_at=timezone.now())
    with tenant_scope(ctx(world.b)):
        assert ChannelAccount.objects.create(
            platform="SANDBOX", name="Copia", external_account_id=taken
        ).pk  # borrada, se puede volver a registrar
    refused(world.a, "channel_accounts_platform_ck", lambda: ChannelAccount.objects.create(
        platform="TELEGRAM", name="x", external_account_id="x", status="PENDING"
    ))  # fmt: skip
    refused(world.a, "channel_accounts_text_ck", lambda: ChannelAccount.objects.create(
        platform="SANDBOX", name=" ", external_account_id="x"
    ))  # fmt: skip


def test_a_message_must_carry_the_channel_account_of_its_conversation(
    world: SimpleNamespace,
) -> None:
    convo = first(world)
    with tenant_scope(ctx(world.a)):
        other = ChannelAccount.objects.create(
            platform="SANDBOX", name="Otra", external_account_id=f"otra-{uuid4()}"
        )
    refused(
        world.a,
        "messages_conversation_fk",
        lambda: Message.objects.create(
            conversation_id=convo.pk,
            channel_account_id=other.pk,
            direction="INBOUND",
            sender_type="CONTACT",
            body="Hola",
        ),
    )


def test_a_note_can_never_be_a_sendable_message(world: SimpleNamespace) -> None:
    convo = first(world)
    note = {"direction": "INTERNAL", "message_type": "NOTE", "sender_type": "USER",
            "sender_user_id": world.ana.pk}  # fmt: skip
    assert message(world.a, convo, **note).message_type == "NOTE"

    def attempt(**over: Any) -> Any:
        return lambda: message(world.a, convo, **({"body": "x"} | over))

    refused(world.a, "messages_internal_ck", attempt(**note, external_message_id="wamid.1"))
    refused(
        world.a,
        "messages_note_ck",
        attempt(**{**note, "direction": "OUTBOUND"}, delivery_status="QUEUED"),
    )
    refused(
        world.a, "messages_note_ck", attempt(**{**note, "message_type": "TEXT"})
    )  # interno, no nota
    refused(
        world.a, "messages_(contact|internal)_ck", attempt(**{**note, "sender_type": "CONTACT"})
    )


def test_message_checks(world: SimpleNamespace) -> None:
    convo = first(world)
    out = {"direction": "OUTBOUND", "sender_type": "USER", "sender_user_id": world.ana.pk}

    def make(**over: Any) -> Any:
        return lambda: message(world.a, convo, **over)

    refused(world.a, "messages_delivery_ck", make(**out))  # lo saliente nace con estado
    refused(world.a, "messages_delivery_ck", make(delivery_status="SENT"))  # lo entrante, no
    refused(world.a, "messages_delivery_values_ck", make(**out, delivery_status="BOGUS"))
    refused(world.a, "messages_contact_ck", make(sender_type="USER", sender_user_id=world.ana.pk))
    refused(
        world.a,
        "messages_contact_ck",
        make(**{**out, "sender_type": "CONTACT"}, delivery_status="QUEUED"),
    )
    refused(
        world.a,
        "messages_sender_user_ck",
        make(direction="OUTBOUND", sender_type="USER", delivery_status="QUEUED"),
    )
    refused(world.a, "messages_body_ck", make(body="   "))
    refused(world.a, "messages_body_ck", make(body="x" * 4097))
    refused(world.a, "messages_direction_ck", make(**{**out, "direction": "SIDEWAYS"}))
    refused(
        world.a,
        "messages_sender_ck",
        make(direction="OUTBOUND", sender_type="ROBOT", delivery_status="QUEUED"),
    )
    refused(world.a, "messages_type_ck", make(message_type="DOCUMENT"))  # aún no (F7-09a2b)
    size = "SELECT character_maximum_length FROM information_schema.columns "
    size += "WHERE table_name = 'messages' AND column_name = 'message_type'"
    assert sql(world.a, size) == [(16,)]  # caben INTERACTIVE y SYSTEM_EVENT de §E.6
    assert make(**out, delivery_status="QUEUED", body="x" * 4096)().delivery_status == "QUEUED"


def test_the_external_and_client_ids_are_unique_where_they_apply(world: SimpleNamespace) -> None:
    convo, other = first(world), conversation(world.b, world.contact_b, world.account_b)
    message(world.a, convo, external_message_id="wamid.1")
    refused(
        world.a,
        "messages_external_uq",
        lambda: message(world.a, convo, external_message_id="wamid.1"),
    )
    message(world.a, convo, external_message_id="wamid.2")  # otro id
    message(world.a, convo)  # sin id externo: no choca con nadie
    message(world.a, convo)
    message(world.b, other, external_message_id="wamid.1")  # otra organización
    with tenant_scope(ctx(world.a)):
        second = ChannelAccount.objects.create(
            platform="SANDBOX", name="Otra", external_account_id=f"otra-{uuid4()}"
        )
    elsewhere = fresh(world.a, second, "CONV-00060")
    message(world.a, elsewhere, external_message_id="wamid.1")  # otra cuenta de la misma: vale
    out = {"direction": "OUTBOUND", "sender_type": "USER", "sender_user_id": world.ana.pk,
           "delivery_status": "QUEUED"}  # fmt: skip
    client = uuid4()
    message(world.a, convo, client_message_id=client, **out)
    refused(
        world.a,
        "messages_client_uq",
        lambda: message(world.a, convo, client_message_id=client, **out),
    )
    message(world.a, convo, client_message_id=uuid4(), **out)


def test_the_runtime_role_cannot_delete_and_the_history_is_immutable(
    world: SimpleNamespace,
) -> None:
    convo = first(world)
    with tenant_scope(ctx(world.a)):
        history = ConversationAssignment.objects.create(
            conversation_id=convo.pk, new_user_id=world.ana.pk, reason_code="TAKE",
            assigned_by_user_id=world.ana.pk,
        )  # fmt: skip
    revoked = {  # la concesión misma: una FK podría estorbar el DELETE por casualidad
        "channel_accounts": ("DELETE", "TRUNCATE"),
        "conversations": ("DELETE", "TRUNCATE"),
        "messages": ("UPDATE", "DELETE", "TRUNCATE"),  # el UPDATE, solo por columna
        "conversation_assignments": ("UPDATE", "DELETE", "TRUNCATE"),
    }
    asked = "SELECT has_table_privilege(current_user, %s, %s)"
    for table, privileges in revoked.items():
        for privilege in privileges:
            assert sql(world.a, asked, [table, privilege]) == [(False,)], (table, privilege)
    with pytest.raises(DatabaseError):  # [INM]: ni cambiarla
        sql(world.a, "UPDATE conversation_assignments SET reason_note = 'otro'")
    with tenant_scope(ctx(world.a)):
        assert ConversationAssignment.objects.get(pk=history.pk).reason_note is None
    sql(world.a, "UPDATE conversations SET version = version + 1")  # lo demás sí se actualiza


def test_an_assignment_names_who_did_it_exactly_once(world: SimpleNamespace) -> None:
    convo = first(world)

    def make(**over: Any) -> Any:
        return lambda: ConversationAssignment.objects.create(
            conversation_id=convo.pk, reason_code="TAKE", **over
        )

    refused(world.a, "conv_assignments_actor_ck", make())  # nadie
    refused(
        world.a,
        "conv_assignments_actor_ck",
        make(assigned_by_user_id=world.ana.pk, assigned_by_system=True),
    )
    refused(world.a, "conv_assignments_reason_ck", lambda: ConversationAssignment.objects.create(
        conversation_id=convo.pk, reason_code="BOGUS", assigned_by_system=True
    ))  # fmt: skip
    with tenant_scope(ctx(world.a)):
        assert make(assigned_by_system=True)().assigned_by_system


def test_the_number_width_and_a_race_for_the_open_conversation(world: SimpleNamespace) -> None:
    with tenant_scope(ctx(world.a)) as tenant, transaction.atomic():
        assert allocate(tenant, "CONV", width=5) == "CONV-00001"
        assert allocate(tenant, "OPP") == "OPP-000001"  # el ancho por omisión no cambia
    start = Barrier(4)

    def race(i: int) -> str:  # entrantes a la vez: solo uno abre la conversación
        try:
            start.wait()
            conversation(world.a, world.contact_a, world.account_a, f"CONV-{i + 10:05d}")
            return "opened"
        except IntegrityError as error:
            return "conversations_open_uq" if "conversations_open_uq" in str(error) else str(error)
        finally:
            connections.close_all()

    with ThreadPoolExecutor(4) as pool:
        outcome = sorted(pool.map(race, range(4)))
    assert outcome == ["conversations_open_uq"] * 3 + ["opened"]
    with tenant_scope(ctx(world.a)):
        assert Conversation.objects.filter(contact_id=world.contact_a.pk).count() == 1


def test_the_delivery_status_only_advances(world: SimpleNamespace) -> None:
    """Todas las parejas (estado actual, estado nuevo): saltar pasos vale, retroceder no, `FAILED`
    solo desde QUEUED, SENDING o SENT y sin salida; solo SENDING vuelve a QUEUED (F7-05b)."""
    convo = first(world)
    out = {"direction": "OUTBOUND", "sender_type": "USER", "sender_user_id": world.ana.pk,
           "delivery_status": "QUEUED"}  # fmt: skip
    order = ["QUEUED", "SENDING", "SENT", "DELIVERED", "READ"]
    allowed = {
        (old, new)
        for old in [*order, "FAILED"]
        for new in [*order, "FAILED"]
        if old != "FAILED"
        and old != new
        and (old in order[:3] if new == "FAILED" else order.index(new) > order.index(old))
    } | {("SENDING", "QUEUED")}  # devolver un reclamo

    def update(pk: UUID, status: str) -> None:
        sql(world.a, "UPDATE messages SET delivery_status = %s WHERE id = %s", [status, pk])

    def status(pk: UUID) -> str:
        return str(sql(world.a, "SELECT delivery_status FROM messages WHERE id = %s", [pk])[0][0])

    for old in [*order, "FAILED"]:
        for new in [*order, "FAILED"]:
            if old == new:
                continue
            pk = message(world.a, convo, **out).pk
            if old != "QUEUED":
                update(pk, old)
            if (old, new) in allowed:
                update(pk, new)
                assert status(pk) == new, (old, new)
            else:
                with pytest.raises(DatabaseError, match="solo avanza"):
                    update(pk, new)
                assert status(pk) == old, (old, new)
    pk = message(world.a, convo, **out).pk
    mutable = (
        "delivery_status",
        "external_message_id",
        "sent_at",
        "delivered_at",
        "read_at",
        "failed_at",
        "policy_decision",  # F7-05a: el despachador la reescribe al revaluar antes de enviar
        "sending_at",  # F7-05b: cuándo se reclamó
        "dispatch_attempts",  # F7-05c: reintentos devueltos y no antes de cuándo
        "not_before",
        "provider_error_code",  # F7-06a: el motivo del fallo, junto al estado
    )
    asked_column = "SELECT has_column_privilege(current_user, 'messages', %s, 'UPDATE')"
    sql(
        world.a,
        "UPDATE messages SET external_message_id = 'wamid.x', sent_at = now() WHERE id = %s",
        [pk],
    )
    columns = sql(
        world.a, "SELECT column_name FROM information_schema.columns WHERE table_name = 'messages'"
    )
    for (column,) in columns:  # toda la demás: la historia no se reescribe
        wanted = column in mutable
        assert sql(world.a, asked_column, [column]) == [(wanted,)], column
    update(pk, "QUEUED")  # el mismo estado no es un cambio: el disparador no actúa


def test_the_same_external_message_arriving_four_times_at_once_is_stored_once(
    world: SimpleNamespace,
) -> None:
    convo, start = first(world), Barrier(4)

    def deliver(_: int) -> str:
        try:
            start.wait()
            message(world.a, convo, external_message_id="wamid.race")
            return "stored"
        except IntegrityError as error:
            return "messages_external_uq" if "messages_external_uq" in str(error) else str(error)
        finally:
            connections.close_all()

    with ThreadPoolExecutor(4) as pool:
        outcome = sorted(pool.map(deliver, range(4)))
    assert outcome == ["messages_external_uq"] * 3 + ["stored"]


def test_the_inbox_list_has_its_recency_index_and_the_runtime_role_can_write_the_preview(
    migrator: psycopg.Connection[Any],
) -> None:
    index = migrator.execute(
        "SELECT indexdef FROM pg_indexes WHERE indexname = 'conversations_org_recent_idx'"
    ).fetchone()
    assert index is not None
    assert "(organization_id, last_message_at DESC, id DESC)" in index[0]
    for column in ("last_message_preview", "last_message_direction"):
        granted = migrator.execute(
            "SELECT has_column_privilege('crm_app', 'conversations', %s, 'UPDATE')", [column]
        ).fetchone()
        assert granted == (True,), column

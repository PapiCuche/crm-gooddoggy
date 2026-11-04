"""F6-01: las tablas `channel_accounts`, `conversations` y
`conversation_assignments` (Inbox), con PostgreSQL real y el rol `crm_app`: RLS, claves compuestas
de tenant, CHECKs, únicos parciales y concesiones mínimas."""

from types import SimpleNamespace
from typing import Any
from uuid import UUID

import psycopg
import pytest
from django.db import DatabaseError, IntegrityError, connection
from django.utils import timezone

from apps.contacts.models import Contact
from apps.conversations.models import (
    ChannelAccount,
    Conversation,
    ConversationAssignment,
)
from core.tenancy.scope import tenant_scope
from tests.factories import make_user
from tests.test_memberships import ctx, join
from tests.test_teams import team

pytestmark = pytest.mark.usefixtures("tenant_db")
TABLES = ("channel_accounts", "conversations", "conversation_assignments")


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
                platform="SANDBOX", name="Sandbox", external_account_id=f"sandbox-{org}"
            )
            setattr(made, f"account_{key}", account)
        setattr(made, f"team_{key}", team(org, f"ventas-{key}"))
    return made


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
        assert not Conversation.objects.exists()


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
        "conv_assignments_conversation_fk": lambda: ConversationAssignment.objects.create(
            conversation_id=theirs.pk, reason_code="MANUAL", assigned_by_system=True
        ),
        "conv_assignments_new_team_fk": lambda: ConversationAssignment.objects.create(
            conversation_id=mine.pk,
            new_team_id=world.team_b.pk,
            reason_code="MANUAL",
            assigned_by_system=True,
        ),  # fmt: skip
        "conv_assignments_assigned_by_user_fk": lambda: ConversationAssignment.objects.create(
            conversation_id=mine.pk, reason_code="MANUAL", assigned_by_user_id=world.luis.pk
        ),
    }
    for constraint, make_row in cases.items():
        refused(world.a, constraint, make_row)


def test_the_assignee_kind_is_derived_and_cannot_be_written(world: SimpleNamespace) -> None:
    join(world.a, make_user())
    kinds = {}
    for number, over in (
        ("CONV-00010", {}),
        ("CONV-00011", {"assigned_team_id": world.team_a.pk}),
        ("CONV-00012", {"assigned_user_id": world.ana.pk}),
        ("CONV-00013", {"assigned_team_id": world.team_a.pk, "assigned_user_id": world.ana.pk}),
    ):
        kinds[number] = fresh(world.a, world.account_a, number, **over).pk
    found = dict(sql(world.a, "SELECT number, assignee_kind FROM conversations"))
    assert found == {
        "CONV-00010": "UNASSIGNED",
        "CONV-00011": "TEAM",
        "CONV-00012": "USER",
        "CONV-00013": "USER",  # el equipo y la persona a la vez (ADR-007)
    }
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
        platform="TELEGRAM", name="x", external_account_id="x"
    ))  # fmt: skip
    refused(world.a, "channel_accounts_text_ck", lambda: ChannelAccount.objects.create(
        platform="SANDBOX", name=" ", external_account_id="x"
    ))  # fmt: skip


def test_the_runtime_role_cannot_delete_and_the_history_is_immutable(
    world: SimpleNamespace,
) -> None:
    convo = first(world)
    with tenant_scope(ctx(world.a)):
        history = ConversationAssignment.objects.create(
            conversation_id=convo.pk, new_user_id=world.ana.pk, reason_code="TAKE",
            assigned_by_user_id=world.ana.pk,
        )  # fmt: skip
    for table in TABLES:
        with pytest.raises(DatabaseError):
            sql(world.a, f"DELETE FROM {table}")  # noqa: S608
        with pytest.raises(DatabaseError):
            sql(world.a, f"TRUNCATE {table}")  # noqa: S608
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

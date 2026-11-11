"""F6-06b: la conversación de origen de una oportunidad (`opportunities.conversation_id`,
migración `0005`): la FK compuesta, el comando, el alcance de `conversations.view` y el detalle.
PostgreSQL real con `crm_app`."""

import json
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.conversations.models import ChannelAccount, Conversation
from apps.deals import opportunities
from apps.deals.models import Opportunity
from apps.deals.opportunities import BadReference, create_opportunity, update_opportunity
from core.tenancy.scope import tenant_scope
from tests import test_opportunity_scopes as scopes
from tests.test_authorization import give
from tests.test_memberships import ctx
from tests.test_opportunities_api import OPPS
from tests.test_opportunities_write_api import acts, create, fail, rows
from tests.test_pipelines_tables import as_migrator
from tests.test_self_context import signed
from tests.test_team_writes import send

crm, pipelines, real_stack = scopes.crm, scopes.pipelines, scopes.real_stack
pytestmark = pytest.mark.usefixtures("tenant_db")
DB = psycopg.Connection[Any]
COLUMN = (
    "SELECT count(*) FROM information_schema.columns"
    " WHERE table_name = 'opportunities' AND column_name = 'conversation_id'"
)
FK = "SELECT count(*) FROM pg_constraint WHERE conname = 'opportunities_conversation_fk'"


def convo(org: UUID, contact: UUID, number: str, via: str = "", **over: Any) -> UUID:
    """Una conversación del contacto, en la cuenta Sandbox de la organización."""
    with tenant_scope(ctx(org)):
        account, _ = ChannelAccount.objects.get_or_create(
            platform="SANDBOX", external_account_id=f"a-{org}{via}", defaults={"name": "Sandbox"}
        )
        made = Conversation.objects.create(
            number=number, contact_id=contact, channel_account_id=account.pk,
            last_message_at=timezone.now(), **over,
        )  # fmt: skip
        return made.pk  # type: ignore[no-any-return]


def made(client: Any, crm: SimpleNamespace, **extra: Any) -> Any:
    return create(client, {"contact_id": str(crm.ids["owned"]), "title": "Desde el Inbox", **extra})


def test_a_conversation_of_the_same_contact_is_saved_shown_and_audited(
    crm: SimpleNamespace, migrator: DB
) -> None:
    origin = convo(crm.a, crm.ids["owned"], "CONV-00001", assigned_user_id=crm.vera.pk)
    answer = made(signed(crm.ana), crm, conversation_id=str(origin))
    assert answer.status_code == 201, answer.content
    assert answer.json()["conversation_id"] == str(origin)
    assert signed(crm.ana).get(f"{OPPS}{answer.json()['id']}/").json() == answer.json()
    changes = migrator.execute(
        "SELECT changes::text FROM audit_logs WHERE action = 'opportunity.created'"
    ).fetchone()
    assert changes is not None and json.loads(changes[0])["conversation_id"] == [None, str(origin)]
    assert acts(migrator) == [("opportunity.created", crm.ana.pk)]
    without = made(signed(crm.ana), crm)
    assert without.json()["conversation_id"] is None  # sin conversación: nulo
    audited = migrator.execute(
        "SELECT changes::text FROM audit_logs WHERE action = 'opportunity.created' ORDER BY id"
    ).fetchall()
    assert "conversation_id" not in audited[-1][0]


def test_every_conversation_that_does_not_fit_gives_the_same_error(
    crm: SimpleNamespace, migrator: DB
) -> None:
    other_contact = convo(crm.a, crm.ids["plain"], "CONV-00002")  # de otro contacto
    theirs = convo(crm.b, crm.ids["b"], "CONV-00003")  # de otra organización
    sams = convo(
        crm.a, crm.ids["owned"], "CONV-00004", "s", assigned_user_id=crm.sam.pk
    )  # fuera de OWN
    bad = [other_contact, theirs, sams, uuid4()]
    vera, before = signed(crm.vera), rows(migrator)
    answers = {made(vera, crm, conversation_id=str(c)).content for c in bad}
    assert len(answers) == 1  # el mismo 400 con el mismo cuerpo: no sirve para sondear
    assert fail(made(vera, crm, conversation_id=str(sams))) == (
        400, "VALIDATION_ERROR", ["conversation_id"],
    )  # fmt: skip
    assert rows(migrator) == before
    mine = convo(crm.a, crm.ids["owned"], "CONV-00005", "m", assigned_user_id=crm.vera.pk)
    assert made(vera, crm, conversation_id=str(mine)).status_code == 201  # la suya
    free = convo(crm.a, crm.ids["owned"], "CONV-00006", "f")  # sin asignar: dentro de OWN
    assert made(vera, crm, conversation_id=str(free)).status_code == 201
    assert made(signed(crm.ana), crm, conversation_id=str(sams)).status_code == 201  # ORGANIZATION


def test_the_scope_that_counts_is_the_one_of_conversations_view_not_the_one_of_contacts_view(
    crm: SimpleNamespace,
) -> None:
    give(  # ve a todos los contactos, pero solo sus propias conversaciones
        crm.a, crm.m["bea"],
        {"opportunities.create": None, "contacts.view": "ORGANIZATION",
         "conversations.view": "OWN"},
    )  # fmt: skip
    sams = convo(crm.a, crm.ids["owned"], "CONV-00012", "b", assigned_user_id=crm.sam.pk)
    mine = convo(crm.a, crm.ids["owned"], "CONV-00013", "c", assigned_user_id=crm.bea.pk)
    bea = signed(crm.bea)
    assert fail(made(bea, crm, conversation_id=str(sams)))[2] == ["conversation_id"]
    assert made(bea, crm, conversation_id=str(mine)).status_code == 201


def test_the_command_never_changes_the_conversation_of_an_opportunity(
    crm: SimpleNamespace,
) -> None:
    created = made(signed(crm.ana), crm).json()
    origin = convo(crm.a, crm.ids["owned"], "CONV-00014")
    with tenant_scope(ctx(crm.a, crm.ana)) as tenant, pytest.raises(ValueError, match="campos"):
        update_opportunity(
            tenant, opportunity_id=UUID(created["id"]), version=created["version"],
            conversation_id=origin,
        )  # fmt: skip


def test_a_conversation_id_that_is_not_an_id_is_a_field_error_and_it_is_not_editable(
    crm: SimpleNamespace,
) -> None:
    ana = signed(crm.ana)
    assert fail(made(ana, crm, conversation_id="no-es-un-id"))[2] == ["conversation_id"]
    created = made(ana, crm).json()
    origin = convo(crm.a, crm.ids["owned"], "CONV-00007")
    patch = send(
        ana, "patch", f"{OPPS}{created['id']}/",
        {"version": created["version"], "conversation_id": str(origin)},
    )  # fmt: skip
    assert patch.status_code == 400  # se guarda al crear, no se cambia después


def test_the_command_checks_the_organization_and_the_contact_and_the_database_the_foreign_key(
    crm: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    wrongs = (
        convo(crm.a, crm.ids["plain"], "CONV-00008"),
        convo(crm.b, crm.ids["b"], "CONV-00009"),
        uuid4(),
    )
    with tenant_scope(ctx(crm.a, crm.ana)) as tenant:
        for wrong in wrongs:
            with pytest.raises(BadReference) as bad:
                create_opportunity(
                    tenant, contact_id=crm.ids["owned"], title="x", conversation_id=wrong
                )
            assert bad.value.field == "conversation_id"
        monkeypatch.setattr(opportunities, "_origin", lambda *args: None)  # solo queda la FK
        with pytest.raises(BadReference, match="conversation_id"):
            create_opportunity(
                tenant, contact_id=crm.ids["owned"], title="x", conversation_id=wrongs[1]
            )
        monkeypatch.undo()
        with pytest.raises(ValueError, match="conversación"):
            create_opportunity(
                tenant,
                contact_id=crm.ids["owned"],
                title="x",
                conversation_id="no",  # type: ignore[arg-type]
            )
    opp_id = made(signed(crm.ana), crm).json()["id"]
    foreign = convo(crm.b, crm.ids["b"], "CONV-00010", "2")
    with (
        tenant_scope(ctx(crm.a)),
        pytest.raises(IntegrityError, match="opportunities_conversation_fk"),
    ):
        with transaction.atomic():  # la FK compuesta: aunque alguien se saltara el comando
            Opportunity.objects.filter(pk=opp_id).update(conversation_id=foreign)


def test_the_migration_goes_back_and_forward_keeping_the_opportunities(
    crm: SimpleNamespace, migrator: DB
) -> None:
    origin = convo(crm.a, crm.ids["owned"], "CONV-00011")
    created = made(signed(crm.ana), crm, conversation_id=str(origin)).json()
    assert migrator.execute(COLUMN).fetchone() == (1,) and migrator.execute(FK).fetchone() == (1,)
    with as_migrator():
        call_command("migrate", "deals", "0004", verbosity=0)
    assert migrator.execute(COLUMN).fetchone() == (0,) and migrator.execute(FK).fetchone() == (0,)
    assert migrator.execute("SELECT count(*) FROM opportunities").fetchone() == (1,)
    with as_migrator():
        call_command("migrate", verbosity=0)
    assert migrator.execute(COLUMN).fetchone() == (1,) and migrator.execute(FK).fetchone() == (1,)
    assert migrator.execute("SELECT conversation_id FROM opportunities").fetchone() == (None,)
    assert created["conversation_id"] == str(
        origin
    )  # lo que se perdió al volver atrás es el vínculo

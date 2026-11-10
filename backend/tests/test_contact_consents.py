"""F7-08: `contact_consents`, su historial inmutable y los comandos que los escriben (ADR-010,
02 §E.5), con PostgreSQL real y el rol `crm_app`; también `contacts.is_blocked`."""

import threading
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from django.core.management import call_command
from django.db import DatabaseError, IntegrityError, connection

from apps.contacts import selectors
from apps.contacts.commands import set_blocked
from apps.contacts.consents import grant_consent, revoke_consent
from apps.contacts.models import Contact, ContactConsent, ContactConsentEvent
from core.tenancy.scope import tenant_scope
from tests.factories import make_user
from tests.test_anti_escalation import audit
from tests.test_memberships import ctx, join
from tests.test_pipelines_tables import as_migrator

pytestmark = pytest.mark.usefixtures("tenant_db")
WA, ALL, OPT_IN, STOP = "WHATSAPP", "ALL_PROACTIVE", "CHANNEL_OPT_IN", "KEYWORD_STOP"
PRIVATE = "Mi DNI es 12345678"  # noqa: S105 — nunca debe llegar a la auditoría


@pytest.fixture
def world(orgs: dict[str, Any]) -> SimpleNamespace:
    ana, luis = make_user(), make_user()
    join(orgs["A"], ana)
    join(orgs["B"], luis)
    with tenant_scope(ctx(orgs["A"])):
        mine = Contact.objects.create(display_name="Clienta").pk
    with tenant_scope(ctx(orgs["B"])):
        theirs = Contact.objects.create(display_name="Ajeno").pk
    return SimpleNamespace(a=orgs["A"], b=orgs["B"], ana=ana, luis=luis, mine=mine, theirs=theirs)


def sql(org: UUID, statement: str, params: list[Any] | None = None) -> list[Any]:
    with tenant_scope(ctx(org)), connection.cursor() as cursor:
        cursor.execute(statement, params)
        return cursor.fetchall() if cursor.description else [(cursor.rowcount,)]


def run(org: UUID, command: Any, who: Any = None, **kw: Any) -> Any:
    with tenant_scope(ctx(org, who)) as tenant:
        return command(tenant, **kw)


def grant(w: SimpleNamespace, purpose: str = ALL, channel: str = WA, **kw: Any) -> Any:
    kw = {"source": OPT_IN} | kw
    return run(w.a, grant_consent, w.ana, contact_id=w.mine, channel=channel, purpose=purpose, **kw)


def revoke(w: SimpleNamespace, purpose: str = ALL, channel: str = WA, **kw: Any) -> Any:
    kw = {"source": STOP} | kw
    return run(w.a, revoke_consent, None, contact_id=w.mine, channel=channel, purpose=purpose, **kw)


def history(w: SimpleNamespace) -> list[Any]:
    with tenant_scope(ctx(w.a)):
        return [(e.status, e.source) for e in selectors.consent_history(w.mine)]


# ---- Tablas: aislamiento, referencias y lo que la base no deja ----


def test_what_a_records_never_exists_for_b(world: SimpleNamespace) -> None:
    grant(world)
    for table in ("contact_consents", "contact_consent_events"):  # RLS, no solo el manager
        assert len(sql(world.a, f"SELECT 1 FROM {table}")) == 1  # noqa: S608
        assert sql(world.b, f"SELECT 1 FROM {table}") == []  # noqa: S608
    insert = (
        "INSERT INTO contact_consent_events (id, organization_id, contact_id, channel, purpose,"
        " status, source, evidence, occurred_at) VALUES (%s, %s, %s, 'WHATSAPP', 'ALL_PROACTIVE',"
        " 'REVOKED', 'AGENT_RECORDED', '{}', now())"
    )
    with pytest.raises(DatabaseError, match="row-level security"):  # tampoco en la ajena
        sql(world.b, insert, [uuid4(), world.a, world.mine])
    with pytest.raises(
        IntegrityError, match="consent_events_contact_fk"
    ):  # ni con un contacto ajeno
        sql(world.a, insert, [uuid4(), world.a, world.theirs])
    state = (
        "INSERT INTO contact_consents (id, organization_id, contact_id, channel, purpose, status,"
        " source, revoked_at, evidence, updated_at) VALUES (%s, %s, %s, 'SMS', 'ALL_PROACTIVE',"
        " 'REVOKED', 'AGENT_RECORDED', now(), '{}', now())"
    )
    with pytest.raises(DatabaseError, match="row-level security"):  # el estado vigente, igual
        sql(world.b, state, [uuid4(), world.a, world.mine])
    sql(world.a, state, [uuid4(), world.a, world.mine])
    with pytest.raises(
        IntegrityError, match="contact_consents_org_combo_uq"
    ):  # una por combinación
        sql(world.a, state, [uuid4(), world.a, world.mine])


def test_the_foreign_keys_keep_every_reference_in_the_organization(world: SimpleNamespace) -> None:
    insert = (
        "INSERT INTO contact_consents (id, organization_id, contact_id, channel, purpose, status,"
        " source, revoked_at, evidence, recorded_by_user_id, updated_at) VALUES (%s, %s, %s,"
        " 'WHATSAPP', 'ALL_PROACTIVE', 'REVOKED', 'AGENT_RECORDED', now(), '{}', %s, now())"
    )
    sql(world.a, insert, [uuid4(), world.a, world.mine, world.ana.pk])  # lo propio sí
    for contact, member, fk in (
        (world.theirs, world.ana.pk, "contact_consents_contact_fk"),  # el contacto de B en A
        (world.mine, world.luis.pk, "contact_consents_recorded_by_fk"),  # un miembro de B en A
    ):
        with pytest.raises(IntegrityError, match=fk):
            sql(world.a, insert.replace("'WHATSAPP'", "'SMS'"), [uuid4(), world.a, contact, member])


def test_the_database_rejects_what_the_commands_never_write(
    world: SimpleNamespace, migrator: psycopg.Connection[Any]
) -> None:
    grant(world)  # el propietario sí puede escribir: aquí lo que se prueba son las restricciones
    for table, prefix in (
        ("contact_consents", "contact_consents"),
        ("contact_consent_events", "consent_events"),
    ):
        for statement, constraint in (
            ("SET channel = 'FAX'", "channel_ck"),
            ("SET purpose = 'SPAM'", "purpose_ck"),
            ("SET status = 'UNKNOWN'", "st(?:atus|amp)_ck"),  # en el vigente, antes salta la fecha
            ("SET source = 'GUESS'", "source_ck"),
            ("SET source = 'KEYWORD_STOP'", "stop_ck"),  # la palabra de baja solo revoca
        ):
            with pytest.raises(psycopg.errors.CheckViolation, match=f"{prefix}_{constraint}"):
                migrator.execute(f"UPDATE {table} {statement}")  # noqa: S608
    with pytest.raises(psycopg.errors.CheckViolation, match="contact_consents_stamp_ck"):
        migrator.execute("UPDATE contact_consents SET status = 'REVOKED'")  # la baja sin su fecha


def test_the_history_is_never_edited_or_deleted_and_the_state_keeps_its_identity(
    world: SimpleNamespace,
) -> None:
    grant(world)
    revoke(world)
    for statement in (
        "UPDATE contact_consent_events SET status = 'GRANTED'",
        "DELETE FROM contact_consent_events",
        "TRUNCATE contact_consent_events",
        "DELETE FROM contact_consents",
        "TRUNCATE contact_consents",
        "UPDATE contact_consents SET id = gen_random_uuid()",
        "UPDATE contact_consents SET organization_id = gen_random_uuid()",
        "UPDATE contact_consents SET channel = 'SMS'",  # lo que identifica la fila no se reescribe
        "UPDATE contact_consents SET purpose = 'MARKETING'",
        "UPDATE contact_consents SET contact_id = gen_random_uuid()",
    ):
        with pytest.raises(DatabaseError, match="permission denied"):
            sql(world.a, statement)
    assert history(world) == [("REVOKED", STOP), ("GRANTED", OPT_IN)]


# ---- Comandos ----


def test_granting_writes_the_state_the_event_and_the_audit_and_repeating_writes_nothing(
    world: SimpleNamespace, migrator: psycopg.Connection[Any]
) -> None:
    row, changed = grant(world, evidence={"form": "web-1"})
    assert (row.status, row.source, changed) == ("GRANTED", OPT_IN, True)
    assert row.granted_at is not None and row.revoked_at is None
    assert row.recorded_by_user_id == world.ana.pk and row.evidence == {"form": "web-1"}
    _, again = grant(world, source="IN_STORE")
    assert again is False and history(world) == [("GRANTED", OPT_IN)]  # sin evento ni cambio
    done = [a for a in audit(migrator) if a[0].startswith("consent.")]
    assert [a[0] for a in done] == ["consent.granted"]
    assert done[0][3] == {"status": [None, "GRANTED"]}  # el antes y el después
    assert done[0][4] == {
        "contact_id": str(world.mine), "channel": WA, "purpose": ALL, "source": OPT_IN,
    }  # fmt: skip


def test_revoking_after_granting_and_granting_again_keep_one_state_and_all_the_history(
    world: SimpleNamespace,
) -> None:
    grant(world)
    row, changed = revoke(world, evidence={"message_id": "wamid.1"})
    assert (row.status, changed, row.revoked_at is not None) == ("REVOKED", True, True)
    assert row.granted_at is not None  # la última concesión se conserva en el estado
    revoke(world)  # repetir la baja no añade nada
    again, _ = grant(world)
    assert (again.pk, again.status) == (row.pk, "GRANTED")
    assert history(world) == [("GRANTED", OPT_IN), ("REVOKED", STOP), ("GRANTED", OPT_IN)]
    with tenant_scope(ctx(world.a)):
        assert ContactConsent.objects.count() == 1 and ContactConsentEvent.objects.count() == 3


def test_a_customer_who_never_agreed_can_still_opt_out(world: SimpleNamespace) -> None:
    row, changed = revoke(world)
    assert (row.status, row.granted_at, changed) == ("REVOKED", None, True)


@pytest.mark.parametrize(
    "bad",
    [
        {"channel": "FAX"}, {"purpose": "SPAM"}, {"source": "GUESS"}, {"source": STOP},
        {"evidence": {"text": PRIVATE}}, {"evidence": {"form": "x" * 201}},
        {"evidence": {"form": 7}}, {"evidence": ["form"]},
    ],
)  # fmt: skip
def test_what_the_commands_do_not_accept_writes_nothing(world: SimpleNamespace, bad: Any) -> None:
    with pytest.raises(ValueError):  # noqa: PT011
        grant(world, **bad)
    with tenant_scope(ctx(world.a)):
        assert not ContactConsent.objects.exists() and not ContactConsentEvent.objects.exists()


def test_a_contact_of_another_organization_or_a_deleted_one_does_not_exist(
    world: SimpleNamespace,
) -> None:
    for org, contact in ((world.a, world.theirs), (world.b, world.mine)):
        with pytest.raises(Contact.DoesNotExist):
            run(org, revoke_consent, contact_id=contact, channel=WA, purpose=ALL, source=STOP)
    sql(world.a, "UPDATE contacts SET deleted_at = now()")
    with pytest.raises(Contact.DoesNotExist):
        grant(world)


def test_the_audit_never_carries_the_evidence(
    world: SimpleNamespace, migrator: psycopg.Connection[Any]
) -> None:
    grant(world, evidence={"note": PRIVATE})
    assert all(PRIVATE not in str(a) for a in audit(migrator))


def test_two_opt_outs_at_once_leave_one_event(world: SimpleNamespace) -> None:
    start, outcome = threading.Barrier(2), []

    def work() -> None:
        try:
            start.wait(timeout=10)
            outcome.append(revoke(world)[1])
        except Exception as error:  # noqa: BLE001 — lo comprueba el test
            outcome.append(type(error).__name__)
        finally:
            connection.close()

    threads = [threading.Thread(target=work) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert sorted(map(str, outcome)) == ["False", "True"], outcome
    assert history(world) == [("REVOKED", STOP)]


# ---- Consulta: qué revocaciones alcanzan a un envío ----


@pytest.mark.parametrize(
    ("revoked", "asked", "blocked"),
    [
        ((WA, ALL), (WA, "FOLLOW_UP"), True),  # la baja de WhatsApp alcanza a todo lo proactivo
        (("ANY", ALL), ("EMAIL", "MARKETING"), True),  # y la global, a cualquier canal
        ((WA, "MARKETING"), (WA, "MARKETING"), True),
        ((WA, "MARKETING"), (WA, "FOLLOW_UP"), False),  # un propósito no arrastra a otro
        ((WA, ALL), ("EMAIL", "FOLLOW_UP"), False),  # ni un canal a otro
    ],
)
def test_which_revocations_reach_a_send(
    world: SimpleNamespace, revoked: tuple[str, str], asked: tuple[str, str], blocked: bool
) -> None:
    revoke(world, channel=revoked[0], purpose=revoked[1])
    with tenant_scope(ctx(world.a)):
        assert selectors.is_revoked(world.mine, *asked) is blocked
    with tenant_scope(ctx(world.b)):  # y lo de A no es de B, ni del contacto de B
        assert not selectors.is_revoked(world.mine, *asked)
        assert not selectors.is_revoked(world.theirs, *asked)


def test_the_revocation_of_one_contact_never_reaches_another_of_the_same_organization(
    world: SimpleNamespace,
) -> None:
    with tenant_scope(ctx(world.a)):
        other = Contact.objects.create(display_name="Otra clienta").pk
    revoke(world, channel="ANY", purpose=ALL)
    with tenant_scope(ctx(world.a)):
        assert selectors.is_revoked(world.mine, WA, "MARKETING")
        assert not selectors.is_revoked(other, WA, "MARKETING")


def test_a_grant_is_not_a_revocation(world: SimpleNamespace) -> None:
    grant(world)
    with tenant_scope(ctx(world.a)):
        assert not selectors.is_revoked(world.mine, WA, ALL)
        assert [c.status for c in selectors.consents_of(world.mine)] == ["GRANTED"]


# ---- `is_blocked` ----


def test_blocking_a_contact_is_audited_and_repeating_it_writes_nothing(
    world: SimpleNamespace, migrator: psycopg.Connection[Any]
) -> None:
    blocked = run(world.a, set_blocked, world.ana, contact_id=world.mine, blocked=True)
    assert (blocked.is_blocked, blocked.version, blocked.updated_by_user_id) == (
        True, 2, world.ana.pk,
    )  # fmt: skip
    again = run(world.a, set_blocked, world.ana, contact_id=world.mine, blocked=True)
    assert again.version == 2
    freed = run(world.a, set_blocked, world.ana, contact_id=world.mine, blocked=False)
    assert (freed.is_blocked, freed.version) == (False, 3)
    done = [(a[0], a[3]) for a in audit(migrator) if a[0].startswith("contact.")]
    assert done[-2:] == [
        ("contact.blocked", {"is_blocked": [False, True]}),
        ("contact.unblocked", {"is_blocked": [True, False]}),
    ]
    with pytest.raises(Contact.DoesNotExist):
        run(world.b, set_blocked, contact_id=world.mine, blocked=True)
    sql(world.a, "UPDATE contacts SET deleted_at = now()")
    with pytest.raises(Contact.DoesNotExist):  # uno borrado no se bloquea ni se desbloquea
        run(world.a, set_blocked, world.ana, contact_id=world.mine, blocked=False)


def test_the_migration_goes_back_and_forward_keeping_the_contacts(
    world: SimpleNamespace, migrator: psycopg.Connection[Any]
) -> None:
    """Atrás se van las dos tablas y la columna, pero el contacto se queda; adelante vuelven vacías,
    con `is_blocked` falso y los mismos privilegios y FORCE RLS."""
    grant(world)
    run(world.a, set_blocked, world.ana, contact_id=world.mine, blocked=True)
    exists = "SELECT to_regclass('public.contact_consents') IS NOT NULL"
    column = (
        "SELECT count(*) FROM information_schema.columns"
        " WHERE table_name = 'contacts' AND column_name = 'is_blocked'"
    )
    update = "SELECT has_table_privilege('crm_app', 'contact_consent_events', 'UPDATE')"
    force = "SELECT relforcerowsecurity FROM pg_class WHERE relname = 'contact_consent_events'"
    assert migrator.execute(exists).fetchone() == (True,)
    assert migrator.execute(update).fetchone() == (False,)
    with as_migrator():
        call_command("migrate", "contacts", "0006", verbosity=0)
    assert migrator.execute(exists).fetchone() == (False,)
    assert migrator.execute(column).fetchone() == (0,)
    assert migrator.execute("SELECT count(*) FROM contacts").fetchone() == (2,)  # los de A y B
    with as_migrator():
        call_command("migrate", "contacts", verbosity=0)
    assert migrator.execute(exists).fetchone() == (True,)
    assert migrator.execute("SELECT count(*) FROM contact_consents").fetchone() == (0,)
    assert migrator.execute("SELECT is_blocked FROM contacts").fetchall() == [(False,), (False,)]
    assert migrator.execute(update).fetchone() == (False,)  # el historial sigue sin UPDATE
    assert migrator.execute(force).fetchone() == (True,)
    assert history(world) == []
    grant(world)  # y vuelve a funcionar
    assert history(world) == [("GRANTED", OPT_IN)]

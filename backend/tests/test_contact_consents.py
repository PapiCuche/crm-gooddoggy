"""F7-08: `contact_consents`, su historial inmutable y los comandos que los escriben (ADR-010,
02 §E.5), con PostgreSQL real y el rol `crm_app`; también `contacts.is_blocked`."""

import datetime as dt
import random
import threading
import time
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from django.core.management import call_command
from django.db import DatabaseError, IntegrityError, connection

from apps.contacts import consents, selectors
from apps.contacts.commands import set_blocked
from apps.contacts.consents import grant_consent, keyword_stop, lift_keyword_stop, revoke_consent
from apps.contacts.models import ConsentSource, Contact, ContactConsent, ContactConsentEvent
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
        call_command("migrate", verbosity=0)  # todo: `quotes` y `tasks` dependen de `contacts`
    assert migrator.execute(exists).fetchone() == (True,)
    assert migrator.execute("SELECT count(*) FROM contact_consents").fetchone() == (0,)
    assert migrator.execute("SELECT is_blocked FROM contacts").fetchall() == [(False,), (False,)]
    assert migrator.execute(update).fetchone() == (False,)  # el historial sigue sin UPDATE
    assert migrator.execute(force).fetchone() == (True,)
    assert history(world) == []
    grant(world)  # y vuelve a funcionar
    assert history(world) == [("GRANTED", OPT_IN)]


# ---- F7b-05f: la hora de Meta acotada; el origen que un alta no levanta; la espera en el contacto


NOW = dt.datetime(2026, 11, 18, 12, 0, 0, 700_000, tzinfo=dt.UTC)  # nuestro reloj, con fracción
SECOND, S = NOW.replace(microsecond=0), dt.timedelta(seconds=1)
OTHERS = sorted(set(ConsentSource.values) - {STOP})  # cada origen que no es una palabra de baja
WAITING_ON_A_CONTACT = (  # un backend con la fila de un contacto pedida, esperando a quien la tiene
    "SELECT count(*) FROM pg_locks t JOIN pg_locks w ON w.pid = t.pid AND NOT w.granted"
    " AND w.locktype = 'transactionid' WHERE t.locktype = 'tuple' AND t.granted"
    " AND t.relation = 'contacts'::regclass"
)


def word(w: SimpleNamespace, said: str, at: dt.datetime | None) -> bool:
    command = keyword_stop if said == "BAJA" else lift_keyword_stop
    return bool(run(w.a, command, contact_id=w.mine, channel=WA, purpose=ALL, said_at=at))


def past() -> dt.datetime:
    """Una hora de Meta de hace una hora, por el reloj real (más vieja que 48 h no se cree)."""
    return dt.datetime.now(dt.UTC).replace(microsecond=0) - dt.timedelta(hours=1)


def stored(w: SimpleNamespace) -> tuple[str, str, dt.datetime | None]:
    with tenant_scope(ctx(w.a)):
        row = ContactConsent.objects.get()
    return row.status, row.source, row.keyword_at


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> list[dt.datetime]:
    """Nuestro reloj, el que lee `consents`, fijo en `NOW` hasta que el test lo mueva."""
    now = [NOW]
    monkeypatch.setattr(consents, "timezone", SimpleNamespace(now=lambda: now[0]))
    return now


@pytest.mark.parametrize("ahead", [59, 60, 61])
@pytest.mark.parametrize("said", ["BAJA", "ALTA"])
@pytest.mark.usefixtures("clock")
def test_an_alta_ahead_of_our_clock_counts_only_up_to_the_allowance_and_a_baja_always(
    world: SimpleNamespace, said: str, ahead: int
) -> None:
    assert consents.KEYWORD_AHEAD == 60 * S
    counts = said == "BAJA" or ahead <= 60
    assert word(world, said, SECOND + ahead * S) is counts
    with tenant_scope(ctx(world.a)):
        found = list(ContactConsent.objects.values_list("status", "keyword_at"))
    status = "REVOKED" if said == "BAJA" else "GRANTED"
    assert found == ([(status, SECOND + ahead * S)] if counts else [])  # ni concede ni guarda


def test_past_the_allowance_an_older_alta_never_lifts_a_newer_baja(
    world: SimpleNamespace, clock: list[dt.datetime]
) -> None:
    """Revisión de #951: nuestro reloj 65 s detrás de Meta; la BAJA de las 12:00:10 llega a las
    11:59:05 y un ALTA de las 12:00:08, entregada 7 s tarde, a las 11:59:10."""
    clock[0] = SECOND + 10 * S - 65 * S
    assert word(world, "BAJA", SECOND + 10 * S) is True
    clock[0] = SECOND + 8 * S - 65 * S + 7 * S
    assert word(world, "ALTA", SECOND + 8 * S) is False
    assert stored(world) == ("REVOKED", STOP, SECOND + 10 * S)  # su hora, no un tope


@pytest.mark.parametrize("granted", [False, True])
def test_a_far_future_alta_never_blocks_a_later_real_baja(
    world: SimpleNamespace, clock: list[dt.datetime], granted: bool
) -> None:
    """Revisión de #951: nuestro reloj 5 min delante de Meta; un ALTA con hora de 2099 no cuenta
    (sin fila, o sobre un alta anterior) y la BAJA real de 10 s después revoca."""
    clock[0] = NOW + 300 * S
    if granted:
        assert word(world, "ALTA", SECOND) is True
    assert word(world, "ALTA", dt.datetime(2099, 12, 31, tzinfo=dt.UTC)) is False
    with tenant_scope(ctx(world.a)):
        assert list(ContactConsent.objects.values_list("keyword_at", flat=True)) == (
            [SECOND] if granted else []
        )
    clock[0] = NOW + 310 * S
    assert word(world, "BAJA", SECOND + 10 * S) is True
    assert stored(world) == ("REVOKED", STOP, SECOND + 10 * S)


def test_a_far_future_baja_blocks_keyword_altas_on_the_side_of_the_opt_out(
    world: SimpleNamespace, clock: list[dt.datetime]
) -> None:
    far = dt.datetime(2099, 12, 31, tzinfo=dt.UTC)
    assert word(world, "BAJA", far) is True
    clock[0] = NOW + 3600 * S
    assert word(world, "ALTA", SECOND + 3600 * S) is False
    assert stored(world) == ("REVOKED", STOP, far)


@pytest.mark.parametrize(
    ("alta", "later", "status"), [(30, 0, "REVOKED"), (60, 0, "REVOKED"), (61, 1, "GRANTED")]
)
def test_a_baja_without_a_time_takes_our_clock_plus_the_allowance(
    world: SimpleNamespace, clock: list[dt.datetime], alta: int, later: int, status: str
) -> None:
    """Revisión de #951 (3.ª pasada): una baja sin hora guarda nuestra hora más el margen; ningún
    alta dentro del margen la levanta (en un empate, la baja); una posterior, ya dentro, sí."""
    assert word(world, "ALTA", SECOND + 30 * S) is True  # 30 s por delante de nuestro reloj
    assert word(world, "BAJA", None) is True
    assert stored(world) == ("REVOKED", STOP, SECOND + 60 * S)
    clock[0] = NOW + later * S
    word(world, "ALTA", SECOND + alta * S)
    assert stored(world)[0] == status


def test_with_our_clock_behind_meta_an_older_alta_never_lifts_an_untimed_baja(
    world: SimpleNamespace, clock: list[dt.datetime]
) -> None:
    """El límite que cerró la 3.ª pasada: nuestro reloj 30 s detrás de Meta; la BAJA sin hora (dicha
    a las 12:00:10) y un ALTA de las 12:00:08 entregada 5 s después."""
    clock[0] = SECOND + 10 * S - 30 * S
    assert word(world, "BAJA", None) is True
    clock[0] += 5 * S
    assert word(world, "ALTA", SECOND + 8 * S) is False
    assert stored(world) == ("REVOKED", STOP, SECOND + 40 * S)


def test_a_baja_without_a_time_keeps_a_later_stored_time(
    world: SimpleNamespace, clock: list[dt.datetime]
) -> None:
    """Un alta al borde del margen y nuestro reloj que luego retrocede 30 s (NTP): una baja sin hora
    toma la hora guardada (la mayor) y revoca: en el empate, la baja."""
    assert word(world, "ALTA", SECOND + 60 * S) is True
    clock[0] = NOW - 30 * S
    assert word(world, "BAJA", None) is True
    assert stored(world) == ("REVOKED", STOP, SECOND + 60 * S)


# ---- F7b-05g: un cambio manual y una hora imposible por lo vieja no pierden la baja


@pytest.mark.parametrize("off", [0, 30, -30, 600, -600])  # nuestro reloj respecto al de Meta
@pytest.mark.parametrize("later", ["now", "older", "untimed"])
def test_a_manual_grant_clears_the_keyword_time_so_a_later_real_baja_always_revokes(
    world: SimpleNamespace, clock: list[dt.datetime], later: str, off: int
) -> None:
    """BAJA con hora futura → concesión del agente → BAJA real (de ahora por Meta, una más vieja
    entregada después, o sin hora): siempre `REVOKED`, con nuestro reloj desfasado o no."""
    assert word(world, "BAJA", SECOND + 3600 * S) is True
    clock[0] = NOW + off * S
    row, changed = grant(world, source="AGENT_RECORDED")
    assert (changed, row.status, row.keyword_at) == (True, "GRANTED", None)
    said = {"now": SECOND, "older": SECOND - 600 * S, "untimed": None}[later]
    clock[0] = NOW + (off + 5) * S
    assert word(world, "BAJA", said) is True
    assert stored(world) == ("REVOKED", STOP, said or SECOND + (off + 65) * S)
    assert word(world, "ALTA", SECOND - 700 * S) is False  # más vieja que esa baja: no la levanta
    assert stored(world)[0] == "REVOKED"


def test_a_manual_grant_that_changes_nothing_keeps_the_time_of_the_last_alta(
    world: SimpleNamespace, clock: list[dt.datetime]
) -> None:
    """Ya concedido por un alta: la concesión no escribe nada; una baja más vieja que esa alta no
    revoca (la última palabra fue el alta) y una más nueva sí."""
    assert word(world, "ALTA", SECOND) is True
    row, changed = grant(world, source="AGENT_RECORDED")
    assert (changed, row.keyword_at) == (False, SECOND)
    assert word(world, "BAJA", SECOND - 5 * S) is False
    assert word(world, "BAJA", SECOND + S) is True


@pytest.mark.parametrize(
    ("age", "stale"),
    [(48 * 3600 - 1, False), (48 * 3600, False), (48 * 3600 + 1, True), (None, True)],
)  # `None`: una hora de 2001, válida para `inbound.instant` pero imposible por lo vieja
@pytest.mark.usefixtures("clock")
def test_a_baja_older_than_meta_retries_counts_as_untimed_and_beats_a_newer_alta(
    world: SimpleNamespace, age: int | None, stale: bool
) -> None:
    """La edad se mide con nuestro reloj, leído con el contacto bloqueado (la prueba de arriba)."""
    assert consents.KEYWORD_STALE == 48 * 3600 * S
    assert word(world, "ALTA", SECOND) is True
    said = dt.datetime(2001, 9, 9, 1, 46, 40, tzinfo=dt.UTC) if age is None else SECOND - age * S
    assert word(world, "BAJA", said) is stale
    assert stored(world) == (
        ("REVOKED", STOP, SECOND + 60 * S) if stale else ("GRANTED", "CUSTOMER_MESSAGE", SECOND)
    )


@pytest.mark.parametrize(("age", "counts"), [(48 * 3600, True), (48 * 3600 + 1, False)])
@pytest.mark.usefixtures("clock")
def test_an_alta_older_than_meta_retries_counts_for_nothing(
    world: SimpleNamespace, age: int, counts: bool
) -> None:
    """Como un alta sin hora (decisión del propietario en #937): ni concede ni guarda una hora, y no
    levanta la baja de una hora de 2001 (que cuenta como sin hora: nuestra hora más el margen)."""
    assert word(world, "ALTA", SECOND - age * S) is counts
    with tenant_scope(ctx(world.a)):
        assert ContactConsent.objects.exists() is counts
    assert word(world, "BAJA", dt.datetime(2001, 9, 9, tzinfo=dt.UTC)) is True
    assert word(world, "ALTA", SECOND - age * S) is False
    assert stored(world) == ("REVOKED", STOP, SECOND + 60 * S)


# `SECOND` es fijo: nuestro reloj también, o pasa de 48 h y deja de creerse
@pytest.mark.usefixtures("clock")
def test_an_alta_without_a_time_writes_nothing(world: SimpleNamespace) -> None:
    """Decisión del propietario (#937): sin fila, y sobre una baja por palabra, nada cambia."""
    assert word(world, "ALTA", None) is False
    with tenant_scope(ctx(world.a)):
        assert not ContactConsent.objects.exists()
    assert word(world, "BAJA", SECOND) is True
    assert word(world, "ALTA", None) is False
    assert stored(world) == ("REVOKED", STOP, SECOND)


def test_the_clock_is_read_with_the_contact_locked(
    world: SimpleNamespace, migrator: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    locked: list[bool] = []
    probe = "SELECT 1 FROM contacts WHERE id = %s FOR NO KEY UPDATE NOWAIT"

    def now() -> dt.datetime:
        try:  # otra conexión: si la fila del contacto está tomada, falla sin esperar
            migrator.execute(probe, [world.mine])
            locked.append(False)
        except psycopg.errors.LockNotAvailable:
            locked.append(True)
        return NOW

    monkeypatch.setattr(consents, "timezone", SimpleNamespace(now=now))
    word(world, "ALTA", SECOND + 61 * S)  # fuera del margen: solo lee el reloj
    word(world, "BAJA", SECOND)
    assert locked and all(locked), locked


@pytest.mark.usefixtures("clock")
def test_two_words_in_the_same_second_of_meta_tie_and_the_baja_wins(
    world: SimpleNamespace,
) -> None:
    assert word(world, "BAJA", SECOND + 10 * S + dt.timedelta(milliseconds=100)) is True
    assert word(world, "ALTA", SECOND + 10 * S + dt.timedelta(milliseconds=900)) is False
    assert stored(world) == ("REVOKED", STOP, SECOND + 10 * S)


@pytest.mark.parametrize("over_keyword", [False, True])
@pytest.mark.parametrize("source", OTHERS)
def test_an_alta_never_lifts_a_revocation_of_any_other_source(
    world: SimpleNamespace, migrator: psycopg.Connection[Any], source: str, over_keyword: bool
) -> None:
    at = past()  # cualquier hora de Meta, ya pasada
    if over_keyword:  # ya de baja por palabra: el otro origen igual se registra
        assert word(world, "BAJA", at) is True
    row, changed = revoke(world, source=source)
    assert (changed, row.status, row.source, row.keyword_at) == (
        True, "REVOKED", source, at if over_keyword else None,
    )  # fmt: skip
    assert word(world, "ALTA", at + 5 * S) is False  # más nueva, y aun así no la levanta
    assert word(world, "BAJA", at + 6 * S) is False  # una palabra no rebaja el origen
    assert stored(world) == ("REVOKED", source, at + 6 * S)
    assert history(world) == [("REVOKED", source)] + [("REVOKED", STOP)] * over_keyword
    done = [a[3] for a in audit(migrator) if a[0] == "consent.revoked"]
    first = [{"status": [None, "REVOKED"]}]  # la primera baja: la de la palabra o la del origen
    assert done == first + [{"status": ["REVOKED"] * 2, "source": [STOP, source]}] * over_keyword
    outbox = "SELECT count(*) FROM outbox_events WHERE event_type = 'consent.revoked'"
    assert migrator.execute(outbox).fetchone() == (1 + over_keyword,)


def contended(migrator: psycopg.Connection[Any], org: UUID, first: Any, second: Any) -> list[Any]:
    """`first` escribe y retiene su transacción; `second` tiene que esperar en la fila del contacto
    (no en otro bloqueo antes) y sigue cuando `first` confirma."""
    wrote, release, outcome = threading.Event(), threading.Event(), list[Any]([None, None])

    def work(index: int, command: Any) -> None:
        try:
            with tenant_scope(ctx(org)) as tenant:
                outcome[index] = command(tenant)
                if index == 0:
                    wrote.set()
                    release.wait(timeout=10)
        except Exception as error:  # noqa: BLE001 — lo comprueba el test
            outcome[index] = type(error).__name__
        finally:
            wrote.set()
            connection.close()

    threads = [threading.Thread(target=work, args=pair) for pair in enumerate((first, second))]
    threads[0].start()
    assert wrote.wait(timeout=10)
    threads[1].start()
    try:
        assert on_a_contact(migrator), "el segundo no esperó en la fila del contacto"
    finally:
        release.set()
        for thread in threads:
            thread.join(timeout=10)
    return outcome


def on_a_contact(migrator: psycopg.Connection[Any]) -> bool:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:  # se observa la espera, no se supone
        if migrator.execute(WAITING_ON_A_CONTACT).fetchone() != (0,):
            return True
        time.sleep(0.02)
    return False


def test_an_alta_racing_an_agent_revocation_waits_on_the_contact_and_lifts_nothing(
    world: SimpleNamespace, migrator: psycopg.Connection[Any]
) -> None:
    at = past()
    word(world, "BAJA", at)
    where = {"contact_id": world.mine, "channel": WA, "purpose": ALL}

    def agent(tenant: Any) -> bool:
        return revoke_consent(tenant, **where, source="AGENT_RECORDED")[1]

    def alta(tenant: Any) -> bool:
        return lift_keyword_stop(tenant, **where, said_at=at + 5 * S)

    assert contended(migrator, world.a, agent, alta) == [True, False]
    assert stored(world) == ("REVOKED", "AGENT_RECORDED", at + 5 * S)


STARTS = ["none", "agent_grant", "keyword", *OTHERS]  # cómo está la fila antes de las palabras


def scenario(rng: random.Random, skews: float, delays: float, untimed: float) -> dict[str, Any]:
    """Unas palabras con su hora de Meta (con empates y milisegundos), nuestro reloj desfasado
    `skew` segundos y cada una procesada con su retraso: el orden de proceso sale de ahí. Con
    probabilidad `untimed`, llega sin una hora que valga (F7b-05f, 2.ª pasada de la revisión)."""
    skew = rng.choice([0.0, rng.uniform(-skews, skews), rng.choice([0.3, -0.3, 0.9, -0.9])])
    words = [
        (rng.choice(["BAJA", "ALTA"]), rng.randint(0, 30) + rng.choice([0, 0, rng.random()]),
         rng.choice([0.0, rng.uniform(0, delays)]), rng.random() >= untimed)
        for _ in range(rng.randint(1, 4))
    ]  # fmt: skip
    return {"skew": skew, "words": words, "start": rng.choice(STARTS), "k0": rng.randint(0, 30)}


def latest(said: list[tuple[int, str]]) -> str:
    """#937: la palabra más reciente por la hora de Meta (segundos enteros); empatadas, la baja."""
    top = max(at for at, _ in said)
    return "REVOKED" if ("BAJA" in {k for at, k in said if at == top}) else "GRANTED"


def owner_rule(case: dict[str, Any]) -> str:
    """La regla del propietario. Una baja de otro origen no la levanta ninguna palabra."""
    if case["start"] in OTHERS:
        return "REVOKED"
    said = [(int(at), kind) for kind, at, _, _ in case["words"]]
    return latest(said + ([(case["k0"], "BAJA")] if case["start"] == "keyword" else []))


def with_manual(rng: random.Random, case: dict[str, Any]) -> dict[str, Any]:
    """F7b-05g: una o dos concesiones o revocaciones del agente, procesadas en un instante al azar
    entre las palabras (por nuestro reloj)."""
    servers = [at - case["skew"] + delay for _, at, delay, _ in case["words"]]
    low, high = min(servers) - 1, max(servers) + 1
    picks = [(rng.choice(["GRANT", "GRANT", "REVOKE"]), rng.uniform(low, high)) for _ in range(2)]
    return case | {"manual": picks[: rng.randint(1, 2)]}


def order(case: dict[str, Any]) -> list[tuple[dt.datetime, dt.datetime, str, bool]]:
    """Lo que se procesa, por nuestro reloj: (servidor, hora de Meta, palabra o cambio manual, si
    trae hora). Un cambio manual no tiene hora de Meta: lleva la del servidor, que no se usa."""
    skew = dt.timedelta(seconds=case["skew"])
    plays = sorted(
        [(SECOND + dt.timedelta(seconds=at) - skew + dt.timedelta(seconds=delay),
          SECOND + dt.timedelta(seconds=at), kind, timed)
         for kind, at, delay, timed in case["words"]]
        + [(SECOND + dt.timedelta(seconds=off),) * 2 + (kind, True)
           for kind, off in case.get("manual", [])]
    )  # fmt: skip
    if case["start"] == "keyword":  # una baja anterior, procesada antes que todas
        k0 = SECOND + case["k0"] * S
        plays.insert(0, (min(plays[0][0], k0 - skew), k0, "BAJA", True))
    return plays


def manual_rule(case: dict[str, Any]) -> set[str]:
    """Lo que puede quedar tras el último cambio manual: tras una revocación, `REVOKED`; tras una
    concesión, la regla sobre las palabras procesadas después (la concesión borró la hora; sin
    palabras, `GRANTED`), o sobre todas si la concesión no cambió nada (ya estaba concedido)."""
    plays = order(case)
    last = max(i for i, play in enumerate(plays) if play[2] in ("GRANT", "REVOKE"))
    if plays[last][2] == "REVOKE":
        return {"REVOKED"}
    said = [
        (i, int((at - SECOND).total_seconds()), kind)
        for i, (_, at, kind, _) in enumerate(plays) if kind in ("BAJA", "ALTA")
    ]  # fmt: skip
    after, words = [(at, k) for i, at, k in said if i > last], [(at, k) for _, at, k in said]
    return {latest(after) if after else "GRANTED", latest(words) if words else "GRANTED"}


def play(org: UUID, case: dict[str, Any], clock: list[dt.datetime]) -> tuple[str, bool]:
    """Juega el caso en un contacto nuevo. Devuelve el estado final y si todas las horas de Meta
    quedaron dentro del margen cuando se procesaron. Una palabra sin hora que valga: la BAJA se
    aplica sin `said_at`; el ALTA no cuenta (decisión del propietario en #937)."""
    with tenant_scope(ctx(org)):
        where = {"contact_id": Contact.objects.create(display_name="Caso").pk}
    where |= {"channel": WA, "purpose": ALL}
    plays = order(case)
    if case["start"] not in ("none", "keyword"):
        clock[0] = plays[0][0] - 120 * S
        with tenant_scope(ctx(org)) as tenant:
            write = grant_consent if case["start"] == "agent_grant" else revoke_consent
            write(
                tenant,
                **where,
                source=OTHERS[0] if case["start"] == "agent_grant" else case["start"],
            )
    within = True
    for server, at, kind, timed in plays:
        clock[0] = server
        if kind in ("GRANT", "REVOKE"):  # F7b-05g: el agente, con un origen que no es una palabra
            manual = grant_consent if kind == "GRANT" else revoke_consent
            run(org, manual, **where, source="AGENT_RECORDED")
            continue
        within &= (
            at.replace(microsecond=0) - server.replace(microsecond=0) <= consents.KEYWORD_AHEAD
        )
        command = keyword_stop if kind == "BAJA" else lift_keyword_stop
        said = at if timed else None
        run(org, command, contact_id=where["contact_id"], channel=WA, purpose=ALL, said_at=said)
    with tenant_scope(ctx(org)):
        row = ContactConsent.objects.filter(**where).first()  # sin fila: un alta que no contó
    return (row.status if row else "NONE"), within


@pytest.mark.parametrize(
    ("seed", "skews", "delays", "untimed"),
    [(1, 0.99, 5, 0.2), (2, 60, 30, 0.2), (3, 59, 200, 0.2), (4, 600, 30, 0.2),
     (5, 600, 200, 0.2), (6, 3600, 3600, 0.2), (7, 5, 30, 0.5), (8, 60, 60, 0.5), (9, 50, 40, 0.6)],
)  # fmt: skip
def test_any_skew_delay_and_order_keeps_the_owner_rule_or_stays_on_the_opt_out(
    world: SimpleNamespace, clock: list[dt.datetime], seed: int, skews: float, delays: float,
    untimed: float,
) -> None:  # fmt: skip
    """Dentro del margen y con todas las horas, la regla exacta del propietario; si no, nunca
    `GRANTED` si la palabra más reciente de Meta fue una baja, con hora o sin ella, salvo una baja
    sin hora con nuestro reloj más de `KEYWORD_AHEAD` detrás del de Meta (su hora real no se
    conoce; límite documentado). Semillas fijas."""
    rng, wrong = random.Random(seed), []  # noqa: S311 — casos de prueba, no secretos
    for _ in range(60):
        case = scenario(rng, skews, delays, untimed)
        want, (got, within) = owner_rule(case), play(world.a, case, clock)
        timed = all(word[3] for word in case["words"])
        exact = within and timed and got != want
        untimed_baja = any(not t and k == "BAJA" for k, _, _, t in case["words"])
        unsafe = want == "REVOKED" and got == "GRANTED" and (within or not untimed_baja)
        if exact or unsafe:
            wrong.append((case, want, got))
    assert not wrong, wrong[:3]


@pytest.mark.parametrize(
    ("seed", "skews", "delays", "untimed"),
    [(11, 0.99, 30, 0.2), (12, 60, 200, 0.2), (13, 600, 200, 0.2), (14, 3600, 3600, 0.3),
     (15, 30, 60, 0.0), (16, 600, 600, 0.0)],
)  # fmt: skip
def test_manual_changes_among_the_words_never_lose_a_later_opt_out(
    world: SimpleNamespace, clock: list[dt.datetime], seed: int, skews: float, delays: float,
    untimed: float,
) -> None:  # fmt: skip
    """F7b-05g: los mismos casos con concesiones y revocaciones del agente entre las palabras. Tras
    una revocación manual, siempre `REVOKED`; tras una concesión, nunca `GRANTED` si la palabra más
    reciente de Meta entre las procesadas después (y entre todas) fue una baja, con la misma
    excepción de la baja sin hora. Dentro del margen y con todas las horas, la regla exacta."""
    rng, wrong = random.Random(seed), []  # noqa: S311 — casos de prueba, no secretos
    for _ in range(60):
        case = with_manual(rng, scenario(rng, skews, delays, untimed))
        allowed, (got, within) = manual_rule(case), play(world.a, case, clock)
        timed = all(word[3] for word in case["words"])
        untimed_baja = any(not t and k == "BAJA" for k, _, _, t in case["words"])
        exact = within and timed and got not in allowed
        unsafe = allowed == {"REVOKED"} and got == "GRANTED" and (within or not untimed_baja)
        if exact or unsafe:
            wrong.append((case, allowed, got))
    assert not wrong, wrong[:3]

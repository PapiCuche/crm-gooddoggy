"""F11-02 (#1176; ADR-049 §4, §7, §8): la base de `apps.automations`. Tablas con FORCE RLS probadas
con SQL directo como `crm_app` en las dos direcciones, privilegios por columna, CHECK, la clave de
idempotencia, permisos de las plantillas, `set_rule` (versión, parámetros cerrados, `simulate_since`
y la semana en `SIMULATE` antes de `ON`, con el reloj de la base), pausar y reanudar, y los tres
interruptores. PostgreSQL real; claves y personas ficticias."""

import datetime as dt
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from django.db import DatabaseError, connection

from apps.access.apps import extend_owner_roles
from apps.access.catalog import BY_CODE, ROLE_TEMPLATES
from apps.access.models import MembershipRole, Role
from apps.access.selectors import AccessDenied, Denied
from apps.automations import commands
from apps.automations.commands import NotSimulatedLongEnough, StaleVersion
from apps.automations.params import CONVERSATION_UNANSWERED as CONV
from apps.automations.params import QUOTE_NO_RESPONSE as QUOTE
from apps.automations.params import defaults
from config.settings import base
from core.tenancy.scope import tenant_scope
from tests.factories import make_user
from tests.test_access import as_migrator
from tests.test_authorization import give
from tests.test_contacts_api import crm
from tests.test_memberships import ctx, join

__all__ = ["crm"]  # el fixture, para pytest
pytestmark = pytest.mark.usefixtures("tenant_db")
DB = psycopg.Connection[Any]
RULE = "INSERT INTO automation_rules (id, organization_id, key, mode, params, version, simulate_since, created_at, updated_at) VALUES (%s, %s, %s, %s, %s, 1, %s, now(), now())"  # noqa: E501
RUN = "INSERT INTO automation_runs (id, organization_id, rule_id, rule_version, rule_key, subject_type, subject_id, episode_at, step, mode, status, recipients, task_ids, attempts, depth, created_at, updated_at) VALUES (%s, %s, %s, %s, 'quote.no_response', 'quote', %s, '2026-11-01', 1, %s, %s, %s, %s, %s, %s, now(), now())"  # noqa: E501
SWITCH = (
    "INSERT INTO automation_switches (organization_id, paused, changed_at) VALUES (%s, %s, now())"  # noqa: E501
)
BAD_PARAMS: tuple[dict[str, Any], ...] = (
    {"note": "Ana Pérez"}, {"max_recipients": 4}, {"max_recipients": 0}, {"max_recipients": True},
    {"days": []}, {"days": [[1]]}, {"opens_at": "19:00"}, {"first_after_minutes": "15"},
)  # fmt: skip
AUDIT = (
    "SELECT action, entity_type, changes, metadata, actor_id FROM audit_logs"
    " WHERE action LIKE 'automation.%%' ORDER BY occurred_at, id"
)


def rule(db: Any, org: UUID, key: str = QUOTE, mode: str = "OFF", params: str = "{}") -> UUID:
    pk = uuid4()
    db.execute(RULE, [pk, org, key, mode, params, "2026-11-01" if mode == "SIMULATE" else None])
    return pk


def run(db: Any, org: UUID, rule_id: UUID, **kw: Any) -> UUID:
    pk, f = uuid4(), {"version": 1, "subject": UUID(int=1), "mode": "ON", "status": "DONE"} | kw
    values = [f.get(k, d) for k, d in (("recipients", []), ("task_ids", []), ("attempts", 0),
                                       ("depth", 0))]  # fmt: skip
    db.execute(RUN, [pk, org, rule_id, f["version"], f["subject"], f["mode"], f["status"], *values])
    return pk


def as_app(org: UUID, sql: str, params: list[Any] | None = None) -> Any:
    """`sql` como `crm_app` dentro del tenant `org`, en su propia transacción."""
    with tenant_scope(ctx(org)), connection.cursor() as cursor:
        cursor.execute(sql, params)
        return cursor.fetchall() if cursor.description else cursor.rowcount


def refused(org: UUID, sql: str, params: list[Any] | None = None) -> str:
    with pytest.raises(DatabaseError) as raised:
        as_app(org, sql, params)
    cause = raised.value.__cause__
    return str(cause.diag.constraint_name or cause.sqlstate)  # type: ignore[union-attr]


def check(migrator: DB, sql: str, params: list[Any]) -> str:
    with pytest.raises(psycopg.errors.IntegrityError) as raised:
        migrator.execute(sql, params)
    return str(raised.value.diag.constraint_name)


@pytest.fixture
def team(crm: SimpleNamespace) -> SimpleNamespace:
    """`crm` con ada, Administradora de A (plantilla)."""
    crm.ada = make_user(email="ada@example.com")
    with tenant_scope(ctx(crm.a)):
        admin = Role.objects.get(code="admin")
        MembershipRole.objects.create(membership_id=join(crm.a, crm.ada).pk, role_id=admin.pk)
    return crm


def test_rls_isolates_the_three_tables_in_both_directions_with_raw_sql(
    crm: SimpleNamespace, migrator: DB
) -> None:
    mine = {org: rule(migrator, org) for org in (crm.a, crm.b)}
    for org in (crm.a, crm.b):
        run(migrator, org, mine[org])
        migrator.execute(SWITCH, [org, False])
    stamp = {"automation_rules": "updated_at", "automation_switches": "changed_at",
             "automation_runs": "updated_at"}  # fmt: skip
    for org, other in ((crm.a, crm.b), (crm.b, crm.a)):
        for table, column in stamp.items():
            assert as_app(org, f"SELECT DISTINCT organization_id FROM {table}") == [(org,)]  # noqa: S608
            touch = f"UPDATE {table} SET {column} = now() WHERE organization_id = %s"  # noqa: S608
            assert as_app(org, touch, [other]) == 0
        assert (
            refused(org, RULE, [uuid4(), other, CONV, "OFF", "{}", None]) == "42501"
        )  # WITH CHECK
        assert refused(org, SWITCH, [other, True]) == "42501"
        # la ejecución de una organización no apunta a la regla de otra (FK compuesta)
        args = [uuid4(), org, mine[other], 1, UUID(int=2), "ON", "DONE", [], [], 0, 0]
        assert refused(org, RUN, args) == "automation_runs_rule_fk"


def test_crm_app_never_deletes_truncates_or_rewrites_keys_and_identity(
    crm: SimpleNamespace, migrator: DB
) -> None:
    pk = rule(migrator, crm.a)
    run(migrator, crm.a, pk)
    migrator.execute(SWITCH, [crm.a, False])
    for table in ("automation_rules", "automation_switches", "automation_runs"):
        assert refused(crm.a, f"DELETE FROM {table}") == "42501"  # noqa: S608
        assert refused(crm.a, f"TRUNCATE {table}") == "42501"
        assert refused(crm.a, f"UPDATE {table} SET organization_id = organization_id") == "42501"  # noqa: S608
    for column in ("key", "id", "created_at"):
        assert refused(crm.a, f"UPDATE automation_rules SET {column} = {column}") == "42501"  # noqa: S608
    for column in ("rule_id", "subject_id", "episode_at", "step", "mode", "depth", "rule_key"):
        assert refused(crm.a, f"UPDATE automation_runs SET {column} = {column}") == "42501"  # noqa: S608
    assert as_app(crm.a, "UPDATE automation_runs SET attempts = 1, status = 'FAILED'") == 1


@pytest.mark.parametrize(("params", "ok"), [
    ('{"max_recipients": 0}', False), ('{"max_recipients": 4}', False),
    ('{"max_recipients": "3"}', False), ('{"max_recipients": 2.5}', False),
    ('{"max_recipients": 1}', True), ('{"max_recipients": 3}', True), ("{}", True),
])  # fmt: skip
def test_max_recipients_stays_between_one_and_three_in_the_database(
    crm: SimpleNamespace, migrator: DB, params: str, ok: bool
) -> None:
    sql, args = RULE, [uuid4(), crm.a, CONV, "OFF", params, None]
    if ok:
        migrator.execute(sql, args)
    else:
        assert check(migrator, sql, args) == "automation_rules_max_recipients_ck"


def test_checks_guard_modes_simulation_caps_and_the_idempotency_key(
    crm: SimpleNamespace, migrator: DB
) -> None:
    a = crm.a
    since = [uuid4(), a, CONV, "SIMULATE", "{}", None]
    assert check(migrator, RULE, since) == "automation_rules_simulate_since_ck"
    stuck = RULE.replace("%s, now(), now())", "now(), now(), now())")
    assert (
        check(migrator, stuck, [uuid4(), a, CONV, "ON", "{}"])
        == "automation_rules_simulate_since_ck"
    )  # noqa: E501
    assert (
        check(migrator, RULE, [uuid4(), a, "custom", "OFF", "{}", None])
        == "automation_rules_key_ck"
    )
    assert (
        check(migrator, RULE, [uuid4(), a, CONV, "OFF", "[]", None]) == "automation_rules_params_ck"
    )
    pk, four = rule(migrator, a), [uuid4() for _ in range(4)]
    for kw, name in (({"mode": "SIMULATE", "task_ids": [uuid4()]}, "simulated"),
                     ({"recipients": four}, "recipients"), ({"task_ids": four}, "task_ids"),
                     ({"depth": 1}, "depth"), ({"status": "DEAD", "attempts": 2}, "dead"),
                     ({"attempts": 4}, "attempts"), ({"mode": "OFF"}, "mode"),
                     ({"status": "CLOSED"}, "closed")):  # fmt: skip
        with pytest.raises(psycopg.errors.IntegrityError) as raised:
            run(migrator, a, pk, **kw)
        assert raised.value.diag.constraint_name == f"automation_runs_{name}_ck", kw
    run(
        migrator, a, pk, mode="SIMULATE", recipients=[uuid4()] * 3, subject=UUID(int=3)
    )  # simular calcula, sin tareas
    run(migrator, a, pk)
    with pytest.raises(psycopg.errors.UniqueViolation):  # otra versión de la regla: misma clave
        run(migrator, a, pk, version=2)
    run(migrator, a, pk, mode="SIMULATE")  # pasar de SIMULATE a ON sí crea la tarea real
    with pytest.raises(psycopg.errors.ForeignKeyViolation) as fk:  # `rule_key`, el de su regla
        run(migrator, a, rule(migrator, a, CONV))
    assert fk.value.diag.constraint_name == "automation_runs_rule_key_fk"
    bare = (  # lo que dará el barrido en SQL (F11-03): el resto, por omisión en la base
        "INSERT INTO automation_runs (organization_id, rule_id, rule_version, rule_key,"
        " subject_type, subject_id, episode_at, step, mode, status) VALUES (%s, %s, 1,"
        " 'quote.no_response', 'quote', %s, '2026-11-02', 1, 'ON', 'DONE') ON CONFLICT DO NOTHING"
    )
    episode = [a, pk, UUID(int=9)]
    assert as_app(a, bare, episode) == 1
    assert as_app(a, bare, episode) == 0  # idempotente


def test_templates_and_denials_owner_admin_manage_supervisor_reads_and_pauses(
    team: SimpleNamespace,
) -> None:
    codes = ("automations.view", "automations.manage", "automations.kill_switch")
    born = {t.code: {c for c in codes if c in t.grants} for t in ROLE_TEMPLATES}
    assert born == {"owner": set(codes), "admin": set(codes), "seller": set(),
                    "supervisor": {codes[0], codes[2]}}  # fmt: skip
    assert [BY_CODE[c].is_sensitive for c in codes] == [False, False, True]
    a = team.a
    for who in (team.vera, team.sam, team.bea):  # vendedora, supervisor y sin rol: no cambian
        for call in (lambda c: commands.set_rule(c, key=CONV, version=0, mode="SIMULATE"),
                     commands.resume):  # fmt: skip
            with tenant_scope(ctx(a, who)) as scope, pytest.raises(AccessDenied) as raised:
                call(scope)
            assert raised.value.reason is Denied.PERMISSION
    for who in (team.vera, team.bea):
        with tenant_scope(ctx(a, who)) as scope, pytest.raises(AccessDenied):
            commands.pause(scope)
    with tenant_scope(ctx(a, team.luis)) as scope, pytest.raises(AccessDenied) as raised:
        commands.pause(scope)  # de otra organización: no es miembro de A
    assert raised.value.reason is Denied.MEMBERSHIP
    with tenant_scope(ctx(a, team.sam)) as scope:
        assert commands.pause(scope) is True  # el Supervisor pausa
    with tenant_scope(ctx(a, team.ada)) as scope:
        assert commands.resume(scope) is False  # la Administradora reanuda y configura
        assert commands.set_rule(scope, key=CONV, version=0, mode="SIMULATE").version == 1
    give(a, team.m["bea"], {"automations.manage": None})  # `manage` solo también pausa
    with tenant_scope(ctx(a, team.bea)) as scope:
        assert commands.pause(scope) is True


def test_existing_organizations_get_them_only_on_the_owner_role(
    crm: SimpleNamespace, migrator: DB
) -> None:
    """ADR-018 §3: el despliegue no se los da al Administrador ni al Supervisor que ya existen."""
    held = (
        "SELECT DISTINCT r.code FROM role_permissions g JOIN roles r ON r.id = g.role_id"
        " WHERE g.permission_code LIKE 'automations.%%' AND r.organization_id = %s ORDER BY 1"
    )
    assert [c for (c,) in migrator.execute(held, [crm.a]).fetchall()] == [
        "admin",
        "owner",
        "supervisor",
    ]
    migrator.execute("DELETE FROM role_permissions WHERE permission_code LIKE 'automations.%'")
    assert as_migrator(lambda: extend_owner_roles("default")) == 3
    assert migrator.execute(held, [crm.a]).fetchall() == [("owner",)]


def test_set_rule_versions_validates_closed_params_and_audits_only_codes_and_numbers(
    crm: SimpleNamespace, migrator: DB
) -> None:
    with tenant_scope(ctx(crm.a, crm.ana)) as scope:
        assert commands.runnable(scope) == []  # sin filas: todo en OFF
        untouched = commands.set_rule(scope, key=CONV, version=0, mode="OFF")  # nada que cambiar
        assert (untouched.version, untouched.mode, untouched.params) == (0, "OFF", defaults(CONV))
        for bad in BAD_PARAMS:
            with pytest.raises(ValueError, match=r"^(params|max_recipients|days|closes_at|first)"):
                commands.set_rule(scope, key=CONV, version=0, params=bad)
        with pytest.raises(ValueError, match="key"):
            commands.set_rule(scope, key="custom", version=0)
        made = commands.set_rule(scope, key=CONV, version=0, params={"max_recipients": 2,
                                 "days": [5, 1, 3], "opens_at": "08:30"})  # fmt: skip
        assert made.version == 1 and made.mode == "OFF" and made.simulate_since is None
        for stale in (0, 2):
            with pytest.raises(StaleVersion):
                commands.set_rule(scope, key=CONV, version=stale, mode="SIMULATE")
        assert (
            commands.set_rule(scope, key=CONV, version=1, params={"days": [1, 3, 5]}).version == 1
        )
        moved = commands.set_rule(scope, key=CONV, version=1, mode="SIMULATE")
    assert moved.version == 2 and moved.simulate_since is not None
    stored = migrator.execute("SELECT params FROM automation_rules").fetchall()
    assert stored == [
        (defaults(CONV) | {"max_recipients": 2, "days": [1, 3, 5], "opens_at": "08:30"},)
    ]
    assert migrator.execute(AUDIT).fetchall() == [
        ("automation.rule.updated", "automation_rule",
         {"params.days": [[1, 2, 3, 4, 5, 6], [1, 3, 5]], "params.opens_at": ["09:00", "08:30"],
          "params.max_recipients": [3, 2]}, {"key": CONV, "version": 1}, crm.ana.pk),
        ("automation.rule.updated", "automation_rule", {"mode": ["OFF", "SIMULATE"]},
         {"key": CONV, "version": 2}, crm.ana.pk),
    ]  # fmt: skip


def test_on_needs_seven_days_in_simulate_by_the_database_clock(
    crm: SimpleNamespace, migrator: DB, monkeypatch: pytest.MonkeyPatch
) -> None:
    def at(when: Any) -> None:
        monkeypatch.setattr(commands, "_db_now", lambda alias: when)

    with tenant_scope(ctx(crm.a, crm.ana)) as scope:
        with pytest.raises(NotSimulatedLongEnough):  # de OFF a ON, nunca
            commands.set_rule(scope, key=QUOTE, version=0, mode="ON")
        since = commands.set_rule(scope, key=QUOTE, version=0, mode="SIMULATE").simulate_since
        assert since is not None
        at(since + dt.timedelta(days=7) - dt.timedelta(seconds=1))  # 6 d 23 h 59 min 59 s
        with pytest.raises(NotSimulatedLongEnough):
            commands.set_rule(scope, key=QUOTE, version=1, mode="ON")
        at(since + dt.timedelta(days=1))  # cambiar parámetros en SIMULATE no reinicia la cuenta
        assert (
            commands.set_rule(
                scope, key=QUOTE, version=1, params={"after_hours": 24}
            ).simulate_since
            == since
        )  # noqa: E501
        at(since + dt.timedelta(days=2))  # salir de SIMULATE la borra; volver la reinicia
        assert commands.set_rule(scope, key=QUOTE, version=2, mode="OFF").simulate_since is None
        again = commands.set_rule(scope, key=QUOTE, version=3, mode="SIMULATE").simulate_since
        assert again == since + dt.timedelta(days=2)
        at(since + dt.timedelta(days=7))
        with pytest.raises(NotSimulatedLongEnough):
            commands.set_rule(scope, key=QUOTE, version=4, mode="ON")
        at(again + dt.timedelta(days=7))  # justo 7 días
        on = commands.set_rule(scope, key=QUOTE, version=4, mode="ON")
        assert (on.mode, on.simulate_since) == ("ON", None)
        assert (
            commands.set_rule(scope, key=QUOTE, version=5, params={"after_hours": 30}).mode == "ON"
        )
        monkeypatch.undo()
        assert [r.key for r in commands.runnable(scope)] == [QUOTE]


def test_db_clock_is_the_one_used(crm: SimpleNamespace, migrator: DB) -> None:
    """Sin parches: `simulate_since` es el `now()` de la transacción de la base."""
    with tenant_scope(ctx(crm.a, crm.ana)) as scope:
        made = commands.set_rule(scope, key=QUOTE, version=0, mode="SIMULATE")
    (db_since,) = migrator.execute("SELECT simulate_since FROM automation_rules").fetchone()  # type: ignore[misc]
    assert made.simulate_since == db_since
    migrator.execute("UPDATE automation_rules SET simulate_since = now() - interval '7 days'")
    with tenant_scope(ctx(crm.a, crm.ana)) as scope:
        assert commands.set_rule(scope, key=QUOTE, version=1, mode="ON").mode == "ON"


def test_pause_resume_and_the_three_switches(
    crm: SimpleNamespace, migrator: DB, settings: Any
) -> None:
    rule(migrator, crm.a, CONV, "SIMULATE")
    rule(migrator, crm.a, QUOTE, "OFF")
    rule(migrator, crm.b, QUOTE, "SIMULATE")
    with tenant_scope(ctx(crm.a, crm.ana)) as scope:
        assert commands.resume(scope) is False  # sin fila no está pausada: no escribe
        assert [r.key for r in commands.runnable(scope)] == [CONV]  # ni OFF ni la regla de B
        for value in (False, None, "true", 1):  # solo `True` enciende
            settings.AUTOMATIONS_ENABLED = value
            assert commands.runnable(scope) == []
        settings.AUTOMATIONS_ENABLED = True
    for _ in range(2):  # repetir la pausa no escribe ni audita
        with tenant_scope(ctx(crm.a, crm.sam)) as scope:
            assert commands.pause(scope) is True
    with tenant_scope(ctx(crm.a, crm.ana)) as scope:
        assert commands.runnable(scope) == []
    with tenant_scope(ctx(crm.b)) as other:
        assert [r.key for r in commands.runnable(other)] == [QUOTE]  # pausar A no pausa B
    with tenant_scope(ctx(crm.a, crm.ana)) as scope:
        assert commands.resume(scope) is False
        assert [r.key for r in commands.runnable(scope)] == [CONV]
    switch = "SELECT organization_id, paused, changed_by_user_id FROM automation_switches"
    assert migrator.execute(switch).fetchall() == [(crm.a, False, crm.ana.pk)]
    assert [row[0::2] for row in migrator.execute(AUDIT).fetchall()] == [
        ("automation.paused", {"paused": [False, True]}, crm.sam.pk),
        ("automation.resumed", {"paused": [True, False]}, crm.ana.pk),
    ]


def test_two_first_writes_at_once_leave_one_rule_and_one_stale_version(
    crm: SimpleNamespace, migrator: DB, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate, real, results = threading.Barrier(2, timeout=10), commands._db_now, []

    def both_read_no_row(alias: str) -> dt.datetime:
        gate.wait()  # las dos transacciones vieron «sin fila» antes de crearla
        return real(alias)

    monkeypatch.setattr(commands, "_db_now", both_read_no_row)

    def write(mode: str) -> None:
        try:
            with tenant_scope(ctx(crm.a, crm.ana)) as scope:
                results.append(commands.set_rule(scope, key=CONV, version=0, mode=mode).mode)
        except StaleVersion:
            results.append("stale")
        finally:
            connection.close()

    threads = [threading.Thread(target=write, args=(m,)) for m in ("SIMULATE", "SIMULATE")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(15)
    assert "stale" in results and len(results) == 2
    assert migrator.execute("SELECT count(*) FROM automation_rules").fetchone() == (1,)


def test_global_switch_is_off_outside_tests_and_documented_as_an_owner_step() -> None:
    assert base.AUTOMATIONS_ENABLED is False  # el entorno de los tests no lo define
    settings_dir = Path(base.__file__).parent
    assert "AUTOMATIONS_ENABLED" not in (settings_dir / "production.py").read_text()
    infra = settings_dir.parents[2] / "infra"
    assert "AUTOMATIONS_ENABLED=false\n" in (infra / "env/.env.example").read_text()
    assert "AUTOMATIONS_ENABLED=\n" in (infra / "prod/.env.production.example").read_text()

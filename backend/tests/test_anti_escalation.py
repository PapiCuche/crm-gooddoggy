"""F2-05C: cambios de RBAC sin escalada, y siempre un Owner activo (ADR-003 §5).

PO-1: quitar un rol exige cubrir todas sus concesiones, igual que asignarlo. PO-2: conceder un
permiso a un rol que el actor tiene asignado cuenta como modificar sus propios roles.
"""

import json
import re
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from django.core.exceptions import ObjectDoesNotExist
from django.db import OperationalError, connection

from apps.access import services
from apps.access.catalog import BY_CODE, PermissionDef, Scope
from apps.access.models import MembershipRole, Role, RolePermission
from apps.access.selectors import AccessDenied, Denied, UnknownPermission
from apps.access.services import (
    assign_role,
    clone_role_templates,
    covers,
    ensure_owner_remains,
    grant_permission,
    remove_role,
)
from apps.accounts.models import User
from apps.organizations.models import OrganizationMembership
from core.tenancy.context import TenantContext, TenantContextError
from core.tenancy.scope import tenant_scope
from tests.factories import TEST_PASSWORD, make_user
from tests.test_access import NEW_PERMISSION
from tests.test_authorization import VIEW, give, world  # noqa: F401 — `world` es una fixture
from tests.test_memberships import ctx, join

pytestmark = pytest.mark.usefixtures("tenant_db")
EDIT = "widgets.manage"  # segundo permiso de prueba con alcance
DELEGATE = {"roles.manage": None, "users.manage": None, VIEW: "TEAM"}  # sin ser Owner
ADMIN_REST = ("organization.view", "users.view", "users.invite", "roles.view")
STATE = (
    "SELECT (SELECT count(*) FROM role_permissions), (SELECT count(*) FROM membership_roles), "
    "(SELECT count(*) FROM audit_logs)"
)
AUDIT = (
    "SELECT action, entity_type, entity_id, changes::text, metadata::text, actor_id "
    "FROM audit_logs ORDER BY occurred_at, id"
)


@pytest.fixture
def rbac(
    request: pytest.FixtureRequest,
    migrator: psycopg.Connection[Any],
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Any]:
    """Organización A con sus plantillas: ana es Owner; luis delega sin serlo; eva no tiene rol."""
    base = request.getfixturevalue("world")

    def drop() -> None:
        migrator.execute("DELETE FROM role_permissions WHERE permission_code = %s", [EDIT])
        migrator.execute("DELETE FROM permissions WHERE code = %s", [EDIT])

    drop()
    migrator.execute(NEW_PERMISSION, [EDIT, True])
    monkeypatch.setitem(BY_CODE, EDIT, PermissionDef(EDIT, "x", supports_scope=True))
    with tenant_scope(ctx(base.a)) as scope:
        roles = {role.code: role for role in clone_role_templates(scope)}
        MembershipRole.objects.create(membership_id=base.membership, role=roles["owner"])
    luis, eva = make_user(email="luis@example.com"), make_user(email="eva@example.com")
    m_luis, m_eva = join(base.a, luis).pk, join(base.a, eva).pk
    delegator = give(base.a, m_luis, DELEGATE, code="delegator")
    with tenant_scope(ctx(base.a)):
        target = Role.objects.create(code="target", name="Rol destino")
    extra = SimpleNamespace(luis=luis, eva=eva, m_luis=m_luis, m_eva=m_eva, target=target)
    try:
        yield SimpleNamespace(**vars(base), **vars(extra), roles=roles, delegator=delegator)
    finally:
        drop()


@contextmanager
def acting(org: UUID, user: User) -> Iterator[TenantContext]:
    with tenant_scope(ctx(org, user)) as tenant:
        yield tenant


def denied(org: UUID, user: User, service: Any, **kwargs: Any) -> Denied:
    with pytest.raises(AccessDenied) as error, acting(org, user) as tenant:
        service(tenant, **kwargs)
    return error.value.reason


def state(migrator: psycopg.Connection[Any]) -> Any:
    return migrator.execute(STATE).fetchone()


def audit(migrator: psycopg.Connection[Any]) -> list[tuple[Any, ...]]:
    rows = migrator.execute(AUDIT).fetchall()
    return [
        (row[0], row[1], row[2], json.loads(row[3]), json.loads(row[4]), row[5]) for row in rows
    ]


def race(org: UUID, changes: dict[str, tuple[User, Any]], hold: str | None) -> dict[str, Any]:
    """Cada cambio en su hilo, con su conexión y su transacción, contra PostgreSQL real.

    `hold`: ese hilo escribe y retiene su transacción; el otro debe quedarse esperando el
    bloqueo. `None`: todos lanzan su cambio a la vez, y gana cualquiera.
    """
    wrote, release, outcome = threading.Event(), threading.Event(), dict[str, Any]()
    together = threading.Barrier(len(changes))

    def run(name: str, actor: User, change: Any) -> None:
        try:
            with tenant_scope(ctx(org, actor)) as tenant:
                if hold is None:
                    together.wait(timeout=10)
                change(tenant)
                if name == hold:  # ya escribió y aún no ha confirmado: conserva el bloqueo
                    wrote.set()
                    release.wait(timeout=10)
            outcome[name] = "ok"
        except AccessDenied as error:
            outcome[name] = error.reason
        finally:
            wrote.set()
            connection.close()

    threads = {name: threading.Thread(target=run, args=(name, *by)) for name, by in changes.items()}
    waiting = [thread for name, thread in threads.items() if name != hold]
    if hold is not None:
        threads[hold].start()
        assert wrote.wait(timeout=10)
    for thread in waiting:
        thread.start()
    if hold is not None:
        waiting[0].join(timeout=1)
        assert waiting[0].is_alive() and not outcome  # espera el bloqueo del rol Owner
        release.set()
    for thread in threads.values():
        thread.join(timeout=10)
    return outcome


def test_covers_is_a_partial_order() -> None:
    own, team, branch, org = Scope.OWN, Scope.TEAM, Scope.BRANCH, Scope.ORGANIZATION
    assert all(covers({held}, own) for held in (own, team, branch, org))
    assert covers({team}, team) and covers({org}, team) and not covers({branch}, team)
    assert covers({branch}, branch) and covers({org}, branch) and not covers({team}, branch)
    assert covers({org}, org) and not covers({team, branch}, org)  # no suman ORGANIZATION
    assert covers({None}, None) and not covers({org}, None) and not covers({None}, own)
    assert not covers(frozenset(), own) and not covers(frozenset(), None)


def test_services_need_the_manage_permission_and_the_active_scope(
    rbac: Any, migrator: psycopg.Connection[Any]
) -> None:
    before = state(migrator)
    grant = {"role_id": rbac.target.pk, "code": "organization.view", "scope": None}
    link = {"membership_id": rbac.m_luis, "role_id": rbac.roles["seller"].pk}
    calls = ((grant_permission, grant), (assign_role, link), (remove_role, link))
    staff = User.objects.create_superuser("ops@example.com", TEST_PASSWORD)
    for service, kwargs in calls:
        assert denied(rbac.a, rbac.eva, service, **kwargs) is Denied.PERMISSION  # sin roles
        assert denied(rbac.a, staff, service, **kwargs) is Denied.MEMBERSHIP  # staff: sin bypass
        assert denied(rbac.b, rbac.ana, service, **kwargs) is Denied.MEMBERSHIP  # Owner de A, en B
        with pytest.raises(TenantContextError):
            service(ctx(rbac.a, rbac.ana), **kwargs)  # fuera de su tenant_scope
    join(rbac.a, staff)  # con membresía y sin concesiones: tampoco
    assert denied(rbac.a, staff, assign_role, **link) is Denied.PERMISSION
    assert state(migrator) == before


def test_grant_needs_the_permission_and_a_scope_not_wider(
    rbac: Any, migrator: psycopg.Connection[Any]
) -> None:
    before = state(migrator)
    for code, scope in ((VIEW, "ORGANIZATION"), (VIEW, "BRANCH"), ("users.view", None)):
        kwargs = {"role_id": rbac.target.pk, "code": code, "scope": scope}  # luis tiene VIEW: TEAM
        assert denied(rbac.a, rbac.luis, grant_permission, **kwargs) is Denied.ESCALATION
    assert state(migrator) == before  # denegar no escribe nada
    with acting(rbac.a, rbac.luis) as tenant:
        grant_permission(tenant, role_id=rbac.target.pk, code=VIEW, scope="TEAM")  # igual
        grant_permission(tenant, role_id=rbac.target.pk, code=VIEW, scope="TEAM")  # repetir: nada
        grant_permission(tenant, role_id=rbac.roles["seller"].pk, code=VIEW, scope="OWN")  # menor
        with pytest.raises(ValueError, match="no corresponde"):
            grant_permission(tenant, role_id=rbac.target.pk, code=VIEW, scope=None)
        with pytest.raises(UnknownPermission):
            grant_permission(tenant, role_id=rbac.target.pk, code="users.mange", scope=None)
        assert set(rbac.target.grants.values_list("permission_id", "scope")) == {(VIEW, "TEAM")}
    assert state(migrator) == (before[0] + 2, before[1], before[2] + 2)
    changes = {"permission": [None, VIEW], "scope": [None, "TEAM"]}
    first: tuple[Any, ...] = ("role.permission_granted", "role", rbac.target.pk, changes, {})
    first += (rbac.luis.pk,)
    assert audit(migrator)[0] == first and audit(migrator)[1][3]["scope"] == [None, "OWN"]


def test_a_scope_changes_only_for_who_covers_both_and_never_on_the_owner_role(
    rbac: Any, migrator: psycopg.Connection[Any]
) -> None:
    target, owner = rbac.target.pk, rbac.roles["owner"].pk
    wide = give(rbac.a, rbac.m_eva, {VIEW: "ORGANIZATION"}).pk  # luis lo tiene con TEAM
    with acting(rbac.a, rbac.luis) as tenant:
        grant_permission(tenant, role_id=target, code=VIEW, scope="TEAM")
    before = state(migrator)
    for role, scope in ((wide, "TEAM"), (wide, "OWN"), (target, "ORGANIZATION")):
        kwargs = {"role_id": role, "code": VIEW, "scope": scope}  # no cubre el anterior, o el nuevo
        assert denied(rbac.a, rbac.luis, grant_permission, **kwargs) is Denied.ESCALATION
    for code, scope in ((VIEW, "TEAM"), (VIEW, "OWN")):  # lo cubre: decide que es el rol Owner
        kwargs = {"role_id": owner, "code": code, "scope": scope}
        assert denied(rbac.a, rbac.luis, grant_permission, **kwargs) is Denied.OWNER_ROLE
    assert state(migrator) == before
    with acting(rbac.a, rbac.luis) as tenant:
        grant_permission(tenant, role_id=target, code=VIEW, scope="OWN")  # reduce: cubre los dos
        grant_permission(tenant, role_id=target, code=VIEW, scope="OWN")  # repetir: nada
        assert set(rbac.target.grants.values_list("permission_id", "scope")) == {(VIEW, "OWN")}
        grant_permission(tenant, role_id=target, code=VIEW, scope="TEAM")  # y lo devuelve
    assert state(migrator) == (before[0], before[1], before[2] + 2)  # la misma fila, otro alcance
    changed = {"permission": [VIEW, VIEW], "scope": ["TEAM", "OWN"]}
    row: tuple[Any, ...] = ("role.permission_scope_changed", "role", target, changed, {})
    assert audit(migrator)[-2] == (*row, rbac.luis.pk)
    assert audit(migrator)[-1][3]["scope"] == ["OWN", "TEAM"]


def test_sensitive_permissions_only_by_an_owner_and_the_flag_alone_grants_nothing(
    rbac: Any, migrator: psycopg.Connection[Any]
) -> None:
    sensitive = {"role_id": rbac.target.pk, "code": "users.manage", "scope": None}
    assert denied(rbac.a, rbac.luis, grant_permission, **sensitive) is Denied.SENSITIVE  # lo tiene
    give(rbac.a, rbac.m_luis, {"audit.view": None}, code="owner2")  # un rol que solo se llama así
    with tenant_scope(ctx(rbac.a)):
        Role.objects.filter(code="owner2").update(code="master", name="Owner")
    assert denied(rbac.a, rbac.luis, grant_permission, **sensitive) is Denied.SENSITIVE
    with tenant_scope(ctx(rbac.a)):  # el Owner real, con otro código y otro nombre
        Role.objects.filter(pk=rbac.roles["owner"].pk).update(code="fundador", name="Otro")
    with acting(rbac.a, rbac.ana) as tenant:
        grant_permission(tenant, **sensitive)
    assert audit(migrator)[-1][3] == {"permission": [None, "users.manage"], "scope": [None, None]}
    before = state(migrator)
    with tenant_scope(ctx(rbac.a)):  # la marca de Owner, sin la concesión: no puede delegarla
        RolePermission.objects.filter(role=rbac.roles["owner"], permission_id="audit.view").delete()
    audit_view = {"role_id": rbac.target.pk, "code": "audit.view", "scope": None}
    assert denied(rbac.a, rbac.ana, grant_permission, **audit_view) is Denied.ESCALATION
    assert state(migrator) == (before[0] - 1, before[1], before[2])


def test_nobody_modifies_their_own_roles(rbac: Any, migrator: psycopg.Connection[Any]) -> None:
    before = state(migrator)
    seller = rbac.roles["seller"].pk
    for user, membership in ((rbac.ana, rbac.membership), (rbac.luis, rbac.m_luis)):
        own = {"membership_id": membership, "role_id": seller}
        assert denied(rbac.a, user, assign_role, **own) is Denied.SELF
        assert denied(rbac.a, user, remove_role, **own) is Denied.SELF
    for own_id in (str(rbac.m_luis), str(rbac.m_luis).upper(), rbac.m_luis.hex):  # como texto
        own = {"membership_id": own_id, "role_id": rbac.target.pk}
        assert denied(rbac.a, rbac.luis, assign_role, **own) is Denied.SELF
        assert denied(rbac.a, rbac.luis, remove_role, **own) is Denied.SELF
    for user, role in ((rbac.ana, rbac.roles["owner"]), (rbac.luis, rbac.delegator)):
        own_role = {"role_id": role.pk, "code": "organization.view", "scope": None}
        assert denied(rbac.a, user, grant_permission, **own_role) is Denied.SELF  # PO-2
    assert state(migrator) == before


def test_assign_requires_covering_every_grant_of_the_role(
    rbac: Any, migrator: psycopg.Connection[Any]
) -> None:
    give(rbac.a, rbac.m_luis, dict.fromkeys(ADMIN_REST))  # luis cubre todo lo del rol admin
    roles, before = rbac.roles, state(migrator)
    to_eva = {"membership_id": rbac.m_eva}
    for code, reason in (("admin", Denied.SENSITIVE), ("owner", Denied.ESCALATION)):
        kwargs = {**to_eva, "role_id": roles[code].pk}  # admin lleva users.manage; luis no es Owner
        assert denied(rbac.a, rbac.luis, assign_role, **kwargs) is reason
    for foreign in (
        {**to_eva, "role_id": uuid4()},
        {"membership_id": uuid4(), "role_id": roles["seller"].pk},
    ):
        with pytest.raises(ObjectDoesNotExist), acting(rbac.a, rbac.ana) as tenant:
            assign_role(tenant, **foreign)  # de otra organización o inexistente: no existe
    assert state(migrator) == before
    with acting(rbac.a, rbac.luis) as tenant:
        assign_role(tenant, **to_eva, role_id=roles["supervisor"].pk)  # lo cubre y no es sensible
        assign_role(tenant, **to_eva, role_id=roles["supervisor"].pk)  # repetir: nada
    with acting(rbac.a, rbac.ana) as tenant:
        assign_role(tenant, **to_eva, role_id=roles["admin"].pk)
        assign_role(tenant, membership_id=rbac.m_luis, role_id=roles["owner"].pk)  # otro Owner
    assert state(migrator) == (before[0], before[1] + 3, before[2] + 3)
    role = {"role": [None, str(roles["supervisor"].pk)]}
    first: tuple[Any, ...] = ("membership.role_assigned", "membership", rbac.m_eva, role)
    assert audit(migrator)[0] == (*first, {"role": "supervisor"}, rbac.luis.pk)


def test_remove_requires_covering_every_grant_too(
    rbac: Any, migrator: psycopg.Connection[Any]
) -> None:
    give(rbac.a, rbac.m_luis, dict.fromkeys(ADMIN_REST))
    roles = rbac.roles
    with tenant_scope(ctx(rbac.a)):
        for code in ("seller", "admin"):
            MembershipRole.objects.create(membership_id=rbac.m_eva, role=roles[code])
    before = state(migrator)
    from_eva = {"membership_id": rbac.m_eva}
    assert denied(rbac.a, rbac.luis, remove_role, **from_eva, role_id=roles["admin"].pk) is (
        Denied.SENSITIVE  # PO-1: tampoco puede quitar lo que no podría asignar
    )
    for missing in (
        {**from_eva, "role_id": roles["supervisor"].pk},
        {**from_eva, "role_id": uuid4()},
    ):
        with pytest.raises(ObjectDoesNotExist), acting(rbac.a, rbac.ana) as tenant:
            remove_role(tenant, **missing)  # no lo tiene asignado
    # Cada concesión, con alcance igual o mayor: aquí VIEW sobra y EDIT se queda corto.
    give(rbac.a, rbac.m_luis, {VIEW: "ORGANIZATION", EDIT: "BRANCH"})
    wide = give(rbac.a, rbac.m_eva, {VIEW: "OWN", EDIT: "ORGANIZATION"}, code="wide")
    before = state(migrator)
    assert denied(rbac.a, rbac.luis, remove_role, **from_eva, role_id=wide.pk) is Denied.ESCALATION
    assert state(migrator) == before
    with acting(rbac.a, rbac.luis) as tenant:
        remove_role(tenant, **from_eva, role_id=roles["seller"].pk)
    with acting(rbac.a, rbac.ana) as tenant:
        remove_role(tenant, **from_eva, role_id=roles["admin"].pk)
    assert state(migrator) == (before[0], before[1] - 2, before[2] + 2)
    last = audit(migrator)[-1]
    assert last[0] == "membership.role_removed" and last[3] == {
        "role": [str(roles["admin"].pk), None]
    }


def test_foreign_ids_never_exist_and_write_nothing(
    rbac: Any, migrator: psycopg.Connection[Any]
) -> None:
    carla = make_user(email="carla@example.com")
    m_carla = join(rbac.b, carla).pk
    role_b = give(rbac.b, m_carla, {"organization.view": None}, code="de-b")
    before = state(migrator)
    attempts: tuple[tuple[Any, dict[str, Any]], ...] = (
        (grant_permission, {"role_id": role_b.pk, "code": "organization.view", "scope": None}),
        (assign_role, {"membership_id": rbac.m_eva, "role_id": role_b.pk}),
        (assign_role, {"membership_id": m_carla, "role_id": rbac.roles["seller"].pk}),
        (remove_role, {"membership_id": m_carla, "role_id": role_b.pk}),
    )
    with acting(rbac.a, rbac.ana) as tenant:
        for service, kwargs in attempts:
            with pytest.raises(ObjectDoesNotExist):
                service(tenant, **kwargs)
        assign_role(tenant, membership_id=rbac.m_eva, role_id=rbac.roles["seller"].pk)  # sigue útil
    assert state(migrator) == (before[0], before[1] + 1, before[2] + 1)


def test_last_active_owner_always_remains(rbac: Any, migrator: psycopg.Connection[Any]) -> None:
    owner, sole = rbac.roles["owner"], {"without_membership_id": rbac.membership}
    assert denied(rbac.a, rbac.ana, ensure_owner_remains, **sole) is Denied.LAST_OWNER
    with tenant_scope(ctx(rbac.a)):
        MembershipRole.objects.create(membership_id=rbac.m_luis, role=owner)  # segundo Owner
    inactive = (("SUSPENDED", True), ("INVITED", True), ("DEACTIVATED", True), ("ACTIVE", False))
    for status, active in inactive:  # un segundo Owner que no cuenta como activo
        with tenant_scope(ctx(rbac.a)):
            OrganizationMembership.objects.filter(pk=rbac.m_luis).update(status=status)
        User.objects.filter(pk=rbac.luis.pk).update(is_active=active)
        assert denied(rbac.a, rbac.ana, ensure_owner_remains, **sole) is Denied.LAST_OWNER
    User.objects.filter(pk=rbac.luis.pk).update(is_active=True)
    with acting(rbac.a, rbac.ana) as tenant:
        ensure_owner_remains(tenant, **sole)  # con otro Owner activo, sí
        remove_role(tenant, membership_id=rbac.m_luis, role_id=owner.pk)  # dos Owners: se quita uno
    # Si el rol Owner no tuviera permisos sensibles, quien lo cubra aún no puede dejar a nadie.
    with tenant_scope(ctx(rbac.a)):
        RolePermission.objects.filter(role=owner).exclude(permission_id="users.view").delete()
        RolePermission.objects.filter(role=rbac.delegator, permission_id=VIEW).delete()
    give(rbac.a, rbac.m_luis, {"users.view": None})
    before = state(migrator)
    last = {"membership_id": rbac.membership, "role_id": owner.pk}
    assert denied(rbac.a, rbac.luis, remove_role, **last) is Denied.LAST_OWNER
    assert state(migrator) == before
    with tenant_scope(ctx(rbac.b)):  # organización sin rol Owner: no se puede garantizar nada
        Role.objects.create(code="suelto", name="Suelto")
    carla = make_user(email="carla@example.com")
    give(rbac.b, join(rbac.b, carla).pk, DELEGATE | {"organization.view": None})
    orphan = {"membership_id": uuid4(), "role_id": uuid4()}
    assert denied(rbac.b, carla, assign_role, **orphan) is Denied.LAST_OWNER


def test_concurrent_mutual_removal_keeps_one_owner(
    rbac: Any, migrator: psycopg.Connection[Any]
) -> None:
    owner = rbac.roles["owner"]
    holders = "SELECT membership_id FROM membership_roles WHERE role_id = %s"

    def losing(target: UUID) -> Any:
        return lambda tenant: remove_role(tenant, membership_id=target, role_id=owner.pk)

    changes = {"ana": (rbac.ana, losing(rbac.m_luis)), "luis": (rbac.luis, losing(rbac.membership))}
    with tenant_scope(ctx(rbac.a)):
        MembershipRole.objects.create(membership_id=rbac.m_luis, role=owner)
    outcome = race(rbac.a, changes, hold="ana")  # luis espera y relee: ya no cubre el rol Owner
    assert outcome == {"ana": "ok", "luis": Denied.ESCALATION}
    assert migrator.execute(holders, [owner.pk]).fetchall() == [(rbac.membership,)]
    # A la vez, y con un rol Owner que ambos cubren por otro rol: lo decide el recuento de Owners.
    with tenant_scope(ctx(rbac.a)):
        MembershipRole.objects.create(membership_id=rbac.m_luis, role=owner)
        RolePermission.objects.filter(role=owner).exclude(permission_id="users.view").delete()
    for membership in (rbac.membership, rbac.m_luis):
        give(rbac.a, membership, {"users.manage": None, "users.view": None})
    before = state(migrator)
    outcome = race(rbac.a, changes, hold=None)
    assert sorted(map(str, outcome.values())) == ["last_owner", "ok"]
    assert len(migrator.execute(holders, [owner.pk]).fetchall()) == 1
    assert state(migrator) == (before[0], before[1] - 1, before[2] + 1)


def test_owner_flag_is_reread_under_the_lock(rbac: Any, migrator: psycopg.Connection[Any]) -> None:
    owner, sensitive = rbac.roles["owner"], "users.manage"
    with tenant_scope(ctx(rbac.a)):
        MembershipRole.objects.create(membership_id=rbac.m_luis, role=owner)
    changes: dict[str, tuple[User, Any]] = {
        "ana": (rbac.ana, lambda t: remove_role(t, membership_id=rbac.m_luis, role_id=owner.pk)),
        "luis": (
            rbac.luis,  # sigue teniendo users.manage por otro rol, pero deja de ser Owner
            lambda t: grant_permission(t, role_id=rbac.target.pk, code=sensitive, scope=None),
        ),
    }
    assert race(rbac.a, changes, hold="ana") == {"ana": "ok", "luis": Denied.SENSITIVE}
    granted = "SELECT count(*) FROM role_permissions WHERE role_id = %s"
    assert migrator.execute(granted, [rbac.target.pk]).fetchone() == (0,)


def test_change_waits_for_the_owner_role_lock_and_rereads_the_actor(
    rbac: Any, migrator: psycopg.Connection[Any]
) -> None:
    link = {"membership_id": rbac.m_eva, "role_id": rbac.roles["seller"].pk}
    with migrator.transaction():  # otra transacción retiene el bloqueo del rol Owner
        migrator.execute("SELECT 1 FROM roles WHERE is_owner_role FOR NO KEY UPDATE")
        with pytest.raises(OperationalError), acting(rbac.a, rbac.ana) as tenant:
            with connection.cursor() as cursor:
                cursor.execute("SET LOCAL lock_timeout = '200ms'")
            assign_role(tenant, **link)
        with pytest.raises(OperationalError), acting(rbac.a, rbac.ana) as tenant:
            with connection.cursor() as cursor:
                cursor.execute("SET LOCAL lock_timeout = '200ms'")
            ensure_owner_remains(tenant, without_membership_id=rbac.membership)  # también espera
    with acting(rbac.a, rbac.ana) as tenant:  # el contexto es anterior a perder el rol
        MembershipRole.objects.filter(membership_id=rbac.membership).delete()
        with pytest.raises(AccessDenied) as error:
            assign_role(tenant, **link)
    assert error.value.reason is Denied.PERMISSION


def test_change_and_its_audit_are_atomic(
    rbac: Any, migrator: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("auditoría caída")

    held = {"membership_id": rbac.m_eva, "role_id": rbac.roles["supervisor"].pk}
    with tenant_scope(ctx(rbac.a)):
        MembershipRole.objects.create(**held)
    before = state(migrator)
    link = {"membership_id": rbac.m_eva, "role_id": rbac.roles["seller"].pk}
    grant = {"role_id": rbac.target.pk, "code": "organization.view", "scope": None}
    with acting(rbac.a, rbac.ana) as tenant, monkeypatch.context() as patch:
        patch.setattr(services, "record", broken)
        for service, kwargs in (
            (assign_role, link),
            (grant_permission, grant),
            (remove_role, held),
        ):
            with pytest.raises(RuntimeError):
                service(tenant, **kwargs)
    assert state(migrator) == before  # sin auditoría no hay cambio, y la petición sigue viva
    with acting(rbac.a, rbac.ana) as tenant:
        assign_role(tenant, **link)
    assert state(migrator) == (before[0], before[1] + 1, before[2] + 1)


def test_services_never_decide_by_role_code_name_or_platform_staff() -> None:
    source = re.sub(r'""".*?"""', "", Path(str(services.__file__)).read_text(), flags=re.S)
    code = "\n".join(line.split("#")[0] for line in source.splitlines())
    for forbidden in ("is_platform_staff", "is_superuser", "is_staff", ".code ==", ".name =="):
        assert forbidden not in code, forbidden
    for forbidden in ("role__code", "role__name", 'code="owner"', "request.user"):
        assert forbidden not in code, forbidden
    assert code.count("is_system") == 1 and "is_system=True," in code  # solo al clonar plantillas
    uses = [line.strip() for line in code.splitlines() if "is_owner_role" in line]
    assert uses == [  # la marca solo localiza el rol Owner (bloqueo) y se copia al clonar
        "owner: Role | None = roles.filter(is_owner_role=True).first()",
        "has_owner = roles.filter(is_owner_role=True).exists()",
        "if template.code in existing or (template.is_owner_role and has_owner):",
        "is_owner_role=template.is_owner_role,",
    ]

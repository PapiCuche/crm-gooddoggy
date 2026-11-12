"""F2-06: alta de una organización con su Owner inicial, sus roles y su auditoría."""

import json
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from django.contrib.auth import authenticate
from django.core.exceptions import ObjectDoesNotExist, PermissionDenied, ValidationError
from django.db import OperationalError, connection, connections, transaction

from apps.access.bootstrap import AlreadyBootstrapped, install_initial_owner
from apps.access.catalog import BY_CODE
from apps.access.models import MembershipRole, Role
from apps.access.selectors import AccessDenied, Denied, execution_context
from apps.access.services import clone_role_templates, remove_role
from apps.audit import platform
from apps.audit.services import record
from apps.organizations.bootstrap import SlugTaken, create_organization
from apps.organizations.models import Organization, OrganizationMembership
from apps.provisioning import services as provisioning
from apps.provisioning.services import Bootstrapped, bootstrap_organization
from apps.quotes.models import DiscountPolicy
from config.settings import base
from core.tenancy.context import ActorType, TenantContext, TenantContextError
from core.tenancy.resolution import resolve_tenant
from core.tenancy.scope import tenant_scope, user_scope
from tests.factories import TEST_PASSWORD, make_user
from tests.test_tenancy import raw_count

pytestmark = pytest.mark.usefixtures("tenant_db")
EMAIL = "ana@acme.pe"
TABLES = ("organizations", "organization_memberships", "roles", "membership_roles", "users")
TRAIL = {"operator": "deploy", "reason": "alta del cliente", "slug": "acme"}


def bootstrap(**overrides: Any) -> Bootstrapped:
    fields: dict[str, Any] = {
        "slug": "acme",
        "name": "Acme SAC",
        "owner_email": EMAIL,
        "owner_password": TEST_PASSWORD,
        "operator": "deploy",
        "reason": "alta del cliente",
        **overrides,
    }
    return bootstrap_organization(**fields)


def counts(migrator: psycopg.Connection[Any]) -> dict[str, int]:
    return {
        table: migrator.execute(f"SELECT count(*) FROM {table}").fetchone()[0]  # type: ignore[index] # noqa: S608
        for table in TABLES
    }


def platform_rows(migrator: psycopg.Connection[Any]) -> list[tuple[Any, ...]]:
    return migrator.execute(
        "SELECT action, result, actor_type, entity_id, metadata::text FROM platform_audit_logs "
        "ORDER BY occurred_at, id"
    ).fetchall()


def test_bootstrap_creates_a_usable_organization(
    migrator: psycopg.Connection[Any], settings: Any
) -> None:
    settings.TENANCY_MEMBERSHIP_RESOLVER = base.TENANCY_MEMBERSHIP_RESOLVER  # el resolvedor real
    result = bootstrap(owner_email="  Ana@ACME.pe ")
    assert counts(migrator) == {
        "organizations": 1, "organization_memberships": 1, "roles": 4, "membership_roles": 1,
        "users": 1,
    }  # fmt: skip
    user = authenticate(username=EMAIL, password=TEST_PASSWORD)  # la contraseña es la indicada
    assert user is not None and user.pk == result.owner_user_id and result.user_created
    ctx = resolve_tenant("acme", user, "test")  # la ruta de tenant resuelve su membresía
    assert (ctx.organization_id, ctx.actor_type) == (result.organization_id, ActorType.USER)
    with tenant_scope(ctx):
        ectx = execution_context(ctx)
        assert ectx.membership_id == result.membership_id
        assert set(ectx.permissions) == set(BY_CODE)  # Owner: todo el catálogo, por concesiones
        owner = Role.objects.get(is_owner_role=True)
        assert list(MembershipRole.objects.values_list("role_id", flat=True)) == [owner.pk]
        assert OrganizationMembership.objects.get().status == "ACTIVE"
        got = dict(DiscountPolicy.objects.values_list("role__code", "max_discount_percent"))
        assert got == {"admin": 100, "supervisor": 10, "seller": 3}  # F5-09b4: sin el Owner
    assert Organization.objects.get().status == "ACTIVE"


def test_bootstrap_is_audited_in_the_tenant_and_in_the_platform(
    migrator: psycopg.Connection[Any],
) -> None:
    result = bootstrap(reason="alta pedida por ana@acme.pe")
    tenant = migrator.execute(
        "SELECT organization_id, actor_type, action, entity_id, metadata::text FROM audit_logs "
        "ORDER BY occurred_at, id"
    ).fetchall()
    assert [(row[0], row[1], row[2]) for row in tenant] == [
        (result.organization_id, "SYSTEM", "membership.role_assigned"),
        (result.organization_id, "SYSTEM", "organization.created"),
    ]
    created = json.loads(tenant[1][4])
    assert created["operator"] == "deploy" and created["owner_user_id"] == str(result.owner_user_id)
    started, done = platform_rows(migrator)
    assert started[:4] == (
        "organization.bootstrap.started",
        "SUCCESS",
        "SYSTEM",
        result.organization_id,
    )
    assert done[:4] == ("organization.bootstrapped", "SUCCESS", "SYSTEM", result.organization_id)
    trail = {**TRAIL, "reason": "alta pedida por [EMAIL]"}  # sin emails en plataforma
    owner = {"owner_user_id": str(result.owner_user_id), "user_created": True}
    assert json.loads(started[4]) == trail and json.loads(done[4]) == {**trail, **owner}
    everything = "".join(str(row) for row in (*tenant, started, done))
    assert TEST_PASSWORD not in everything


@pytest.mark.parametrize(
    "step", ["owner_account", "create_organization", "install_initial_owner", "record"]
)
def test_a_failure_in_any_step_leaves_nothing_behind(
    migrator: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch, step: str
) -> None:
    original = getattr(provisioning, step)

    def broken(*args: Any, **kwargs: Any) -> Any:
        original(*args, **kwargs)  # el paso escribe, y después falla
        raise RuntimeError("fallo a mitad del alta")

    monkeypatch.setattr(provisioning, step, broken)
    with pytest.raises(RuntimeError):
        bootstrap()
    assert counts(migrator) == dict.fromkeys(TABLES, 0)  # ni organización, ni usuario, ni roles
    assert migrator.execute("SELECT count(*) FROM audit_logs").fetchone() == (0,)
    rows = platform_rows(migrator)
    assert [(row[0], row[1]) for row in rows] == [
        ("organization.bootstrap.started", "SUCCESS"),
        ("organization.bootstrapped", "FAILED"),
    ]
    assert json.loads(rows[1][4]) == {**TRAIL, "error": "RuntimeError"}  # la clase, no el mensaje
    monkeypatch.undo()
    assert bootstrap().slug == "acme"  # la conexión quedó limpia: el alta siguiente funciona


def test_duplicate_slug_is_rejected_and_creates_nothing_more(
    migrator: psycopg.Connection[Any],
) -> None:
    first = bootstrap()
    before = counts(migrator)
    with pytest.raises(SlugTaken):
        bootstrap(owner_email="otra@acme.pe")
    assert counts(migrator) == before  # tampoco el segundo usuario
    with tenant_scope(TenantContext(first.organization_id, "test")):
        assert MembershipRole.objects.count() == 1  # el Owner sigue siendo único
    assert platform_rows(migrator)[-1][:2] == ("organization.bootstrapped", "FAILED")


def test_an_existing_user_keeps_its_password_and_can_own_several_organizations(
    migrator: psycopg.Connection[Any],
) -> None:
    ana = make_user(email=EMAIL)
    with pytest.raises(ValidationError, match="ya existe"):
        bootstrap()  # con contraseña: nunca se cambia la de una cuenta existente
    first = bootstrap(owner_password=None, owner_email="  Ana@ACME.pe ")  # se canoniza
    second = bootstrap(owner_password=None, slug="acme-norte", name="Acme Norte")
    assert not first.user_created and first.owner_user_id == second.owner_user_id == ana.pk
    assert authenticate(username=EMAIL, password=TEST_PASSWORD) == ana
    assert counts(migrator)["users"] == 1 and counts(migrator)["organizations"] == 2
    with tenant_scope(TenantContext(first.organization_id, "test")):
        assert raw_count("organization_memberships") == raw_count("roles") // 4 == 1  # solo lo suyo


INVALID: list[dict[str, Any]] = [
    {"slug": "Acme"},
    {"slug": "-acme"},
    {"slug": "acme--sac"},
    {"slug": "a" * 64},
    {"slug": ""},
    {"slug": "acme sac"},
    {"name": "   "},
    {"owner_email": "no-es-un-email"},
    {"owner_password": None},
    {"owner_password": "corta"},
    {"owner_password": "12345678901234"},
    {"owner_email": "anabelen.rodriguez@acme.pe", "owner_password": "anabelen.rodriguez"},
    {"reason": "  "},
    {"operator": ""},
]


@pytest.mark.parametrize("overrides", INVALID)
def test_invalid_input_creates_nothing(
    migrator: psycopg.Connection[Any], overrides: dict[str, Any]
) -> None:
    with pytest.raises(ValidationError):
        bootstrap(**overrides)
    assert counts(migrator) == dict.fromkeys(TABLES, 0)


@pytest.mark.parametrize(
    "overrides",
    [
        {"reason": "ñ" * 201},
        {"reason": "dos\nlíneas"},
        {"operator": "o" * 65},
        {"operator": "nulo\x00"},
        {"slug": "Acme"},
        {"name": " "},
        {"name": "n" * 201},
        {"owner_email": "no-es-un-email"},
    ],
)
def test_the_trail_is_validated_before_the_intent_row(
    migrator: psycopg.Connection[Any], overrides: dict[str, Any]
) -> None:
    with pytest.raises(ValidationError):
        bootstrap(**overrides)
    assert platform_rows(migrator) == []
    # En el tope, y con el texto que más ocupa, las dos filas de plataforma siguen cabiendo.
    bootstrap(reason="🐶" * 200, operator="🐶" * 64, slug="a" * 63)
    assert [row[1] for row in platform_rows(migrator)] == ["SUCCESS", "SUCCESS"]


@pytest.mark.parametrize(
    ("account", "message"), [({"is_active": False}, "desactivado"), ({"password": None}, "utiliz")]
)
def test_an_account_that_cannot_sign_in_does_not_become_owner(
    migrator: psycopg.Connection[Any], account: dict[str, Any], message: str
) -> None:
    make_user(email=EMAIL, **account)
    with pytest.raises(ValidationError, match=message):
        bootstrap(owner_password=None)
    assert counts(migrator)["organizations"] == 0


def test_initial_owner_is_not_a_general_way_to_assign_roles(
    migrator: psycopg.Connection[Any],
) -> None:
    result = bootstrap()
    system = TenantContext(result.organization_id, "command")
    luis = make_user(email="luis@acme.pe")
    with tenant_scope(system):
        other = OrganizationMembership.objects.create(user=luis)
        with pytest.raises(AlreadyBootstrapped):  # ya hay un rol asignado: camino cerrado
            install_initial_owner(system, membership_id=other.pk)
        assert MembershipRole.objects.count() == 1
    as_user = TenantContext(
        result.organization_id, "test", result.owner_user_id, ActorType.USER, result.owner_user_id
    )
    with tenant_scope(as_user), pytest.raises(AccessDenied) as denied:
        install_initial_owner(as_user, membership_id=other.pk)  # nunca en nombre de un usuario
    assert denied.value.reason is Denied.MEMBERSHIP
    with tenant_scope(as_user), pytest.raises(AccessDenied) as last:
        owner = Role.objects.get(is_owner_role=True)
        remove_role(as_user, membership_id=result.membership_id, role_id=owner.pk)
    assert last.value.reason is Denied.SELF  # y el Owner inicial no se quita a sí mismo


def test_initial_owner_only_fits_an_organization_that_is_being_born(orgs: dict[str, UUID]) -> None:
    """Una organización que ya existe, aunque no tenga roles asignados, no es un alta."""
    ana, luis, ghost = (make_user(), make_user(), make_user(is_active=False))
    with tenant_scope(ctx := TenantContext(orgs["A"], "celery")):  # SYSTEM, como una tarea
        first = OrganizationMembership.objects.create(user=ana, status="SUSPENDED")
        with pytest.raises(ObjectDoesNotExist):  # la membresía no está activa
            install_initial_owner(ctx, membership_id=first.pk)
        OrganizationMembership.objects.create(user=luis)
        with pytest.raises(AlreadyBootstrapped):  # hay más de un miembro
            install_initial_owner(ctx, membership_id=first.pk)
        assert not Role.objects.exists() and not MembershipRole.objects.exists()
    with tenant_scope(ctx := TenantContext(orgs["B"], "celery")):
        inactive = OrganizationMembership.objects.create(user=ghost)
        with pytest.raises(ObjectDoesNotExist):  # su usuario no cuenta como Owner activo
            install_initial_owner(ctx, membership_id=inactive.pk)
        clone_role_templates(ctx)
        with pytest.raises(AlreadyBootstrapped):  # ya tiene roles
            install_initial_owner(ctx, membership_id=inactive.pk)
        assert not MembershipRole.objects.exists()


@pytest.mark.parametrize(
    "actor",
    [
        {"user_id": uuid4()},  # el tipo por defecto es SYSTEM: no basta
        {"actor_type": ActorType.USER},
        {"actor_id": uuid4()},
        {"actor_type": ActorType.INTEGRATION, "actor_id": uuid4()},
    ],
)
def test_the_bootstrap_frontier_never_acts_for_an_actor(
    orgs: dict[str, UUID], migrator: psycopg.Connection[Any], actor: dict[str, Any]
) -> None:
    ana = make_user()
    with tenant_scope(ctx := TenantContext(orgs["A"], "x", **actor)):
        member = OrganizationMembership.objects.create(user=ana)
        with pytest.raises(AccessDenied):
            install_initial_owner(ctx, membership_id=member.pk)
        assert not Role.objects.exists()
    with tenant_scope(ctx := TenantContext(uuid4(), "x", **actor)), pytest.raises(PermissionDenied):
        create_organization(ctx, slug="nueva", name="Nueva", owner_user_id=ana.pk)
    assert counts(migrator)["organizations"] == 2  # solo las del fixture


def test_the_frontier_validates_and_never_adopts_an_existing_organization(
    orgs: dict[str, UUID], migrator: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    ana = make_user()
    with tenant_scope(ctx := TenantContext(uuid4(), "command")), pytest.raises(ValidationError):
        create_organization(ctx, slug="Acme", name="Acme", owner_user_id=ana.pk)  # su propia regla
    monkeypatch.setattr(provisioning, "new_id", lambda: orgs["A"])  # un id que ya existe
    with pytest.raises(SlugTaken):
        bootstrap()
    assert platform_rows(migrator)[-1][:2] == ("organization.bootstrapped", "FAILED")
    assert counts(migrator)["organization_memberships"] == 0  # la que existía sigue sin miembros


def test_an_account_created_meanwhile_is_reported_as_existing(
    migrator: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def meanwhile(password: str, user: Any = None) -> None:
        make_user(email=EMAIL)  # otra alta crea la misma cuenta después de la comprobación

    monkeypatch.setattr("apps.accounts.bootstrap.validate_password", meanwhile)
    with pytest.raises(ValidationError, match="ya existe"):  # no un IntegrityError con el email
        bootstrap()
    assert counts(migrator) == dict.fromkeys(TABLES, 0)


def test_bootstrap_does_not_run_inside_a_tenant_or_touch_another_one(
    orgs: dict[str, UUID], migrator: psycopg.Connection[Any]
) -> None:
    with tenant_scope(TenantContext(orgs["A"], "test")), pytest.raises(TenantContextError):
        bootstrap()
    with transaction.atomic(), pytest.raises(TenantContextError):  # ni en una transacción ajena
        bootstrap()
    with user_scope(uuid4()), pytest.raises(TenantContextError):
        bootstrap()
    assert platform_rows(migrator) == [] and counts(migrator)["organizations"] == 2
    result = bootstrap()
    with tenant_scope(TenantContext(orgs["A"], "test")):
        assert raw_count("roles") == raw_count("organization_memberships") == 0  # A no ve nada
        assert raw_count("audit_logs") == 0
    with connection.cursor() as cursor:  # sin tenant: RLS no deja ver los roles de nadie
        cursor.execute("SELECT count(*) FROM roles")
        assert cursor.fetchone() == (0,)
    assert result.organization_id not in orgs.values()


def test_a_commit_whose_answer_is_lost_is_recorded_as_done(
    migrator: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    wrapper = connections["default"]

    def lost_answer() -> None:
        monkeypatch.undo()
        wrapper.commit()  # el servidor confirma, pero la respuesta no llega
        raise OperationalError("server closed the connection unexpectedly")

    def arm(*args: Any, **kwargs: Any) -> None:
        record(*args, **kwargs)
        monkeypatch.setattr(wrapper, "commit", lost_answer)  # el siguiente COMMIT es el del alta

    monkeypatch.setattr(provisioning, "record", arm)
    result = bootstrap()
    assert Organization.objects.get().pk == result.organization_id
    done = platform_rows(migrator)[-1]
    assert done[:2] == ("organization.bootstrapped", "SUCCESS")  # no un FAILED falso
    assert json.loads(done[4])["commit_error"] == "OperationalError"


def test_a_result_row_that_cannot_be_written_does_not_hide_the_error(
    migrator: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    bootstrap()
    write = platform.record

    def sink(action: str, **kwargs: Any) -> Any:
        if action == provisioning.DONE:
            raise RuntimeError("sin sumidero de auditoría")
        return write(action, **kwargs)

    monkeypatch.setattr(platform, "record", sink)
    with pytest.raises(SlugTaken):  # el error del alta, no el de su auditoría
        bootstrap(owner_email="otra@acme.pe")
    assert platform_rows(migrator)[-1][0] == "organization.bootstrap.started"

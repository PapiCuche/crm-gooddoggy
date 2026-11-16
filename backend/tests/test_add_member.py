"""F2-127 (D-F2-14, opción B): `manage.py add_member`, el alta de un miembro por un operador."""

import io
import json
import logging
from typing import Any
from uuid import UUID

import psycopg
import pytest
from django.contrib.auth import authenticate
from django.core.exceptions import PermissionDenied
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import OperationalError, connections
from django.test import Client

from apps.access.bootstrap import grant_template_role
from apps.access.selectors import AccessDenied, Denied
from apps.accounts.models import User
from apps.accounts.tasks import send_password_changed
from apps.audit.services import record
from apps.organizations.bootstrap import add_operator_membership
from apps.provisioning import members
from config.settings import base
from core.tenancy.context import TenantContext
from core.tenancy.resolution import resolve_tenant
from core.tenancy.scope import tenant_scope
from tests.factories import TEST_PASSWORD, make_user
from tests.test_auth_api import CSRF, login, send
from tests.test_bootstrap import bootstrap, platform_rows
from tests.test_bootstrap_command import stdin
from tests.test_password_reset_change import CHANGE, issued

pytestmark = pytest.mark.usefixtures("tenant_db")
EMAIL, TEMPORARY = "luis@acme.pe", "temporal-larga-y-rara-58"  # noqa: S105
REASON = ["--reason", "alta pedida por el Owner"]
MEMBERS = (
    "SELECT m.organization_id, r.code FROM organization_memberships m JOIN users u"
    " ON u.id = m.user_id LEFT JOIN membership_roles mr ON mr.membership_id = m.id"
    " LEFT JOIN roles r ON r.id = mr.role_id AND r.organization_id = m.organization_id"
    " WHERE u.email = %s ORDER BY 1, 2"
)
CREATED = (
    "SELECT organization_id, actor_type, entity_id, metadata FROM audit_logs WHERE action = %s"
)


@pytest.fixture(autouse=True)
def stack(settings: Any) -> None:
    settings.TENANCY_MEMBERSHIP_RESOLVER = base.TENANCY_MEMBERSHIP_RESOLVER  # el resolvedor real


@pytest.fixture
def real_orgs() -> dict[str, UUID]:
    """Dos organizaciones de verdad, cada una con su Owner y sus roles de plantilla."""
    acme = bootstrap().organization_id
    beta = bootstrap(slug="beta", name="Beta SAC", owner_email="eva@beta.pe").organization_id
    return {"acme": acme, "beta": beta}


@pytest.fixture
def no_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("getpass.getpass", refuse)


def refuse(prompt: str = "") -> str:
    raise AssertionError("no debía pedir contraseña")


def add(*args: str, **options: Any) -> str:
    out = io.StringIO()
    call_command("add_member", *args, *REASON, stdout=out, **options)
    return out.getvalue()


def memberships(migrator: psycopg.Connection[Any], email: str = EMAIL) -> list[tuple[Any, ...]]:
    return migrator.execute(MEMBERS, [email]).fetchall()


def test_a_new_account_joins_with_a_temporary_password_that_is_never_printed(
    real_orgs: dict[str, UUID],
    migrator: psycopg.Connection[Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr("sys.stdin", stdin(TEMPORARY + "\n"))
    out = add("--org", "acme", "--email", " Luis@ACME.pe ", "--role", "seller", "--password-stdin")
    captured = capsys.readouterr()
    printed = out + captured.out + captured.err + caplog.text
    assert "Miembro añadido a acme" in out and "temporal" in out
    assert TEMPORARY not in printed and "@" not in out
    user = User.objects.get(email=EMAIL)
    assert user.password_change_required and user.check_password(TEMPORARY)
    assert memberships(migrator) == [(real_orgs["acme"], "seller")]  # nunca en la otra organización
    ((org, actor, entity, metadata),) = migrator.execute(CREATED, ["membership.created"])
    assert (org, actor, metadata["role"], metadata["user_created"]) == (
        real_orgs["acme"],
        "SYSTEM",
        "seller",
        True,
    )
    assert TEMPORARY not in json.dumps(metadata)
    rows = platform_rows(migrator)[-2:]
    assert [(action, result, actor) for action, result, actor, *_ in rows] == [
        ("membership.add.started", "SUCCESS", "SYSTEM"),
        ("membership.added", "SUCCESS", "SYSTEM"),
    ]
    done = json.loads(rows[1][4])
    assert done["added"] is True and done["membership_id"] == str(entity)
    assert all(TEMPORARY not in str(row) for row in platform_rows(migrator))


def test_running_it_again_changes_nothing_and_asks_for_nothing(
    real_orgs: dict[str, UUID], migrator: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("getpass.getpass", lambda prompt="": TEMPORARY)  # por el terminal
    add("--org", "acme", "--email", EMAIL, "--role", "seller")
    password = User.objects.get(email=EMAIL).password
    monkeypatch.setattr("getpass.getpass", refuse)
    for role in ("seller", "admin"):  # ni duplica ni cambia el rol de quien ya es miembro
        assert "ya es miembro de acme" in add("--org", "acme", "--email", EMAIL, "--role", role)
    assert memberships(migrator) == [(real_orgs["acme"], "seller")]
    assert len(migrator.execute(CREATED, ["membership.created"]).fetchall()) == 1
    assert User.objects.get(email=EMAIL).password == password
    assert json.loads(platform_rows(migrator)[-1][4])["added"] is False


def test_an_existing_account_keeps_its_password_and_joins_only_the_named_organization(
    real_orgs: dict[str, UUID], migrator: psycopg.Connection[Any], no_prompt: None
) -> None:
    luis = make_user(email=EMAIL)
    out = add("--org", "beta", "--email", EMAIL, "--role", "admin", "--password-stdin")
    assert "existente" in out and str(luis.pk) in out
    assert memberships(migrator) == [(real_orgs["beta"], "admin")]
    luis.refresh_from_db()
    assert luis.check_password(TEST_PASSWORD) and not luis.password_change_required
    add("--org", "acme", "--email", EMAIL, "--role", "supervisor")
    assert memberships(migrator) == sorted(
        [(real_orgs["acme"], "supervisor"), (real_orgs["beta"], "admin")]
    )
    owners = memberships(migrator, "eva@beta.pe")  # el Owner de la otra no cambia
    assert owners == [(real_orgs["beta"], "owner")]


@pytest.mark.parametrize(
    ("role", "refused"),
    [
        ("owner", "Owner"),
        ("Owner", "Rol desconocido"),
        ("gerente", "^Rol desconocido: usa uno de admin, seller, supervisor$"),
        ("", "Rol desconocido"),
    ],
)
def test_the_owner_role_and_roles_outside_the_catalog_are_refused(
    real_orgs: dict[str, UUID],
    migrator: psycopg.Connection[Any],
    no_prompt: None,
    role: str,
    refused: str,
) -> None:
    before = platform_rows(migrator)
    with pytest.raises(CommandError, match=refused):
        add("--org", "acme", "--email", EMAIL, "--role", role)
    assert memberships(migrator) == [] and not User.objects.filter(email=EMAIL).exists()
    assert platform_rows(migrator) == before  # rechazado antes de entrar en la organización


def test_the_boundaries_never_grant_the_owner_role_nor_act_for_a_user(
    real_orgs: dict[str, UUID],
) -> None:
    luis = make_user(email=EMAIL)
    ctx = TenantContext(real_orgs["acme"], "command")
    with tenant_scope(ctx):
        membership_id, added = add_operator_membership(ctx, user_id=luis.pk)
        with pytest.raises(AccessDenied) as denied:
            grant_template_role(ctx, membership_id=membership_id, template="owner")
    assert added and denied.value.reason == Denied.OWNER_ROLE
    owner = authenticate(username="ana@acme.pe", password=TEST_PASSWORD)
    as_owner = resolve_tenant("acme", owner, "test")  # ni el Owner usa la frontera del operador
    with tenant_scope(as_owner), pytest.raises(PermissionDenied):
        add_operator_membership(as_owner, user_id=make_user(email="otro@acme.pe").pk)


def test_a_custom_role_with_a_catalog_code_is_not_a_catalog_role(
    real_orgs: dict[str, UUID], migrator: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    migrator.execute(  # la organización borró la plantilla y creó un rol propio con su código
        "UPDATE roles SET is_system = false WHERE organization_id = %s AND code = 'seller'",
        [real_orgs["acme"]],
    )
    monkeypatch.setattr("sys.stdin", stdin(TEMPORARY + "\n"))
    with pytest.raises(CommandError, match="no tiene ese rol"):
        add("--org", "acme", "--email", EMAIL, "--role", "seller", "--password-stdin")
    assert memberships(migrator) == [] and not User.objects.filter(email=EMAIL).exists()
    assert platform_rows(migrator)[-1][:2] == ("membership.added", "FAILED")


@pytest.mark.parametrize(
    ("org", "account", "refused"),
    [
        ("nadie", {}, "no encontrada"),
        ("acme", {"is_active": False}, "desactivado"),
        ("acme", {"is_platform_staff": True}, "personal de plataforma"),
    ],
)
def test_unknown_organizations_and_unusable_accounts_are_refused(
    real_orgs: dict[str, UUID],
    migrator: psycopg.Connection[Any],
    no_prompt: None,
    org: str,
    account: dict[str, Any],
    refused: str,
) -> None:
    if account:
        User.objects.filter(pk=make_user(email=EMAIL).pk).update(**account)
    else:
        make_user(email=EMAIL)
    with pytest.raises(CommandError, match=refused):
        add("--org", org, "--email", EMAIL, "--role", "seller")
    assert memberships(migrator) == []


def test_a_weak_password_creates_nothing(
    real_orgs: dict[str, UUID], migrator: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("sys.stdin", stdin("123\n"))
    with pytest.raises(CommandError) as refused:
        add("--org", "acme", "--email", EMAIL, "--role", "seller", "--password-stdin")
    assert "123" not in str(refused.value)
    assert not User.objects.filter(email=EMAIL).exists() and memberships(migrator) == []
    assert platform_rows(migrator)[-1][:2] == ("membership.added", "FAILED")


def test_the_temporary_password_opens_no_session_until_the_account_picks_its_own(
    real_orgs: dict[str, UUID],
    migrator: psycopg.Connection[Any],
    monkeypatch: pytest.MonkeyPatch,
    settings: Any,
) -> None:
    settings.ROOT_URLCONF, settings.MIDDLEWARE = "config.urls", base.MIDDLEWARE
    settings.APP_ORIGIN = "https://app.example.com"
    monkeypatch.setattr(send_password_changed, "apply_async", lambda **how: None)
    monkeypatch.setattr("sys.stdin", stdin(TEMPORARY + "\n"))
    add("--org", "acme", "--email", EMAIL, "--role", "seller", "--password-stdin")
    browser = Client(enforce_csrf_checks=True)
    browser.get(CSRF)
    refused = login(browser, EMAIL, TEMPORARY)
    assert (refused.status_code, refused.json()["code"]) == (403, "PASSWORD_CHANGE_REQUIRED")
    assert "crm_session" not in browser.cookies or not browser.cookies["crm_session"].value
    assert platform_rows(migrator)[-1][:3] == (
        "auth.login.password_change_required",
        "DENIED",
        "ANONYMOUS",
    )
    assert login(browser, EMAIL, "otra-cosa-larga-12").status_code == 401  # la mala, como siempre
    mine = "la-mia-larga-y-rara-31"  # noqa: S105
    assert (
        send(browser, "POST", CHANGE, {"token": issued(EMAIL), "password": mine}).status_code == 204
    )
    assert not User.objects.get(email=EMAIL).password_change_required
    assert login(browser, EMAIL, mine).status_code == 200


def test_a_lockout_started_by_the_temporary_password_is_audited(
    real_orgs: dict[str, UUID],
    migrator: psycopg.Connection[Any],
    monkeypatch: pytest.MonkeyPatch,
    settings: Any,
) -> None:
    settings.ROOT_URLCONF, settings.MIDDLEWARE = "config.urls", base.MIDDLEWARE
    settings.LOGIN_THROTTLE = base.LOGIN_THROTTLE  # 5 intentos por cuenta y dirección
    monkeypatch.setattr("sys.stdin", stdin(TEMPORARY + "\n"))
    add("--org", "acme", "--email", EMAIL, "--role", "seller", "--password-stdin")
    browser = Client(enforce_csrf_checks=True)
    browser.get(CSRF)
    for _ in range(4):
        assert login(browser, EMAIL, "otra-cosa-larga-12").status_code == 401
    assert login(browser, EMAIL, TEMPORARY).status_code == 403  # el quinto empieza el bloqueo
    throttled = [row for row in platform_rows(migrator) if row[0] == "auth.login.throttled"]
    assert len(throttled) == 1 and json.loads(throttled[0][4])["scope"] == "pair"
    assert login(browser, EMAIL, TEMPORARY).status_code == 429


def test_the_command_warns_when_no_email_can_reach_the_new_account(
    real_orgs: dict[str, UUID], monkeypatch: pytest.MonkeyPatch, settings: Any
) -> None:
    def run(email: str) -> str:
        monkeypatch.setattr("sys.stdin", stdin(TEMPORARY + "\n"))
        err = io.StringIO()
        call_command(
            "add_member", "--org", "acme", "--email", email, "--role", "seller",
            "--password-stdin", *REASON, stdout=io.StringIO(), stderr=err,
        )  # fmt: skip
        return err.getvalue()

    settings.EMAIL_HOST = ""
    assert "EMAIL_HOST" in run(EMAIL)
    assert run(EMAIL) == ""  # ya es miembro: no se creó ninguna cuenta
    settings.EMAIL_HOST = "smtp.example.com"
    assert run("otra@acme.pe") == ""


@pytest.mark.parametrize("committed", [True, False])
def test_a_commit_error_is_recorded_by_what_the_database_kept(
    real_orgs: dict[str, UUID],
    migrator: psycopg.Connection[Any],
    monkeypatch: pytest.MonkeyPatch,
    committed: bool,
) -> None:
    wrapper = connections["default"]

    def lost_answer() -> None:
        monkeypatch.undo()
        if committed:
            wrapper.commit()  # el servidor confirma, pero la respuesta no llega
        raise OperationalError("server closed the connection unexpectedly")

    def arm(*args: Any, **kwargs: Any) -> Any:
        written = record(*args, **kwargs)
        monkeypatch.setattr(wrapper, "commit", lost_answer)  # el siguiente COMMIT es el del alta
        return written

    monkeypatch.setattr(members, "record", arm)
    fields = {"slug": "acme", "email": EMAIL, "role": "seller", "password": TEMPORARY}
    if committed:
        assert members.add_member(**fields, operator="deploy", reason="alta").added
        assert memberships(migrator) == [(real_orgs["acme"], "seller")]
    else:
        with pytest.raises(OperationalError):
            members.add_member(**fields, operator="deploy", reason="alta")
        assert memberships(migrator) == []
    done = platform_rows(migrator)[-1]
    expected = ("SUCCESS", "commit_error") if committed else ("FAILED", "error")
    assert (done[0], done[1]) == ("membership.added", expected[0])
    assert json.loads(done[4])[expected[1]] == "OperationalError"


@pytest.mark.parametrize("committed", [True, False])
def test_a_recheck_that_fails_after_a_commit_error_writes_no_result_row(
    real_orgs: dict[str, UUID],
    migrator: psycopg.Connection[Any],
    monkeypatch: pytest.MonkeyPatch,
    committed: bool,
) -> None:
    """ADR-013 §5: si no se sabe qué quedó en la base, no hay fila de resultado, ni `SUCCESS` ni
    `FAILED`, y manda el error original."""
    wrapper = connections["default"]

    def unknown(*args: Any) -> bool:
        raise RuntimeError("la comprobación tampoco llega")

    def lost_answer() -> None:
        monkeypatch.undo()
        monkeypatch.setattr(members, "_stored", unknown)
        if committed:
            wrapper.commit()
        raise OperationalError("server closed the connection unexpectedly")

    def arm(*args: Any, **kwargs: Any) -> Any:
        written = record(*args, **kwargs)
        monkeypatch.setattr(wrapper, "commit", lost_answer)
        return written

    monkeypatch.setattr(members, "record", arm)
    fields = {"slug": "acme", "email": EMAIL, "role": "seller", "password": TEMPORARY}
    with pytest.raises(OperationalError):
        members.add_member(**fields, operator="deploy", reason="alta")
    assert platform_rows(migrator)[-1][0] == "membership.add.started"

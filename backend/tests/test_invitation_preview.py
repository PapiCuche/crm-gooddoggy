"""F2-130a: `POST /api/v1/invitations/preview/` encuentra una invitación por el secreto de su enlace
y enseña una vista previa segura (ADR-020 §3 a §5, con la excepción de §4: D-F2-14). Middleware,
CSRF, límite de intentos y la función `resolve_invitation` reales, como `crm_app`."""

import hashlib
import json
import logging
import secrets
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import psycopg
import pytest
from django.conf import settings as django_settings
from django.test import Client

from apps.accounts import recovery
from apps.members import invitation_links
from apps.organizations import services as organizations
from core.tenancy import scope
from tests import test_auth_api, test_authorization
from tests.factories import make_user
from tests.test_invitations_table import invite
from tests.test_memberships import join, raw

real_stack, browser, world = (
    test_auth_api.real_stack,
    test_auth_api.browser,
    test_authorization.world,
)
pytestmark = pytest.mark.usefixtures("tenant_db")
PREVIEW = "/api/v1/invitations/preview/"
LOOKUP = "SELECT * FROM public.resolve_invitation(%s)"
REFUSED = (
    400,
    {
        "code": "INVALID_INVITATION_LINK",
        "message": "El enlace no vale o ya caducó. Pide que te inviten otra vez.",
    },
)
SOON = datetime.now(UTC) + timedelta(days=7)
PAST = datetime.now(UTC) - timedelta(seconds=1)
ROWS = "SELECT id, status, token_hash, send_count, updated_at FROM user_invitations ORDER BY id"


def digest(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def link(org: UUID, email: str, by: Any, **fields: Any) -> str:
    """Una invitación con enlace; devuelve su secreto, el que iría en el correo."""
    secret = fields.pop("secret", None) or secrets.token_urlsafe(32)
    invite(org, email, by, token_hash=digest(secret), **fields)
    return str(secret)


def ask(client: Client, token: Any, *, address: str = "127.0.0.1", **extra: Any) -> Any:
    csrf = client.cookies["csrftoken"].value
    body = json.dumps({"token": token, **extra})
    headers = {"X-CSRFToken": csrf}
    return client.post(PREVIEW, body, "application/json", headers=headers, REMOTE_ADDR=address)


def wrong(secret: str) -> str:
    """El mismo secreto con su último carácter cambiado."""
    return secret[:-1] + ("A" if secret[-1] != "A" else "B")


def said(answer: Any) -> tuple[int, Any]:
    return answer.status_code, json.loads(answer.content)


def heads(answer: Any) -> dict[str, str]:
    return {k: v for k, v in answer.headers.items() if k.lower() != "x-request-id"}


@pytest.fixture(autouse=True)
def roomy(settings: Any) -> None:
    settings.INVITATION_LINK_THROTTLE = (100, 900)


@pytest.fixture
def lookups(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Cada búsqueda por hash, con lo que se buscó."""
    seen: list[str] = []
    real = organizations.find_invitation

    def spy(hashed: str) -> Any:
        seen.append(hashed)
        return real(hashed)

    monkeypatch.setattr(organizations, "find_invitation", spy)
    return seen


@pytest.fixture
def scopes(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Cada `tenant_scope` que abre la vista previa, con su contexto."""
    opened: list[Any] = []
    real = scope.tenant_scope

    @contextmanager
    def spy(ctx: Any) -> Any:
        opened.append(ctx)
        with real(ctx) as active:
            yield active

    monkeypatch.setattr(invitation_links, "tenant_scope", spy)
    return opened


def test_every_link_that_is_not_valid_gets_the_same_bytes_and_the_same_work(
    browser: Client, world: Any, migrator: psycopg.Connection[Any], lookups: list[str], scopes: Any
) -> None:
    join(world.b, world.ana)
    malformed = "no es un secreto!"
    cases = {
        "malformed": malformed,
        "short": "a" * 42,
        "unknown": secrets.token_urlsafe(32),
        "expired": link(world.a, "exp@cliente.pe", world.ana, expires_at=PAST),
        "revoked": link(world.a, "rev@cliente.pe", world.ana, status="REVOKED"),
        "accepted": link(world.a, "acc@cliente.pe", world.ana, status="ACCEPTED", accepted_at=PAST),
        # Un secreto mal formado cuyo hash sí está guardado: la forma se comprueba igual.
        "stored_malformed": link(world.b, "mal@cliente.pe", world.ana, secret=malformed + "x"),
        "wrong_secret": wrong(link(world.b, "b@cliente.pe", world.ana)),
    }
    before = migrator.execute(ROWS).fetchall()
    answers = {reason: ask(browser, token) for reason, token in cases.items()}
    for reason, answer in answers.items():
        assert said(answer) == REFUSED, reason
    assert len({a.content for a in answers.values()}) == 1  # los mismos bytes
    assert len({json.dumps(heads(a), sort_keys=True) for a in answers.values()}) == 1
    # El mismo trabajo: una búsqueda, con un SHA-256 del secreto presentado, también si está mal.
    assert lookups == [digest(token) for token in cases.values()]
    assert scopes == []  # ninguna organización se abre sin una invitación encontrada
    assert migrator.execute(ROWS).fetchall() == before  # no cambia nada
    assert migrator.execute("SELECT count(*) FROM platform_audit_logs").fetchone() == (0,)


def test_a_valid_link_shows_the_organization_the_email_and_whether_to_create_an_account(
    browser: Client, world: Any, migrator: psycopg.Connection[Any], lookups: Any, scopes: Any
) -> None:
    secret = link(world.a, "luis@cliente.pe", world.ana)
    before = migrator.execute(ROWS).fetchall()
    answer = ask(browser, secret, organization_id=str(world.b), slug="org-b")  # se ignoran
    assert said(answer) == (
        200,
        {"organization_name": "Org A", "email": "luis@cliente.pe", "needs_account": True},
    )
    assert answer.headers["Cache-Control"] == "no-store"
    assert secret not in answer.content.decode()
    assert lookups == [digest(secret)]
    (ctx,) = scopes  # abierto una vez, después de encontrarla, con SYSTEM y su organización
    assert (ctx.organization_id, ctx.actor_type, ctx.user_id) == (world.a, "SYSTEM", None)
    assert migrator.execute(ROWS).fetchall() == before  # abrir el enlace no lo gasta
    assert migrator.execute("SELECT count(*) FROM platform_audit_logs").fetchone() == (0,)
    make_user(email="luis@cliente.pe")  # con cuenta: hay que iniciar sesión, no crearla
    assert said(ask(browser, secret))[1]["needs_account"] is False


def test_each_link_shows_only_its_own_organization(browser: Client, world: Any) -> None:
    join(world.b, world.ana)
    mine = link(world.a, "a@cliente.pe", world.ana)
    theirs = link(world.b, "b@cliente.pe", world.ana)
    assert said(ask(browser, mine))[1]["organization_name"] == "Org A"
    assert said(ask(browser, theirs))[1] == {
        "organization_name": "Org B",
        "email": "b@cliente.pe",
        "needs_account": True,
    }


def test_a_suspended_organization_or_a_row_that_changed_after_the_lookup_is_not_shown(
    browser: Client,
    world: Any,
    migrator: psycopg.Connection[Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = link(world.a, "a@cliente.pe", world.ana)
    migrator.execute("UPDATE organizations SET status = 'SUSPENDED' WHERE id = %s", [world.a])
    assert said(ask(browser, secret)) == REFUSED
    migrator.execute("UPDATE organizations SET status = 'TRIAL' WHERE id = %s", [world.a])
    assert said(ask(browser, secret))[0] == 200
    # La búsqueda la devolvió, pero dentro del scope ya no vale: la misma respuesta.
    found = organizations.find_invitation(digest(secret))
    monkeypatch.setattr(organizations, "find_invitation", lambda hashed: found)
    other = link(world.a, "otra@cliente.pe", world.ana)
    assert said(ask(browser, other)) == REFUSED  # su hash no es el de la fila
    assert said(ask(browser, secret))[0] == 200
    change = "UPDATE user_invitations SET {} WHERE token_hash = %s"
    migrator.execute(change.format("status = 'REVOKED'"), [digest(secret)])
    assert said(ask(browser, secret)) == REFUSED  # revocada entre la búsqueda y el scope
    migrator.execute(change.format("status = 'PENDING'"), [digest(secret)])
    migrator.execute(change.format("expires_at = now() - interval '1 second'"), [digest(secret)])
    assert said(ask(browser, secret)) == REFUSED  # caducó entre la búsqueda y el scope


def test_the_attempts_are_limited_per_address_before_any_lookup(
    browser: Client, world: Any, settings: Any, lookups: list[str]
) -> None:
    settings.INVITATION_LINK_THROTTLE = (3, 900)
    secret = link(world.a, "a@cliente.pe", world.ana)
    assert said(ask(browser, "x"))[0] == 400
    assert said(ask(browser, secret))[0] == 200  # también cuenta un enlace que vale
    assert said(ask(browser, "y"))[0] == 400
    refused = [ask(browser, secret), ask(browser, "z")]
    for answer in refused:
        assert answer.status_code == 429 and 0 < int(answer.headers["Retry-After"]) <= 900
        assert json.loads(answer.content)["code"] == "RATE_LIMITED"
    assert refused[0].content == refused[1].content  # sin decir si el enlace valía
    assert len(lookups) == 3  # los rechazados no buscan
    other = ask(browser, secret, address="198.51.100.7")  # otra dirección: su propio tope
    assert other.status_code == 200
    v6 = [ask(browser, "w", address=f"2001:db8::{n}") for n in range(1, 5)]
    assert [a.status_code for a in v6] == [400, 400, 400, 429]  # la misma red /64


def test_the_counter_lives_with_the_link_counters_and_its_window_is_purged(
    browser: Client, world: Any, migrator: psycopg.Connection[Any], settings: Any
) -> None:
    settings.PASSWORD_RESET_THROTTLE = {"ip": (20, 60), "identifier": (5, 60), "change": (10, 60)}
    settings.INVITATION_LINK_THROTTLE = (10, 900)
    ask(browser, "x")
    keys = migrator.execute("SELECT key FROM login_throttles").fetchall()
    assert keys == [("rst:inv:ip:127.0.0.1",)]
    migrator.execute("UPDATE login_throttles SET updated_at = now() - interval '61 seconds'")
    assert recovery.purge_limits() == 0  # su ventana es la más larga: aún cuenta
    migrator.execute("UPDATE login_throttles SET updated_at = now() - interval '901 seconds'")
    assert recovery.purge_limits() == 1


def test_without_csrf_nothing_is_counted_or_looked_up(
    world: Any, migrator: psycopg.Connection[Any], lookups: list[str]
) -> None:
    secret = link(world.a, "a@cliente.pe", world.ana)
    bare = Client(enforce_csrf_checks=True)
    answer = bare.post(PREVIEW, json.dumps({"token": secret}), "application/json")
    assert said(answer)[0] == 403 and said(answer)[1]["code"] == "CSRF_FAILED"
    assert lookups == []
    assert migrator.execute("SELECT count(*) FROM login_throttles").fetchone() == (0,)


def test_neither_the_secret_nor_the_email_nor_the_organization_reach_the_logs(
    browser: Client, world: Any, caplog: pytest.LogCaptureFixture
) -> None:
    secret = link(world.a, "luis@cliente.pe", world.ana)
    caplog.set_level(logging.DEBUG)
    for token in (secret, wrong(secret), "malo"):
        ask(browser, token)
    logged = caplog.text + json.dumps([vars(r) for r in caplog.records], default=str)
    for value in (secret, digest(secret), "luis@cliente.pe", "Org A", str(world.a)):
        assert value not in logged


def test_the_lookup_is_a_minimal_read_only_definer_for_crm_app(
    migrator: psycopg.Connection[Any],
) -> None:
    row = migrator.execute(
        "SELECT p.prosecdef, p.provolatile, pg_get_userbyid(p.proowner), p.proconfig,"
        " pg_get_function_result(p.oid), has_function_privilege('public', p.oid, 'EXECUTE'),"
        " has_function_privilege(%s, p.oid, 'EXECUTE'), p.prosrc"
        " FROM pg_proc p WHERE p.oid = 'public.resolve_invitation(text)'::regprocedure",
        [django_settings.DB_APP_ROLE],
    ).fetchone()
    assert row is not None
    assert row[:7] == (
        True,
        "s",  # STABLE: no escribe, tampoco para contar intentos
        django_settings.DB_MIGRATOR_ROLE,
        ["search_path=pg_catalog, public, pg_temp"],
        "TABLE(organization_id uuid, invitation_id uuid)",  # dos identificadores y nada más
        False,
        True,
    )
    assert "'^[0-9a-f]{64}$'" in row[7]  # solo la forma de un SHA-256, nunca el secreto en claro


def test_the_lookup_finds_only_a_pending_unexpired_invitation_by_its_hash(
    world: Any, migrator: psycopg.Connection[Any]
) -> None:
    secret = link(world.a, "a@cliente.pe", world.ana)
    expired = link(world.a, "e@cliente.pe", world.ana, expires_at=PAST)
    revoked = link(world.a, "r@cliente.pe", world.ana, status="REVOKED")
    accepted = link(world.a, "c@cliente.pe", world.ana, status="ACCEPTED", accepted_at=PAST)
    found = migrator.execute("SELECT id FROM user_invitations WHERE email = 'a@cliente.pe'")
    (invitation,) = found.fetchone() or (None,)
    assert raw("SELECT count(*) FROM user_invitations") == [(0,)]  # sin scope, RLS: nada
    assert raw(LOOKUP, [digest(secret)]) == [(world.a, invitation)]  # la función, sí
    for token in (expired, revoked, accepted):
        assert raw(LOOKUP, [digest(token)]) == []
    stored = digest(secret)
    for shape in (None, stored.upper(), stored[:-1], stored + "0", secret, " " + stored):
        assert raw(LOOKUP, [shape]) == []

"""F2-03C: cierre de sesión, caducidad por inactividad y absoluta, organizaciones del usuario
y purga (ADR-003 §2, D-F2-2, ADR-013 §5). Middleware, sesiones en BD y auditoría reales."""

import time
from datetime import timedelta
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import psycopg
import pytest
from celery.schedules import crontab
from django.conf import settings
from django.contrib.sessions.backends.db import SessionStore
from django.contrib.sessions.models import Session
from django.db import OperationalError, connection, transaction
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.accounts import services
from apps.accounts.models import User
from apps.accounts.tasks import purge_expired_sessions
from apps.audit import platform
from apps.organizations.models import Organization
from config.settings import base
from core.observability import reporting
from tests import test_auth_api
from tests.factories import make_user, sign_in
from tests.test_auth_api import NO_SESSION, SESSION, audit, login, reply, send
from tests.test_memberships import join

pytestmark = pytest.mark.usefixtures("tenant_db")
real_stack, ana, browser = test_auth_api.real_stack, test_auth_api.ana, test_auth_api.browser
LOGOUT, MINE = "/api/v1/auth/logout/", "/api/v1/me/organizations/"
LOCAL = ("127.0.0.1", test_auth_api.AGENT)


def test_my_organizations_lists_only_active_memberships_without_a_tenant(
    orgs: dict[str, UUID], ana: User, browser: Client, migrator: psycopg.Connection[Any]
) -> None:
    assert reply(browser.get(MINE)) == NO_SESSION
    luis = make_user(email="luis@example.com")
    join(orgs["B"], luis)  # otra persona, otra organización
    migrator.execute(
        "INSERT INTO organizations (id, slug, name, status, created_at) VALUES "
        "(uuidv7(), 'org-c', 'Org C', 'SUSPENDED', now()), "
        "(uuidv7(), 'org-d', 'Org D', 'ACTIVE', now()), "
        "(uuidv7(), 'zeta', 'Alfa', 'TRIAL', now()), "
        "(uuidv7(), 'a-gemela', 'Org B', 'ACTIVE', now())"
    )
    by_slug = {row.slug: row.pk for row in Organization.objects.all()}
    join(by_slug["org-c"], ana)  # organización suspendida
    join(by_slug["org-d"], ana, status="INVITED")  # membresía que aún no está activa
    login(browser)
    assert browser.get(MINE).json() == [{"id": str(orgs["A"]), "slug": "org-a", "name": "Org A"}]
    for slug in ("org-b", "zeta", "a-gemela"):  # en prueba cuenta; dos con el mismo nombre
        join(by_slug[slug], ana)
    listed = [row["slug"] for row in browser.get(MINE).json()]
    assert listed == ["zeta", "org-a", "a-gemela", "org-b"]  # por nombre y, a igualdad, por slug
    other = Client()
    sign_in(other, luis)
    assert [row["slug"] for row in other.get(MINE).json()] == ["org-b"]  # cada cual, las suyas


def test_expired_sessions_are_purged() -> None:
    old = timezone.now() - timedelta(days=1)
    Session.objects.create(session_key="a" * 32, session_data="x", expire_date=old)
    Session.objects.create(
        session_key="b" * 32,
        session_data="x",
        expire_date=timezone.now() + timedelta(hours=1),
    )
    assert purge_expired_sessions() == 1
    entry = settings.CELERY_BEAT_SCHEDULE["accounts.purge_expired_sessions"]
    assert isinstance(entry["schedule"], crontab)  # hora fija: no depende del arranque de beat
    assert (
        purge_expired_sessions.name == entry["task"]
        and purge_expired_sessions.tenancy == "platform"
    )
    assert list(Session.objects.values_list("session_key", flat=True)) == ["b" * 32]


def test_logout_invalidates_the_session_and_is_audited(
    ana: User, browser: Client, migrator: psycopg.Connection[Any]
) -> None:
    assert reply(send(browser, "POST", LOGOUT)) == NO_SESSION  # sin sesión no hay nada que cerrar
    login(browser)
    stolen = browser.cookies["crm_session"].value
    assert browser.post(LOGOUT).status_code == 403  # sin token CSRF
    assert browser.get(SESSION).status_code == 200  # y la sesión sigue
    closed = send(browser, "POST", LOGOUT)
    assert closed.status_code == 204 and closed.cookies["crm_session"].value == ""  # y la cookie
    assert Session.objects.count() == 0 and reply(browser.get(SESSION)) == NO_SESSION
    thief = Client()
    thief.cookies["crm_session"] = stolen  # la cookie antigua ya no vale
    assert reply(thief.get(SESSION)) == NO_SESSION
    assert [row[0] for row in audit(migrator)] == ["auth.login.succeeded", "auth.logout"]
    assert audit(migrator)[1][2:4] == ("USER", ana.pk) and audit(migrator)[1][8:] == LOCAL


def age(**marks: int) -> Session:
    """Como si hubiera pasado el tiempo: cada marca queda esos segundos atrás."""
    row = Session.objects.get()
    data = {**row.get_decoded(), **{mark: int(time.time()) - ago for mark, ago in marks.items()}}
    Session.objects.filter(pk=row.pk).update(session_data=Session.objects.encode(data))
    return row


def test_activity_renews_the_session_but_not_on_every_request(ana: User, browser: Client) -> None:
    first = login(browser).cookies["crm_session"]
    age(auth_at=3600, seen_at=6 * 60)
    started = Session.objects.get().get_decoded()[services.AUTH_AT]
    Session.objects.update(expire_date=timezone.now() + timedelta(hours=1))
    with CaptureQueriesContext(connection) as queries:
        active = browser.get(SESSION)
    writes = [q for q in queries if q["sql"].startswith("UPDATE") and "django_session" in q["sql"]]
    assert active.status_code == 200 and len(writes) == 1  # una escritura, antes de la vista
    cookie = active.cookies["crm_session"]  # la cookie también se renueva: la misma del login
    assert cookie.value == first.value and int(cookie["max-age"]) == 12 * 3600
    for attribute in ("httponly", "samesite", "path", "domain", "secure"):
        assert cookie[attribute] == first[attribute], attribute
    renewed = Session.objects.get()
    assert (renewed.expire_date - timezone.now()).total_seconds() > 11 * 3600
    assert renewed.get_decoded()[services.AUTH_AT] == started  # el inicio no se mueve
    again = browser.get(SESSION)
    assert again.status_code == 200 and "crm_session" not in again.cookies
    assert Session.objects.get().expire_date == renewed.expire_date  # sin escritura por petición
    Session.objects.update(expire_date=timezone.now() - timedelta(seconds=1))
    assert reply(browser.get(SESSION)) == NO_SESSION  # inactividad: caducada, no hay sesión


def test_the_renewed_cookie_keeps_the_production_shape(ana: User, settings: Any) -> None:
    settings.SESSION_COOKIE_NAME, settings.SESSION_COOKIE_SECURE = "__Host-crm_session", True
    client = Client()
    sign_in(client, ana)
    age(seen_at=6 * 60)
    renewed = client.get(SESSION)
    assert "crm_session" not in renewed.cookies
    cookie = renewed.cookies["__Host-crm_session"]
    assert cookie["secure"] and cookie["httponly"] and cookie["samesite"] == "Lax"
    assert cookie["path"] == "/" and not cookie["domain"]


def test_a_session_ends_seven_days_after_login_whatever_the_activity(
    ana: User, browser: Client
) -> None:
    week = 7 * 24 * 3600
    login(browser)
    age(auth_at=week - 60, seen_at=6 * 60)
    assert browser.get(SESSION).status_code == 200  # dentro del límite, con actividad reciente
    key = age(auth_at=week + 1, seen_at=0).pk
    ended = browser.get(SESSION)
    assert reply(ended) == NO_SESSION and ended.cookies["crm_session"].value == ""
    assert not Session.objects.filter(pk=key).exists()  # se cierra, no solo se ignora


@pytest.mark.parametrize("mark", [None, "ayer", 12.5])
def test_a_session_without_a_valid_start_mark_is_treated_as_too_old(ana: User, mark: Any) -> None:
    client = Client()
    client.force_login(ana)  # una sesión que no nació en el login: sin marcas
    if mark is not None:
        session = client.session
        session[services.AUTH_AT] = mark
        session.save()
    ended = client.get(SESSION)
    assert reply(ended) == NO_SESSION and ended.cookies["crm_session"].value == ""
    assert Session.objects.count() == 0


def test_the_lifetime_is_checked_before_the_tenant_is_resolved(
    ana: User, browser: Client, settings: Any
) -> None:
    order = [name.rsplit(".", 1)[-1] for name in base.MIDDLEWARE]
    assert order.index("SessionLifetimeMiddleware") < order.index("TenantResolutionMiddleware")
    assert order.index("AuthenticationMiddleware") < order.index("SessionLifetimeMiddleware")
    settings.ROOT_URLCONF = "tests.urls"  # con una ruta de tenant real
    login(browser)
    assert browser.get("/api/v1/o/org-a/widgets/").status_code == 200
    key = age(auth_at=7 * 24 * 3600 + 1).pk
    assert reply(browser.get("/api/v1/o/org-a/widgets/")) == NO_SESSION  # la primera petición
    assert not Session.objects.filter(pk=key).exists()


def test_a_session_deleted_while_being_renewed_ends_in_401_not_in_an_error(
    ana: User, browser: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    login(browser)
    age(seen_at=6 * 60)
    save = SessionStore.save

    def closed_elsewhere(self: SessionStore, must_create: bool = False) -> None:
        Session.objects.all().delete()  # otra pestaña cierra la sesión justo antes
        save(self, must_create)

    monkeypatch.setattr(SessionStore, "save", closed_elsewhere)
    ended = browser.get(SESSION)
    assert reply(ended) == NO_SESSION and ended.cookies["crm_session"].value == ""


def test_a_passing_database_error_while_renewing_does_not_close_the_session(
    ana: User, browser: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    login(browser)
    age(seen_at=6 * 60)
    save, failures = (
        Session.save,
        [OperationalError("canceling statement due to statement timeout")],
    )

    def flaky(self: Session, *args: Any, **kwargs: Any) -> None:
        if failures:
            raise failures.pop()
        save(self, *args, **kwargs)

    monkeypatch.setattr(Session, "save", flaky)
    browser.raise_request_exception = False
    failed = browser.get(SESSION)
    assert failed.status_code == 500 and "crm_session" not in failed.cookies  # la petición falla
    assert Session.objects.count() == 1 and browser.get(SESSION).status_code == 200  # la sesión no


def test_a_failed_login_over_a_session_about_to_be_renewed_keeps_it(
    ana: User, browser: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    login(browser)
    before = browser.cookies["crm_session"].value
    age(seen_at=6 * 60)

    def broken(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("auditoría no disponible")

    monkeypatch.setattr(platform, "record", broken)
    browser.raise_request_exception = False
    failed = login(browser)  # autentica, pero no se puede auditar: se deshace
    monkeypatch.undo()
    assert failed.status_code == 500 and "crm_session" not in failed.cookies  # ni la clave deshecha
    assert browser.get(SESSION).status_code == 200
    assert list(Session.objects.values_list("session_key", flat=True)) == [before]


def test_logout_does_not_depend_on_the_audit(
    ana: User, browser: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("auditoría no disponible")

    reports: list[Any] = []
    spy = SimpleNamespace(capture_exception=lambda error, context=None: reports.append(context))
    monkeypatch.setattr(reporting, "reporter", lambda: spy)
    login(browser)
    monkeypatch.setattr(platform, "record", broken)
    assert send(browser, "POST", LOGOUT).status_code == 204
    assert Session.objects.count() == 0
    assert reports == [{"action": "auth.logout"}]  # sin auditar, pero no en silencio
    monkeypatch.undo()
    login(browser)
    monkeypatch.setattr(platform, "record", broken)
    monkeypatch.setattr(reporting, "reporter", broken)  # ni un reporte roto la mantiene abierta
    assert send(browser, "POST", LOGOUT).status_code == 204 and Session.objects.count() == 0


def test_the_session_of_a_deactivated_user_is_destroyed(ana: User, browser: Client) -> None:
    login(browser)
    User.objects.filter(pk=ana.pk).update(is_active=False)
    gone = browser.get(SESSION)
    assert reply(gone) == NO_SESSION and Session.objects.count() == 0  # no solo se ignora
    assert gone.cookies["crm_session"].value == ""  # y el navegador suelta la cookie
    User.objects.filter(pk=ana.pk).update(is_active=True)
    assert reply(browser.get(SESSION)) == NO_SESSION  # reactivar no la devuelve


def test_revoking_ends_every_open_session_of_that_user_and_only_those(
    ana: User, browser: Client
) -> None:
    login(browser)
    other, third, luis = Client(), Client(), make_user(email="luis@example.com")
    sign_in(other, ana)
    sign_in(third, luis)
    with pytest.raises(RuntimeError), transaction.atomic():
        services.revoke_sessions(ana.pk)
        raise RuntimeError("quien llamaba deshizo su transacción")
    assert browser.get(SESSION).status_code == other.get(SESSION).status_code == 200
    services.revoke_sessions(ana.pk)
    for client in (browser, other):  # todas las suyas, en la siguiente petición de cada una
        ended = client.get(SESSION)
        assert reply(ended) == NO_SESSION and ended.cookies["crm_session"].value == ""
    assert third.get(SESSION).status_code == 200  # la de otro usuario no cambia
    assert [row.get_decoded()["_auth_user_id"] for row in Session.objects.all()] == [str(luis.pk)]
    assert User.objects.get(pk=luis.pk).session_epoch == 0
    late = Client()
    sign_in(late, ana)  # `ana` se leyó antes de revocar: como un login simultáneo, no sobrevive
    assert reply(late.get(SESSION)) == NO_SESSION
    assert login(browser).status_code == 200  # puede volver a entrar: sesión de la época nueva
    assert browser.get(SESSION).status_code == 200
    assert Session.objects.get(pk=browser.session.session_key).get_decoded()[services.EPOCH] == 1


def test_a_session_without_its_epoch_is_treated_as_revoked(ana: User) -> None:
    client = Client()
    client.force_login(ana)  # una sesión que no nació en el login
    session = client.session
    session[services.AUTH_AT] = session[services.SEEN_AT] = int(time.time())
    session.save()
    ended = client.get(SESSION)
    assert reply(ended) == NO_SESSION and Session.objects.count() == 0

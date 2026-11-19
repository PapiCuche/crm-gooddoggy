"""F2-130b2: cada regla de aceptar una invitación (ADR-020 §2, §4 y §6) con una prueba que falla si
se quita. Carreras con hilos y conexiones propias, como `crm_app`; el resto, por HTTP."""

import json
from typing import Any

import psycopg
import pytest
from django.test import Client

from apps.access.services import grant_permission
from apps.accounts.models import User
from apps.audit import platform
from apps.members import invitation_links
from apps.members import services as members
from apps.organizations.models import OrganizationMembership
from tests import test_invitation_accept as accepting
from tests.factories import make_user
from tests.test_authorization import give
from tests.test_invitation_accept import AUDITED, DENIALS, NEW, STATE, held, q, reasons, take
from tests.test_invitation_preview import REFUSED, ROWS, ask, heads, link, said
from tests.test_memberships import join
from tests.test_self_context import signed

real_stack, browser, world, rbac = (
    accepting.real_stack,
    accepting.browser,
    accepting.world,
    accepting.rbac,
)
team, roomy = accepting.team, accepting.roomy
pytestmark = pytest.mark.usefixtures("tenant_db")
INVITER: dict[str, str | None] = {"users.invite": None, "users.manage": None}
INVALID = {"\x01", "\x1b", "\x7f", "\x85", "\n", "\t", "\u2028", "\u200b", "\u200f", "\u061c"}
BIDI = {"\u202a", "\u202e", "\u2066", "\u2069"}  # incrustan o aíslan: reordenan lo que se ve


def unchanged(migrator: psycopg.Connection[Any]) -> Any:
    return migrator.execute(STATE).fetchone(), migrator.execute(ROWS).fetchall()


def test_a_membership_another_connection_creates_meanwhile_makes_the_accept_wait_and_refuse(
    team: Any, migrator: psycopg.Connection[Any]
) -> None:
    ines = make_user(email="ines@cliente.pe")
    secret = link(team.a, "ines@cliente.pe", team.ana, role_ids=[team.target.pk])
    before = unchanged(migrator)

    def other(_: Any) -> Any:  # otra conexión le da una membresía y no confirma todavía
        return OrganizationMembership.objects.create(user=ines, status="SUSPENDED").pk

    out = held(team.a, other, lambda: take(signed(ines), secret, **NEW))
    assert said(out["second"]) == REFUSED  # esperó el índice único y se deshizo entera
    assert reasons(migrator) == [{"reason": "member"}]
    after = unchanged(migrator)[0]
    assert (after[0], after[1] - 1, *after[2:]) == before[0]  # solo la membresía de la otra
    assert migrator.execute(ROWS).fetchall() == before[1]  # la invitación sigue pendiente
    assert q(migrator, AUDITED) == []


def test_an_existing_account_is_audited_as_not_created(
    team: Any, migrator: psycopg.Connection[Any]
) -> None:
    ines = make_user(email="ines@cliente.pe")
    secret = link(team.a, "ines@cliente.pe", team.ana, role_ids=[team.target.pk])
    assert said(take(signed(ines), secret, **NEW))[0] == 200
    (row,) = q(migrator, AUDITED)
    assert (row[5]["user_created"], row[5]["user_id"]) == (False, str(ines.pk))


def test_an_inviter_whose_account_was_deactivated_cannot_grant(
    team: Any, migrator: psycopg.Connection[Any]
) -> None:
    secret = link(team.a, "nora@cliente.pe", team.luis, role_ids=[team.target.pk])
    User.objects.filter(pk=team.luis.pk).update(is_active=False)  # la membresía sigue ACTIVE
    before = unchanged(migrator)
    assert said(take(Client(), secret, **NEW)) == REFUSED
    assert unchanged(migrator) == before
    assert reasons(migrator) == [{"reason": "inviter", "denied": "membership"}]


@pytest.mark.parametrize("change", ["suspend_inviter", "sensitive_grant"])
def test_an_rbac_change_held_by_another_connection_is_seen_by_the_waiting_accept(
    team: Any, migrator: psycopg.Connection[Any], change: str
) -> None:
    rita = make_user(email="rita@example.com")  # invita sin ser Owner, y ana puede suspenderla
    give(team.a, m_rita := join(team.a, rita).pk, INVITER, code="rita")
    secret = link(team.a, "nora@cliente.pe", rita, role_ids=[team.target.pk])
    before = unchanged(migrator)

    def suspend(tenant: Any) -> Any:  # toma el bloqueo de RBAC y no confirma todavía
        return members.set_member_status(tenant, membership_id=m_rita, status="SUSPENDED")

    def sensitive(tenant: Any) -> Any:  # `users.manage` es sensible y rita no es Owner
        return grant_permission(tenant, role_id=team.target.pk, code="users.manage", scope=None)

    first = suspend if change == "suspend_inviter" else sensitive
    out = held(team.a, first, lambda: take(Client(), secret, **NEW), by=team.ana)
    assert said(out["second"]) == REFUSED
    denied = "membership" if change == "suspend_inviter" else "sensitive"
    assert reasons(migrator) == [{"reason": "inviter", "denied": denied}]
    assert unchanged(migrator)[0][:3] == before[0][:3]  # ni cuenta ni membresía
    assert migrator.execute(ROWS).fetchall() == before[1]  # la invitación sigue pendiente


def test_a_platform_audit_that_fails_changes_no_answer_and_is_reported(
    team: Any, browser: Client, migrator: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = link(team.a, "nora@cliente.pe", team.ana, role_ids=[team.target.pk])
    preview, reported = ask(browser, "x"), list[tuple[type, str]]()

    def broken(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("la auditoría de plataforma no responde")

    monkeypatch.setattr(platform, "record", broken)
    monkeypatch.setattr(invitation_links, "unaudited", lambda e, a: reported.append((type(e), a)))
    refused = take(Client(), "x", **NEW)
    assert (refused.content, heads(refused)) == (preview.content, heads(preview))
    weak = take(Client(), secret, first_name="Nora", password="corta")  # noqa: S106
    assert said(weak)[1]["code"] == "VALIDATION_ERROR"
    assert reported == [(RuntimeError, "invitation.accept.denied")] * 2
    assert migrator.execute(DENIALS).fetchall() == []
    assert said(take(Client(), secret, **NEW))[0] == 200


@pytest.mark.parametrize("field", ["first_name", "last_name"])
def test_a_password_equal_to_a_name_is_refused(
    team: Any, migrator: psycopg.Connection[Any], field: str
) -> None:
    secret = link(team.a, "nora@cliente.pe", team.ana, role_ids=[team.target.pk])
    names = {"first_name": "Valentina Quispe", "last_name": "Huamanchumo Rivas"}
    weak = take(Client(), secret, **names, password=names[field])
    status, body = said(weak)
    assert (status, body["code"]) == (400, "VALIDATION_ERROR")
    assert [error["code"] for error in body["fields"]["password"]] == ["password_too_similar"]
    assert list(body["fields"]) == ["password"] and names[field].encode() not in weak.content
    assert reasons(migrator) == [{"reason": "password"}]


def test_each_refused_password_carries_the_code_of_its_validator(
    team: Any, migrator: psycopg.Connection[Any]
) -> None:
    secret = link(team.a, "nora@cliente.pe", team.ana, role_ids=[team.target.pk])
    cases = {
        "": ["password_too_short"],
        "abc": ["password_too_short", "password_too_common"],
        "password1234": ["password_too_common"],
        "123456789012": ["password_entirely_numeric"],
        "nora@cliente.pe": ["password_too_similar"],
    }
    for password, codes in cases.items():
        answer = take(Client(), secret, **NEW | {"password": password})
        fields = said(answer)[1]["fields"]
        assert [error["code"] for error in fields["password"]] == codes, password
        assert list(fields) == ["password"] and b"cliente" not in answer.content
    first = take(Client(), secret, **NEW | {"first_name": "Ra\u202equel", "last_name": "\x07"})
    named = said(first)[1]["fields"]
    assert {field: [e["code"] for e in errors] for field, errors in named.items()} == {
        "first_name": ["invalid"], "last_name": ["invalid"]
    }  # fmt: skip
    blank = said(take(Client(), secret, **NEW | {"first_name": "- ."}))[1]["fields"]
    assert blank == {"first_name": [{"code": "required", "message": "Escribe tu nombre."}]}
    assert reasons(migrator) == [{"reason": "password"}] * len(cases) + [{"reason": "name"}] * 2


@pytest.mark.parametrize("field", ["first_name", "last_name"])
def test_a_name_with_control_or_direction_characters_is_refused_without_echoing_it(
    team: Any, migrator: psycopg.Connection[Any], field: str
) -> None:
    secret = link(team.a, "nora@cliente.pe", team.ana, role_ids=[team.target.pk])
    before = unchanged(migrator)
    for char in sorted(INVALID | BIDI):
        sent = NEW | {field: f"Ra{char}quel"}
        answer = take(Client(), secret, **sent)
        status, body = said(answer)
        assert (status, body["code"], list(body["fields"])) == (400, "VALIDATION_ERROR", [field])
        assert "Ra" not in json.dumps(body) and b"quel" not in answer.content, repr(char)
    assert unchanged(migrator) == before  # el enlace no se gasta
    assert reasons(migrator) == [{"reason": "name"}] * len(INVALID | BIDI)
    stored = json.dumps(migrator.execute(DENIALS).fetchall(), default=str)
    assert "quel" not in stored


def test_a_name_needs_something_to_read_and_is_stored_tidy(
    team: Any, migrator: psycopg.Connection[Any]
) -> None:
    secret = link(team.a, "nora@cliente.pe", team.ana, role_ids=[team.target.pk])
    for blank in ("\u3164\u3164", "- .", "\u00a0"):  # rellenos, signos, un espacio
        answer = take(Client(), secret, **NEW | {"first_name": blank})
        assert list(said(answer)[1]["fields"]) == ["first_name"], repr(blank)
    tidy = {"first_name": "Ana\u00a0  Mari\u0301a", "last_name": "de\u3000la  Cruz"}
    assert said(take(Client(), secret, **NEW | tidy))[0] == 200
    nora = User.objects.get(email="nora@cliente.pe")
    assert (nora.first_name, nora.last_name) == ("Ana María", "de la Cruz")  # NFC, un espacio

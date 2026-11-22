"""F8-08l (#1046; ADR-034 §8, decisión del propietario del 2026-11-21 en #771): el texto de traspaso
de cada organización. `GET`/`PUT …/ai/handoff-reply/` y `apps.ai_agents.replies`: primero
`ai.manage`, después el segundo factor reciente; el `PUT` en el tope de escrituras; auditoría
`ai.handoff_reply.changed` sin el texto; la organización, la de la ruta. Validado al guardar y al
usarlo (`detectors.reply_problems`); sin texto guardado, el inicial del propietario. Y el runtime y
la ruta de sugerir proponen el de la organización. PostgreSQL con `crm_app`; datos ficticios."""

import importlib
import logging
import pathlib
import pkgutil
import tomllib
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest
from django.db import DatabaseError, connection
from django.test import Client

from apps.access.selectors import AccessDenied
from apps.ai_agents import detectors, replies
from apps.ai_agents.api.handoff import PROBLEMS
from apps.ai_agents.models import HANDOFF_MAX
from apps.organizations.models import OrganizationMembership as Membership
from core.api.step_up import STEP_UP_REQUIRED
from core.tenancy.scope import tenant_scope
from tests.test_ai_copilot_context import say
from tests.test_ai_copilot_runtime import PLAIN, desk, run
from tests.test_ai_copilot_suggest_api import ask
from tests.test_ai_gateway import world
from tests.test_ai_provider_accounts import crm, real_stack
from tests.test_ai_records_retention import events
from tests.test_authorization import give
from tests.test_channel_accounts_api import signed
from tests.test_conversations_api import UNAUTHENTICATED
from tests.test_member_branch import DENIED
from tests.test_memberships import ctx
from tests.test_self_context import NOT_FOUND, reply
from tests.test_self_context import signed as plain
from tests.test_team_writes import send

__all__ = ["crm", "desk", "real_stack", "world"]  # los fixtures, para pytest
pytestmark = pytest.mark.usefixtures("tenant_db")
DB = psycopg.Connection[Any]
URL = "/api/v1/o/{}/ai/handoff-reply/"
OWNER = (  # la decisión del propietario (#771), letra por letra
    "Entiendo. Voy a solicitar que una persona de nuestro equipo continúe contigo para ayudarte "
    "directamente. Tu conversación quedará disponible para que pueda revisar el contexto y no "
    "tengas que repetir toda la información"
)
MINE = "Gracias por tu paciencia. Una persona del equipo de Good Doggy seguirá contigo por aquí."
ROWS = "SELECT organization_id, text, changed_by_user_id FROM ai_handoff_replies ORDER BY 1"
AUDIT = ("SELECT organization_id, actor_id, result, changes, metadata FROM audit_logs"
         " WHERE action = 'ai.handoff_reply.changed' ORDER BY occurred_at, id")  # fmt: skip
STORE = "INSERT INTO ai_handoff_replies (organization_id, text, changed_at) VALUES (%s, %s, now())"
COUNTED = "SELECT key, failures FROM login_throttles WHERE key LIKE 'aik:%'"


@pytest.fixture(autouse=True)
def no_limit(settings: Any) -> None:
    settings.AI_ACCOUNT_THROTTLE = (60, 600)


def put(client: Client, text: Any, org: str = "org-a", **over: Any) -> Any:
    return send(client, "put", URL.format(org), {"text": text} | over)


def test_the_owner_text_is_the_initial_one_and_passes_every_check() -> None:
    assert detectors.handoff_reply() == OWNER and detectors.reply_problems(OWNER) == ()
    # la frase de la conversación es la única excepción de los detectores; sin ella, se marca
    assert detectors.reply_problems(OWNER.replace("conversación quedará", "sesión estará")) == (
        "AVAILABILITY",
    )


def test_the_owner_reads_the_initial_text_then_saves_one_and_it_is_audited_without_it(
    crm: SimpleNamespace, migrator: DB, caplog: pytest.LogCaptureFixture
) -> None:
    ana = signed(crm.ana)
    first = ana.get(URL.format("org-a"))
    assert first.status_code == 200
    assert first.json() == {"text": OWNER, "initial": True, "changed_at": None}
    with caplog.at_level(logging.DEBUG):
        done = put(ana, MINE, organization_id=str(crm.b))  # la organización, de la ruta
        again = put(ana, MINE)  # el mismo: nada que cambiar ni auditar
        other = put(ana, OWNER)
    assert done.status_code == again.status_code == other.status_code == 200
    assert done.json()["text"] == MINE and done.json()["initial"] is False
    assert done.json()["changed_at"] is not None
    assert ana.get(URL.format("org-a")).json()["text"] == OWNER
    assert migrator.execute(ROWS).fetchall() == [(crm.a, OWNER, crm.ana.pk)]
    lengths = [{"length": [len(OWNER), len(MINE)]}, {"length": [len(MINE), len(OWNER)]}]
    assert migrator.execute(AUDIT).fetchall() == [
        (crm.a, crm.ana.pk, "SUCCESS", lengths[0], {}),
        (crm.a, crm.ana.pk, "SUCCESS", lengths[1], {}),
    ]
    logged = caplog.text + str([r.__dict__ for r in caplog.records])
    everything = str(migrator.execute("SELECT a::text FROM audit_logs a").fetchall())
    assert "Good Doggy" not in logged + everything and "Entiendo" not in everything


def test_the_permission_goes_before_the_second_factor_and_nothing_changes_without_both(
    crm: SimpleNamespace, migrator: DB
) -> None:
    url = URL.format("org-a")
    give(crm.a, crm.m["vera"], {"ai.use": None})  # usa el copiloto; no lo administra
    for vera in (plain(crm.vera), signed(crm.vera)):  # vendedora: sin `ai.manage`
        assert reply(vera.get(url)) == reply(put(vera, MINE)) == reply(put(vera, "S/ 30")) == DENIED
    late = plain(crm.ana)  # Owner sin segundo factor reciente
    for answer in (late.get(url), put(late, MINE)):
        assert (answer.status_code, answer.json()["code"]) == (403, STEP_UP_REQUIRED)
    assert reply(Client().get(url)) == reply(put(Client(), MINE)) == UNAUTHENTICATED
    with tenant_scope(ctx(crm.a, crm.vera)) as seller:
        with pytest.raises(AccessDenied):
            replies.read(seller)
        with pytest.raises(AccessDenied):  # antes de mirar el texto
            replies.change(seller, "S/ 30")
    assert migrator.execute(ROWS).fetchall() == [] and migrator.execute(AUDIT).fetchall() == []


def test_each_organization_has_its_own_text(crm: SimpleNamespace, migrator: DB) -> None:
    give(crm.b, crm.m_luis, {"ai.manage": None})
    ana, luis = signed(crm.ana), signed(crm.luis)
    assert put(luis, MINE, "org-b").status_code == 200
    assert ana.get(URL.format("org-a")).json()["text"] == OWNER  # el de B no llega a A
    assert reply(put(luis, "Otro texto", "org-a")) == NOT_FOUND  # no es miembro de A
    assert reply(luis.get(URL.format("org-a"))) == NOT_FOUND
    assert put(ana, "Una persona del equipo seguirá contigo.").status_code == 200
    assert luis.get(URL.format("org-b")).json()["text"] == MINE
    assert migrator.execute(ROWS).fetchall() == sorted(
        [(crm.a, "Una persona del equipo seguirá contigo.", crm.ana.pk), (crm.b, MINE, crm.luis.pk)]
    )
    with tenant_scope(ctx(crm.a)), connection.cursor() as cursor:  # RLS: solo la suya
        cursor.execute("SELECT organization_id FROM ai_handoff_replies")
        assert cursor.fetchall() == [(crm.a,)]


CASES = [  # revisión de #1054: más prisas, la frase anclada y solo letras latinas
    ("Te atendemos en 24 horas", ["DELIVERY"]), ("Somos un equipo de 3 personas", ["FIGURE"]),
    ("Te atendemos en 5 minutos", ["FIGURE", "SOON"]), ("Te escribimos mañana", ["DELIVERY"]),
    ("Una persona te atiende de inmediato", ["DELIVERY"]),
    ("Una persona te responde enseguida", ["SOON"]), ("Te atenderán en unos minutos", ["SOON"]),
    ("Pagas en cuotas con tarjeta", ["TERMS"]), ("Mira tiendagooddoggy.pe", ["LINK"]),
    ("Revisa [verificar]", ["MARKUP"]), ("Te damos un descuento", ["DISCOUNT"]),
    ("El collar está disponible", ["AVAILABILITY"]),
    ("Tu conversación quedará disponible dos días", ["DELIVERY"]),
    ("Ya te atiende una persona", ["SOON"]), ("En este momento te atiende una persona", ["SOON"]),
    ("En un ratito te escriben", ["SOON"]), ("Prontito te escribe una persona", ["SOON"]),
    ("Te atienden sin esperas", ["SOON"]), ("Te atienden de una", ["SOON"]),
    ("Luego luego te atienden", ["SOON"]), ("ASAP te atienden", ["SOON"]),
    # la excepción, solo con su sujeto al empezar una frase
    ("El producto de tu conversación quedará disponible", ["AVAILABILITY"]),
    ("Tu pedido y tu conversación quedará disponible", ["AVAILABILITY"]),
    ("El collar quedará disponible", ["AVAILABILITY"]),
    ("Gracias. Conversación quedará disponible", ["AVAILABILITY"]),
    ("Te escribimos h\u043ey", ["SCRIPT"]), ("Ho\u200bla", ["BODY"]), ("\u202eHola", ["BODY"]),
    ("x" * (HANDOFF_MAX + 1), ["TOO_LONG"]),
]  # fmt: skip


@pytest.mark.parametrize(("text", "codes"), CASES)
def test_a_text_with_a_figure_a_deadline_a_hurry_or_a_link_is_refused_and_never_echoed(
    crm: SimpleNamespace, migrator: DB, text: str, codes: list[str]
) -> None:
    refusal = put(signed(crm.ana), text)
    assert refusal.status_code == 400, refusal.content
    assert refusal.json()["fields"] == {
        "text": [{"code": code, "message": PROBLEMS[code]} for code in codes]
    }
    assert text not in refusal.content.decode()
    with tenant_scope(ctx(crm.a, crm.ana)) as tenant, pytest.raises(replies.InvalidReply):
        replies.change(tenant, text)
    assert migrator.execute(ROWS).fetchall() == [] and migrator.execute(AUDIT).fetchall() == []


@pytest.mark.parametrize("text", ["", "   ", None, 7])
def test_an_empty_or_wrong_type_text_is_a_400(
    crm: SimpleNamespace, migrator: DB, text: Any
) -> None:
    refusal = put(signed(crm.ana), text)
    assert refusal.status_code == 400 and list(refusal.json()["fields"]) == ["text"]
    assert migrator.execute(ROWS).fetchall() == []


def test_the_change_counts_in_the_shared_write_limit_and_reading_does_not(
    crm: SimpleNamespace, migrator: DB, settings: Any
) -> None:
    settings.AI_ACCOUNT_THROTTLE = (2, 600)
    ana = signed(crm.ana)
    answers = [put(ana, text) for text in (MINE, OWNER, MINE)]
    assert [a.status_code for a in answers] == [200, 200, 429]
    assert int(answers[-1]["Retry-After"]) > 0 and ana.get(URL.format("org-a")).status_code == 200
    assert migrator.execute(COUNTED).fetchall() == [(f"aik:usr:{crm.ana.pk}", 3)]


def test_crm_app_writes_only_the_text_who_and_when_of_its_own_row(
    crm: SimpleNamespace, migrator: DB
) -> None:
    migrator.execute(STORE, [crm.b, MINE])
    with tenant_scope(ctx(crm.a, crm.ana)) as tenant:
        replies.change(tenant, MINE)
    for statement, args in (
        ("DELETE FROM ai_handoff_replies", []), ("TRUNCATE ai_handoff_replies", []),
        ("UPDATE ai_handoff_replies SET organization_id = %s", [crm.b]),
        ("UPDATE ai_handoff_replies SET id = uuidv7()", []),
        (STORE, [crm.b, MINE]),  # la de otra organización: RLS
    ):  # fmt: skip
        with tenant_scope(ctx(crm.a)), pytest.raises(DatabaseError):
            connection.cursor().execute(statement, args)
    for bad in ("", "x" * (HANDOFF_MAX + 1)):
        with tenant_scope(ctx(crm.a)), pytest.raises(DatabaseError, match="text_ck"):
            connection.cursor().execute("UPDATE ai_handoff_replies SET text = %s", [bad])
    with tenant_scope(ctx(crm.a)), connection.cursor() as cursor:
        cursor.execute("UPDATE ai_handoff_replies SET text = %s", [OWNER])
        assert cursor.rowcount == 1  # solo la suya
    assert [r[:2] for r in migrator.execute(ROWS).fetchall()] == sorted(
        [(crm.a, OWNER), (crm.b, MINE)]
    )


def test_the_runtime_proposes_the_organization_text_and_the_initial_one_without_it(
    desk: SimpleNamespace, migrator: DB
) -> None:
    say(desk, "Quiero hablar con un asesor")
    assert run(desk, PLAIN).text == OWNER
    migrator.execute(STORE, [desk.a, MINE])
    migrator.execute(STORE, [desk.b, "Texto de otra organización."])
    draft = run(desk, PLAIN)
    assert (draft.text, draft.findings, draft.handoff_reason) == (MINE, (), "ASKED_FOR_PERSON")
    saved = migrator.execute("SELECT text FROM ai_suggestions ORDER BY created_at").fetchall()
    assert saved == [(OWNER,), (MINE,)]


def test_a_stored_text_that_no_longer_passes_is_never_proposed(
    desk: SimpleNamespace, migrator: DB, caplog: pytest.LogCaptureFixture
) -> None:
    """Uno que la base aceptó (SQL directo, o los detectores cambiaron después) se valida al
    usarlo: se propone el inicial y el log lo avisa con los códigos, sin el texto."""
    say(desk, "Quiero hablar con un asesor")
    migrator.execute(STORE, [desk.a, "Te atendemos en 5 minutos"])
    with caplog.at_level(logging.WARNING):
        draft = run(desk, PLAIN)
    assert draft.text == OWNER
    [warned] = events(caplog, "ai.handoff_reply_invalid")
    assert warned["problems"] == ["FIGURE", "SOON"] and "minutos" not in caplog.text
    with tenant_scope(ctx(desk.a)):
        membership = Membership.objects.get(user=desk.ana).pk
    give(desk.a, membership, {"ai.manage": None})
    with tenant_scope(ctx(desk.a, desk.ana)) as tenant:
        shown = replies.read(tenant)
    assert (shown.text, shown.initial) == (OWNER, True) and shown.changed_at is not None


@pytest.mark.usefixtures("real_stack")
def test_the_suggest_route_answers_with_the_organization_text(
    desk: SimpleNamespace, migrator: DB
) -> None:
    say(desk, "Quiero hablar con una persona, por favor")
    migrator.execute(STORE, [desk.a, MINE])
    desk.fake.replies = [PLAIN]
    answer = ask(desk)
    assert answer.status_code == 201
    assert answer.json()["text"] == MINE
    assert answer.json()["handoff"] == {"requested": True, "reason": "ASKED_FOR_PERSON"}


def test_only_the_runtime_and_its_route_reach_the_handoff_text_even_indirectly() -> None:
    """`replies -> audit.services` está en `ignore_imports`: todo módulo de `ai_agents` está en el
    `forbidden` (con los indirectos), es `replies` o es del runtime y sus rutas."""
    config = tomllib.loads((pathlib.Path(__file__).parents[1] / "pyproject.toml").read_text())
    contracts = config["tool"]["importlinter"]["contracts"]
    [guard] = [c for c in contracts if c.get("protected_modules") == ["apps.ai_agents.replies"]]
    assert guard["allowed_importers"] == ["apps.ai_agents.copilot", "apps.ai_agents.api.handoff"]
    [deep] = [c for c in contracts if c.get("forbidden_modules") == ["apps.ai_agents.replies"]]
    assert deep["type"] == "forbidden" and not deep.get("allow_indirect_imports")
    assert not deep.get("ignore_imports")
    package = importlib.import_module("apps.ai_agents")
    found = {m.name for m in pkgutil.walk_packages(package.__path__, "apps.ai_agents.")}
    sources = deep["source_modules"]
    covered = {
        name for name in found if any(name == s or name.startswith(s + ".") for s in sources)
    }
    runtime = {"apps.ai_agents.copilot", "apps.ai_agents.api", "apps.ai_agents.api.urls",
               "apps.ai_agents.api.views", "apps.ai_agents.api.handoff"}  # fmt: skip
    assert found - covered - runtime == {"apps.ai_agents.replies"}

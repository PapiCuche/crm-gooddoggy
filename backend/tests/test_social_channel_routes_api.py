"""F7b-06a: las rutas de Messenger e Instagram (ADR-035 §3) con las garantías de WhatsApp (F7-02d,
F7-07c, F7-02e), sobre el stack real y la Graph API de la página simulada. Nunca sale el token."""

import json
import logging
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from django.test import Client

from apps.conversations import accounts, commands
from apps.conversations.api import channels
from core.api.step_up import STEP_UP_REQUIRED
from tests.meta_sim import transport
from tests.meta_sim.graph import TEST_TOKEN, FakeGraph
from tests.meta_sim.page_graph import FakePageGraph
from tests.meta_sim.social import IG_ACCOUNT_ID, PAGE_ID
from tests.meta_sim.webhooks import PHONE_NUMBER_ID, WABA_ID
from tests.test_channel_accounts import NEW_TOKEN, everything, run
from tests.test_channel_accounts import connect as connect_whatsapp
from tests.test_channel_accounts_api import signed
from tests.test_conversations_api import UNAUTHENTICATED
from tests.test_member_branch import DENIED
from tests.test_self_context import NOT_FOUND, reply
from tests.test_self_context import signed as plain
from tests.test_social_accounts import OTHER, Down, connect, crm, meta, real_stack
from tests.test_team_writes import send

__all__ = ["crm", "meta", "real_stack"]  # los fixtures, para pytest
pytestmark = pytest.mark.usefixtures("tenant_db")
DB = psycopg.Connection[Any]
BASE = "/api/v1/o/org-a/channels/"
PLATFORMS = pytest.mark.parametrize("platform", ["messenger", "instagram"])
REJECTED = "otro-token-de-la-api-falsa-0000"  # noqa: S105 — Meta no lo acepta


def body(platform: str, **over: Any) -> dict[str, Any]:
    ig = {"instagram_account_id": IG_ACCOUNT_ID} if platform == "instagram" else {}
    return {"name": "Tienda", "page_id": PAGE_ID, "access_token": TEST_TOKEN} | ig | over


def made(crm: SimpleNamespace, platform: str, org: str = "a") -> Any:
    return connect(crm, platform.upper(), org)[0]


def rotate(client: Client, platform: str, pk: Any, token: str = NEW_TOKEN) -> Any:
    return send(client, "post", f"{BASE}{platform}/{pk}/token/", {"access_token": token})


@PLATFORMS
def test_connect_rotate_and_disconnect_and_no_output_carries_the_token(
    crm: SimpleNamespace, meta: FakePageGraph, migrator: DB, caplog: pytest.LogCaptureFixture,
    platform: str,
) -> None:  # fmt: skip
    ana, instagram = signed(crm.ana), platform == "instagram"
    with caplog.at_level(logging.DEBUG):
        connected = send(ana, "post", f"{BASE}{platform}/", body(platform))
        account = connected.json()
        meta.token, meta.calls[:] = NEW_TOKEN, []
        rotated = rotate(ana, platform, account["id"])
        checked = list(meta.calls)
        gone = send(ana, "delete", f"{BASE}{platform}/{account['id']}/", None)
    assert connected.status_code == 201 and meta.subscribed.keys() == {PAGE_ID}
    assert (account["platform"], account["status"]) == (platform.upper(), "PENDING")
    ig = IG_ACCOUNT_ID if instagram else None
    assert (account["page_id"], account["instagram_account_id"]) == (PAGE_ID, ig)
    assert account["token_last_four"] == TEST_TOKEN[-4:] and "credential_id" not in account
    assert rotated.status_code == 200 and rotated.json()["token_last_four"] == NEW_TOKEN[-4:]
    assert rotated.json()["token_rotated_at"] is not None and gone.status_code == 204
    # la rotación se comprueba con `graph.page` (la misma página) y lee sus apps suscritas (exige
    # un rol en la página, F7b-06e), sin volver a suscribirla
    assert [(c["method"], c["path"]) for c in checked] == [
        ("GET", f"/v23.0/{PAGE_ID}"), ("GET", f"/v23.0/{PAGE_ID}/subscribed_apps"),
    ]  # fmt: skip
    assert "instagram_business_account" in checked[0]["query"]["fields"][0]
    outputs = b"".join(r.content for r in (connected, rotated, gone)).decode() + caplog.text
    for token in (TEST_TOKEN, NEW_TOKEN):
        assert token not in outputs + everything(migrator)
    audited = migrator.execute("SELECT action FROM audit_logs ORDER BY occurred_at").fetchall()
    assert [a for (a,) in audited if a.startswith("channel_account")] == [
        "channel_account.connected", "channel_account.credentials_rotated",
        "channel_account.disconnected",
    ]  # fmt: skip
    status = "SELECT status, deleted_at IS NOT NULL FROM channel_accounts WHERE id = %s"
    assert migrator.execute(status, [account["id"]]).fetchone() == ("DISCONNECTED", True)


@PLATFORMS
def test_the_permission_is_checked_before_the_body_the_lookup_and_meta(
    crm: SimpleNamespace, meta: FakePageGraph, platform: str
) -> None:
    account = made(crm, platform)
    meta.calls.clear()
    paths = (
        (f"{BASE}{platform}/", "post"),
        (f"{BASE}{platform}/{account.pk}/token/", "post"),
        (f"{BASE}{platform}/{account.pk}/", "delete"),
    )
    for who in (crm.vera, crm.sam, crm.bea):  # sin `channels.manage`
        for path, method in paths:
            assert reply(send(signed(who), method, path, {"basura": 1})) == DENIED, (who, path)
    for path, method in paths:
        assert reply(send(Client(enforce_csrf_checks=True), method, path, {})) == UNAUTHENTICATED
        other = path.replace("org-a", "org-b")  # ana no es miembro de B
        assert reply(send(signed(crm.ana), method, other, body(platform))) == NOT_FOUND
    unknown = send(signed(crm.ana), "post", f"{BASE}telegram/", body(platform))
    assert unknown.status_code == 404 and meta.calls == []  # nunca llega a `connect_social`


@PLATFORMS
def test_connecting_and_rotating_ask_for_a_recent_second_factor_and_disconnecting_does_not(
    crm: SimpleNamespace, meta: FakePageGraph, platform: str
) -> None:
    account = made(crm, platform)
    meta.calls.clear()
    ana = plain(crm.ana)  # el Owner, sin la verificación reciente del segundo factor
    for denied in (send(ana, "post", f"{BASE}{platform}/", body(platform)),
                   rotate(ana, platform, account.pk)):  # fmt: skip
        assert denied.status_code == 403 and denied.json()["code"] == STEP_UP_REQUIRED
    assert meta.calls == []
    assert send(ana, "delete", f"{BASE}{platform}/{account.pk}/", None).status_code == 204


@pytest.mark.parametrize(
    "target", ["other_org", "other_platform", "sandbox", "gone", "none", "whatsapp", "on_whatsapp"]
)
def test_an_account_that_is_not_yours_or_not_of_the_route_is_not_found(
    crm: SimpleNamespace, meta: FakePageGraph, monkeypatch: pytest.MonkeyPatch, target: str
) -> None:
    ana = signed(crm.ana)
    ids: dict[str, Any] = {
        "other_org": lambda: made(crm, "instagram", "b").pk,
        "other_platform": lambda: made(crm, "messenger").pk,  # por la ruta de Instagram
        "sandbox": lambda: run(crm.a, crm.ana, commands.ensure_sandbox_account).pk,
        "gone": lambda: made(crm, "instagram").pk,
        "none": uuid4,
        "whatsapp": lambda: connect_whatsapp(crm)[0].pk,  # por las rutas sociales
        "on_whatsapp": lambda: made(crm, "messenger").pk,  # por las de WhatsApp
    }
    if target == "whatsapp":  # se conecta con la Graph API de WhatsApp
        transport.install(monkeypatch, FakeGraph(numbers={PHONE_NUMBER_ID: WABA_ID}))
    pk = ids[target]()
    transport.install(monkeypatch, meta)  # la de la página, que cuenta lo que se le pregunta
    if target == "gone":
        assert send(ana, "delete", f"{BASE}instagram/{pk}/", None).status_code == 204
    meta.calls.clear()
    routes = {"whatsapp": ["messenger", "instagram"], "on_whatsapp": ["whatsapp"]}
    for route in routes.get(target, ["instagram"]):
        assert reply(rotate(ana, route, pk)) == NOT_FOUND, route
        assert reply(send(ana, "delete", f"{BASE}{route}/{pk}/", None)) == NOT_FOUND, route
    assert meta.calls == []  # a Meta no se le preguntó nada
    assert rotate(ana, "instagram", pk, "corto").status_code == 400  # el cuerpo, antes del 404


FIELDS = [("messenger", "page_id"), ("instagram", "instagram_account_id")]


@pytest.mark.parametrize(("platform", "field"), FIELDS)
def test_a_malformed_taken_and_foreign_account_get_the_very_same_answer(
    crm: SimpleNamespace, meta: FakePageGraph, migrator: DB, platform: str, field: str
) -> None:
    made(crm, platform)  # ya es de A
    ana, path = signed(crm.ana), f"{BASE}{platform}/"
    answers = {
        "malformed": reply(send(ana, "post", path, body(platform, **{field: "no-es-un-id"}))),
        "mine": reply(send(ana, "post", path, body(platform))),
        "theirs": reply(
            send(signed(crm.luis), "post", path.replace("org-a", "org-b"), body(platform))
        ),
    }
    assert len(set(answers.values())) == 1, answers  # la misma respuesta, byte a byte
    status, content = answers["mine"]
    assert status == 400 and list(json.loads(content)["fields"]) == [field]
    assert accounts.ACCOUNT_REFUSED in content.decode() and b"external_uq" not in content
    assert TEST_TOKEN not in everything(migrator)


@pytest.mark.parametrize(
    ("platform", "over", "fields"),
    [
        ("instagram", {"instagram_account_id": None}, {"instagram_account_id"}),
        ("messenger", {"instagram_account_id": IG_ACCOUNT_ID}, {"instagram_account_id"}),
        ("messenger", {"platform": "INSTAGRAM"}, {"platform"}),
        ("messenger", {"access_token": "corto", "page_id": "x"}, {"access_token", "page_id"}),
        ("instagram", {"organization_id": str(uuid4())}, {"organization_id"}),
    ],
)
def test_the_body_is_closed_and_strict_and_an_error_never_repeats_the_token(
    crm: SimpleNamespace, meta: FakePageGraph, platform: str, over: dict[str, Any],
    fields: set[str],
) -> None:  # fmt: skip
    wanted = {k: v for k, v in (body(platform) | over).items() if v is not None}
    response = send(signed(crm.ana), "post", f"{BASE}{platform}/", wanted)
    assert response.status_code == 400 and set(response.json()["fields"]) == fields
    assert TEST_TOKEN not in response.content.decode() and meta.calls == []


def test_rotating_checks_the_same_page_and_linked_account_and_revives_a_rejected_token(
    crm: SimpleNamespace, meta: FakePageGraph, migrator: DB, monkeypatch: pytest.MonkeyPatch,
    settings: Any,
) -> None:  # fmt: skip
    account, ana = made(crm, "instagram"), signed(crm.ana)
    run(crm.a, crm.ana, accounts.mark_token_invalid, channel_account_id=account.pk)
    live = "SELECT status, status_detail, credential_id FROM channel_accounts WHERE id = %s"
    before = migrator.execute(live, [account.pk]).fetchone()
    meta.pages[PAGE_ID] = OTHER  # la página ya no está vinculada a esa cuenta de Instagram
    linked = rotate(ana, "instagram", account.pk, TEST_TOKEN)
    assert (linked.status_code, linked.json()["code"]) == (400, "CHANNEL_CREDENTIALS_REJECTED")
    # F7b-06e: el texto de la página, nunca el de WhatsApp («ese número»)
    social = "Meta no aceptó ese token para esa página. Revisa el token, la página y la cuenta."
    assert linked.json()["message"] == social and "número" not in linked.content.decode()
    refused = rotate(ana, "instagram", account.pk, REJECTED)
    assert (refused.status_code, refused.json()["code"]) == (400, "CHANNEL_CREDENTIALS_REJECTED")
    transport.install(monkeypatch, Down())
    down = rotate(ana, "instagram", account.pk, TEST_TOKEN)
    assert (down.status_code, down.json()["code"]) == (503, "CHANNEL_PROVIDER_UNAVAILABLE")
    settings.META_ENABLED = False
    off = send(ana, "post", f"{BASE}messenger/", body("messenger"))
    assert (off.status_code, off.json()["code"]) == (409, "CHANNEL_NOT_ENABLED")
    assert off.json()["message"] == channels.SOCIAL_DISABLED
    assert migrator.execute(live, [account.pk]).fetchone() == before  # nada cambió
    assert before is not None and before[:2] == ("ERROR", "TOKEN_INVALID")
    settings.META_ENABLED, meta.pages[PAGE_ID] = True, IG_ACCOUNT_ID
    transport.install(monkeypatch, meta)
    revived = rotate(ana, "instagram", account.pk, TEST_TOKEN)
    assert revived.status_code == 200, revived.content
    assert (revived.json()["status"], revived.json()["status_detail"]) == ("CONNECTED", None)


def test_the_social_routes_share_the_rate_limit_of_the_whatsapp_routes(
    crm: SimpleNamespace, meta: FakePageGraph, settings: Any
) -> None:
    settings.CHANNEL_CHECK_THROTTLE = (3, 600)
    meta.token = REJECTED  # Meta rechaza el token: aun así cuenta
    ana = signed(crm.ana)
    asked = [
        send(ana, "post", f"{BASE}messenger/", body("messenger")).status_code,
        rotate(ana, "instagram", uuid4()).status_code,
        send(ana, "delete", f"{BASE}instagram/{uuid4()}/", None).status_code,
    ]
    assert asked == [400, 404, 404] and len(meta.calls) == 1
    for path, method in ((f"{BASE}instagram/", "post"), (f"{BASE}messenger/{uuid4()}/", "delete"),
                         (f"{BASE}whatsapp/{uuid4()}/", "delete")):  # fmt: skip
        limited = send(ana, method, path, body("instagram"))
        assert limited.status_code == 429 and limited.json()["code"] == "RATE_LIMITED", path
        assert int(limited["Retry-After"]) > 0
    assert len(meta.calls) == 1  # ni se preguntó a Meta


@PLATFORMS
@pytest.mark.parametrize("name", ["a\x07b", "a\nb", "a\u200bb", "a\u202eb"])
def test_a_name_with_control_or_invisible_characters_is_a_field_error_not_a_500(
    crm: SimpleNamespace, meta: FakePageGraph, migrator: DB, platform: str, name: str
) -> None:  # F7b-06e: antes, `accounts._name` lanzaba un `ValueError` que nadie capturaba
    response = send(signed(crm.ana), "post", f"{BASE}{platform}/", body(platform, name=name))
    assert response.status_code == 400 and list(response.json()["fields"]) == ["name"]
    assert name not in response.content.decode() and meta.calls == []
    assert migrator.execute("SELECT count(*) FROM channel_accounts").fetchone() == (0,)


class NoList(FakePageGraph):
    """Contesta 200 a las apps suscritas, pero sin la lista de `data`."""

    def handle(self, method: str, target: str, *args: Any) -> Any:
        if target.split("?")[0].endswith("/subscribed_apps") and method == "GET":
            return 200, {"Content-Type": "application/json"}, b'{"data": {}}'
        return super().handle(method, target, *args)


@PLATFORMS
def test_rotating_needs_a_role_on_the_page_like_connecting_and_changes_nothing_without_it(
    crm: SimpleNamespace, meta: FakePageGraph, migrator: DB, monkeypatch: pytest.MonkeyPatch,
    platform: str,
) -> None:  # fmt: skip
    account, ana = made(crm, platform), signed(crm.ana)
    live = "SELECT status, credential_id, version FROM channel_accounts WHERE id = %s"
    before = migrator.execute(live, [account.pk]).fetchone()
    meta.no_role, meta.token = {PAGE_ID}, NEW_TOKEN  # el token ve la página, sin rol en ella
    refused = rotate(ana, platform, account.pk)
    assert (refused.status_code, refused.json()["code"]) == (400, "CHANNEL_CREDENTIALS_REJECTED")
    weird = NoList(token=NEW_TOKEN)
    transport.install(monkeypatch, weird)
    odd = rotate(ana, platform, account.pk)
    assert (odd.status_code, odd.json()["code"]) == (503, "CHANNEL_PROVIDER_UNAVAILABLE")
    assert migrator.execute(live, [account.pk]).fetchone() == before  # nada cambió
    meta.no_role = set()
    transport.install(monkeypatch, meta)
    assert rotate(ana, platform, account.pk).status_code == 200

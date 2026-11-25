"""F7b-19: un 5xx de Meta es ambiguo con cualquier código, también con uno de límite (`130429`,
`4`…): nunca se reenvía, en WhatsApp y en la API de páginas. Y un rechazo que llega después de que
el propietario rotó o reconectó la cuenta no la degrada: lo decide la credencial del envío, bajo el
bloqueo de la cuenta. PostgreSQL real con `crm_app`; el simulador sustituye solo la red."""

import threading
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest
from django.db import connection

from apps.conversations import accounts, channels, meta_page, tasks, whatsapp
from apps.conversations.channels import AmbiguousChannelError, SendResult, TemporaryChannelError
from apps.conversations.models import ChannelAccount, Conversation
from apps.organizations.models import OrganizationMembership
from core.tenancy.scope import tenant_scope
from tests.conftest import migrator_conninfo
from tests.meta_sim import GRAPH_VERSION
from tests.meta_sim.graph import TEST_TOKEN
from tests.meta_sim.webhooks import PHONE_NUMBER_ID, WABA_ID
from tests.test_authorization import give
from tests.test_channel_health import account as number
from tests.test_conversation_commands import audit, outbound, world, writer
from tests.test_conversations_dispatch import Retrying, state
from tests.test_late_refusal import account_audits, answers, error, wait_for
from tests.test_memberships import ctx
from tests.test_social_outbound import talk as social
from tests.test_whatsapp_outbound import graph, redis, talk

__all__ = ["graph", "redis", "world", "writer"]  # los fixtures
pytestmark = pytest.mark.usefixtures("tenant_db")
SERVER = (500, 502, 503)


@pytest.mark.parametrize("status", SERVER)
@pytest.mark.parametrize("code", whatsapp.TOO_FAST)
def test_a_5xx_with_a_rate_limit_code_is_ambiguous_and_only_an_upload_retries(
    status: int, code: int
) -> None:
    with pytest.raises(AmbiguousChannelError):  # el envío: pudo haber salido
        whatsapp._refused(status, error(code), AmbiguousChannelError)
    with pytest.raises(TemporaryChannelError) as caught:  # la subida nunca llega al cliente
        whatsapp._refused(status, error(code), TemporaryChannelError)
    assert (caught.value.detail, caught.value.args) == (str(code), ())


@pytest.mark.parametrize("code", whatsapp.TOO_FAST)
def test_a_rate_limit_meta_refused_without_a_5xx_still_retries_with_its_code(code: int) -> None:
    for status in (400, 429, 499):
        with pytest.raises(TemporaryChannelError) as caught:
            whatsapp._refused(status, error(code), AmbiguousChannelError)
        assert caught.value.detail == str(code)
    with pytest.raises(TemporaryChannelError):  # un 429 sin cuerpo legible, como antes
        whatsapp._refused(429, b"nada", AmbiguousChannelError)


@pytest.mark.parametrize("code", [130429, 131056, 4, 80007])
def test_a_whatsapp_send_answered_5xx_with_a_rate_limit_code_is_never_resent(
    world: SimpleNamespace, graph: Any, monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    calls = answers(monkeypatch, (503, error(code)), (200, b"{}"))
    message, _ = outbound(world, talk(world))
    task = Retrying(0)
    tasks.deliver(task, world.a, message.pk)
    saved = state(world, message)
    assert (saved.delivery_status, saved.dispatch_attempts, task.asked) == ("SENDING", 0, [])
    tasks.deliver(Retrying(1), world.a, message.pk)  # otro evento: lo que está `SENDING` no sale
    assert len(calls) == 1 and state(world, message).delivery_status == "SENDING"


@pytest.fixture
def pages(settings: Any) -> None:
    settings.META_ENABLED, settings.META_GRAPH_VERSION = True, GRAPH_VERSION


@pytest.mark.parametrize("status", [*SERVER, 400])
@pytest.mark.parametrize("code", meta_page.TOO_FAST)
def test_a_page_send_answered_5xx_with_a_rate_limit_code_is_never_resent(
    world: SimpleNamespace, pages: None, monkeypatch: pytest.MonkeyPatch, status: int, code: int
) -> None:
    calls = answers(monkeypatch, (status, error(code)))
    message, _ = outbound(world, social(world, "MESSENGER"))
    task = Retrying(0)
    if status < 500:  # Meta no lo aceptó: vuelve a la cola, como siempre
        with pytest.raises(RuntimeError, match="retry"):
            tasks.deliver(task, world.a, message.pk)
        assert (state(world, message).delivery_status, task.asked) == ("QUEUED", [30])
        return
    tasks.deliver(task, world.a, message.pk)
    saved = state(world, message)
    assert (saved.delivery_status, saved.dispatch_attempts, task.asked) == ("SENDING", 0, [])
    assert len(calls) == 1


# --- Un rechazo tardío y una cuenta rotada o reconectada ----------------------------------------


@pytest.fixture
def manager(world: SimpleNamespace) -> None:
    """`ana` administra los canales de A: rota, desconecta y conecta."""
    with tenant_scope(ctx(world.a)):
        mine = OrganizationMembership.objects.get(user_id=world.ana.pk).pk
    give(world.a, mine, {"channels.manage": None})


def rotate(world: SimpleNamespace, account_id: Any) -> None:
    with tenant_scope(ctx(world.a, world.ana)) as tenant:
        accounts.rotate_token(tenant, channel_account_id=account_id, access_token=TEST_TOKEN)


def reconnect(world: SimpleNamespace, account_id: Any) -> Any:
    """Desconecta la cuenta y conecta el mismo número de nuevo: otra cuenta, otra credencial."""
    with tenant_scope(ctx(world.a, world.ana)) as tenant:
        accounts.disconnect(tenant, channel_account_id=account_id)
        made, _ = accounts.connect_whatsapp(
            tenant, name="WA", phone_number_id=PHONE_NUMBER_ID, waba_id=WABA_ID,
            access_token=TEST_TOKEN,
        )  # fmt: skip
    return made.pk


class After:
    """El envío y, antes de cerrar su resultado, lo que hace el propietario (`then`)."""

    def __init__(self, send: Any, then: Any) -> None:
        self.real, self.then = send, then

    def send(self, claim: channels.Claim) -> SendResult:
        result: SendResult = self.real(claim)
        self.then(claim.account_id)
        return result


def account(world: SimpleNamespace, pk: Any) -> tuple[str, str | None]:
    with tenant_scope(ctx(world.a)):
        found = ChannelAccount.objects.get(pk=pk)
    return found.status, found.status_detail


def demotions(migrator: Any) -> list[str]:
    owner = {f"channel_account.{a}" for a in ("degraded", "messages_access_off", "token_invalid")}
    return [a for a in account_audits(migrator) if a in owner]


@pytest.mark.parametrize("then", ["nothing", "rotate", "rotate_red", "reconnect"])
def test_a_late_quality_refusal_never_degrades_an_account_the_owner_reconnected(
    world: SimpleNamespace, graph: Any, migrator: Any, manager: None, then: str
) -> None:
    convo: Conversation = talk(world)
    old, made = convo.channel_account_id, []

    def red(pk: Any) -> None:  # Meta ya da la calidad baja al rotar
        graph.quality = "RED"
        rotate(world, pk)

    owner = {
        "nothing": lambda _: None,
        "rotate": lambda pk: rotate(world, pk),
        "rotate_red": red,
        "reconnect": lambda pk: made.append(reconnect(world, pk)),
    }[then]
    graph.then("quality")  # Meta responde `131048`
    message, _ = outbound(world, convo)
    sends = After(whatsapp.WhatsAppAdapter().send, owner)
    tasks.deliver(Retrying(0), world.a, message.pk, resolve=lambda _: sends)
    saved = state(world, message)
    assert (saved.delivery_status, saved.provider_error_code) == ("FAILED", "QUALITY_BLOCKED")
    if then == "nothing":  # la misma credencial: se degrada, una vez
        assert account(world, old) == ("DEGRADED", "QUALITY_LOW")
        assert demotions(migrator) == ["channel_account.degraded"]
        tasks.deliver(Retrying(0), world.a, message.pk)  # repetido: ni reenvía ni repite
        assert demotions(migrator) == ["channel_account.degraded"] and len(graph.sent) == 0
        return
    assert demotions(migrator) == []  # ni el estado ni la auditoría (con su evento)
    if then == "rotate":  # Meta acaba de leer la calidad: más reciente que el rechazo
        assert account(world, old) == ("CONNECTED", None)
    elif then == "rotate_red":  # la rotación ya la degradó (una auditoría): el rechazo no repite
        assert account(world, old) == ("DEGRADED", "QUALITY_LOW")
        rotated = [c for a, c, _ in audit(migrator) if a == "channel_account.credentials_rotated"]
        assert len(rotated) == 1 and rotated[0]["status"] == ["CONNECTED", "DEGRADED"]
    else:  # la cuenta anterior quedó desconectada y la nueva sigue como se conectó
        assert account(world, made[0]) == ("PENDING", "WEBHOOK_NOT_SEEN")
        assert account(world, old) == ("DISCONNECTED", "DISCONNECTED_BY_ADMIN")


@pytest.mark.parametrize("rotated", [False, True])
@pytest.mark.parametrize(
    ("reason", "status", "detail"),
    [("QUALITY_BLOCKED", "DEGRADED", "QUALITY_LOW"),
     ("MESSAGES_ACCESS_OFF", "ERROR", "MESSAGES_ACCESS_OFF"),
     ("TOKEN_INVALID", "ERROR", "TOKEN_INVALID")],
)  # fmt: skip
def test_each_late_account_effect_follows_the_credential_of_the_send(
    world: SimpleNamespace, graph: Any, migrator: Any, manager: None,
    rotated: bool, reason: str, status: str, detail: str,
) -> None:  # fmt: skip
    convo: Conversation = talk(world)
    message, _ = outbound(world, convo)
    refused = SendResult(False, reason=reason, detail="131048")
    owner = (lambda pk: rotate(world, pk)) if rotated else (lambda _: None)
    sends = After(lambda _: refused, owner)
    tasks.deliver(Retrying(0), world.a, message.pk, resolve=lambda _: sends)
    # La rotación no lee «Allow access to messages» de Instagram: los mensajes apagados valen igual
    skipped = rotated and reason != "MESSAGES_ACCESS_OFF"
    expected = ("CONNECTED", None) if skipped else (status, detail)
    assert account(world, convo.channel_account_id) == expected
    assert len(demotions(migrator)) == (not skipped)


@pytest.mark.parametrize("first", ["rotate", "refusal"])
def test_a_rotation_and_a_late_refusal_at_once_serialise_on_the_account_lock(
    world: SimpleNamespace, graph: Any, migrator: Any, manager: None, first: str
) -> None:
    convo: Conversation = talk(world)
    pk, done = convo.channel_account_id, {}
    with tenant_scope(ctx(world.a)):
        used = ChannelAccount.objects.get(pk=pk).credential_id  # la del envío

    def rotation() -> None:
        try:
            rotate(world, pk)
            done["rotate"] = True
        finally:
            connection.close()

    def refusal() -> None:
        try:
            with tenant_scope(ctx(world.a)) as tenant:
                done["refusal"] = accounts.mark_degraded(
                    tenant, channel_account_id=pk, credential_id=used
                )
        finally:
            connection.close()

    order = {"rotate": rotation, "refusal": refusal}
    second = "refusal" if first == "rotate" else "rotate"
    threads = [threading.Thread(target=order[name]) for name in (first, second)]
    with psycopg.connect(migrator_conninfo()) as held:  # la cuenta bloqueada: los dos esperan
        held.execute("SELECT 1 FROM channel_accounts WHERE id = %s FOR UPDATE", [pk])
        for waiting, thread in enumerate(threads, start=1):  # en este orden
            thread.start()
            wait_for(migrator, waiting)
    for thread in threads:
        thread.join(timeout=10)
    degraded = first == "refusal"  # con la credencial aún vigente, el rechazo vale
    assert done == {"rotate": True, "refusal": degraded}
    # y la rotación posterior aplica la calidad que Meta acaba de dar (`GREEN`): vuelve
    assert account(world, pk) == ("CONNECTED", None)
    assert len(demotions(migrator)) == degraded


@pytest.mark.parametrize(
    ("before", "quality", "after"),
    [(("CONNECTED", None), "RED", ("DEGRADED", "QUALITY_LOW")),
     (("CONNECTED", None), "YELLOW", ("DEGRADED", "QUALITY_LOW")),
     (("CONNECTED", None), "GREEN", ("CONNECTED", None)),
     (("DEGRADED", "QUALITY_LOW"), "GREEN", ("CONNECTED", None)),
     (("DEGRADED", "QUALITY_LOW"), "RED", ("DEGRADED", "QUALITY_LOW")),
     (("ERROR", "MESSAGES_ACCESS_OFF"), "GREEN", ("ERROR", "MESSAGES_ACCESS_OFF")),
     # F7b-20: una `PENDING` con el webhook ya visto sube como en la comprobación de salud; la que
     # aún no lo vio sigue `PENDING`
     (("PENDING", None), "GREEN", ("CONNECTED", None)),
     (("PENDING", None), "RED", ("DEGRADED", "QUALITY_LOW")),
     (("PENDING", "WEBHOOK_NOT_SEEN"), "GREEN", ("PENDING", "WEBHOOK_NOT_SEEN")),
     (("ERROR", "TOKEN_INVALID"), "RED", ("DEGRADED", "QUALITY_LOW"))],
)  # fmt: skip
def test_a_rotation_applies_the_quality_meta_just_gave_like_the_health_check(
    world: SimpleNamespace, graph: Any, migrator: Any, manager: None,
    before: tuple[str, str | None], quality: str, after: tuple[str, str | None],
) -> None:  # fmt: skip
    # nace en `before`: la base no deja volver a `PENDING` con un UPDATE
    pk = number(world.a, status=before[0], status_detail=before[1])
    graph.quality = quality
    rotate(world, pk)
    assert account(world, pk) == after
    [(action, changes, _)] = [row for row in audit(migrator) if row[0].startswith("channel_acc")]
    assert action == "channel_account.credentials_rotated"  # una sola auditoría, la de rotar
    assert changes.get("status") == ([before[0], after[0]] if before != after else None)

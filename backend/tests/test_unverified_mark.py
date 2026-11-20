"""F8-08a (ADR-034 §9.3): el envío rechaza la marca `[verificar]` del copiloto, también disfrazada.
La búsqueda pura (`core.text.has_verify_mark`), la tabla de confusables y su generador, y la ruta
de envío del Inbox contra PostgreSQL con `crm_app`: 400 `UNVERIFIED_CONTENT` y nada escrito."""

import hashlib
import unicodedata
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from django.db import connections
from django.test.utils import CaptureQueriesContext

from apps.conversations import commands
from apps.conversations.api.messages import UNVERIFIED
from core import confusables, confusables_build
from core.tenancy.scope import require_scope, tenant_scope
from core.text import VERIFY_MARK, has_verify_mark
from tests.test_conversations_api import crm, inbox, real_stack
from tests.test_conversations_send_api import DB, counts, post
from tests.test_memberships import ctx
from tests.test_self_context import signed

__all__ = ["crm", "inbox", "real_stack"]  # los fixtures, para pytest
FAMILY = "\U0001f468\u200d\U0001f469\u200d\U0001f467"  # familia: tres emoji unidos por ZWJ
CODER = "\U0001f469\U0001f3fd\u200d\U0001f4bb"  # profesión con tono de piel
CHEROKEE = "\u13d9\u13ac\u13a1\u13a5\u15b4\u13a5\u13df\u13aa\u13a1"  # mayúsculas que no se pliegan
WIDE = "\uff3b\uff56\uff45\uff52\uff49\uff46\uff49\uff43\uff41\uff52\uff3d"  # ancho completo
MARKED = {  # la variante y el código con el que la ruta la rechaza
    "[v\u0435rificar]": "UNVERIFIED_CONTENT",  # la «e» es cirílica (U+0435)
    "\uff3bverificar\uff3d": "UNVERIFIED_CONTENT",  # corchetes de ancho completo
    "[veri\u200eficar]": "UNVERIFIED_CONTENT",  # una marca de dirección (Cf) que el texto admite
    WIDE: "UNVERIFIED_CONTENT",  # corchetes y letras
    "[VERIFICAR]": "UNVERIFIED_CONTENT",
    "[VER\u0130F\u0130CAR]": "UNVERIFIED_CONTENT",  # la «I con punto»: su punto (Mn) no cuenta
    "[veri\u200aficar]": "UNVERIFIED_CONTENT",  # un espacio fino no separa la palabra
    "[\u2800verificar\u2800]": "UNVERIFIED_CONTENT",  # el braille vacío se ve como un espacio
    "(verificar)": "UNVERIFIED_CONTENT",
    "\u3010 verificar \u3011": "UNVERIFIED_CONTENT",
    "Son S/ 120 [ verificar ] con envío": "UNVERIFIED_CONTENT",
    "[\ufe00verificar]": "UNVERIFIED_CONTENT",  # un selector de variación tras el corchete
    f"[{CHEROKEE}]": "UNVERIFIED_CONTENT",  # cheroqui: el prototipo «V» cuenta como «v»
    "[veri\u200bficar]": "VALIDATION_ERROR",  # el espacio de ancho cero ya lo rechaza `clean_body`
}
PLAIN = ("verificar", "Lo vamos a verificar", "por verificar", "[verificado]", "[ver ificar]")

HIDDEN = (  # más variantes que la búsqueda ve (`clean_body` ya rechaza algunas)
    "[veri\u2060ficar]",
    "[veri\u0307ficar]",
    "[\u115fverificar]",
    "[\nverificar\t]",
    "[\u2028verificar\u1680]",
    *(f"[veri{space}ficar]" for space in "\u2009\u202f\u2006\u205f"),
)


@pytest.mark.parametrize("text", [VERIFY_MARK, *HIDDEN, *MARKED])
def test_the_mark_is_found_however_it_is_disguised(text: str) -> None:
    assert VERIFY_MARK == "[verificar]" and has_verify_mark(text)


@pytest.mark.parametrize("text", [*PLAIN, FAMILY, CODER, "", "[]", "verificar]", "[verificar"])
def test_the_word_without_its_brackets_or_another_word_is_not_the_mark(text: str) -> None:
    assert not has_verify_mark(text)


def test_the_table_comes_from_the_pinned_file_and_only_what_can_form_the_mark_enters() -> None:
    assert (confusables.VERSION, confusables.SHA256) == (
        confusables_build.VERSION, confusables_build.SHA256,
    )  # fmt: skip
    assert unicodedata.unidata_version == confusables.VERSION  # otro Unicode: regenerar la tabla
    header = confusables.__doc__ or ""
    assert confusables_build.SHA256 in header and confusables_build.URL in header
    source = "\n".join((
        "# confusables.txt", "0435 ;\t0065 ;\tMA\t# cirílica", "13D9 ;\t0056 ;\tMA\t# cheroqui",
        "2768 ;\t0028 ;\tMA\t# un paréntesis", "1F707 ;\t0061 0072 ;\tMA\t# «ar»: cabe en «car»",
        "0444 ;\t0278 ;\tMA\t# otro prototipo", "00E6 ;\t0061 0065 ;\tMA\t# «ae» no cabe",
        "0415 ;\t0045 ;\tMA\t# `casefold` la quita", "FF41 ;\t0061 ;\tMA\t# NFKC la quita",
    ))  # fmt: skip
    found = confusables_build.table(source)
    assert found == {"(": "\u2768", "ar": "\U0001f707", "e": "\u0435", "v": "\u13d9"}
    namespace: dict[str, Any] = {}
    exec(confusables_build.render(found), namespace)  # noqa: S102 — el módulo que genera
    assert namespace["PROTOTYPES"] == found
    with pytest.raises(ValueError, match="no es el confusables.txt"):
        confusables_build.build(source.encode())  # sin red: solo el archivo fijado
    assert hashlib.sha256(source.encode()).hexdigest() != confusables_build.SHA256
    # un prototipo cuenta entero o por un extremo: «x(» empieza una marca y «)x» la termina
    assert [confusables_build.contributes(t) for t in ("x(", ")x", "(x", "x)")] == [
        True, True, False, False,
    ]  # fmt: skip


@pytest.mark.usefixtures("tenant_db")
def test_the_mark_is_refused_before_any_query(inbox: SimpleNamespace) -> None:
    with tenant_scope(ctx(inbox.a, inbox.ana)) as tenant:
        alias = require_scope(tenant)
        with CaptureQueriesContext(connections[alias]) as seen:
            with pytest.raises(commands.UnverifiedContent):
                commands.record_outbound(
                    tenant, conversation_id=inbox.convs["nobody"], client_message_id=uuid4(),
                    body="[VERIFICAR]", purpose="CUSTOMER_REPLY",
                )  # fmt: skip
    assert seen.captured_queries == []  # ni el permiso, ni la conversación, ni la transacción


@pytest.mark.usefixtures("tenant_db")
def test_the_inbox_refuses_every_variant_and_writes_nothing(
    inbox: SimpleNamespace, migrator: DB
) -> None:
    ana, convo = signed(inbox.ana), inbox.convs["nobody"]
    for text, code in MARKED.items():
        refused = post(ana, convo, body=text)
        assert (refused.status_code, refused.json()["code"]) == (400, code), text
        assert code != "UNVERIFIED_CONTENT" or refused.json()["message"] == UNVERIFIED  # fijo
    assert counts(migrator) == (0, 0, 0)  # ni mensaje, ni evento en el outbox, ni auditoría
    for text in (*PLAIN, f"Listo {FAMILY} {CODER}"):
        assert post(ana, convo, body=text).status_code == 202, text
    stored = sorted(row[0] for row in migrator.execute("SELECT body FROM messages"))
    assert stored == sorted((*PLAIN, f"Listo {FAMILY} {CODER}"))  # idéntico: la clave no se guarda

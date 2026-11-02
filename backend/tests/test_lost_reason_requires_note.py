"""F5-04j (D-F5-02): `lost_reasons.requires_note`. Perder con un motivo que exige nota y sin ella
es un 400 en `note`; el indicador se lee con el motivo bloqueado; los motivos existentes no la
exigen; las rutas de motivos lo aceptan y lo devuelven. Middleware, sesión, motor de autorización
y PostgreSQL con el rol `crm_app`."""

import json
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import psycopg
import pytest

from apps.deals.models import LostReason
from core.tenancy.scope import tenant_scope
from tests import test_opportunity_scopes as scopes
from tests.test_memberships import ctx
from tests.test_opportunities_assign_delete_api import held_call, stored
from tests.test_opportunities_close_api import lose, reason
from tests.test_opportunities_write_api import acts, fail, rows
from tests.test_pipelines_api import REASONS, read
from tests.test_products import send
from tests.test_self_context import signed

crm, sales, pipelines = scopes.crm, scopes.sales, scopes.pipelines
real_stack = scopes.real_stack
pytestmark = pytest.mark.usefixtures("tenant_db")
NOTE = "Dijo que Juan Perez DNI 12345678 compró en otra tienda"


def strict(org: UUID, code: str = "NEEDS_NOTE", **fields: Any) -> LostReason:
    with tenant_scope(ctx(org)):
        made: LostReason = LostReason.objects.create(
            name="Exige nota", code=code, requires_note=True, **fields
        )
    return made


def lost(sales: SimpleNamespace, key: str, who: Any, why: UUID, **body: Any) -> Any:
    sent = {"lost_reason_id": str(why), "version": 1, **body}
    return lose(signed(who), sales.opps[key], sent)


def test_the_seeded_reasons_do_not_require_a_note(sales: SimpleNamespace) -> None:
    with tenant_scope(ctx(sales.a)):
        flags = set(LostReason.objects.values_list("requires_note", flat=True))
    assert flags == {False}  # los existentes siguen sin exigirla


def test_losing_without_the_note_a_reason_requires_is_a_400_on_note_and_writes_nothing(
    sales: SimpleNamespace, migrator: psycopg.Connection[Any]
) -> None:
    why, before = strict(sales.a).pk, rows(migrator)
    for extra in ({}, {"note": None}, {"note": ""}, {"note": "   "}):
        response = lost(sales, "ana", sales.ana, why, **extra)
        assert fail(response) == (400, "VALIDATION_ERROR", ["note"]), extra
        message = response.json()["fields"]["note"][0]["message"]
        assert message == "Este motivo de pérdida exige una nota."
        assert str(why) not in response.text  # sin eco
    assert rows(migrator) == before and stored(sales, sales.opps["ana"]).status == "OPEN"
    assert stored(sales, sales.opps["ana"]).version == 1


def test_with_a_note_it_works_and_a_reason_without_the_flag_is_unchanged(
    sales: SimpleNamespace,
) -> None:
    done = lost(sales, "ana", sales.ana, strict(sales.a).pk, note=f"  {NOTE} ")
    assert done.status_code == 200 and done.json()["lost_reason_note"] == NOTE
    plain = lost(sales, "vera", sales.ana, reason(sales.a))  # `PRICE`, sin nota: como siempre
    assert plain.status_code == 200 and plain.json()["lost_reason_note"] is None
    blank = lost(sales, "sam", sales.ana, reason(sales.a), note="  ")
    assert blank.status_code == 200 and blank.json()["lost_reason_note"] is None


def test_the_audit_still_masks_the_note(
    sales: SimpleNamespace, migrator: psycopg.Connection[Any]
) -> None:
    done = lost(sales, "ana", sales.ana, strict(sales.a).pk, note=NOTE)
    assert done.status_code == 200 and acts(migrator)[-1] == ("opportunity.lost", sales.ana.pk)
    found = migrator.execute(
        "SELECT changes::text FROM audit_logs WHERE action = 'opportunity.lost'"
    ).fetchone()
    assert found is not None
    changes = json.loads(found[0])
    assert changes["lost_reason_note"] == [None, "[texto]"]
    assert "12345678" not in found[0] and "Juan" not in found[0]


HOLD = "UPDATE lost_reasons SET requires_note = %s, version = version + 1 WHERE id = %s"


def test_a_flag_flipped_while_the_loss_waits_is_the_one_that_counts(
    sales: SimpleNamespace, migrator: psycopg.Connection[Any]
) -> None:
    """El indicador se lee con el motivo bloqueado: una edición del motivo en vuelo (aún sin
    confirmar) hace esperar a quien pierde, que al despertar ve el valor nuevo."""
    needs, plain = strict(sales.a).pk, reason(sales.a)
    body = {"version": 1}
    # el motivo pasa a exigir nota mientras se pierde sin ella: 400, nada escrito
    waited, answer = held_call(
        sales.ana,
        lambda c: lose(c, sales.opps["ana"], {"lost_reason_id": str(plain), **body}),
        HOLD,
        [True, plain],
    )
    assert waited and answer == (400, "VALIDATION_ERROR")
    assert stored(sales, sales.opps["ana"]).status == "OPEN"
    # y al revés: deja de exigirla mientras se pierde sin ella: 200
    waited, answer = held_call(
        sales.ana,
        lambda c: lose(c, sales.opps["vera"], {"lost_reason_id": str(needs), **body}),
        HOLD,
        [False, needs],
    )
    assert waited and answer == (200, None)
    assert stored(sales, sales.opps["vera"]).status == "LOST"
    flag = migrator.execute("SELECT requires_note FROM lost_reasons WHERE id = %s", [needs])
    assert flag.fetchone() == (False,)


def test_the_reason_routes_accept_and_return_the_flag(
    sales: SimpleNamespace, migrator: psycopg.Connection[Any]
) -> None:
    ana = signed(sales.ana)
    made = send(
        ana, "post", REASONS, {"name": "Con nota", "code": "WITH_NOTE", "requires_note": True}
    )
    assert made.status_code == 201 and made.json()["requires_note"] is True
    plain = send(ana, "post", REASONS, {"name": "Sin nota", "code": "NO_NOTE"})
    assert plain.json()["requires_note"] is False  # por omisión no la exige
    url = f"{REASONS}{made.json()['id']}/"
    flip = send(ana, "patch", url, {"version": 1, "requires_note": False})
    assert flip.status_code == 200 and flip.json()["requires_note"] is False
    assert flip.json()["version"] == 2
    again = send(ana, "patch", url, {"version": 2, "requires_note": True, "name": "Con nota"})
    assert again.json()["requires_note"] is True and again.json()["version"] == 3
    listed = {row["code"]: row["requires_note"] for row in read(ana, REASONS)["results"]}
    assert listed["WITH_NOTE"] is True and listed["NO_NOTE"] is False and listed["PRICE"] is False
    audited = migrator.execute(
        "SELECT action, changes::text FROM audit_logs WHERE entity_type = 'lost_reason' "
        "ORDER BY occurred_at, id"
    ).fetchall()
    assert [a for a, _ in audited] == [
        "lost_reason.created", "lost_reason.created", "lost_reason.updated", "lost_reason.updated",
    ]  # fmt: skip
    assert '"requires_note": [null, true]' in audited[0][1].replace("\n", "")
    assert '"requires_note": [true, false]' in audited[2][1].replace("\n", "")


@pytest.mark.parametrize("value", ["true", 1, 0, None, "", [True], {"a": 1}, 1.0])
def test_a_flag_that_is_not_a_boolean_is_a_400_on_requires_note(
    sales: SimpleNamespace, migrator: psycopg.Connection[Any], value: Any
) -> None:
    ana, before = signed(sales.ana), rows(migrator)
    created = send(ana, "post", REASONS, {"name": "X", "code": "X_NOTE", "requires_note": value})
    assert fail(created) == (400, "VALIDATION_ERROR", ["requires_note"])
    url = f"{REASONS}{reason(sales.a)}/"
    changed = send(ana, "patch", url, {"version": 1, "requires_note": value})
    assert fail(changed) == (400, "VALIDATION_ERROR", ["requires_note"])
    assert rows(migrator) == before


def test_the_migration_keeps_row_level_security_and_the_runtime_grants(
    migrator: psycopg.Connection[Any],
) -> None:
    rls = migrator.execute(
        "SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = 'lost_reasons'"
    ).fetchone()
    assert rls == (True, True)
    column = migrator.execute(
        "SELECT is_nullable, column_default FROM information_schema.columns "
        "WHERE table_name = 'lost_reasons' AND column_name = 'requires_note'"
    ).fetchone()
    assert column == ("NO", "false")
    grants = migrator.execute(
        "SELECT privilege_type FROM information_schema.role_table_grants "
        "WHERE table_name = 'lost_reasons' AND grantee = 'crm_app' ORDER BY 1"
    ).fetchall()
    assert [g[0] for g in grants] == ["INSERT", "SELECT", "UPDATE"]  # borrado lógico: sin DELETE

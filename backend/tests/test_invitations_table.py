"""F2-79: la tabla `user_invitations` (ADR-020). Solo la base y el modelo: sin rutas todavía.
PostgreSQL con el rol `crm_app`; las restricciones se prueban con el migrador."""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from django.db import IntegrityError, ProgrammingError

from apps.organizations.models import UserInvitation
from apps.organizations.selectors import invitations as stored
from core.tenancy.context import TenantContextError, TenantContextMissing
from core.tenancy.scope import tenant_scope
from tests import test_authorization
from tests.test_memberships import ctx, join, raw

world = test_authorization.world
pytestmark = pytest.mark.usefixtures("tenant_db")
SOON = datetime.now(UTC) + timedelta(days=7)
HASH = "ab" * 32
INSERT = (
    "INSERT INTO user_invitations (organization_id, email, role_ids, expires_at,"
    " invited_by_user_id, status, created_at, updated_at)"
    " VALUES (%s, %s, %s, %s, %s, 'PENDING', now(), now())"
)


def invite(org: UUID, email: str, by: Any, **fields: Any) -> UserInvitation:
    with tenant_scope(ctx(org)):
        made: UserInvitation = UserInvitation.objects.create(
            email=email,
            role_ids=fields.pop("role_ids", [uuid4()]),
            expires_at=fields.pop("expires_at", SOON),
            invited_by=by,
            **fields,
        )
    return made


def test_it_keeps_what_an_invitation_is_and_starts_pending_without_a_link(world: Any) -> None:
    roles = [uuid4(), uuid4()]
    made = invite(world.a, "luis@cliente.pe", world.ana, role_ids=roles)
    with tenant_scope(ctx(world.a)):
        row = stored().get()
    assert (row.pk, row.organization_id, row.email) == (made.pk, world.a, "luis@cliente.pe")
    assert (row.role_ids, row.invited_by_id, row.expires_at) == (roles, world.ana.pk, SOON)
    assert (row.status, row.token_hash, row.sent_at, row.accepted_at) == (
        "PENDING",
        None,
        None,
        None,
    )
    assert row.send_count == 0
    assert row.created_at is not None and row.updated_at is not None
    assert str(row) == str(made.pk)  # nunca el correo


def test_rls_hides_and_refuses_the_invitations_of_another_organization(world: Any) -> None:
    join(world.b, world.ana)  # quien invita es miembro de la organización que invita
    mine = invite(world.a, "a@cliente.pe", world.ana)
    theirs = invite(world.b, "b@cliente.pe", world.ana)
    assert raw("SELECT count(*) FROM user_invitations") == [(0,)]  # sin tenant, nada
    with pytest.raises(TenantContextMissing):
        stored().count()
    with tenant_scope(ctx(world.a)):
        assert raw("SELECT id FROM user_invitations") == [(mine.pk,)]  # RLS, no solo el manager
        assert list(stored()) == [mine]
        assert not UserInvitation._base_manager.filter(pk=theirs.pk).exists()
        assert raw("UPDATE user_invitations SET status = 'REVOKED'") == [(1,)]
        assert raw("DELETE FROM user_invitations WHERE id = %s", [theirs.pk]) == [(0,)]
    with tenant_scope(ctx(world.b)):
        assert stored().get().status == "PENDING"  # intacta
    with pytest.raises(ProgrammingError, match="row-level security"), tenant_scope(ctx(world.a)):
        raw(INSERT, [world.b, "c@cliente.pe", [uuid4()], SOON, world.ana.pk])
    with pytest.raises(TenantContextError), tenant_scope(ctx(world.a)):
        UserInvitation(
            organization_id=world.b, email="d@x.pe", role_ids=[uuid4()], expires_at=SOON
        ).save()  # y el modelo, antes


def test_only_one_pending_invitation_per_email_and_organization(world: Any) -> None:
    first = invite(world.a, "luis@cliente.pe", world.ana)
    join(world.b, world.ana)
    invite(world.b, "luis@cliente.pe", world.ana)  # otra organización: otra invitación
    invite(world.a, "otra@cliente.pe", world.ana)
    with pytest.raises(IntegrityError, match="user_invitations_pending_uq"):
        invite(world.a, "luis@cliente.pe", world.ana)
    with tenant_scope(ctx(world.a)):  # revocada o aceptada, deja invitar otra vez
        UserInvitation.objects.filter(pk=first.pk).update(status="REVOKED")
    second = invite(world.a, "luis@cliente.pe", world.ana)
    with tenant_scope(ctx(world.a)):
        UserInvitation.objects.filter(pk=second.pk).update(
            status="ACCEPTED", accepted_at=datetime.now(UTC)
        )
    invite(world.a, "luis@cliente.pe", world.ana)
    with tenant_scope(ctx(world.a)):
        assert stored().filter(email="luis@cliente.pe").count() == 3


@pytest.mark.parametrize(
    "email",
    [
        "",
        "Luis@cliente.pe",  # en minúsculas: la unicidad no depende de cómo se escribió
        "luis@Cliente.pe",
        "luis@cliente.pe ",
        " luis@cliente.pe",
        "luis @cliente.pe",
        "luis@cliente.pe\n",
        "luis\x7f@cliente.pe",  # ni un carácter de control
        "luis",
        "luis@",
        "@cliente.pe",
        "luis@cliente@pe",
        "luís@cliente.pe",  # ASCII: lo que el correo saliente puede entregar
        "luis@cliënte.pe",
        "ｌuis@cliente.pe",
    ],
)
def test_the_database_only_takes_a_lowercase_ascii_address(
    world: Any, migrator: psycopg.Connection[Any], email: str
) -> None:
    with pytest.raises(psycopg.errors.CheckViolation, match="user_invitations_email_ck"):
        migrator.execute(INSERT, [world.a, email, [uuid4()], SOON, world.ana.pk])


def test_the_database_keeps_the_other_rules_of_an_invitation(
    world: Any, migrator: psycopg.Connection[Any]
) -> None:
    user, good = world.ana.pk, [uuid4()]

    def insert(**changes: Any) -> None:
        row = {"org": world.a, "email": f"{uuid4().hex}@x.pe", "roles": good, "by": user}
        row.update(changes)
        migrator.execute(INSERT, [row["org"], row["email"], row["roles"], SOON, row["by"]])

    insert()
    insert(email="o'brien+tag_1.x@sub-dominio.cliente.pe")  # lo que sí es una dirección
    insert(roles=[uuid4() for _ in range(20)])
    insert(email=f"{'a' * 249}@x.pe")  # hasta 254 caracteres, como `users.email`
    with pytest.raises(psycopg.errors.StringDataRightTruncation):
        insert(email=f"{'a' * 250}@x.pe")
    nested = [[uuid4() for _ in range(20)] for _ in range(20)]  # 400: `array_length` ve 20
    for roles in ([], [uuid4() for _ in range(21)], [None], [uuid4(), None], nested):
        with pytest.raises(psycopg.errors.CheckViolation, match="user_invitations_roles_ck"):
            insert(roles=roles)  # al menos un rol de verdad, y no más de los que caben
    with pytest.raises(psycopg.errors.ForeignKeyViolation, match="organization_fk"):
        insert(org=uuid4())
    for other in ({"by": uuid4()}, {"org": world.b}):  # quien invita es miembro de esta
        with pytest.raises(psycopg.errors.ForeignKeyViolation, match="invited_by_org_fk"):
            insert(**other)
    with pytest.raises(psycopg.errors.ForeignKeyViolation, match="invited_by_org_fk"):
        migrator.execute("DELETE FROM organization_memberships WHERE user_id = %s", [user])
    update = "UPDATE user_invitations SET {} WHERE organization_id = %s"
    for column in ("role_ids", "expires_at", "invited_by_user_id", "send_count"):  # ni falta
        with pytest.raises(psycopg.errors.NotNullViolation):
            migrator.execute(update.format(f"{column} = NULL"), [world.a])
    for value in ("AB" * 32, "ab" * 31, "zz" * 32, "", f"{'ab' * 31}a\n"):  # un SHA-256 en hex
        with pytest.raises(psycopg.errors.CheckViolation, match="user_invitations_token_hash_ck"):
            migrator.execute(update.format("token_hash = %s"), [value, world.a])
    with pytest.raises(psycopg.errors.StringDataRightTruncation):  # y no cabe nada más largo
        migrator.execute(update.format("token_hash = %s"), ["ab" * 33, world.a])
    # Un hash es de una sola invitación, de la organización que sea: por él se la encuentra.
    one = "UPDATE user_invitations SET token_hash = %s, sent_at = now() WHERE id = %s"
    ids = [row[0] for row in migrator.execute("SELECT id FROM user_invitations").fetchall()]
    migrator.execute(one, [HASH, ids[0]])
    with pytest.raises(psycopg.errors.UniqueViolation, match="user_invitations_token_hash_uq"):
        migrator.execute(one, [HASH, ids[1]])
    migrator.execute(one, ["cd" * 32, ids[1]])  # otro hash sí; y sin hash, las que sean
    join(world.b, world.ana)
    insert(org=world.b)
    with pytest.raises(psycopg.errors.UniqueViolation, match="user_invitations_token_hash_uq"):
        migrator.execute(update.format("token_hash = %s"), [HASH, world.b])  # ni en otra
    for count in (-1, 6):  # un correo no se envía más de cinco veces por invitación
        with pytest.raises(psycopg.errors.CheckViolation, match="user_invitations_send_count_ck"):
            migrator.execute(update.format("send_count = %s"), [count, world.a])
    migrator.execute(update.format("send_count = 5"), [world.a])
    revoked = "status = 'REVOKED', accepted_at = now()"
    for change in ("status = 'ACCEPTED'", "accepted_at = now()", revoked):  # las dos, o ninguna
        with pytest.raises(psycopg.errors.CheckViolation, match="user_invitations_accepted_ck"):
            migrator.execute(update.format(change), [world.a])
    with pytest.raises(psycopg.errors.CheckViolation, match="user_invitations_status_ck"):
        migrator.execute(update.format("status = 'SENT'"), [world.a])
    for status in ("REVOKED", "EXPIRED"):
        migrator.execute(update.format("status = %s"), [status, world.a])
    migrator.execute(update.format("status = 'ACCEPTED', accepted_at = now()"), [world.a])
    rls = migrator.execute(
        "SELECT relrowsecurity, relforcerowsecurity FROM pg_class"
        " WHERE relname = 'user_invitations'"
    ).fetchone()
    assert rls == (True, True)

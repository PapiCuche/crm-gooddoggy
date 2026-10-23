"""F4-02: los permisos de contactos, la política de alcance de `Contact` y las rutas de lectura
(`…/contacts/`). Middleware, sesión, motor de autorización y PostgreSQL con el rol `crm_app`."""

from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.access.catalog import BY_CODE, ROLE_TEMPLATES, Scope
from apps.access.models import MembershipRole
from apps.access.services import clone_role_templates
from apps.contacts.models import Contact
from apps.organizations.models import OrganizationMembership
from core.tenancy.scope import tenant_scope
from tests import test_self_context
from tests.factories import make_user
from tests.test_authorization import give
from tests.test_branches import branch
from tests.test_member_branch import DENIED
from tests.test_memberships import ctx, join
from tests.test_self_context import reply, signed
from tests.test_team_members import add
from tests.test_teams import team

real_stack = test_self_context.real_stack
pytestmark = pytest.mark.usefixtures("tenant_db")
CONTACTS = "/api/v1/o/org-a/contacts/"
SCOPED = ("contacts.view", "contacts.update", "contacts.delete")
FIELDS = {
    "id",
    "display_name",
    "first_name",
    "last_name",
    "primary_phone_e164",
    "primary_email",
    "document_type",
    "document_number",
    "owner_user_id",
    "branch_id",
    "source",
    "created_by_user_id",
    "created_at",
    "updated_at",
    "version",
}


def contact(org: UUID, name: str, **fields: Any) -> UUID:
    with tenant_scope(ctx(org)):
        made: Contact = Contact.objects.create(display_name=name, **fields)
    return made.pk


@pytest.fixture
def crm(orgs: dict[str, UUID]) -> SimpleNamespace:
    """Org A con sus plantillas: ana es Owner, vera Vendedora, sam Supervisor (en el mismo equipo
    que vera), bea sin rol. luis es de B. Contactos de A: de vera (uno suyo, otro que creó, otro
    borrado), uno de la sucursal de Lima, uno de sam y uno de ana sin sucursal."""
    a = orgs["A"]
    ana, vera, sam, bea, luis = (make_user(email=f"{n}@example.com") for n in "avsbl")
    m = {name: join(a, user).pk for name, user in (("ana", ana), ("vera", vera), ("sam", sam))}
    m["bea"] = join(a, bea).pk
    m_luis = join(orgs["B"], luis).pk
    with tenant_scope(ctx(a)) as scope:
        roles = {role.code: role for role in clone_role_templates(scope)}
        for name, code in (("ana", "owner"), ("vera", "seller"), ("sam", "supervisor")):
            MembershipRole.objects.create(membership_id=m[name], role=roles[code])
    sales = team(a, "ventas")
    add(a, sales.pk, m["vera"])
    add(a, sales.pk, m["sam"])
    lima, cusco = branch(a, "LIM"), branch(a, "CUZ")
    ids = {
        "owned": contact(a, "De vera", owner_user_id=vera.pk, created_by_user_id=ana.pk),
        "created": contact(a, "Creado por vera", owner_user_id=ana.pk, created_by_user_id=vera.pk),
        "deleted": contact(a, "Borrado", owner_user_id=vera.pk, deleted_at=timezone.now()),
        "lima": contact(a, "De Lima", branch_id=lima.pk, owner_user_id=ana.pk),
        "cusco": contact(a, "De Cusco", branch_id=cusco.pk),
        "sam": contact(a, "De sam", created_by_user_id=sam.pk),
        "plain": contact(a, "Sin nadie"),
        "b": contact(orgs["B"], "De B", owner_user_id=luis.pk),
    }
    return SimpleNamespace(
        a=a,
        b=orgs["B"],
        ana=ana,
        vera=vera,
        sam=sam,
        bea=bea,
        luis=luis,
        m_luis=m_luis,
        m=m,
        lima=lima,
        ids=ids,
    )


def listed(client: Client, url: str = CONTACTS) -> set[UUID]:
    response = client.get(url)
    assert response.status_code == 200, response.content
    return {UUID(row["id"]) for row in response.json()["results"]}


def names(crm: SimpleNamespace, *keys: str) -> set[UUID]:
    return {crm.ids[key] for key in keys}


def test_the_catalog_and_the_templates_carry_the_contacts_permissions() -> None:
    for code in (*SCOPED, "contacts.create"):
        permission = BY_CODE[code]
        assert (permission.module, permission.is_sensitive) == ("contacts", False)
        assert permission.supports_scope is (code != "contacts.create")
    grants = {template.code: template.grants for template in ROLE_TEMPLATES}
    contacts = {
        code: {k: v for k, v in held.items() if k.startswith("contacts.")}
        for code, held in grants.items()
    }
    organization = dict.fromkeys(SCOPED, Scope.ORGANIZATION)
    assert contacts["owner"] == contacts["admin"] == {**organization, "contacts.create": None}
    assert contacts["supervisor"] == {"contacts.view": Scope.TEAM}
    assert contacts["seller"] == {
        "contacts.view": Scope.OWN,
        "contacts.update": Scope.OWN,
        "contacts.create": None,
    }


def test_a_seller_sees_only_what_they_own_or_created(crm: SimpleNamespace) -> None:
    vera = signed(crm.vera)
    assert listed(vera) == names(crm, "owned", "created")  # ni el borrado ni los demás
    body = vera.get(f"{CONTACTS}{crm.ids['owned']}/")
    assert body.status_code == 200 and set(body.json()) == FIELDS
    assert body.json()["owner_user_id"] == str(crm.vera.pk)
    assert vera.get(f"{CONTACTS}{crm.ids['created']}/").status_code == 200


def test_out_of_scope_deleted_foreign_or_missing_are_the_same_404(crm: SimpleNamespace) -> None:
    vera = signed(crm.vera)
    missing = reply(vera.get(f"{CONTACTS}{uuid4()}/"))
    assert missing[0] == 404
    for key in ("lima", "sam", "plain", "deleted", "b"):  # sin oráculo de existencia
        assert reply(vera.get(f"{CONTACTS}{crm.ids[key]}/")) == missing, key
    assert reply(signed(crm.ana).get(f"{CONTACTS}{crm.ids['deleted']}/")) == missing  # ni Owner
    assert reply(signed(crm.ana).get(f"{CONTACTS}{crm.ids['b']}/")) == missing


def test_team_widens_nothing_beyond_own_until_d_f4_01(crm: SimpleNamespace) -> None:
    """sam comparte equipo con vera y tiene sucursal, pero con `TEAM` solo ve lo suyo
    (phase-4.md, D-F4-01): ni lo del equipo ni lo de su sucursal."""
    with tenant_scope(ctx(crm.a)):
        OrganizationMembership.objects.filter(pk=crm.m["sam"]).update(default_branch=crm.lima)
    sam = signed(crm.sam)
    assert listed(sam) == names(crm, "sam")
    for key in ("owned", "lima"):
        assert sam.get(f"{CONTACTS}{crm.ids[key]}/").status_code == 404


def test_branch_sees_its_branch_plus_own(crm: SimpleNamespace) -> None:
    give(crm.a, crm.m["bea"], {"contacts.view": "BRANCH"})
    mine = contact(crm.a, "De bea", owner_user_id=crm.bea.pk)
    bea = signed(crm.bea)
    assert listed(bea) == {mine}  # sin sucursal, `BRANCH` es `OWN`
    with tenant_scope(ctx(crm.a)):
        OrganizationMembership.objects.filter(pk=crm.m["bea"]).update(default_branch=crm.lima)
    assert listed(bea) == {mine, crm.ids["lima"]}  # no la de Cusco
    assert bea.get(f"{CONTACTS}{crm.ids['lima']}/").status_code == 200
    assert bea.get(f"{CONTACTS}{crm.ids['owned']}/").status_code == 404


def test_scopes_of_several_roles_add_up(crm: SimpleNamespace) -> None:
    give(crm.a, crm.m["vera"], {"contacts.view": "BRANCH"})
    with tenant_scope(ctx(crm.a)):
        OrganizationMembership.objects.filter(pk=crm.m["vera"]).update(default_branch=crm.lima)
    assert listed(signed(crm.vera)) == names(crm, "owned", "created", "lima")


def test_organization_sees_every_live_contact_of_its_own_organization(crm: SimpleNamespace) -> None:
    every = names(crm, "owned", "created", "lima", "cusco", "sam", "plain")
    assert listed(signed(crm.ana)) == every
    give(crm.b, crm.m_luis, {"contacts.view": "ORGANIZATION"})
    luis = signed(crm.luis)
    assert listed(luis, "/api/v1/o/org-b/contacts/") == names(crm, "b")  # nada de A
    assert luis.get(CONTACTS).status_code == 404  # en A no es miembro


def test_without_contacts_view_it_is_403_whatever_the_id(crm: SimpleNamespace) -> None:
    bea = signed(crm.bea)
    give(crm.a, crm.m["bea"], {"contacts.create": None, "contacts.update": "ORGANIZATION"})
    assert reply(bea.get(CONTACTS)) == DENIED  # crear o editar no da leer
    for key in ("plain", "b"):
        assert reply(bea.get(f"{CONTACTS}{crm.ids[key]}/")) == DENIED
    assert reply(bea.get(f"{CONTACTS}{uuid4()}/")) == DENIED
    vera = signed(crm.vera)
    assert vera.get(CONTACTS).status_code == 200
    with tenant_scope(ctx(crm.a)):
        MembershipRole.objects.filter(membership_id=crm.m["vera"]).delete()
    assert reply(vera.get(CONTACTS)) == DENIED  # en su siguiente petición


def test_the_list_pages_by_creation_within_the_scope(crm: SimpleNamespace) -> None:
    ana, seen, url = signed(crm.ana), [], f"{CONTACTS}?limit=4"
    while url:
        page = ana.get(url).json()
        seen += [row["id"] for row in page["results"]]
        url = page["next"] and f"{CONTACTS}?limit=4&cursor={page['next']}"
    assert seen == sorted(seen) and len(seen) == 6  # UUIDv7: orden de creación, sin repetir
    vera = signed(crm.vera).get(f"{CONTACTS}?limit=1").json()
    assert vera["next"] is not None
    rest = signed(crm.vera).get(f"{CONTACTS}?limit=1&cursor={vera['next']}").json()
    assert rest["next"] is None and len(rest["results"]) == 1  # solo lo suyo


def test_the_list_costs_the_same_queries_for_one_row_or_many(crm: SimpleNamespace) -> None:
    ana = signed(crm.ana)
    with CaptureQueriesContext(connection) as one:
        assert ana.get(f"{CONTACTS}?limit=1").status_code == 200
    with CaptureQueriesContext(connection) as many:
        assert ana.get(f"{CONTACTS}?limit=6").status_code == 200
    assert len(one) == len(many)

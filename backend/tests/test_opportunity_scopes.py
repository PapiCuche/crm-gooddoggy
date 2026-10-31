"""F5-04: los permisos de oportunidades, las plantillas de rol y la política de alcance
`AssignedTo` (por responsable; `TEAM`, los compañeros de equipo), con PostgreSQL real y el rol
`crm_app`. Las rutas: `test_opportunities_api`."""

from dataclasses import replace
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest
from django.utils import timezone

from apps.access.catalog import BY_CODE, ROLE_TEMPLATES, Scope
from apps.access.scopes import policy_for
from apps.access.selectors import can, execution_context, scoped
from apps.deals.models import Opportunity
from apps.deals.scopes import AssignedTo
from apps.deals.seed import ensure_default_pipeline
from apps.organizations.models import TeamMember
from core.tenancy.scope import tenant_scope
from tests import test_contacts_api
from tests.test_memberships import ctx
from tests.test_opportunities_tables import make
from tests.test_team_members import add
from tests.test_teams import team

real_stack = test_contacts_api.real_stack
pytestmark = pytest.mark.usefixtures("tenant_db")
SCOPED = ("view", "update", "delete", "mark_won", "mark_lost")

crm = test_contacts_api.crm


@pytest.fixture(autouse=True)
def pipelines(crm: SimpleNamespace) -> None:
    """El pipeline minorista, sembrado en A y en B."""
    for org in (crm.a, crm.b):
        with tenant_scope(ctx(org)) as tenant:
            ensure_default_pipeline(tenant)


@pytest.fixture
def sales(crm: SimpleNamespace) -> SimpleNamespace:
    """Sobre `crm` (ana Owner, vera Vendedora y sam Supervisor, del mismo equipo; bea sin rol; luis
    de B): una oportunidad de cada uno, una sin responsable, una borrada y una de B."""
    a = crm.a

    def opp(who: Any, title: str, contact: str, **fields: Any) -> UUID:
        user = None if who is None else who.pk
        return make(  # type: ignore[no-any-return]
            a, title=title, assigned_user_id=user, **fields
        ).pk

    # `make` crea su propio contacto: cámbialo por el de `crm`, que tiene dueño y alcance
    ids = {
        "vera": opp(crm.vera, "De vera", "owned"),
        "sam": opp(crm.sam, "De sam", "sam"),
        "ana": opp(crm.ana, "De ana", "plain"),
        "nobody": opp(None, "Sin responsable", "plain"),
        "gone": opp(crm.vera, "Borrada", "owned", deleted_at=timezone.now()),
        "b": make(crm.b, title="De B", assigned_user_id=crm.luis.pk).pk,
    }
    with tenant_scope(ctx(a)):
        for key, contact in (
            ("vera", "owned"),
            ("sam", "sam"),
            ("ana", "plain"),
            ("nobody", "plain"),
        ):
            Opportunity.objects.filter(pk=ids[key]).update(contact_id=crm.ids[contact])
    return SimpleNamespace(**vars(crm), opps=ids)


def pick(s: SimpleNamespace, *keys: str) -> set[UUID]:
    return {s.opps[key] for key in keys}


def test_the_catalog_and_the_templates_carry_the_opportunity_permissions() -> None:
    for action in (*SCOPED, "create", "assign"):
        permission = BY_CODE[f"opportunities.{action}"]
        assert (permission.module, permission.is_sensitive) == ("opportunities", False)
        assert permission.supports_scope is (action in SCOPED)
    held = {
        t.code: {k.split(".")[1]: v for k, v in t.grants.items() if k.startswith("opportunities.")}
        for t in ROLE_TEMPLATES
    }
    organization = dict.fromkeys(SCOPED, Scope.ORGANIZATION)
    assert held["owner"] == held["admin"] == {**organization, "create": None, "assign": None}
    assert held["supervisor"] == {
        **dict.fromkeys(("view", "update", "mark_won", "mark_lost"), Scope.TEAM),
        "create": None,
        "assign": None,
    }
    assert held["seller"] == {
        **dict.fromkeys(("view", "update", "mark_won", "mark_lost"), Scope.OWN),
        "create": None,
    }


def test_the_policy_is_registered_for_opportunities() -> None:
    assert policy_for(Opportunity) == AssignedTo()


def narrowed(tenant: Any, scope: Scope, user: Any = None, **changes: Any) -> Any:
    return replace(
        execution_context(tenant), permissions={"opportunities.view": frozenset({scope})}, **changes
    )


def test_the_policy_lists_exactly_what_it_allows_under_every_scope(sales: SimpleNamespace) -> None:
    """`q` y `allows` coinciden para cada oportunidad, con cada alcance y cada persona."""
    results: dict[tuple[str, Scope], set[str]] = {}
    for name in ("ana", "vera", "sam", "bea"):
        user = getattr(sales, name)
        for scope in Scope:
            with tenant_scope(ctx(sales.a, user)) as tenant:
                narrow = narrowed(tenant, scope)
                everything = list(Opportunity.objects.all())
                listed_ = set(scoped(narrow, "opportunities.view", Opportunity.objects.all()))
                allowed = {o for o in everything if can(narrow, "opportunities.view", o)}
                assert listed_ == allowed, (name, scope)
                results[name, scope] = {o.title for o in allowed}
    everyone = {"De vera", "De sam", "De ana", "Sin responsable", "Borrada"}
    assert results["ana", Scope.ORGANIZATION] == everyone  # también sin responsable y borrada
    for name, mine in (("ana", "De ana"), ("vera", "De vera"), ("sam", "De sam")):
        for scope in (Scope.OWN, Scope.BRANCH):  # sin sucursal en la oportunidad: lo propio
            assert results[name, scope] == {mine} | ({"Borrada"} if name == "vera" else set())
    assert results["sam", Scope.TEAM] == {"De sam", "De vera", "Borrada"}  # vera es del equipo
    assert results["vera", Scope.TEAM] == {"De vera", "De sam", "Borrada"}  # y al revés
    assert results["ana", Scope.TEAM] == {"De ana"}  # sin equipos: lo propio
    assert results["bea", Scope.TEAM] == results["bea", Scope.OWN] == set()
    assert results["bea", Scope.ORGANIZATION] == everyone


def test_team_covers_every_team_of_the_actor_inactive_ones_and_nobody_elsewhere(
    sales: SimpleNamespace,
) -> None:
    other = team(sales.a, "soporte")
    add(sales.a, other.pk, sales.m["ana"])  # ana comparte equipo con nadie todavía
    with tenant_scope(ctx(sales.a, sales.ana)) as tenant:
        narrow = narrowed(tenant, Scope.TEAM)
        titles = {o.title for o in scoped(narrow, "opportunities.view", Opportunity.objects.all())}
    assert titles == {"De ana"}  # el equipo existe, pero vera y sam no están en él
    add(sales.a, other.pk, sales.m["sam"], is_active=False)  # inactiva: cuenta igual
    with tenant_scope(ctx(sales.a, sales.ana)) as tenant:
        narrow = narrowed(tenant, Scope.TEAM)
        titles = {o.title for o in scoped(narrow, "opportunities.view", Opportunity.objects.all())}
        sam = Opportunity.objects.get(pk=sales.opps["sam"])
        assert can(narrow, "opportunities.view", sam)
        vera = Opportunity.objects.get(pk=sales.opps["vera"])
        assert not can(narrow, "opportunities.view", vera)
    assert titles == {"De ana", "De sam"}
    # la oportunidad de otra organización no entra por compartir un id de usuario
    with tenant_scope(ctx(sales.a, sales.ana)) as tenant:
        foreign = Opportunity(organization_id=sales.b, assigned_user_id=sales.ana.pk)
        assert not can(narrowed(tenant, Scope.ORGANIZATION), "opportunities.view", foreign)


def test_without_a_user_the_policy_lists_and_allows_nothing(sales: SimpleNamespace) -> None:
    """`assigned_user_id=None` sería `IS NULL` y daría las que no tienen responsable."""
    nobody: Any = SimpleNamespace(tenant=SimpleNamespace(user_id=None), team_ids=frozenset())
    with tenant_scope(ctx(sales.a, sales.ana)):
        everything = list(Opportunity.objects.all())
        assert any(o.assigned_user_id is None for o in everything)
        for scope in Scope:
            assert not Opportunity.objects.filter(AssignedTo().q(scope, nobody)).exists(), scope
            assert not any(AssignedTo().allows(scope, nobody, o) for o in everything), scope


def test_combined_scopes_add_up_and_a_removed_teammate_stops_counting(
    sales: SimpleNamespace,
) -> None:
    def titles(user: Any, scopes: set[Scope]) -> set[str]:
        with tenant_scope(ctx(sales.a, user)) as tenant:
            narrow = replace(
                execution_context(tenant), permissions={"opportunities.view": frozenset(scopes)}
            )
            listed_ = set(scoped(narrow, "opportunities.view", Opportunity.objects.all()))
            assert listed_ == {
                o for o in Opportunity.objects.all() if can(narrow, "opportunities.view", o)
            }
            return {o.title for o in listed_}

    both = {Scope.OWN, Scope.TEAM}
    assert (
        titles(sales.sam, both)
        == titles(sales.sam, {Scope.TEAM})
        == {"De sam", "De vera", "Borrada"}
    )
    assert titles(sales.sam, {Scope.OWN, Scope.BRANCH}) == {"De sam"}
    assert titles(sales.sam, {Scope.TEAM, Scope.ORGANIZATION}) == {
        "De vera",
        "De sam",
        "De ana",
        "Sin responsable",
        "Borrada",
    }
    with tenant_scope(ctx(sales.a)):
        TeamMember.objects.filter(membership_id=sales.m["vera"]).delete()  # vera deja el equipo
    assert titles(sales.sam, {Scope.TEAM}) == {"De sam"}  # y sus oportunidades dejan de contar
    assert titles(sales.vera, {Scope.TEAM}) == {"De vera", "Borrada"}  # sin equipos: lo propio

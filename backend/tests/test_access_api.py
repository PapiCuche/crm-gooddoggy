"""F2-05B: el motor de autorización en DRF. Denegación por defecto; 401, 404 y 403.

F2-12 amplía la auditoría del URLconf a las rutas de plataforma y a la autenticación (ADR-014).

Las vistas de DRF de prueba y su URLconf viven aquí (no en `tests/urls.py`): middleware y
resolvedor reales, sesión de Django y PostgreSQL con el rol `crm_app`.
"""

import json
import re
from collections.abc import Iterator, Mapping
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from django.db import connection
from django.db.models import QuerySet
from django.http import HttpResponse
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import URLResolver, path, re_path
from django.urls.resolvers import RegexPattern
from django.utils.decorators import method_decorator
from django.views.decorators.cache import cache_page
from drf_spectacular.utils import extend_schema, extend_schema_view
from rest_framework import generics, serializers, viewsets
from rest_framework.exceptions import NotFound
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

import config.urls
from apps.access.catalog import BY_CODE, PermissionDef
from apps.access.models import MembershipRole, Role, RolePermission
from apps.access.permissions import HasPermission, IsMember, ScopeFilter
from apps.access.services import clone_role_templates
from apps.accounts.models import User
from apps.organizations.models import OrganizationMembership
from config.settings import base
from core.api.permissions import Authenticated, Public
from core.tenancy.middleware import TENANT_PATH
from core.tenancy.scope import tenant_scope
from tests import urls as legacy_urls
from tests.factories import TEST_PASSWORD, sign_in
from tests.tenancy_app.models import Widget
from tests.test_access import NEW_PERMISSION
from tests.test_authorization import VIEW, give, world  # noqa: F401 — `world` es una fixture
from tests.test_memberships import ctx, join

pytestmark = [pytest.mark.usefixtures("tenant_db"), pytest.mark.urls(__name__)]
MANAGE = "widgets.manage"  # segundo permiso de prueba con alcance
TENANT = "api/v1/o/<slug:org_slug>/"
PLATFORM = frozenset(  # rutas de plataforma del proyecto: otra frontera (ADR-014 §4)
    {"api/schema/", "api/v1/me/organizations/"}
    | {f"api/v1/auth/{name}/" for name in ("csrf", "login", "logout", "session")}
)
MEMBER = frozenset({TENANT + "me/"})  # rutas de tenant abiertas a todo miembro activo (F2-11)
PREFIX = "api/v1/o/"  # las rutas de tenant, como `TENANT_PATH` en el middleware
API = "api/"  # todo lo que hay debajo es contrato: o es de tenant o figura en `PLATFORM`
DYNAMIC = re.compile(r"^.*\||.?[?*{]|[<(\[\\.+]")  # donde una ruta deja de ser texto literal
GUARDS: tuple[str, ...] = (
    "get_permissions", "check_permissions", "permission_denied", "initial", "dispatch",
    # F2-12: la sesión y el contrato de errores no son por vista.
    "get_authenticators", "perform_authentication", "get_authenticate_header",
    "handle_exception", "check_object_permissions",
)  # fmt: skip
STALE = "exclusión de plataforma obsoleta"
HOOKS = ("get_object", "filter_queryset")  # por donde `ScopeFilter` llega al queryset


class WidgetSerializer(serializers.ModelSerializer):
    class Meta:
        model = Widget
        fields = ("id", "name")
        read_only_fields = ("id",)  # la clave primaria nunca llega del cliente (OBS-F2-05B-2)


class WidgetView:
    serializer_class = WidgetSerializer

    def get_queryset(self) -> QuerySet[Widget]:
        return Widget.objects.order_by("name")


class WidgetList(WidgetView, generics.ListAPIView):
    required_permissions = {"GET": VIEW}


class WidgetDetail(WidgetView, generics.RetrieveUpdateDestroyAPIView):
    required_permissions = {"GET": VIEW, "PUT": MANAGE, "PATCH": MANAGE, "DELETE": MANAGE}


class Described(WidgetDetail):
    required_permissions = {**WidgetDetail.required_permissions, "OPTIONS": VIEW}


class Members(APIView):
    required_permissions = {"GET": "users.view"}  # permiso real del catálogo, sin alcance

    def get(self, request: Any, **kwargs: Any) -> Response:
        return Response({"ok": True})


class Open(Members):
    """Vista de plataforma: se excluye declarando una clase de plataforma (ADR-014 §4)."""

    permission_classes = [Public]


class Undeclared(WidgetView, generics.ListAPIView):
    """Vista de tenant sin `required_permissions`."""


class Partial(WidgetDetail):
    required_permissions = {"GET": VIEW}  # PUT, PATCH y DELETE existen y no se declaran


class Typo(Members):
    required_permissions = {"GET": "users.vew"}


class Unfiltered(WidgetList):
    filter_backends: list[Any] = []


class Defaults(APIView):
    """Vista de DRF fuera de las rutas de tenant que no declara nada."""

    def get(self, request: Any) -> Response:
        return Response({"ok": True})


class WidgetSet(WidgetView, viewsets.ModelViewSet):
    required_permissions = {"GET": VIEW}


class OwnPermissions(Members):
    def get_permissions(self) -> list[Any]:
        return [AllowAny()]


class OwnLookup(WidgetDetail):
    def get_object(self) -> Any:
        return self.get_queryset().get(pk=self.kwargs["pk"])  # sin filter_queryset()


class OwnCheck(Members):
    def check_permissions(self, request: Any) -> None:
        """No comprueba nada."""


class Lax(HasPermission):
    def has_permission(self, request: Any, view: Any) -> bool:
        return True


class CachedHandler(Members):
    @method_decorator(cache_page(300))
    def get(self, request: Any, **kwargs: Any) -> Response:
        return Response({"ok": True})


@extend_schema_view(get=extend_schema(summary="Widgets"))
class Documented(WidgetList):
    """`extend_schema_view` envuelve `get`, heredado, solo para anotar el contrato."""


class OwnAuthentication(Members):
    def perform_authentication(self, request: Any) -> None:
        """No autentica."""


class MemberWrites(Members):
    permission_classes = [IsMember]

    def post(self, request: Any, **kwargs: Any) -> Response:
        return Response({"ok": True})


class MemberHead(Members):
    permission_classes = [IsMember]

    def head(self, request: Any, **kwargs: Any) -> Response:
        return Response()


class MemberSet(viewsets.ViewSet):
    permission_classes = [IsMember]

    def list(self, request: Any, **kwargs: Any) -> Response:
        return Response({"ok": True})

    def other(self, request: Any, **kwargs: Any) -> Response:
        return Response()


class LaxMember(IsMember):
    """Una subclase no es `IsMember`: puede aflojar la regla."""


def plain(request: Any) -> HttpResponse:
    return HttpResponse("ok")


def member_view(**initkwargs: Any) -> Any:
    """La vista de solo membresía que cumple."""
    return Members.as_view(permission_classes=[IsMember], **initkwargs)


secure = [
    path(TENANT + "widgets/", WidgetList.as_view()),
    path(TENANT + "widgets/<uuid:pk>/", WidgetDetail.as_view()),
    path(TENANT + "widgets/<uuid:pk>/described/", Described.as_view()),
    path(TENANT + "members/", Members.as_view()),
    path(TENANT + "documented/", Documented.as_view()),
    path(TENANT + "self/", member_view()),
    path("api/platform/open/", Open.as_view()),
    path("api/v1/auth/session/", Members.as_view(permission_classes=[Authenticated])),
]
SECURE_PLATFORM = frozenset({"api/platform/open/", "api/v1/auth/session/"})
# Rutas de solo membresía: `self/` cumple; las otras figuran en la lista y aun así fallan.
MEMBERS_ONLY = frozenset(
    TENANT + f"self{name}/"
    for name in (
        "", "-writes", "-list", "-perm", "-dup", "-plain", "-head", "-set-head", "-sub", "-both",
        "-hook", "-noauth", "-cached", "-handler",
    )
)  # fmt: skip
# Rutas de plataforma listadas que aun así no cumplen: la lista no las salva.
LISTED = frozenset(
    {"api/platform/any/", "api/platform/both/", "api/platform/noauth/", "api/v1/auth/plain/"}
    | {"api/platform/dup/", "platform/outside/"}
    | {"api/platform/cached/", "api/platform/cached-handler/", "api/platform/own-auth/"}
)
TENANT_RE = r"^api/v1/o/(?P<org_slug>[-\w]+)/"
FLAWED: list[tuple[Any, str]] = [  # ruta y motivo que debe dar la auditoría
    (path(TENANT + "undeclared/", Undeclared.as_view()), "sin required_permissions"),
    (path(TENANT + "widgets/<uuid:pk>/partial/", Partial.as_view()), "métodos sin permiso"),
    (path(TENANT + "set/", WidgetSet.as_view({"get": "list", "post": "create"})), "['POST']"),
    (path(TENANT + "open/", Open.as_view()), "sin HasPermission"),
    (path(TENANT + "typo/", Typo.as_view()), "fuera del catálogo"),
    (path(TENANT + "unfiltered/", Unfiltered.as_view()), "sin ScopeFilter"),
    # Lo mismo, pero por ruta (`as_view(**initkwargs)`, como hacen los routers con `@action`):
    (path(TENANT + "kw-open/", Members.as_view(permission_classes=[AllowAny])), "sin Has"),
    (
        path(TENANT + "kw-any/", Members.as_view(permission_classes=[HasPermission | AllowAny])),
        "sin Has",
    ),
    (path(TENANT + "kw-sub/", Members.as_view(permission_classes=[Lax])), "sin HasPermission"),
    (path(TENANT + "kw-bare/", Members.as_view(permission_classes=HasPermission)), "sin Has"),
    (path(TENANT + "kw-list/", Members.as_view(required_permissions={"GET": [VIEW]})), "catálogo"),
    (path(TENANT + "kw-unfiltered/", WidgetList.as_view(filter_backends=[])), "sin ScopeFilter"),
    *(  # cada gancho de DRF por separado: ninguno se redefine, tampoco por ruta
        (path(TENANT + f"kw-{hook}/", Members.as_view(**{hook: list})), f"redefine {hook}")
        for hook in (  # escritos aquí, no leídos de GUARDS: quitar uno de la lista se nota
            "get_permissions",
            "get_authenticators",
            "get_authenticate_header",
            "handle_exception",
            "check_object_permissions",
            "initial",
        )
    ),
    (path(TENANT + "kw-lookup/", WidgetDetail.as_view(get_object=dict)), "redefine get_object"),
    # Ganchos de DRF redefinidos y decoradores alrededor de `as_view()`:
    (path(TENANT + "own-permissions/", OwnPermissions.as_view()), "redefine get_permissions"),
    (path(TENANT + "own-check/", OwnCheck.as_view()), "redefine check_permissions"),
    (path(TENANT + "own-lookup/<uuid:pk>/", OwnLookup.as_view()), "redefine get_object"),
    (path(TENANT + "cached/", cache_page(300)(Members.as_view())), "decorador"),
    (path("api/platform/defaults/", Defaults.as_view()), "plataforma"),
    # F2-12: toda ruta bajo `api/` es de tenant o figura en la lista, sea o no de DRF…
    (path("api/v1/auth/unlisted/", Open.as_view()), "sin exclusión explícita"),
    (path("api/v1/plain/", plain), "sin exclusión explícita"),
    # …y una ruta listada declara exactamente una clase de plataforma y la sesión del proyecto.
    (path("api/v1/auth/plain/", plain), "no es una vista de DRF"),
    (
        path("api/platform/any/", Members.as_view(permission_classes=[AllowAny])),
        "clase de plataforma",
    ),
    (
        path("api/platform/both/", Members.as_view(permission_classes=[Public, Authenticated])),
        "clase de plataforma",
    ),
    (path("api/platform/noauth/", Open.as_view(authentication_classes=[])), "autenticación"),
    # La primera ruta que casa es la que responde: una gemela correcta después no la tapa.
    (
        path("api/platform/dup/", Members.as_view(permission_classes=[AllowAny])),
        "clase de plataforma",
    ),
    # Una vista de DRF fuera de `api/` no tiene CSRF ni contrato de errores, esté o no listada.
    (path("platform/outside/", Open.as_view()), "fuera de api/"),
    # Un manejador decorado responde después del permiso y antes del filtro de alcance.
    (path(TENANT + "cached-handler/", CachedHandler.as_view()), "manejador decorado"),
    (path(TENANT + "kw-noauth/", Members.as_view(authentication_classes=[])), "autenticación"),
    (path(TENANT + "own-auth/", OwnAuthentication.as_view()), "redefine perform_authentication"),
    # Las mismas reglas valen para una ruta de plataforma listada.
    (path("api/platform/cached/", cache_page(300)(Open.as_view())), "decorador"),
    (
        path("api/platform/cached-handler/", CachedHandler.as_view(permission_classes=[Public])),
        "manejador decorado",
    ),
    (
        path("api/platform/own-auth/", OwnAuthentication.as_view(permission_classes=[Public])),
        "redefine perform_authentication",
    ),
    # F2-11: `IsMember` solo vale en una ruta listada, de solo lectura y sin queryset.
    (path(TENANT + "kw-member/", Members.as_view(permission_classes=[IsMember])), "sin Has"),
    (path(TENANT + "self-writes/", MemberWrites.as_view()), "solo lee"),
    (path(TENANT + "self-list/", WidgetList.as_view(permission_classes=[IsMember])), "solo lee"),
    (path(TENANT + "self-perm/", Members.as_view()), "sin IsMember"),
    (path(TENANT + "self-sub/", Members.as_view(permission_classes=[LaxMember])), "sin IsMember"),
    (
        path(TENANT + "self-both/", Members.as_view(permission_classes=[IsMember, AllowAny])),
        "sin IsMember",  # exactamente `[IsMember]`: ni acompañada
    ),
    (path(TENANT + "self-head/", MemberHead.as_view()), "HEAD propio"),
    (
        path(TENANT + "self-set-head/", MemberSet.as_view({"get": "list", "head": "other"})),
        "HEAD propio",
    ),
    # La primera que casa responde también aquí: la gemela correcta no borra el motivo.
    (path(TENANT + "self-dup/", MemberWrites.as_view()), "solo lee"),
    (path(TENANT + "self-plain/", plain), "no es una vista de DRF"),
    # Y las reglas comunes a toda vista valen igual con `IsMember`.
    (
        path(TENANT + "self-hook/", OwnPermissions.as_view(permission_classes=[IsMember])),
        "redefine get_permissions",
    ),
    (
        path(TENANT + "self-noauth/", member_view(authentication_classes=[])),
        "autenticación",
    ),
    (path(TENANT + "self-cached/", cache_page(300)(member_view())), "decorador"),
    (
        path(TENANT + "self-handler/", CachedHandler.as_view(permission_classes=[IsMember])),
        "manejador decorado",
    ),
    # Rutas que pueden casar con `TENANT_PATH` sin empezar por el prefijo literal:
    (path("api/<str:version>/o/<slug:org_slug>/dynamic/", Open.as_view()), "ruta dinámica"),
    (re_path(r"^apis?/v1/o/(?P<org_slug>[-\w]+)/optional/$", Open.as_view()), "ruta dinámica"),
    (re_path(r"^healthz/$|" + TENANT_RE + "either/$", Open.as_view()), "ruta dinámica"),
    (re_path(r"unanchored/", Open.as_view()), "ruta dinámica"),  # sin `^`: casa en cualquier parte
    # Sin `$`, Django casa por prefijo: `^api` y `^` sirven también las rutas de tenant.
    (re_path(r"^api", plain), "ruta dinámica"),
    (re_path(r"^", plain), "ruta dinámica"),
]
urlpatterns = [
    *secure,
    *(entry for entry, _ in FLAWED),
    path("api/platform/dup/", Open.as_view()),  # la gemela correcta de `dup/`, que nunca responde
    path(TENANT + "self-dup/", member_view()),  # y las de las rutas de solo membresía
    path(TENANT + "self-plain/", member_view()),
]


def routes(patterns: Any, prefix: str = "") -> Iterator[tuple[str, Any]]:
    for entry in patterns:
        text = str(entry.pattern)
        regex = isinstance(entry.pattern, RegexPattern)
        route = prefix + (".*" if regex and not text.startswith("^") else "") + text.lstrip("^")
        if isinstance(entry, URLResolver):
            yield from routes(entry.url_patterns, route)
        else:  # una expresión sin `$` casa por prefijo: es abierta por la derecha
            yield route + (".*" if regex and not text.endswith("$") else ""), entry.callback


def insecure(
    patterns: Any, platform: frozenset[str], member: frozenset[str] = frozenset()
) -> dict[str, str]:
    """Auditoría del URLconf: `{ruta: motivo}` de cada ruta que no puede llegar a `main`.

    Una ruta de tenant debe ser una vista de DRF con `HasPermission`, un permiso del catálogo
    por cada método que implementa y, si es genérica, `ScopeFilter`; se mira lo que la ruta usa
    de verdad (`as_view(**initkwargs)` incluido), que no redefina los ganchos de DRF y que nada
    envuelva a `as_view()`. Una vista de DRF fuera de las rutas de tenant es de plataforma: tiene
    que figurar en `platform`, y su ruta debe apartarse de `api/v1/o/` en su parte literal, antes
    de cualquier segmento dinámico (por exceso: un router de plataforma va bajo su propio prefijo).
    Es estática: una vista que consulte por su cuenta debe usar `scoped()` (OBS-F2-05B-2).
    `member` son las rutas de tenant que solo exigen ser miembro activo (`IsMember`).
    """
    found = dict.fromkeys(platform | member, STALE)
    for route, callback in routes(patterns):
        cls: Any = getattr(callback, "cls", None)
        drf = isinstance(cls, type) and issubclass(cls, APIView)
        declared = effective(callback, "required_permissions")
        literal = DYNAMIC.split(route.rstrip("$"), maxsplit=1)[0]
        if not route.startswith(PREFIX):
            if PREFIX.startswith(literal) and literal != route.rstrip("$"):
                found[route] = "ruta dinámica que puede coincidir con una ruta de tenant"
            elif not drf and not route.startswith(API):
                continue  # sondas internas: fuera del contrato
            elif drf and not route.startswith(API):
                found[route] = "vista de DRF fuera de api/: sin CSRF ni contrato de errores"
            elif route not in platform:
                found[route] = "ruta de plataforma sin exclusión explícita"
            elif reason := platform_flaw(callback, drf):
                found[route] = reason
            elif found.get(route) == STALE:
                del found[route]  # solo la marca de obsoleta: un motivo ya escrito no se borra
        elif not drf:
            found[route] = "no es una vista de DRF"
        elif route in member:
            if reason := member_flaw(callback):
                found[route] = reason
            elif found.get(route) == STALE:
                del found[route]  # solo la marca de obsoleta, como en plataforma
        elif not uses(effective(callback, "permission_classes"), HasPermission):
            found[route] = "sin HasPermission"
        elif reason := view_flaw(callback):
            found[route] = reason
        elif not isinstance(declared, Mapping):
            found[route] = "sin required_permissions"
        elif missing := implemented(callback) - set(declared):
            found[route] = f"métodos sin permiso: {sorted(missing)}"
        elif unknown := [
            c for c in declared.values() if not isinstance(c, str) or c not in BY_CODE
        ]:
            found[route] = f"permisos fuera del catálogo: {sorted(map(repr, unknown))}"
        elif not issubclass(cls, generics.GenericAPIView):
            continue
        elif not uses(effective(callback, "filter_backends"), ScopeFilter):
            found[route] = "sin ScopeFilter"
        elif redefined := [
            h for h in HOOKS if effective(callback, h) is not getattr(generics.GenericAPIView, h)
        ]:
            found[route] = "redefine " + ", ".join(redefined)
    return found


def platform_flaw(callback: Any, drf: bool) -> str | None:
    """Una ruta de plataforma listada: vista de DRF con UNA clase de plataforma y la sesión."""
    if not drf:
        return "no es una vista de DRF"
    if effective(callback, "permission_classes") not in ([Public], [Authenticated]):
        return "sin una clase de plataforma (Public o Authenticated)"
    return view_flaw(callback)


def member_flaw(callback: Any) -> str | None:
    """Una ruta de tenant abierta a todo miembro activo: solo lee el contexto propio."""
    if effective(callback, "permission_classes") != [IsMember]:
        return "sin IsMember"
    if implemented(callback) != {"GET"} or issubclass(callback.cls, generics.GenericAPIView):
        return "una ruta de solo membresía solo lee el contexto propio: GET y sin queryset"
    actions = getattr(callback, "actions", None) or {}  # DRF añade `head` al atender: se compara
    if hasattr(callback.cls, "head") or actions.get("head", actions.get("get")) != actions.get(
        "get"
    ):
        return "una ruta de solo membresía solo lee el contexto propio: HEAD propio"
    return view_flaw(callback)


def view_flaw(callback: Any) -> str | None:
    """Lo que ninguna vista de la API puede hacer, sea de tenant o de plataforma."""
    if effective(callback, "authentication_classes") != APIView.authentication_classes:
        return "cambia la autenticación"
    if redefined := [h for h in GUARDS if effective(callback, h) is not getattr(APIView, h)]:
        return "redefine " + ", ".join(redefined)
    if wrapped(callback):
        return "vista envuelta en un decorador"
    decorated = [name for name, fn in handlers(callback) if hasattr(documented(fn), "__wrapped__")]
    return f"manejador decorado: {decorated}" if decorated else None


def documented(fn: Any) -> Any:
    """`extend_schema_view` envuelve un manejador heredado solo para anotar su contrato: se mira
    el original. Cualquier otro envoltorio responde después del permiso y antes del filtro."""
    code = getattr(fn, "__code__", None)
    if (
        code is None
        or code.co_name != "wrapped_method"
        or "drf_spectacular" not in code.co_filename
    ):
        return fn
    return fn.__wrapped__


def effective(callback: Any, name: str) -> Any:
    """Lo que la ruta usa: `as_view(**initkwargs)` pisa el atributo de la clase."""
    kwargs = getattr(callback, "initkwargs", None) or {}
    return kwargs.get(name, getattr(getattr(callback, "cls", None), name, None))


def uses(items: Any, kind: type) -> bool:
    """`kind` figura tal cual; una subclase o una composición (`A | B`) se revisan a mano."""
    return isinstance(items, (list, tuple)) and any(item is kind for item in items)


def wrapped(callback: Any) -> bool:
    """¿Hay un decorador alrededor de `as_view()`? Respondería antes que `HasPermission`."""
    view = getattr(callback, "__wrapped__", None)  # DRF solo añade `csrf_exempt`
    dispatch = callback.cls.dispatch
    return view is None or getattr(view, "__wrapped__", dispatch) is not dispatch


def handlers(callback: Any) -> list[tuple[str, Any]]:
    """Las funciones que atienden cada método (o cada acción de un viewset)."""
    actions = getattr(callback, "actions", None) or {}
    names = set(actions.values()) or set(callback.cls.http_method_names)
    return [(n, getattr(callback.cls, n)) for n in sorted(names) if hasattr(callback.cls, n)]


def implemented(callback: Any) -> set[str]:
    """Métodos que la ruta atiende (OPTIONS y HEAD sin declarar ya se deniegan solos)."""
    names = getattr(callback, "actions", None) or [
        name for name in callback.cls.http_method_names if hasattr(callback.cls, name)
    ]
    return {name.upper() for name in names} - {"OPTIONS", "HEAD"}


def anyone(user: Any, organization_id: UUID) -> UUID:
    """Resolvedor demasiado permisivo: todo usuario pasa el middleware."""
    return user.pk  # type: ignore[no-any-return]


def url(tail: str = "widgets/", org: str = "org-a") -> str:
    return f"/api/v1/o/{org}/{tail}"


def detail(pk: UUID, tail: str = "", org: str = "org-a") -> str:
    return url(f"widgets/{pk}/{tail}", org)


def send(client: Client, method: str, target: str) -> tuple[int, bytes]:
    body = json.dumps({"name": "cambiado"})
    response = client.generic(method, target, body, content_type="application/json")
    return response.status_code, response.content


@pytest.fixture
def api(
    request: pytest.FixtureRequest,
    settings: Any,
    migrator: psycopg.Connection[Any],
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Any]:
    """El mundo de F2-05A con `widgets.manage`, dos widgets más y la sesión real de `ana`."""
    base_world = request.getfixturevalue("world")
    settings.MIDDLEWARE = base.MIDDLEWARE
    settings.TENANCY_MEMBERSHIP_RESOLVER = base.TENANCY_MEMBERSHIP_RESOLVER

    def drop() -> None:
        migrator.execute("DELETE FROM role_permissions WHERE permission_code = %s", [MANAGE])
        migrator.execute("DELETE FROM permissions WHERE code = %s", [MANAGE])

    drop()
    try:
        migrator.execute(NEW_PERMISSION, [MANAGE, True])
        monkeypatch.setitem(BY_CODE, MANAGE, PermissionDef(MANAGE, "x", supports_scope=True))
        ids = {"mine": uuid4(), "theirs": uuid4()}
        insert = "INSERT INTO tenancy_app_widget (id, organization_id, name, assigned_user_id) "
        for name, owner in (("mine", base_world.ana.pk), ("theirs", uuid4())):
            migrator.execute(
                insert + "VALUES (%s, %s, %s, %s)", [ids[name], base_world.a, name, owner]
            )
        client = Client()
        sign_in(client, base_world.ana)
        yield SimpleNamespace(**vars(base_world), **ids, client=client)
    finally:
        drop()


def names(migrator: psycopg.Connection[Any]) -> list[str]:
    rows = migrator.execute("SELECT name FROM tenancy_app_widget ORDER BY name").fetchall()
    return [row[0] for row in rows]


def test_unauthenticated_is_401_and_non_member_is_404(api: Any) -> None:
    give(api.a, api.membership, {VIEW: "ORGANIZATION", MANAGE: "ORGANIZATION"})
    for target in (url(), detail(api.mine), url("undeclared/")):
        assert Client().get(target).status_code == 401  # sin sesión
    targets = (url(org="org-b"), url(org="no-existe"), detail(api.orgs["widget_B"], org="org-b"))
    replies = {send(api.client, "GET", target) for target in targets}
    assert replies == {(404, b'{"code":"NOT_FOUND"}')}  # ana no es de B: indistinguible


def test_engine_denies_what_a_lax_resolver_lets_through(api: Any, settings: Any) -> None:
    settings.TENANCY_MEMBERSHIP_RESOLVER = f"{__name__}.anyone"
    give(api.a, api.membership, {VIEW: "ORGANIZATION"})
    assert api.client.get(url()).status_code == 200
    assert api.client.get(url("self/")).status_code == 200  # IsMember: basta la membresía activa
    assert api.client.head(url("self/")).status_code == 200
    for method in ("OPTIONS", "POST", "PUT", "PATCH", "DELETE"):
        assert send(api.client, method, url("self/"))[0] == 403, method  # y solo lee
        assert send(api.client, method, url("self/", org="org-b"))[0] == 404, method
    assert api.client.get(url("self/", org="org-b")).status_code == 404
    assert api.client.get(url(org="org-b")).status_code == 404  # sin membresía en B
    assert api.client.get(url("undeclared/", org="org-b")).status_code == 404  # y no 403
    with tenant_scope(ctx(api.a)):
        OrganizationMembership.objects.update(status="SUSPENDED")
    targets = (url(), detail(api.mine), detail(uuid4()), url("self/"))
    replies = {send(api.client, "GET", target) for target in targets}
    assert len(replies) == 1 and replies.pop()[0] == 404  # suspendida: como si no existiera


def test_member_without_the_permission_is_403(api: Any, migrator: psycopg.Connection[Any]) -> None:
    for target in (url(), detail(api.mine), detail(uuid4()), url("members/")):
        assert api.client.get(target).status_code == 403  # miembro sin roles
    assert api.client.head(url("members/")).status_code == 403  # HEAD: el permiso de GET
    give(api.a, api.membership, {VIEW: "ORGANIZATION", "organization.view": None})
    assert api.client.get(detail(api.mine)).status_code == 200
    assert api.client.get(url("members/")).status_code == 403  # otro permiso
    for method in ("PATCH", "PUT", "DELETE"):  # cada método, su permiso: tiene `view`, no `manage`
        for pk in (api.mine, api.theirs, uuid4()):  # exista o no: la misma respuesta
            assert send(api.client, method, detail(pk))[0] == 403
    assert names(migrator) == ["mine", "theirs", "widget A", "widget B"]


def test_view_or_method_without_declaration_is_denied(
    api: Any, migrator: psycopg.Connection[Any]
) -> None:
    give(api.a, api.membership, {VIEW: "ORGANIZATION", MANAGE: "ORGANIZATION", "users.view": None})
    assert api.client.get(url("undeclared/")).status_code == 403  # vista sin declaración
    partial = detail(api.mine, "partial/")
    for method in ("PUT", "PATCH", "DELETE", "POST", "OPTIONS"):
        assert send(api.client, method, partial)[0] == 403  # método sin declarar
    assert api.client.get(partial).status_code == api.client.head(partial).status_code == 200
    assert send(api.client, "OPTIONS", detail(api.mine))[0] == 403
    assert names(migrator) == ["mine", "theirs", "widget A", "widget B"]


def test_list_returns_only_rows_in_scope_filtered_in_sql(api: Any) -> None:
    give(api.a, api.membership, {VIEW: "OWN"})
    with CaptureQueriesContext(connection) as queries:
        listed = api.client.get(url()).json()["results"]
    assert [row["name"] for row in listed] == ["mine"]
    widgets = [query["sql"] for query in queries if "tenancy_app_widget" in query["sql"]]
    assert len(widgets) == 1 and '"assigned_user_id" IN' in widgets[0]  # no se filtra en Python
    give(api.a, api.membership, {VIEW: "ORGANIZATION"})  # otro rol: los alcances se unen
    listed = api.client.get(url()).json()["results"]
    assert [row["name"] for row in listed] == ["mine", "theirs", "widget A"]  # nada de B


def test_object_out_of_scope_or_of_another_tenant_is_404_and_unchanged(
    api: Any, migrator: psycopg.Connection[Any]
) -> None:
    give(api.a, api.membership, {VIEW: "OWN", MANAGE: "OWN"})
    hidden = (api.theirs, api.orgs["widget_A"], api.orgs["widget_B"], uuid4())
    replies = {
        send(api.client, method, detail(pk))
        for pk in hidden  # de otra persona, de nadie, de otra organización e inexistente
        for method in ("GET", "PATCH", "PUT", "DELETE")
    }
    assert len(replies) == 1 and replies.pop()[0] == 404  # indistinguibles entre sí
    assert names(migrator) == ["mine", "theirs", "widget A", "widget B"]
    assert WidgetSerializer().fields["id"].read_only  # un `id` escribible delataría a otro tenant
    assert send(api.client, "PATCH", detail(api.mine))[0] == 200  # el suyo, sí
    assert names(migrator) == ["cambiado", "theirs", "widget A", "widget B"]
    assert send(api.client, "DELETE", detail(api.mine))[0] == 204
    assert names(migrator) == ["theirs", "widget A", "widget B"]


def test_write_scope_narrower_than_read_scope_is_404_and_unchanged(
    api: Any, migrator: psycopg.Connection[Any]
) -> None:
    give(api.a, api.membership, {VIEW: "ORGANIZATION", MANAGE: "OWN"})
    assert api.client.get(detail(api.theirs)).status_code == 200  # lo ve
    for method in ("PATCH", "PUT", "DELETE"):  # pero cada método usa su permiso y su alcance
        reply = send(api.client, method, detail(api.theirs))  # lo oculta el filtro, en SQL
        assert reply[0] == 404 and reply == send(api.client, method, detail(uuid4()))
    assert names(migrator) == ["mine", "theirs", "widget A", "widget B"]
    assert send(api.client, "PATCH", detail(api.mine))[0] == 200


def test_object_check_is_a_second_barrier_and_nothing_passes_without_tenant(api: Any) -> None:
    give(api.a, api.membership, {VIEW: "OWN", MANAGE: "OWN"})
    check, get = HasPermission().has_object_permission, SimpleNamespace(method="GET")
    mine = Widget(organization_id=api.a, assigned_user_id=api.ana.pk)
    with tenant_scope(ctx(api.a, api.ana)):
        assert check(get, WidgetDetail(), mine)
        for hidden in (
            Widget(organization_id=api.a),  # de nadie
            Widget(organization_id=api.b, assigned_user_id=api.ana.pk),  # de otra organización
        ):
            for method in ("GET", "PUT", "PATCH", "DELETE"):
                with pytest.raises(NotFound):
                    check(SimpleNamespace(method=method), WidgetDetail(), hidden)
        assert not check(SimpleNamespace(method="DELETE"), Partial(), mine)  # sin declarar
        assert not HasPermission().has_permission(get, SimpleNamespace(required_permissions=VIEW))
        none = ScopeFilter().filter_queryset(get, Widget.objects.all(), Undeclared())
        assert none.query.is_empty()  # sin declaración, ninguna fila
    assert not HasPermission().has_permission(get, WidgetList())  # sin contexto de tenant
    assert not IsMember().has_permission(get, Members())  # tampoco la de solo membresía
    assert not check(get, WidgetDetail(), mine)
    none = ScopeFilter().filter_queryset(get, Widget._base_manager.all(), WidgetList())
    assert none.query.is_empty()


def test_permissions_are_read_once_per_request_and_again_on_the_next(api: Any) -> None:
    role = give(api.a, api.membership, {VIEW: "OWN"})
    with CaptureQueriesContext(connection) as queries:
        assert api.client.get(detail(api.mine)).status_code == 200
    grants = [query for query in queries if "role_permissions" in query["sql"]]
    assert len(grants) == 1  # permiso, filtro y objeto comparten una sola lectura
    with CaptureQueriesContext(connection) as queries:
        assert send(api.client, "OPTIONS", detail(api.mine, "described/"))[0] == 200
    grants = [query for query in queries if "role_permissions" in query["sql"]]
    assert len(grants) == 1  # también cuando DRF clona su Request para describir la vista
    with tenant_scope(ctx(api.a)):
        RolePermission.objects.filter(role=role).delete()
    assert api.client.get(detail(api.mine)).status_code == 403  # nada queda entre peticiones


def test_custom_role_and_renamed_owner_count_only_by_their_grants(api: Any) -> None:
    give(api.a, api.membership, {VIEW: "ORGANIZATION"}, code="warehouse_manager")
    assert len(api.client.get(url()).json()["results"]) == 3  # rol propio de la organización
    assert api.client.get(url("members/")).status_code == 403
    with tenant_scope(ctx(api.a)) as scope:
        owner = next(role for role in clone_role_templates(scope) if role.is_owner_role)
        MembershipRole.objects.create(membership_id=api.membership, role=owner)
    assert api.client.get(url("members/")).status_code == 200
    with tenant_scope(ctx(api.a)):
        Role.objects.filter(pk=owner.pk).update(code="fundador", name="Otro nombre")
    assert api.client.get(url("members/")).status_code == 200  # renombrarlo no cambia nada
    with tenant_scope(ctx(api.a)):  # Owner por código, nombre y marca, pero sin la concesión
        Role.objects.filter(pk=owner.pk).update(code="owner", name="Owner")
        RolePermission.objects.filter(role=owner, permission_id="users.view").delete()
    assert api.client.get(url("members/")).status_code == 403


def test_platform_staff_gets_no_bypass_and_platform_routes_are_excluded_by_route(api: Any) -> None:
    staff = User.objects.create_superuser("ops@example.com", TEST_PASSWORD)
    client = Client()
    sign_in(client, staff)
    for target in (url(), detail(api.mine), url("members/"), url("undeclared/")):
        assert send(client, "GET", target) == (404, b'{"code":"NOT_FOUND"}')  # sin membresía
    membership = join(api.a, staff)
    for target in (url(), detail(api.mine), url("members/"), url("undeclared/")):
        assert client.get(target).status_code == 403  # con membresía y sin concesiones
    give(api.a, membership.pk, {VIEW: "OWN"})
    assert client.get(url()).json()["results"] == []  # solo lo que conceden sus roles
    for anybody in (Client(), api.client, client):
        assert anybody.get("/api/platform/open/").status_code == 200  # declara sus clases
    assert Client().get("/api/platform/defaults/").status_code == 401  # sin declarar ni sesión
    for somebody in (api.client, client):
        assert somebody.get("/api/platform/defaults/").status_code == 403  # sin declarar: no


def test_every_tenant_route_declares_its_permissions(api: Any) -> None:
    assert insecure(config.urls.urlpatterns, PLATFORM, MEMBER) == {}  # el proyecto real
    assert insecure(secure, SECURE_PLATFORM, frozenset({TENANT + "self/"})) == {}
    assert TENANT_PATH.match("/api/v1/o/org-a/") and not TENANT_PATH.match("/api/v1/x/org-a/")


def test_audit_flags_every_insecure_route(api: Any) -> None:
    found = insecure(urlpatterns, SECURE_PLATFORM | LISTED, MEMBERS_ONLY)
    flawed = {route: reason for entry, reason in FLAWED for route, _ in routes([entry])}
    assert set(found) == set(flawed) and len(flawed) == len(FLAWED)
    for route, reason in flawed.items():
        assert reason in found[route], route
    legacy = insecure(legacy_urls.urlpatterns, PLATFORM, MEMBER)  # vistas de Django en `include()`
    assert len(legacy) == 2 and set(legacy.values()) == {"no es una vista de DRF"}
    gone = TENANT + "ya-no/"  # una exclusión que ya no corresponde a ninguna ruta se retira
    stale = insecure(config.urls.urlpatterns, PLATFORM | {"ya/no/existe/"}, MEMBER | {gone})
    assert stale == {"ya/no/existe/": STALE, gone: STALE}

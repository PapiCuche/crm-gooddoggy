"""F2-125b: `manage.py check` falla si una vista declara `step_up_methods` sin `HasPermission`."""

import subprocess
import sys
from typing import Any

import pytest
from django.core.management import call_command
from django.core.management.base import SystemCheckError
from django.urls import include, path
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

import config.urls
from apps.access import checks
from apps.access.permissions import HasPermission


class Base(APIView):
    required_permissions = {"GET": "users.view"}

    def get(self, request: Any) -> Response:
        return Response({})


def declared(name: str, classes: Any, methods: Any = frozenset({"POST"})) -> Any:
    return type(name, (Base,), {"permission_classes": classes, "step_up_methods": methods})


class Sub(HasPermission):
    pass


Good = declared("Good", [HasPermission])
Mixed = declared("Mixed", [IsAuthenticated, HasPermission])
Subclassed = declared("Subclassed", [Sub])
Bare = declared("Bare", [IsAuthenticated])
Open = declared("Open", [AllowAny, IsAuthenticated], {"GET"})
Empty = declared("Empty", [IsAuthenticated], set())
Missing = declared("Missing", [IsAuthenticated], None)
Composed = declared("Composed", [IsAuthenticated | HasPermission])  # no se sabe cuál aplica


def routes(*views: Any) -> list[Any]:
    return [path(f"v/{view.__name__}/", view.as_view()) for view in views]


def test_a_view_with_step_up_methods_and_no_has_permission_is_reported() -> None:
    assert checks.unenforced(routes(Bare, Open, Composed)) == [Bare, Open, Composed]


def test_has_permission_alone_mixed_with_others_or_subclassed_is_enough() -> None:
    assert checks.unenforced(routes(Good, Mixed, Subclassed)) == []


def test_a_view_without_a_step_up_declaration_is_not_reported() -> None:
    assert checks.unenforced(routes(Empty, Missing, Base)) == []


def test_it_walks_nested_includes_and_reports_each_view_once() -> None:
    nested = [path("a/", include([path("b/", include(routes(Bare)))])), *routes(Bare)]
    assert checks.unenforced(nested) == [Bare]


def test_the_real_urlconf_has_no_unenforced_step_up() -> None:
    assert checks.step_up_is_enforced(None) == []


def test_manage_check_fails_for_a_misconfigured_view(settings: Any) -> None:
    settings.ROOT_URLCONF = __name__
    with pytest.raises(SystemCheckError, match=checks.UNENFORCED):
        call_command("check", "--tag", "urls")
    messages = checks.step_up_is_enforced(None)
    assert [(m.id, m.obj) for m in messages] == [(checks.UNENFORCED, Bare)]


def test_the_app_registers_the_check_when_django_starts() -> None:
    """En un proceso nuevo, sin importar `checks` antes: lo registra `AccessConfig.ready`."""
    code = (
        "import django; django.setup(); from django.core.checks.registry import registry;"
        "print([c.__name__ for c in registry.get_checks()].count('step_up_is_enforced'))"
    )
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)  # noqa: S603
    assert done.stdout.strip() == "1", done.stderr


urlpatterns = [*config.urls.urlpatterns, *routes(Good, Bare, Missing)]

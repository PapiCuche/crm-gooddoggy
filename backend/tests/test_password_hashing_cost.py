"""CI-PERF-01: la suite baja el coste de Argon2id solo en tests; producción conserva el de Django.

`tests/conftest.py` fija el coste mínimo en la clase del hasher para que `create_user` no cueste
unos 80 ms de CPU. Estos tests impiden que ese ajuste (o uno parecido) llegue a producción o que
el coste de producción baje en silencio (p. ej. en un salto de versión de Django).
"""

import json
import os
import pathlib
import re
import subprocess
import sys

from django.contrib.auth.hashers import make_password

# Valores por defecto de Django 5.2 (la clase sin tocar): lo que carga producción.
PRODUCTION_COST = {"time_cost": 2, "memory_cost": 102400, "parallelism": 8}
BACKEND = pathlib.Path(__file__).resolve().parents[1]
# Intérprete limpio (sin conftest) con los ajustes de Django cargados: si algún settings tocara la
# clase del hasher, se vería aquí.
CLEAN_INTERPRETER_CODE = """
import json, sys
sys.path.insert(0, sys.argv[1])
import django
django.setup()
from django.contrib.auth.hashers import Argon2PasswordHasher as H
print(json.dumps({k: getattr(H, k) for k in ('time_cost', 'memory_cost', 'parallelism')}))
"""


def test_a_clean_interpreter_hashes_with_at_least_the_production_cost() -> None:
    """Sin conftest: el coste que cargaría producción (la clase de Django sin tocar)."""
    result = subprocess.run(  # noqa: S603 — nuestro propio intérprete, código fijo
        [sys.executable, "-I", "-c", CLEAN_INTERPRETER_CODE, str(BACKEND)],
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, "DJANGO_SETTINGS_MODULE": "config.settings.test"},
    )
    cost = json.loads(result.stdout)
    assert all(cost[k] >= v for k, v in PRODUCTION_COST.items()), cost


def test_nothing_outside_the_tests_changes_the_hasher_cost() -> None:
    pattern = re.compile(r"\b(time_cost|memory_cost|parallelism)\b")
    offenders = [
        str(path.relative_to(BACKEND))
        for path in BACKEND.rglob("*.py")
        if not {".venv", "tests"} & set(path.relative_to(BACKEND).parts)
        and not path.name.startswith("test_")
        and pattern.search(path.read_text())
    ]
    assert offenders == [], offenders


def test_the_cheaper_test_cost_is_still_argon2id() -> None:
    assert make_password("una-clave-larga-123").startswith("argon2$argon2id$")

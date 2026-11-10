"""Reparto de la suite del backend en shards paralelos (CI-PERF-01; ADR-009).

Solo stdlib. Cada shard del CI recolecta la suite completa (`pytest --collect-only -q`, ~8 s),
calcula el MISMO reparto determinista por archivo (el más pesado primero, al shard menos
cargado) y ejecuta solo sus archivos. Dos modos:

  plan    — lee la recolección por stdin, comprueba que los shards son una partición exacta
            (disjuntos y suman el total) y escribe los archivos del shard, uno por línea.
  verify  — compara el informe JUnit del shard con lo planeado: ni un test de más ni de
            menos, y todos dentro de sus archivos (cada test corre exactamente una vez).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import xml.etree.ElementTree as ET  # noqa: S405 — el informe lo genera nuestro propio pytest
from collections import Counter

NODE_RE = re.compile(r"^(?P<file>[^\s:]+\.py)::")


def collected_counts(text: str) -> Counter[str]:
    """Tests recolectados por archivo (`-q` imprime un node id por línea)."""
    counts: Counter[str] = Counter()
    for line in text.splitlines():
        m = NODE_RE.match(line)
        if m:
            counts[m["file"]] += 1
    return counts


def weigh(counts: Counter[str], seconds: dict[str, float]) -> dict[str, float]:
    """Peso por archivo: segundos medidos en CI; si falta, tests x segundos medios por test."""
    known = [n for n in counts if n in seconds and counts[n]]
    mean = sum(seconds[n] for n in known) / max(1, sum(counts[n] for n in known)) or 1.0
    return {n: seconds.get(n, counts[n] * mean) for n in counts}


def partition(weights: dict[str, float], shards: int) -> list[list[str]]:
    """LPT determinista: archivos por peso descendente (y nombre), al shard con menos carga."""
    bins: list[list[str]] = [[] for _ in range(shards)]
    load = [0.0] * shards
    for name, weight in sorted(weights.items(), key=lambda kv: (-kv[1], kv[0])):
        target = min(range(shards), key=lambda i: (load[i], i))
        bins[target].append(name)
        load[target] += weight
    return [sorted(b) for b in bins]


def check_partition(counts: Counter[str], plan: list[list[str]]) -> None:
    assigned = [name for shard in plan for name in shard]
    if len(assigned) != len(set(assigned)) or set(assigned) != set(counts):
        raise SystemExit("reparto inválido: archivos duplicados u omitidos entre shards")
    total = sum(counts.values())
    summed = sum(counts[name] for shard in plan for name in shard)
    if summed != total:
        raise SystemExit(f"reparto inválido: los shards suman {summed} tests y la suite {total}")
    if any(not shard for shard in plan):
        raise SystemExit("reparto inválido: hay un shard vacío")


def file_seconds(junit_path: str, files: list[str]) -> dict[str, float]:
    """Segundos por archivo según JUnit (para regenerar el archivo de pesos)."""
    out: dict[str, float] = {}
    for case in ET.parse(junit_path).getroot().iter("testcase"):  # noqa: S314
        name = module_file(case.get("classname", ""), set(files))
        if name:
            out[name] = out.get(name, 0.0) + float(case.get("time", 0))
    return {n: round(t, 1) for n, t in sorted(out.items())}


def executed(junit_path: str) -> Counter[str]:
    """Tests ejecutados por módulo según JUnit (`classname` = ruta con puntos + clase)."""
    counts: Counter[str] = Counter()
    for case in ET.parse(junit_path).getroot().iter("testcase"):  # noqa: S314
        counts[case.get("classname", "")] += 1
    return counts


def module_file(classname: str, files: set[str]) -> str | None:
    """`tests.test_x.TestY` -> `tests/test_x.py` (la clase es opcional)."""
    parts = classname.split(".")
    for end in range(len(parts), 0, -1):
        candidate = "/".join(parts[:end]) + ".py"
        if candidate in files:
            return candidate
    return None


def verify(counts: Counter[str], files: list[str], junit_path: str) -> str:
    expected = {name: counts[name] for name in files}
    ran: Counter[str] = Counter()
    for classname, n in executed(junit_path).items():
        name = module_file(classname, set(expected))
        if name is None:
            raise SystemExit(f"se ejecutó un test fuera de los archivos del shard: {classname}")
        ran[name] += n
    if ran != Counter(expected):
        diff = {n: (expected[n], ran[n]) for n in expected if expected[n] != ran[n]}
        raise SystemExit(f"tests ejecutados distintos de los planeados (archivo: plan, real): {diff}")
    return f"{sum(ran.values())} tests ejecutados en {len(files)} archivos: coincide con el plan"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["plan", "verify"])
    ap.add_argument("--shards", type=int, required=True)
    ap.add_argument("--shard", type=int, required=True, help="1..shards")
    ap.add_argument("--collected", required=True, help="salida de `pytest --collect-only -q`")
    ap.add_argument("--junit", help="informe JUnit del shard (modo verify)")
    ap.add_argument("--weights", help="JSON {archivo: segundos} medido en CI (opcional)")
    args = ap.parse_args()
    if not 1 <= args.shard <= args.shards:
        raise SystemExit("--shard fuera de rango")
    with open(args.collected, encoding="utf-8") as fh:
        counts = collected_counts(fh.read())
    seconds: dict[str, float] = {}
    if args.weights:
        with open(args.weights, encoding="utf-8") as fh:
            seconds = json.load(fh)
    plan = partition(weigh(counts, seconds), args.shards)
    check_partition(counts, plan)
    mine = plan[args.shard - 1]
    total = sum(counts.values())
    per_shard = [sum(counts[n] for n in shard) for shard in plan]
    if args.mode == "plan":
        print(f"suite: {total} tests; shards: {per_shard}; este: {args.shard}/{args.shards}", file=sys.stderr)
        print("\n".join(mine))
        return 0
    if not args.junit:
        raise SystemExit("verify exige --junit")
    print(f"shard {args.shard}/{args.shards} (suite {total}, reparto {per_shard}): {verify(counts, mine, args.junit)}")
    print("SECONDS " + json.dumps(file_seconds(args.junit, mine), separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main())

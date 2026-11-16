"""Retención de backups (F1-16, ADR-033 §8.2): los prefijos de backup.sh más las reglas de
lifecycle.json guardan 7 diarios, 4 semanales y 6 mensuales, y solo borran lo que sale de la política.

Simula dos años de noches: cada volcado sube a los prefijos que da `retention_prefixes` (lib.sh), con
el periodo de la última noche buena como backup.sh, y la regla de su prefijo lo borra como S3: creación
+ Days, redondeado a la medianoche UTC siguiente. Cada plazo es el mínimo que cumple la política: con un
día menos, se borraría una copia que hay que guardar. Una noche fallida (el domingo o el día 1) se
recupera en la siguiente buena (F1-16b).

También, que el simulacro corre cuando cambia una migración y que sus imágenes van por digest.
"""

import json
import re
import subprocess
import unittest
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
BACKUP = REPO / "infra/prod/backup"
START = date(2026, 1, 1)
DAYS = [START + timedelta(n) for n in range(730)]
POLICY = {"daily": 7, "weekly": 4, "monthly": 6}
NONCURRENT_MIN = 30  # un servidor comprometido que sobrescribe no borra nada antes de 30 días


def prefixes(kind: str = "nightly", failed: frozenset[date] = frozenset()) -> dict[date, list[str]]:
    """Lo que sube cada noche (03:15 UTC). Una noche fallida no sube nada ni guarda su periodo."""
    epoch = date(1970, 1, 1)
    args = " ".join(f"{(d - epoch).days * 86400 + 11700},{d:%Y-%m},{int(d in failed)}" for d in DAYS)
    script = (
        f'. "{BACKUP}/lib.sh"; last=; for n in {args}; do s=${{n%%,*}} f=${{n##*,}} m=${{n#*,}}; '
        f'p=$(backup_period "$s" "${{m%,*}}"); if [ "$f" = 1 ]; then echo; continue; fi; '
        f'echo $(retention_prefixes {kind} "$p" "$last"); last=$p; done'
    )
    out = subprocess.run(["bash", "-c", script], check=True, capture_output=True, text=True).stdout
    return {d: line.split() for d, line in zip(DAYS, out.splitlines(), strict=True)}


def period(d: date, tier: str) -> tuple[int, int]:
    """La semana (de domingo a sábado) o el mes de un día."""
    sunday = d - timedelta((d.weekday() + 1) % 7)
    return (sunday.toordinal(), 0) if tier == "weekly" else (d.year, d.month)


def checked_rules(rules: list[dict]) -> list[dict]:
    """Solo formas que la simulación entiende: una regla de otra forma podría borrar sin que se vea."""
    for rule in rules:
        name = rule.get("ID")
        assert rule.get("Status") == "Enabled", f"{name}: Status {rule.get('Status')}"
        assert set(rule.get("Filter", {"?": 0})) == {"Prefix"}, f"{name}: filtro {rule.get('Filter')}"
        assert "Date" not in rule.get("Expiration", {}), f"{name}: Expiration.Date"
        assert not {"Transitions", "Transition", "NoncurrentVersionTransitions"} & set(rule), f"{name}: transiciones"
    return rules


def expiry(rules: list[dict], key: str) -> int | None:
    """Los días que vive un objeto actual: el plazo más corto de las reglas cuyo prefijo lo cubre."""
    days = [r["Expiration"]["Days"] for r in rules
            if key.startswith(r["Filter"]["Prefix"]) and "Days" in r.get("Expiration", {})]  # fmt: skip
    return min(days, default=None)


def loses(uploads: dict[date, list[str]], tier: str, days: int) -> date | None:
    """El primer día en que la política pierde una copia con ese plazo, o None si nunca la pierde."""
    for today in DAYS[200:]:  # justo antes de la noche `today`
        made = [d for d in DAYS if d < today and tier in uploads[d]]
        present = {d for d in made if today <= d + timedelta(days)}  # Days=N: hasta la medianoche de d+N+1
        if not set(made[-POLICY[tier] :]) <= present:
            return today
    return None


class RetentionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.uploads = prefixes()
        self.rules = checked_rules(json.loads((BACKUP / "lifecycle.json").read_text())["Rules"])

    def test_prefixes_by_period(self) -> None:
        self.assertEqual(self.uploads[date(2026, 11, 2)], ["daily"])  # lunes
        self.assertEqual(self.uploads[date(2026, 11, 1)], ["daily", "weekly", "monthly"])  # domingo y día 1
        self.assertEqual(self.uploads[date(2026, 11, 7)], ["daily"])  # sábado
        self.assertEqual(self.uploads[date(2026, 11, 8)], ["daily", "weekly"])  # domingo
        self.assertEqual(self.uploads[date(2026, 12, 1)], ["daily", "monthly"])
        self.assertEqual(prefixes("predeploy")[date(2026, 11, 1)], ["predeploy"])

    def test_a_missed_night_is_caught_up_on_the_next_good_one(self) -> None:
        # Fallan el domingo 1 (también día 1), el domingo 8 y el lunes 9, y el martes 1 de diciembre.
        failed = frozenset({date(2026, 11, 1), date(2026, 11, 8), date(2026, 11, 9), date(2026, 12, 1)})
        up = prefixes(failed=failed)
        self.assertEqual(up[date(2026, 11, 1)], [])
        self.assertEqual(up[date(2026, 11, 2)], ["daily", "weekly", "monthly"])
        self.assertEqual(up[date(2026, 11, 3)], ["daily"])  # una sola copia por periodo
        self.assertEqual(up[date(2026, 11, 10)], ["daily", "weekly"])
        self.assertEqual(up[date(2026, 11, 15)], ["daily", "weekly"])  # el periodo siguiente, a su hora
        self.assertEqual(up[date(2026, 12, 2)], ["daily", "monthly"])
        for tier in ("weekly", "monthly"):  # cada semana y cada mes tienen exactamente una copia
            per = Counter(period(d, tier) for d in DAYS if tier in up[d])
            self.assertEqual(set(per.values()), {1}, tier)
            self.assertEqual(len(per), len({period(d, tier) for d in DAYS}), tier)

    def test_keeps_the_policy_and_deletes_only_what_is_outside_it(self) -> None:
        for tier in POLICY:
            days = expiry(self.rules, f"db/{tier}/crm-x.dump.age")
            self.assertIsNotNone(days, f"{tier}: sin regla de caducidad")
            self.assertIsNone(loses(self.uploads, tier, days), f"{tier}: {days} días borran una copia de la política")
            self.assertIsNotNone(loses(self.uploads, tier, days - 1), f"{tier}: {days} días guardan de más")
        self.assertEqual(expiry(self.rules, "db/predeploy/crm-x.dump.age"), 7)

    def test_overwritten_versions_survive_and_media_never_expires(self) -> None:
        for rule_id, prefix in (("db-versions", "db/"), ("media-versions", "media/")):
            rule = next((r for r in self.rules if r.get("ID") == rule_id), None)
            self.assertIsNotNone(rule, f"falta la regla {rule_id}")
            self.assertEqual(rule["Filter"]["Prefix"], prefix)
            covering = [r["NoncurrentVersionExpiration"]["NoncurrentDays"] for r in self.rules
                        if "NoncurrentVersionExpiration" in r
                        and (prefix.startswith(r["Filter"]["Prefix"]) or r["Filter"]["Prefix"].startswith(prefix))]  # fmt: skip
            self.assertGreaterEqual(min(covering), NONCURRENT_MIN, f"{prefix}: versiones antiguas a {covering} días")
        self.assertIsNone(expiry(self.rules, "media/org/x/attachment/2026/11/y.age"))


class DrillTest(unittest.TestCase):
    def test_runs_when_a_backend_migration_changes(self) -> None:
        text = (REPO / ".github/workflows/infra-prod.yml").read_text()
        paths = re.search(r"paths: &paths\n((?:\s+- .*\n)+)", text)
        self.assertIsNotNone(paths)
        self.assertIn('"backend/**/migrations/**"', paths.group(1))

    def test_images_are_pinned_by_digest(self) -> None:
        text = (BACKUP / "drill.sh").read_text()
        for image in ("dxflrs/garage", "caddy", "postgres", "redis"):
            refs = re.findall(rf"(?<![\w/.]){re.escape(image)}:[\w-]*\.[\w.-]+(?:@sha256:[0-9a-f]{{64}})?", text)
            self.assertTrue(refs, f"{image}: no aparece en drill.sh")
            for ref in refs:
                self.assertRegex(ref, r"@sha256:[0-9a-f]{64}$", f"{ref}: sin digest")


if __name__ == "__main__":
    unittest.main()

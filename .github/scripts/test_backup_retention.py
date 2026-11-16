"""Retención de backups (F1-16, ADR-033 §8.2): los prefijos de backup.sh más las reglas de
lifecycle.json guardan 7 diarios, 4 semanales y 6 mensuales, y solo borran lo que sale de la política.

Simula dos años de noches: cada volcado sube a los prefijos que da `retention_prefixes` (lib.sh) y la
regla de su prefijo lo borra como S3: creación + Days, redondeado a la medianoche UTC siguiente. Cada
plazo es el mínimo que cumple la política: con un día menos, se borraría una copia que hay que guardar.
"""

import json
import subprocess
import unittest
from datetime import date, timedelta
from pathlib import Path

BACKUP = Path(__file__).resolve().parents[2] / "infra/prod/backup"
START = date(2026, 1, 1)
DAYS = [START + timedelta(n) for n in range(730)]
POLICY = {"daily": 7, "weekly": 4, "monthly": 6}
NONCURRENT_MIN = 30  # un servidor comprometido que sobrescribe no borra nada antes de 30 días


def prefixes(kind: str = "nightly") -> dict[date, list[str]]:
    args = " ".join(f"{d.isoweekday()},{d:%d}" for d in DAYS)
    script = (
        f'. "{BACKUP}/lib.sh"; for d in {args}; do '
        f'echo $(retention_prefixes {kind} "${{d%,*}}" "${{d#*,}}"); done'
    )
    out = subprocess.run(["bash", "-c", script], check=True, capture_output=True, text=True).stdout
    return {d: line.split() for d, line in zip(DAYS, out.splitlines(), strict=True)}


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

    def test_prefixes_by_calendar(self) -> None:
        self.assertEqual(self.uploads[date(2026, 11, 2)], ["daily"])  # lunes
        self.assertEqual(self.uploads[date(2026, 11, 1)], ["daily", "weekly", "monthly"])  # domingo y día 1
        self.assertEqual(self.uploads[date(2026, 12, 1)], ["daily", "monthly"])
        self.assertEqual(prefixes("predeploy")[date(2026, 11, 1)], ["predeploy"])

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


if __name__ == "__main__":
    unittest.main()

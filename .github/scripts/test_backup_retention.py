"""Retención de backups (F1-16, ADR-033 §8.2): los prefijos de backup.sh más las reglas de
lifecycle.json guardan 7 diarios, 4 semanales y 6 mensuales, y solo borran lo que sale de la política.

Simula dos años de noches: cada volcado sube a los prefijos que da `retention_prefixes` (lib.sh) y la
regla de su prefijo lo borra como S3: creación + Days, redondeado a la medianoche UTC siguiente.
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


def prefixes(kind: str = "nightly") -> dict[date, list[str]]:
    args = " ".join(f"{d.isoweekday()},{d:%d}" for d in DAYS)
    script = (
        f'. "{BACKUP}/lib.sh"; for d in {args}; do '
        f'echo $(retention_prefixes {kind} "${{d%,*}}" "${{d#*,}}"); done'
    )
    out = subprocess.run(["bash", "-c", script], check=True, capture_output=True, text=True).stdout
    return {d: line.split() for d, line in zip(DAYS, out.splitlines(), strict=True)}


def rules() -> dict[str, int]:
    config = json.loads((BACKUP / "lifecycle.json").read_text())["Rules"]
    days = {r["Filter"]["Prefix"]: r["Expiration"]["Days"] for r in config if "Days" in r.get("Expiration", {})}
    return {prefix.removeprefix("db/").strip("/"): n for prefix, n in days.items()}


class RetentionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.uploads = prefixes()
        self.expire = rules()

    def test_prefixes_by_calendar(self) -> None:
        self.assertEqual(self.uploads[date(2026, 11, 2)], ["daily"])  # lunes
        self.assertEqual(self.uploads[date(2026, 11, 1)], ["daily", "weekly", "monthly"])  # domingo y día 1
        self.assertEqual(self.uploads[date(2026, 12, 1)], ["daily", "monthly"])
        self.assertEqual(prefixes("predeploy")[date(2026, 11, 1)], ["predeploy"])

    def test_every_prefix_has_a_rule_and_media_never_expires(self) -> None:
        self.assertEqual(set(self.expire), {"daily", "weekly", "monthly", "predeploy"})
        self.assertEqual(self.expire["predeploy"], 7)
        config = json.loads((BACKUP / "lifecycle.json").read_text())["Rules"]
        media = [r for r in config if r["Filter"]["Prefix"].startswith("media/")]
        self.assertTrue(media and all("Days" not in r.get("Expiration", {}) for r in media))

    def test_keeps_the_policy_and_deletes_only_what_is_outside_it(self) -> None:
        # Justo antes de la noche `today`: lo que sigue en el destino frente a lo que exige la política.
        for today in DAYS[200:]:
            for tier, keep in POLICY.items():
                made = [d for d in DAYS if d < today and tier in self.uploads[d]]
                # S3: un objeto con Days=N creado el día d desaparece a la medianoche que empieza d+N+1.
                present = [d for d in made if today <= d + timedelta(self.expire[tier])]
                wanted = made[-keep:]
                self.assertTrue(set(wanted) <= set(present), f"{today} {tier}: borra {set(wanted) - set(present)}")
                extra = sorted(set(present) - set(wanted))
                # El mensual (184 días) guarda a veces un séptimo mientras el mes es corto; nunca más.
                self.assertLessEqual(len(extra), 1 if tier == "monthly" else 0, f"{today} {tier}: guarda {extra}")


if __name__ == "__main__":
    unittest.main()

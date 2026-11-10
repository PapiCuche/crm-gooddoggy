import pathlib
import tempfile
import re
import unittest

import shard_backend as sb

COLLECTED = "\n".join(
    [f"tests/test_a.py::test_{i}" for i in range(5)]
    + [f"tests/test_b.py::TestB::test_{i}[x y]" for i in range(3)]
    + [f"tests/test_c.py::test_{i}" for i in range(2)]
    + ["", "10 tests collected in 1.00s"]
)


def junit(cases):
    body = "".join(f'<testcase classname="{c}" name="t{i}"/>' for i, c in enumerate(cases))
    return f'<testsuites><testsuite tests="{len(cases)}">{body}</testsuite></testsuites>'


class ShardTest(unittest.TestCase):
    def test_counts_ignore_non_node_lines(self):
        self.assertEqual(sb.collected_counts(COLLECTED), {"tests/test_a.py": 5, "tests/test_b.py": 3, "tests/test_c.py": 2})

    def test_partition_is_exact_and_deterministic(self):
        counts = sb.collected_counts(COLLECTED)
        plan = sb.partition(sb.weigh(counts, {}), 2)
        sb.check_partition(counts, plan)
        self.assertEqual(plan, sb.partition(sb.weigh(counts, {}), 2))
        self.assertEqual(sorted(n for s in plan for n in s), sorted(counts))

    def test_measured_seconds_move_the_heavy_file_alone(self):
        counts = sb.collected_counts(COLLECTED)
        plan = sb.partition(sb.weigh(counts, {"tests/test_a.py": 1.0, "tests/test_b.py": 1.0, "tests/test_c.py": 100.0}), 2)
        self.assertIn(["tests/test_c.py"], plan)

    def test_partition_rejects_duplicates_and_omissions(self):
        counts = sb.collected_counts(COLLECTED)
        with self.assertRaises(SystemExit):
            sb.check_partition(counts, [["tests/test_a.py", "tests/test_b.py"], ["tests/test_b.py", "tests/test_c.py"]])
        with self.assertRaises(SystemExit):
            sb.check_partition(counts, [["tests/test_a.py"], ["tests/test_b.py"]])
        with self.assertRaises(SystemExit):
            sb.check_partition(counts, [["tests/test_a.py", "tests/test_b.py", "tests/test_c.py"], []])

    def run_verify(self, files, cases):
        counts = sb.collected_counts(COLLECTED)
        with tempfile.TemporaryDirectory() as d:
            path = pathlib.Path(d, "r.xml")
            path.write_text(junit(cases))
            return sb.verify(counts, files, str(path))

    def test_verify_matches_plan(self):
        cases = ["tests.test_b.TestB"] * 3 + ["tests.test_c"] * 2
        self.assertIn("5 tests ejecutados", self.run_verify(["tests/test_b.py", "tests/test_c.py"], cases))

    def test_verify_fails_on_missing_extra_or_foreign_tests(self):
        files = ["tests/test_b.py", "tests/test_c.py"]
        with self.assertRaises(SystemExit):
            self.run_verify(files, ["tests.test_b.TestB"] * 3 + ["tests.test_c"])
        with self.assertRaises(SystemExit):
            self.run_verify(files, ["tests.test_b.TestB"] * 3 + ["tests.test_c"] * 3)
        with self.assertRaises(SystemExit):
            self.run_verify(files, ["tests.test_b.TestB"] * 3 + ["tests.test_c"] * 2 + ["tests.test_a"])

    def test_module_file_maps_classes_to_files(self):
        files = {"tests/test_b.py"}
        self.assertEqual(sb.module_file("tests.test_b.TestB", files), "tests/test_b.py")
        self.assertEqual(sb.module_file("tests.test_b", files), "tests/test_b.py")
        self.assertIsNone(sb.module_file("tests.test_z", files))


class WorkflowTest(unittest.TestCase):
    def test_matrix_lists_every_shard_without_gaps(self):
        """El plan usa `strategy.job-total`: un hueco en la matriz dejaría archivos sin ejecutar."""
        text = (pathlib.Path(__file__).resolve().parents[1] / "workflows" / "backend.yml").read_text()
        m = re.search(r"^\s+shard: \[([\d, ]+)\]", text, re.M)
        self.assertIsNotNone(m)
        shards = [int(n) for n in m.group(1).split(",")]
        self.assertEqual(shards, list(range(1, len(shards) + 1)))
        self.assertIn("name: backend tests (shard ${{ matrix.shard }}/${{ strategy.job-total }})", text)


if __name__ == "__main__":
    unittest.main()

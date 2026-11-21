import unittest
from types import SimpleNamespace

from selector import executor_fixture_readers


class ExecutorFixtureSelectionTest(unittest.TestCase):
    def setUp(self):
        prefix = "tests/test_executor.py::TestExecutor::"
        self.index = SimpleNamespace(unknown=[], test_ids={
            11: prefix + "test_execute_tpch",
            22: prefix + "test_execute_tpcds",
            33: prefix + "test_optimized_tpch",
            44: prefix + "test_array_slice",
            55: "tests/test_optimizer.py::TestOptimizer::test_fixture",
        })
        self.before = "# execute: true\nSELECT x; SELECT expected;"
        self.readers = set(self.index.test_ids)

    def readers_after(self, suite, after):
        path = f"tests/fixtures/optimizer/{suite}/{suite}.sql"
        return executor_fixture_readers(path, self.before, after, self.readers, self.index)

    def test_expected_query_edit(self):
        after = self.before.replace("SELECT expected", "INVALID")
        self.assertEqual(self.readers_after("tpc-h", after), {33, 55})
        self.assertEqual(self.readers_after("tpc-ds", after), {55})

    def test_input_edit(self):
        after = self.before.replace("SELECT x", "INVALID")
        self.assertEqual(self.readers_after("tpc-h", after), {11, 33, 55})
        self.assertEqual(self.readers_after("tpc-ds", after), {22, 55})

    def test_uncertain_setup_or_unknown_test_inventory(self):
        for after in ("# broken\nSELECT x; result;", "# execute: true; result;",
                      self.before + "another; pair;"):
            self.assertEqual(self.readers_after("tpc-h", after), self.readers)
        self.index.unknown = [66]
        self.assertEqual(self.readers_after("tpc-h", self.before), self.readers)

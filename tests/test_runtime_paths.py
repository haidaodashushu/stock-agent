import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from config.runtime_paths import PROJECT_ROOT, configurable_path
from data.strategic_theme_pool import load_strategic_pool


class RuntimePathTests(unittest.TestCase):
    def test_default_path_is_project_relative(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(
                configurable_path("STOCK_TEST_CONFIG", "config/example.local.json"),
                PROJECT_ROOT / "config/example.local.json",
            )

    def test_relative_override_is_project_relative(self):
        with patch.dict(os.environ, {"STOCK_TEST_CONFIG": "private/account.json"}):
            self.assertEqual(
                configurable_path("STOCK_TEST_CONFIG", "unused.json"),
                PROJECT_ROOT / "private/account.json",
            )

    def test_absolute_override_and_user_expansion(self):
        with tempfile.TemporaryDirectory() as temporary:
            expected = Path(temporary) / "account.json"
            with patch.dict(os.environ, {"STOCK_TEST_CONFIG": str(expected)}):
                self.assertEqual(
                    configurable_path("STOCK_TEST_CONFIG", "unused.json"), expected,
                )

    def test_public_json_examples_are_valid(self):
        examples = sorted((PROJECT_ROOT / "config").glob("*.example.json"))
        self.assertGreaterEqual(len(examples), 5)
        for path in examples:
            with self.subTest(path=path.name):
                self.assertIsInstance(json.loads(path.read_text(encoding="utf-8")), dict)

    def test_strategic_pool_example_satisfies_contract(self):
        path = PROJECT_ROOT / "config/strategic_theme_pool.example.json"
        load_strategic_pool.cache_clear()
        try:
            pool = load_strategic_pool(path)
            self.assertEqual(pool["target_size"], len(pool["stocks"]))
        finally:
            load_strategic_pool.cache_clear()


if __name__ == "__main__":
    unittest.main()

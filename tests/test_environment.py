import sys
from pathlib import Path
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import check_environment as env


class EnvironmentTests(unittest.TestCase):
    def run_check(self, engines, mode="basic", missing=None):
        def load(name):
            if name == missing:
                raise ImportError("missing")
        with patch.object(env.platform, "system", return_value="Windows"), \
             patch.object(env, "discover_acc", return_value=engines), \
             patch.object(env.importlib, "import_module", side_effect=load), \
             patch.object(env.importlib.metadata, "version", return_value="99.0"):
            return env.check(mode)

    def test_basic_ready_without_print_dependencies(self):
        self.assertTrue(self.run_check([(2020, "cad")], missing="cv2")["ready"])

    def test_missing_runtime_dependency_blocks(self):
        result = self.run_check([(2022, "cad")], "print", "cv2")
        self.assertFalse(result["ready"])
        self.assertTrue(next(c for c in result["checks"] if c["name"] == "opencv-python-headless")["next_step"])

    def test_no_engine_blocks(self):
        self.assertFalse(self.run_check([])["ready"])

    def test_strip_rejects_old_and_unknown_year(self):
        for year in (0, 2020, 2021):
            self.assertFalse(self.run_check([(year, "cad")], "strip")["ready"])
        self.assertTrue(self.run_check([(2022, "cad")], "strip")["ready"])


if __name__ == "__main__":
    unittest.main()

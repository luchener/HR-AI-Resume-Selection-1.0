"""Verify dotenv loading can be disabled without reading workspace secrets."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
import uuid


class DotenvLoadingTests(unittest.TestCase):
    def run_config(self, *, disabled=None):
        root = Path(__file__).resolve().parents[2] / ".test-tmp" / (
            "config-dotenv-" + uuid.uuid4().hex)
        try:
            backend = root / "backend"
            backend.mkdir(parents=True)
            source = Path(__file__).resolve().parent
            shutil.copyfile(source / "config.py", backend / "config.py")
            shutil.copyfile(source / "source_drain.py", backend / "source_drain.py")
            (backend / ".env").write_text(
                "DOTENV_CANARY=synthetic-dotenv-value\n", encoding="utf-8")

            env = dict(os.environ)
            env.pop("DOTENV_CANARY", None)
            env.pop("PYTHON_DOTENV_DISABLED", None)
            if disabled is None:
                env.pop("DOTENV_DISABLED", None)
            else:
                env["DOTENV_DISABLED"] = disabled
            env["PYTHONDONTWRITEBYTECODE"] = "1"
            env["PYTHONPATH"] = os.pathsep.join(
                part for part in (str(backend), env.get("PYTHONPATH", "")) if part)

            result = subprocess.run(
                [sys.executable, "-c",
                 "import os; import config; print(os.getenv('DOTENV_CANARY', ''))"],
                cwd=backend, env=env, capture_output=True, text=True,
                check=True, timeout=15)
            return result.stdout.strip()
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_disabled_flag_prevents_dotenv_canary_loading(self):
        for value in ("1", "true", "yes", "on"):
            with self.subTest(value=value):
                self.assertEqual(self.run_config(disabled=value), "")

    def test_dotenv_loads_by_default_and_when_flag_is_false(self):
        for value in (None, "false", "0"):
            with self.subTest(value=value):
                self.assertEqual(
                    self.run_config(disabled=value), "synthetic-dotenv-value")


if __name__ == "__main__":
    unittest.main()

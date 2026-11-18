"""Логика окна разбора в странице: сценарии в node с заглушкой DOM и сервера (пропускается без node).
Раскладку и внешний вид это не проверяет – их надо смотреть в браузере.
"""
from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
PAGE = HERE.parent / "localcoach" / "web" / "index.html"


@unittest.skipUnless(shutil.which("node"), "node не установлен")
class TriageUiTest(unittest.TestCase):
    def test_flows(self) -> None:
        res = subprocess.run(["node", str(HERE / "triage_ui.js"), str(PAGE)], capture_output=True, text=True, timeout=60)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("ok", res.stdout)


if __name__ == "__main__":
    unittest.main()

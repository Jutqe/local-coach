"""Веб-страница на английском: все русские строки подменены, скрипт остаётся синтаксически верным.
Русский режим страницу не меняет."""
from __future__ import annotations

import os as _os

_os.environ.setdefault("LOCAL_COACH_LANG", "ru")      # публичная копия по умолчанию английская, тесты написаны на русском

import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

CODE = Path(__file__).resolve().parents[1]
PAGE = CODE / "localcoach" / "web" / "index.html"
SNIPPET = r'''
import re, sys
sys.path.insert(0, ".")
from localcoach import webi18n
page = open("localcoach/web/index.html", encoding="utf-8").read()
sys.stdout.write(webi18n.localize(page))
'''


def localized(lang: str) -> str:
    env = {**_os.environ, "LOCAL_COACH_LANG": lang, "PYTHONIOENCODING": "utf-8"}
    res = subprocess.run([sys.executable, "-c", SNIPPET], capture_output=True, text=True, env=env, cwd=str(CODE), timeout=60)
    assert res.returncode == 0, res.stderr[-1500:]
    return res.stdout


class EnglishWebTest(unittest.TestCase):
    def test_russian_page_is_unchanged(self) -> None:
        self.assertEqual(localized("ru"), PAGE.read_text(encoding="utf-8"))

    def test_english_page_has_no_russian_outside_comments(self) -> None:
        out = localized("en")
        self.assertIn('<html lang="en">', out)
        code = re.sub(r"//[^\n]*|/\*.*?\*/|<!--.*?-->", "", out, flags=re.S)
        self.assertEqual(sorted(set(re.findall(r"[Ѐ-ӿ]+", code))), [])

    @unittest.skipUnless(shutil.which("node"), "node не установлен")
    def test_english_scripts_are_valid_javascript(self) -> None:
        out = localized("en")
        for k, script in enumerate(re.findall(r"<script\b[^>]*>(.*?)</script>", out, flags=re.S)):
            with tempfile.TemporaryDirectory() as d:
                path = Path(d) / f"s{k}.js"
                path.write_text(script, encoding="utf-8")
                res = subprocess.run(["node", "--check", str(path)], capture_output=True, text=True)
                self.assertEqual(res.returncode, 0, res.stderr[:600])

    def test_catalog_covers_every_page_string(self) -> None:
        import json
        sys.path.insert(0, str(CODE))
        from localcoach import webi18n
        cat = json.loads((CODE / "localcoach" / "locales" / "en_web.json").read_text(encoding="utf-8"))
        found = webi18n.extract(PAGE.read_text(encoding="utf-8"))
        for kind, strings in found.items():
            self.assertEqual([s for s in strings if s not in cat.get(kind, {})], [], kind)


if __name__ == "__main__":
    unittest.main()

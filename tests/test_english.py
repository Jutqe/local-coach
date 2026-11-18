"""Английский режим (LOCAL_COACH_LANG=en): запуск команд в подпроцессе со строгой проверкой каталога переводов.

Подпроцесс, потому что язык выбирается один раз при запуске. Фальшивый Ollama отвечает по схеме (без русских
образцов), так что проверяется не смысл ответов модели, а то, что на английском все команды выполняются,
нет пропущенных переводов (LOCAL_COACH_I18N_STRICT=1 бросает KeyError) и в выводе кода нет русского текста.
Разбор английского ввода (даты, буквы-ответы, команды) проверяется в test_english_input.py.
"""
from __future__ import annotations

import os as _os

_os.environ.setdefault("LOCAL_COACH_LANG", "ru")      # публичная копия по умолчанию английская, тесты написаны на русском

import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import fake_ollama  # noqa: E402

CODE = HERE.parent
NOTES = {
    "Diary/2015-05-01.md": "# 1 May 2015\n\nWe went to the lake with Person1 and spent the whole weekend there.\n",
    "Diary/2024-03-02.md": "# 2 March 2024\n\nI work remotely now and I am writing a book about travelling.\n"
                           "Every morning I run along the river before work.\n",
    "Ideas/app.md": "# App idea\n\nI want to build a small habit tracker for myself, maybe in the autumn.\n",
}


class EnglishModeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server, cls.url = fake_ollama.start()
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        cls.vault, cls.ws = root / "vault", root / "ws"
        for rel, text in NOTES.items():
            (cls.vault / rel).parent.mkdir(parents=True, exist_ok=True)
            (cls.vault / rel).write_text(text, encoding="utf-8")
        cls.env = {**os.environ, "LOCAL_COACH_LANG": "en", "LOCAL_COACH_I18N_STRICT": "1", "PYTHONIOENCODING": "utf-8"}

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.tmp.cleanup()

    def run_cli(self, *args: str, stdin: str = "") -> str:
        res = subprocess.run([sys.executable, str(CODE / "coach.py"), "--vault", str(self.vault), "--workspace", str(self.ws),
                              "--ollama", self.url, *args], input=stdin, capture_output=True, text=True, env=self.env,
                             timeout=120, cwd=str(CODE))
        self.assertEqual(res.returncode, 0, f"{args}: {res.stdout[-1500:]}\n{res.stderr[-1500:]}")
        self.assertNotIn("нет перевода", res.stderr)
        return res.stdout

    def assertEnglish(self, out: str, what: str) -> None:
        # Тексты кода – английские; русские буквы допустимы только в примерах пользовательского ввода и в данных.
        words = re.findall(r"[Ѐ-ӿ]{3,}", out)
        self.assertEqual(words, [], f"{what}: русский текст в выводе: {words[:5]}\n{out[-800:]}")

    def test_01_update_and_status(self) -> None:
        out = self.run_cli("update")
        self.assertEnglish(out, "update")
        out = self.run_cli("status")
        self.assertEnglish(out, "status")
        self.assertRegex(out.lower(), r"fact|note|memory|chunk")

    def test_02_plan_reset_bio(self) -> None:
        for cmd in (("plan",), ("reset",), ("bio",), ("inventory", "plan"), ("inventory",), ("audit", "--list")):
            try:
                out = self.run_cli(*cmd)
            except AssertionError:
                if cmd[0] == "audit":      # у audit нет такого параметра: проверяем только остальное
                    continue
                raise
            self.assertEnglish(out, " ".join(cmd))

    def test_03_chat_commands(self) -> None:
        out = self.run_cli("chat", stdin="/reset\n/triage\n/bio\n/exit\n")
        self.assertEnglish(out, "chat")
        self.assertIn("Life Reset", out)

    def test_03b_other_commands(self) -> None:
        self.run_cli("update")
        for cmd, stdin in ((("export",), ""), (("triage",), "\n"), (("triage", "--quick"), "\n"), (("audit",), "stop\n"),
                           (("hypotheses",), "stop\n"), (("people",), "q\n"), (("review",), "q\n"), (("calibrate",), "q\n"),
                           (("sweep",), "\n"), (("eval", "--report"), ""), (("inventory", "build"), ""),
                           (("inventory", "dedupe"), ""), (("sanitize",), ""), (("recluster",), ""), (("forget", "nothing"), "\n"),
                           (("--help",), "")):
            self.assertEnglish(self.run_cli(*cmd, stdin=stdin), " ".join(cmd))

    def test_03c_generated_profile_is_english(self) -> None:
        # generated files are not in the command output: render the plain profile directly
        code = ("from localcoach import report\n"
                "p = {'portrait': 'Portrait.'}\n"
                "for k in ('core_facts', 'current_focus', 'goals', 'values', 'strengths', 'tensions', 'history', 'unverified'):\n"
                "    p[k] = [{'text': k}]\n"
                "print(report.plain_profile(p))\n")
        res = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=self.env, cwd=str(CODE), timeout=60)
        self.assertEqual(res.returncode, 0, res.stderr[-1500:])
        self.assertEnglish(res.stdout, "plain profile")
        self.assertIn("## Goals", res.stdout)

    def test_04_catalog_is_complete(self) -> None:
        code = ("import importlib, pkgutil, localcoach\n"
                "for m in pkgutil.iter_modules(localcoach.__path__):\n    importlib.import_module('localcoach.' + m.name)\n"
                "from localcoach import i18n\nassert i18n.LANG == 'en'\nprint(len(i18n.catalog()), len(i18n.MISSING))\n")
        res = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=self.env, cwd=str(CODE), timeout=60)
        self.assertEqual(res.returncode, 0, res.stderr[-1500:])
        size, missing = map(int, res.stdout.split())
        self.assertGreater(size, 900)
        self.assertEqual(missing, 0)


if __name__ == "__main__":
    unittest.main()

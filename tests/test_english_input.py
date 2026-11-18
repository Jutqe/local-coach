"""Английский ввод рядом с русским: даты, ответы-буквы, команды, слова. Работает в обычном (ru) режиме процесса:
английские варианты добавлены к русским, а не вместо них (язык интерфейса на ввод не влияет)."""
from __future__ import annotations

import os as _os

_os.environ.setdefault("LOCAL_COACH_LANG", "ru")      # публичная копия по умолчанию английская, тесты написаны на русском

import re
import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from localcoach import audit, bodydouble, chat, history, i18n, inventory, people, util  # noqa: E402


class EnglishInputTest(unittest.TestCase):
    def test_dates(self) -> None:
        for text, want in (("1 May 2015", "2015-05-01"), ("May 1st, 2015", "2015-05-01"), ("3rd of March 2020", "2020-03-03"),
                           ("Sept. 9, 2019", "2019-09-09"), ("1 мая 2015", "2015-05-01"), ("2024-03-05", "2024-03-05"),
                           ("31 Feb 2020", None)):
            self.assertEqual(util.find_date(text), want, text)

    def test_review_intervals(self) -> None:
        base = date.fromisoformat(util.today())
        self.assertEqual(inventory.parse_review("in 10 days"), (base.fromordinal(base.toordinal() + 10)).isoformat())
        self.assertEqual(inventory.parse_review("in a week"), inventory.parse_review("через неделю"))
        self.assertEqual(inventory.parse_review("in half a year"), inventory.parse_review("через полгода"))
        self.assertEqual(inventory.parse_review("in a couple of months"), inventory.parse_review("через пару месяцев"))
        self.assertEqual(inventory.parse_review("in 2 years"), inventory.parse_review("через 2 года"))
        self.assertIsNone(inventory.parse_review("in 10"))

    def test_inventory_letters_and_quick_mode(self) -> None:
        self.assertEqual(inventory.SHORTCUTS["a"], inventory.SHORTCUTS["а"])
        self.assertEqual({inventory.SHORTCUTS[k] for k in "aiswhdlxun"}, set(inventory.SHORTCUTS.values()))
        self.assertEqual(inventory.parse_quick("1a 2s 3l 4?", 5), [(1, "a"), (2, "s"), (3, "l"), (4, "?")])
        self.assertEqual(inventory.parse_quick("1а 2к", 5), [(1, "а"), (2, "к")], "русские буквы работают, как раньше")
        self.assertIsNone(inventory.parse_quick("1a nonsense", 5))
        self.assertTrue(inventory.STOP.match("quit") and inventory.STOP.match("стоп"))

    def test_resolve_area_in_both_languages(self) -> None:
        self.assertEqual(inventory.resolve_area("finance"), "finance")
        self.assertEqual(inventory.resolve_area("фин"), "finance")
        self.assertEqual(inventory.resolve_area("Fin"), "finance", "английское начало слова – при русском интерфейсе")

    def test_answer_key_normalization(self) -> None:
        self.assertEqual(i18n.ru_key("c"), "с")
        self.assertEqual(i18n.ru_key("p 2019"), "б 2019")
        self.assertEqual(i18n.ru_key("w younger sister"), "к younger sister")
        self.assertEqual(i18n.ru_key("so long ago"), "so long ago", "слово, а не буква")
        self.assertEqual(i18n.ru_key("с"), "с")

    def test_audit_words(self) -> None:
        for word, want in (("yes", "yes"), ("no", "no"), ("maybe", "maybe"), ("да", "yes"), ("нет", "no")):
            self.assertEqual(audit.CHANGE_WORDS[word], want)

    def test_author_prefix(self) -> None:
        self.assertEqual(inventory._claim_text("The author moved to Berlin."), "moved to Berlin")
        self.assertEqual(inventory._claim_text("Автор переехал в Берлин."), "переехал в Берлин")
        self.assertEqual(history._plain("The author moved to Berlin"), "Moved to Berlin")
        self.assertEqual(history._plain("Автор переехал"), "Переехал")

    def test_memory_command_and_coach_topics(self) -> None:
        self.assertEqual(chat._REMEMBER.match("Remember that my sister lives in Berlin").group(1), "my sister lives in Berlin")
        self.assertTrue(chat._REMEMBER.match("запомни, что я бросил курсы"))
        self.assertIsNone(chat._REMEMBER.match("I remember nothing"))
        self.assertTrue(chat._ABOUT_COACH.search("your calibration questions are odd"))
        self.assertFalse(chat._ABOUT_COACH.search("I like cooking"))

    def test_body_double_phrases(self) -> None:
        self.assertTrue(bodydouble.START.search("please stay with me while I work"))
        self.assertTrue(bodydouble.STOP.match("done!") and bodydouble.STOP.match("всё"))
        self.assertIsNone(bodydouble.STOP.match("done with the report, now the email"))
        self.assertEqual(bodydouble.PLAN_MIN.search("work for 50 minutes").group(1), "50")

        def planned(text: str) -> int | None:
            m = bodydouble.PLAN_HOUR.search(text)
            if not m:
                return None
            g = m.groups()
            return int(g[0]) * 60 if g[0] else 90 if g[1] else 60 if g[2] else 30 if g[3] else 90
        for text, want in (("for an hour and a half", 90), ("for an hour", 60), ("for half an hour", 30), ("for 2 hours", 120),
                           ("на полтора часа", 90), ("на час", 60), ("на полчаса", 30), ("на 2 часа", 120)):
            self.assertEqual(planned(text), want, text)

    def test_people_roles_and_names(self) -> None:
        for text in ("my sister lives in Berlin", "talked to my boss", "мама звонила"):
            self.assertTrue(people._ROLE.search(text), text)
        for text in ("a sonata for piano", "I bought bread"):
            self.assertFalse(people._ROLE.search(text), text)

    def test_history_vocabulary(self) -> None:
        self.assertTrue(history._TILL_NOW.search("from 2015 to present"))
        self.assertEqual(history._YEARISH.match("2015 to present").groups(), ("2015", "present"))
        self.assertTrue(history.ASK.search("show me everything by years"))
        self.assertFalse(history.ASK.search("show my plans for the week"))

    def test_language_choice_at_startup(self) -> None:
        # env wins over the file in the working folder, the file wins over the default; checked in subprocesses
        import os
        import subprocess
        import tempfile
        code = "import sys; sys.path.insert(0, '.'); from localcoach import i18n; print(i18n.LANG)"
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as ws:
            def lang(env_lang: str | None, file_lang: str | None) -> str:
                env = {k: v for k, v in os.environ.items() if k != "LOCAL_COACH_LANG"}
                if env_lang:
                    env["LOCAL_COACH_LANG"] = env_lang
                (Path(ws) / "lang").unlink(missing_ok=True)
                if file_lang in i18n.LANGS:
                    i18n.save_lang(Path(ws), file_lang)
                elif file_lang:
                    (Path(ws) / "lang").write_text(file_lang, encoding="utf-8")
                res = subprocess.run([sys.executable, "-c", code, "--workspace", ws], capture_output=True, text=True,
                                     cwd=str(root), env=env, timeout=30)
                return res.stdout.strip()
            self.assertEqual(lang(None, "en"), "en", "saved by the web button")
            self.assertEqual(lang("ru", "en"), "ru", "the environment variable wins")
            self.assertEqual(lang(None, None), i18n.DEFAULT_LANG)
            self.assertEqual(lang(None, "xx"), i18n.DEFAULT_LANG, "unknown value falls back")

    def test_english_plurals(self) -> None:
        # английская схема склонений включается только в английском режиме; в русском работает прежняя
        from localcoach import timeline
        self.assertEqual(timeline._plural(21, "год", "года", "лет"), "год")


if __name__ == "__main__":
    unittest.main()

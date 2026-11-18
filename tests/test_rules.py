"""The person's own rules for the coach (/rule, /rules): file, limits, prompt block."""
from __future__ import annotations

import os as _os

_os.environ.setdefault("LOCAL_COACH_LANG", "ru")      # the public copy defaults to English, these tests are in Russian

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from localcoach import rules  # noqa: E402


class RulesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_add_list_delete(self) -> None:
        self.assertIn("пока нет", rules.handle(self.ws, ""))
        self.assertEqual(rules.block(self.ws), "")
        self.assertIn("Добавил правило 1", rules.handle(self.ws, "отвечай короче"))
        self.assertIn("Добавил правило 2", rules.handle(self.ws, "не спрашивай про работу по вечерам"))
        self.assertEqual(rules.load(self.ws), ["отвечай короче", "не спрашивай про работу по вечерам"])
        listing = rules.handle(self.ws, "")
        self.assertIn("1. отвечай короче", listing)
        self.assertIn("2. не спрашивай про работу по вечерам", listing)
        self.assertEqual(rules.handle(self.ws, "list"), listing)
        self.assertEqual((self.ws / "rules.md").read_text(encoding="utf-8"), "- отвечай короче\n- не спрашивай про работу по вечерам\n")
        self.assertIn("Убрал правило: отвечай короче", rules.handle(self.ws, "удалить 1"))
        self.assertIn("Убрал правило", rules.handle(self.ws, "delete 1"), "English words work too")
        self.assertEqual(rules.load(self.ws), [])

    def test_block_goes_to_prompt_with_priority_note(self) -> None:
        rules.handle(self.ws, "answer shorter")
        text = rules.block(self.ws)
        self.assertTrue(text.startswith("RULES FROM THE PERSON"))
        self.assertIn("never override the rules above about safety", text)
        self.assertTrue(text.endswith("- answer shorter"))

    def test_edited_by_hand(self) -> None:
        (self.ws / "rules.md").write_text("# my rules\n- be warm\nnot a rule\n- \n- be brief\n", encoding="utf-8")
        self.assertEqual(rules.load(self.ws), ["be warm", "be brief"])

    def test_limits_and_refusals(self) -> None:
        self.assertIn("длиннее", rules.handle(self.ws, "x" * (rules.MAX_LEN + 1)))
        self.assertIn("пароль", rules.handle(self.ws, "мой пароль: hunter2hunter2"))
        self.assertEqual(rules.load(self.ws), [])
        rules.handle(self.ws, "be brief")
        self.assertIn("уже есть", rules.handle(self.ws, "Be Brief"))
        for i in range(rules.MAX_RULES - 1):
            rules.handle(self.ws, f"rule number {i}")
        self.assertEqual(len(rules.load(self.ws)), rules.MAX_RULES)
        self.assertIn("убери лишнее", rules.handle(self.ws, "one more"))
        self.assertIn("Нет правила", rules.handle(self.ws, "удалить 99"))
        self.assertIn("Нет правила", rules.handle(self.ws, "удалить x"))


if __name__ == "__main__":
    unittest.main()

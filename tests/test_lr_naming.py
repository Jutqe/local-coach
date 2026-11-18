"""Registry build: the model names every group, single mentions included (LOCAL_COACH_NAME_SINGLES, default on)."""
from __future__ import annotations

import os as _os

_os.environ.setdefault("LOCAL_COACH_LANG", "ru")      # the public copy defaults to English, these tests are in Russian

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

import fake_ollama  # noqa: E402
from localcoach import cli, inventory, store  # noqa: E402
from localcoach.config import Config  # noqa: E402


def group(key: str, claim: str, mentions: int, notes: int) -> dict:
    return {"key": key, "anchors": [], "fact_ids": list(range(mentions)), "claims": [{"claim": claim, "kind": "goal", "event_date": None,
            "note_date": None, "path": "a.md", "note_title": "a", "evidence_quote": "", "quote_match": "none"}],
            "paths": [f"n{i}.md" for i in range(notes)], "titles": [], "first_date": None, "last_date": None, "last_seen": None,
            "touched": None, "mentions": mentions, "notes_count": notes, "kinds": ["goal"], "intents": [], "domain": "personal_growth",
            "sensitive": False, "currency": set(), "fingerprint": "fp-" + key}


class NamingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server, cls.url = fake_ollama.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()

    def build(self, name_singles: bool) -> tuple[list[str], list]:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config()
            cfg.workspace, cfg.ollama, cfg.name_singles = Path(tmp), self.url, name_singles
            conn = store.connect(Path(tmp) / "coach.sqlite3")
            seen: list[str] = []

            def fake_name(llm, cfg_, batch):
                seen.extend(g["key"] for g in batch)
                return {g["key"]: {"title": "Learn " + g["key"], "type": "wish", "summary": "s " + g["key"], "want": "", "same_as": "",
                                   "not_item": False} for g in batch}
            gs = [group("cluster:1", "Автор хочет выучить испанский", 3, 2), group("cluster:2", "Автор хочет собрать трекер привычек", 1, 1)]
            with mock.patch.object(inventory, "groups", return_value=gs), mock.patch.object(inventory, "_name_batch", fake_name):
                inventory.build(conn, cli.connect_llm(cfg, fallback=False), cfg, quiet=True)
            rows = conn.execute("SELECT title, bulk FROM lr_items ORDER BY id").fetchall()
            return seen, [(r["title"], r["bulk"]) for r in rows]

    def test_default_model_names_single_mentions_too(self) -> None:
        seen, rows = self.build(True)
        self.assertEqual(sorted(seen), ["cluster:1", "cluster:2"], "both groups go to the model")
        self.assertEqual(sorted(rows), [("Learn cluster:1", 0), ("Learn cluster:2", 1)], "the single mention is named by the model and stays in bulk review")

    def test_switch_restores_code_naming_for_single_mentions(self) -> None:
        seen, rows = self.build(False)
        self.assertEqual(seen, ["cluster:1"], "only the multi-mention group is sent to the model")
        titles = dict(rows)
        self.assertEqual(titles["Learn cluster:1"], 0)
        self.assertEqual(titles["хочет собрать трекер привычек"], 1, "named by code from the claim, bulk review")

    def test_plan_counts_all_groups_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = store.connect(Path(tmp) / "coach.sqlite3")
            gs = [group("cluster:1", "Автор хочет выучить испанский", 3, 2), group("cluster:2", "Автор хочет собрать трекер", 1, 1)]
            with mock.patch.object(inventory, "groups", return_value=gs):
                self.assertEqual(inventory.plan(conn)["todo"], 2)
                self.assertEqual(inventory.plan(conn, name_singles=False)["todo"], 1)


if __name__ == "__main__":
    unittest.main()

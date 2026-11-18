"""Режим присутствия: пункт реестра, план по времени, «как прошло», время по направлениям.
Запуск из папки code/:  python3 -m unittest tests.test_bodydouble -v
"""
from __future__ import annotations

import os as _os

_os.environ.setdefault("LOCAL_COACH_LANG", "ru")      # публичная копия по умолчанию английская, тесты написаны на русском

import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from localcoach import bodydouble, lifereset, store  # noqa: E402
from localcoach.config import Config  # noqa: E402
from localcoach.util import iso_now  # noqa: E402


def add_item(conn, title: str, status: str = "active", sensitive: int = 0) -> int:
    now = iso_now()
    cur = conn.execute("INSERT INTO lr_items(title, type, status, sensitive, created_at, updated_at, decided_at) "
                       "VALUES(?, 'project', ?, ?, ?, ?, ?)", (title, status, sensitive, now, now, now))
    conn.commit()
    return cur.lastrowid


class BodyDoubleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = store.connect(Path(self.tmp.name) / "coach.sqlite")
        self.cfg = Config(vault=Path(self.tmp.name), workspace=Path(self.tmp.name))

    def tearDown(self) -> None:
        self.conn.close()
        self.tmp.cleanup()

    def bd(self, task: str = "", **kw) -> bodydouble.BodyDouble:
        return bodydouble.BodyDouble(None, self.cfg, task, lambda _l: None, lambda: True, every=(0.05, 0.1), **kw)

    def test_parse_item_and_plan(self) -> None:
        iid = add_item(self.conn, "Курс испанского")
        self.assertEqual(bodydouble.parse_start(self.conn, f"LR-{iid} на 50 минут черновик"),
                         ("черновик", iid, "Курс испанского", 50))
        self.assertEqual(bodydouble.parse_start(self.conn, f"lr-{iid}"), ("", iid, "Курс испанского", None))
        self.assertEqual(bodydouble.parse_start(self.conn, "отчёт на час")[3], 60)
        self.assertEqual(bodydouble.parse_start(self.conn, "на полчаса почта"), ("почта", None, "", 30))
        self.assertEqual(bodydouble.parse_start(self.conn, "через час позвонить"), ("через час позвонить", None, "", None))
        self.assertEqual(bodydouble.parse_start(self.conn, "LR-999 код"), ("LR-999 код", None, "", None),
                         "несуществующий номер остаётся в тексте")
        self.assertIsNone(bodydouble.parse_start(self.conn, "на 500 минут")[3], "больше 4 часов – не план")

    def test_item_title_becomes_task(self) -> None:
        bd = self.bd("", item_id=1, item_title="Балкон")
        self.assertEqual((bd.task, bd.typed), ("Балкон", "Балкон"))

    def test_plan_end_calls_back_once(self) -> None:
        done = threading.Event()
        calls = []
        bd = self.bd("отчёт", planned=1, on_plan=lambda: (calls.append(1), done.set()))
        bd.started -= 60 - 0.3          # до конца плана – доля секунды
        bd.start()
        self.assertTrue(done.wait(3), "время вышло – веб узнаёт сам")
        time.sleep(0.3)
        bd.stop()
        self.assertEqual(calls, [1])
        self.assertTrue(bd.plan_reached)
        self.assertIsNone(bd.plan_left())
        self.assertIn("как и наметил", bodydouble.end_text(bd, 1))

    def test_plan_end_without_callback_just_says(self) -> None:
        said = []
        bd = bodydouble.BodyDouble(None, self.cfg, "код", said.append, lambda: True, every=(5, 6), planned=1)
        bd.started -= 60 - 0.2
        bd.start()
        deadline = time.monotonic() + 3
        while not said and time.monotonic() < deadline:
            time.sleep(0.05)
        bd.stop()
        self.assertTrue(said and "столько ты наметил" in said[0], said)

    def test_record_outcome_and_minutes(self) -> None:
        iid = add_item(self.conn, "Курс испанского")
        add_item(self.conn, "Балкон")
        secret = add_item(self.conn, "Личное", sensitive=1)
        for item, mins in ((iid, 25), (iid, 50), (secret, 10)):
            bd = self.bd(item_id=item, planned=50)
            bd.started -= mins * 60 + 5
            self.assertEqual(bodydouble.record(self.conn, None, bd), mins)
        bodydouble.save_outcome(self.conn, bd.row_id, "  дописал введение  ")
        row = self.conn.execute("SELECT item_id, planned, outcome FROM focus_sessions WHERE id=?", (bd.row_id,)).fetchone()
        self.assertEqual(tuple(row), (secret, 50, "дописал введение"))
        self.assertEqual(bodydouble.item_minutes(self.conn), {iid: 75, secret: 10})
        line = lifereset._focus_line(self.conn)
        self.assertIn("Курс испанского – 1 ч 15 мин", line)
        self.assertIn("Балкон – 0 минут", line, "ноль по активному направлению тоже видно")
        self.assertIn(f"LR-{secret} – 10 минут", line)
        self.assertNotIn("Личное", line, "чувствительный пункт – только номером")
        self.assertTrue(any("в фокусе за 30 дней" in x for x in lifereset.overview_lines(self.conn)))

    def test_no_focus_line_until_linked(self) -> None:
        add_item(self.conn, "Балкон")
        self.assertEqual(lifereset._focus_line(self.conn), "")

    def test_end_text_asks_only_after_work(self) -> None:
        bd = self.bd("код")
        self.assertNotIn("Как прошло", bodydouble.end_text(bd, 0))
        self.assertIn("Как прошло", bodydouble.end_text(bd, 3))
        self.assertTrue(bodydouble.outcome_reply("нормально, но отвлекался"))
        self.assertFalse(bodydouble.outcome_reply("/think"))
        self.assertFalse(bodydouble.outcome_reply("побудь рядом"))


if __name__ == "__main__":
    unittest.main()

"""Разбор реестра в вебе: очередь, решения, ёмкость, отмена, склейка, свободный ответ.
Запуск из папки code/:  python3 -m unittest tests.test_triageweb -v
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from localcoach import inventory, store, triageweb  # noqa: E402
from localcoach.util import iso_now, today  # noqa: E402


def add(conn, title: str, domain: str = "work_career", type_: str = "project", status: str = "inbox", **kw) -> int:
    now = iso_now()
    cols = {"title": title, "type": type_, "status": status, "domain": domain, "created_at": now, "updated_at": now,
            "notes_count": 1, "mentions": 1, **kw}
    cur = conn.execute(f"INSERT INTO lr_items({', '.join(cols)}) VALUES({', '.join('?' * len(cols))})", tuple(cols.values()))
    conn.commit()
    return cur.lastrowid


class StubLLM:
    def __init__(self, answer: dict | None = None):
        self.answer = answer or {}

    def chat_json(self, *_a, **_k):
        return self.answer


class TriageWebTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = store.connect(Path(self.tmp.name) / "coach.sqlite")

    def tearDown(self) -> None:
        self.conn.close()
        self.tmp.cleanup()

    def test_list_filters_and_pages(self) -> None:
        ids = [add(self.conn, f"Пункт {i}", domain="work_career" if i % 2 else "health_wellbeing", notes_count=10 - i) for i in range(6)]
        add(self.conn, "Уже решён", status="active")
        add(self.conn, "Пачкой", bulk=1)
        d = triageweb.list_page(self.conn, limit=4)
        self.assertEqual((d["total"], len(d["items"]), d["more"]), (6, 4, True))
        self.assertEqual(d["items"][0]["id"], ids[0], "сначала то, что занимало больше внимания")
        shown = [i["id"] for i in d["items"]]
        d2 = triageweb.list_page(self.conn, exclude=shown, limit=4)
        self.assertEqual((len(d2["items"]), d2["more"]), (2, False))
        self.assertFalse(set(shown) & {i["id"] for i in d2["items"]})
        health = triageweb.list_page(self.conn, area="health_wellbeing")
        self.assertEqual((health["area_total"], health["total"]), (3, 6))
        self.assertEqual(sum(a["n"] for a in health["areas"]), 6)
        self.assertEqual(triageweb.list_page(self.conn, area="нет-такой")["area"], "", "неизвестная область – все")
        self.assertEqual(d["bulk"], 1)

    def test_decide_parked_gets_review_date_and_undo(self) -> None:
        iid = add(self.conn, "Когда-нибудь это")
        res = triageweb.decide(self.conn, iid, "someday")
        self.assertTrue(res["ok"])
        self.assertEqual(res["review_on"], inventory.default_review("someday"))
        self.assertEqual(res["left"], 0)
        row = self.conn.execute("SELECT status, decided_at FROM lr_items WHERE id=?", (iid,)).fetchone()
        self.assertEqual(row["status"], "someday")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM lr_decisions").fetchone()[0], 1)
        back = triageweb.undo(self.conn, res["undo"])
        self.assertEqual(back["left"], 1)
        row = self.conn.execute("SELECT status, review_on, decided_at FROM lr_items WHERE id=?", (iid,)).fetchone()
        self.assertEqual((row["status"], row["review_on"], row["decided_at"]), ("inbox", None, None))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM lr_decisions").fetchone()[0], 0, "строка журнала удалена")
        self.assertEqual(triageweb.undo(self.conn, res["undo"])["error"], "late", "дважды отменить нельзя")

    def test_undo_refused_after_later_decision(self) -> None:
        iid = add(self.conn, "Дважды")
        first = triageweb.decide(self.conn, iid, "waiting")
        triageweb.decide(self.conn, iid, "archived")
        self.assertEqual(triageweb.undo(self.conn, first["undo"])["error"], "late")
        self.assertEqual(self.conn.execute("SELECT status FROM lr_items WHERE id=?", (iid,)).fetchone()[0], "archived")

    def test_capacity_flow(self) -> None:
        a, b, c = (add(self.conn, f"Проект {n}") for n in "ABC")
        self.assertEqual(triageweb.decide(self.conn, a, "active"), {"need": "capacity"})
        self.assertEqual(triageweb.set_capacity(self.conn, "0")["error"], "number")
        self.assertEqual(triageweb.set_capacity(self.conn, "x")["error"], "number")
        self.assertTrue(triageweb.set_capacity(self.conn, 1)["ok"])
        self.assertTrue(triageweb.decide(self.conn, a, "active", step=" позвонить ")["ok"])
        self.assertEqual(self.conn.execute("SELECT next_step FROM lr_items WHERE id=?", (a,)).fetchone()[0], "позвонить")
        over = triageweb.decide(self.conn, b, "active")
        self.assertEqual((over["need"], over["active"], over["capacity"], over["names"]), ("confirm", 1, 1, ["Проект A"]))
        self.assertEqual(self.conn.execute("SELECT status FROM lr_items WHERE id=?", (b,)).fetchone()[0], "inbox")
        self.assertTrue(triageweb.decide(self.conn, b, "active", force=True)["ok"])
        routine = add(self.conn, "Зарядка", type_="routine")
        self.assertTrue(triageweb.decide(self.conn, routine, "active")["ok"], "рутина в ёмкость не считается")
        # без числа можно двигаться, если человек выбрал «пока без лимита»
        self.conn.execute("DELETE FROM meta WHERE key='lr_capacity'")
        self.conn.commit()
        self.assertTrue(triageweb.decide(self.conn, c, "active", skip_capacity=True)["ok"])

    def test_secret_and_bad_input(self) -> None:
        iid = add(self.conn, "Пункт")
        self.assertEqual(triageweb.decide(self.conn, iid, "archived", reason="api_key = sk-1234567890abcdef1234567890abcdef")["error"], "secret")
        self.assertEqual(triageweb.decide(self.conn, iid, "что-то")["error"], "status")
        self.assertEqual(triageweb.decide(self.conn, 999, "archived")["error"], "gone")
        self.assertEqual(self.conn.execute("SELECT status FROM lr_items WHERE id=?", (iid,)).fetchone()[0], "inbox")

    def test_merge(self) -> None:
        a, b = add(self.conn, "Сайт про машины"), add(self.conn, "Сайт про старые машины")
        self.assertEqual(triageweb.merge(self.conn, a, "LR-" + str(a))["error"], "merge", "с самим собой нельзя")
        self.assertEqual(triageweb.merge(self.conn, a, "abc")["error"], "number")
        res = triageweb.merge(self.conn, a, f"LR-{b}")
        self.assertEqual((res["ok"], res["into"], res["left"]), (True, f"LR-{b}", 1))
        self.assertEqual(self.conn.execute("SELECT merged_into FROM lr_items WHERE id=?", (a,)).fetchone()[0], b)

    def test_say_proposes_without_writing(self) -> None:
        iid = add(self.conn, "Курс гончарного дела", type_="wish")
        llm = StubLLM({"status": "someday", "reason": "нет времени", "next_step": "", "review_on": "через год"})
        res = triageweb.say(self.conn, llm, iid, "хочу, но не в этом году")
        self.assertEqual((res["status"], res["reason"]), ("someday", "нет времени"))
        self.assertRegex(res["review_on"], r"^\d{4}-\d{2}-\d{2}$")
        self.assertEqual(self.conn.execute("SELECT status FROM lr_items WHERE id=?", (iid,)).fetchone()[0], "inbox",
                         "предложение модели ничего не записывает")
        done = triageweb.decide(self.conn, iid, "someday", reason=res["reason"], review=res["review_on"],
                                answer="хочу, но не в этом году", proposal="someday")
        self.assertEqual(done["review_on"], res["review_on"])
        d = self.conn.execute("SELECT answer, proposal, reason FROM lr_decisions").fetchone()
        self.assertEqual(tuple(d), ("хочу, но не в этом году", "someday", "нет времени"))

    def test_say_without_decision_records_clarification(self) -> None:
        iid = add(self.conn, "Что-то непонятное")
        res = triageweb.say(self.conn, StubLLM({"status": ""}), iid, "ну даже не знаю")
        self.assertEqual(res["recorded"], "clarification")
        self.assertEqual(self.conn.execute("SELECT status FROM lr_items WHERE id=?", (iid,)).fetchone()[0], "clarification")
        self.assertEqual(triageweb.say(self.conn, StubLLM(), iid, "   ")["error"], "empty")
        self.assertEqual(triageweb.say(self.conn, StubLLM(), iid, "password: hunter2hunter2hunter2xyz token=sk-1234567890abcdef1234567890abcdef")["error"], "secret")

    def test_card_has_text_and_questions(self) -> None:
        iid = add(self.conn, "Балкон", summary="Утеплить и застеклить")
        c = triageweb.card(self.conn, iid)
        self.assertEqual((c["title"], c["summary"], c["status"]), ("Балкон", "Утеплить и застеклить", "inbox"))
        self.assertEqual((c["sources"], c["related"], c["last_decision"]), ([], [], None))
        triageweb.decide(self.conn, iid, "archived", reason="не актуально")
        self.assertIsNone(triageweb.card(self.conn, iid).get("error"))
        self.assertEqual(triageweb.card(self.conn, iid)["last_decision"]["reason"], "не актуально")
        self.assertEqual(triageweb.card(self.conn, 999)["error"], "gone")

    def test_dismissed_hides_item_and_keeps_it_out_of_queue(self) -> None:
        iid = add(self.conn, "Список книг на потом")
        res = triageweb.decide(self.conn, iid, "dismissed", answer="Ещё интересно: нет")
        self.assertEqual((res["label"], res["left"]), ("не пункт", 0))
        self.assertEqual(triageweb.list_page(self.conn)["items"], [])
        self.assertEqual(triageweb.find(self.conn, "список книг"), [], "скрытый пункт для склейки не предлагается")
        self.assertEqual(triageweb.undo(self.conn, res["undo"])["left"], 1)

    def test_review_months_choice(self) -> None:
        iid = add(self.conn, "Дальний проект")
        res = triageweb.decide(self.conn, iid, "waiting", months=3)
        self.assertEqual(res["review_on"], inventory.add_months(__import__("datetime").date.fromisoformat(today()), 3).isoformat())
        other = add(self.conn, "Ближний проект")
        res = triageweb.decide(self.conn, other, "someday", months=7)
        self.assertEqual(res["review_on"], inventory.default_review("someday"), "неизвестный срок – по умолчанию")

    def test_find_by_title_and_number(self) -> None:
        a, b = add(self.conn, "Сайт про старые машины"), add(self.conn, "Балкон")
        self.assertEqual([i["id"] for i in triageweb.find(self.conn, "МАШИН", exclude=b)], [a])
        self.assertEqual([i["id"] for i in triageweb.find(self.conn, f"LR-{b}", exclude=a)], [b])
        self.assertEqual(triageweb.find(self.conn, str(a), exclude=a), [], "сам с собой не склеивают")
        self.assertEqual(triageweb.find(self.conn, "  "), [])

    def test_set_area_changes_filter_and_undoes(self) -> None:
        a = add(self.conn, "Отдых в Греции осенью", domain="learning_knowledge")
        b = add(self.conn, "Балкон", domain="learning_knowledge")
        res = triageweb.set_area(self.conn, a, "hobbies_interests")
        self.assertEqual((res["ok"], res["area"], res["area_title"]), (True, "hobbies_interests", "Хобби и интересы"))
        self.assertEqual(res["prev_title"], "Учёба и знания")
        self.assertEqual([i["id"] for i in triageweb.list_page(self.conn, "learning_knowledge")["items"]], [b])
        self.assertEqual([i["id"] for i in triageweb.list_page(self.conn, "hobbies_interests")["items"]], [a])
        self.assertEqual(triageweb.card(self.conn, a)["area"], "hobbies_interests")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM lr_decisions").fetchone()[0], 0, "журнал решений не трогается")
        self.assertEqual(self.conn.execute("SELECT status FROM lr_items WHERE id=?", (a,)).fetchone()[0], "inbox")
        back = triageweb.undo(self.conn, res["undo"])
        self.assertEqual((back["ok"], back["restored"]), (True, 1))
        self.assertEqual(self.conn.execute("SELECT domain FROM lr_items WHERE id=?", (a,)).fetchone()[0], "learning_knowledge")

    def test_set_area_rejects_bad_input_and_late_undo(self) -> None:
        a = add(self.conn, "Отдых в Греции осенью", domain="learning_knowledge")
        self.assertEqual(triageweb.set_area(self.conn, a, "nonsense")["error"], "area")
        self.assertEqual(triageweb.set_area(self.conn, a, "administrative_records")["error"], "area", "закрытая область недоступна")
        self.assertEqual(triageweb.set_area(self.conn, 9999, "other")["error"], "gone")
        same = triageweb.set_area(self.conn, a, "learning_knowledge")
        self.assertEqual((same["ok"], same["same"], "undo" in same), (True, True, False), "та же область: ничего не меняется")
        first = triageweb.set_area(self.conn, a, "hobbies_interests")
        triageweb.set_area(self.conn, a, "finance")
        self.assertEqual(triageweb.undo(self.conn, first["undo"])["error"], "late", "после новой смены старую отменять нельзя")
        self.assertEqual(self.conn.execute("SELECT domain FROM lr_items WHERE id=?", (a,)).fetchone()[0], "finance")

    def test_set_area_survives_registry_refresh(self) -> None:
        # Пересборка реестра не должна возвращать область: домен берётся только при создании пункта.
        import inspect
        src = inspect.getsource(inventory)
        self.assertEqual(src.count("UPDATE lr_items SET domain"), 0, "в сборке нет обновления домена существующего пункта")

    def test_dedupe_same_title_merges_and_undoes(self) -> None:
        # Три пункта с одним названием, как «Изучение японского языка» у человека: описания разные, векторы не сошлись.
        a = add(self.conn, "Изучение японского языка", notes_count=5, mentions=9, fact_ids_json="[1,2]", paths_json='["a.md"]')
        b = add(self.conn, "изучение японского языка!", notes_count=1, mentions=1, fact_ids_json="[3]", paths_json='["b.md"]')
        c = add(self.conn, "Изучение  японского  языка", notes_count=1, mentions=2, fact_ids_json="[4]", paths_json='["c.md"]')
        add(self.conn, "Идеи", notes_count=1)
        add(self.conn, "Идеи", notes_count=1)       # слишком общее название – не склеивается
        res = triageweb.dedupe(self.conn)
        self.assertEqual(res["merged"], 2)
        self.assertEqual(sorted(r[0] for r in self.conn.execute("SELECT id FROM lr_items WHERE merged_into IS NULL AND title LIKE '%японск%'")), [a],
                         "остаётся самый «весомый»")
        row = self.conn.execute("SELECT fact_ids_json, paths_json, mentions FROM lr_items WHERE id=?", (a,)).fetchone()
        self.assertEqual((row["fact_ids_json"], row["paths_json"]), ("[1, 2, 3, 4]", '["a.md", "b.md", "c.md"]'), "источники сохранены")
        self.assertEqual(triageweb.list_page(self.conn)["total"], 3, "японский один и две общие «Идеи»")
        self.assertEqual(triageweb.dedupe(self.conn)["merged"], 0, "повторно ничего не склеивается")
        back = triageweb.undo(self.conn, res["undo"])
        self.assertEqual((back["ok"], back["restored"]), (True, 2))
        row = self.conn.execute("SELECT fact_ids_json, mentions FROM lr_items WHERE id=?", (a,)).fetchone()
        self.assertEqual((row["fact_ids_json"], row["mentions"]), ("[1,2]", 9))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM lr_items WHERE merged_into IS NULL").fetchone()[0], 5)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM lr_decisions").fetchone()[0], 0)

    def test_dedupe_into_decided_item_keeps_decision(self) -> None:
        done = add(self.conn, "Курс гончарного дела", status="active", decided_at=iso_now())
        dup = add(self.conn, "Курс гончарного дела")
        other = add(self.conn, "Курс гончарного дела", status="someday", decided_at="2020-01-01T00:00:00+03:00")
        self.assertEqual(inventory.dedupe(self.conn), 1)
        self.assertEqual(self.conn.execute("SELECT merged_into FROM lr_items WHERE id=?", (dup,)).fetchone()[0], done)
        self.assertIsNone(self.conn.execute("SELECT merged_into FROM lr_items WHERE id=?", (other,)).fetchone()[0],
                          "два решённых между собой не склеиваются")

    def test_unmerge_only_in_reverse_order(self) -> None:
        x = add(self.conn, "Целевой пункт", fact_ids_json="[1]", paths_json='["x.md"]')
        a = add(self.conn, "Первый дубль", fact_ids_json="[2]", paths_json='["a.md"]')
        b = add(self.conn, "Второй дубль", fact_ids_json="[3]", paths_json='["b.md"]')
        first, second = {}, {}
        self.assertTrue(inventory.merge(self.conn, a, x, first))
        self.assertTrue(inventory.merge(self.conn, b, x, second))
        self.assertFalse(inventory.unmerge(self.conn, first), "после первой склейки в цель добавили второй пункт")
        self.assertTrue(inventory.unmerge(self.conn, second))
        self.assertTrue(inventory.unmerge(self.conn, first))
        row = self.conn.execute("SELECT fact_ids_json, merged_into FROM lr_items WHERE id=?", (x,)).fetchone()
        self.assertEqual((row["fact_ids_json"], row["merged_into"]), ("[1]", None))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM lr_items WHERE merged_into IS NULL").fetchone()[0], 3)

    def test_waiting_is_zero_for_empty_registry(self) -> None:
        self.assertEqual(triageweb.waiting(self.conn), 0)
        add(self.conn, "Один")
        self.assertEqual(triageweb.waiting(self.conn), 1)


if __name__ == "__main__":
    unittest.main()

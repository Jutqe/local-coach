"""Life Reset на заглушке Ollama: реестр, разбор, аудит, гипотезы, обзор, чат, выгрузка в Obsidian.

Запуск из папки code/:  python3 -m unittest tests.test_lifereset -v
"""
from __future__ import annotations

import contextlib
import io
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ["LOCAL_COACH_OLLAMA_PORTS"] = ""

import fake_ollama  # noqa: E402
from localcoach import audit, cli, hypotheses, inventory, lifereset, store, vault  # noqa: E402
from localcoach.config import Config  # noqa: E402
from localcoach.util import today  # noqa: E402

NOTES = {
    "Проекты/Сайт с идеями.md": "# Сайт с идеями\n\nХочу сделать сайт, где будут собраны все мои идеи.\n"
                                "Надо купить домен для сайта и выбрать движок.\n",
    "Идеи/Сайт про машины.md": "Хочу сделать сайт про старые машины и их ремонт.\n",
    "Проекты/Гараж в Казани.md": "Хочу найти гараж в Казани недорого.\n",
    "Проекты/Идеи на потом.md": "Хочу собрать мини-трактор из старого мотоблока.\n"
                                "Хочу открыть мастерскую по ремонту велосипедов во дворе.\n",
    "Дневник/2019-05-01.md": "# 1 мая 2019\n\nХочу научиться ходить под парусом летом.\n"
                             "Опять бросил проект на середине, как обычно.\n",
    "Дневник/2021-03-01.md": "# 1 марта 2021\n\nОпять забросил новый проект через месяц.\n"
                             "Хочу научиться ходить под парусом летом.\n",
    "Дневник/2014-03-10.md": "# 10 марта 2014\n\nРаботаю инженером и живу в Казани постоянно.\n",
    "Дневник/2022-01-01.md": "# 1 января 2022\n\nХочу переехать из Казани поближе к морю.\n",
    # Своя папка с таким же именем глубже в vault – обычные заметки, их коуч читает.
    "Проекты/Life Reset/План.md": "Хочу пройти курс по гончарному делу весной.\n",
}


def answers(*items: str):
    it = iter(items)

    def fake(prompt: str = "") -> str:
        try:
            return next(it)
        except StopIteration:
            raise EOFError from None
    return mock.patch("builtins.input", fake)


def quiet():
    return contextlib.redirect_stdout(io.StringIO())


class LifeResetTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server, cls.url = fake_ollama.start()
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        cls.vault, cls.ws = root / "vault", root / "ws"
        for rel, text in NOTES.items():
            (cls.vault / rel).parent.mkdir(parents=True, exist_ok=True)
            (cls.vault / rel).write_text(text, encoding="utf-8")
        cls.run_cli("update")
        cls.run_cli("inventory")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.tmp.cleanup()

    @classmethod
    def run_cli(cls, *args: str, inputs: tuple[str, ...] = ()) -> str:
        out = io.StringIO()
        with answers(*inputs), contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = cli.main(["--vault", str(cls.vault), "--workspace", str(cls.ws), "--ollama", cls.url, *args])
        assert code in (0, None), f"{args}: код {code}\n{out.getvalue()[-2000:]}"
        return out.getvalue()

    def db(self) -> sqlite3.Connection:
        return store.connect(self.ws / "coach.sqlite3")

    def cfg(self) -> Config:
        c = Config()
        c.workspace, c.vault, c.ollama = self.ws, self.vault, self.url
        return c

    def llm(self):
        return cli.connect_llm(self.cfg(), fallback=False)

    def item(self, conn, like: str) -> sqlite3.Row:
        r = conn.execute("SELECT * FROM lr_items WHERE merged_into IS NULL AND title LIKE ?", (f"%{like}%",)).fetchone()
        self.assertIsNotNone(r, like)
        return r

    # --- реестр ----------------------------------------------------------------------------

    def test_01_build_makes_items_without_decisions(self) -> None:
        conn = self.db()
        rows = conn.execute("SELECT * FROM lr_items WHERE merged_into IS NULL").fetchall()
        self.assertTrue(rows)
        self.assertTrue(all(r["status"] == "inbox" for r in rows), "статус ставит только человек")
        titles = " | ".join(r["title"] for r in rows)
        self.assertIn("парусом", titles)
        self.assertNotIn("гараж", titles.casefold(), "модель сказала «не пункт» – в реестр не идёт")
        self.assertNotIn("тёплую погоду", titles, "попутное сведение из заметки-списка – не пункт")
        # Одиночные упоминания – без модели, ждут разбора пачкой; не в очереди карточек.
        move = self.item(conn, "переехать")
        self.assertEqual((move["bulk"], move["status"]), (1, "inbox"))
        self.assertNotIn(move["id"], [r["id"] for r in inventory.queue(conn)])
        self.assertEqual(self.item(conn, "парусом")["bulk"], 0)
        sail = self.item(conn, "парусом")
        self.assertEqual(sail["notes_count"], 2, "одно желание из двух заметок – один пункт")
        # Две заметки про сайты модель назвала одной темой (same_as) – склеены в один пункт.
        sites = conn.execute("SELECT * FROM lr_items WHERE title LIKE '%сайт%'").fetchall()
        self.assertEqual(len(sites), 1)
        self.assertEqual(sites[0]["type"], "project")
        self.assertTrue(store.get_meta(conn, "lr_built_at"))
        # Заметка-список («Идеи на потом») – по пункту на тему, обычная проектная – один пункт.
        self.assertTrue(conn.execute("SELECT 1 FROM lr_items WHERE summary LIKE '%трактор%'").fetchone())
        self.assertTrue(conn.execute("SELECT 1 FROM lr_items WHERE summary LIKE '%велосипед%'").fetchone())
        conn.close()

    def test_02_rebuild_keeps_items(self) -> None:
        conn = self.db()
        before = store.scalar(conn, "SELECT COUNT(*) FROM lr_items")
        mentions = [tuple(r) for r in conn.execute("SELECT id, mentions, notes_count FROM lr_items ORDER BY id")]
        with quiet():
            st = inventory.build(conn, self.llm(), self.cfg())
        self.assertEqual(st["new"], 0)
        self.assertEqual(st["embedded"], 0, "векторы названий – из кэша, эмбеддинг-модель не вызывается")
        self.assertEqual([tuple(r) for r in conn.execute("SELECT id, mentions, notes_count FROM lr_items ORDER BY id")],
                         mentions, "пересборка не накручивает упоминания")
        self.assertEqual(st["named"], 0, "готовые группы берутся из кэша, модель не вызывается")
        self.assertEqual(store.scalar(conn, "SELECT COUNT(*) FROM lr_items"), before)
        self.assertTrue(list((self.ws / "backups").glob("coach-*.sqlite3")), "перед пересборкой – копия базы")
        n = len(list((self.ws / "backups").glob("coach-*.sqlite3")))
        with quiet():
            inventory.build(conn, self.llm(), self.cfg(), quiet=True)     # так зовёт update
        self.assertEqual(len(list((self.ws / "backups").glob("coach-*.sqlite3"))), n, "update копий не плодит")
        conn.close()

    def test_03_card_shows_sources_and_questions(self) -> None:
        conn = self.db()
        text = inventory.card(conn, self.item(conn, "сайт"))
        self.assertIn("Откуда:", text)
        self.assertIn("«Сайт с идеями»", text)
        conn.close()

    # --- разбор ----------------------------------------------------------------------------

    def test_04_triage_free_text_needs_confirmation_and_logs_reason(self) -> None:
        conn = self.db()
        with answers("не хочу этим заниматься", ""), quiet():
            n = inventory.triage(conn, self.llm(), self.cfg(), limit=1, type_="project")
        self.assertEqual(n, 1)
        site = self.item(conn, "сайт")
        self.assertEqual(site["status"], "abandoned")
        d = conn.execute("SELECT * FROM lr_decisions WHERE item_id=?", (site["id"],)).fetchone()
        self.assertEqual((d["from_status"], d["to_status"], d["proposal"], d["reason"]),
                         ("inbox", "abandoned", "abandoned", "перегорел"))
        self.assertIn("не хочу", d["answer"])
        conn.close()

    def test_05_someday_gets_review_date(self) -> None:
        conn = self.db()
        with answers("к", "", "к", ""), quiet():
            inventory.triage(conn, self.llm(), self.cfg(), limit=2, type_="wish")
        sail = self.item(conn, "парусом")
        self.assertEqual(sail["status"], "someday")
        self.assertEqual(sail["review_on"], inventory.default_review("someday"), "без срока «когда-нибудь» не бывает")
        conn.close()

    def test_06_unclear_answer_goes_to_clarification(self) -> None:
        conn = self.db()
        iid = inventory.capture(conn, self.llm(), "Научиться играть на варгане")
        with answers("ну не знаю даже"), quiet():
            inventory.triage(conn, self.llm(), self.cfg(), limit=1, type_="idea")
        self.assertEqual(conn.execute("SELECT status FROM lr_items WHERE id=?", (iid,)).fetchone()[0], "clarification")
        conn.close()

    def test_07_capacity_guard(self) -> None:
        conn = self.db()
        a = inventory.capture(conn, self.llm(), "Ремонт балкона", "project")
        b = inventory.capture(conn, self.llm(), "Курс испанского", "project")
        inventory.decide(conn, a, "active")
        store.set_meta(conn, "lr_capacity", "1")
        conn.commit()
        conn.execute("UPDATE lr_items SET status='dismissed' WHERE id NOT IN (?, ?) AND status='inbox' AND bulk=0", (a, b))
        conn.commit()
        with answers("а", ""), quiet():
            inventory.triage(conn, self.llm(), self.cfg(), limit=1, type_="project")
        self.assertEqual(conn.execute("SELECT status FROM lr_items WHERE id=?", (b,)).fetchone()[0], "inbox",
                         "сверх ёмкости – только после явного «да»")
        with answers("а", "д", "записаться на пробный урок"), quiet():
            inventory.triage(conn, self.llm(), self.cfg(), limit=1, type_="project")
        r = conn.execute("SELECT status, next_step FROM lr_items WHERE id=?", (b,)).fetchone()
        self.assertEqual(tuple(r), ("active", "записаться на пробный урок"))
        conn.close()

    def test_08_merge_by_code(self) -> None:
        conn = self.db()
        a = inventory.capture(conn, self.llm(), "Поездка в Норвегию", "wish")
        b = inventory.capture(conn, self.llm(), "Фьорды Норвегии посмотреть", "wish")
        self.assertTrue(inventory.merge(conn, b, a))
        self.assertEqual(conn.execute("SELECT merged_into FROM lr_items WHERE id=?", (b,)).fetchone()[0], a)
        self.assertNotIn(b, [r["id"] for r in inventory.queue(conn)])
        conn.close()

    def test_09_closed_item_resurfaces(self) -> None:
        conn = self.db()
        site = self.item(conn, "сайт")
        self.assertEqual(site["status"], "abandoned")
        note = self.vault / "Проекты/Сайт с идеями.md"
        # Только дата файла (синхронизация vault) – не повод: содержимое то же.
        os.utime(note, (note.stat().st_atime, note.stat().st_mtime + 86400))
        self.run_cli("update")
        self.assertEqual(self.item(conn, "сайт")["resurfaced"], 0)
        # Дописал в заметку после решения – всплыло; update сам дособирает реестр.
        note.write_text(note.read_text(encoding="utf-8") + "Надо всё-таки доделать сайт к лету.\n", encoding="utf-8")
        out = self.run_cli("update")
        self.assertIn("Life Reset: новых пунктов", out)
        self.assertEqual(self.item(conn, "сайт")["resurfaced"], 1)
        self.assertEqual(store.scalar(conn, "SELECT COUNT(*) FROM lr_items WHERE paths_json LIKE '%Сайт с идеями%' "
                                            "AND merged_into IS NULL"), 1, "одна проектная заметка – один пункт")
        self.assertEqual(inventory.queue(conn)[0]["id"], site["id"], "всплывшее – первым в очереди")
        self.assertIn("Снова появилось", inventory.card(conn, self.item(conn, "сайт")))
        conn.close()

    def test_09b_plan_counts_without_model(self) -> None:
        out = self.run_cli("inventory", "plan")
        self.assertIn("Групп-кандидатов в реестр", out)
        self.assertIn("вызовов модели: 0", out, "всё уже названо – работы нет")
        self.assertIn("без модели, разбор пачкой", out)
        self.assertIn("заметок", out)

    def test_09c_sweep_keeps_marked_archives_rest(self) -> None:
        conn = self.db()
        bulk = conn.execute("SELECT id, summary FROM lr_items WHERE bulk=1 AND status='inbox' "
                            "ORDER BY COALESCE(json_extract(paths_json, '$[0]'), ''), id").fetchall()
        self.assertGreaterEqual(len(bulk), 3)
        with answers("1"), quiet():
            st = inventory.sweep(conn)
        self.assertEqual((st["kept"], st["archived"]), (1, len(bulk) - 1))
        kept = conn.execute("SELECT * FROM lr_items WHERE id=?", (bulk[0]["id"],)).fetchone()
        self.assertEqual((kept["bulk"], kept["status"]), (0, "inbox"))
        self.assertIn(kept["id"], [r["id"] for r in inventory.queue(conn)], "живое – в обычный разбор")
        gone = conn.execute("SELECT status FROM lr_items WHERE id=?", (bulk[1]["id"],)).fetchone()[0]
        self.assertEqual(gone, "archived")
        self.assertEqual(inventory.bulk_count(conn), 0)
        # Вернуть из архива.
        self.assertEqual(inventory.reopen(conn, f"LR-{bulk[1]['id']}"), bulk[1]["id"])
        self.assertEqual(conn.execute("SELECT status FROM lr_items WHERE id=?", (bulk[1]["id"],)).fetchone()[0], "inbox")
        # Пересборка не возвращает разобранное пачкой в пачку.
        with quiet():
            inventory.build(conn, self.llm(), self.cfg(), quiet=True)
        self.assertEqual(inventory.bulk_count(conn), 0)
        conn.close()

    def test_09d_quotas_balanced_by_area(self) -> None:
        q = inventory._quotas({"work": 100, "health": 3, "money": 40, "home": 10}, 30)
        self.assertEqual(sum(q.values()), 30)
        self.assertEqual(q["health"], 3, "маленькая область – целиком")
        self.assertTrue(q["work"] >= q["home"] and q["money"] >= q["home"])
        self.assertEqual(inventory._quotas({"a": 2, "b": 1}, 10), {"a": 2, "b": 1})

    def test_09e_sweep_with_model_pick_and_tail(self) -> None:
        conn = self.db()
        ids = [inventory.capture(conn, self.llm(), f"Одиночное дело номер {n} про дачу", "question") for n in range(6)]
        conn.execute(f"UPDATE lr_items SET bulk=1, bulk_rank=NULL, domain='life_logistics' WHERE id IN "
                     f"({','.join(map(str, ids))})")
        conn.execute("DELETE FROM meta WHERE key='lr_pick_ratio'")
        conn.commit()
        out = io.StringIO()
        with answers("1", "архив"), contextlib.redirect_stdout(out):
            st = inventory.sweep(conn, self.llm(), self.cfg(), target=2)
        text = out.getvalue()
        self.assertIn("самых значимых по областям", text)
        self.assertIn("Быт и организация жизни", text, "заголовки – области жизни")
        ranks = dict(conn.execute(f"SELECT id, bulk_rank FROM lr_items WHERE id IN ({','.join(map(str, ids))})").fetchall())
        chosen = [i for i, r in ranks.items() if r]
        self.assertEqual(len(chosen), 2, "квота 2 из 6, лишний id модели отброшен")
        self.assertIn(ids[-1], chosen, "выбор модели, а не первые по списку")
        self.assertEqual((st["kept"], st["rest"]), (1, 4))
        statuses = dict(conn.execute(f"SELECT id, status FROM lr_items WHERE id IN ({','.join(map(str, ids))})").fetchall())
        self.assertEqual(sorted(statuses.values()).count("archived"), 5)
        d = conn.execute("SELECT answer FROM lr_decisions WHERE item_id=? ORDER BY id DESC", (ids[0],)).fetchone()
        self.assertEqual(d["answer"], inventory.REST_ANSWER, "хвост в архиве – решение человека, с пометкой")
        conn.close()

    def test_09f_tail_untouched_without_answer(self) -> None:
        conn = self.db()
        ids = [inventory.capture(conn, self.llm(), f"Мелочь номер {n} про гараж", "question") for n in range(4)]
        conn.execute(f"UPDATE lr_items SET bulk=1, bulk_rank=NULL, domain='finance' WHERE id IN ({','.join(map(str, ids))})")
        conn.commit()
        with answers("", ""), quiet():
            inventory.sweep(conn, self.llm(), self.cfg())
        left = conn.execute(f"SELECT COUNT(*) FROM lr_items WHERE id IN ({','.join(map(str, ids))}) AND status='inbox' "
                            "AND bulk=1").fetchone()[0]
        self.assertGreater(left, 0, "без ответа хвост остаётся ждать, модель ничего не архивирует сама")
        conn.close()

    def test_09g_quick_parse_and_area(self) -> None:
        self.assertEqual(inventory.parse_quick("1а 2к, 4?", 5), [(1, "а"), (2, "к"), (4, "?")])
        self.assertEqual(inventory.parse_quick("10х", 10), [(10, "х")])
        self.assertIsNone(inventory.parse_quick("1а и ещё", 5), "лишние слова – переспросить")
        self.assertIsNone(inventory.parse_quick("7а", 5), "номер вне страницы")
        self.assertEqual(inventory.resolve_area("финансы"), "finance")
        self.assertEqual(inventory.resolve_area("Здоров"), "health_wellbeing")
        self.assertEqual(inventory.resolve_area("finance"), "finance")
        self.assertIsNone(inventory.resolve_area("космос"))

    def test_09h_quick_triage_by_area(self) -> None:
        conn = self.db()
        ids = [inventory.capture(conn, self.llm(), t, "question") for t in
               ("Поменять страховку на машину", "Разобраться с налоговым вычетом", "Открыть вклад", "Продать старый ноут")]
        conn.execute(f"UPDATE lr_items SET domain='finance' WHERE id IN ({','.join(map(str, ids))})")
        conn.commit()
        out = io.StringIO()
        # «zz» – не понял, переспросит; потом 1 – отпускаю, 2 – когда-нибудь, 3 – карточкой (там «г»), 4 не тронут.
        order = [r["id"] for r in inventory.queue(conn, area="finance")]
        self.assertEqual(sorted(order), sorted(ids), "фильтр по области – только финансы")
        with answers("zz", "1о 2к 3?", "г"), contextlib.redirect_stdout(out):
            n = inventory.quick_triage(conn, self.llm(), self.cfg(), area="finance")
        self.assertIn("Не понял", out.getvalue())
        self.assertEqual(n, 3)
        st = dict(conn.execute(f"SELECT id, status FROM lr_items WHERE id IN ({','.join(map(str, ids))})").fetchall())
        self.assertEqual([st[i] for i in order], ["abandoned", "someday", "completed", "inbox"])
        self.assertIsNotNone(conn.execute("SELECT review_on FROM lr_items WHERE id=?", (order[1],)).fetchone()[0])
        self.assertIn("Ждут решения в области «Финансы»: 1", out.getvalue())
        conn.close()

    def test_10_parse_review(self) -> None:
        self.assertEqual(inventory.parse_review("2027-06"), "2027-06-01")
        self.assertEqual(len(inventory.parse_review("через полгода") or ""), 10)
        self.assertIsNotNone(inventory.parse_review("через 2 недели"))
        self.assertIsNone(inventory.parse_review("как-нибудь"))
        from datetime import date, timedelta
        self.assertEqual(inventory.parse_review("через 10 дней"), (date.fromisoformat(today()) + timedelta(days=10)).isoformat())
        self.assertIsNone(inventory.parse_review("через 10"), "без единицы – не угадывать месяцы")

    def test_10b_future_deadline_does_not_resurface(self) -> None:
        conn = self.db()
        iid = inventory.capture(conn, self.llm(), "Сдать на права категории A", "wish")
        inventory.decide(conn, iid, "abandoned", reason="передумал")
        g = {"fact_ids": [], "paths": [], "anchors": [], "first_date": "2027-06", "last_date": "2027-06",
             "last_seen": None, "touched": None, "sensitive": False, "intents": [], "currency": set()}
        self.assertFalse(inventory._refresh(conn, iid, g), "срок в будущем – не новое упоминание")
        g["last_seen"] = "2099-01-01"
        self.assertTrue(inventory._refresh(conn, iid, g))
        conn.close()

    # --- аудит -----------------------------------------------------------------------------

    def test_11_audit_line_parse(self) -> None:
        self.assertEqual(audit.parse("6 9 да, сплю плохо"),
                         {"satisfaction": 6, "importance": 9, "change": "yes", "note": "сплю плохо"})
        self.assertEqual(audit.parse("10/3 нет")["importance"], 3)
        self.assertIsNone(audit.parse("хорошо всё"))

    def test_12_audit_run_and_continue(self) -> None:
        conn = self.db()
        with answers("4 9 да, мало сплю", "п", "стоп"), quiet():
            n = audit.run(conn)
        self.assertEqual(n, 1)
        r = conn.execute("SELECT * FROM lr_audit WHERE area='health'").fetchone()
        self.assertEqual((r["satisfaction"], r["importance"], r["change"], r["nature"]), (4, 9, "yes", "problem"))
        self.assertEqual(audit.current_round(conn), (1, True), "незаконченный раунд продолжается")
        self.assertEqual(audit.focus_areas(audit.answers(conn, 1)), ["health"])
        with answers("3 4 нет", "", "стоп"), quiet():
            audit.run(conn)
        rows = audit.answers(conn, 1)
        self.assertIn("mind", rows)
        self.assertIsNone(rows["relationships"]["satisfaction"], "пропуск записан")
        with answers("стоп"), quiet():
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                audit.run(conn)
        self.assertIn("Карьера", out.getvalue())
        self.assertNotIn("Отношения –", out.getvalue(), "пропущенную область второй раз не спрашивает")
        self.assertIn("пропущено: 1", audit.summary_text(conn))
        # Исправить ответ – в том же раунде, а не новый раунд из одной области.
        with answers("5 9 да", ""), quiet():
            audit.run(conn, ["health"])
        self.assertEqual(audit.latest_round(conn), 1)
        self.assertEqual(audit.answers(conn, 1)["health"]["satisfaction"], 5)
        self.assertIn("не обязательно цель", audit.summary_text(conn))
        conn.close()

    # --- гипотезы --------------------------------------------------------------------------

    def test_13_hypotheses_merged_with_alternatives(self) -> None:
        conn = self.db()
        rows = conn.execute("SELECT * FROM lr_hypotheses").fetchall()
        self.assertEqual(len(rows), 1, "одно и то же наблюдение из двух заметок – одна гипотеза")
        h = rows[0]
        self.assertTrue(h["text"].startswith("Возможно"))
        self.assertEqual(h["status"], "new")
        self.assertEqual(h["confidence"], "low", "код сам выше «средней» не ставит; две заметки – низкая")
        self.assertIn("не хватало времени", h["alternatives_json"])
        with quiet():
            st = hypotheses.build(conn, self.llm(), self.cfg())
        self.assertEqual(st["raw"], 0, "повторная сборка ничего не дублирует")
        conn.close()

    def test_14_hypothesis_review_and_depth(self) -> None:
        conn = self.db()
        with answers("п"), quiet():
            hypotheses.review(conn)
        self.assertEqual(conn.execute("SELECT status FROM lr_hypotheses").fetchone()[0], "pattern")
        lifereset.set_depth(conn, 1)
        self.assertNotIn("новизна", lifereset.coach_static(conn))
        lifereset.set_depth(conn, 2)
        self.assertIn("новизна", lifereset.coach_static(conn))
        conn.close()

    # --- обзор, чат, выгрузка --------------------------------------------------------------

    def test_15_dashboard_and_stage(self) -> None:
        out = self.run_cli("reset")
        self.assertIn("Life Reset. Этап:", out)
        self.assertIn("Реестр:", out)
        out = self.run_cli("reset", "stage", "4")
        self.assertIn("4. Глубокая рефлексия", out)
        self.assertIn("назначен вручную", out)
        self.run_cli("reset", "stage", "авто")
        conn = self.db()
        self.assertIsNone(store.get_meta(conn, "lr_stage"))
        conn.close()

    def test_16_chat_knows_program_and_captures_idea(self) -> None:
        fake_ollama.LAST_CHAT.clear()
        out = self.run_cli("chat", inputs=("/idea написать книгу о путешествиях", "/depth 3",
                                           "хочу сделать сайт где будут собраны все идеи", "/exit"))
        self.assertIn("Записал в реестр", out)
        conn = self.db()
        r = conn.execute("SELECT * FROM lr_items WHERE title LIKE '%книгу%'").fetchone()
        self.assertEqual((r["status"], r["origin"]), ("inbox", "chat"))
        conn.close()
        system = fake_ollama.LAST_CHAT[-1]
        self.assertIn("LIFE RESET", system)
        self.assertIn("Глубина разговора – 3", system)
        self.assertIn("/idea", system)
        user = fake_ollama.LAST_USER[-1]
        # К теме реплики – в блоке памяти ответа, а не в системном промпте: так начало промпта
        # от реплики к реплике одинаковое и Ollama его не пересчитывает.
        self.assertIn("Из его реестра Life Reset к теме разговора", user)
        self.assertIn("«перегорел»", user, "всплывший отпущенный пункт – с причиной решения")
        self.assertIn("Его решения по реестру за неделю", user)
        self.assertNotIn("к теме разговора", system)
        # Повторная /idea с тем же текстом не заводит второй пункт.
        out = self.run_cli("chat", inputs=("/idea Написать книгу о путешествиях.", "/exit"))
        self.assertIn("Такая уже есть", out)
        conn = self.db()
        self.assertEqual(store.scalar(conn, "SELECT COUNT(*) FROM lr_items WHERE title LIKE '%книгу%'"), 1)
        conn.close()

    def test_16b_compact_prompt_without_pc(self) -> None:
        conn = self.db()
        full, short = lifereset.coach_static(conn), lifereset.coach_static(conn, compact=True)
        self.assertLess(len(short), len(full))
        self.assertIn("LIFE RESET", short)
        self.assertNotIn("Гипотезы", short)
        conn.close()

    def test_17_export_to_vault_folder_not_indexed(self) -> None:
        self.run_cli("reset", "export")
        folder = self.vault / "Life Reset"
        for name in ("Обзор.md", "Реестр.md", "Решения.md", "Аудит.md", "Гипотезы.md"):
            self.assertTrue((folder / name).exists(), name)
        reg = (folder / "Реестр.md").read_text(encoding="utf-8")
        self.assertIn("[[Проекты/Сайт с идеями]]", reg)
        decisions = (folder / "Решения.md").read_text(encoding="utf-8")
        self.assertIn("перегорел", decisions)
        self.assertIn("разобрано пачкой, в архив", decisions)
        self.assertNotIn(inventory.SWEEP_REASON, decisions, "пачка – одной строкой, не по пункту")
        self.assertIn("разобрано пачкой", reg)
        self.assertIn("| Здоровье | 5 / 9 ↑ |", (folder / "Аудит.md").read_text(encoding="utf-8"))
        files = [p.relative_to(self.vault).as_posix() for p in vault.walk_markdown(self.vault)]
        self.assertFalse([f for f in files if f.startswith("Life Reset/")], "свои итоги коуч как заметки не читает")
        self.assertEqual(sorted(files), sorted(NOTES), "остальной vault не тронут, своя «Life Reset» глубже читается")

    def test_17b_sensitive_items_not_detailed_in_vault(self) -> None:
        conn = self.db()
        iid = inventory.capture(conn, self.llm(), "Вернуть долг соседу", "commitment")
        conn.execute("UPDATE lr_items SET sensitive=1 WHERE id=?", (iid,))
        inventory.decide(conn, iid, "active", reason="стыдно тянуть")
        with quiet():
            lifereset.export(conn, self.cfg())
        conn.close()
        folder = self.vault / "Life Reset"
        for name in ("Реестр.md", "Решения.md"):
            text = (folder / name).read_text(encoding="utf-8")
            self.assertNotIn("долг соседу", text, name)
            self.assertNotIn("стыдно", text, name)
            self.assertIn(f"LR-{iid}** личное", text, name)

    def test_18_export_falls_back_when_vault_read_only(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("root пишет и в папку без прав")
        ro = Path(self.tmp.name) / "ro_vault"
        ro.mkdir(exist_ok=True)
        ro.chmod(0o500)
        try:
            cfg = self.cfg()
            cfg.vault = ro
            conn = self.db()
            with quiet():
                where = lifereset.export(conn, cfg)
            conn.close()
            self.assertEqual(where, self.ws / "life-reset")
            self.assertTrue((self.ws / "life-reset" / "Реестр.md").exists())
        finally:
            ro.chmod(0o700)

    def test_19_metrics(self) -> None:
        conn = self.db()
        m = inventory.metrics(conn)
        self.assertGreater(m["total"], 0)
        self.assertGreaterEqual(m["decided"], 3)
        self.assertEqual(m["capacity"], 1)
        self.assertGreaterEqual(m["active"], 1)
        conn.close()


if __name__ == "__main__":
    unittest.main()

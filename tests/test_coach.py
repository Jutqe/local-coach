"""Сквозные тесты на заглушке Ollama: все команды на маленьком выдуманном vault.

Запуск из папки code/:  python3 -m unittest discover -s tests -v
Модель не нужна, занимает секунды.
"""
from __future__ import annotations

import os as _os

_os.environ.setdefault("LOCAL_COACH_LANG", "ru")      # публичная копия по умолчанию английская, тесты написаны на русском

import contextlib
import io
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
# Не искать Ollama на соседних портах: на машине с настоящим Ollama (11434) тесты ушли бы к нему.
os.environ["LOCAL_COACH_OLLAMA_PORTS"] = ""

import fake_ollama  # noqa: E402
from localcoach import bodydouble, cli, commitments, evaluate, extract, guard, history, importer, store, synth, timeline, util  # noqa: E402
from localcoach.vectors import pack  # noqa: E402
from localcoach.config import Config  # noqa: E402

NOTES = {
    "Дневник/2014-03-10.md": "# 10 марта 2014\n\nРаботаю инженером в компании Северный мост и живу в Казани.\n"
                             "Каждое утро бегаю вдоль набережной перед работой.\n",
    "Дневник/2016-08-02.md": "# 2 августа 2016\n\nПо вечерам играю на гитаре, это меня успокаивает.\n",
    "Дневник/2015-05-01.md": "# 1 мая 2015\n\nЕздили с Персоной на дачу всей семьёй.\n"
                             "вечером Персона рассказывала про новую работу.\n",
    "Дневник/2025-02-10.md": "# 10 февраля 2025\n\nсозвонился с Персоной, она переезжает в Питер.\n",
    "Дневник/2026-09-15.md": "# 15 сентября 2026\n\nРаботаю удалённо и пишу книгу о путешествиях.\n",
    "доступы.md": "Пароль от почты: hunter2hunter2\napi_key = sk-1234567890abcdef1234567890abcdef\n",
}
BIO = ("Год начала;Год окончания;Событие\n2010;2017;Работал инженером в компании Северный мост в Казани\n"
       "2023;по настоящее время;Работаю удалённо на фрилансе\n")


def answers(*items: str):
    """Ответы для input(); когда кончаются — EOF, как при закрытом вводе."""
    it = iter(items)

    def fake(prompt: str = "") -> str:
        try:
            return next(it)
        except StopIteration:
            raise EOFError from None

    @contextlib.contextmanager
    def patched():
        # stdin is replaced too: the code asks some questions only when stdin is a terminal (isatty),
        # and the tests must not depend on whether they are run from a terminal or from CI
        with mock.patch("builtins.input", fake), mock.patch("sys.stdin", io.StringIO()):
            yield
    return patched()


class CoachTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server, cls.url = fake_ollama.start()
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        cls.vault, cls.ws = root / "vault", root / "ws"
        for rel, text in NOTES.items():
            (cls.vault / rel).parent.mkdir(parents=True, exist_ok=True)
            (cls.vault / rel).write_text(text, encoding="utf-8")
        cls.bio = root / "Биография.csv"
        cls.bio.write_text(BIO, encoding="utf-8")
        cls.run_cli("update")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.tmp.cleanup()

    @classmethod
    def run_cli(cls, *args: str) -> str:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = cli.main(["--vault", str(cls.vault), "--workspace", str(cls.ws), "--ollama", cls.url, *args])
        assert code in (0, None), f"{args}: код {code}\n{out.getvalue()[-2000:]}"
        return out.getvalue()

    def db(self) -> sqlite3.Connection:
        return store.connect(self.ws / "coach.sqlite3")

    # --- конвейер --------------------------------------------------------------------------

    def test_01_update_builds_memory(self) -> None:
        conn = self.db()
        self.assertGreater(store.scalar(conn, "SELECT COUNT(*) FROM facts"), 3)
        self.assertGreater(store.scalar(conn, "SELECT COUNT(*) FROM clusters"), 0)
        self.assertEqual(store.scalar(conn, "SELECT COUNT(*) FROM chunks WHERE status!='done'"), 0)
        for name in ("profile.md", "profile_plain.md", "clusters_review.md", "hypotheses.md"):
            self.assertTrue((self.ws / "memory" / name).exists(), name)
        full = (self.ws / "memory" / "profile.md").read_text(encoding="utf-8")
        plain = (self.ws / "memory" / "profile_plain.md").read_text(encoding="utf-8")
        self.assertIn("источники:", full)
        self.assertGreater(plain.count("\n- "), 0, plain)
        for noise in ("источники:", "`", "Обновлено:", "одно упоминание", "заметок:"):
            self.assertNotIn(noise, plain)
        conn.close()

    def test_02_secrets_never_reach_facts(self) -> None:
        conn = self.db()
        self.assertEqual(store.scalar(conn, "SELECT status FROM notes WHERE path='доступы.md'"), "security_hold")
        self.assertEqual(store.scalar(conn, "SELECT COUNT(*) FROM facts WHERE claim LIKE '%hunter2%' "
                                            "OR claim LIKE '%sk-123%'"), 0)
        conn.close()

    def test_03_invented_quote_is_not_evidence(self) -> None:
        conn = self.db()
        row = conn.execute("SELECT quote_match, evidence_quote FROM facts WHERE claim='Автор любит тёплую погоду' "
                           "LIMIT 1").fetchone()
        self.assertEqual((row["quote_match"], row["evidence_quote"]), ("none", ""))
        conn.close()

    def test_04_profile_cites_only_real_clusters(self) -> None:
        conn = self.db()
        profile = store.cache_any(conn, "profile", "final")
        ids = {x for it in profile.get("core_facts", []) for x in it["ids"]}
        self.assertNotIn("C999999", ids)
        conn.close()

    # --- актуальность ------------------------------------------------------------------------

    def test_05_import_and_timeline(self) -> None:
        self.run_cli("import", str(self.bio))
        conn = self.db()
        self.assertTrue(timeline.timeline_entries(conn))
        past = conn.execute("SELECT valid_until, period FROM clusters WHERE currency='past' AND currency_src='timeline'"
                            ).fetchall()
        self.assertTrue(past, "сверка с хронологией не дала ни одного «в прошлом»")
        self.assertTrue(all(p["period"] in (None, "2010–2017") for p in past))
        # Вердикт без ссылки на строку хронологии (A1 в заглушке) не принимается.
        unknown = [v for v in (store.loads(r[0], {}) for r in conn.execute(
            "SELECT payload_json FROM artifacts WHERE stage='currency'")) if v.get("verdict") == "unknown"]
        self.assertTrue(unknown)
        # Свежая заметка 2026 года — «сейчас».
        self.assertGreater(store.scalar(conn, "SELECT COUNT(*) FROM clusters WHERE currency='current'"), 0)
        conn.close()

    def test_06_calibrate_answers_persist(self) -> None:
        conn = self.db()
        before = len(timeline.question_queue(conn, cli.Config()))
        conn.close()
        with answers("с", "б 2012-2016", "уволился в 2019", "п", "в"):
            out = self.run_cli("calibrate")
        conn = self.db()
        marks = store.scalar(conn, "SELECT COUNT(DISTINCT created_at) FROM currency_marks WHERE source='user'")
        if before:
            self.assertGreaterEqual(marks, 1, out)
        self.assertNotIn("Traceback", out)
        conn.close()

    def test_07_recluster_keeps_currency(self) -> None:
        conn = self.db()
        before = dict(conn.execute("SELECT currency, COUNT(*) FROM clusters GROUP BY currency").fetchall())
        conn.close()
        self.run_cli("recluster")
        conn = self.db()
        after = dict(conn.execute("SELECT currency, COUNT(*) FROM clusters GROUP BY currency").fetchall())
        self.assertEqual(before.get("past", 0), after.get("past", 0))
        conn.close()

    # --- разговор и договорённости ------------------------------------------------------------

    def test_08_chat_commitments(self) -> None:
        with answers("Решил сходить на пробежку до 2 ноября", "да", "/exit"):
            self.run_cli("chat")
        conn = self.db()
        rows = conn.execute("SELECT text, due, status FROM commitments").fetchall()
        self.assertEqual(len(rows), 1, "почти повторяющаяся договорённость не должна добавиться")
        self.assertEqual((rows[0]["due"], rows[0]["status"]), ("2026-11-02", "open"))
        conn.close()
        with answers("Сделал пробежку", "ага", "/agreements", "", "/exit"):
            self.run_cli("chat")
        conn = self.db()
        self.assertEqual(store.scalar(conn, "SELECT status FROM commitments LIMIT 1"), "done")
        self.assertEqual(store.scalar(conn, "SELECT COUNT(*) FROM sessions WHERE summary!=''"), 2)
        conn.close()

    def test_09a_chat_about_coach(self) -> None:
        fake_ollama.LAST_CHAT.clear()
        fake_ollama.LAST_USER.clear()
        with answers("Подправь, что я переехал в Казань в 2024. Сейчас ещё запущу калибровку", "1",
                     "я имел в виду твою калибровку, ты выдаёшь подсказку про пункты", "/exit"):
            out = self.run_cli("chat")
        self.assertIn("Запомнил", out)
        self.assertIn("Похожее в памяти", out)
        self.assertIn("[Теперь в прошлом: 1]", out)
        self.assertIn("профиль учтёт при следующем", out.lower())
        conn = self.db()
        claims = [r[0] for r in conn.execute("SELECT claim FROM facts WHERE origin='chat' AND claim LIKE '%Казань%'")]
        self.assertTrue(claims)
        self.assertFalse(any("калибровк" in c for c in claims), "запомнена только первая фраза")
        conn.close()
        self.assertIn("О СЕБЕ. Ты — программа Local Coach", fake_ollama.LAST_CHAT[-1])
        self.assertIn("Ничего подходящего не найдено", fake_ollama.LAST_USER[-1],
                      "сообщение о самом коуче — без поиска по заметкам")
        self.assertNotIn("Ничего подходящего не найдено", fake_ollama.LAST_USER[0])

    def test_09b_think_fallback(self) -> None:
        import os
        os.environ["LOCAL_COACH_THINK_CHAT"] = "1"
        try:
            from importlib import reload
            from localcoach import config
            reload(config)
            cli.Config = config.Config
            with answers("Как дела?", "/exit"):
                out = self.run_cli("chat")
        finally:
            del os.environ["LOCAL_COACH_THINK_CHAT"]
            reload(config)
            cli.Config = config.Config
        self.assertIn("(думаю…) ..", out)
        self.assertIn("рассуждение не уложилось", out)
        self.assertIn("Слышу тебя.", out)

    def test_09f_finds_ollama_on_another_port(self) -> None:
        port = self.url.rsplit(":", 1)[1]
        found = self.ws / "ollama_found.txt"
        cfg = cli.Config()
        cfg.workspace, cfg.ollama, cfg.ollama_fallback, cfg.ollama_ports = self.ws, "http://127.0.0.1:9", "", f"9-10,{port}"
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.connect_llm(cfg)
        self.assertEqual(cfg.ollama, self.url)
        self.assertIn("нашёл его на", out.getvalue())
        self.assertEqual(found.read_text(encoding="utf-8").strip(), self.url)
        # Следующий запуск — сразу на запомненный, без поиска и сообщения.
        cfg2 = cli.Config()
        cfg2.workspace, cfg2.ollama, cfg2.ollama_fallback, cfg2.ollama_ports = self.ws, "http://127.0.0.1:9", "", f"9,{port}"
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.connect_llm(cfg2)
        self.assertEqual(cfg2.ollama, self.url)
        self.assertNotIn("нашёл", out.getvalue())
        # Свой порт снова отвечает — запомненный забывается.
        cfg3 = cli.Config()
        cfg3.workspace, cfg3.ollama, cfg3.ollama_ports = self.ws, self.url, ""
        with contextlib.redirect_stdout(io.StringIO()):
            cli.connect_llm(cfg3)
        self.assertFalse(found.exists())
        self.assertEqual(cli.port_candidates("http://10.0.0.5:11434", "11434-11436"),
                         ["http://10.0.0.5:11435", "http://10.0.0.5:11436"])

    def test_09d_server_fallback(self) -> None:
        # ПК выключен: разговор идёт на запасном Ollama, тяжёлые команды — отказ с подсказкой.
        down = ["--ollama", "http://127.0.0.1:9", "--ollama-fallback", self.url]
        with answers("Как дела?", "/exit"):
            out = self.run_cli(*down, "chat")
        self.assertIn("ПК недоступен — работаю на модели сервера", out)
        self.assertIn("Слышу тебя.", out)
        for extra, why in ((down, "Без ПК работают chat"), (["--ollama", "http://127.0.0.1:9", "--ollama-fallback", ""], "Включи ПК")):
            args = ["--vault", str(self.vault), "--workspace", str(self.ws), *extra, "update", "--skip-synth"]
            with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as stop:
                cli.main(args)
            self.assertIn(why, str(stop.exception))
        cfg = cli.Config()
        cfg.workspace, cfg.ollama, cfg.ollama_fallback = self.ws, "http://127.0.0.1:9", self.url
        with contextlib.redirect_stdout(io.StringIO()):
            cli.connect_llm(cfg, fallback=True)
        self.assertTrue(cfg.on_fallback)
        self.assertEqual(cfg.ollama, self.url)
        self.assertLessEqual(cfg.chat_budget_chars, cfg.fallback_chat_budget)

    def test_09_chat_commands(self) -> None:
        with answers("/help", "/sources", "/remember я переехал в Санкт-Петербург в 2017", "", "/calibrate", "в",
                     "/forget Казань", "", "/new", "/exit"):
            out = self.run_cli("chat")
        self.assertIn("Запомнил", out)
        conn = self.db()
        self.assertGreater(store.scalar(conn, "SELECT COUNT(*) FROM facts WHERE origin='chat'"), 0)
        conn.close()

    def test_09d_think_command(self) -> None:
        with answers("привет", "/think", "как дела", "/think off", "ещё раз", "/think подумай над этим", "/exit"):
            out = self.run_cli("chat")
        self.assertIn("Рассуждение включено", out)
        self.assertIn("Рассуждение выключено", out)
        # «(думаю…)» только у второго ответа (после /think) и у разового вопроса — не у первого и не у третьего
        self.assertEqual(out.count("(думаю…)"), 2)

    def test_09e_history_without_model(self) -> None:
        out = self.run_cli("bio", "2010-2026")
        self.assertIn("2010\n  - 2010–2017: Работал инженером в компании Северный мост в Казани (биография)", out)
        self.assertIn("с 2023 по настоящее время: Работаю удалённо на фрилансе (биография)", out)
        self.assertIn("пересказы строк биографии", out)      # что модель извлекла из тех же строк — не повтор
        self.assertIn("Пустые годы: 2018–2022.", out)          # период строки биографии — не пустые годы
        full = self.run_cli("history", "--all")   # прежнее имя тоже работает
        self.assertIn("Всё, что есть в базе с датой", full)
        self.assertIn("Каждое утро бегаю", full)
        # Просьба в чате идёт мимо модели: ответа «Коуч:» нет, список напечатан.
        with answers("выведи весь список фактов по годам из базы, не сочиняя", "/exit"):
            chat = self.run_cli("chat")
        self.assertIn("Хронология за", chat)
        self.assertNotIn("Коуч:", chat)

    def test_09b_focus_mode(self) -> None:
        with answers("побудь рядом, сажусь за отчёт", "застрял на введении", "стоп", "/focus код", "/focus", "/exit"):
            out = self.run_cli("chat")
        self.assertEqual(out.count("Начали"), 2)
        self.assertEqual(out.count("Закончили"), 2)
        conn = self.db()
        rows = conn.execute("SELECT task, ended_at FROM focus_sessions ORDER BY id").fetchall()
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r["ended_at"] for r in rows))
        # «стоп» выключил режим, а не ушёл коучу как реплика.
        self.assertIsNone(store.scalar(conn, "SELECT id FROM turns WHERE content='стоп'"))
        conn.close()

    def test_09c_people_and_checkin(self) -> None:
        conn = self.db()
        masha = conn.execute("SELECT * FROM people WHERE name='Персона'").fetchone()
        self.assertIsNotNone(masha)
        self.assertEqual(masha["mentions"], 3)
        self.assertEqual((masha["first_date"][:4], masha["last_date"][:4]), ("2015", "2025"))
        self.assertIsNone(conn.execute("SELECT id FROM people WHERE name='Имярек'").fetchone(),
                          "имени нет в тексте — модель его выдумала")
        conn.close()
        with answers("к младшая сестра", "с", "в"):
            out = self.run_cli("people")
        self.assertIn("кто это: младшая сестра; сейчас в жизни", out)
        self.assertIn("Персона — младшая сестра (сказал ты)", self.run_cli("people", "--list"))
        fake_ollama.LAST_CHAT.clear()
        with answers("Как там Персона?", "/checkin", "7, бодро", "пишу книгу о путешествиях", "", "что дальше?", "/exit"):
            out = self.run_cli("chat")
        self.assertIn("Запомнил: Автор сейчас", out)
        first, last = fake_ollama.LAST_CHAT[0], fake_ollama.LAST_CHAT[-1]
        self.assertIn("Персона — младшая сестра (сказал ты)", first, "названный человек — в подсказке коуча")
        self.assertNotIn("ПОСЛЕДНИЙ ЧЕК-ИН", first)
        self.assertIn("ПОСЛЕДНИЙ ЧЕК-ИН", last)
        self.assertIn("главное: пишу книгу о путешествиях", last)
        conn = self.db()
        self.assertEqual(store.scalar(conn, "SELECT mood FROM checkins ORDER BY id DESC LIMIT 1"), 7)
        conn.close()
        # Ответ про человека переживает повторное извлечение.
        self.run_cli("reextract")
        self.run_cli("update", "--skip-synth")
        conn = self.db()
        masha = conn.execute("SELECT * FROM people WHERE name='Персона'").fetchone()
        self.assertEqual((masha["relation_src"], masha["status"], masha["mentions"]), ("user", "current", 3))
        conn.close()

    def test_10_agreements_command(self) -> None:
        with answers("+ Позвонить маме до 2026-10-26", "+ Позвонить маме", "1 б передумал", "абв", ""):
            out = self.run_cli("agreements")
        self.assertIn("такая уже есть", out)
        conn = self.db()
        self.assertEqual(store.scalar(conn, "SELECT status FROM commitments WHERE text LIKE 'Позвонить%'"), "dropped")
        conn.close()

    # --- оценка и служебные команды -------------------------------------------------------------

    def test_11_eval_and_report(self) -> None:
        with answers("в", "н", "б 2015", "в", "0", "0", "х", "00"):
            self.run_cli("eval", "--n", "3")
        with answers("н", "о", "ч", "", "00"):
            self.run_cli("eval", "--only", "profile", "--n", "2")
        report = self.run_cli("eval", "--report")
        self.assertIn("Оценка качества", report)
        self.assertIn("профиль 20", report)
        self.assertIn("обобщение из одного случая 1", report)
        conn = self.db()
        self.assertGreater(store.scalar(conn, "SELECT COUNT(*) FROM eval_marks"), 0)
        # В отчёте нет содержимого пунктов — только числа.
        for (claim,) in conn.execute("SELECT claim FROM facts"):
            self.assertNotIn(claim, report)
        conn.close()

    def test_12_service_commands(self) -> None:
        for args in (("status",), ("plan",), ("export",), ("review",), ("update",), ("sanitize",), ("update",)):
            with answers("п", "п", "п"):
                self.run_cli(*args)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.main(["--vault", str(self.vault), "--workspace", str(self.ws), "--ollama", self.url,
                      "--synth-model", "qwen3.5:27b", "update", "--skip-synth"])
        self.assertIn("не найдена — синтез на qwen3.5:9b", out.getvalue())
        # Облачные модели и адрес вне домашней сети — отказ до отправки заметок.
        for extra in (["--synth-model", "qwen3.5:397b-cloud"], ["--synth-model", "qwen3.5:27b-remote"],
                      ["--model", "qwen3.5"], ["--ollama", "http://8.8.8.8:11434"]):
            args = ["--vault", str(self.vault), "--workspace", str(self.ws), "--ollama", self.url,
                    *extra, "update", "--skip-synth"]
            with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as stop:
                cli.main(args)
            self.assertRegex(str(stop.exception), "облачная|нет в Ollama|вне домашней сети", extra)
        conn = self.db()
        self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
        conn.close()



class DatesTest(unittest.TestCase):
    """Даты без надёжной даты заметки: дневник по годам, плотный список, дата изменения файла."""

    SHORT = "".join(f"Шаг {i}: купить продукты и свежий хлеб к ужину\n" for i in range(1, 19))
    LIST = "".join(f"Пункт номер {i}: позвонить в сервис и договориться о ремонте машины\n" for i in range(1, 41))
    DIARY = ("# Дневник\n\nВступление без даты, просто несколько слов.\n\n## 2013\n\n"
             "Переехал в город Самару и снял квартиру.\n\n## 2014 год\n\nУстроился работать в компанию Волга.\n")
    BOLD_DIARY = ("# Летопись\n\n**2016 - мне 26 - новая работа**\n\nНачал учить испанский язык по вечерам.\n\n"
                  "**2018**\n\nКупил велосипед и ездил на нём летом.\n\n**Важно:** просто жирный текст без года.\n\n"
                  "2019 год:\n- Выучил песню на гитаре за месяц.\n")

    @classmethod
    def setUpClass(cls) -> None:
        cls.server, cls.url = fake_ollama.start()
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        cls.vault, cls.ws = root / "vault", root / "ws"
        cls.vault.mkdir()
        (cls.vault / "Список дел.md").write_text(cls.LIST, encoding="utf-8")
        (cls.vault / "Короткий список.md").write_text(cls.SHORT, encoding="utf-8")
        (cls.vault / "Мои годы.md").write_text(cls.DIARY, encoding="utf-8")
        (cls.vault / "Летопись.md").write_text(cls.BOLD_DIARY, encoding="utf-8")
        (cls.vault / "Мотор.md").write_text("# Коробка\n\n**2000–3000 об:**\nНа прогреве держу 2000 оборотов, "
                                            "переключения мягкие.\n\n2000-2500:\nНа трассе всё ровно.\n",
                                            encoding="utf-8")
        (cls.vault / "Я - дневник").mkdir()
        (cls.vault / "Я - дневник" / "Мысли.md").write_text("Заметка без годов, просто мысли о жизни и работе.\n",
                                                             encoding="utf-8")
        cls.out = cls.run_cli("update")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.tmp.cleanup()

    run_cli = classmethod(CoachTest.run_cli.__func__)
    db = CoachTest.db

    def test_dense_chunk_is_split(self) -> None:
        conn = self.db()
        n = store.scalar(conn, "SELECT COUNT(*) FROM facts WHERE path='Список дел.md' AND claim LIKE 'Автор: Пункт%'")
        self.assertEqual(n, 40, "плотный фрагмент потерял факты на потолке")
        conn.close()

    def test_short_dense_chunk_is_split(self) -> None:
        self.assertLess(len(self.SHORT), 1200)
        conn = self.db()
        n = store.scalar(conn, "SELECT COUNT(*) FROM facts WHERE path='Короткий список.md' AND claim LIKE 'Автор: Шаг%'")
        self.assertEqual(n, 18, "короткий плотный фрагмент потерял факты на потолке")
        conn.close()

    def test_mtime_year_is_not_a_date(self) -> None:
        conn = self.db()
        self.assertEqual(store.scalar(conn, "SELECT COUNT(*) FROM facts WHERE path='Список дел.md' AND date_reliable=1"), 0)
        conn.close()

    def test_year_headings_date_diary(self) -> None:
        conn = self.db()
        dates = dict(conn.execute("SELECT substr(claim, 8, 12), event_date FROM facts WHERE path='Мои годы.md' "
                                  "AND date_reliable=1").fetchall())
        self.assertEqual(dates.get("Переехал в г"), "2013")
        self.assertEqual(dates.get("Устроился ра"), "2014")
        self.assertIn("Мои годы.md", timeline.chronology_paths(conn))
        self.assertTrue(any("(дневник)" in e["text"] for e in timeline.timeline_entries(conn)))
        conn.close()

    def test_bold_year_lines_date_diary(self) -> None:
        conn = self.db()
        dates = dict(conn.execute("SELECT substr(claim, 8, 12), event_date FROM facts WHERE path='Летопись.md' "
                                  "AND date_reliable=1").fetchall())
        self.assertEqual(dates.get("Начал учить "), "2016")
        self.assertEqual(dates.get("Купил велоси"), "2018")
        self.assertEqual(dates.get("Выучил песню"), "2019")
        chrono = timeline.chronology_paths(conn)
        self.assertIn("Летопись.md", chrono)
        self.assertIn("Я - дневник/Мысли.md", chrono, "папка «дневник» — тоже хронология")
        conn.close()

    def test_rpm_is_not_a_year(self) -> None:
        conn = self.db()
        self.assertEqual(store.scalar(conn, "SELECT COUNT(*) FROM chunks WHERE path='Мотор.md' "
                                            "AND chunk_date LIKE '2000%'"), 0)
        self.assertEqual(store.scalar(conn, "SELECT COUNT(*) FROM facts WHERE path='Мотор.md' "
                                            "AND date_reliable=1 AND event_date LIKE '2000%'"), 0)
        conn.close()
        for text in ("2000–3000 об:", "2000-2500", "2000 - 3000 оборотов", "2000-е"):
            self.assertIsNone(util.heading_year(text), text)
        for text in ("2000", "2000 год", "2000 — переезд", "2015: 3 события"):
            self.assertIsNotNone(util.heading_year(text), text)
        for text in ("2000 оборотов", "на 1500–2000 об", "стоил 2000 руб", "2000-е", "2000-3000"):
            self.assertFalse(util.year_in_text(text, "2000"), text)
        for text in ("в 2000 году", "2000:", "ремонт 2000 г.", "в '00 году"):
            self.assertTrue(util.year_in_text(text, "2000"), text)

    def test_portrait_present_tense_and_name(self) -> None:
        self.assertEqual(synth.past_verbs("Семен работал техлидом и воспитывал дочь, жил в Риге."), 3)
        self.assertEqual(synth.past_verbs("Он работает техлидом, растит дочь; дела, сила, школа."), 0)
        out = self.run_cli("name", "Персонаж")
        self.assertIn("Персонаж", out)
        cfg = cli.Config()
        cfg.workspace, cfg.ollama = self.ws, self.url
        conn = self.db()
        llm = cli.connect_llm(cfg)
        fixed = synth.fix_portrait(conn, llm, cfg, "Семен работал техлидом и воспитывал дочь.")
        self.assertEqual(fixed, "Персонаж работает техлидом и растит дочь.")
        # Переписанное «прошлее» исходного не принимается.
        self.assertFalse(synth.portrait_ok("Он работал и жил там.", "Он работает и живёт там.", "", set()))
        self.assertTrue(synth.portrait_ok("Он работает там.", "Он работал там.", "", set()))
        conn.close()
        self.run_cli("name", "-")

    def test_plan_counts_dense_chunks(self) -> None:
        conn = self.db()
        conn.execute("DELETE FROM meta WHERE key=?", (extract.RESPLIT_KEY,))
        conn.commit()
        conn.close()
        out = self.run_cli("plan")
        self.assertIn("Фрагментов потребуется извлечь: 1 ", out)
        self.assertIn("будут разобраны по частям: 1", out, "после 4.6 — только короткие, длинные уже поделены")
        conn = self.db()
        conn.execute("DELETE FROM meta WHERE key='resplit_v46'")
        conn.commit()
        conn.close()
        self.assertIn("будут разобраны по частям: 2", self.run_cli("plan"), "с 4.5 сразу — все на потолке")

    def test_repair_and_resplit_on_update(self) -> None:
        conn = self.db()
        fid = store.scalar(conn, "SELECT id FROM facts WHERE path='Список дел.md' LIMIT 1")
        conn.execute("UPDATE facts SET date_reliable=1, event_date='2026' WHERE id=?", (fid,))
        conn.execute("DELETE FROM meta WHERE key=?", (extract.RESPLIT_KEY,))
        conn.commit()
        conn.close()
        out = self.run_cli("update")
        self.assertIn("упёрлись в потолок фактов", out)
        conn = self.db()
        self.assertEqual(store.scalar(conn, "SELECT COUNT(*) FROM facts WHERE path='Список дел.md' AND date_reliable=1"), 0)
        self.assertEqual(store.scalar(conn, "SELECT COUNT(*) FROM facts WHERE path='Список дел.md' "
                                            "AND claim LIKE 'Автор: Пункт%'"), 40)
        conn.close()


class MigrationTest(unittest.TestCase):
    def test_old_database_gets_new_columns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "coach.sqlite3"
            store.connect(path).close()
            raw = sqlite3.connect(path)
            for table, column, _ddl in store.MIGRATIONS:
                raw.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
            raw.commit()
            raw.close()
            conn = store.connect(path)
            for table, column, _ddl in store.MIGRATIONS:
                cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
                self.assertIn(column, cols)
            conn.close()


class HelpersTest(unittest.TestCase):
    def test_period_and_labels(self) -> None:
        self.assertEqual(timeline._period("до 2016-08"), "")
        self.assertEqual(timeline._period("2025–2025"), "")
        self.assertEqual(timeline._period("с 2017 по 2023 год"), "2017–2023")
        self.assertEqual(timeline.label("past", "", "2018", "до 2016-08", None), "в прошлом (до 2018)")
        self.assertEqual(timeline._years("б 2012-2016"), ("2016", "2012–2016"))

    def test_think_command(self) -> None:
        cfg = Config()
        cfg.think_chat = False
        self.assertEqual(util.think_command(cfg, ""), "set")        # пусто: переключить
        self.assertTrue(cfg.think_chat)
        self.assertEqual(util.think_command(cfg, "выкл"), "set")
        self.assertFalse(cfg.think_chat)
        self.assertEqual(util.think_command(cfg, "ВКЛ"), "set")
        self.assertTrue(cfg.think_chat)
        self.assertEqual(util.think_command(cfg, "почему я откладываю дела?"), "once")  # вопрос: режим не меняется
        self.assertTrue(cfg.think_chat)

    def test_until_not_note_date(self) -> None:
        v = timeline._clean_verdict({"verdict": "past", "until": "2016-08", "ids": ["T1"]}, {"T1"}, "2016-08")
        self.assertEqual(v["until"], "")
        self.assertEqual(timeline._clean_verdict({"verdict": "past", "ids": ["T9"]}, {"T1"})["verdict"], "unknown")

    def test_similar_commitments(self) -> None:
        self.assertTrue(commitments.similar("Сходить на две утренние пробежки по 3 км",
                                            "Продолжать утренние пробежки по 3 км"))
        self.assertFalse(commitments.similar("Сходить на пробежку", "Позвонить маме в воскресенье"))

    def test_secret_patterns(self) -> None:
        self.assertTrue(guard.text_hold('{"password": "hunter2hunter2"}'))
        self.assertTrue(guard.text_hold("bot 1234567890:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw1"))
        self.assertIsNone(guard.text_hold("У аккаунта нет пароля, поэтому вход по ссылке."))
        self.assertIsNone(guard.text_hold("Встреча в 10:30, обсудили пароль-политику."))

    def test_body_double(self) -> None:
        said: list[str] = []
        speak = threading.Event()
        bd = bodydouble.BodyDouble(None, Config(), "отчёт", said.append, speak.is_set, every=(0.01, 0.02))
        bd.start()
        time.sleep(0.1)
        self.assertEqual(said, [], "пока человек набирает, коуч молчит")
        speak.set()
        time.sleep(0.2)
        bd.stop()
        self.assertGreaterEqual(len(said), 3)
        self.assertTrue(all(len(s.split()) <= 5 and "?" not in s for s in said))
        self.assertEqual(len(said[:4]), len(set(said[:4])), "подряд без повторов")
        bd.started -= 11 * 60
        self.assertEqual(bd.next_line(), "10 минут.")
        self.assertNotEqual(bd.next_line(), "10 минут.")
        self.assertEqual([bodydouble._minutes(n) for n in (1, 3, 11, 21, 60, 70)],
                         ["1 минута", "3 минуты", "11 минут", "21 минута", "1 ч", "1 ч 10 мин"])
        self.assertTrue(bodydouble.START.search("Побудь рядом, сажусь писать"))
        self.assertTrue(bodydouble.START.search("давай body double"))
        self.assertIsNone(bodydouble.START.search("он был рядом со мной"))
        self.assertTrue(bodydouble.STOP.match("Закончил!"))
        self.assertIsNone(bodydouble.STOP.match("закончил введение, дальше сложнее"))
        self.assertEqual(bodydouble._clean(["Я тут", "Как дела?", "Твой прогресс виден", "очень длинная реплика из многих разных слов"]),
                         ["Я тут."])

    def test_history_request_and_range(self) -> None:
        for text in ("выведи то что у тебя есть", "выведи историю по тем годам что есть, с 1988 по 2026",
                     "покажи хронологию", "выведи весь список фактов по годам из базы"):
            self.assertTrue(history.ASK.search(text), text)
        for text in ("покажи всё, что есть в памяти про работу", "дай совет, как быть с работой",
                     "напиши, как ты думаешь, главные события моей жизни"):
            self.assertFalse(history.ASK.search(text), text)
        self.assertEqual(history.parse_range("с 1988 по 2026"), (1988, 2026))
        self.assertEqual(history.parse_range("2005"), (2005, 2005))
        self.assertEqual(history.parse_range("начиная с 2010"), (2010, None))
        self.assertEqual(history.parse_range("всё"), (None, None))

    def _history_db(self, tmp: str) -> sqlite3.Connection:
        conn = store.connect(Path(tmp) / "h.sqlite3")
        conn.execute("INSERT INTO notes(path, title, sha256, size_bytes, mtime, date_source, status, updated_at) "
                     "VALUES('_imports/Био_xlsx.md', 'Био', 'x', 1, 0, 'import', 'indexed', '')")
        conn.execute("INSERT INTO chunks(path, idx, char_start, char_end, line_start, line_end, sha256, body, updated_at) "
                     "VALUES('_imports/Био_xlsx.md', 0, 0, 1, 1, 3, 'x', ?, '')",
                     ("## Лист1\n- Год начала: 2021; Год окончания: 2026; Событие: Владел Porsche 911 Cabriolet\n"
                      "- Сфера: Работа; 2015–2017: Company1; 2018: Company2\n- Заметка без года\n",))
        return conn

    @staticmethod
    def _add(conn: sqlite3.Connection, cluster: int, when: str, claim: str, vec: list[float], kind: str = "event",
             path: str = "n.md", project: int = 0) -> None:
        conn.execute("INSERT INTO facts(source_ref, origin, path, claim, kind, domain, event_date, cluster_id, "
                     "embedding, is_project, created_at) VALUES('x', 'note', ?, ?, ?, 'work', ?, ?, ?, ?, '')",
                     (path, claim, kind, when, cluster, pack(vec + [0.0] * 253), project))

    def test_history_merges_multi_year_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = store.connect(Path(tmp) / "h.sqlite3")
            job, city = [1.0, 0.0, 0.0], [0.0, 1.0, 0.0]
            near = [0.95, 0.31, 0.0]   # косинус с job ≈ 0,95
            add = self._add
            add(conn, 1, "2010", "Автор работал инженером 2010–2017", job)
            add(conn, 2, "2014", "Автор стал старшим инженером", near)       # внутри периода — та же строка
            add(conn, 3, "2014", "Автор переехал в Казань", city)            # о другом — своя строка
            add(conn, 4, "2019", "Автор снова устроился инженером", near)    # после периода — своя строка
            add(conn, 5, "2020", "Автор переехал в Питер", city)
            add(conn, 6, "2021", "Автор переехал в Питер, район Купчино", city)  # сразу следующий год — та же строка
            data = history.entries(conn)
            conn.close()
        claims = {y: [e["claim"] for e in v] for y, v in data["years"].items()}
        self.assertEqual(claims[2010], ["Автор работал инженером 2010–2017"])
        self.assertEqual(claims[2014], ["Автор переехал в Казань"])
        self.assertEqual(claims[2019], ["Автор снова устроился инженером"])
        self.assertNotIn(2021, claims)
        self.assertEqual(data["years"][2010][0]["n"], 2)
        self.assertEqual(data["years"][2020][0]["last"], 2021)
        self.assertEqual(history.claim_span("Год начала: 2023; Год окончания: по настоящее время"),
                         __import__("datetime").date.today().year)

    def test_history_biography_rows_projects_and_repeats(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = self._history_db(tmp)
            v, w = [1.0, 0.0, 0.0], [0.0, 1.0, 0.0]
            self._add(conn, 1, "2021", "Автор занимается проектом по восстановлению Porsche 911", v,
                      path="_imports/Био_xlsx.md")                                       # пересказ строки биографии
            self._add(conn, 2, "2021", "Автор купил Porsche 911 в 2021 году", w)        # есть в биографии
            self._add(conn, 3, "2024", "Общая сумма затрат на Porsche 911 — 2,8 млн рублей", v, kind="fact")
            self._add(conn, 4, "2000", "Автор держит 2000 оборотов на прогреве", w, path="Проекты Авто/Мотор.md",
                      project=1)
            self._add(conn, 5, "2019", "Автор переехал в Минск", v)
            self._add(conn, 6, "2019", "Автор устроился на работу в Минске", w)         # тот же город — другое событие
            self._add(conn, 7, "2019", "Автор продал Honda Accord", v)
            self._add(conn, 8, "2019", "Автор отдал Honda Accord новому владельцу", w)  # тот же предмет и год
            data = history.entries(conn)
            out = history.render(data)
            full = history.render(history.entries(conn, full=True))
            conn.close()
        claims = {y: [e["claim"] for e in v] for y, v in data["years"].items()}
        self.assertEqual(claims[2021], ["Владел Porsche 911 Cabriolet"])
        self.assertEqual(claims[2015], ["Работа: Company1"])
        self.assertEqual(claims[2018], ["Работа: Company2"])
        self.assertEqual(claims[2019], ["Автор переехал в Минск", "Автор устроился на работу в Минске",
                                        "Автор продал Honda Accord"])
        self.assertNotIn(2000, claims)
        self.assertNotIn(2024, claims)
        self.assertIn("2021–2026: Владел Porsche 911 Cabriolet (биография, и в заметках: 1)", out)
        for why in ("уже есть в биографии", "о том же в том же году", "пересказы строк биографии",
                    "из папок проектов и идей", "не события"):
            self.assertIn(why, out)
        self.assertIn("2000 оборотов", full)
        self.assertIn("проект", full)

    def test_bio_age_column_is_not_a_fact(self) -> None:
        rows = ["- Год: 1988; Возраст: 0; Событие: Родился в Казани",
                "- Год: 2010; Возраст: 22; Событие: Начал работать инженером",
                "- Год: 2026; Возраст: 38; Событие: Живёт в Минске"]
        events = [e for line in rows for e in history._row_events(line)]
        self.assertEqual([e["claim"] for e in events],
                         ["Родился в Казани", "Начал работать инженером", "Живёт в Минске"])   # без «Возраст: …»
        self.assertEqual(history.birth_year(events), 1988)
        out = history.render({"years": {1988: [{**events[0], "source": "биография", "date": "1988", "n": 1,
                                                "notes": 0, "last": 1988}],
                                        2026: [{**events[2], "source": "биография", "date": "2026", "n": 1,
                                                "notes": 0, "last": 2026}]},
                              "seen": {1988, 2026}, "total": 0, "undated": 0, "start": None, "end": None,
                              "hidden": {k: 0 for k in ("project", "kind", "bio_retold", "covered", "grouped")},
                              "full": False, "bio": 3, "birth": 1988})
        self.assertIn("1988 — 0 лет\n", out)
        self.assertIn("2026 — 38 лет\n", out)
        table = importer.table_markdown([("Лист", [["Год", "Возраст", "Событие"], ["1988", "0", "Родился"]], {})])
        self.assertNotIn("Возраст", table)
        self.assertIn("Событие: Родился", table)

    def test_bio_transposed_table_age_row_series_and_future(self) -> None:
        # Годы — в столбцах, категории — в строках: возраст и ряд чисел (курс) — не события.
        rows = ["- Столбец 1: Возраст; 1988: Старт; 1989: 1; 1990: 2; 1991: 3",
                "- Столбец 1: Курс рубля к Евро-доллару; 1989: 100; 1990: 103; 2027: 107",
                "- Столбец 1: Где жил; 1988–1995: СПб, Гражданский пр.",
                "- 1988: Старт; 1989: 1; 1990: 2; 1991: 3"]
        births: list[int] = []
        events = [e for line in rows for e in history._row_events(line, births)]
        self.assertEqual([e["claim"] for e in events], ["Где жил: СПб, Гражданский пр."])
        self.assertEqual(births, [1988, 1988])
        self.assertEqual(history.birth_year([{"birth": b} for b in births]), 1988)
        year = __import__("datetime").date.today().year
        table = importer.table_markdown([("Лист", [
            ["", "1988", "1989", "1990", "1991", str(year + 1)],
            ["Возраст", "0", "1", "2", "3", str(year + 1 - 1988)],
            ["Событие", "Родился", "", "", "", "Планы"]], {})])
        self.assertNotIn("Возраст", table)
        self.assertIn("<!-- birth: 1988 -->", table)    # строка возраста ушла, год рождения остался меткой
        self.assertNotIn("Планы", table)       # столбец будущего года не запоминается
        self.assertIn("1988: Родился", table)

    def test_bio_future_and_birth_mark_in_db(self) -> None:
        year = __import__("datetime").date.today().year
        with tempfile.TemporaryDirectory() as tmp:
            conn = self._history_db(tmp)
            conn.execute("UPDATE chunks SET body = body || ?",
                         (f"<!-- birth: 1988 -->\n- Сфера: Планы; {year + 1}: Переезд; {year}: Ремонт\n",))
            self._add(conn, 1, f"{year + 2}-05-01", "Автор планирует купить дом", [1.0, 0.0, 0.0])
            self._add(conn, 2, f"{year}-01-10", "Автор начал ремонт квартиры", [0.0, 1.0, 0.0])
            out = history.show(conn, "")
            conn.close()
        self.assertIn(f"{year} — {year - 1988} ", out)
        self.assertIn("Ремонт", out)
        self.assertNotIn("Переезд", out)
        self.assertNotIn("планирует купить дом", out)
        self.assertNotIn(str(year + 1), out)

    def test_bio_plain_claims_and_single_year_first(self) -> None:
        self.assertEqual(history._plain("Автор переехал в Минск"), "Переехал в Минск")
        self.assertEqual(history._plain("Автору подарили часы"), "Подарили часы")
        self.assertEqual(history._plain("Работа: Company1"), "Работа: Company1")
        self.assertEqual(history._plain("Авторитет вырос"), "Авторитет вырос")
        with tempfile.TemporaryDirectory() as tmp:
            conn = self._history_db(tmp)
            conn.execute("UPDATE chunks SET body = body || ?", ("- Год: 2015; Событие: Съездил в отпуск\n",))
            self._add(conn, 1, "2015-06-01", "Автор купил велосипед", [1.0, 0.0, 0.0])
            out = history.show(conn, "2015")
            conn.close()
        lines = [x.strip() for x in out.splitlines()]
        i = lines.index("2015")
        block = lines[i + 1:i + 4]
        self.assertEqual([x.split(" (")[0] for x in block[:2]], ["- Съездил в отпуск", "- 01.06 Купил велосипед"])
        self.assertTrue(block[2].startswith("- 2015–2017: Работа: Company1"), block)     # период — после событий года
        self.assertNotIn("Автор", out)

    def test_project_folders(self) -> None:
        cfg = Config()
        for path in ("Проекты/Авто/Мотор.md", "Проекты 2024/план.md", "Projects - cars/x.md", "Идеи/а.md"):
            self.assertTrue(extract.is_project_path(path, cfg), path)
        for path in ("Дневник/2021.md", "Проектирование/x.md", "Мотор.md"):
            self.assertFalse(extract.is_project_path(path, cfg), path)

    def test_wilson(self) -> None:
        lo, hi = evaluate.wilson(8, 10)
        self.assertTrue(0.4 < lo < 0.5 and 0.9 < hi < 0.97)


if __name__ == "__main__":
    unittest.main()

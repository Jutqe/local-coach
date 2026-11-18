"""Веб-интерфейс коуча на заглушке Ollama: вход по PIN, разговор потоком, команды, режим присутствия.

Запуск из папки code/:  python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import os as _os

_os.environ.setdefault("LOCAL_COACH_LANG", "ru")      # публичная копия по умолчанию английская, тесты написаны на русском

import argparse
import contextlib
import http.client
import io
import json
import os
import queue
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ["LOCAL_COACH_OLLAMA_PORTS"] = ""

import fake_ollama  # noqa: E402
from localcoach import cli, web  # noqa: E402
from test_coach import NOTES  # noqa: E402

PIN = "4821"


class WatchdogTest(unittest.TestCase):
    """Возврат с сервера на ПК: основной адрес ПК (не только другие порты) должен проверяться."""

    def _wc(self, up: set[str]):
        wc = object.__new__(web.WebCoach)
        wc.primary = "http://pc:11434"
        cfg = argparse.Namespace(ollama="http://127.0.0.1:11434", on_fallback=True, ollama_ports="11434-11436")
        wc.coach = argparse.Namespace(cfg=cfg)
        self._orig = web.answers
        web.answers = lambda url, timeout=2.0: url in up
        self.addCleanup(setattr, web, "answers", self._orig)
        return wc

    def test_returns_to_pc_on_main_port(self) -> None:
        self.assertTrue(self._wc({"http://127.0.0.1:11434", "http://pc:11434"})._llm_changed())

    def test_stays_on_server_while_pc_down(self) -> None:
        self.assertFalse(self._wc({"http://127.0.0.1:11434"})._llm_changed())


class WebTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.fake, cls.url = fake_ollama.start()
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        vault, cls.ws = root / "vault", root / "ws"
        for rel, text in NOTES.items():
            (vault / rel).parent.mkdir(parents=True, exist_ok=True)
            (vault / rel).write_text(text, encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = cli.main(["--vault", str(vault), "--workspace", str(cls.ws), "--ollama", cls.url,
                             "--ollama-fallback", "", "update"])
        assert code in (0, None)
        args = argparse.Namespace(workspace=str(cls.ws), ollama=cls.url, ollama_fallback="", model=None,
                                  embedding_model=None, synth_model=None, vault=None, command="serve")
        web.FAIL_DELAY = 0
        with contextlib.redirect_stdout(io.StringIO()):
            cls.wc = web.WebCoach(lambda: cli.build_config(args), lambda c: cli.connect_llm(c, fallback=True))
        web.Handler.wc, web.Handler.auth = cls.wc, web.Auth(cls.ws, PIN)
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
        cls.httpd.daemon_threads = True
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.port = cls.httpd.server_address[1]
        cls.cookie = ""

    @classmethod
    def tearDownClass(cls) -> None:
        cls.wc.stop.set()
        cls.httpd.shutdown()
        cls.fake.shutdown()
        cls.tmp.cleanup()

    def call(self, method: str, path: str, body: dict | None = None, *, cookie: bool = True, ctype: str = "application/json"):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        headers = {"Content-Type": ctype}
        if cookie and WebTest.cookie:
            headers["Cookie"] = WebTest.cookie
        conn.request(method, path, json.dumps(body or {}) if method == "POST" else None, headers)
        res = conn.getresponse()
        data = res.read()
        return res, data

    def login(self) -> None:
        res, _ = self.call("POST", "/api/login", {"pin": PIN}, cookie=False)
        self.assertEqual(res.status, 200)
        WebTest.cookie = res.getheader("Set-Cookie").split(";")[0]

    def chat(self, text: str) -> list[dict]:
        res, data = self.call("POST", "/api/chat", {"text": text})
        self.assertEqual(res.status, 200)
        return [json.loads(line) for line in data.decode().splitlines() if line.strip()]

    def say(self, events: list[dict]) -> str:
        return " ".join(e["t"] for e in events if e["ev"] == "system")

    # --- доступ ---
    def test_01_page_and_auth_gate(self) -> None:
        res, data = self.call("GET", "/", cookie=False)
        self.assertEqual(res.status, 200)
        self.assertIn("Базилий", data.decode())
        res, data = self.call("GET", "/api/state", cookie=False)
        self.assertEqual(json.loads(data), {"auth": False, "pin": True})
        res, _ = self.call("POST", "/api/chat", {"text": "привет"}, cookie=False)
        self.assertEqual(res.status, 401)
        res, _ = self.call("GET", "/api/events", cookie=False)
        self.assertEqual(res.status, 401)

    def test_02_login(self) -> None:
        res, _ = self.call("POST", "/api/login", {"pin": "0000"}, cookie=False)
        self.assertEqual(res.status, 403)
        res, _ = self.call("POST", "/api/login", {"pin": PIN}, cookie=False)
        self.assertEqual(res.status, 200)
        cookie = res.getheader("Set-Cookie")
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        WebTest.cookie = cookie.split(";")[0]
        res, data = self.call("GET", "/api/state")
        state = json.loads(data)
        self.assertTrue(state["auth"])
        for key in ("fallback", "think", "focus", "turns", "pending", "agreements"):
            self.assertIn(key, state)

    def test_03_bad_requests(self) -> None:
        self.login()
        res, _ = self.call("POST", "/api/chat", {"text": "привет"}, ctype="text/plain")
        self.assertEqual(res.status, 400)      # простая форма с чужого сайта
        res, _ = self.call("POST", "/api/chat", {"text": "привет"}, cookie=False)
        self.assertEqual(res.status, 401)
        res, _ = self.call("GET", "/api/nothing")
        self.assertEqual(res.status, 404)

    # --- разговор ---
    def test_04_chat_streams_and_cat_states(self) -> None:
        self.login()
        events = self.chat("Как мне распределить день?")
        kinds = [e["ev"] for e in events]
        self.assertEqual(events[0], {"ev": "state", "v": "think"})
        self.assertLess(kinds.index("state"), kinds.index("token"))
        self.assertIn({"ev": "state", "v": "speak"}, events)
        self.assertEqual("".join(e["t"] for e in events if e["ev"] == "token"), "Слышу тебя.")
        self.assertEqual(events[-2], {"ev": "state", "v": "idle"})
        self.assertEqual(events[-1], {"ev": "done"})
        _, data = self.call("GET", "/api/state")
        turns = json.loads(data)["turns"]
        self.assertEqual([t["role"] for t in turns][-2:], ["user", "assistant"])
        self.assertEqual(turns[-1]["content"], "Слышу тебя.")

    def test_05_terminal_only_and_help(self) -> None:
        self.login()
        self.assertIn("в терминале", self.say(self.chat("/review")))
        self.assertIn("/think", self.say(self.chat("/help")))
        self.assertIn("Такой команды здесь нет", self.say(self.chat("/xyz")))
        # Life Reset: аудит и разбор пачкой – в терминале, а записать идею, глубина, обзор и разбор – прямо здесь.
        self.assertIn("в терминале", self.say(self.chat("/sweep")))
        self.assertIn({"ev": "open", "v": "triage", "area": ""}, self.chat("/triage"))
        self.assertIn("Записал в реестр как LR-", self.say(self.chat("/idea собрать лодку")))
        self.assertIn("Глубина разговора: 3", self.say(self.chat("/depth 3")))
        self.assertIn("Life Reset. Этап:", self.say(self.chat("/reset")))

    def test_06_think_toggle(self) -> None:
        self.login()
        self.assertFalse(self.wc.cfg.think_chat)
        self.assertIn("включено", self.say(self.chat("/think")))
        self.assertTrue(self.wc.cfg.think_chat)
        self.assertIn("выключено", self.say(self.chat("/think off")))
        self.assertFalse(self.wc.cfg.think_chat)

    def test_07_secret_is_not_sent(self) -> None:
        self.login()
        before = len(fake_ollama.LAST_USER)
        events = self.chat("api_key = sk-1234567890abcdef1234567890abcdef")
        self.assertIn("не отправлено", self.say(events))
        self.assertEqual(len(fake_ollama.LAST_USER), before)

    def test_08_focus_mode(self) -> None:
        self.login()
        events = self.chat("/focus писать отчёт")
        self.assertIn("Начали", self.say(events))
        _, data = self.call("GET", "/api/state")
        focus = json.loads(data)["focus"]
        self.assertIsNotNone(focus)
        self.assertEqual(focus["typed"], "писать отчёт", "над котом – задача своими словами")
        self.assertGreaterEqual(focus["seconds"], 0)
        events = self.chat("стоп")
        self.assertIn("Закончили", self.say(events))
        _, data = self.call("GET", "/api/state")
        self.assertIsNone(json.loads(data)["focus"])

    def test_08b_focus_on_item_with_plan(self) -> None:
        self.login()
        conn = self.wc.coach.conn
        now = "2026-01-01T10:00:00+03:00"
        iid = conn.execute("INSERT INTO lr_items(title, type, status, created_at, updated_at, decided_at) "
                           "VALUES('Курс испанского', 'project', 'active', ?, ?, ?)", (now, now, now)).lastrowid
        conn.commit()
        _, data = self.call("GET", "/api/state")
        self.assertIn({"id": iid, "title": "Курс испанского", "min30": 0}, json.loads(data)["lr_active"])
        self.assertIn("на 50 минут", self.say(self.chat(f"/focus LR-{iid} на 50 минут")))
        focus = json.loads(self.call("GET", "/api/state")[1])["focus"]
        self.assertEqual((focus["item"], focus["planned"], focus["typed"]), (iid, 50, "Курс испанского"))
        fo = self.wc.focus
        fo.started -= 50 * 60           # будто 50 минут прошли
        fo.plan_reached = True
        q: queue.Queue = queue.Queue()
        self.wc.subs.append(q)
        try:
            self.wc._plan_done(fo)
            evs = [q.get(timeout=2) for _ in range(2)]
        finally:
            self.wc.subs.remove(q)
        self.assertIn({"ev": "state", "v": "stretch"}, evs, "Базилий потягивается")
        said = next(e["t"] for e in evs if e["ev"] == "say")
        self.assertIn("как и наметил", said)
        self.assertIn("Как прошло", said)
        self.assertIsNone(self.wc.focus)
        self.chat("нормально, дописал два урока")
        row = conn.execute("SELECT item_id, planned, minutes, outcome FROM focus_sessions ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(tuple(row), (iid, 50, 50, "нормально, дописал два урока"))
        self.chat("а это уже просто разговор")
        self.assertEqual(conn.execute("SELECT outcome FROM focus_sessions ORDER BY id DESC LIMIT 1").fetchone()[0],
                         "нормально, дописал два урока", "записывается только первый ответ")

    def test_08c_triage_api(self) -> None:
        conn = self.wc.coach.conn
        now = "2026-01-01T10:00:00+03:00"
        ids = [conn.execute("INSERT INTO lr_items(title, type, status, domain, created_at, updated_at, notes_count) "
                            "VALUES(?, 'wish', 'inbox', 'finance', ?, ?, ?)", (f"Веб-разбор {n}", now, now, 5 - n)).lastrowid
               for n in range(3)]
        conn.commit()
        res, _ = self.call("GET", "/api/triage", cookie=False)
        self.assertEqual(res.status, 401, "без входа разбор недоступен")
        res, _ = self.call("POST", "/api/triage/decide", {"id": ids[0], "status": "archived"}, cookie=False)
        self.assertEqual(res.status, 401)
        self.login()
        _, data = self.call("GET", "/api/triage?area=finance")
        d = json.loads(data)
        self.assertEqual([i["id"] for i in d["items"]][:3], ids)
        self.assertEqual(json.loads(self.call("GET", "/api/state")[1])["lr_wait"], d["total"])
        _, data = self.call("GET", f"/api/triage?exclude={ids[0]},{ids[1]}&area=finance")
        self.assertEqual([i["id"] for i in json.loads(data)["items"]], [ids[2]])
        _, data = self.call("GET", f"/api/triage/card?id={ids[0]}")
        self.assertEqual(json.loads(data)["title"], "Веб-разбор 0")
        _, data = self.call("GET", f"/api/triage/find?q=%D1%80%D0%B0%D0%B7%D0%B1%D0%BE%D1%80%202&id={ids[0]}")
        self.assertEqual([i["id"] for i in json.loads(data)["items"]], [ids[2]])
        _, data = self.call("POST", "/api/triage/dedupe", {})
        self.assertEqual(json.loads(data)["merged"], 0)
        _, data = self.call("POST", "/api/triage/area", {"id": ids[1], "area": "hobbies_interests"})
        area = json.loads(data)
        self.assertTrue(area["ok"])
        self.assertEqual(conn.execute("SELECT domain FROM lr_items WHERE id=?", (ids[1],)).fetchone()[0], "hobbies_interests")
        _, data = self.call("POST", "/api/triage/undo", {"undo": area["undo"]})
        self.assertTrue(json.loads(data)["ok"])
        self.assertEqual(conn.execute("SELECT domain FROM lr_items WHERE id=?", (ids[1],)).fetchone()[0], "finance")
        _, data = self.call("POST", "/api/triage/decide", {"id": ids[0], "status": "archived", "reason": "не актуально"})
        res = json.loads(data)
        self.assertTrue(res["ok"])
        self.assertEqual(conn.execute("SELECT status FROM lr_items WHERE id=?", (ids[0],)).fetchone()[0], "archived")
        _, data = self.call("POST", "/api/triage/undo", {"undo": res["undo"]})
        self.assertTrue(json.loads(data)["ok"])
        self.assertEqual(conn.execute("SELECT status FROM lr_items WHERE id=?", (ids[0],)).fetchone()[0], "inbox")
        _, data = self.call("POST", "/api/triage/say", {"id": ids[1], "text": "пока не знаю"})
        self.assertIn("recorded", json.loads(data), "модель-заглушка решения не дала – пункт «уточнить»")
        events = self.chat("/triage финансы")
        self.assertIn({"ev": "open", "v": "triage", "area": "finance"}, events)
        self.assertIn("Не нашёл", self.say(self.chat("/triage нет-такой-области")))
        self.assertEqual(self.call("POST", "/api/triage/unknown", {})[0].status, 404)

    def test_09_new_session(self) -> None:
        self.login()
        self.chat("Расскажу, как прошёл день: был на работе.")
        self.chat("Ещё вечером бегал.")
        events = self.chat("/new")
        self.assertIn("Новая сессия", self.say(events))
        self.assertIn("reset", [e["ev"] for e in events])
        _, data = self.call("GET", "/api/state")
        self.assertEqual(json.loads(data)["turns"], [])

    def test_09b_audio_files(self) -> None:
        if not (web.AUDIO / "stream.wav").is_file():
            self.skipTest("записей нет (в публичной версии их не бывает)")
        res, _ = self.call("GET", "/audio/stream.wav", cookie=False)
        self.assertEqual(res.status, 401)
        self.login()
        res, data = self.call("GET", "/audio/stream.wav")
        self.assertEqual(res.status, 200)
        self.assertEqual(data[:4], b"RIFF")
        for bad in ("/audio/../web.py", "/audio/nope.wav", "/audio/CREDITS.md", "/audio/%2e%2e/web.py"):
            res, _ = self.call("GET", bad)
            self.assertEqual(res.status, 404, bad)

    # --- защита ---
    def test_10_lockout_after_failures(self) -> None:
        web.Handler.auth = web.Auth(self.ws, PIN)       # чистый счётчик
        codes = [self.call("POST", "/api/login", {"pin": "bad"}, cookie=False)[0].status for _ in range(6)]
        self.assertEqual(codes, [403] * 5 + [429])
        res, _ = self.call("POST", "/api/login", {"pin": PIN}, cookie=False)
        self.assertEqual(res.status, 429)               # даже верный PIN в блокировке
        web.Handler.auth = web.Auth(self.ws, PIN)

    def test_11_no_network_without_pin(self) -> None:
        os.environ.pop("LOCAL_COACH_WEB_PIN", None)
        with self.assertRaises(SystemExit) as ctx:
            web.serve(lambda: cli.build_config(argparse.Namespace(
                workspace=str(self.ws / "none"), ollama=self.url, ollama_fallback="", model=None, embedding_model=None,
                synth_model=None, vault=None, command="serve")), lambda c: None, "0.0.0.0", 0)
        self.assertIn("PIN", str(ctx.exception))

    def test_12_cookie_forgery_rejected(self) -> None:
        auth = web.Auth(self.ws, PIN)
        self.assertTrue(auth.valid(f"basil={auth.token()}"))
        self.assertFalse(auth.valid("basil=AAAA.deadbeef"))
        self.assertFalse(auth.valid(""))
        other = web.Auth(self.ws, PIN)
        other.key = b"x" * 32
        self.assertFalse(auth.valid(f"basil={other.token()}"))

    def test_13_own_pin_replaces_temporary(self) -> None:
        ws2 = Path(self.tmp.name) / "pin-ws"
        saved = web.Handler.auth
        web.Handler.auth = web.Auth(ws2, PIN)
        try:
            WebTest.cookie = ""
            self.login()
            _, data = self.call("GET", "/api/state")
            self.assertTrue(json.loads(data)["pin_temp"])
            old_cookie = WebTest.cookie
            for body, code in (({"old": PIN, "new": "12"}, 400), ({"old": PIN, "new": PIN}, 400),
                               ({"old": "0000", "new": "my-new-pin"}, 403)):
                self.assertEqual(self.call("POST", "/api/pin", body)[0].status, code, body)
            res, _ = self.call("POST", "/api/pin", {"old": PIN, "new": "my-new-pin"})
            self.assertEqual(res.status, 200)
            new_cookie = res.getheader("Set-Cookie").split(";")[0]
            self.assertNotEqual(new_cookie, old_cookie)
            WebTest.cookie = old_cookie                               # прежняя кука больше не годится
            self.assertEqual(self.call("GET", "/api/events")[0].status, 401)
            WebTest.cookie = new_cookie
            _, data = self.call("GET", "/api/state")
            self.assertFalse(json.loads(data)["pin_temp"])
            self.assertEqual(self.call("POST", "/api/login", {"pin": PIN}, cookie=False)[0].status, 403)   # временный умер
            self.assertEqual(self.call("POST", "/api/login", {"pin": "my-new-pin"}, cookie=False)[0].status, 200)
            self.assertNotIn(b"my-new-pin", (ws2 / ".web_pin").read_bytes())                              # хранится хэш
            self.assertEqual(oct((ws2 / ".web_pin").stat().st_mode & 0o777), "0o600")
            web.Handler.auth = web.Auth(ws2, PIN)                       # после перезапуска — тот же свой PIN
            self.assertEqual(self.call("POST", "/api/login", {"pin": "my-new-pin"}, cookie=False)[0].status, 200)
            self.assertTrue(web.Handler.auth.valid(f"basil={web.Auth(ws2, PIN).token()}"))
        finally:
            web.Handler.auth = saved
            WebTest.cookie = ""


if __name__ == "__main__":
    unittest.main()

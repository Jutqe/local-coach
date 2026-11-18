"""Веб-интерфейс коуча: лицо — кот Базилий. Запуск: python3 coach.py serve.

Рассчитан на постоянную работу на сервере (юнит systemd — в deploy/). Тот же коуч, что в терминале:
память, профиль, поиск, режим присутствия. Разговор идёт потоком, кот меняет состояние по ходу ответа.
Команды, которые спрашивают построчно (review, calibrate, people, agreements, import), остаются в терминале.

Доступ — по PIN-коду (переменная LOCAL_COACH_WEB_PIN): без него сервер стартует только на localhost.
Тексты разговоров в журнал не пишутся.
"""
from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import io
import json
import os
import queue
import re
import secrets
import signal
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from typing import Any, Callable

from . import bodydouble, commitments, guard, i18n, inventory, lifereset, people, store, triageweb, webi18n
from . import history as chronology
from .chat import Coach, _REMEMBER, pending_count, remember_text
from .config import Config
from .ollama import Ollama, OllamaError, answers, port_candidates
from .util import think_command, today
from .i18n import trl

PAGE = Path(__file__).resolve().parent / "web" / "index.html"
_PAGE_CACHE: list[bytes] = []


def _restart_soon() -> bool:
    """After the language is saved: under systemd (INVOCATION_ID is set) stop the process gracefully, Restart=always
    brings it back with the new language. Elsewhere there is nobody to restart it: return False, the page says so."""
    if not os.environ.get("INVOCATION_ID"):
        return False
    threading.Timer(1.0, os.kill, (os.getpid(), signal.SIGTERM)).start()
    return True


def _page_bytes() -> bytes:
    """Страница на языке процесса (webi18n); собирается один раз."""
    if not _PAGE_CACHE:
        _PAGE_CACHE.append(webi18n.localize(PAGE.read_text(encoding="utf-8")).encode("utf-8"))
    return _PAGE_CACHE[0]
AUDIO = PAGE.parent / "audio"            # записи для фоновых звуков; нет файла — страница рисует звук сама
AUDIO_TYPES = {".wav": "audio/wav", ".mp3": "audio/mpeg", ".ogg": "audio/ogg"}
COOKIE = "basil"
COOKIE_DAYS = 30
MAX_BODY = 64 * 1024
IDLE_END_SESSION = 90 * 60     # тихий час в разговоре — итог сессии сохраняется сам, как при выходе из терминала
FOCUS_ORPHAN = 10 * 60         # режим присутствия без открытой страницы столько минут — выключается
WATCH_EVERY = 60
FAIL_DELAY = 1.0               # пауза после неверного PIN-а
TERMINAL_ONLY = {"/review", "/calibrate", "/checkin", "/чекин", "/чек-ин", "/people", "/люди", "/agreements",
                 "/договорённости", "/договоренности", "/import", "/forget", "/remember",
                 "/sweep", "/пачкой", "/audit", "/аудит", "/hypotheses", "/гипотезы"}
HELP = (trl("Здесь работает разговор и режим присутствия.\n"
            "/think — рассуждение вкл/выкл (или /think вопрос — на один ответ)\n"
            "/focus задача — режим присутствия (стоп — «стоп» или /focus); можно с пунктом и планом: /focus LR-12 на 50 минут\n"
            "/bio [год] — хронология из базы\n"
            "/sources — откуда ответ\n"
            "/new — новая сессия (итог сохранится)\n"
            "Life Reset: /reset – обзор программы, /triage [область] – разбор реестра, /idea текст – записать идею в реестр, /depth 1|2|3 – глубина разговора\n"
            "Разбор предложений в память, калибровка, люди, договорённости, импорт, а также аудит, гипотезы и разбор пачкой (sweep) Life Reset — пока в терминале."))


class Auth:
    """PIN и подписанная кука. PIN из LOCAL_COACH_WEB_PIN — временный: при первом входе человек задаёт свой,
    он хранится хэшем в рабочей папке, а временный перестаёт действовать. Смена PIN-а выпускает новую куку
    и обесценивает все прежние."""

    ITER = 200_000

    def __init__(self, workspace: Path, pin: str):
        self.pin = pin
        self.pinfile = workspace / ".web_pin"
        keyfile = workspace / ".web_secret"
        try:
            self.key = keyfile.read_bytes()
        except OSError:
            self.key = secrets.token_bytes(32)
            workspace.mkdir(parents=True, exist_ok=True)
            keyfile.write_bytes(self.key)
            os.chmod(keyfile, 0o600)
        self.custom = self._load()
        self.fails: list[float] = []
        self._lock = threading.Lock()

    def _load(self) -> tuple[bytes, bytes] | None:
        try:
            salt, digest = self.pinfile.read_text().strip().split("$", 1)
            return bytes.fromhex(salt), bytes.fromhex(digest)
        except (OSError, ValueError):
            return None

    @property
    def enabled(self) -> bool:
        return bool(self.pin or self.custom)

    @property
    def needs_change(self) -> bool:
        return bool(self.pin) and self.custom is None

    def _hash(self, pin: str, salt: bytes) -> bytes:
        return hashlib.pbkdf2_hmac("sha256", pin.encode(), salt, self.ITER)

    def _matches(self, pin: str) -> bool:
        if self.custom:
            salt, digest = self.custom
            return hmac.compare_digest(self._hash(pin, salt), digest)
        return bool(self.pin) and hmac.compare_digest(pin.encode(), self.pin.encode())

    def _sign_key(self) -> bytes:
        return hashlib.sha256(self.key + (self.custom[1] if self.custom else b"")).digest()

    def token(self) -> str:
        exp = str(int(time.time()) + COOKIE_DAYS * 86400).encode()
        sig = hmac.new(self._sign_key(), exp, hashlib.sha256).hexdigest()
        return base64.urlsafe_b64encode(exp).decode() + "." + sig

    def valid(self, cookie_header: str) -> bool:
        if not self.enabled:
            return True
        m = re.search(rf"(?:^|;\s*){COOKIE}=([^;]+)", cookie_header or "")
        if not m:
            return False
        try:
            raw, sig = m.group(1).split(".", 1)
            exp = base64.urlsafe_b64decode(raw.encode())
            good = hmac.new(self._sign_key(), exp, hashlib.sha256).hexdigest()
            return hmac.compare_digest(sig, good) and int(exp) > time.time()
        except (ValueError, TypeError):
            return False

    def login(self, pin: str) -> str:
        """'ok', 'bad' или 'locked'."""
        with self._lock:
            now = time.time()
            self.fails = [t for t in self.fails if now - t < 600]
            if len(self.fails) >= 5:
                return "locked"
            if self.enabled and self._matches(pin):
                return "ok"
            self.fails.append(now)
            return "bad"

    def set_pin(self, new: str) -> None:
        salt = os.urandom(16)
        data = (salt.hex() + "$" + self._hash(new, salt).hex()).encode()
        fd = os.open(self.pinfile, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        self.custom = self._load()


class WebCoach:
    def __init__(self, make_cfg: Callable[[], Config], connect: Callable[[Config], Ollama]):
        self.make_cfg, self.connect = make_cfg, connect
        cfg = make_cfg()
        self.primary = cfg.ollama
        llm = connect(cfg)
        if not cfg.db_path.exists():
            raise SystemExit(trl("База ещё не построена. Сначала: python3 coach.py update"))
        self.coach = Coach(cfg, llm)
        stored = store.get_meta(self.coach.conn, "embed_model")
        if stored and stored != cfg.embed_model:
            raise SystemExit(trl("База построена с эмбеддинг-моделью {0}, а выбрана {1}. Запусти python3 coach.py update.", stored, cfg.embed_model))
        self.lock = threading.Lock()          # один разговор за раз: коуч хранит одну сессию
        self.focus: bodydouble.BodyDouble | None = None
        self.ask_outcome: tuple[int, float] | None = None   # (id сессии фокуса, когда спросили «как прошло»)
        self.subs: list[queue.Queue] = []
        self.last_activity = time.monotonic()
        self.last_seen = time.monotonic()
        self.stop = threading.Event()
        threading.Thread(target=self._watch, daemon=True).start()

    # --- состояние ---------------------------------------------------------------------
    @property
    def cfg(self) -> Config:
        return self.coach.cfg

    def state(self) -> dict[str, Any]:
        c = self.coach
        last = c.last_session
        deals = commitments.open_list(c.conn)
        turns = [t for t in c.turns if not t["content"].startswith("[")][-40:]
        return {
            "fallback": bool(self.cfg.on_fallback), "think": bool(self.cfg.think_chat), "model": self.cfg.model,
            "busy": self.lock.locked(),
            "focus": ({"task": self.focus.task, "typed": self.focus.typed, "minutes": self.focus.elapsed_min(),
                       "seconds": self.focus.elapsed_sec(), "planned": self.focus.planned,
                       "item": self.focus.item_id} if self.focus else None),
            "lr_active": self._lr_active(), "lr_wait": triageweb.waiting(c.conn),
            "lang": i18n.LANG, "lang_locked": i18n.locked(),
            "last_session": ({"date": last["started_at"][:10], "title": last["title"] or ""} if last else None),
            "pending": pending_count(c.conn), "agreements": len(deals),
            "late": sum(1 for d in deals if d["due"] and d["due"] < today()),
            "people": len(people.queue(c.conn)), "turns": turns,
        }

    def _lr_active(self) -> list[dict[str, Any]]:
        """Активные пункты Life Reset – чтобы начать фокус на одном из них; минуты – за 30 дней."""
        mins = bodydouble.item_minutes(self.coach.conn)
        rows = self.coach.conn.execute("SELECT id, title FROM lr_items WHERE status='active' AND merged_into IS NULL "
                                       "ORDER BY decided_at DESC LIMIT 12").fetchall()
        return [{"id": r["id"], "title": r["title"], "min30": mins.get(r["id"], 0)} for r in rows]

    def broadcast(self, ev: dict[str, Any]) -> None:
        for q in list(self.subs):
            q.put(ev)

    # --- связь с моделью ---------------------------------------------------------------
    def _reconnect(self) -> None:
        old = self.coach
        fresh = self.make_cfg()
        try:
            llm = self.connect(fresh)
        except SystemExit as exc:
            print(trl("[web] модель не найдена: {0}", exc), flush=True)
            return
        if not fresh.on_fallback:
            fresh.think_chat = old.cfg.think_chat
        old.cfg, old.llm = fresh, llm
        old.retriever.cfg, old.retriever.llm = fresh, llm

    def _llm_changed(self) -> bool:
        """Без блокировки (проверки сети долгие): ПК выключился — нужно на сервер; включился — назад на ПК
        (свой порт ПК проверяется первым: port_candidates его не включает)."""
        cfg = self.cfg
        if not answers(cfg.ollama, timeout=1.5):
            return True
        return bool(cfg.on_fallback and any(answers(u, timeout=1.0) for u in [self.primary, *port_candidates(self.primary, cfg.ollama_ports)]))

    def _watch(self) -> None:
        while not self.stop.wait(WATCH_EVERY):
            try:
                need = self._llm_changed()
            except Exception:  # noqa: BLE001
                need = False
            if not self.lock.acquire(blocking=False):
                continue     # идёт разговор: проверим в следующий раз
            try:
                if need:
                    self._reconnect()
                now = time.monotonic()
                if self.focus and now - self.last_seen > FOCUS_ORPHAN:
                    self._focus_off(announce=False)
                if self.coach.turns and now - self.last_activity > IDLE_END_SESSION:
                    self._end_session()
            except Exception as exc:  # noqa: BLE001 — сторож не должен падать
                print(trl("[web] сторож: {0}", type(exc).__name__), flush=True)
            finally:
                self.lock.release()

    def _end_session(self) -> None:
        self.coach.end_session()
        self.broadcast({"ev": "state", "v": "idle"})

    # --- режим присутствия -------------------------------------------------------------
    def _say(self, line: str) -> None:
        self.broadcast({"ev": "say", "t": line})

    def _focus_on(self, arg: str) -> str:
        self.coach._wait_bg()
        self.ask_outcome = None
        task, item_id, title, planned = bodydouble.parse_start(self.coach.conn, arg)
        fo = bodydouble.BodyDouble(self.coach.llm, self.cfg, task, self._say, lambda: not self.lock.locked(),
                                   item_id=item_id, item_title=title, planned=planned)
        fo.on_plan = lambda: threading.Thread(target=self._plan_done, args=(fo,), daemon=True).start()
        self.focus = fo
        fo.start()
        self.broadcast({"ev": "state", "v": "focus"})
        return bodydouble.start_text(planned)

    def _plan_done(self, fo: bodydouble.BodyDouble) -> None:
        """Время по плану вышло: Базилий просыпается, потягивается и спрашивает, как прошло."""
        with self.lock:
            if self.focus is not fo:
                return
            text = self._focus_off()
        self.broadcast({"ev": "say", "t": text})

    def _focus_off(self, announce: bool = True) -> str:
        fo = self.focus
        if fo is None:
            return ""
        self.focus = None
        fo.stop()
        minutes = bodydouble.record(self.coach.conn, self.coach._ensure_session(), fo)
        self.ask_outcome = (fo.row_id, time.monotonic()) if minutes and fo.row_id else None
        if minutes:
            self.coach.turns.append({"role": "user", "content": trl("[Работал в режиме присутствия: {0}, {1}]", fo.task or trl("без названия"), bodydouble._minutes(minutes))})
        self.broadcast({"ev": "state", "v": "stretch" if announce else "idle"})
        return bodydouble.end_text(fo, minutes)

    # --- сообщение ---------------------------------------------------------------------
    def handle(self, text: str, out: Callable[[dict[str, Any]], None]) -> None:
        """Одно сообщение человека. out — запись события (NDJSON) клиенту."""
        text = text.strip()
        if not text:
            return
        self.last_seen = self.last_activity = time.monotonic()
        if not self.lock.acquire(blocking=False):
            out({"ev": "system", "t": trl("Коуч ещё отвечает. Подожди секунду.")})
            return
        try:
            self._handle_locked(text, out)
        finally:
            self.lock.release()
            out({"ev": "done"})

    def _capture(self, fn: Callable[[], None]) -> str:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            fn()
        return buf.getvalue().strip()

    def _handle_locked(self, text: str, out: Callable[[dict[str, Any]], None]) -> None:
        c, cfg = self.coach, self.cfg
        cmd = text.split(maxsplit=1)
        head = cmd[0].casefold()
        arg = cmd[1].strip() if len(cmd) > 1 else ""
        asked, self.ask_outcome = self.ask_outcome, None
        if (asked and self.focus is None and time.monotonic() - asked[1] < bodydouble.OUTCOME_WAIT
                and bodydouble.outcome_reply(text)):
            bodydouble.save_outcome(c.conn, asked[0], text)   # и дальше – обычный ответ коуча
        if head == "/focus" or (self.focus is not None and bodydouble.STOP.match(text)):
            if self.focus is not None:
                out({"ev": "system", "t": self._focus_off()})
                out({"ev": "state", "v": "stretch"})
            else:
                out({"ev": "system", "t": self._focus_on(arg)})
            return
        if self.focus is None and bodydouble.START.search(text) and not guard.text_hold(text):
            out({"ev": "system", "t": self._focus_on(bodydouble.START.sub("", text).strip(" ,.;:—-!"))})
            return
        if head == "/help":
            out({"ev": "system", "t": HELP})
            return
        if head in ("/triage", "/разбор"):
            word = arg.casefold()
            area = inventory.resolve_area(arg) if arg and word not in ("быстро", "quick") else None
            if arg and word not in ("быстро", "quick") and not area:
                out({"ev": "system", "t": trl("Не нашёл такую область. Напиши /triage без слов или с названием, например /triage финансы.")})
                return
            out({"ev": "open", "v": "triage", "area": area or ""})
            return
        if head in TERMINAL_ONLY:
            out({"ev": "system", "t": trl("Это пока только в терминале: python3 coach.py chat (там ") + head + ")."})
            return
        if head == "/new":
            out({"ev": "state", "v": "think"})
            self._end_session()
            out({"ev": "reset"})
            out({"ev": "system", "t": trl("Новая сессия.")})
            return
        if head == "/sources":
            out({"ev": "system", "t": "\n".join(dict.fromkeys(c.last_refs)) or "—"})
            return
        if head in ("/think", "/подумай", "/думай"):
            if cfg.on_fallback:
                out({"ev": "system", "t": trl("Сейчас модель на процессоре сервера (ПК выключен): с рассуждением ответ шёл бы десятки минут, поэтому оно выключено.")})
                return
            if think_command(cfg, arg) == "once":
                c.think_once = True
                text = arg
                head = ""
            if head:
                out({"ev": "system", "t": trl("Рассуждение ") + (trl("включено: ответы дольше, но вдумчивее.") if cfg.think_chat else trl("выключено."))})
                return
        if head in ("/bio", "/био", "/history", "/история") or (self.focus is None and chronology.ASK.search(text)
                                                           and not head.startswith("/")):
            out({"ev": "system", "t": self._capture(lambda: c.show_history(arg if head.startswith("/") and arg else text))})
            return
        if head in ("/reset", "/ресет"):
            out({"ev": "system", "t": self._capture(lambda: lifereset.dashboard(c.conn))})
            return
        if head in ("/idea", "/идея"):
            same = inventory.find_same_title(c.conn, arg) if arg else None
            if same:
                out({"ev": "system", "t": trl("Такая уже есть: {0}.", inventory.code(same))})
                return
            iid = inventory.capture(c.conn, c.llm, arg, check_dup=False) if arg else None
            out({"ev": "system", "t": trl("Записал в реестр как {0} – «не разобрано». Решение – при разборе (python3 coach.py triage).", inventory.code(iid)) if iid else trl("Напиши идею после команды: /idea текст")})
            return
        if head in ("/depth", "/глубина"):
            if arg.isdigit() and lifereset.set_depth(c.conn, int(arg)):
                out({"ev": "system", "t": trl("Глубина разговора: {0} – {1}.", arg, lifereset.DEPTHS[int(arg)][0])})
            else:
                d = lifereset.depth(c.conn)
                out({"ev": "system", "t": trl("Глубина сейчас: {0} – {1}. Сменить: /depth 1, 2 или 3.", d, lifereset.DEPTHS[d][0])})
            return
        if head.startswith("/"):
            out({"ev": "system", "t": trl("Такой команды здесь нет. /help — что есть.")})
            return
        if guard.text_hold(text):
            out({"ev": "system", "t": trl("Сообщение не отправлено: похоже на пароль или токен. Не вводи их сюда.")})
            return
        m = _REMEMBER.match(text)
        if m:
            note = self._capture(lambda: c.remember(remember_text(m)))
            if note:
                out({"ev": "system", "t": note})
        self._reply(text, out)

    def _reply(self, text: str, out: Callable[[dict[str, Any]], None]) -> None:
        c = self.coach
        first = [True]

        def sink(kind: str, piece: str) -> None:
            if kind == "start":
                out({"ev": "state", "v": "think"})
            elif kind == "token":
                if first[0]:
                    first[0] = False
                    out({"ev": "state", "v": "speak"})
                out({"ev": "token", "t": piece})
            elif kind == "note":
                out({"ev": "note", "t": piece.strip()})

        c.sink = sink
        try:
            c.reply(text, focus_task=self.focus.task if self.focus else None)
        except (OllamaError, OSError) as exc:
            out({"ev": "system", "t": trl("Модель не ответила ({0}). Проверю связь и попробуем ещё раз.", type(exc).__name__)})
            self._reconnect()
        finally:
            c.sink = None
            out({"ev": "state", "v": "focus" if self.focus else "idle"})


class Handler(BaseHTTPRequestHandler):
    server_version = "LocalCoachWeb"
    wc: WebCoach
    auth: Auth
    restart = staticmethod(_restart_soon)     # replaced in tests

    def log_message(self, *args: Any) -> None:    # без журнала запросов: ни путей, ни текстов
        pass

    # --- вспомогательное ---
    def _send(self, code: int, body: bytes, ctype: str, extra: dict[str, str] | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if "Cache-Control" not in (extra or {}):
            self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj: Any, extra: dict[str, str] | None = None) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8", extra)

    def _authed(self) -> bool:
        return self.auth.valid(self.headers.get("Cookie", ""))

    def _body(self) -> dict[str, Any] | None:
        if "application/json" not in self.headers.get("Content-Type", ""):
            return None                                    # простая форма с чужого сайта сюда не пройдёт
        try:
            n = int(self.headers.get("Content-Length", "0"))
            if n < 0 or n > MAX_BODY:
                return None
            data = json.loads(self.rfile.read(n) or b"{}")
            return data if isinstance(data, dict) else None
        except (ValueError, OSError):
            return None

    def _stream_start(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

    def _write(self, ev: dict[str, Any]) -> bool:
        try:
            self.wfile.write((json.dumps(ev, ensure_ascii=False) + "\n").encode())
            self.wfile.flush()
            return True
        except OSError:
            return False

    # --- маршруты ---
    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/":
            self._send(200, _page_bytes(), "text/html; charset=utf-8")
        elif path == "/api/state":
            if not self._authed():
                self._json(200, {"auth": False, "pin": self.auth.enabled})
                return
            self.wc.last_seen = time.monotonic()
            self._json(200, {"auth": True, "pin": self.auth.enabled, "pin_temp": self.auth.needs_change, **self.wc.state()})
        elif path in ("/api/triage", "/api/triage/card", "/api/triage/find"):
            if not self._authed():
                self._json(401, {"error": "auth"})
                return
            self._triage_get(path)
        elif path == "/api/events":
            if not self._authed():
                self._json(401, {"error": "auth"})
                return
            self._events()
        elif path.startswith("/audio/"):
            f = AUDIO / path[len("/audio/"):]
            if not self._authed():
                self._json(401, {"error": "auth"})
            elif f.parent != AUDIO or f.suffix not in AUDIO_TYPES or not f.is_file():
                self._json(404, {"error": "not found"})
            else:
                self._send(200, f.read_bytes(), AUDIO_TYPES[f.suffix], {"Cache-Control": "private, max-age=86400"})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]
        body = self._body()
        if body is None:
            self._json(400, {"error": "bad request"})
            return
        if path == "/api/login":
            res = self.auth.login(str(body.get("pin", "")))
            if res == "ok":
                cookie = f"{COOKIE}={self.auth.token()}; Path=/; HttpOnly; SameSite=Strict; Max-Age={COOKIE_DAYS * 86400}"
                self._json(200, {"ok": True}, {"Set-Cookie": cookie})
            else:
                time.sleep(FAIL_DELAY)
                self._json(429 if res == "locked" else 403, {"error": res})
            return
        if path == "/api/pin":
            if not self._authed() or not self.auth.enabled:
                self._json(401, {"error": "auth"})
                return
            new = str(body.get("new", "")).strip()
            if not 4 <= len(new) <= 64:
                self._json(400, {"error": "short"})
                return
            if self.auth.pin and hmac.compare_digest(new.encode(), self.auth.pin.encode()):
                self._json(400, {"error": "same"})       # новый не должен совпадать с временным
                return
            res = self.auth.login(str(body.get("old", "")))
            if res != "ok":
                time.sleep(FAIL_DELAY)
                self._json(429 if res == "locked" else 403, {"error": res})
                return
            self.auth.set_pin(new)
            cookie = f"{COOKIE}={self.auth.token()}; Path=/; HttpOnly; SameSite=Strict; Max-Age={COOKIE_DAYS * 86400}"
            self._json(200, {"ok": True}, {"Set-Cookie": cookie})
            return
        if path == "/api/lang":
            if not self._authed():
                self._json(401, {"error": "auth"})
                return
            code = str(body.get("lang", "")).strip().lower()
            if code not in i18n.LANGS:
                self._json(400, {"error": "lang"})
                return
            if i18n.locked():
                self._json(409, {"error": "locked"})      # LOCAL_COACH_LANG is set: the environment wins
                return
            i18n.save_lang(self.wc.cfg.workspace, code)
            self._json(200, {"ok": True, "lang": code, "restart": bool(code != i18n.LANG and self.restart())})
            return
        if path == "/api/logout":
            self._json(200, {"ok": True}, {"Set-Cookie": f"{COOKIE}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"})
            return
        if not self._authed():
            self._json(401, {"error": "auth"})
            return
        if path.startswith("/api/triage/"):
            self._triage_post(path[len("/api/triage/"):], body)
            return
        if path == "/api/chat":
            self._stream_start()
            self.wc.handle(str(body.get("text", "")), self._write)
        else:
            self._json(404, {"error": "not found"})

    def _triage_get(self, path: str) -> None:
        q = parse_qs(urlsplit(self.path).query)
        one = lambda k: (q.get(k) or [""])[0]            # noqa: E731
        conn = self.wc.coach.conn
        if path == "/api/triage/card":
            self._json(200, triageweb.card(conn, int(one("id")) if one("id").isdigit() else 0))
            return
        if path == "/api/triage/find":
            self._json(200, {"items": triageweb.find(conn, one("q"), int(one("id")) if one("id").isdigit() else 0)})
            return
        ids = [int(x) for x in one("exclude").split(",") if x.isdigit()]
        self._json(200, triageweb.list_page(conn, one("area"), ids))

    def _triage_post(self, action: str, body: dict[str, Any]) -> None:
        wc, conn = self.wc, self.wc.coach.conn
        try:
            iid = int(body.get("id") or 0)
        except (TypeError, ValueError):
            iid = 0
        text = lambda k: str(body.get(k) or "").strip()  # noqa: E731
        if action == "decide":
            res = triageweb.decide(conn, iid, text("status"), step=text("step"), reason=text("reason"),
                                   answer=text("answer"), proposal=text("proposal"), review=text("review"),
                                   months=_int(body.get("months")), force=bool(body.get("force")), skip_capacity=bool(body.get("skip_capacity")))
        elif action == "undo":
            res = triageweb.undo(conn, body.get("undo") or "")
        elif action == "dedupe":
            res = triageweb.dedupe(conn)
        elif action == "merge":
            res = triageweb.merge(conn, iid, body.get("into"))
        elif action == "area":
            res = triageweb.set_area(conn, iid, text("area"))
        elif action == "capacity":
            res = triageweb.set_capacity(conn, body.get("n"))
        elif action == "say":
            # Модель занята разговором или фокусом-репликой – не вставать в очередь, пусть человек нажмёт ещё раз.
            if not wc.lock.acquire(blocking=False):
                self._json(200, {"busy": True})
                return
            try:
                res = triageweb.say(conn, wc.coach.llm, iid, text("text"))
            except OllamaError:
                res = {"error": "model"}
            finally:
                wc.lock.release()
        else:
            self._json(404, {"error": "not found"})
            return
        self._json(200, res)

    def _events(self) -> None:
        q: queue.Queue = queue.Queue()
        self.wc.subs.append(q)
        self._stream_start()
        try:
            while True:
                try:
                    ev = q.get(timeout=15)
                except queue.Empty:
                    ev = {"ev": "ping"}
                self.wc.last_seen = time.monotonic()
                if not self._write(ev):
                    break
        finally:
            with contextlib.suppress(ValueError):
                self.wc.subs.remove(q)


def _int(v: Any) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def serve(make_cfg: Callable[[], Config], connect: Callable[[Config], Ollama], host: str, port: int) -> int:
    pin = os.environ.get("LOCAL_COACH_WEB_PIN", "").strip()
    own = (make_cfg().workspace / ".web_pin").exists()
    if not pin and not own and host not in ("127.0.0.1", "localhost", "::1"):
        raise SystemExit(trl("Без PIN-кода коуч слушает только localhost. Задай LOCAL_COACH_WEB_PIN (например в файле окружения юнита) или --host 127.0.0.1."))
    if pin and len(pin) < 4:
        raise SystemExit(trl("LOCAL_COACH_WEB_PIN короче 4 символов."))
    wc = WebCoach(make_cfg, connect)
    Handler.wc, Handler.auth = wc, Auth(wc.cfg.workspace, pin)
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True

    def stop_gracefully(*_args: Any) -> None:    # systemd stop / restart: итог сессии успеет сохраниться
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop_gracefully)
    print(trl("Local Coach web: http://{0}:{1}/  (PIN {2}; модель: {3})", host, port, trl("свой") if own else trl("временный, при первом входе предложит свой") if pin else trl("не задан — только localhost"), trl("сервер, процессор") if wc.cfg.on_fallback else wc.cfg.ollama), flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        wc.stop.set()
        with wc.lock:
            wc._focus_off(announce=False)
            wc.coach.end_session()
        httpd.server_close()
    return 0

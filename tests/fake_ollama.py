"""Заглушка Ollama для тестов: детерминированные ответы по JSON-схеме каждого вызова.

Не проверяет качество модели — только то, что код правильно разбирает ответы,
отбрасывает неподтверждённое и проходит все пути. Запускается в потоке теста.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LAST_CHAT: list[str] = []   # системные промпты ответов коуча по порядку — для проверок в тестах
LAST_USER: list[str] = []   # и последние сообщения (с блоком памяти)
YEAR = re.compile(r"(?<!\d)(19[5-9]\d|20[0-4]\d)(?!\d)")


def vec(text: str) -> list[float]:
    """Мешок основ слов: одинаковые формулировки — одинаковый вектор, похожие — близкие."""
    v = [0.0] * 768
    for w in re.findall(r"\w+", text.lower()):
        v[int(hashlib.md5(w[:5].encode()).hexdigest(), 16) % 768] += 1.0
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def fill(schema: dict) -> object:
    if "enum" in schema:
        return schema["enum"][0]
    t = schema.get("type")
    if t == "object":
        return {k: fill(s) for k, s in schema["properties"].items()}
    if t == "array":
        return []
    return "" if t == "string" else None


def extract(user: str) -> dict:
    src = user.split("<<<BEGIN_SOURCE>>>\n", 1)[1].split("\n<<<END_SOURCE>>>")[0]
    # Как настоящая модель: без года в строке берёт год из даты изменения файла.
    mtime = re.search(r"последний раз изменён (\d{4})", user)
    facts = []
    for line in src.splitlines():
        words = line.lstrip("-# ").split()
        if len(words) < 4:
            continue
        year = YEAR.search(line)
        time_ref = year.group(1) if year else (mtime.group(1) if mtime else "")
        low = line.casefold()
        # Для Life Reset: «хочу» – цель, «надо» – незакрытое дело, «бросил» – отказ, «сделал» – готово.
        kind = "goal" if "хочу" in low else "open_loop" if "надо" in low else "fact"
        intent = ("abandoned" if "бросил" in low else "completed" if "сделал" in low else "wanted") \
            if kind != "fact" else "none"
        facts.append({"claim": "Автор: " + " ".join(words)[:120], "kind": kind,
                      "domain": "projects_creative" if kind != "fact" else "work_career",
                      "subject": "self", "explicitness": "explicit", "intent_status": intent,
                      "time_ref": time_ref, "evidence_quote": " ".join(words[:6]),
                      "sensitivity": "normal"})
    # Выдуманная цитата: код должен её не принять как опору.
    facts.append({"claim": "Автор любит тёплую погоду", "kind": "preference", "domain": "hobbies_interests",
                  "subject": "self", "explicitness": "explicit", "intent_status": "none", "time_ref": "",
                  "evidence_quote": "цитаты такой в тексте нет", "sensitivity": "normal"})
    hyps = []
    if "опять" in src.casefold():
        quote = next(l for l in src.splitlines() if "опять" in l.casefold()).strip()
        hyps.append({"hypothesis": "Автор бросает проекты, когда пропадает новизна", "evidence_quote": quote[:80],
                     "alternative": "не хватало времени"})
    return {"summary": "Фрагмент.", "facts": facts[:15], "hypotheses": hyps}


def timeline(user: str) -> dict:
    """Каждое утверждение — «в прошлом» со ссылкой на первую строку хронологии; A1 — без ссылки
    (код должен превратить это в unknown)."""
    refs = re.findall(r"^([TU]\d+) \|", user, re.M)
    items = []
    for a in re.findall(r"^(A\d+) \|", user, re.M):
        items.append({"id": a, "verdict": "past", "until": "2017", "period": "2010-2017",
                      "ids": [] if a == "A1" else refs[:1]})
    return {"items": items}


def session(user: str) -> dict:
    out = {"title": "Тест", "summary": "Поговорили.", "commitments": [], "updates": [], "revisit": []}
    open_ids = re.findall(r"^(D\d+):", user, re.M)
    talk = user.split("Разговор:", 1)[-1]
    if "Решил" in talk:
        out["commitments"].append({"text": "Сходить на пробежку", "due": "2026-11-02"})
        out["commitments"].append({"text": "Сходить на пробежку снова", "due": ""})  # почти дубль
    if "Сделал" in talk and open_ids:
        out["updates"].append({"id": open_ids[0], "status": "done", "note": "сделал"})
    out["updates"].append({"id": "D999999", "status": "done", "note": "нет такой"})
    return out


def people(user: str) -> dict:
    """Заглавные слова (кроме «Автор») — люди; «Имярек» — выдуманный, его в тексте нет: код должен отбросить."""
    items = []
    for fid, claim in re.findall(r"^(F\d+) \([^)]*\): (.*)$", user, re.M):
        body = claim.split(":", 1)[-1].split(None, 1)[-1]   # без «Автор:» и первого слова фразы
        names = re.findall(r"\b[А-ЯЁ][а-яё]{2,}", body)
        found = [{"name": re.sub(r"(ей|ой|е|и|у|ы)$", "а", n), "relation": "сестра"} for n in names]
        items.append({"id": fid, "people": found + [{"name": "Имярек", "relation": ""}]})
    return {"items": items}


def checkin(user: str) -> dict:
    answers = user.split("Ответы:", 1)[-1]
    facts = [{"claim": "Автор сейчас " + line.split(":", 1)[1].strip(), "kind": "fact", "domain": "work_career"}
             for line in answers.strip().splitlines() if ":" in line and len(line.split(":", 1)[1].strip()) > 3]
    return {"facts": facts}


def lr_groups(user: str) -> dict:
    """Название – первые слова первого утверждения группы; группа про Казань – «не пункт»;
    G2 объявляется той же темой, что G1, если в обеих есть «сайт»."""
    items = []
    blocks = re.split(r"\n\n(?=G\d+ \|)", user.split("Группы:\n\n", 1)[1])
    firsts = {}
    for b in blocks:
        gid = b.split(" |", 1)[0].strip()
        claim = re.search(r"\) Автор: (.*?) \[", b)
        text = claim.group(1) if claim else gid
        firsts[gid] = text
        same = next((g for g, t in firsts.items() if g != gid and "сайт" in t.casefold() and "сайт" in text.casefold()), "")
        items.append({"id": gid, "title": " ".join(text.split()[:5]), "type": "project" if "сайт" in text.casefold() else "wish",
                      "summary": text, "want": "", "same_as": same, "not_item": "Казан" in text})
    return {"items": items}


def lr_answer(user: str) -> dict:
    answer = user.rsplit("Ответ человека:\n", 1)[-1].casefold()
    if "не хочу" in answer or "отпуска" in answer:
        return {"status": "abandoned", "reason": "перегорел", "next_step": "", "review_on": ""}
    if "когда-нибудь" in answer:
        return {"status": "someday", "reason": "", "next_step": "", "review_on": "через год"}
    if "займусь" in answer:
        return {"status": "active", "reason": "", "next_step": "купить домен", "review_on": ""}
    return {"status": "", "reason": "", "next_step": "", "review_on": ""}


def lr_hypotheses(user: str) -> dict:
    return {"items": [{"id": h, "text": "Возможно, тебе труднее продолжать, когда пропадает новизна.",
                       "alternatives": ["не хватало времени", "не было ясного шага"],
                       "question": "Что обычно происходит, когда проект перестаёт быть новым?", "same_as": ""}
                      for h in re.findall(r"^(H\d+):", user, re.M)]}


def respond(payload: dict) -> dict:
    system = payload["messages"][0]["content"]
    user = payload["messages"][-1]["content"]
    if system.startswith("Ты извлекаешь"):
        return extract(user)
    if system.startswith("Ты помогаешь человеку разобрать"):
        return lr_groups(user)
    if system.startswith("Человек разбирает накопленное"):
        return lr_answer(user)
    if system.startswith("Даны осторожные предположения"):
        return lr_hypotheses(user)
    if system.startswith("Выбери самое значимое"):
        # Как модель, которая берёт с конца списка: проверяет, что код не полагается на порядок.
        k = payload["format"]["properties"]["chosen"]["maxItems"]
        ids = re.findall(r"^(S\d+) \|", user, re.M)
        return {"chosen": list(reversed(ids))[:k] + ["S999"]}
    if system.startswith("Ты помогаешь понять, что из старых заметок"):
        return timeline(user)
    if system.startswith("Ты подводишь итог"):
        return session(user)
    if system.startswith("Даны утверждения из заметок"):
        return people(user)
    if system.startswith("Человек ответил на короткий чек-ин"):
        return checkin(user)
    if system.startswith("Дан короткий портрет"):
        # Как послушная модель: настоящее время и имя из входа.
        text = user.split("ПОРТРЕТ:\n", 1)[1]
        name = re.search(r"Имя: ([А-ЯЁ][а-яё]+)\.", user)
        text = text.replace("работал", "работает").replace("воспитывал", "растит")
        return {"portrait": re.sub(r"^[А-ЯЁ][а-яё]+", name.group(1) if name else "Он", text)}
    if system.startswith("Человеку показали утверждение"):
        answer = user.rsplit("Ответ:", 1)[-1]
        years = YEAR.findall(answer)
        return {"verdict": "past", "until": years[-1] if years else "", "period": "",
                "now_claim": "Автор сейчас работает удалённо" if "Сейчас" in answer else ""}
    out = fill(payload["format"]) if isinstance(payload.get("format"), dict) else {}
    ids = re.findall(r"\b([CH]\d+)\b", user)
    if isinstance(out, dict) and "portrait" in out:
        out["portrait"] = "Портрет."
        out["core_facts"] = [{"text": "пункт", "ids": ids[:1] + ["C999999"]}]
        out["current_focus"] = [{"text": "фокус", "ids": ids[:1]}]
    if isinstance(out, dict) and "key_points" in out:
        out["overview"] = "Обзор."
        out["key_points"] = [{"text": "главное", "ids": ids[:2]}]
    if isinstance(out, dict) and "items" in out:
        out["items"] = [{"id": p, "verdict": "ok", "note": "", "safer_text": "", "ask_user": ""}
                        for p in re.findall(r"\b(P\d+)\b", user)]
    return out


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:
        pass

    def _send(self, obj: dict) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        self._send({"models": [{"name": "qwen3.5:9b-q4_K_M"}, {"name": "embeddinggemma:latest"},
                               {"name": "qwen3.5:397b-cloud", "remote_host": "https://ollama.com:443"},
                               {"name": "qwen3.5:27b-remote"}]})

    def do_POST(self) -> None:
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if payload.get("stream"):
            LAST_CHAT.append(payload["messages"][0]["content"])
            LAST_USER.append(payload["messages"][-1]["content"])
        if self.path == "/api/show":
            # Облачная модель без признака в имени: узнаётся только по /api/show.
            return self._send({"remote_model": "qwen3.5:27b"} if payload["model"] == "qwen3.5:27b-remote" else {})
        if self.path == "/api/embed":
            return self._send({"embeddings": [vec(t) for t in payload["input"]]})
        if payload.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.end_headers()
            # С рассуждением — как модель, которая думает до конца лимита и не доходит до ответа.
            if payload.get("think"):
                for _ in range(100):
                    self.wfile.write((json.dumps({"message": {"content": "", "thinking": "хм "}, "done": False},
                                                 ensure_ascii=False) + "\n").encode())
                self.wfile.write((json.dumps({"message": {"content": ""}, "done": True, "done_reason": "length",
                                              "prompt_eval_count": 100}) + "\n").encode())
                return
            for piece in ("Слышу ", "тебя."):
                self.wfile.write((json.dumps({"message": {"content": piece}, "done": False}, ensure_ascii=False)
                                  + "\n").encode())
            self.wfile.write((json.dumps({"message": {"content": ""}, "done": True, "done_reason": "stop",
                                          "prompt_eval_count": 100}) + "\n").encode())
            return
        self._send({"message": {"content": json.dumps(respond(payload), ensure_ascii=False)}, "done": True,
                    "done_reason": "stop", "prompt_eval_count": 100, "eval_count": 50})


def start() -> tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"

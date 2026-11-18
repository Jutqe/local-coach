"""Клиент локального Ollama: чат (JSON по схеме или потоковый текст) и эмбеддинги."""
from __future__ import annotations

import http.client
import ipaddress
import json
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Iterator

from .config import Config


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    # Перенаправление увело бы заметки на другой адрес — не следуем.
    def redirect_request(self, *_args, **_kwargs):
        raise OllamaError("Ollama ответил перенаправлением на другой адрес — запрос не отправлен")


# Локальный трафик не должен уходить через прокси из окружения.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


_TAILSCALE = ipaddress.ip_network("100.64.0.0/10")


def check_local_url(url: str) -> str | None:
    """Причина отказа, если адрес Ollama не localhost и не домашняя сеть; иначе None."""
    try:
        parts = urllib.parse.urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            return "нужен адрес вида http://localhost:11434"
        infos = socket.getaddrinfo(parts.hostname, parts.port or 11434, proto=socket.IPPROTO_TCP)
    except (ValueError, OSError):
        return f"не удалось разобрать адрес {url}"
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if not (ip.is_loopback or ip.is_private or ip.is_link_local or ip in _TAILSCALE):
            return f"{parts.hostname} — адрес вне домашней сети ({ip})"
    return None


def answers(url: str, timeout: float = 2.0) -> bool:
    """Отвечает ли по адресу Ollama — быстрая проверка, без загрузки модели."""
    try:
        with _OPENER.open(url.rstrip("/") + "/api/tags", timeout=timeout) as resp:
            return "models" in json.loads(resp.read().decode("utf-8") or "{}")
    except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException):
        return False


def port_candidates(url: str, ports: str) -> list[str]:
    """Тот же адрес на других портах: «11434-11439» или «11434,11500»; сам url не входит."""
    parts = urllib.parse.urlsplit(url)
    nums: list[int] = []
    for piece in (ports or "").replace(" ", "").split(","):
        a, _, b = piece.partition("-")
        if a.isdigit():
            nums += range(int(a), int(b) + 1) if b.isdigit() else [int(a)]
    host = parts.hostname or ""
    host = f"[{host}]" if ":" in host else host
    out = [f"{parts.scheme}://{host}:{n}" for n in dict.fromkeys(nums) if n != (parts.port or 11434)]
    return out[:20]


def is_cloud(name: str, entry: dict[str, Any] | None = None) -> bool:
    """Облачные модели Ollama (…:cloud, …-cloud) отправляют запрос на ollama.com."""
    entry = entry or {}
    return (name.endswith((":cloud", "-cloud")) or "-cloud:" in name
            or bool(entry.get("remote_host") or entry.get("remote_model")))

# У qwen3.5 в Ollama по умолчанию presence_penalty 1.5: штраф за повтор токенов
# бьёт по кавычкам и ключам JSON. Для структурированных ответов отключаем.
JSON_OPTIONS = {"temperature": 0, "top_p": 1.0, "presence_penalty": 0.0, "repeat_penalty": 1.0}
# Высокий presence_penalty толкает модель к «новым» токенам — в русском тексте это
# английские вставки. Держим его низким.
CHAT_OPTIONS = {"temperature": 0.7, "top_p": 0.8, "top_k": 20, "presence_penalty": 0.3, "repeat_penalty": 1.05}


class OllamaError(RuntimeError):
    pass


class TruncatedError(OllamaError):
    """Ответ упёрся в num_predict: повтор того же запроса не поможет."""


class Ollama:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.base = cfg.ollama.rstrip("/")

    # --- транспорт ---------------------------------------------------------

    def _request(self, path: str, payload: dict[str, Any] | None, timeout: int):
        if payload is None:
            req = urllib.request.Request(self.base + path, method="GET")
        else:
            req = urllib.request.Request(
                self.base + path, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                headers={"Content-Type": "application/json"}, method="POST")
        return _OPENER.open(req, timeout=timeout)

    def _json(self, path: str, payload: dict[str, Any] | None, timeout: int) -> dict[str, Any]:
        with self._request(path, payload, timeout) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))

    # --- служебное -----------------------------------------------------------

    def models(self) -> dict[str, dict[str, Any]]:
        """Установленные модели: имя → запись из /api/tags."""
        data = self._json("/api/tags", None, timeout=10)
        return {str(m["name"]): m for m in data.get("models", []) if isinstance(m, dict) and m.get("name")}

    @staticmethod
    def resolve(wanted: str, names) -> str | None:
        # Только точное имя или с :latest: подбор по семейству молча подменил бы
        # qwen3.5:27b на qwen3.5:9b, а то и на облачную qwen3.5:cloud.
        return next((n for n in (wanted, wanted + ":latest") if n in names), None)

    def is_remote(self, name: str, entry: dict[str, Any] | None = None) -> bool:
        if is_cloud(name, entry):
            return True
        try:
            info = self._json("/api/show", {"model": name}, timeout=10)
        except (urllib.error.URLError, TimeoutError, ConnectionError, json.JSONDecodeError,
                http.client.HTTPException):
            return False
        return bool(info.get("remote_host") or info.get("remote_model"))

    # --- чат -----------------------------------------------------------------

    def chat_json(self, system: str, user: str, schema: dict[str, Any], *,
                  think: bool = False, num_predict: int = 3000, timeout: int = 900,
                  attempts: int = 3, model: str | None = None, heavy: bool = False) -> dict[str, Any]:
        """Запрос со структурированным выводом. TruncatedError — если ответ обрезан.
        heavy – вычисление, где важнее качество, чем отклик: идёт на тяжёлую модель, если она задана."""
        payload = {
            "model": model or (self.cfg.heavy_name if heavy else self.cfg.model), "stream": False, "think": think, "format": schema,
            "keep_alive": self.cfg.keep_alive,
            "options": {**JSON_OPTIONS, "num_ctx": self.cfg.num_ctx, "num_predict": num_predict},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        }
        last: Exception | None = None
        for attempt in range(attempts):
            try:
                data = self._json("/api/chat", payload, timeout)
            except (urllib.error.URLError, TimeoutError, ConnectionError, json.JSONDecodeError,
                    http.client.HTTPException) as exc:
                last = exc
                time.sleep(3 * (attempt + 1))
                continue
            if data.get("error"):
                raise OllamaError(str(data["error"])[:300])
            content = (data.get("message") or {}).get("content", "")
            if data.get("done_reason") == "length":
                raise TruncatedError(f"ответ обрезан на {data.get('eval_count')} токенах")
            try:
                parsed = json.loads(content)
            except json.JSONDecodeError as exc:
                last = exc
                time.sleep(2)
                continue
            if not isinstance(parsed, dict):
                last = ValueError("ожидался JSON-объект")
                continue
            self._check_context(data)
            return parsed
        raise OllamaError(f"нет корректного ответа после {attempts} попыток: {type(last).__name__}: {str(last)[:200]}")

    def chat_stream(self, messages: list[dict[str, str]], *, think: bool = False,
                    num_predict: int = 1500, timeout: int = 600,
                    on_think: Callable[[], None] | None = None) -> Iterator[str]:
        """Потоковый текстовый ответ. Отдаёт куски текста по мере генерации; on_think — на каждый кусок рассуждения."""
        payload = {
            "model": self.cfg.model, "stream": True, "think": think, "keep_alive": self.cfg.keep_alive,
            "options": {**CHAT_OPTIONS, "num_ctx": self.cfg.num_ctx, "num_predict": num_predict},
            "messages": messages,
        }
        with self._request("/api/chat", payload, timeout) as resp:
            for raw in resp:
                if not raw.strip():
                    continue
                try:
                    data = json.loads(raw.decode("utf-8", errors="replace"))
                except json.JSONDecodeError as exc:
                    raise OllamaError("поток ответа прервался") from exc
                if data.get("error"):
                    raise OllamaError(str(data["error"])[:300])
                if on_think and (data.get("message") or {}).get("thinking"):
                    on_think()
                piece = (data.get("message") or {}).get("content", "")
                if piece:
                    yield piece
                if data.get("done"):
                    self._check_context(data)
                    break

    def _check_context(self, data: dict[str, Any]) -> None:
        # Ollama молча обрезает начало запроса, если он не влез в num_ctx.
        used = int(data.get("prompt_eval_count") or 0)
        if used >= self.cfg.num_ctx - 16:
            print(f"  ПРЕДУПРЕЖДЕНИЕ: запрос занял весь контекст ({used} токенов) — начало могло быть обрезано",
                  flush=True)

    # --- эмбеддинги ----------------------------------------------------------

    def embed(self, texts: list[str], timeout: int = 300) -> list[list[float]]:
        payload = {"model": self.cfg.embed_model, "input": texts, "truncate": True,
                   "keep_alive": self.cfg.keep_alive}
        last: Exception | None = None
        for attempt in range(3):
            try:
                data = self._json("/api/embed", payload, timeout)
                vectors = data.get("embeddings") or []
                if len(vectors) != len(texts):
                    raise OllamaError(f"ожидалось {len(texts)} векторов, пришло {len(vectors)}")
                return [[float(x) for x in v] for v in vectors]
            except (urllib.error.URLError, TimeoutError, ConnectionError, json.JSONDecodeError,
                    http.client.HTTPException) as exc:
                last = exc
                time.sleep(3 * (attempt + 1))
        raise OllamaError(f"эмбеддинги недоступны: {last}")


# Префиксы задач, с которыми обучена embeddinggemma.
def embed_query_text(query: str) -> str:
    return "task: search result | query: " + query


def embed_doc_text(text: str, title: str = "none") -> str:
    return f"title: {title or 'none'} | text: {text}"

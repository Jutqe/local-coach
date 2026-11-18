"""Перевод веб-страницы на лету: страница одна (русская), в английском режиме строки подменяются при отдаче.

Берутся три вида русских строк: текст между тегами, значения атрибутов (title, placeholder, aria-label, alt, label)
и строковые литералы в <script>. Каталог – locales/en_web.json: {"html": {...}, "attr": {...}, "js": {...}},
ключ – русская строка точно как в исходнике, значение – перевод (в js – с теми же экранированиями кавычек).
Русский режим страницу не меняет.
"""
from __future__ import annotations

import json
import re

from .i18n import CATALOG_DIR, LANG

CYR = re.compile(r"[Ѐ-ӿ]")
_BLOCK = re.compile(r"(<script\b[^>]*>.*?</script>|<style\b[^>]*>.*?</style>)", re.S)
_TEXT = re.compile(r">([^<>]*[Ѐ-ӿ][^<>]*)<")
_ATTR = re.compile(r"\b((?:title|placeholder|aria-label|alt|label|value)=\")([^\"]*[Ѐ-ӿ][^\"]*)(\")")
_JS = re.compile(r"'((?:\\.|[^'\\\n])*)'|\"((?:\\.|[^\"\\\n])*)\"|`((?:\\.|[^`\\])*)`")


def extract(page: str) -> dict[str, list[str]]:
    """Русские строки страницы по видам (для подготовки каталога)."""
    out: dict[str, set[str]] = {"html": set(), "attr": set(), "js": set()}
    for part in _BLOCK.split(page):
        if part.startswith("<script"):
            for m in _JS.finditer(part):
                t = m.group(1) or m.group(2) or m.group(3)
                if t and CYR.search(t):
                    out["js"].add(t)
        elif part.startswith("<style"):
            continue
        else:
            out["html"].update(m.group(1).strip() for m in _TEXT.finditer(part))
            out["attr"].update(m.group(2) for m in _ATTR.finditer(part))
    return {k: sorted(v) for k, v in out.items()}


_catalog: dict[str, dict[str, str]] | None = None


def _load() -> dict[str, dict[str, str]]:
    global _catalog
    if _catalog is None:
        path = CATALOG_DIR / f"{LANG}_web.json"
        _catalog = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    return _catalog


def localize(page: str) -> str:
    """Страница на языке процесса. Нет перевода строки – остаётся русской (видно в проверке test_english_web)."""
    if LANG == "ru":
        return page
    cat = _load()
    html, attr, js = cat.get("html", {}), cat.get("attr", {}), cat.get("js", {})

    def js_sub(m: re.Match) -> str:
        for g in (1, 2, 3):
            if m.group(g) is not None:
                q = "'\"`"[g - 1]
                t = m.group(g)
                return q + js.get(t, t) + q if CYR.search(t) else m.group(0)
        return m.group(0)

    def text_sub(m: re.Match) -> str:
        raw = m.group(1)
        key = raw.strip()
        return ">" + raw.replace(key, html.get(key, key), 1) + "<"

    parts = []
    for part in _BLOCK.split(page):
        if part.startswith("<script"):
            part = _JS.sub(js_sub, part)
        elif part.startswith("<style"):
            pass
        else:
            part = _TEXT.sub(text_sub, part)
            part = _ATTR.sub(lambda m: m.group(1) + attr.get(m.group(2), m.group(2)) + m.group(3), part)
        parts.append(part)
    out = "".join(parts)
    return out.replace('<html lang="ru">', '<html lang="en">', 1)

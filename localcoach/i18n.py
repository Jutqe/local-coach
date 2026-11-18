"""Язык коуча: один код, язык задаёт переменная LOCAL_COACH_LANG (ru или en).

- `LANG` – язык процесса; выбирается один раз при запуске (смена языка = перезапуск): переменная LOCAL_COACH_LANG,
  иначе файл `lang` в рабочей папке (его пишет кнопка в веб-настройках), иначе DEFAULT_LANG.
- `trl("русская строка", *args)` – текст интерфейса и сообщений. Русская строка остаётся ключом: на ru она
  возвращается как есть, на en берётся перевод из `locales/en.json`. Параметры подставляются через `str.format`
  только если они переданы, так что фигурные скобки в обычных строках безопасны.
- Промпты для модели – целые наборы (`prompts.py` на ru, `prompts_en.py` на en), потому что это проза, а не строки.
- Нет перевода: на en возвращается русский текст, ключ запоминается в `MISSING` (тесты и `i18n_check.py`
  его показывают); с LOCAL_COACH_I18N_STRICT=1 вместо этого бросается KeyError.

В публичной копии `sync.py` ставит DEFAULT_LANG = "en".
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

LANGS = ("ru", "en")
DEFAULT_LANG = "en"
CATALOG_DIR = Path(__file__).resolve().parent / "locales"


LANG_FILE = "lang"


def _workspace() -> Path:
    """Working folder as Config will see it: --workspace argument, else the default (see defaults.py)."""
    argv = sys.argv
    for i, arg in enumerate(argv):
        if arg == "--workspace" and i + 1 < len(argv):
            return Path(argv[i + 1]).expanduser()
        if arg.startswith("--workspace="):
            return Path(arg.split("=", 1)[1]).expanduser()
    from .defaults import DEFAULT_WORKSPACE
    return DEFAULT_WORKSPACE


def saved_lang(workspace: Path | None = None) -> str:
    """Language saved by the web button in the working folder ('' if none)."""
    try:
        return ((workspace or _workspace()) / LANG_FILE).read_text(encoding="utf-8").strip().lower()
    except OSError:
        return ""


def locked() -> bool:
    """True when LOCAL_COACH_LANG is set: the environment wins over the saved choice, the button must not offer a change."""
    return bool(os.environ.get("LOCAL_COACH_LANG"))


def save_lang(workspace: Path, code: str) -> None:
    """Remember the language for the next start (atomic write)."""
    if code not in LANGS:
        raise ValueError(code)
    workspace.mkdir(parents=True, exist_ok=True)
    tmp = workspace / (LANG_FILE + ".tmp")
    tmp.write_text(code + "\n", encoding="utf-8")
    tmp.replace(workspace / LANG_FILE)


def _pick() -> str:
    raw = (os.environ.get("LOCAL_COACH_LANG") or saved_lang() or DEFAULT_LANG).strip().lower()
    if raw in LANGS:
        return raw
    print(f"LOCAL_COACH_LANG={raw!r} не поддерживается (есть: {', '.join(LANGS)}), беру {DEFAULT_LANG}.", file=sys.stderr)
    return DEFAULT_LANG


LANG = _pick()
STRICT = os.environ.get("LOCAL_COACH_I18N_STRICT") == "1"
MISSING: set[str] = set()
_catalog: dict[str, str] | None = None


def catalog() -> dict[str, str]:
    """Каталог перевода на текущий язык: русская строка -> перевод. Для ru пуст."""
    global _catalog
    if _catalog is None:
        path = CATALOG_DIR / f"{LANG}.json"
        _catalog = json.loads(path.read_text(encoding="utf-8")) if LANG != "ru" and path.exists() else {}
    return _catalog


def trl(text: str, *args, **kwargs) -> str:
    """Перевести строку интерфейса и подставить параметры (см. описание модуля)."""
    out = text
    if LANG != "ru":
        found = catalog().get(text)
        if found is None:
            MISSING.add(text)
            if STRICT:
                raise KeyError(f"нет перевода ({LANG}): {text[:80]!r}")
        else:
            out = found
    return out.format(*args, **kwargs) if (args or kwargs) else out


def pick(ru, en):
    """Значение по языку: для кода, где различается не строка, а целая структура (правила, регулярки, слова)."""
    return ru if LANG == "ru" else en


# Английские ответы-буквы в диалогах «так и сейчас / было / изменилось / неверно / пропустить / выйти».
# Русские буквы работают всегда; английские приводятся к ним на входе (язык интерфейса на ввод не влияет).
_EN_KEYS = {"c": "с", "p": "б", "u": "и", "n": "н", "s": "п", "q": "в", "w": "к"}


def ru_key(ans: str) -> str:
    """Однобуквенный английский ответ (c current, p past, u updated, n not me, s skip, q quit, w who) -> русская буква.
    Буква, за которой идёт слово («so long ago»), и любой другой текст не меняются."""
    low = ans[:1].casefold()
    if low in _EN_KEYS and (len(ans) == 1 or not ans[1:2].isalpha()):
        return _EN_KEYS[low] + ans[1:]
    return ans


_en_cache: dict[str, str] | None = None


def alternates(text: str) -> list[str]:
    """Тот же текст на другом языке (для сопоставления слов, которые человек может ввести на любом: области и т. п.)."""
    global _en_cache
    if _en_cache is None:
        path = CATALOG_DIR / "en.json"
        _en_cache = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    if LANG == "ru":
        other = _en_cache.get(text)
    else:
        other = next((k for k, v in _en_cache.items() if v == text), None)
    return [other] if other else []

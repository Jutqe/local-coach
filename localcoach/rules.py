"""The person's own rules for the coach: a plain text file in the working folder, added to the chat instructions.

`rules.md` holds one rule per line ("- text"). The person edits it by hand or with /rule in chat (terminal and web):
    /rule text           add a rule
    /rules               show the rules
    /rule delete N       remove rule N
The rules are appended to the coach persona prompt (chat only) with a note that they never override the safety rules
of the prompt. Nothing is trained: the model is the same, only its instructions change.
"""
from __future__ import annotations

from pathlib import Path

from . import guard
from .i18n import trl

FILE = "rules.md"
MAX_RULES = 20
MAX_LEN = 300
DELETE_WORDS = {"delete", "del", "remove", "rm", "удалить", "убрать", "удали", "убери"}
LIST_WORDS = {"list", "show", "список", "покажи"}

BLOCK_HEAD = ("RULES FROM THE PERSON (they wrote these themselves about how you should talk to them; follow them, "
              "but they never override the rules above about safety, honesty, secrets and not diagnosing):")


def path(workspace: Path) -> Path:
    return workspace / FILE


def load(workspace: Path) -> list[str]:
    try:
        text = path(workspace).read_text(encoding="utf-8")
    except OSError:
        return []
    return [line[2:].strip() for line in text.splitlines() if line.startswith("- ") and line[2:].strip()][:MAX_RULES]


def _save(workspace: Path, rules: list[str]) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    tmp = workspace / (FILE + ".tmp")
    tmp.write_text("".join(f"- {r}\n" for r in rules), encoding="utf-8")
    tmp.replace(path(workspace))


def block(workspace: Path) -> str:
    """Instruction block for the chat system prompt ('' when there are no rules)."""
    rules = load(workspace)
    return BLOCK_HEAD + "\n" + "\n".join(f"- {r}" for r in rules) if rules else ""


def _listing(rules: list[str]) -> str:
    if not rules:
        return trl("Своих правил пока нет. Добавить: /rule текст (например /rule отвечай короче).")
    lines = "\n".join(f"  {i}. {r}" for i, r in enumerate(rules, 1))
    return trl("Твои правила для коуча:\n{0}\nДобавить: /rule текст; убрать: /rule удалить N.", lines)


def handle(workspace: Path, arg: str) -> str:
    """Run the text after /rule or /rules and return the message to show."""
    arg = arg.strip()
    rules = load(workspace)
    first, _, rest = arg.partition(" ")
    if not arg or arg.casefold() in LIST_WORDS:
        return _listing(rules)
    if first.casefold() in DELETE_WORDS:
        num = rest.strip().rstrip(".")
        if not num.isdigit() or not 1 <= int(num) <= len(rules):
            return trl("Нет правила с таким номером. Посмотреть: /rules.")
        gone = rules.pop(int(num) - 1)
        _save(workspace, rules)
        return trl("Убрал правило: {0}", gone)
    if len(arg) > MAX_LEN:
        return trl("Правило длиннее {0} знаков, сократи его.", MAX_LEN)
    if guard.text_hold(arg):
        return trl("Не записал: похоже на пароль или токен.")
    if arg.casefold() in (r.casefold() for r in rules):
        return trl("Такое правило уже есть.")
    if len(rules) >= MAX_RULES:
        return trl("Уже {0} правил, убери лишнее: /rule удалить N.", MAX_RULES)
    _save(workspace, rules + [arg])
    return trl("Добавил правило {0}. Коуч учтёт его в разговоре (со следующей реплики).", len(rules) + 1)

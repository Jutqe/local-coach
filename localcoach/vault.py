"""Чтение vault: frontmatter, даты, теги, ссылки и нарезка на фрагменты по заголовкам."""
from __future__ import annotations

import bisect
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import guard
from .config import Config, DEFAULT_VAULT_CANDIDATES, EXCLUDED_DIRS, LIFE_RESET_DIR
from .util import common_date, find_date, heading_year, sha256_text, title_year, warn
from .i18n import trl


@dataclass
class Chunk:
    idx: int
    char_start: int
    char_end: int
    line_start: int
    line_end: int
    heading: str          # путь заголовков первого раздела: «Отпуск › План»
    chunk_date: str | None
    text: str


@dataclass
class Note:
    path: str             # относительный POSIX-путь
    title: str
    sha256: str
    size: int
    mtime: float
    content: str
    note_date: str | None
    date_source: str      # frontmatter | filename | mtime
    tags: list[str] = field(default_factory=list)
    links: list[str] = field(default_factory=list)
    hold_reason: str | None = None
    chunks: list[Chunk] = field(default_factory=list)


def find_vault(cli_value: str | None) -> Path:
    candidates: list[Path] = []
    if cli_value:
        candidates.append(Path(cli_value).expanduser())
    if os.environ.get("LOCAL_COACH_VAULT"):
        candidates.append(Path(os.environ["LOCAL_COACH_VAULT"]).expanduser())
    candidates.extend(DEFAULT_VAULT_CANDIDATES)
    for c in candidates:
        if c.is_dir() and next(c.rglob("*.md"), None) is not None:
            return c.resolve()
    raise FileNotFoundError(trl("Не найдена папка vault с Markdown-файлами. Проверены:\n")
                            + "\n".join(f"  - {c}" for c in candidates)
                            + trl("\nУкажи путь: --vault /путь/к/vault"))


def walk_markdown(vault: Path) -> list[Path]:
    files: list[Path] = []
    for root, dirs, names in os.walk(vault, followlinks=False):
        dirs[:] = [d for d in dirs if d not in EXCLUDED_DIRS and not d.startswith(".")
                   # Свои итоги коуч как заметки не читает – только папку в корне vault, а не любую с тем же именем.
                   and not (d == LIFE_RESET_DIR and Path(root) == vault)]
        for name in names:
            if not name.lower().endswith(".md"):
                continue
            p = Path(root) / name
            if p.is_symlink() or not p.is_file():
                continue
            files.append(p)
    files.sort(key=lambda p: p.relative_to(vault).as_posix().casefold())
    return files


# --- frontmatter -----------------------------------------------------------

_FM_KEY = re.compile(r"^([A-Za-zА-Яа-яЁё_][\w -]*):\s*(.*)$")


def split_frontmatter(content: str) -> tuple[dict[str, object], int]:
    """Простой разбор YAML-шапки (ключ: значение, списки). Возвращает (поля, смещение тела)."""
    if not content.startswith("---"):
        return {}, 0
    m = re.match(r"---[ \t]*\r?\n(.*?)\r?\n(?:---|\.\.\.)[ \t]*(?:\r?\n|$)", content, re.S)
    if not m:
        return {}, 0
    data: dict[str, object] = {}
    current: str | None = None
    for line in m.group(1).splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        item = re.match(r"^\s*-\s+(.*)$", line)
        if item and current:
            value = data.get(current)
            if not isinstance(value, list):
                value = [] if value in (None, "") else [value]
            value.append(item.group(1).strip().strip("'\""))
            data[current] = value
            continue
        km = _FM_KEY.match(line)
        if km:
            current = km.group(1).strip().casefold()
            raw = km.group(2).strip()
            if raw.startswith("[") and raw.endswith("]"):
                data[current] = [x.strip().strip("'\"") for x in raw[1:-1].split(",") if x.strip()]
            else:
                data[current] = raw.strip("'\"")
    return data, m.end()


_DATE_KEYS = ("date", "created", "created_at", "creation date", "day", "дата", "создано")


def _fm_list(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(v) for v in value]
    if isinstance(value, str) and value:
        return [v for v in re.split(r"[,\s]+", value) if v]
    return []


# --- теги, ссылки, разделы ---------------------------------------------------

_TAG = re.compile(r"(?<![\w/#&])#([A-Za-zА-Яа-яЁё_][\wА-Яа-яЁё/-]*)")
_LINK = re.compile(r"\[\[([^\]|#^]+)(?:[#^][^\]|]*)?(?:\|[^\]]*)?\]\]")
_HEADING = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t#]*$")
_FENCE = re.compile(r"^\s*(```|~~~)")
# Год жирной строкой («**2015 - мне 25 - переезд**») — тоже заголовок раздела, ниже любого «#».
_BOLD_LINE = re.compile(r"^\s*\*\*([^*]+?)(?:\*\*|$)")
# …и строка вида «2020:» или «2020 год — переезд:», за которой идёт список того, что было в этом году.
_COLON_LINE = re.compile(r"^\s*([^\s*#>|-][^\n]{0,78}?)\s*:\s*$")


def _strip_code(text: str) -> str:
    out, fenced = [], False
    for line in text.splitlines():
        if _FENCE.match(line):
            fenced = not fenced
            continue
        if not fenced:
            out.append(re.sub(r"`[^`]*`", " ", line))
    return "\n".join(out)


def extract_tags_links(body: str, fm: dict[str, object]) -> tuple[list[str], list[str]]:
    plain = _strip_code(body)
    tags = {t.casefold() for t in _TAG.findall(plain)}
    tags.update(t.lstrip("#").casefold() for t in _fm_list(fm.get("tags") or fm.get("tag")))
    links = {l.strip() for l in _LINK.findall(plain) if l.strip()}
    return sorted(tags)[:40], sorted(links)[:80]


def _sections(content: str, body_start: int) -> list[tuple[int, int, str, str | None]]:
    """Разделы тела по заголовкам: (начало, конец, путь заголовков, дата из заголовка)."""
    bounds: list[tuple[int, int, str]] = []   # (offset, level, title)
    fenced = False
    offset = body_start
    for line in content[body_start:].splitlines(keepends=True):
        if _FENCE.match(line):
            fenced = not fenced
        elif not fenced:
            m = _HEADING.match(line.rstrip("\r\n"))
            if m:
                bounds.append((offset, len(m.group(1)), m.group(2).strip()))
            else:
                b = _BOLD_LINE.match(line) or _COLON_LINE.match(line)
                if b and heading_year(b.group(1)):
                    bounds.append((offset, 7, b.group(1).strip()))
        offset += len(line)
    sections: list[tuple[int, int, str, str | None]] = []
    starts = [body_start] + [b[0] for b in bounds if b[0] > body_start]
    path: list[tuple[int, str]] = []
    heading_at = {b[0]: (b[1], b[2]) for b in bounds}
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else len(content)
        if start in heading_at:
            level, title = heading_at[start]
            path = [p for p in path if p[0] < level] + [(level, title)]
        crumbs = " › ".join(t for _, t in path)
        dated = next((d for d in (find_date(t) or heading_year(t) for _, t in reversed(path)) if d), None)
        if content[start:end].strip():
            sections.append((start, end, crumbs, dated))
    return sections


def _split_long(content: str, start: int, end: int, max_chars: int, overlap: int) -> list[tuple[int, int]]:
    """Нарезка длинного раздела по абзацам, с небольшим перекрытием."""
    ranges: list[tuple[int, int]] = []
    pos = start
    while pos < end:
        stop = min(end, pos + max_chars)
        if stop < end:
            floor = pos + max_chars // 2
            cut = max(content.rfind("\n\n", floor, stop), content.rfind("\n", floor, stop))
            if cut > floor:
                stop = cut
        ranges.append((pos, stop))
        if stop >= end:
            break
        pos = max(pos + 1, stop - overlap)
    return ranges


def make_chunks(content: str, body_start: int, note_date: str | None, cfg: Config) -> list[Chunk]:
    newlines = [i for i, ch in enumerate(content) if ch == "\n"]

    def line_at(offset: int) -> int:
        return bisect.bisect_left(newlines, offset) + 1

    pieces: list[tuple[int, int, str, str | None]] = []
    for start, end, crumbs, dated in _sections(content, body_start):
        if end - start > cfg.chunk_chars:
            for s, e in _split_long(content, start, end, cfg.chunk_chars, cfg.chunk_overlap_chars):
                pieces.append((s, e, crumbs, dated))
        else:
            pieces.append((start, end, crumbs, dated))

    # Соседние короткие разделы склеиваем, пока влезают в размер фрагмента.
    groups: list[list[tuple[int, int, str, str | None]]] = []
    for piece in pieces:
        if groups:
            g = groups[-1]
            size = piece[1] - g[0][0]
            contiguous = piece[0] <= g[-1][1]
            # Разделы разных лет не склеиваем: дата фрагмента служит датой его фактов.
            same_year = (piece[3] or "")[:4] == (g[0][3] or "")[:4]
            if contiguous and same_year and size <= cfg.chunk_chars:
                g.append(piece)
                continue
        groups.append([piece])
    # Слишком короткий хвост присоединяем к предыдущему, если не раздувает его сильно.
    if len(groups) > 1:
        tail = groups[-1]
        if tail[-1][1] - tail[0][0] < cfg.chunk_min_chars and \
                tail[-1][1] - groups[-2][0][0] <= int(cfg.chunk_chars * 1.3) and tail[0][0] <= groups[-2][-1][1] and \
                (tail[0][3] or "")[:4] == (groups[-2][0][3] or "")[:4]:
            groups[-2].extend(groups.pop())

    chunks: list[Chunk] = []
    for idx, g in enumerate(groups, 1):
        start, end = g[0][0], max(p[1] for p in g)
        text = content[start:end]
        if not text.strip():
            continue
        # Дата фрагмента — общая часть дат его разделов (день, месяц или год); точные даты
        # разделов модель видит в заголовках и сама относит к ним факты.
        dated = common_date([p[3] for p in g])
        chunks.append(Chunk(idx=len(chunks) + 1, char_start=start, char_end=end,
                            line_start=line_at(start), line_end=line_at(max(start, end - 1)),
                            heading=g[0][2], chunk_date=dated or note_date, text=text))
    return chunks


IMPORT_PREFIX = "_imports/"


def read_note(vault: Path, file_path: Path, cfg: Config, imported: bool = False) -> Note | None:
    try:
        stat = file_path.stat()
        raw = file_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        warn(trl("не удалось прочитать {0}: {1}", file_path.name, exc.strerror))
        return None
    rel = (IMPORT_PREFIX if imported else "") + file_path.relative_to(vault).as_posix()
    title = file_path.stem
    # BOM от Windows-редакторов мешает распознать frontmatter. Хэш считаем по файлу
    # как есть, чтобы совпадать с v3 при импорте.
    content = raw.lstrip("\ufeff")
    fm, body_start = split_frontmatter(content)

    note_date, source = None, "mtime"
    for key in _DATE_KEYS:
        value = fm.get(key)
        if isinstance(value, str) and find_date(value):
            note_date, source = find_date(value), "frontmatter"
            break
    if imported and not note_date:
        # Дата импорта — не дата событий: время берётся только из текста документа.
        source = "import"
    elif not note_date and (find_date(title) or title_year(title)):
        note_date, source = find_date(title) or title_year(title), "filename"
    if not note_date and source == "mtime":
        note_date = datetime.fromtimestamp(stat.st_mtime).date().isoformat()

    tags, links = extract_tags_links(content[body_start:], fm)
    note = Note(path=rel, title=title, sha256=sha256_text(raw), size=stat.st_size, mtime=stat.st_mtime,
                content=content, note_date=note_date, date_source=source, tags=tags, links=links)
    note.hold_reason = guard.note_hold(rel, title, content)
    if not note.hold_reason:
        note.chunks = make_chunks(content, body_start, note_date, cfg)
    return note


def scan(vault: Path, cfg: Config) -> list[Note]:
    notes = [n for p in walk_markdown(vault) if (n := read_note(vault, p, cfg)) is not None]
    if cfg.imports_dir.is_dir():
        notes += [n for p in walk_markdown(cfg.imports_dir)
                  if (n := read_note(cfg.imports_dir, p, cfg, imported=True)) is not None]
    mtime_days = [n.note_date for n in notes if n.date_source == "mtime"]
    if len(mtime_days) >= 20:
        top = max(set(mtime_days), key=mtime_days.count)
        if mtime_days.count(top) / len(mtime_days) > 0.5:
            warn(trl("у {0} заметок без даты одинаковый mtime ({1}) — похоже на дату копирования, а не написания. Такие даты коуч помечает как ненадёжные.", mtime_days.count(top), top))
            for n in notes:
                if n.date_source == "mtime" and n.note_date == top:
                    n.date_source = "mtime_bulk"
    return notes

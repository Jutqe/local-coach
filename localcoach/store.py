"""SQLite-хранилище: заметки, фрагменты, факты, кластеры, сессии, кандидаты в память."""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Iterable

from .util import dumps, iso_now, loads

SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS notes (
  path TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  size_bytes INTEGER NOT NULL,
  mtime REAL NOT NULL,
  note_date TEXT,
  date_source TEXT NOT NULL,
  tags_json TEXT NOT NULL DEFAULT '[]',
  links_json TEXT NOT NULL DEFAULT '[]',
  status TEXT NOT NULL,              -- indexed | security_hold | stale
  hold_reason TEXT,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chunks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  path TEXT NOT NULL REFERENCES notes(path) ON DELETE CASCADE,
  idx INTEGER NOT NULL,
  char_start INTEGER NOT NULL,
  char_end INTEGER NOT NULL,
  line_start INTEGER NOT NULL,
  line_end INTEGER NOT NULL,
  heading TEXT NOT NULL DEFAULT '',
  chunk_date TEXT,
  sha256 TEXT NOT NULL,
  body TEXT NOT NULL,
  summary TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'pending',   -- pending | done | imported | error
  attempts INTEGER NOT NULL DEFAULT 0,
  error TEXT,
  embedding BLOB,
  updated_at TEXT NOT NULL,
  UNIQUE(path, idx)
);
CREATE INDEX IF NOT EXISTS idx_chunks_status ON chunks(status);
CREATE INDEX IF NOT EXISTS idx_chunks_sha ON chunks(sha256);

CREATE TABLE IF NOT EXISTS facts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  chunk_id INTEGER REFERENCES chunks(id) ON DELETE SET NULL,
  path TEXT,                          -- NULL для фактов из диалога
  source_ref TEXT NOT NULL,           -- путь#Lx-Ly или session:N
  origin TEXT NOT NULL,               -- note | v3_import | chat | session
  claim TEXT NOT NULL,
  kind TEXT NOT NULL,
  domain TEXT NOT NULL,
  subject TEXT NOT NULL DEFAULT 'self',
  explicitness TEXT NOT NULL DEFAULT 'explicit',
  intent_status TEXT NOT NULL DEFAULT 'none',
  event_date TEXT,                    -- когда это было (по тексту или дате заметки)
  note_date TEXT,
  date_reliable INTEGER NOT NULL DEFAULT 1,
  evidence_quote TEXT NOT NULL DEFAULT '',
  quote_match TEXT NOT NULL DEFAULT 'none',   -- exact | partial | none
  sensitivity TEXT NOT NULL DEFAULT 'normal',
  is_project INTEGER NOT NULL DEFAULT 0,
  user_state TEXT NOT NULL DEFAULT 'none',    -- none | confirmed | rejected
  cluster_id INTEGER,
  embedding BLOB,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_facts_path ON facts(path);
CREATE INDEX IF NOT EXISTS idx_facts_cluster ON facts(cluster_id);
CREATE INDEX IF NOT EXISTS idx_facts_chunk ON facts(chunk_id);

CREATE TABLE IF NOT EXISTS hypotheses_raw (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  chunk_id INTEGER REFERENCES chunks(id) ON DELETE CASCADE,
  path TEXT NOT NULL,
  source_ref TEXT NOT NULL,
  hypothesis TEXT NOT NULL,
  evidence_quote TEXT NOT NULL,
  alternative TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_hyp_chunk ON hypotheses_raw(chunk_id);
CREATE INDEX IF NOT EXISTS idx_hyp_path ON hypotheses_raw(path);

CREATE TABLE IF NOT EXISTS clusters (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  domain TEXT NOT NULL,
  centroid BLOB,
  size INTEGER NOT NULL DEFAULT 0,
  support_notes INTEGER NOT NULL DEFAULT 0,
  first_date TEXT,
  last_date TEXT,
  head_fact_id INTEGER,              -- самый свежий надёжный факт кластера
  score REAL NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL
);

-- Результаты синтеза (домены, профиль, аудит) с отпечатком входа для кэша.
CREATE TABLE IF NOT EXISTS artifacts (
  stage TEXT NOT NULL,
  key TEXT NOT NULL,
  fingerprint TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(stage, key)
);

CREATE TABLE IF NOT EXISTS sessions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  started_at TEXT NOT NULL,
  ended_at TEXT,
  title TEXT NOT NULL DEFAULT '',
  summary TEXT NOT NULL DEFAULT '',
  agreements_json TEXT NOT NULL DEFAULT '[]',
  embedding BLOB
);
CREATE TABLE IF NOT EXISTS turns (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  role TEXT NOT NULL,                -- user | assistant
  content TEXT NOT NULL,
  refs_json TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL
);

-- Предложения в память из диалогов и вопросы аудита: ждут решения пользователя.
CREATE TABLE IF NOT EXISTS candidates (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id INTEGER,
  source TEXT NOT NULL,              -- chat | audit
  claim TEXT NOT NULL,
  kind TEXT NOT NULL,
  domain TEXT NOT NULL DEFAULT 'other',
  reason TEXT NOT NULL DEFAULT '',
  target_fact_id INTEGER,            -- для вопросов аудита: о каком факте речь
  status TEXT NOT NULL DEFAULT 'pending',   -- pending | accepted | rejected | skipped
  created_at TEXT NOT NULL,
  decided_at TEXT
);

-- Решения пользователя по утверждениям (подтвердил / отверг). Хранятся отдельно от
-- фактов, поэтому переживают повторное извлечение заметок: новые факты, близкие
-- по смыслу, получают ту же отметку.
CREATE TABLE IF NOT EXISTS user_marks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  claim TEXT NOT NULL,
  mark TEXT NOT NULL,                -- confirmed | rejected
  embedding BLOB,
  created_at TEXT NOT NULL
);

-- Договорённости из разговоров: что человек решил сделать, со сроком и статусом.
CREATE TABLE IF NOT EXISTS commitments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id INTEGER,
  text TEXT NOT NULL,
  due TEXT,                          -- срок, если звучал (ГГГГ-ММ-ДД)
  status TEXT NOT NULL DEFAULT 'open',   -- open | done | dropped
  note TEXT NOT NULL DEFAULT '',     -- как продвигается, почему бросил
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_commitments_status ON commitments(status);

-- Чек-ин: короткие ответы «как ты / что главное / что мешает» (своими словами).
CREATE TABLE IF NOT EXISTS checkins (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  mood INTEGER,                      -- 1..10, если назвал
  answers_json TEXT NOT NULL,        -- {"mood": "...", "focus": "...", "worry": "..."}
  created_at TEXT NOT NULL
);

-- Люди из заметок и ответов. Строка живёт по имени и переживает повторное извлечение:
-- твои ответы (кто это, в жизни ли сейчас) не теряются; связи с фактами пересобираются.
CREATE TABLE IF NOT EXISTS people (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  relation TEXT NOT NULL DEFAULT '',
  relation_src TEXT NOT NULL DEFAULT '',   -- model | user
  status TEXT NOT NULL DEFAULT 'unknown',  -- current | past | unknown | ignored (не человек / ошибка)
  until TEXT,                              -- для past: примерно когда
  note TEXT NOT NULL DEFAULT '',
  merged_into INTEGER,                     -- это тот же человек, что people.id
  mentions INTEGER NOT NULL DEFAULT 0,
  first_date TEXT,
  last_date TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_people_name ON people(name COLLATE NOCASE);
CREATE TABLE IF NOT EXISTS fact_people (
  fact_id INTEGER NOT NULL,
  person_id INTEGER NOT NULL,
  PRIMARY KEY (fact_id, person_id)
);
CREATE INDEX IF NOT EXISTS idx_fact_people_person ON fact_people(person_id);

-- Режим присутствия (body double): над чем и сколько человек работал с коучем «рядом».
CREATE TABLE IF NOT EXISTS focus_sessions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id INTEGER,
  task TEXT NOT NULL DEFAULT '',
  started_at TEXT NOT NULL,
  ended_at TEXT,
  minutes INTEGER NOT NULL DEFAULT 0
);

-- Оценка качества: твои вердикты по выборке. Хранится ключ пункта, а не сам текст,
-- поэтому отчёт состоит только из чисел и его можно пересылать.
CREATE TABLE IF NOT EXISTS eval_marks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  target TEXT NOT NULL,              -- currency | fact | profile | reply
  item_key TEXT NOT NULL,            -- хэш пункта: один пункт не показывается дважды
  breakdown TEXT NOT NULL DEFAULT '',-- разрез отчёта: раздел профиля, источник актуальности, тип факта
  verdict TEXT NOT NULL,
  version TEXT NOT NULL,             -- версия кода / промптов / модель
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_eval_target ON eval_marks(target, item_key);

-- Актуальность: что из памяти верно сейчас, а что было раньше. Решения хранятся по
-- формулировке (как user_marks) и переживают повторное извлечение заметок.
CREATE TABLE IF NOT EXISTS currency_marks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  claim TEXT NOT NULL,
  status TEXT NOT NULL,              -- current | past
  until TEXT,                        -- когда примерно закончилось (для past)
  period TEXT,                       -- когда это было, если известно («2012–2016»)
  source TEXT NOT NULL,              -- user — ответ человека; timeline — сверка с биографией и ответами
  note TEXT NOT NULL DEFAULT '',
  embedding BLOB,
  created_at TEXT NOT NULL
);

-- Life Reset: реестр накопленного. Пункт живёт по смыслу (название + суть + вектор) и
-- переживает повторное извлечение заметок: на `inventory build` новые группы фактов
-- сверяются с пунктами по вектору, статус и история решений не теряются.
CREATE TABLE IF NOT EXISTS lr_items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT NOT NULL,
  type TEXT NOT NULL,                 -- commitment | project | idea | wish | question | situation | routine | material | experience
  status TEXT NOT NULL DEFAULT 'inbox',
  domain TEXT NOT NULL DEFAULT 'other',
  summary TEXT NOT NULL DEFAULT '',
  want TEXT NOT NULL DEFAULT '',      -- исходное желание: чего тогда хотелось
  hint TEXT NOT NULL DEFAULT '',      -- что видно по памяти (кодом): «в заметках отмечено как сделанное»
  origin TEXT NOT NULL DEFAULT 'memory',   -- memory | chat
  sensitive INTEGER NOT NULL DEFAULT 0,
  first_date TEXT,
  last_date TEXT,                     -- последнее упоминание в заметках и разговорах
  mentions INTEGER NOT NULL DEFAULT 0,
  notes_count INTEGER NOT NULL DEFAULT 0,
  next_step TEXT NOT NULL DEFAULT '',
  review_on TEXT,                     -- когда пересмотреть (someday / incubating / waiting)
  touched TEXT,                       -- когда последний раз менялся файл заметки-источника (для заметок без дат)
  resurfaced INTEGER NOT NULL DEFAULT 0,   -- закрытый пункт снова появился в заметках
  src_sig TEXT,                       -- отпечаток проектных заметок-источников на момент решения
  merged_into INTEGER,
  fact_ids_json TEXT NOT NULL DEFAULT '[]',
  paths_json TEXT NOT NULL DEFAULT '[]',
  anchors_json TEXT NOT NULL DEFAULT '[]',  -- «note:путь»: проектная заметка – всегда этот пункт, как бы её ни назвали
  embedding BLOB,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  decided_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_lr_items_status ON lr_items(status);

-- Журнал решений: исходное состояние, предложение коуча, решение человека и причина.
CREATE TABLE IF NOT EXISTS lr_decisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  item_id INTEGER NOT NULL,
  from_status TEXT NOT NULL,
  to_status TEXT NOT NULL,
  proposal TEXT NOT NULL DEFAULT '',  -- что предложил коуч (если предлагал)
  answer TEXT NOT NULL DEFAULT '',    -- ответ человека своими словами
  reason TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_lr_decisions_item ON lr_decisions(item_id);

-- Аудит жизни по областям: один раунд — одна дата, по строке на область.
CREATE TABLE IF NOT EXISTS lr_audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  round INTEGER NOT NULL,
  area TEXT NOT NULL,
  satisfaction INTEGER,               -- 1..10: насколько устраивает сейчас
  importance INTEGER,                 -- 1..10: насколько важно
  change TEXT NOT NULL DEFAULT '',    -- yes | no | maybe: хочу ли менять
  nature TEXT NOT NULL DEFAULT '',    -- problem | should | '': настоящая проблема или «так должно быть»
  note TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  UNIQUE(round, area)
);

-- Психологические гипотезы: не факты и не диагнозы. Подтверждённая — это описание
-- повторяемости, которое человек признал, а не черта личности.
CREATE TABLE IF NOT EXISTS lr_hypotheses (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  text TEXT NOT NULL,
  observations_json TEXT NOT NULL DEFAULT '[]',   -- [{"quote", "ref", "date"}]
  alternatives_json TEXT NOT NULL DEFAULT '[]',
  question TEXT NOT NULL DEFAULT '',  -- вопрос для проверки
  confidence TEXT NOT NULL DEFAULT 'low',          -- low | medium | high
  status TEXT NOT NULL DEFAULT 'new',              -- new | testing | pattern | rejected
  user_note TEXT NOT NULL DEFAULT '',
  embedding BLOB,
  created_at TEXT NOT NULL,
  checked_at TEXT
);
"""

# Колонки, добавленные после первой версии схемы: в старой базе они появляются через ALTER.
MIGRATIONS = (
    ("facts", "people_scanned", "INTEGER NOT NULL DEFAULT 0"),            # люди в факте уже найдены
    ("clusters", "currency", "TEXT NOT NULL DEFAULT 'unknown'"),   # current | past | history | unknown
    ("clusters", "currency_src", "TEXT NOT NULL DEFAULT ''"),      # user | timeline | confirmed | recent | kind
    ("clusters", "valid_until", "TEXT"),
    ("clusters", "period", "TEXT"),
    ("facts", "currency_mark_id", "INTEGER"),
    ("candidates", "replace_kind", "TEXT NOT NULL DEFAULT ''"),    # changed | wrong
    ("eval_marks", "reason", "TEXT NOT NULL DEFAULT ''"),          # почему неверно: invented | past | overgeneral | mixed
    ("lr_items", "bulk", "INTEGER NOT NULL DEFAULT 0"),            # одиночное упоминание: ждёт разбора пачкой (sweep)
    ("lr_items", "bulk_rank", "INTEGER"),                          # NULL – не размечено; 0 – модель не выбрала; N – место в области
    ("focus_sessions", "item_id", "INTEGER"),                      # пункт реестра Life Reset, над которым работал
    ("focus_sessions", "planned", "INTEGER"),                      # минут по плану; NULL – без плана
    ("focus_sessions", "outcome", "TEXT NOT NULL DEFAULT ''"),     # «как прошло» одной фразой
)

FTS = (
    ("chunks_fts", "CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(body, title UNINDEXED, tokenize='trigram')"),
    ("facts_fts", "CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts USING fts5(claim, tokenize='trigram')"),
)


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=60, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    for _name, ddl in FTS:
        try:
            conn.execute(ddl)
        except sqlite3.OperationalError:
            # Без trigram (SQLite < 3.34) — обычный токенизатор.
            conn.execute(ddl.replace("tokenize='trigram'", "tokenize='unicode61'"))
    for table, column, ddl in MIGRATIONS:
        if column not in {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
    set_meta(conn, "schema_version", str(SCHEMA_VERSION))
    conn.commit()
    return conn


def get_meta(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (key, value))


def scalar(conn: sqlite3.Connection, sql: str, params: Iterable[Any] = ()) -> Any:
    row = conn.execute(sql, tuple(params)).fetchone()
    return row[0] if row else None


# --- FTS -------------------------------------------------------------------

def fold(text: str) -> str:
    """trigram не отождествляет «ё» и «е»: приводим и индекс, и запрос к «е»."""
    return text.replace("ё", "е").replace("Ё", "Е")


def fts_put_chunk(conn: sqlite3.Connection, chunk_id: int, body: str, title: str) -> None:
    conn.execute("DELETE FROM chunks_fts WHERE rowid=?", (chunk_id,))
    conn.execute("INSERT INTO chunks_fts(rowid, body, title) VALUES(?,?,?)", (chunk_id, fold(body), title))


def fts_put_fact(conn: sqlite3.Connection, fact_id: int, claim: str) -> None:
    conn.execute("DELETE FROM facts_fts WHERE rowid=?", (fact_id,))
    conn.execute("INSERT INTO facts_fts(rowid, claim) VALUES(?,?)", (fact_id, fold(claim)))


# --- удаление с учётом зависимостей ----------------------------------------

def delete_note_derived(conn: sqlite3.Connection, path: str) -> set[int]:
    """Удалить фрагменты и факты заметки. Возвращает затронутые кластеры."""
    clusters = {r[0] for r in conn.execute(
        "SELECT DISTINCT cluster_id FROM facts WHERE path=? AND cluster_id IS NOT NULL", (path,))}
    delete_facts(conn, "path=?", (path,))
    for (cid,) in conn.execute("SELECT id FROM chunks WHERE path=?", (path,)).fetchall():
        conn.execute("DELETE FROM chunks_fts WHERE rowid=?", (cid,))
    conn.execute("DELETE FROM hypotheses_raw WHERE path=?", (path,))
    conn.execute("DELETE FROM chunks WHERE path=?", (path,))
    return clusters


def delete_chunk_derived(conn: sqlite3.Connection, chunk_id: int) -> set[int]:
    clusters = {r[0] for r in conn.execute(
        "SELECT DISTINCT cluster_id FROM facts WHERE chunk_id=? AND cluster_id IS NOT NULL", (chunk_id,))}
    delete_facts(conn, "chunk_id=?", (chunk_id,))
    conn.execute("DELETE FROM hypotheses_raw WHERE chunk_id=?", (chunk_id,))
    return clusters


def delete_facts(conn: sqlite3.Connection, where: str, params: tuple = ()) -> int:
    """Удалить факты вместе с их FTS-записями и пометить их кластеры к пересчёту."""
    rows = conn.execute(f"SELECT id, cluster_id FROM facts WHERE {where}", params).fetchall()
    for r in rows:
        conn.execute("DELETE FROM facts_fts WHERE rowid=?", (r["id"],))
    conn.execute(f"DELETE FROM facts WHERE {where}", params)
    mark_clusters_dirty(conn, [r["cluster_id"] for r in rows])
    return len(rows)


def mark_clusters_dirty(conn: sqlite3.Connection, cluster_ids: Iterable[int]) -> None:
    ids = sorted({int(c) for c in cluster_ids if c is not None})
    if not ids:
        return
    existing = loads(get_meta(conn, "dirty_clusters"), []) or []
    set_meta(conn, "dirty_clusters", dumps(sorted(set(existing) | set(ids))))


def take_dirty_clusters(conn: sqlite3.Connection) -> set[int]:
    ids = set(loads(get_meta(conn, "dirty_clusters"), []) or [])
    set_meta(conn, "dirty_clusters", "[]")
    return ids


# --- кэш артефактов -----------------------------------------------------------

def cache_get(conn: sqlite3.Connection, stage: str, key: str, fingerprint: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT fingerprint, payload_json FROM artifacts WHERE stage=? AND key=?",
                       (stage, key)).fetchone()
    if not row or row["fingerprint"] != fingerprint:
        return None
    return loads(row["payload_json"])


def cache_any(conn: sqlite3.Connection, stage: str, key: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT payload_json FROM artifacts WHERE stage=? AND key=?", (stage, key)).fetchone()
    return loads(row["payload_json"]) if row else None


def cache_put(conn: sqlite3.Connection, stage: str, key: str, fingerprint: str, payload: dict[str, Any]) -> None:
    conn.execute(
        "INSERT INTO artifacts(stage,key,fingerprint,payload_json,updated_at) VALUES(?,?,?,?,?) "
        "ON CONFLICT(stage,key) DO UPDATE SET fingerprint=excluded.fingerprint, "
        "payload_json=excluded.payload_json, updated_at=excluded.updated_at",
        (stage, key, fingerprint, dumps(payload), iso_now()))
    conn.commit()


def cache_drop_stage(conn: sqlite3.Connection, stage: str) -> None:
    conn.execute("DELETE FROM artifacts WHERE stage=?", (stage,))

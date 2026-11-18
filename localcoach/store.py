"""SQLite-хранилище: заметки, фрагменты, факты, кластеры, сессии, кандидаты в память."""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Iterable

from .util import dumps, iso_now, loads
from .i18n import trl

SCHEMA_VERSION = 2

SCHEMA = trl("\n"
             "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);\n"
             "\n"
             "CREATE TABLE IF NOT EXISTS notes (\n"
             "  path TEXT PRIMARY KEY,\n"
             "  title TEXT NOT NULL,\n"
             "  sha256 TEXT NOT NULL,\n"
             "  size_bytes INTEGER NOT NULL,\n"
             "  mtime REAL NOT NULL,\n"
             "  note_date TEXT,\n"
             "  date_source TEXT NOT NULL,\n"
             "  tags_json TEXT NOT NULL DEFAULT '[]',\n"
             "  links_json TEXT NOT NULL DEFAULT '[]',\n"
             "  status TEXT NOT NULL,              -- indexed | security_hold | stale\n"
             "  hold_reason TEXT,\n"
             "  updated_at TEXT NOT NULL\n"
             ");\n"
             "\n"
             "CREATE TABLE IF NOT EXISTS chunks (\n"
             "  id INTEGER PRIMARY KEY AUTOINCREMENT,\n"
             "  path TEXT NOT NULL REFERENCES notes(path) ON DELETE CASCADE,\n"
             "  idx INTEGER NOT NULL,\n"
             "  char_start INTEGER NOT NULL,\n"
             "  char_end INTEGER NOT NULL,\n"
             "  line_start INTEGER NOT NULL,\n"
             "  line_end INTEGER NOT NULL,\n"
             "  heading TEXT NOT NULL DEFAULT '',\n"
             "  chunk_date TEXT,\n"
             "  sha256 TEXT NOT NULL,\n"
             "  body TEXT NOT NULL,\n"
             "  summary TEXT NOT NULL DEFAULT '',\n"
             "  status TEXT NOT NULL DEFAULT 'pending',   -- pending | done | imported | error\n"
             "  attempts INTEGER NOT NULL DEFAULT 0,\n"
             "  error TEXT,\n"
             "  embedding BLOB,\n"
             "  updated_at TEXT NOT NULL,\n"
             "  UNIQUE(path, idx)\n"
             ");\n"
             "CREATE INDEX IF NOT EXISTS idx_chunks_status ON chunks(status);\n"
             "CREATE INDEX IF NOT EXISTS idx_chunks_sha ON chunks(sha256);\n"
             "\n"
             "CREATE TABLE IF NOT EXISTS facts (\n"
             "  id INTEGER PRIMARY KEY AUTOINCREMENT,\n"
             "  chunk_id INTEGER REFERENCES chunks(id) ON DELETE SET NULL,\n"
             "  path TEXT,                          -- NULL для фактов из диалога\n"
             "  source_ref TEXT NOT NULL,           -- путь#Lx-Ly или session:N\n"
             "  origin TEXT NOT NULL,               -- note | v3_import | chat | session\n"
             "  claim TEXT NOT NULL,\n"
             "  kind TEXT NOT NULL,\n"
             "  domain TEXT NOT NULL,\n"
             "  subject TEXT NOT NULL DEFAULT 'self',\n"
             "  explicitness TEXT NOT NULL DEFAULT 'explicit',\n"
             "  intent_status TEXT NOT NULL DEFAULT 'none',\n"
             "  event_date TEXT,                    -- когда это было (по тексту или дате заметки)\n"
             "  note_date TEXT,\n"
             "  date_reliable INTEGER NOT NULL DEFAULT 1,\n"
             "  evidence_quote TEXT NOT NULL DEFAULT '',\n"
             "  quote_match TEXT NOT NULL DEFAULT 'none',   -- exact | partial | none\n"
             "  sensitivity TEXT NOT NULL DEFAULT 'normal',\n"
             "  is_project INTEGER NOT NULL DEFAULT 0,\n"
             "  user_state TEXT NOT NULL DEFAULT 'none',    -- none | confirmed | rejected\n"
             "  cluster_id INTEGER,\n"
             "  embedding BLOB,\n"
             "  created_at TEXT NOT NULL\n"
             ");\n"
             "CREATE INDEX IF NOT EXISTS idx_facts_path ON facts(path);\n"
             "CREATE INDEX IF NOT EXISTS idx_facts_cluster ON facts(cluster_id);\n"
             "CREATE INDEX IF NOT EXISTS idx_facts_chunk ON facts(chunk_id);\n"
             "\n"
             "CREATE TABLE IF NOT EXISTS hypotheses_raw (\n"
             "  id INTEGER PRIMARY KEY AUTOINCREMENT,\n"
             "  chunk_id INTEGER REFERENCES chunks(id) ON DELETE CASCADE,\n"
             "  path TEXT NOT NULL,\n"
             "  source_ref TEXT NOT NULL,\n"
             "  hypothesis TEXT NOT NULL,\n"
             "  evidence_quote TEXT NOT NULL,\n"
             "  alternative TEXT NOT NULL DEFAULT '',\n"
             "  created_at TEXT NOT NULL\n"
             ");\n"
             "CREATE INDEX IF NOT EXISTS idx_hyp_chunk ON hypotheses_raw(chunk_id);\n"
             "CREATE INDEX IF NOT EXISTS idx_hyp_path ON hypotheses_raw(path);\n"
             "\n"
             "CREATE TABLE IF NOT EXISTS clusters (\n"
             "  id INTEGER PRIMARY KEY AUTOINCREMENT,\n"
             "  domain TEXT NOT NULL,\n"
             "  centroid BLOB,\n"
             "  size INTEGER NOT NULL DEFAULT 0,\n"
             "  support_notes INTEGER NOT NULL DEFAULT 0,\n"
             "  first_date TEXT,\n"
             "  last_date TEXT,\n"
             "  head_fact_id INTEGER,              -- самый свежий надёжный факт кластера\n"
             "  score REAL NOT NULL DEFAULT 0,\n"
             "  updated_at TEXT NOT NULL\n"
             ");\n"
             "\n"
             "-- Результаты синтеза (домены, профиль, аудит) с отпечатком входа для кэша.\n"
             "CREATE TABLE IF NOT EXISTS artifacts (\n"
             "  stage TEXT NOT NULL,\n"
             "  key TEXT NOT NULL,\n"
             "  fingerprint TEXT NOT NULL,\n"
             "  payload_json TEXT NOT NULL,\n"
             "  updated_at TEXT NOT NULL,\n"
             "  PRIMARY KEY(stage, key)\n"
             ");\n"
             "\n"
             "CREATE TABLE IF NOT EXISTS sessions (\n"
             "  id INTEGER PRIMARY KEY AUTOINCREMENT,\n"
             "  started_at TEXT NOT NULL,\n"
             "  ended_at TEXT,\n"
             "  title TEXT NOT NULL DEFAULT '',\n"
             "  summary TEXT NOT NULL DEFAULT '',\n"
             "  agreements_json TEXT NOT NULL DEFAULT '[]',\n"
             "  embedding BLOB\n"
             ");\n"
             "CREATE TABLE IF NOT EXISTS turns (\n"
             "  id INTEGER PRIMARY KEY AUTOINCREMENT,\n"
             "  session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,\n"
             "  role TEXT NOT NULL,                -- user | assistant\n"
             "  content TEXT NOT NULL,\n"
             "  refs_json TEXT NOT NULL DEFAULT '[]',\n"
             "  created_at TEXT NOT NULL\n"
             ");\n"
             "\n"
             "-- Предложения в память из диалогов и вопросы аудита: ждут решения пользователя.\n"
             "CREATE TABLE IF NOT EXISTS candidates (\n"
             "  id INTEGER PRIMARY KEY AUTOINCREMENT,\n"
             "  session_id INTEGER,\n"
             "  source TEXT NOT NULL,              -- chat | audit\n"
             "  claim TEXT NOT NULL,\n"
             "  kind TEXT NOT NULL,\n"
             "  domain TEXT NOT NULL DEFAULT 'other',\n"
             "  reason TEXT NOT NULL DEFAULT '',\n"
             "  target_fact_id INTEGER,            -- для вопросов аудита: о каком факте речь\n"
             "  status TEXT NOT NULL DEFAULT 'pending',   -- pending | accepted | rejected | skipped\n"
             "  created_at TEXT NOT NULL,\n"
             "  decided_at TEXT\n"
             ");\n"
             "\n"
             "-- Решения пользователя по утверждениям (подтвердил / отверг). Хранятся отдельно от\n"
             "-- фактов, поэтому переживают повторное извлечение заметок: новые факты, близкие\n"
             "-- по смыслу, получают ту же отметку.\n"
             "CREATE TABLE IF NOT EXISTS user_marks (\n"
             "  id INTEGER PRIMARY KEY AUTOINCREMENT,\n"
             "  claim TEXT NOT NULL,\n"
             "  mark TEXT NOT NULL,                -- confirmed | rejected\n"
             "  embedding BLOB,\n"
             "  created_at TEXT NOT NULL\n"
             ");\n"
             "\n"
             "-- Договорённости из разговоров: что человек решил сделать, со сроком и статусом.\n"
             "CREATE TABLE IF NOT EXISTS commitments (\n"
             "  id INTEGER PRIMARY KEY AUTOINCREMENT,\n"
             "  session_id INTEGER,\n"
             "  text TEXT NOT NULL,\n"
             "  due TEXT,                          -- срок, если звучал (ГГГГ-ММ-ДД)\n"
             "  status TEXT NOT NULL DEFAULT 'open',   -- open | done | dropped\n"
             "  note TEXT NOT NULL DEFAULT '',     -- как продвигается, почему бросил\n"
             "  created_at TEXT NOT NULL,\n"
             "  updated_at TEXT NOT NULL\n"
             ");\n"
             "CREATE INDEX IF NOT EXISTS idx_commitments_status ON commitments(status);\n"
             "\n"
             "-- Чек-ин: короткие ответы «как ты / что главное / что мешает» (своими словами).\n"
             "CREATE TABLE IF NOT EXISTS checkins (\n"
             "  id INTEGER PRIMARY KEY AUTOINCREMENT,\n"
             "  mood INTEGER,                      -- 1..10, если назвал\n"
             "  answers_json TEXT NOT NULL,        -- {\"mood\": \"...\", \"focus\": \"...\", \"worry\": \"...\"}\n"
             "  created_at TEXT NOT NULL\n"
             ");\n"
             "\n"
             "-- Люди из заметок и ответов. Строка живёт по имени и переживает повторное извлечение:\n"
             "-- твои ответы (кто это, в жизни ли сейчас) не теряются; связи с фактами пересобираются.\n"
             "CREATE TABLE IF NOT EXISTS people (\n"
             "  id INTEGER PRIMARY KEY AUTOINCREMENT,\n"
             "  name TEXT NOT NULL,\n"
             "  relation TEXT NOT NULL DEFAULT '',\n"
             "  relation_src TEXT NOT NULL DEFAULT '',   -- model | user\n"
             "  status TEXT NOT NULL DEFAULT 'unknown',  -- current | past | unknown | ignored (не человек / ошибка)\n"
             "  until TEXT,                              -- для past: примерно когда\n"
             "  note TEXT NOT NULL DEFAULT '',\n"
             "  merged_into INTEGER,                     -- это тот же человек, что people.id\n"
             "  mentions INTEGER NOT NULL DEFAULT 0,\n"
             "  first_date TEXT,\n"
             "  last_date TEXT,\n"
             "  created_at TEXT NOT NULL,\n"
             "  updated_at TEXT NOT NULL\n"
             ");\n"
             "CREATE UNIQUE INDEX IF NOT EXISTS idx_people_name ON people(name COLLATE NOCASE);\n"
             "CREATE TABLE IF NOT EXISTS fact_people (\n"
             "  fact_id INTEGER NOT NULL,\n"
             "  person_id INTEGER NOT NULL,\n"
             "  PRIMARY KEY (fact_id, person_id)\n"
             ");\n"
             "CREATE INDEX IF NOT EXISTS idx_fact_people_person ON fact_people(person_id);\n"
             "\n"
             "-- Режим присутствия (body double): над чем и сколько человек работал с коучем «рядом».\n"
             "CREATE TABLE IF NOT EXISTS focus_sessions (\n"
             "  id INTEGER PRIMARY KEY AUTOINCREMENT,\n"
             "  session_id INTEGER,\n"
             "  task TEXT NOT NULL DEFAULT '',\n"
             "  started_at TEXT NOT NULL,\n"
             "  ended_at TEXT,\n"
             "  minutes INTEGER NOT NULL DEFAULT 0\n"
             ");\n"
             "\n"
             "-- Оценка качества: твои вердикты по выборке. Хранится ключ пункта, а не сам текст,\n"
             "-- поэтому отчёт состоит только из чисел и его можно пересылать.\n"
             "CREATE TABLE IF NOT EXISTS eval_marks (\n"
             "  id INTEGER PRIMARY KEY AUTOINCREMENT,\n"
             "  target TEXT NOT NULL,              -- currency | fact | profile | reply\n"
             "  item_key TEXT NOT NULL,            -- хэш пункта: один пункт не показывается дважды\n"
             "  breakdown TEXT NOT NULL DEFAULT '',-- разрез отчёта: раздел профиля, источник актуальности, тип факта\n"
             "  verdict TEXT NOT NULL,\n"
             "  version TEXT NOT NULL,             -- версия кода / промптов / модель\n"
             "  created_at TEXT NOT NULL\n"
             ");\n"
             "CREATE INDEX IF NOT EXISTS idx_eval_target ON eval_marks(target, item_key);\n"
             "\n"
             "-- Актуальность: что из памяти верно сейчас, а что было раньше. Решения хранятся по\n"
             "-- формулировке (как user_marks) и переживают повторное извлечение заметок.\n"
             "CREATE TABLE IF NOT EXISTS currency_marks (\n"
             "  id INTEGER PRIMARY KEY AUTOINCREMENT,\n"
             "  claim TEXT NOT NULL,\n"
             "  status TEXT NOT NULL,              -- current | past\n"
             "  until TEXT,                        -- когда примерно закончилось (для past)\n"
             "  period TEXT,                       -- когда это было, если известно («2012–2016»)\n"
             "  source TEXT NOT NULL,              -- user — ответ человека; timeline — сверка с биографией и ответами\n"
             "  note TEXT NOT NULL DEFAULT '',\n"
             "  embedding BLOB,\n"
             "  created_at TEXT NOT NULL\n"
             ");\n"
             "\n"
             "-- Life Reset: реестр накопленного. Пункт живёт по смыслу (название + суть + вектор) и\n"
             "-- переживает повторное извлечение заметок: на `inventory build` новые группы фактов\n"
             "-- сверяются с пунктами по вектору, статус и история решений не теряются.\n"
             "CREATE TABLE IF NOT EXISTS lr_items (\n"
             "  id INTEGER PRIMARY KEY AUTOINCREMENT,\n"
             "  title TEXT NOT NULL,\n"
             "  type TEXT NOT NULL,                 -- commitment | project | idea | wish | question | situation | routine | material | experience\n"
             "  status TEXT NOT NULL DEFAULT 'inbox',\n"
             "  domain TEXT NOT NULL DEFAULT 'other',\n"
             "  summary TEXT NOT NULL DEFAULT '',\n"
             "  want TEXT NOT NULL DEFAULT '',      -- исходное желание: чего тогда хотелось\n"
             "  hint TEXT NOT NULL DEFAULT '',      -- что видно по памяти (кодом): «в заметках отмечено как сделанное»\n"
             "  origin TEXT NOT NULL DEFAULT 'memory',   -- memory | chat\n"
             "  sensitive INTEGER NOT NULL DEFAULT 0,\n"
             "  first_date TEXT,\n"
             "  last_date TEXT,                     -- последнее упоминание в заметках и разговорах\n"
             "  mentions INTEGER NOT NULL DEFAULT 0,\n"
             "  notes_count INTEGER NOT NULL DEFAULT 0,\n"
             "  next_step TEXT NOT NULL DEFAULT '',\n"
             "  review_on TEXT,                     -- когда пересмотреть (someday / incubating / waiting)\n"
             "  touched TEXT,                       -- когда последний раз менялся файл заметки-источника (для заметок без дат)\n"
             "  resurfaced INTEGER NOT NULL DEFAULT 0,   -- закрытый пункт снова появился в заметках\n"
             "  src_sig TEXT,                       -- отпечаток проектных заметок-источников на момент решения\n"
             "  merged_into INTEGER,\n"
             "  fact_ids_json TEXT NOT NULL DEFAULT '[]',\n"
             "  paths_json TEXT NOT NULL DEFAULT '[]',\n"
             "  anchors_json TEXT NOT NULL DEFAULT '[]',  -- «note:путь»: проектная заметка – всегда этот пункт, как бы её ни назвали\n"
             "  embedding BLOB,\n"
             "  created_at TEXT NOT NULL,\n"
             "  updated_at TEXT NOT NULL,\n"
             "  decided_at TEXT\n"
             ");\n"
             "CREATE INDEX IF NOT EXISTS idx_lr_items_status ON lr_items(status);\n"
             "\n"
             "-- Журнал решений: исходное состояние, предложение коуча, решение человека и причина.\n"
             "CREATE TABLE IF NOT EXISTS lr_decisions (\n"
             "  id INTEGER PRIMARY KEY AUTOINCREMENT,\n"
             "  item_id INTEGER NOT NULL,\n"
             "  from_status TEXT NOT NULL,\n"
             "  to_status TEXT NOT NULL,\n"
             "  proposal TEXT NOT NULL DEFAULT '',  -- что предложил коуч (если предлагал)\n"
             "  answer TEXT NOT NULL DEFAULT '',    -- ответ человека своими словами\n"
             "  reason TEXT NOT NULL DEFAULT '',\n"
             "  created_at TEXT NOT NULL\n"
             ");\n"
             "CREATE INDEX IF NOT EXISTS idx_lr_decisions_item ON lr_decisions(item_id);\n"
             "\n"
             "-- Аудит жизни по областям: один раунд — одна дата, по строке на область.\n"
             "CREATE TABLE IF NOT EXISTS lr_audit (\n"
             "  id INTEGER PRIMARY KEY AUTOINCREMENT,\n"
             "  round INTEGER NOT NULL,\n"
             "  area TEXT NOT NULL,\n"
             "  satisfaction INTEGER,               -- 1..10: насколько устраивает сейчас\n"
             "  importance INTEGER,                 -- 1..10: насколько важно\n"
             "  change TEXT NOT NULL DEFAULT '',    -- yes | no | maybe: хочу ли менять\n"
             "  nature TEXT NOT NULL DEFAULT '',    -- problem | should | '': настоящая проблема или «так должно быть»\n"
             "  note TEXT NOT NULL DEFAULT '',\n"
             "  created_at TEXT NOT NULL,\n"
             "  UNIQUE(round, area)\n"
             ");\n"
             "\n"
             "-- Психологические гипотезы: не факты и не диагнозы. Подтверждённая — это описание\n"
             "-- повторяемости, которое человек признал, а не черта личности.\n"
             "CREATE TABLE IF NOT EXISTS lr_hypotheses (\n"
             "  id INTEGER PRIMARY KEY AUTOINCREMENT,\n"
             "  text TEXT NOT NULL,\n"
             "  observations_json TEXT NOT NULL DEFAULT '[]',   -- [{\"quote\", \"ref\", \"date\"}]\n"
             "  alternatives_json TEXT NOT NULL DEFAULT '[]',\n"
             "  question TEXT NOT NULL DEFAULT '',  -- вопрос для проверки\n"
             "  confidence TEXT NOT NULL DEFAULT 'low',          -- low | medium | high\n"
             "  status TEXT NOT NULL DEFAULT 'new',              -- new | testing | pattern | rejected\n"
             "  user_note TEXT NOT NULL DEFAULT '',\n"
             "  embedding BLOB,\n"
             "  created_at TEXT NOT NULL,\n"
             "  checked_at TEXT\n"
             ");\n")

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

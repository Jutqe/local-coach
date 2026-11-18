"""Настройки по умолчанию. Всё переопределяется переменными окружения или флагами."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from .i18n import trl


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


DEFAULT_VAULT_CANDIDATES: tuple[Path, ...] = ()
# База, память и импорт — вне vault: заметки только читаются.
DEFAULT_WORKSPACE = Path(os.environ.get("LOCAL_COACH_WORKSPACE", "~/.local-coach")).expanduser()

EXCLUDED_DIRS = {
    ".obsidian", ".trash", ".git", "node_modules", ".cache",
    "__pycache__", "venv", ".venv", "attachments-cache",
}
# Папка в vault, куда коуч пишет итоги Life Reset (единственное место, где он пишет в vault).
LIFE_RESET_DIR = "Life Reset"

# Папки, факты из которых считаются проектными идеями, а не целями жизни.
PROJECT_DIR_NAMES = {
    "проекты", "проект", "projects", "project",
    "идеи", "ideas", "бизнес-идеи", "business ideas",
}

DOMAINS = (
    "work_career", "health_wellbeing", "relationships_social",
    "personal_growth", "life_logistics", "finance", "hobbies_interests",
    "projects_creative", "learning_knowledge", "identity_values",
    "administrative_records", "other",
)
DOMAIN_TITLES = {
    "work_career": trl("Работа и карьера"),
    "health_wellbeing": trl("Здоровье и самочувствие"),
    "relationships_social": trl("Отношения и люди"),
    "personal_growth": trl("Личное развитие"),
    "life_logistics": trl("Быт и организация жизни"),
    "finance": trl("Финансы"),
    "hobbies_interests": trl("Хобби и интересы"),
    "projects_creative": trl("Проекты и творчество"),
    "learning_knowledge": trl("Учёба и знания"),
    "identity_values": trl("Ценности и самоопределение"),
    "administrative_records": trl("Документы и администрирование"),
    "other": trl("Прочее"),
}
# Домены, которые не попадают в чат и синтез: там обычно реквизиты и документы.
PRIVATE_DOMAINS = {"administrative_records"}

FACT_KINDS = (
    "fact", "event", "state", "goal", "decision", "preference", "value",
    "constraint", "habit", "strategy", "strength", "concern", "open_loop",
)
KIND_TITLES = {
    "fact": trl("факт"), "event": trl("событие"), "state": trl("состояние"), "goal": trl("цель"),
    "decision": trl("решение"), "preference": trl("предпочтение"), "value": trl("ценность"),
    "constraint": trl("ограничение"), "habit": trl("привычка"), "strategy": trl("стратегия"),
    "strength": trl("сильная сторона"), "concern": trl("тревога/забота"),
    "open_loop": trl("незакрытое дело"),
}
# Статус намерения: для целей, решений и незакрытых дел. Остальным — none.
INTENT_STATUSES = ("none", "idea", "wanted", "planned", "in_progress", "completed", "abandoned", "unclear")
SUBJECTS = ("self", "other_person", "general")

# --- Life Reset --------------------------------------------------------------------------
# Что накопилось: не всё — задача. Тип определяет, какие вопросы задавать при разборе.
LR_TYPES = ("commitment", "project", "idea", "wish", "question", "situation", "routine", "material", "experience")
LR_TYPE_TITLES = {
    "commitment": trl("обязательство"), "project": trl("незаконченный проект"), "idea": trl("идея"), "wish": trl("желание"),
    "question": trl("нерешённый вопрос"), "situation": trl("незакрытая ситуация"), "routine": trl("повторяющаяся обязанность"),
    "material": trl("знание и материал"), "experience": trl("жизненный опыт"),
}
# Судьба пункта. abandoned — осознанно оставлено (закрыто как дело, но остаётся в биографии);
# archived — сохранено как история или справка; dismissed — вообще не пункт (ошибка извлечения).
LR_STATUSES = ("inbox", "clarification", "active", "incubating", "someday", "waiting", "delegated",
               "completed", "abandoned", "archived", "dismissed")
LR_STATUS_TITLES = {
    "inbox": trl("не разобрано"), "clarification": trl("уточнить"), "active": trl("активно"), "incubating": trl("инкубатор"),
    "someday": trl("когда-нибудь"), "waiting": trl("жду"), "delegated": trl("передано"), "completed": trl("завершено"),
    "abandoned": trl("отпущено"), "archived": trl("в архиве"), "dismissed": trl("не пункт"),
}
LR_OPEN = {"inbox", "clarification"}                       # ещё не решено
LR_PARKED = {"incubating", "someday", "waiting"}            # решено, но с пересмотром
LR_CLOSED = {"completed", "abandoned", "archived", "delegated"}
# Двенадцать областей аудита и домены памяти, из которых коуч показывает известное.
LR_AREAS = (
    ("health", trl("Здоровье"), trl("физическое состояние, энергия, сон"), ("health_wellbeing",)),
    ("mind", trl("Психологическое состояние"), trl("эмоциональное благополучие, устойчивость"), ("health_wellbeing", "personal_growth")),
    ("relationships", trl("Отношения"), trl("партнёрство, семья, друзья"), ("relationships_social",)),
    ("career", trl("Карьера"), trl("работа, профессиональное развитие"), ("work_career",)),
    ("finance", trl("Финансы"), trl("доход, капитал, безопасность"), ("finance",)),
    ("place", trl("Место жизни"), trl("страна, город, жильё, среда"), ("life_logistics",)),
    ("education", trl("Образование"), trl("знания, навыки, интеллектуальное развитие"), ("learning_knowledge",)),
    ("creativity", trl("Творчество"), trl("свои проекты"), ("projects_creative",)),
    ("rest", trl("Отдых"), trl("развлечения, путешествия, восстановление"), ("hobbies_interests",)),
    ("social", trl("Социальная жизнь"), trl("сообщество, общение, вклад"), ("relationships_social",)),
    ("meaning", trl("Самореализация"), trl("смысл, самоопределение, личные амбиции"), ("identity_values", "personal_growth")),
    ("everyday", trl("Повседневность"), trl("быт, порядок, привычки, организация"), ("life_logistics",)),
)
EXPLICITNESS = ("explicit", "inferred")


@dataclass
class Config:
    vault: Path | None = None
    workspace: Path = DEFAULT_WORKSPACE
    ollama: str = _env("LOCAL_COACH_OLLAMA", "http://localhost:11434")
    # Если Ollama не ответил на своём порту (например, 11434 занят и его запустили на 11435), коуч ищет
    # на том же адресе среди этих портов и запоминает найденный (ollama_found.txt в рабочей папке).
    ollama_ports: str = _env("LOCAL_COACH_OLLAMA_PORTS", "11434-11439")
    # Запасной Ollama, когда ПК выключен: та же модель на процессоре сервера. Пусто — без запасного.
    # Только для разговора, чек-ина, review и forget: update на процессоре шёл бы сутками.
    ollama_fallback: str = _env("LOCAL_COACH_OLLAMA_FALLBACK", "")
    # На процессоре память читается ~35 токенов/с: в чате берём меньше (системный блок ~11 тыс. символов).
    fallback_chat_budget: int = _env_int("LOCAL_COACH_FALLBACK_CHAT_BUDGET", 18000)
    on_fallback: bool = False
    model: str = _env("LOCAL_COACH_MODEL", "qwen3.5:9b-q4_K_M")
    embed_model: str = _env("LOCAL_COACH_EMBED_MODEL", "embeddinggemma:latest")
    # «Тяжёлая» модель для вычислений, где важно качество, а не отклик: извлечение фактов, синтез профиля,
    # сверка с биографией, названия и отбор пунктов Life Reset, гипотезы, люди. Чат и быстрые вызовы
    # (переписывание запроса, итог сессии, «запомни», понимание ответа в разборе, реплики фокуса) остаются
    # на model: там важна задержка. Пусто – всё на model, как раньше.
    heavy_model: str = _env("LOCAL_COACH_HEAVY_MODEL", "")
    # Отдельно для синтеза профиля и сверки с биографией; пусто – как heavy_model (а если и она пуста – model).
    synth_model: str = _env("LOCAL_COACH_SYNTH_MODEL", "")
    # Как называть человека в профиле; пусто — берётся из базы (coach.py name), иначе «он».
    user_name: str = _env("LOCAL_COACH_NAME", "")

    # Один размер контекста на все вызовы: смена num_ctx заставляет Ollama
    # перезагружать модель.
    num_ctx: int = _env_int("LOCAL_COACH_NUM_CTX", 16384)
    keep_alive: str = _env("LOCAL_COACH_KEEP_ALIVE", "30m")
    think_extract: bool = _env("LOCAL_COACH_THINK_EXTRACT", "0") == "1"
    think_synth: bool = _env("LOCAL_COACH_THINK_SYNTH", "0") == "1"
    think_chat: bool = _env("LOCAL_COACH_THINK_CHAT", "0") == "1"
    chat_timing: bool = _env("LOCAL_COACH_CHAT_TIMING", "0") == "1"   # печатать время ответа в чате

    # Фрагменты: около 2 тыс. токенов. Меньше, чем в v3 (12,5 тыс. символов),
    # чтобы 9B-модель не теряла факты в плотных дневниковых записях.
    chunk_chars: int = _env_int("LOCAL_COACH_CHUNK_CHARS", 7000)
    chunk_min_chars: int = 200
    chunk_overlap_chars: int = 400
    max_facts_per_chunk: int = _env_int("LOCAL_COACH_MAX_FACTS", 15)
    extract_predict: int = _env_int("LOCAL_COACH_EXTRACT_PREDICT", 3000)
    max_extract_attempts: int = 3

    embed_batch: int = 32
    # Порог косинусной близости для склейки фактов в один кластер. Замер на embeddinggemma
    # (256 измерений): перефразировки 0.93–0.98, «брат/сестра живёт в Москве» 0.88,
    # «хочет бросить курить / бросил» 0.84, разные темы 0.70–0.75.
    cluster_threshold: float = _env_float("LOCAL_COACH_CLUSTER_THRESHOLD", 0.90)
    # Порог, выше которого новый факт наследует твоё решение (подтвердил или отверг) по похожему.
    mark_threshold: float = 0.90

    synth_clusters_per_domain: int = _env_int("LOCAL_COACH_SYNTH_CLUSTERS", 45)
    synth_predict: int = _env_int("LOCAL_COACH_SYNTH_PREDICT", 4500)

    # Бюджет контекста чата в символах (≈3,5 символа на токен для русского).
    chat_budget_chars: int = _env_int("LOCAL_COACH_CHAT_BUDGET", 26000)
    chat_predict: int = _env_int("LOCAL_COACH_CHAT_PREDICT", 1500)
    chat_facts: int = 25
    # Векторные совпадения слабее этого порога в разговор не попадают (полнотекстовые — попадают).
    chat_min_similarity: float = _env_float("LOCAL_COACH_MIN_SIMILARITY", 0.38)
    chat_excerpts: int = 3
    chat_excerpt_chars: int = 1600
    chat_sessions: int = 2
    # Режим присутствия (/focus): реплика не реже, чем раз в столько секунд (и не чаще, чем вдвое чаще).
    focus_every: int = _env_int("LOCAL_COACH_FOCUS_EVERY", 120)

    project_dirs: set[str] = field(default_factory=lambda: set(PROJECT_DIR_NAMES))

    @property
    def heavy_name(self) -> str:
        return self.heavy_model or self.model

    @property
    def synth_name(self) -> str:
        return self.synth_model or self.heavy_model or self.model

    @property
    def db_path(self) -> Path:
        return self.workspace / "coach.sqlite3"

    @property
    def imports_dir(self) -> Path:
        """Импортированные файлы (таблицы, документы) — как заметки, вне vault."""
        return self.workspace / "imports"

    @property
    def memory_dir(self) -> Path:
        return self.workspace / "memory"

"""Эвристический фильтр учётных данных.

Заметка, похожая на хранилище паролей или содержащая токен, целиком
не отправляется модели и не индексируется. Совпавший текст нигде не выводится.
"""
from __future__ import annotations

import re

CREDENTIAL_NAME_TERMS = (
    "пароли", "пароль", "passwords", "password manager", "credentials",
    "api keys", "ключи доступа", "токены доступа", "secrets",
)

SECRET_PATTERNS = (
    # Кавычки вокруг ключа и значения — чтобы ловить и JSON: {"password": "…"}.
    re.compile(r"""(?i)\b(?:пароль|password|passwd|client_secret|api[_ -]?key|access[_ -]?token|refresh[_ -]?token)["']?\s*[:=]\s*["']?[^\s,;"'{}]{4,}"""),
    re.compile(r"(?i)\b(?:пароль|password|passwd)\s+(?:от|для|аккаунта|уч[её]тной\s+записи|account|is|это)\s*[:=]?\s*[^\s,;]{4,}"),
    # Шаблон v3 «аккаунт … пароль … любое слово» убран: он срабатывал на обычных
    # фразах («у аккаунта нет пароля, поэтому…») и молча выкидывал заметки.
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\b(?:sk-[A-Za-z0-9_-]{16,}|ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|xox[baprs]-[A-Za-z0-9-]{15,})\b"),
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{20,}"),
    # Токен Telegram-бота: 123456789:AA…
    re.compile(r"\b\d{8,12}:[A-Za-z0-9_-]{30,}\b"),
    # Номер банковской карты: 16 цифр группами по 4.
    re.compile(r"(?<!\d)(?:\d{4}[ -]){3}\d{4}(?!\d)"),
)


def name_hold(relpath: str, title: str) -> str | None:
    name = (relpath + " " + title).casefold()
    for term in CREDENTIAL_NAME_TERMS:
        if term in name:
            return "путь или название похоже на хранилище учётных данных"
    return None


def text_hold(text: str) -> str | None:
    for pattern in SECRET_PATTERNS:
        if pattern.search(text or ""):
            return "найден возможный пароль, токен, ключ или номер карты"
    return None


def note_hold(relpath: str, title: str, content: str) -> str | None:
    return name_hold(relpath, title) or text_hold(content)
